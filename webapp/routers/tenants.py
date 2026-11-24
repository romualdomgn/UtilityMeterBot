"""Lease contract and tenant management endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

import config
from database import crud, operations
from database.models import format_property_short
from services.access import user_can_access_apartment
from services.notifications import notify_tenant_added, notify_tenant_removed
from webapp.auth import WebAppUser, get_current_user
from webapp.schemas import ContractBody, TenantCreateBody, parse_iso_date, require_role

router = APIRouter()


@router.put("/api/apartments/{apt_id}/contract")
async def api_update_contract(
    apt_id: int, body: ContractBody, user: WebAppUser = Depends(get_current_user)
):
    require_role(user, ("admin", "landlord"))
    if not await user_can_access_apartment(user.telegram_id, apt_id):
        raise HTTPException(403, "Нет доступа")

    date_start = parse_iso_date(body.agreement_date_start)
    date_finish = parse_iso_date(body.agreement_date_finish)
    if date_start and date_finish and date_start > date_finish:
        raise HTTPException(400, "Дата начала договора не может быть позже даты окончания")

    updates = {
        "agreement_no": (body.agreement_no or "").strip() or None,
        "agreement_date_start": date_start,
        "agreement_date_finish": date_finish,
    }
    if body.has_tenant is not None:
        updates["has_tenant"] = body.has_tenant

    await crud.update_apartment(apt_id, **updates)
    await crud.add_audit_log(user.telegram_id, "update_contract", f"Объект id={apt_id}")
    return {"ok": True}


@router.delete("/api/apartments/{apt_id}/contract")
async def api_delete_contract(apt_id: int, user: WebAppUser = Depends(get_current_user)):
    require_role(user, ("admin", "landlord"))
    if not await user_can_access_apartment(user.telegram_id, apt_id):
        raise HTTPException(403, "Нет доступа")

    await crud.update_apartment(
        apt_id,
        agreement_no=None,
        agreement_date_start=None,
        agreement_date_finish=None,
    )
    await crud.add_audit_log(user.telegram_id, "delete_contract", f"Договор объекта id={apt_id} удалён")
    return {"ok": True}


@router.post("/api/apartments/{apt_id}/tenants")
async def api_add_tenant(
    apt_id: int,
    body: TenantCreateBody,
    request: Request,
    user: WebAppUser = Depends(get_current_user),
):
    require_role(user, ("admin", "landlord"))
    if not await user_can_access_apartment(user.telegram_id, apt_id):
        raise HTTPException(403, "Нет доступа")
    if body.telegram_id <= 0:
        raise HTTPException(400, "Telegram ID должен быть положительным числом")
    if body.telegram_id == config.ADMIN_TELEGRAM_ID:
        raise HTTPException(400, "Нельзя добавить администратора в качестве нанимателя")
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Имя нанимателя не может быть пустым")

    apt = await crud.get_apartment(apt_id)
    if not apt:
        raise HTTPException(404, "Объект недвижимости не найден")

    existing = await crud.get_user_by_telegram_id(body.telegram_id)
    if existing:
        if existing.role != "tenant":
            role_map = {"admin": "администратор", "landlord": "владелец", "revoked": "отозван"}
            raise HTTPException(
                400,
                f"Пользователь ID {body.telegram_id} уже добавлен ({role_map.get(existing.role, existing.role)})",
            )
        if existing.is_blocked:
            raise HTTPException(400, f"Пользователь ID {body.telegram_id} заблокирован")
        if existing.family_id is not None and existing.family_id != apt.family_id:
            raise HTTPException(400, "Этот наниматель уже привязан к другой семье")
        if await crud.is_user_linked_to_apartment(apt_id, existing.id):
            raise HTTPException(400, "Этот наниматель уже привязан к данному объекту")

    prop_label = format_property_short(apt)
    has_contract = all([apt.agreement_no, apt.agreement_date_start, apt.agreement_date_finish])
    if not has_contract and not body.force_no_contract:
        return {
            "needs_confirm": True,
            "warning": f"Данные договора для объекта {prop_label} не заполнены. Всё равно добавить нанимателя и отправить приглашение?",
        }

    phone = (body.phone or "").strip() or None
    target_user, _ = await crud.get_or_create_user(
        telegram_id=body.telegram_id,
        name=name,
        phone=phone,
        role="tenant",
        family_id=apt.family_id,
    )
    await crud.link_user_to_apartment(apt_id, target_user.id, role="tenant")
    await crud.update_apartment(apt_id, has_tenant=True)
    await crud.add_audit_log(
        user.telegram_id, "add_tenant",
        f"Наниматель {target_user.name or name} (ID: {body.telegram_id}) добавлен в {prop_label}",
    )
    notified = await notify_tenant_added(
        request.app.state.bot, apt, body.telegram_id, target_user.name or name
    )
    return {"ok": True, "notified": notified}


@router.delete("/api/apartments/{apt_id}/tenants/{tenant_tg_id}")
async def api_remove_tenant(
    apt_id: int,
    tenant_tg_id: int,
    request: Request,
    user: WebAppUser = Depends(get_current_user),
):
    require_role(user, ("admin", "landlord"))
    if not await user_can_access_apartment(user.telegram_id, apt_id):
        raise HTTPException(403, "Нет доступа")

    try:
        prop_label, fully_removed = await operations.remove_tenant_from_apartment_atomic(
            apartment_id=apt_id,
            tenant_tg_id=tenant_tg_id,
            actor_tg_id=user.telegram_id,
        )
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except LookupError as e:
        raise HTTPException(404, str(e))

    await notify_tenant_removed(
        request.app.state.bot,
        tenant_tg_id,
        apt_number=prop_label,
        fully_removed=fully_removed,
    )
    return {"ok": True, "fully_removed": fully_removed}

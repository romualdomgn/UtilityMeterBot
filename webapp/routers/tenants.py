"""Lease contract and tenant management endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from config import sync_id_to_env
from database import crud
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

    updates = {
        "agreement_no": (body.agreement_no or "").strip() or None,
        "agreement_date_start": parse_iso_date(body.agreement_date_start),
        "agreement_date_finish": parse_iso_date(body.agreement_date_finish),
    }
    if body.has_tenant is not None:
        updates["has_tenant"] = body.has_tenant

    await crud.update_apartment(apt_id, **updates)
    await crud.add_audit_log(user.telegram_id, "update_contract", f"Кв. id={apt_id}")
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
    await crud.add_audit_log(user.telegram_id, "delete_contract", f"Договор кв. id={apt_id} удалён")
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
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Имя нанимателя не может быть пустым")

    existing = await crud.get_user_by_telegram_id(body.telegram_id)
    if existing:
        role_map = {"admin": "администратор", "landlord": "владелец", "tenant": "арендатор"}
        raise HTTPException(
            400,
            f"Пользователь ID {body.telegram_id} уже добавлен ({role_map.get(existing.role, existing.role)})",
        )

    apt = await crud.get_apartment(apt_id)
    if not apt:
        raise HTTPException(404, "Квартира не найдена")

    has_contract = all([apt.agreement_no, apt.agreement_date_start, apt.agreement_date_finish])
    if not has_contract and not body.force_no_contract:
        return {
            "needs_confirm": True,
            "warning": f"Данные договора для кв. №{apt.number} не заполнены. Всё равно добавить нанимателя и отправить приглашение?",
        }

    phone = (body.phone or "").strip() or None
    new_user, _ = await crud.get_or_create_user(
        telegram_id=body.telegram_id,
        name=name,
        phone=phone,
        role="tenant",
        family_id=apt.family_id,
    )
    sync_id_to_env("tenant", body.telegram_id, remove=False)
    await crud.link_user_to_apartment(apt_id, new_user.id, role="tenant")
    await crud.update_apartment(apt_id, has_tenant=True)
    await crud.add_audit_log(
        user.telegram_id, "add_tenant",
        f"Наниматель {name} (ID: {body.telegram_id}) добавлен в кв. №{apt.number}",
    )
    await notify_tenant_added(request.app.state.bot, apt, body.telegram_id, name)
    return {"ok": True}


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

    tenant = await crud.get_user_by_telegram_id(tenant_tg_id)
    if tenant:
        await crud.unlink_user_from_apartment(apt_id, tenant.id)
        if not await crud.get_apartments_for_user(tenant_tg_id):
            await crud.delete_user(tenant_tg_id)
            sync_id_to_env("tenant", tenant_tg_id, remove=True)
        await notify_tenant_removed(request.app.state.bot, tenant_tg_id)

    if not await crud.get_apartment_tenants(apt_id):
        await crud.update_apartment(apt_id, has_tenant=False)

    await crud.add_audit_log(
        user.telegram_id, "remove_tenant",
        f"Наниматель ID={tenant_tg_id} удалён из кв. id={apt_id}",
    )
    return {"ok": True}

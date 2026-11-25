"""Lease contract and tenant management endpoints with atomic isolation."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

import config
from database import operations
from database.models import format_property_short
from services.access import is_admin, user_can_access_apartment
from services.notifications import (
    dispatch_notification,
    notify_tenant_added,
    notify_tenant_removed,
)
from webapp.auth import WebAppUser, get_current_user
from webapp.schemas import (
    ContractBody,
    TenantCreateBody,
    TenantUpdateBody,
    parse_iso_date,
    require_role,
)

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

    try:
        await operations.update_contract_atomic(apt_id, updates, user.telegram_id)
    except LookupError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@router.delete("/api/apartments/{apt_id}/contract")
async def api_delete_contract(
    apt_id: int,
    request: Request = None,
    unlink_tenants: bool = Query(default=False),
    user: WebAppUser = Depends(get_current_user),
):
    require_role(user, ("admin", "landlord"))
    if not await user_can_access_apartment(user.telegram_id, apt_id):
        raise HTTPException(403, "Нет доступа")

    try:
        prop_label, affected_tenants = await operations.delete_contract_atomic(
            apt_id, user.telegram_id, unlink_tenants=unlink_tenants
        )
    except LookupError as e:
        raise HTTPException(404, str(e))

    if request is not None and affected_tenants:
        for tenant_tg_id, fully_removed in affected_tenants:
            await dispatch_notification(
                request,
                notify_tenant_removed(
                    request.app.state.bot,
                    tenant_tg_id,
                    apt_number=prop_label,
                    fully_removed=fully_removed,
                ),
                name=f"notify_tenant_removed_{tenant_tg_id}",
            )

    return {"ok": True, "unlinked_tenants": len(affected_tenants)}


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
    if config.ADMIN_TELEGRAM_ID and body.telegram_id == config.ADMIN_TELEGRAM_ID:
        raise HTTPException(400, "Нельзя добавить администратора в качестве нанимателя")
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Имя нанимателя не может быть пустым")
    if len(name) > 120:
        raise HTTPException(400, "Имя слишком длинное (максимум 120 символов)")

    phone = (body.phone or "").strip() or None
    try:
        needs_confirm, apt, display_name = await operations.add_tenant_atomic(
            apartment_id=apt_id,
            telegram_id=body.telegram_id,
            name=name,
            phone=phone,
            actor_tg_id=user.telegram_id,
            force_no_contract=body.force_no_contract,
        )
    except operations.ConflictError as e:
        raise HTTPException(409, str(e))
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))

    if needs_confirm:
        prop_label = format_property_short(apt)
        return {
            "needs_confirm": True,
            "warning": f"Данные договора для объекта {prop_label} не заполнены. Всё равно добавить нанимателя и отправить приглашение?",
        }

    notified = await notify_tenant_added(
        request.app.state.bot, apt, body.telegram_id, display_name
    )
    return {"ok": True, "notified": notified}


@router.put("/api/tenants/{tenant_tg_id}")
async def api_update_tenant(
    tenant_tg_id: int,
    body: TenantUpdateBody,
    user: WebAppUser = Depends(get_current_user),
):
    require_role(user, ("admin", "landlord"))
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Имя не может быть пустым")
    if len(name) > 120:
        raise HTTPException(400, "Имя слишком длинное (максимум 120 символов)")

    update_phone = "phone" in body.model_fields_set
    phone_val = (body.phone or "").strip() or None if update_phone else None

    try:
        updated = await operations.update_tenant_profile_atomic(
            tenant_tg_id=tenant_tg_id,
            name=name,
            phone=phone_val,
            update_phone=update_phone,
            actor_tg_id=user.telegram_id,
            actor_family_id=user.family_id,
            is_actor_admin=is_admin(user.telegram_id),
        )
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except LookupError as e:
        raise HTTPException(404, str(e))

    return {"ok": True, "name": updated.name, "phone": updated.phone}


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

    await dispatch_notification(
        request,
        notify_tenant_removed(
            request.app.state.bot,
            tenant_tg_id,
            apt_number=prop_label,
            fully_removed=fully_removed,
        ),
        name=f"notify_tenant_removed_{tenant_tg_id}",
    )
    return {"ok": True, "fully_removed": fully_removed}

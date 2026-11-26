"""Lease contract and tenant management endpoints with atomic isolation."""

from __future__ import annotations

import calendar
import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request

import config
from database import operations
from database.models import (
    RENT_REMINDER_AFTER_DAYS,
    RENT_REMINDER_MODES,
    format_property_short,
)
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

LONG_TERM_CONTRACT_WARNING = (
    "Срок договора больше года! Если срок договора превышает 12 месяцев, "
    "возникающее обременение права собственности (факт долгосрочного найма) нужно зарегистрировать в Росреестре! "
    "Безопасный срок договора найма не более 11 месяцев с дальнейшим продлением проживания по новому договору!"
)


def is_contract_over_one_year(
    date_start: datetime.date | None,
    date_finish: datetime.date | None,
) -> bool:
    """Return True if the period between start and finish strictly exceeds 1 calendar year."""
    if not date_start or not date_finish:
        return False
    max_day = calendar.monthrange(date_start.year + 1, date_start.month)[1]
    one_year_later = datetime.date(date_start.year + 1, date_start.month, min(date_start.day, max_day))
    return date_finish > one_year_later


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

    updates: dict = {
        "agreement_no": (body.agreement_no or "").strip() or None,
        "agreement_date_start": date_start,
        "agreement_date_finish": date_finish,
    }
    if body.has_tenant is not None:
        updates["has_tenant"] = body.has_tenant

    payment_fields_touched = bool(
        body.model_fields_set
        & {"rent_payment_day", "rent_reminder_enabled", "rent_reminder_mode", "rent_reminder_days_after"}
    )
    if payment_fields_touched:
        pay_day = body.rent_payment_day
        if pay_day is not None and not (1 <= pay_day <= 31):
            raise HTTPException(400, "Дата ежемесячного платежа должна быть числом от 1 до 31")
        updates["rent_payment_day"] = pay_day

        rem_enabled = bool(body.rent_reminder_enabled)
        if rem_enabled:
            if pay_day is None:
                raise HTTPException(
                    400,
                    "Укажите дату ежемесячного платежа, чтобы включить напоминание об оплате",
                )
            mode = body.rent_reminder_mode
            if mode not in RENT_REMINDER_MODES:
                raise HTTPException(400, "Выберите вариант напоминания об оплате")
            days_after = body.rent_reminder_days_after if mode == RENT_REMINDER_AFTER_DAYS else None
            if mode == RENT_REMINDER_AFTER_DAYS:
                if days_after is None or not (1 <= days_after <= 20):
                    raise HTTPException(
                        400,
                        "Укажите количество дней после даты платежа (от 1 до 20)",
                    )
            updates["rent_reminder_enabled"] = True
            updates["rent_reminder_mode"] = mode
            updates["rent_reminder_days_after"] = days_after
        else:
            updates["rent_reminder_enabled"] = False
            updates["rent_reminder_mode"] = None
            updates["rent_reminder_days_after"] = None

    try:
        await operations.update_contract_atomic(apt_id, updates, user.telegram_id)
    except LookupError as e:
        raise HTTPException(404, str(e))
    resp: dict = {"ok": True}
    if is_contract_over_one_year(date_start, date_finish):
        resp["long_term_warning"] = LONG_TERM_CONTRACT_WARNING
    return resp


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

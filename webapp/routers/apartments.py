"""Profile, properties (real estate), tariffs, and initial readings endpoints."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Request

from config import local_today
from database import crud, operations
from database.models import (
    ALL_METERS,
    METER_LABELS,
    format_property_short,
    get_apartment_enabled_meters,
)
from services.access import get_accessible_apartments, is_admin, user_can_access_apartment
from services.calculations import calculate_billing, get_prev_value
from services.notifications import notify_tenant_removed
from webapp.auth import WebAppUser, get_current_user
from webapp.schemas import (
    ApartmentCreateBody,
    ApartmentSettingsBody,
    InitialReadingsBody,
    ProfileUpdateBody,
    require_role,
    serialize_apartment,
)

router = APIRouter()
MAX_TARIFF_RATE = Decimal("1000000")


def _validate_enabled_meters(raw_meters: list[str] | None) -> list[str] | None:
    if raw_meters is None:
        return None
    valid = [m for m in ALL_METERS if m in set(raw_meters)]
    if not valid:
        raise HTTPException(400, "Выберите хотя бы один счётчик для объекта")
    return valid


@router.get("/api/me")
async def api_me(user: WebAppUser = Depends(get_current_user)):
    apts = await get_accessible_apartments(user.telegram_id)
    return {
        "user": {
            "telegram_id": user.telegram_id,
            "name": user.name,
            "role": user.role,
            "family_id": user.family_id,
        },
        "apartments": [serialize_apartment(a) for a in apts],
    }


@router.put("/api/me/profile")
async def api_update_own_profile(
    body: ProfileUpdateBody,
    user: WebAppUser = Depends(get_current_user),
):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Имя не может быть пустым")
    if len(name) > 120:
        raise HTTPException(400, "Имя слишком длинное (максимум 120 символов)")

    updated = await crud.update_user_name(user.telegram_id, name)
    if not updated:
        raise HTTPException(404, "Профиль пользователя не найден")

    await crud.add_audit_log(
        user.telegram_id,
        "update_own_name",
        f"Пользователь ID={user.telegram_id} изменил имя на '{name}'",
    )
    return {"ok": True, "name": updated.name}


@router.get("/api/apartments/{apt_id}")
async def api_apartment_detail(apt_id: int, user: WebAppUser = Depends(get_current_user)):
    if not await user_can_access_apartment(user.telegram_id, apt_id):
        raise HTTPException(403, "Нет доступа к объекту недвижимости")
    apt = await crud.get_apartment(apt_id)
    if not apt:
        raise HTTPException(404, "Объект недвижимости не найден")

    enabled_meters = get_apartment_enabled_meters(apt)
    tariffs_raw = await crud.get_all_tariffs(apt_id, as_of_date=local_today())
    tariffs = {
        mt: float(tariffs_raw[mt].rate) if tariffs_raw.get(mt) else None
        for mt in ALL_METERS
    }
    initial = await crud.get_all_initial_readings(apt_id)
    prev_vals = {
        mt: (await get_prev_value(apt_id, mt) if mt in enabled_meters else None)
        for mt in ALL_METERS
    }
    billing = await calculate_billing(apt_id)

    tenants = (
        [
            {"telegram_id": t.telegram_id, "name": t.name, "phone": t.phone}
            for t in await crud.get_apartment_tenants(apt_id)
        ]
        if user.role in ("admin", "landlord")
        else []
    )

    readings_count = await crud.get_readings_count(apt_id) if user.role in ("admin", "landlord") else 0

    return {
        "apartment": serialize_apartment(apt),
        "tariffs": tariffs,
        "initial_readings": initial,
        "prev_readings": prev_vals,
        "billing": billing.to_dict(),
        "tenants": tenants,
        "readings_count": readings_count,
    }


@router.post("/api/apartments")
async def api_create_apartment(body: ApartmentCreateBody, user: WebAppUser = Depends(get_current_user)):
    require_role(user, ("admin", "landlord"))
    number = body.number.strip()
    if not number:
        raise HTTPException(400, "Укажите номер или название объекта")
    if len(number) > 50:
        raise HTTPException(400, "Название/номер объекта не длиннее 50 символов")

    enabled_meters = _validate_enabled_meters(body.enabled_meters)

    if is_admin(user.telegram_id):
        family_id = body.family_id
        if family_id is not None:
            if not await crud.get_family(family_id):
                raise HTTPException(404, "Указанная семья не найдена")
        else:
            fams = await crud.get_all_families()
            family_id = fams[0].id if fams else (await crud.create_family("Основная семья")).id
    else:
        family_id = user.family_id
        if not family_id:
            raise HTTPException(400, "Администратор должен сначала привязать вас к семье")

    apt = await crud.create_apartment(
        number=number,
        family_id=family_id,
        property_type=body.property_type,
        enabled_meters=enabled_meters,
    )
    await crud.add_audit_log(
        user.telegram_id,
        "create_apartment",
        f"Создан объект {format_property_short(apt)} (id={apt.id}, type={apt.property_type}, family_id={family_id})",
    )
    return {"ok": True, "apartment": serialize_apartment(apt)}

@router.delete("/api/apartments/{apt_id}")
async def api_delete_apartment(
    apt_id: int,
    request: Request,
    user: WebAppUser = Depends(get_current_user),
):
    require_role(user, ("admin", "landlord"))
    if not await user_can_access_apartment(user.telegram_id, apt_id):
        raise HTTPException(403, "Нет доступа к объекту недвижимости")

    try:
        prop_label, affected_tenants = await operations.delete_apartment_atomic(
            apartment_id=apt_id,
            actor_tg_id=user.telegram_id,
        )
    except LookupError as e:
        raise HTTPException(404, str(e))

    for tenant_tg_id, fully_removed in affected_tenants:
        await notify_tenant_removed(
            request.app.state.bot,
            tenant_tg_id,
            apt_number=prop_label,
            fully_removed=fully_removed,
        )

    return {
        "ok": True,
        "deleted": prop_label,
        "affected_tenants": len(affected_tenants),
    }



@router.put("/api/apartments/{apt_id}/settings")
async def api_update_settings(
    apt_id: int, body: ApartmentSettingsBody, user: WebAppUser = Depends(get_current_user)
):
    require_role(user, ("admin", "landlord"))
    if not await user_can_access_apartment(user.telegram_id, apt_id):
        raise HTTPException(403, "Нет доступа")

    for val in (body.water_submission_day, body.electricity_submission_day, body.gas_submission_day):
        if val is not None and not (1 <= val <= 31):
            raise HTTPException(400, "День подачи должен быть от 1 до 31")

    clean_number: str | None = None
    if body.number is not None:
        clean_number = body.number.strip()
        if not clean_number:
            raise HTTPException(400, "Номер или название объекта не может быть пустым")
        if len(clean_number) > 50:
            raise HTTPException(400, "Название/номер объекта не длиннее 50 символов")

    enabled_meters = _validate_enabled_meters(body.enabled_meters)

    parsed_tariffs: dict[str, Decimal] = {}
    for mt, raw_rate in body.tariffs.items():
        if mt not in ALL_METERS or raw_rate in (None, ""):
            continue
        try:
            rate = Decimal(str(raw_rate).strip().replace(",", "."))
            if not rate.is_finite() or rate <= 0 or rate > MAX_TARIFF_RATE:
                raise ValueError
        except (InvalidOperation, ValueError):
            raise HTTPException(400, f"Некорректный тариф для {mt}")
        parsed_tariffs[mt] = rate

    try:
        updated_tariffs = await operations.update_apartment_settings_atomic(
            apartment_id=apt_id,
            water_submission_day=body.water_submission_day,
            electricity_submission_day=body.electricity_submission_day,
            gas_submission_day=body.gas_submission_day,
            tariffs=parsed_tariffs,
            changed_by=user.telegram_id,
            effective_from=local_today(),
            number=clean_number,
            property_type=body.property_type,
            enabled_meters=enabled_meters,
        )
    except LookupError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "updated_tariffs": updated_tariffs}


@router.get("/api/apartments/{apt_id}/tariffs/history")
async def api_tariff_history(apt_id: int, user: WebAppUser = Depends(get_current_user)):
    if not await user_can_access_apartment(user.telegram_id, apt_id):
        raise HTTPException(403, "Нет доступа")
    rows = await crud.get_tariff_history(apt_id)
    return {
        "items": [
            {
                "id": r.id,
                "tariff_type": r.tariff_type,
                "label": METER_LABELS.get(r.tariff_type, r.tariff_type),
                "rate": float(r.rate),
                "effective_from": r.effective_from.strftime("%d.%m.%Y"),
                "changed_by": r.changed_by,
            }
            for r in rows
        ]
    }


@router.put("/api/apartments/{apt_id}/initial-readings")
async def api_update_initial_readings(
    apt_id: int, body: InitialReadingsBody, user: WebAppUser = Depends(get_current_user)
):
    require_role(user, ("admin", "landlord"))
    if not await user_can_access_apartment(user.telegram_id, apt_id):
        raise HTTPException(403, "Нет доступа")

    readings_map: dict[str, int] = {}
    for mt in ALL_METERS:
        val = getattr(body, mt, None)
        if val is None:
            continue
        if val < 0:
            raise HTTPException(400, f"Начальное показание {mt} не может быть отрицательным")
        readings_map[mt] = val

    if readings_map:
        try:
            await operations.update_initial_readings_atomic(apt_id, readings_map, user.telegram_id)
        except LookupError as e:
            raise HTTPException(404, str(e))
    return {"ok": True}

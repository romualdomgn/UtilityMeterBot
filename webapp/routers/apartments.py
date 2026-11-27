"""Profile, properties (real estate), meters/replacements, tariffs, and initial readings endpoints."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Request

import config
from config import local_today
from database import crud, operations
from database.models import (
    ALL_METERS,
    ELECTRICITY_METERS,
    ALL_TARIFF_TYPES,
    IRRIGATION_METERS,
    IRRIGATION_MODE_AFTER_COLD,
    LEGACY_METERS,
    MULTI_ELECTRICITY_METERS,
    SEWERAGE_TARIFF,
    SINGLE_ELECTRICITY_METER,
    TARIFF_LABELS,
    get_apartment_enabled_meters,
    get_apartment_irrigation_mode,
    resolve_user_display_name,
)
from services.access import (
    get_accessible_apartments,
    has_overdue_meters_for_apartment,
    is_admin,
    user_can_access_apartment,
)
from services.calculations import (
    calculate_billing,
    get_month_base_info,
    get_prev_value,
    sanitize_billing_for_tenant,
)
from services.notifications import dispatch_notification, notify_tenant_removed
from webapp.auth import WebAppUser, get_current_user
from webapp.schemas import (
    ApartmentCreateBody,
    ApartmentReorderBody,
    ApartmentSettingsBody,
    InitialReadingsBody,
    MeterDevicesUpdateBody,
    MeterReplacementBody,
    ProfileUpdateBody,
    parse_iso_date,
    require_role,
    serialize_apartment,
    serialize_meter_device,
)

router = APIRouter()
MAX_TARIFF_RATE = Decimal("1000000")


def _validate_enabled_meters(
    raw_meters: list[str] | None,
    property_type: str = "apartment",
    irrigation_mode: str = "parallel",
) -> list[str] | None:
    if raw_meters is None:
        return None
    valid = [m for m in ALL_METERS if m in set(raw_meters)]
    if not valid:
        raise HTTPException(400, "Выберите хотя бы один счётчик для объекта")
    if SINGLE_ELECTRICITY_METER in valid and any(m in valid for m in MULTI_ELECTRICITY_METERS):
        raise HTTPException(400, "Нельзя одновременно выбрать «Один тариф (Т)» и тарифы Т1, Т2, Т3")
    if property_type != "house" and any(m in valid for m in IRRIGATION_METERS):
        raise HTTPException(400, "Счётчик «Полив» доступен только для типа недвижимости «Дом»")
    if (
        any(m in valid for m in IRRIGATION_METERS)
        and irrigation_mode == IRRIGATION_MODE_AFTER_COLD
        and "cold_water" not in valid
    ):
        raise HTTPException(400, "Режим «Полив после ХВС» требует включённого счётчика ХВС")
    return valid


def _validate_effective_configuration(
    property_type: str,
    effective_meters: list[str],
    irrigation_mode: str,
) -> None:
    _validate_enabled_meters(
        effective_meters,
        property_type=property_type,
        irrigation_mode=irrigation_mode,
    )


def _build_canonical_meter_devices(enabled_meters: list[str], devices_map: dict) -> list[dict]:
    """Build physical meter list: T1/T2/T3 collapse into one 't' electricity meter device."""
    result: list[dict] = []
    elec_added = False
    for mt in enabled_meters:
        if mt in ELECTRICITY_METERS:
            if elec_added:
                continue
            elec_added = True
            dev = (
                devices_map.get(SINGLE_ELECTRICITY_METER)
                or devices_map.get("t1")
                or devices_map.get("t2")
                or devices_map.get("t3")
            )
            result.append(serialize_meter_device(SINGLE_ELECTRICITY_METER, dev))
        else:
            result.append(serialize_meter_device(mt, devices_map.get(mt)))
    return result


@router.get("/api/me")
async def api_me(user: WebAppUser = Depends(get_current_user)):
    apts = await get_accessible_apartments(user.telegram_id)
    tenant_apt_ids = await crud.get_apartment_ids_with_tenants() if user.role != "tenant" else set()
    serialized = []
    for a in apts:
        overdue = await has_overdue_meters_for_apartment(a)
        serialized.append(
            serialize_apartment(
                a,
                has_linked_tenants=(a.id in tenant_apt_ids),
                has_overdue_meters=overdue,
            )
        )
    return {
        "user": {
            "telegram_id": user.telegram_id,
            "name": user.name,
            "role": user.role,
            "family_id": user.family_id,
        },
        "apartments": serialized,
        "app_info": {
            "repository": config.REPOSITORY,
            "tg_username": config.TG_USERNAME,
        },
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

    try:
        updated = await operations.update_own_profile_atomic(user.telegram_id, name, is_actor_admin=is_admin(user.telegram_id))
    except LookupError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "name": updated.name}


@router.put("/api/apartments/reorder")
async def api_reorder_apartments(
    body: ApartmentReorderBody,
    user: WebAppUser = Depends(get_current_user),
):
    require_role(user, ("admin", "landlord"))
    requested_ids = list(dict.fromkeys(body.apartment_ids))
    if not requested_ids:
        raise HTTPException(400, "Список объектов не может быть пустым")

    accessible = await get_accessible_apartments(user.telegram_id)
    accessible_ids = {a.id for a in accessible}
    if any(aid not in accessible_ids for aid in requested_ids):
        raise HTTPException(403, "Нет доступа к одному из объектов недвижимости")

    full_order = requested_ids + [a.id for a in accessible if a.id not in set(requested_ids)]
    updated_ids = await operations.reorder_apartments_atomic(full_order, user.telegram_id)
    return {"ok": True, "apartment_ids": updated_ids}


@router.get("/api/apartments/{apt_id}")
async def api_apartment_detail(apt_id: int, user: WebAppUser = Depends(get_current_user)):
    if not await user_can_access_apartment(user.telegram_id, apt_id):
        raise HTTPException(403, "Нет доступа к объекту недвижимости")
    apt = await crud.get_apartment(apt_id)
    if not apt:
        raise HTTPException(404, "Объект недвижимости не найден")

    is_tenant = user.role == "tenant"
    enabled_meters = get_apartment_enabled_meters(apt)
    initial = await crud.get_all_initial_readings(apt_id)
    prev_vals = {
        mt: (await get_prev_value(apt_id, mt) if mt in enabled_meters else None)
        for mt in ALL_METERS
    }
    month_base_info = await get_month_base_info(apt_id)
    billing = await calculate_billing(apt_id)
    overdue = await has_overdue_meters_for_apartment(apt)

    if is_tenant:
        return {
            "apartment": serialize_apartment(apt, has_linked_tenants=True, has_overdue_meters=overdue),
            "initial_readings": initial,
            "prev_readings": prev_vals,
            "month_base_info": month_base_info,
            "billing": billing.to_dict(),
            "tenants": [],
            "readings_count": 0,
        }

    tariffs_raw = await crud.get_all_tariffs(apt_id, as_of_date=local_today())
    tariffs = {
        mt: float(tariffs_raw[mt].rate) if tariffs_raw.get(mt) else None
        for mt in ALL_TARIFF_TYPES
    }
    tenants = [
        {"telegram_id": t.telegram_id, "name": resolve_user_display_name(t, user.role, user.telegram_id)}
        for t in await crud.get_apartment_tenants(apt_id)
    ]
    readings_count = await crud.get_readings_count(apt_id)
    devices_map = await crud.get_meter_devices(apt_id)
    meter_devices = _build_canonical_meter_devices(enabled_meters, devices_map)

    return {
        "apartment": serialize_apartment(apt, has_linked_tenants=bool(tenants), has_overdue_meters=overdue),
        "tariffs": tariffs,
        "initial_readings": initial,
        "prev_readings": prev_vals,
        "month_base_info": month_base_info,
        "billing": billing.to_dict(),
        "tenants": tenants,
        "readings_count": readings_count,
        "meter_devices": meter_devices,
    }


@router.post("/api/apartments")
async def api_create_apartment(body: ApartmentCreateBody, user: WebAppUser = Depends(get_current_user)):
    require_role(user, ("admin", "landlord"))
    number = body.number.strip()
    if not number:
        raise HTTPException(400, "Укажите номер или название объекта")
    if len(number) > 50:
        raise HTTPException(400, "Название/номер объекта не длиннее 50 символов")

    enabled_meters = _validate_enabled_meters(
        body.enabled_meters,
        property_type=body.property_type,
        irrigation_mode=body.irrigation_mode,
    )
    effective_meters = enabled_meters if enabled_meters is not None else list(LEGACY_METERS)
    _validate_effective_configuration(body.property_type, effective_meters, body.irrigation_mode)

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

    apt = await operations.create_apartment_atomic(
        number=number,
        family_id=family_id,
        property_type=body.property_type,
        enabled_meters=enabled_meters,
        irrigation_mode=body.irrigation_mode,
        actor_tg_id=user.telegram_id,
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

    existing_apt = await crud.get_apartment(apt_id)
    if not existing_apt:
        raise HTTPException(404, "Объект недвижимости не найден")

    effective_ptype = body.property_type or existing_apt.property_type or "apartment"
    effective_irr_mode = body.irrigation_mode or get_apartment_irrigation_mode(existing_apt)
    enabled_meters = _validate_enabled_meters(
        body.enabled_meters,
        property_type=effective_ptype,
        irrigation_mode=effective_irr_mode,
    )
    final_meters = enabled_meters if enabled_meters is not None else get_apartment_enabled_meters(existing_apt)
    _validate_effective_configuration(effective_ptype, final_meters, effective_irr_mode)

    parsed_tariffs: dict[str, Decimal] = {}
    for mt, raw_rate in body.tariffs.items():
        if mt not in ALL_TARIFF_TYPES or raw_rate in (None, ""):
            continue
        if mt in IRRIGATION_METERS and effective_irr_mode == IRRIGATION_MODE_AFTER_COLD:
            continue
        try:
            rate = Decimal(str(raw_rate).strip().replace(",", "."))
            too_small = rate < Decimal("0") if mt == SEWERAGE_TARIFF else rate <= Decimal("0")
            if not rate.is_finite() or too_small or rate > MAX_TARIFF_RATE:
                raise ValueError
        except (InvalidOperation, ValueError):
            raise HTTPException(400, f"Некорректный тариф для {mt}")
        parsed_tariffs[mt] = rate

    eff_wday = (
        body.water_submission_day
        if "water_submission_day" in body.model_fields_set
        else existing_apt.water_submission_day
    )
    eff_eday = (
        body.electricity_submission_day
        if "electricity_submission_day" in body.model_fields_set
        else existing_apt.electricity_submission_day
    )
    eff_gday = (
        body.gas_submission_day
        if "gas_submission_day" in body.model_fields_set
        else existing_apt.gas_submission_day
    )

    try:
        updated_tariffs = await operations.update_apartment_settings_atomic(
            apartment_id=apt_id,
            water_submission_day=eff_wday,
            electricity_submission_day=eff_eday,
            gas_submission_day=eff_gday,
            tariffs=parsed_tariffs,
            changed_by=user.telegram_id,
            effective_from=local_today(),
            number=clean_number,
            property_type=body.property_type,
            enabled_meters=enabled_meters,
            irrigation_mode=body.irrigation_mode,
        )
    except LookupError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "updated_tariffs": updated_tariffs}


@router.get("/api/apartments/{apt_id}/tariffs/history")
async def api_tariff_history(apt_id: int, user: WebAppUser = Depends(get_current_user)):
    require_role(user, ("admin", "landlord"))
    if not await user_can_access_apartment(user.telegram_id, apt_id):
        raise HTTPException(403, "Нет доступа")
    rows = await crud.get_tariff_history(apt_id)
    return {
        "items": [
            {
                "id": r.id,
                "tariff_type": r.tariff_type,
                "label": TARIFF_LABELS.get(r.tariff_type, r.tariff_type),
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


@router.get("/api/apartments/{apt_id}/meters")
async def api_get_meter_devices(apt_id: int, user: WebAppUser = Depends(get_current_user)):
    require_role(user, ("admin", "landlord"))
    if not await user_can_access_apartment(user.telegram_id, apt_id):
        raise HTTPException(403, "Нет доступа")
    apt = await crud.get_apartment(apt_id)
    if not apt:
        raise HTTPException(404, "Объект недвижимости не найден")

    enabled = get_apartment_enabled_meters(apt)
    devices_map = await crud.get_meter_devices(apt_id)
    replacements = await crud.get_meter_replacements(apt_id, limit=20)
    return {
        "devices": _build_canonical_meter_devices(enabled, devices_map),
        "replacements": [
            {
                "id": r.id,
                "meter_type": r.meter_type,
                "replacement_date": r.replacement_date.strftime("%d.%m.%Y"),
                "old_final_value": r.old_final_value,
                "new_initial_value": r.new_initial_value,
                "old_serial_number": r.old_serial_number,
                "new_serial_number": r.new_serial_number,
            }
            for r in replacements
        ],
    }


@router.put("/api/apartments/{apt_id}/meters")
async def api_update_meter_devices(
    apt_id: int,
    body: MeterDevicesUpdateBody,
    user: WebAppUser = Depends(get_current_user),
):
    require_role(user, ("admin", "landlord"))
    if not await user_can_access_apartment(user.telegram_id, apt_id):
        raise HTTPException(403, "Нет доступа")

    parsed_items = []
    for item in body.items:
        due = parse_iso_date(item.verification_due_date)
        parsed_items.append({
            "meter_type": item.meter_type,
            "serial_number": (item.serial_number or "").strip() or None,
            "verification_due_date": due,
            "notify_intervals": list(item.notify_intervals),
        })

    try:
        updated = await operations.upsert_meter_devices_atomic(apt_id, parsed_items, user.telegram_id)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))

    return {"ok": True, "updated": updated}


@router.post("/api/apartments/{apt_id}/meters/replace")
async def api_replace_meter(
    apt_id: int,
    body: MeterReplacementBody,
    user: WebAppUser = Depends(get_current_user),
):
    require_role(user, ("admin", "landlord"))
    if not await user_can_access_apartment(user.telegram_id, apt_id):
        raise HTTPException(403, "Нет доступа")

    due = parse_iso_date(body.new_verification_due_date)
    try:
        if body.tariff_entries:
            reps = await operations.replace_meter_batch_atomic(
                apartment_id=apt_id,
                entries=[e.model_dump() for e in body.tariff_entries],
                actor_tg_id=user.telegram_id,
                replacement_date=local_today(),
                new_serial_number=body.new_serial_number,
                new_verification_due_date=due,
            )
            rep_id = reps[0].id if reps else None
        else:
            if body.old_final_value is None or body.new_initial_value is None:
                raise HTTPException(400, "Укажите конечное показание старого и начальное показание нового счётчика")
            rep = await operations.replace_meter_atomic(
                apartment_id=apt_id,
                meter_type=body.meter_type,
                old_final_value=body.old_final_value,
                new_initial_value=body.new_initial_value,
                actor_tg_id=user.telegram_id,
                replacement_date=local_today(),
                new_serial_number=body.new_serial_number,
                new_verification_due_date=due,
            )
            rep_id = rep.id
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))

    billing = await calculate_billing(apt_id)
    return {
        "ok": True,
        "replacement_id": rep_id,
        "billing": billing.to_dict(),
    }

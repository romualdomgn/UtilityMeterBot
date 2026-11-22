"""Profile, apartments, tariffs, and initial readings endpoints."""

from __future__ import annotations

import datetime
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException

from database import crud
from database.models import ALL_METERS, METER_LABELS
from services.access import get_accessible_apartments, is_admin, user_can_access_apartment
from services.calculations import calculate_billing, get_prev_value
from webapp.auth import WebAppUser, get_current_user
from webapp.schemas import (
    ApartmentCreateBody,
    ApartmentSettingsBody,
    InitialReadingsBody,
    require_role,
    serialize_apartment,
)

router = APIRouter()


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


@router.get("/api/apartments/{apt_id}")
async def api_apartment_detail(apt_id: int, user: WebAppUser = Depends(get_current_user)):
    if not await user_can_access_apartment(user.telegram_id, apt_id):
        raise HTTPException(403, "Нет доступа к квартире")
    apt = await crud.get_apartment(apt_id)
    if not apt:
        raise HTTPException(404, "Квартира не найдена")

    tariffs_raw = await crud.get_all_tariffs(apt_id)
    tariffs = {
        mt: float(tariffs_raw[mt].rate) if tariffs_raw.get(mt) else None
        for mt in ALL_METERS
    }
    initial = await crud.get_all_initial_readings(apt_id)
    prev_vals = {mt: await get_prev_value(apt_id, mt) for mt in ALL_METERS}
    billing = await calculate_billing(apt_id)
    tenants = await crud.get_apartment_tenants(apt_id)

    return {
        "apartment": serialize_apartment(apt),
        "tariffs": tariffs,
        "initial_readings": initial,
        "prev_readings": prev_vals,
        "billing": billing.to_dict(),
        "tenants": [
            {"telegram_id": t.telegram_id, "name": t.name, "phone": t.phone}
            for t in tenants
        ],
    }


@router.post("/api/apartments")
async def api_create_apartment(body: ApartmentCreateBody, user: WebAppUser = Depends(get_current_user)):
    require_role(user, ("admin", "landlord"))
    number = body.number.strip()
    if not number:
        raise HTTPException(400, "Номер квартиры не может быть пустым")

    if is_admin(user.telegram_id):
        family_id = body.family_id
        if not family_id:
            fams = await crud.get_all_families()
            family_id = fams[0].id if fams else (await crud.create_family("Основная семья")).id
    else:
        family_id = user.family_id
        if not family_id:
            raise HTTPException(400, "Администратор должен сначала привязать вас к семье")

    apt = await crud.create_apartment(number=number, family_id=family_id)
    await crud.add_audit_log(
        user.telegram_id, "create_apartment",
        f"Создана квартира №{apt.number} (id={apt.id}, family_id={family_id})",
    )
    return {"ok": True, "apartment": serialize_apartment(apt)}


@router.put("/api/apartments/{apt_id}/settings")
async def api_update_settings(
    apt_id: int, body: ApartmentSettingsBody, user: WebAppUser = Depends(get_current_user)
):
    require_role(user, ("admin", "landlord"))
    if not await user_can_access_apartment(user.telegram_id, apt_id):
        raise HTTPException(403, "Нет доступа")

    updates = {}
    for field_name in ("water_submission_day", "electricity_submission_day"):
        val = getattr(body, field_name)
        if val is not None and not (1 <= val <= 31):
            raise HTTPException(400, "День подачи должен быть от 1 до 31")
        updates[field_name] = val
    await crud.update_apartment(apt_id, **updates)

    current_tariffs = await crud.get_all_tariffs(apt_id)
    today = datetime.date.today()
    for mt, raw_rate in body.tariffs.items():
        if mt not in ALL_METERS or raw_rate in (None, ""):
            continue
        try:
            rate = Decimal(str(raw_rate).strip().replace(",", "."))
            if rate <= 0:
                raise ValueError
        except (InvalidOperation, ValueError):
            raise HTTPException(400, f"Некорректный тариф для {mt}")

        existing = current_tariffs.get(mt)
        if not existing or Decimal(existing.rate) != rate:
            await crud.set_tariff(apt_id, mt, rate, today, user.telegram_id)

    await crud.add_audit_log(user.telegram_id, "update_settings", f"Кв. id={apt_id}")
    return {"ok": True}


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

    for mt in ALL_METERS:
        val = getattr(body, mt)
        if val is None:
            continue
        if val < 0:
            raise HTTPException(400, f"Начальное показание {mt} не может быть отрицательным")
        await crud.set_initial_reading(apt_id, mt, val, user.telegram_id)

    await crud.add_audit_log(user.telegram_id, "set_initial_readings", f"Кв. id={apt_id}")
    return {"ok": True}

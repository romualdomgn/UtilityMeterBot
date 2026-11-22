"""Meter readings submission, history, correction, and photo upload endpoints."""

from __future__ import annotations

import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile

from config import PHOTOS_DIR
from database import crud
from database.models import METER_LABELS, METER_UNITS
from services.access import user_can_access_apartment
from services.calculations import calculate_billing, get_prev_value
from services.notifications import forward_meter_photo_file, notify_new_readings
from webapp.auth import WebAppUser, get_current_user
from webapp.schemas import (
    ElectricityReadingBody,
    ReadingCorrectionBody,
    WaterReadingBody,
    require_role,
)

router = APIRouter()


async def _save_meter_group(
    request: Request,
    apt_id: int,
    meters: dict[str, int],
    force: bool,
    action: str,
    audit_details: str,
    user: WebAppUser,
) -> dict:
    if not await user_can_access_apartment(user.telegram_id, apt_id):
        raise HTTPException(403, "Нет доступа к квартире")
    if any(v < 0 for v in meters.values()):
        raise HTTPException(400, "Показания должны быть неотрицательными целыми числами")

    prev_meters = {mt: await get_prev_value(apt_id, mt) for mt in meters}
    decreased = [mt for mt, v in meters.items() if v < prev_meters[mt]]
    billing = await calculate_billing(apt_id, current_readings=meters)

    if decreased and not force:
        return {
            "needs_confirm": True,
            "warning": "Введённое значение меньше предыдущего (возможна замена или переполнение счётчика). Подтвердите сохранение.",
            "billing": billing.to_dict(),
        }

    today = datetime.date.today()
    for mt, val in meters.items():
        await crud.add_reading(apt_id, mt, val, user.telegram_id, today)

    await crud.add_audit_log(user.telegram_id, action, audit_details)
    await notify_new_readings(request.app.state.bot, apt_id, meters, prev_meters, user.telegram_id)
    return {"ok": True, "billing": billing.to_dict()}


@router.post("/api/apartments/{apt_id}/readings/water")
async def api_submit_water(
    apt_id: int,
    body: WaterReadingBody,
    request: Request,
    user: WebAppUser = Depends(get_current_user),
):
    meters = {"cold_water": body.cold_water, "hot_water": body.hot_water}
    return await _save_meter_group(
        request, apt_id, meters, body.force, "submit_water",
        f"Кв. id={apt_id}: ХВС={body.cold_water}, ГВС={body.hot_water}", user,
    )


@router.post("/api/apartments/{apt_id}/readings/electricity")
async def api_submit_electricity(
    apt_id: int,
    body: ElectricityReadingBody,
    request: Request,
    user: WebAppUser = Depends(get_current_user),
):
    meters = {"t1": body.t1, "t2": body.t2, "t3": body.t3}
    return await _save_meter_group(
        request, apt_id, meters, body.force, "submit_electricity",
        f"Кв. id={apt_id}: T1={body.t1}, T2={body.t2}, T3={body.t3}", user,
    )


@router.get("/api/apartments/{apt_id}/history")
async def api_history(
    apt_id: int,
    year: int | None = Query(default=None),
    month: int | None = Query(default=None),
    user: WebAppUser = Depends(get_current_user),
):
    if not await user_can_access_apartment(user.telegram_id, apt_id):
        raise HTTPException(403, "Нет доступа")

    months = await crud.get_reading_months(apt_id)
    if year and month:
        rows = await crud.get_readings_by_month(apt_id, year, month)
        billing = (await calculate_billing(apt_id, year=year, month=month)).to_dict()
    else:
        rows = await crud.get_all_readings(apt_id, limit=60)
        billing = (await calculate_billing(apt_id)).to_dict()

    return {
        "months": [{"year": y, "month": m} for y, m in months],
        "billing": billing,
        "readings": [
            {
                "id": r.id,
                "meter_type": r.meter_type,
                "label": METER_LABELS.get(r.meter_type, r.meter_type),
                "unit": METER_UNITS.get(r.meter_type, ""),
                "value": r.value,
                "reading_date": r.reading_date.strftime("%d.%m.%Y"),
                "submitted_by": r.submitted_by,
            }
            for r in rows
        ],
    }


@router.put("/api/readings/{reading_id}")
async def api_correct_reading(
    reading_id: int, body: ReadingCorrectionBody, user: WebAppUser = Depends(get_current_user)
):
    require_role(user, ("admin", "landlord"))
    if body.value < 0:
        raise HTTPException(400, "Значение не может быть отрицательным")
    reading = await crud.get_reading_by_id(reading_id)
    if not reading:
        raise HTTPException(404, "Показание не найдено")
    if not await user_can_access_apartment(user.telegram_id, reading.apartment_id):
        raise HTTPException(403, "Нет доступа")

    old_val = reading.value
    await crud.update_reading(reading_id, body.value)
    await crud.add_audit_log(
        user.telegram_id, "correct_reading",
        f"Показание #{reading_id}: {old_val} → {body.value}",
    )
    return {"ok": True, "old_value": old_val, "new_value": body.value}


@router.post("/api/apartments/{apt_id}/photo")
async def api_upload_photo(
    apt_id: int,
    request: Request,
    file: UploadFile = File(...),
    comment: str = Form(default=""),
    user: WebAppUser = Depends(get_current_user),
):
    if not await user_can_access_apartment(user.telegram_id, apt_id):
        raise HTTPException(403, "Нет доступа к квартире")

    content = await file.read()
    if not content or len(content) > 15 * 1024 * 1024:
        raise HTTPException(400, "Файл пуст или превышает 15 МБ")

    ext = Path(file.filename or "photo.jpg").suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = PHOTOS_DIR / f"apt{apt_id}_{user.telegram_id}_{ts}{ext}"
    filepath.write_bytes(content)

    sent = await forward_meter_photo_file(
        bot=request.app.state.bot,
        apt_id=apt_id,
        filepath=filepath,
        sender_tg_id=user.telegram_id,
        sender_name=user.name,
        comment=comment,
    )
    await crud.add_audit_log(
        user.telegram_id, "upload_photo_webapp",
        f"Кв. id={apt_id}, получателей: {sent}",
    )
    return {"ok": True, "recipients_count": sent}

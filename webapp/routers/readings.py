"""Meter readings submission, history, correction, and streamed photo upload endpoints."""

from __future__ import annotations

import asyncio
import datetime
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile

from config import BOT_TZ, MAX_PHOTO_BYTES, PHOTOS_DIR, local_today
from database import crud, operations
from database.models import (
    METER_LABELS,
    METER_PLAIN_LABELS,
    METER_UNITS,
    format_property_short,
    get_apartment_enabled_meters,
)
from services.access import user_can_access_apartment
from services.calculations import calculate_billing, get_prev_value
from services.notifications import forward_meter_photo_file, notify_new_readings
from webapp.auth import WebAppUser, get_current_user
from webapp.schemas import (
    ElectricityReadingBody,
    GasReadingBody,
    ReadingCorrectionBody,
    WaterReadingBody,
    require_role,
)

router = APIRouter()
CHUNK_SIZE = 64 * 1024


async def _save_meter_group(
    request: Request,
    apt_id: int,
    meters: dict[str, int],
    force: bool,
    action: str,
    user: WebAppUser,
) -> dict:
    if not await user_can_access_apartment(user.telegram_id, apt_id):
        raise HTTPException(403, "Нет доступа к объекту недвижимости")
    if not meters:
        raise HTTPException(400, "Укажите хотя бы одно показание счётчика")
    if any(v < 0 for v in meters.values()):
        raise HTTPException(400, "Показания должны быть неотрицательными целыми числами")

    apt = await crud.get_apartment(apt_id)
    if not apt:
        raise HTTPException(404, "Объект недвижимости не найден")

    enabled = set(get_apartment_enabled_meters(apt))
    for mt in meters:
        if mt not in enabled:
            label = METER_PLAIN_LABELS.get(mt, mt)
            raise HTTPException(400, f"Счётчик «{label}» не подключён для этого объекта")

    prev_meters = {mt: await get_prev_value(apt_id, mt) for mt in meters}
    decreased = [
        mt for mt, v in meters.items()
        if prev_meters[mt] is not None and v < prev_meters[mt]
    ]
    billing = await calculate_billing(apt_id, current_readings=meters)

    if decreased and not force:
        return {
            "needs_confirm": True,
            "warning": "Введённое значение меньше предыдущего (возможна замена или переполнение счётчика). Подтвердите сохранение.",
            "billing": billing.to_dict(),
        }

    parts = ", ".join(f"{METER_PLAIN_LABELS.get(mt, mt)}={val}" for mt, val in meters.items())
    audit_details = f"{format_property_short(apt)} (id={apt_id}): {parts}"

    saved = await operations.save_meter_group_readings(
        apartment_id=apt_id,
        meters=meters,
        submitted_by=user.telegram_id,
        reading_date=local_today(),
        action=action,
        audit_details=audit_details,
    )

    if saved:
        await notify_new_readings(request.app.state.bot, apt_id, meters, prev_meters, user.telegram_id)

    return {"ok": True, "deduplicated": not saved, "billing": billing.to_dict()}


@router.post("/api/apartments/{apt_id}/readings/water")
async def api_submit_water(
    apt_id: int,
    body: WaterReadingBody,
    request: Request,
    user: WebAppUser = Depends(get_current_user),
):
    meters = {
        k: v
        for k, v in (
            ("cold_water", body.cold_water),
            ("hot_water", body.hot_water),
            ("irrigation", body.irrigation),
        )
        if v is not None
    }
    if not meters:
        raise HTTPException(400, "Укажите показание хотя бы одного счётчика воды")
    return await _save_meter_group(request, apt_id, meters, body.force, "submit_water", user)


@router.post("/api/apartments/{apt_id}/readings/electricity")
async def api_submit_electricity(
    apt_id: int,
    body: ElectricityReadingBody,
    request: Request,
    user: WebAppUser = Depends(get_current_user),
):
    meters = {
        k: v
        for k, v in (("t", body.t), ("t1", body.t1), ("t2", body.t2), ("t3", body.t3))
        if v is not None
    }
    if not meters:
        raise HTTPException(400, "Укажите показание хотя бы одного тарифа электричества")
    return await _save_meter_group(request, apt_id, meters, body.force, "submit_electricity", user)


@router.post("/api/apartments/{apt_id}/readings/gas")
async def api_submit_gas(
    apt_id: int,
    body: GasReadingBody,
    request: Request,
    user: WebAppUser = Depends(get_current_user),
):
    meters = {"gas": body.gas}
    return await _save_meter_group(request, apt_id, meters, body.force, "submit_gas", user)


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
        if not (1 <= month <= 12):
            raise HTTPException(400, "Некорректный месяц")
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
        raise HTTPException(403, "Нет доступа к объекту недвижимости")

    buf = bytearray()
    while True:
        chunk = await file.read(CHUNK_SIZE)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > MAX_PHOTO_BYTES:
            raise HTTPException(413, "Файл превышает максимально допустимый размер 15 МБ")

    if not buf:
        raise HTTPException(400, "Файл пуст")

    ext = Path(file.filename or "photo.jpg").suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"

    ts = datetime.datetime.now(BOT_TZ).strftime("%Y%m%d_%H%M%S")
    filepath = PHOTOS_DIR / f"apt{apt_id}_{user.telegram_id}_{ts}_{uuid.uuid4().hex[:8]}{ext}"
    await asyncio.to_thread(filepath.write_bytes, bytes(buf))

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
        f"Объект id={apt_id}, файл={filepath.name}, получателей={sent}",
    )
    return {"ok": True, "recipients_count": sent}

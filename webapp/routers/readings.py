"""Meter readings submission, history, correction, and direct photo forwarding (no server storage)."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile

from config import MAX_PHOTO_BYTES, local_today
from database import crud, operations
from database.models import (
    METER_LABELS,
    METER_PLAIN_LABELS,
    METER_UNITS,
)
from services.access import user_can_access_apartment
from services.calculations import calculate_billing, sanitize_billing_for_tenant
from services.notifications import (
    dispatch_notification,
    forward_meter_photo_file,
    notify_new_readings,
)
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
MIN_HISTORY_YEAR = 1970
MAX_HISTORY_YEAR = 2100


def _detect_image_extension(header: bytes) -> str | None:
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return ".webp"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if (
        len(header) >= 12
        and header[4:8] == b"ftyp"
        and header[8:12] in (b"heic", b"heix", b"hevc", b"mif1", b"msf1")
    ):
        return ".jpg"
    return None


async def _save_meter_group(
    request: Request,
    apt_id: int,
    meters: dict[str, int],
    force: bool,
    submission_id: str | None,
    action: str,
    user: WebAppUser,
) -> dict:
    if not await user_can_access_apartment(user.telegram_id, apt_id):
        raise HTTPException(403, "Нет доступа к объекту недвижимости")
    if not meters:
        raise HTTPException(400, "Укажите хотя бы одно показание счётчика")
    if any(v < 0 for v in meters.values()):
        raise HTTPException(400, "Показания должны быть неотрицательными целыми числами")

    try:
        outcome = await operations.submit_meter_group_atomic(
            apartment_id=apt_id,
            meters=meters,
            submitted_by=user.telegram_id,
            reading_date=local_today(),
            action=action,
            force=force,
            submission_id=submission_id,
        )
    except operations.ConflictError as e:
        raise HTTPException(409, str(e))
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))

    if outcome.status == "rejected_decrease":
        if user.role == "tenant":
            warning = (
                "Вы ввели значение меньше, чем уже зафиксировано. "
                "Если счётчик был заменён или ранее допущена ошибка, обратитесь к владельцу объекта."
            )
        else:
            warning = (
                "Вы ввели значение меньше, чем уже зафиксировано. "
                "Если заменён счётчик, то перейдите в раздел «Недвижимость» -> Настройки "
                "и в разделе «Счётчики» найдите кнопку «Замена счётчика»."
            )
        return {
            "ok": False,
            "rejected_decrease": True,
            "decreased": outcome.decreased,
            "can_open_meters": user.role in ("admin", "landlord"),
            "warning": warning,
        }

    if outcome.status == "needs_confirm_repeat":
        labels = ", ".join(METER_PLAIN_LABELS.get(mt, mt) for mt in outcome.repeated)
        return {
            "ok": False,
            "needs_confirm": True,
            "confirm_reason": "repeat_in_month",
            "repeated": outcome.repeated,
            "warning": (
                f"Показания по счётчикам ({labels}) в этом месяце уже подавались. "
                f"Вы уверены, что хотите сохранить новые значения?"
            ),
        }

    if outcome.status == "saved":
        await dispatch_notification(
            request,
            notify_new_readings(
                request.app.state.bot,
                apt_id,
                meters,
                outcome.prev_meters,
                user.telegram_id,
            ),
            name=f"notify_readings_{apt_id}",
        )

    billing_dict = (await calculate_billing(apt_id)).to_dict()
    if user.role == "tenant":
        billing_dict = sanitize_billing_for_tenant(billing_dict)

    return {
        "ok": True,
        "deduplicated": outcome.status == "deduplicated",
        "billing": billing_dict,
    }


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
    return await _save_meter_group(
        request, apt_id, meters, body.force, body.submission_id, "submit_water", user
    )


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
    return await _save_meter_group(
        request, apt_id, meters, body.force, body.submission_id, "submit_electricity", user
    )


@router.post("/api/apartments/{apt_id}/readings/gas")
async def api_submit_gas(
    apt_id: int,
    body: GasReadingBody,
    request: Request,
    user: WebAppUser = Depends(get_current_user),
):
    meters = {"gas": body.gas}
    return await _save_meter_group(
        request, apt_id, meters, body.force, body.submission_id, "submit_gas", user
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

    if (year is None) != (month is None):
        raise HTTPException(400, "Необходимо указать одновременно год и месяц")

    months = await crud.get_reading_months(apt_id)
    if year is not None and month is not None:
        if not (MIN_HISTORY_YEAR <= year <= MAX_HISTORY_YEAR):
            raise HTTPException(400, f"Некорректный год (допустимо {MIN_HISTORY_YEAR}–{MAX_HISTORY_YEAR})")
        if not (1 <= month <= 12):
            raise HTTPException(400, "Некорректный месяц (допустимо 1–12)")
        rows = await crud.get_readings_by_month(apt_id, year, month)
        billing = (await calculate_billing(apt_id, year=year, month=month)).to_dict()
    else:
        rows = await crud.get_all_readings(apt_id, limit=60)
        billing = (await calculate_billing(apt_id)).to_dict()

    if user.role == "tenant":
        billing = sanitize_billing_for_tenant(billing)

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
                "is_replacement_init": bool(getattr(r, "is_replacement_init", False)),
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

    try:
        _, old_val, new_val = await operations.correct_reading_atomic(
            reading_id, body.value, user.telegram_id
        )
    except LookupError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "old_value": old_val, "new_value": new_val}


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

    content_len = getattr(request, "headers", {}).get("content-length")
    if content_len:
        try:
            if int(content_len) > MAX_PHOTO_BYTES + 1024 * 1024:
                raise HTTPException(413, "Файл превышает максимально допустимый размер 15 МБ")
        except ValueError:
            pass

    tmp_path: Path | None = None
    total_size = 0
    detected_ext: str | None = None
    fd = None
    try:
        first_chunk = await file.read(CHUNK_SIZE)
        if not first_chunk:
            raise HTTPException(400, "Файл пуст")
        detected_ext = _detect_image_extension(first_chunk)
        if detected_ext is None:
            raise HTTPException(400, "Неподдерживаемый формат файла. Загрузите изображение (JPEG, PNG, WebP).")

        fd_num, raw_name = tempfile.mkstemp(prefix=f"umb_photo_{apt_id}_", suffix=detected_ext)
        tmp_path = Path(raw_name)
        fd = os.fdopen(fd_num, "wb")

        chunk = first_chunk
        while chunk:
            total_size += len(chunk)
            if total_size > MAX_PHOTO_BYTES:
                raise HTTPException(413, "Файл превышает максимально допустимый размер 15 МБ")
            await asyncio.to_thread(fd.write, chunk)
            chunk = await file.read(CHUNK_SIZE)

        await asyncio.to_thread(fd.close)
        fd = None

        sent = await forward_meter_photo_file(
            bot=request.app.state.bot,
            apt_id=apt_id,
            filepath=tmp_path,
            sender_tg_id=user.telegram_id,
            sender_name=user.name,
            comment=comment,
        )
        await crud.add_audit_log(
            user.telegram_id,
            "upload_photo_webapp",
            f"Объект id={apt_id}, получателей={sent}",
        )
        return {"ok": True, "recipients_count": sent}
    finally:
        if fd is not None:
            try:
                fd.close()
            except OSError:
                pass
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

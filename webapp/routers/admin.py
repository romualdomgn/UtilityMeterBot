"""Export (Excel/PDF), admin user list, audit logs (BOT_TZ), and manual DB backup endpoints."""

from __future__ import annotations

import datetime
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from config import BOT_TZ
from database import crud
from services.access import get_accessible_apartments, is_admin, user_can_access_apartment
from services.backup import create_family_backup, get_or_create_manual_backup
from services.export import generate_excel_report, generate_pdf_report
from services.notifications import send_document_to_user
from webapp.auth import WebAppUser, get_current_user
from webapp.schemas import ExportBody, require_role

logger = logging.getLogger(__name__)
router = APIRouter()


def _format_audit_local(dt_utc_naive: datetime.datetime) -> str:
    aware_utc = dt_utc_naive.replace(tzinfo=datetime.timezone.utc)
    return aware_utc.astimezone(BOT_TZ).strftime("%d.%m.%Y %H:%M")


@router.post("/api/export")
async def api_export(
    body: ExportBody,
    request: Request,
    user: WebAppUser = Depends(get_current_user),
):
    require_role(user, ("admin", "landlord"))
    if body.apartment_id:
        if not await user_can_access_apartment(user.telegram_id, body.apartment_id):
            raise HTTPException(403, "Нет доступа к объекту недвижимости")
        apt = await crud.get_apartment(body.apartment_id)
        apartments = [apt] if apt else []
    else:
        apartments = await get_accessible_apartments(user.telegram_id)

    if not apartments:
        raise HTTPException(400, "Нет объектов недвижимости для выгрузки")

    if body.format == "excel":
        filepath = await generate_excel_report(apartments)
        caption = "📊 Выгрузка показаний (Excel)"
    else:
        filepath = await generate_pdf_report(apartments)
        caption = "📄 Выгрузка показаний (PDF)"

    try:
        await send_document_to_user(request.app.state.bot, user.telegram_id, filepath, caption)
    finally:
        try:
            filepath.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("Failed to remove temp export %s: %s", filepath.name, e)

    await crud.add_audit_log(
        user.telegram_id, f"export_{body.format}",
        f"Файл {filepath.name}, объектов: {len(apartments)}",
    )
    return {"ok": True, "filename": filepath.name}


@router.get("/api/admin/users")
async def api_admin_users(user: WebAppUser = Depends(get_current_user)):
    require_role(user, ("admin", "landlord"))
    if is_admin(user.telegram_id):
        users = await crud.get_all_users()
    else:
        users = await crud.get_family_scoped_users(user.family_id) if user.family_id else []
    return {
        "users": [
            {
                "telegram_id": u.telegram_id,
                "name": u.name,
                "phone": u.phone,
                "role": u.role,
                "family_id": u.family_id,
                "is_blocked": u.is_blocked,
            }
            for u in users
        ]
    }


@router.get("/api/admin/audit")
async def api_admin_audit(user: WebAppUser = Depends(get_current_user)):
    require_role(user, ("admin", "landlord"))
    if is_admin(user.telegram_id):
        logs = await crud.get_audit_logs(limit=50)
    else:
        logs = await crud.get_family_audit_logs(user.family_id, limit=50) if user.family_id else []
    return {
        "logs": [
            {
                "id": l.id,
                "created_at": _format_audit_local(l.created_at),
                "user_telegram_id": l.user_telegram_id,
                "action": l.action,
                "details": l.details,
            }
            for l in logs
        ]
    }


@router.post("/api/admin/backup")
async def api_admin_backup(request: Request, user: WebAppUser = Depends(get_current_user)):
    require_role(user, ("admin", "landlord"))
    if is_admin(user.telegram_id):
        filepath, created_new = await get_or_create_manual_backup()
        caption = (
            "💾 Резервная копия базы данных"
            if created_new
            else "💾 Актуальная резервная копия БД (изменений с последнего бэкапа не было)"
        )
        await send_document_to_user(
            request.app.state.bot, user.telegram_id, filepath, caption
        )
        await crud.add_audit_log(
            user.telegram_id,
            "manual_backup",
            f"{filepath.name} ({'новая' if created_new else 'актуальная'})",
        )
        return {"ok": True, "filename": filepath.name, "created_new": created_new, "scope": "full"}

    if not user.family_id:
        raise HTTPException(400, "Вы не привязаны к семье для создания резервной копии")

    filepath = await create_family_backup(user.family_id)
    try:
        await send_document_to_user(
            request.app.state.bot,
            user.telegram_id,
            filepath,
            "💾 Резервная копия базы данных (ваша семья)",
        )
    finally:
        try:
            filepath.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("Failed to remove temp family backup %s: %s", filepath.name, e)

    await crud.add_audit_log(
        user.telegram_id,
        "manual_family_backup",
        f"Семья id={user.family_id}, файл {filepath.name}",
    )
    return {"ok": True, "filename": filepath.name, "created_new": True, "scope": "family"}

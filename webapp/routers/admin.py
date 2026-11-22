"""Export (Excel/PDF), admin user list, audit logs, and manual DB backup endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from database import crud
from services.access import get_accessible_apartments, user_can_access_apartment
from services.backup import create_backup
from services.export import generate_excel_report, generate_pdf_report
from services.notifications import send_document_to_user
from webapp.auth import WebAppUser, get_current_user
from webapp.schemas import ExportBody, require_role

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/api/export")
async def api_export(
    body: ExportBody,
    request: Request,
    user: WebAppUser = Depends(get_current_user),
):
    require_role(user, ("admin", "landlord"))
    if body.apartment_id:
        if not await user_can_access_apartment(user.telegram_id, body.apartment_id):
            raise HTTPException(403, "Нет доступа к квартире")
        apt = await crud.get_apartment(body.apartment_id)
        apartments = [apt] if apt else []
    else:
        apartments = await get_accessible_apartments(user.telegram_id)

    if not apartments:
        raise HTTPException(400, "Нет квартир для выгрузки")

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
        f"Файл {filepath.name}, квартир: {len(apartments)}",
    )
    return {"ok": True, "filename": filepath.name}


@router.get("/api/admin/users")
async def api_admin_users(user: WebAppUser = Depends(get_current_user)):
    require_role(user, ("admin",))
    users = await crud.get_all_users()
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
    require_role(user, ("admin",))
    logs = await crud.get_audit_logs(limit=50)
    return {
        "logs": [
            {
                "id": l.id,
                "created_at": l.created_at.strftime("%d.%m.%Y %H:%M"),
                "user_telegram_id": l.user_telegram_id,
                "action": l.action,
                "details": l.details,
            }
            for l in logs
        ]
    }


@router.post("/api/admin/backup")
async def api_admin_backup(request: Request, user: WebAppUser = Depends(get_current_user)):
    require_role(user, ("admin",))
    filepath = await create_backup()
    await send_document_to_user(
        request.app.state.bot, user.telegram_id, filepath, "💾 Резервная копия базы данных"
    )
    await crud.add_audit_log(user.telegram_id, "manual_backup", filepath.name)
    return {"ok": True, "filename": filepath.name}

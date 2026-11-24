"""Family groups and co-owners management endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

import config
from database import crud, operations
from services.access import is_admin, user_can_manage_family
from webapp.auth import WebAppUser, get_current_user
from webapp.schemas import FamilyBody, FamilyOwnerBody, FamilyOwnerUpdateBody, require_role

router = APIRouter()


@router.get("/api/families")
async def api_families(user: WebAppUser = Depends(get_current_user)):
    require_role(user, ("admin", "landlord"))
    if is_admin(user.telegram_id):
        fams = await crud.get_all_families()
    else:
        fam = await crud.get_family(user.family_id) if user.family_id else None
        fams = [fam] if fam else []

    items = []
    for f in fams:
        owners = await crud.get_family_owners(f.id)
        apts = await crud.get_apartments_by_family(f.id)
        items.append({
            "id": f.id,
            "name": f.name,
            "apartments_count": len(apts),
            "owners": [
                {"telegram_id": o.telegram_id, "name": o.name, "phone": o.phone}
                for o in owners
            ],
        })
    return {"can_create": is_admin(user.telegram_id), "families": items}


@router.post("/api/families")
async def api_create_family(body: FamilyBody, user: WebAppUser = Depends(get_current_user)):
    require_role(user, ("admin",))
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Название семьи не может быть пустым")
    fam = await crud.create_family(name)
    await crud.add_audit_log(user.telegram_id, "create_family", f"Семья '{name}' (id={fam.id})")
    return {"ok": True, "id": fam.id}


@router.put("/api/families/{family_id}")
async def api_rename_family(
    family_id: int, body: FamilyBody, user: WebAppUser = Depends(get_current_user)
):
    if not await user_can_manage_family(user.telegram_id, family_id):
        raise HTTPException(403, "Нет доступа")
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Название не может быть пустым")
    await crud.rename_family(family_id, name)
    await crud.add_audit_log(user.telegram_id, "rename_family", f"Семья id={family_id} → '{name}'")
    return {"ok": True}


@router.delete("/api/families/{family_id}")
async def api_delete_family(family_id: int, user: WebAppUser = Depends(get_current_user)):
    require_role(user, ("admin",))
    try:
        await operations.delete_family_atomic(family_id, user.telegram_id)
    except LookupError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@router.post("/api/families/{family_id}/owners")
async def api_add_family_owner(
    family_id: int, body: FamilyOwnerBody, user: WebAppUser = Depends(get_current_user)
):
    if not await user_can_manage_family(user.telegram_id, family_id):
        raise HTTPException(403, "Нет доступа")
    if body.telegram_id <= 0:
        raise HTTPException(400, "Telegram ID должен быть положительным числом")
    if body.telegram_id == config.ADMIN_TELEGRAM_ID:
        raise HTTPException(400, "Администратор уже имеет доступ ко всем семьям")
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Имя не может быть пустым")

    existing = await crud.get_user_by_telegram_id(body.telegram_id)
    if existing:
        raise HTTPException(400, f"Пользователь ID {body.telegram_id} уже зарегистрирован ({existing.role})")

    await crud.get_or_create_user(
        telegram_id=body.telegram_id,
        name=name,
        phone=(body.phone or "").strip() or None,
        role="landlord",
        family_id=family_id,
    )
    await crud.add_audit_log(
        user.telegram_id, "add_family_owner",
        f"Владелец {name} (ID: {body.telegram_id}) добавлен в семью id={family_id}",
    )
    return {"ok": True}


@router.put("/api/families/{family_id}/owners/{owner_tg_id}")
async def api_update_family_owner(
    family_id: int,
    owner_tg_id: int,
    body: FamilyOwnerUpdateBody,
    user: WebAppUser = Depends(get_current_user),
):
    if not await user_can_manage_family(user.telegram_id, family_id):
        raise HTTPException(403, "Нет доступа")
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Имя не может быть пустым")
    if len(name) > 120:
        raise HTTPException(400, "Имя слишком длинное (максимум 120 символов)")

    update_phone = "phone" in body.model_fields_set
    phone_val = (body.phone or "").strip() or None if update_phone else None

    updated = await crud.update_family_owner(
        family_id=family_id,
        owner_tg_id=owner_tg_id,
        name=name,
        phone=phone_val,
        update_phone=update_phone,
    )
    if not updated:
        raise HTTPException(404, "Член семьи не найден")

    await crud.add_audit_log(
        user.telegram_id,
        "update_family_owner",
        f"Владелец ID={owner_tg_id} (семья id={family_id}) обновлён: имя='{name}'",
    )
    return {"ok": True, "name": updated.name, "phone": updated.phone}


@router.delete("/api/families/{family_id}/owners/{owner_tg_id}")
async def api_delete_family_owner(
    family_id: int, owner_tg_id: int, user: WebAppUser = Depends(get_current_user)
):
    if not await user_can_manage_family(user.telegram_id, family_id):
        raise HTTPException(403, "Нет доступа")
    try:
        await operations.delete_family_owner_atomic(family_id, owner_tg_id, user.telegram_id)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except LookupError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}

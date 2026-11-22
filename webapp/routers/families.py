"""Family groups and co-owners management endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from config import sync_id_to_env
from database import crud
from services.access import is_admin, user_can_manage_family
from webapp.auth import WebAppUser, get_current_user
from webapp.schemas import FamilyBody, FamilyOwnerBody, require_role

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
    for u in await crud.get_users_by_family(family_id):
        if u.role in ("landlord", "tenant"):
            sync_id_to_env(u.role, u.telegram_id, remove=True)
    await crud.delete_family(family_id)
    await crud.add_audit_log(user.telegram_id, "delete_family", f"Семья id={family_id} удалена")
    return {"ok": True}


@router.post("/api/families/{family_id}/owners")
async def api_add_family_owner(
    family_id: int, body: FamilyOwnerBody, user: WebAppUser = Depends(get_current_user)
):
    if not await user_can_manage_family(user.telegram_id, family_id):
        raise HTTPException(403, "Нет доступа")
    if body.telegram_id <= 0:
        raise HTTPException(400, "Telegram ID должен быть положительным числом")
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
    sync_id_to_env("landlord", body.telegram_id, remove=False)
    await crud.add_audit_log(
        user.telegram_id, "add_family_owner",
        f"Владелец {name} (ID: {body.telegram_id}) добавлен в семью id={family_id}",
    )
    return {"ok": True}


@router.delete("/api/families/{family_id}/owners/{owner_tg_id}")
async def api_delete_family_owner(
    family_id: int, owner_tg_id: int, user: WebAppUser = Depends(get_current_user)
):
    if not await user_can_manage_family(user.telegram_id, family_id):
        raise HTTPException(403, "Нет доступа")
    await crud.delete_user(owner_tg_id)
    sync_id_to_env("landlord", owner_tg_id, remove=True)
    await crud.add_audit_log(
        user.telegram_id, "delete_family_owner",
        f"Владелец ID={owner_tg_id} удалён из семьи id={family_id}",
    )
    return {"ok": True}

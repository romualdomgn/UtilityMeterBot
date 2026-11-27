"""People (family groups, co-owners including admin, and family tenants) endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

import config
from database import crud, operations
from database.models import format_property_short, resolve_family_name, resolve_user_display_name
from services.access import is_admin, user_can_manage_family
from services.notifications import dispatch_notification, notify_admin_new_family_created
from webapp.auth import WebAppUser, get_current_user
from webapp.schemas import FamilyBody, FamilyInviteBody, FamilyOwnerBody, FamilyOwnerUpdateBody, require_role

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
    family_ids: list[int] = []
    for f in fams:
        family_ids.append(f.id)
        owners = await crud.get_family_owners(f.id)
        apts = await crud.get_apartments_by_family(f.id)
        items.append({
            "id": f.id,
            "name": resolve_family_name(f, user.role),
            "apartments_count": len(apts),
            "owners": [
                {
                    "telegram_id": o.telegram_id,
                    "name": resolve_user_display_name(o, user.role, user.telegram_id),
                    "is_admin": bool(config.ADMIN_TELEGRAM_ID and o.telegram_id == config.ADMIN_TELEGRAM_ID),
                }
                for o in owners
            ],
        })

    tenant_pairs = await crud.get_tenants_for_families(family_ids)
    tenants_map: dict[int, dict] = {}
    for t_user, apt in tenant_pairs:
        entry = tenants_map.get(t_user.telegram_id)
        if entry is None:
            entry = {
                "telegram_id": t_user.telegram_id,
                "name": resolve_user_display_name(t_user, user.role, user.telegram_id),
                "family_id": t_user.family_id,
                "apartments": [],
            }
            tenants_map[t_user.telegram_id] = entry
        entry["apartments"].append({
            "id": apt.id,
            "display_name": format_property_short(apt),
            "agreement_no": apt.agreement_no,
            "agreement_date_finish": (
                apt.agreement_date_finish.isoformat() if apt.agreement_date_finish else None
            ),
        })

    return {
        "can_create": is_admin(user.telegram_id),
        "can_invite_family": user.role in ("admin", "landlord"),
        "families": items,
        "tenants": list(tenants_map.values()),
    }


@router.post("/api/families")
async def api_create_family(body: FamilyBody, user: WebAppUser = Depends(get_current_user)):
    require_role(user, ("admin",))
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Название семьи не может быть пустым")
    try:
        fam = await operations.create_family_atomic(name, user.telegram_id)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    return {"ok": True, "id": fam.id}


def build_new_owner_invite_text(owner_name: str) -> str:
    return (
        f"Привет {owner_name}, приглашаю присоединиться к боту для управления недвижимостью "
        f"(передавать показания счётчиков, следить за сроками поверки, отслеживать договоры найма, "
        f"нанимателей и даты платежей, а так же многое другое). "
        f"Ссылка на Телеграм бот - @TG_Utility_Meter_bot"
    )


@router.post("/api/families/invite")
async def api_invite_family(
    body: FamilyInviteBody,
    request: Request,
    user: WebAppUser = Depends(get_current_user),
):
    """Allow any landlord (or admin) to create a new isolated family with one initial owner."""
    require_role(user, ("admin", "landlord"))
    family_name = body.family_name.strip()
    if not family_name:
        raise HTTPException(400, "Название семьи не может быть пустым")
    if len(family_name) > 120:
        raise HTTPException(400, "Название семьи слишком длинное (максимум 120 символов)")

    owner_name = body.owner_name.strip()
    if not owner_name:
        raise HTTPException(400, "Имя нового владельца не может быть пустым")
    if len(owner_name) > 120:
        raise HTTPException(400, "Имя слишком длинное (максимум 120 символов)")

    try:
        fam, new_owner = await operations.create_family_with_owner_atomic(
            family_name=family_name,
            owner_telegram_id=body.owner_telegram_id,
            owner_name=owner_name,
            actor_tg_id=user.telegram_id,
        )
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except operations.ConflictError as e:
        raise HTTPException(409, str(e))

    await dispatch_notification(
        request,
        notify_admin_new_family_created(
            bot=request.app.state.bot,
            creator_tg_id=user.telegram_id,
            creator_name=user.name,
            family_name=fam.name,
            owner_name=new_owner.name or owner_name,
            owner_tg_id=new_owner.telegram_id,
        ),
        name=f"notify_admin_new_family_{fam.id}",
    )

    return {
        "ok": True,
        "family_name": fam.name,
        "owner_name": new_owner.name or owner_name,
        "invite_text": build_new_owner_invite_text(new_owner.name or owner_name),
    }


@router.put("/api/families/{family_id}")
async def api_rename_family(
    family_id: int, body: FamilyBody, user: WebAppUser = Depends(get_current_user)
):
    if not await user_can_manage_family(user.telegram_id, family_id):
        raise HTTPException(403, "Нет доступа")
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Название не может быть пустым")
    try:
        await operations.rename_family_atomic(family_id, name, user.telegram_id, is_actor_admin=is_admin(user.telegram_id))
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except LookupError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@router.delete("/api/families/{family_id}")
async def api_delete_family(family_id: int, user: WebAppUser = Depends(get_current_user)):
    require_role(user, ("admin",))
    try:
        await operations.delete_family_atomic(family_id, user.telegram_id)
    except PermissionError as e:
        raise HTTPException(403, str(e))
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
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Имя не может быть пустым")
    if len(name) > 120:
        raise HTTPException(400, "Имя слишком длинное (максимум 120 символов)")

    try:
        await operations.add_family_owner_atomic(
            family_id=family_id,
            telegram_id=body.telegram_id,
            name=name,
            phone=None,
            actor_tg_id=user.telegram_id,
        )
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except LookupError as e:
        raise HTTPException(404, str(e))
    except operations.ConflictError as e:
        raise HTTPException(409, str(e))
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

    try:
        updated = await operations.update_family_owner_atomic(
            family_id=family_id,
            owner_tg_id=owner_tg_id,
            name=name,
            phone=None,
            update_phone=False,
            actor_tg_id=user.telegram_id,
            is_actor_admin=is_admin(user.telegram_id),
        )
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except LookupError as e:
        raise HTTPException(404, str(e))

    return {
        "ok": True,
        "name": resolve_user_display_name(updated, user.role, user.telegram_id),
    }


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

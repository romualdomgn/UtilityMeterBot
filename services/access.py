"""Access control — role checks, lease expiration enforcement, and family isolation."""

from __future__ import annotations

import datetime

import config
from database import crud


def is_admin(telegram_id: int) -> bool:
    return telegram_id == config.ADMIN_TELEGRAM_ID


def is_landlord(telegram_id: int) -> bool:
    return telegram_id in config.LANDLORD_TELEGRAM_IDS


def is_tenant(telegram_id: int) -> bool:
    return telegram_id in config.TENANT_IDS


def is_admin_or_landlord(telegram_id: int) -> bool:
    return is_admin(telegram_id) or is_landlord(telegram_id)


async def get_user_role(telegram_id: int) -> str:
    """Return 'admin', 'landlord', 'tenant', or 'unknown'."""
    if is_admin(telegram_id):
        return "admin"
    user = await crud.get_user_by_telegram_id(telegram_id)
    if user and not user.is_blocked:
        if user.role in ("admin", "landlord", "tenant"):
            return user.role
    if is_landlord(telegram_id):
        return "landlord"
    if is_tenant(telegram_id):
        return "tenant"
    return "unknown"


async def is_tenant_lease_active(telegram_id: int, apartment_id: int) -> bool:
    apt = await crud.get_apartment(apartment_id)
    if not apt:
        return False
    if apt.agreement_date_finish is None:
        return True
    return apt.agreement_date_finish >= datetime.date.today()


async def check_user_access(telegram_id: int) -> tuple[bool, str | None]:
    user = await crud.get_user_by_telegram_id(telegram_id)
    if user and user.is_blocked:
        return False, "⛔ Ваш доступ к боту заблокирован."

    role = await get_user_role(telegram_id)
    if role == "unknown":
        return False, "⛔ Доступ запрещён."

    if role == "tenant":
        apts = await crud.get_apartments_for_user(telegram_id)
        if apts:
            active_any = False
            for apt in apts:
                if await is_tenant_lease_active(telegram_id, apt.id):
                    active_any = True
                    break
            if not active_any:
                return False, "⛔ Срок действия договора найма квартиры истёк. Доступ прекращён."

    return True, None


async def get_user_family_id(telegram_id: int) -> int | None:
    user = await crud.get_user_by_telegram_id(telegram_id)
    return user.family_id if user else None


async def get_accessible_apartments(telegram_id: int):
    role = await get_user_role(telegram_id)
    if role == "admin":
        return await crud.get_all_apartments()
    if role == "landlord":
        fid = await get_user_family_id(telegram_id)
        return await crud.get_apartments_by_family(fid) if fid else []
    if role == "tenant":
        linked = await crud.get_apartments_for_user(telegram_id)
        active = []
        for apt in linked:
            if await is_tenant_lease_active(telegram_id, apt.id):
                active.append(apt)
        return active
    return []


async def user_can_access_apartment(telegram_id: int, apartment_id: int) -> bool:
    if is_admin(telegram_id):
        return True

    apt = await crud.get_apartment(apartment_id)
    if not apt:
        return False

    role = await get_user_role(telegram_id)
    user = await crud.get_user_by_telegram_id(telegram_id)

    if role == "landlord" and user:
        return user.family_id == apt.family_id

    if role == "tenant":
        linked = await crud.get_apartments_for_user(telegram_id)
        if not any(a.id == apartment_id for a in linked):
            return False
        return await is_tenant_lease_active(telegram_id, apartment_id)

    return False


async def user_can_manage_family(telegram_id: int, family_id: int) -> bool:
    if is_admin(telegram_id):
        return True
    role = await get_user_role(telegram_id)
    if role == "landlord":
        user = await crud.get_user_by_telegram_id(telegram_id)
        return user is not None and user.family_id == family_id
    return False


async def get_notification_recipients(apartment_id: int) -> list[int]:
    recipients: list[int] = []

    if config.ADMIN_TELEGRAM_ID:
        recipients.append(config.ADMIN_TELEGRAM_ID)

    apt = await crud.get_apartment(apartment_id)
    if apt:
        owners = await crud.get_family_owners(apt.family_id)
        for owner in owners:
            if owner.telegram_id not in recipients and not owner.is_blocked:
                recipients.append(owner.telegram_id)

        apt_owners = await crud.get_apartment_owners(apartment_id)
        for owner in apt_owners:
            if owner.telegram_id not in recipients and not owner.is_blocked:
                recipients.append(owner.telegram_id)

    return recipients

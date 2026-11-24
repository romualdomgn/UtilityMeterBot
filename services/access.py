"""Access control — role checks (SQLite + ADMIN_TELEGRAM_ID), lease checks, family isolation."""

from __future__ import annotations

import config
from database import crud
from database.models import Apartment, User


def is_admin(telegram_id: int) -> bool:
    return bool(config.ADMIN_TELEGRAM_ID) and telegram_id == config.ADMIN_TELEGRAM_ID


def _role_from_user(telegram_id: int, user: User | None) -> str:
    if is_admin(telegram_id):
        return "admin"
    if user and not user.is_blocked and user.role in ("landlord", "tenant"):
        return user.role
    return "unknown"


def is_lease_active_for_apartment(apt: Apartment | None) -> bool:
    if not apt:
        return False
    if apt.agreement_date_finish is None:
        return True
    return apt.agreement_date_finish >= config.local_today()


async def get_user_role(telegram_id: int) -> str:
    """Return 'admin', 'landlord', 'tenant', or 'unknown'.

    CRITICAL-02 & MEDIUM-07: admin comes exclusively from ADMIN_TELEGRAM_ID;
    landlord/tenant roles come exclusively from SQLite.
    """
    if is_admin(telegram_id):
        return "admin"
    user = await crud.get_user_by_telegram_id(telegram_id)
    return _role_from_user(telegram_id, user)


async def is_tenant_lease_active(telegram_id: int, apartment_id: int) -> bool:
    apt = await crud.get_apartment(apartment_id)
    return is_lease_active_for_apartment(apt)


async def resolve_user_access(telegram_id: int) -> tuple[bool, str | None, str, User | None]:
    """Resolve (allowed, denial_reason, role, db_user) with minimal DB queries."""
    user = await crud.get_user_by_telegram_id(telegram_id)
    if is_admin(telegram_id):
        return True, None, "admin", user

    if user and user.is_blocked:
        return False, "⛔ Ваш доступ к боту заблокирован.", "unknown", user

    role = _role_from_user(telegram_id, user)
    if role == "unknown":
        return False, "⛔ Доступ запрещён.", "unknown", user

    if role == "tenant":
        apts = await crud.get_apartments_for_user(telegram_id)
        if apts and not any(is_lease_active_for_apartment(apt) for apt in apts):
            return False, "⛔ Срок действия договора найма истёк. Доступ прекращён.", role, user

    return True, None, role, user


async def check_user_access(telegram_id: int) -> tuple[bool, str | None]:
    allowed, reason, _, _ = await resolve_user_access(telegram_id)
    return allowed, reason


async def get_user_family_id(telegram_id: int) -> int | None:
    user = await crud.get_user_by_telegram_id(telegram_id)
    return user.family_id if user else None


async def get_accessible_apartments(telegram_id: int):
    if is_admin(telegram_id):
        return await crud.get_all_apartments()
    user = await crud.get_user_by_telegram_id(telegram_id)
    role = _role_from_user(telegram_id, user)
    if role == "landlord":
        return await crud.get_apartments_by_family(user.family_id) if (user and user.family_id) else []
    if role == "tenant":
        linked = await crud.get_apartments_for_user(telegram_id)
        return [apt for apt in linked if is_lease_active_for_apartment(apt)]
    return []


async def user_can_access_apartment(telegram_id: int, apartment_id: int) -> bool:
    if is_admin(telegram_id):
        return True

    apt = await crud.get_apartment(apartment_id)
    if not apt:
        return False

    user = await crud.get_user_by_telegram_id(telegram_id)
    role = _role_from_user(telegram_id, user)

    if role == "landlord" and user:
        return user.family_id == apt.family_id

    if role == "tenant":
        linked = await crud.get_apartments_for_user(telegram_id)
        if not any(a.id == apartment_id for a in linked):
            return False
        return is_lease_active_for_apartment(apt)

    return False


async def user_can_manage_family(telegram_id: int, family_id: int) -> bool:
    if is_admin(telegram_id):
        return True
    user = await crud.get_user_by_telegram_id(telegram_id)
    role = _role_from_user(telegram_id, user)
    return role == "landlord" and user is not None and user.family_id == family_id


async def get_notification_recipients(apartment_id: int) -> list[int]:
    recipients: list[int] = []

    if config.ADMIN_TELEGRAM_ID:
        recipients.append(config.ADMIN_TELEGRAM_ID)

    apt = await crud.get_apartment(apartment_id)
    if apt:
        for owner in await crud.get_family_owners(apt.family_id):
            if owner.telegram_id not in recipients and not owner.is_blocked:
                recipients.append(owner.telegram_id)

        for owner in await crud.get_apartment_owners(apartment_id):
            if owner.telegram_id not in recipients and not owner.is_blocked:
                recipients.append(owner.telegram_id)

    return recipients

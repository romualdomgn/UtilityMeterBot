"""Access control — role checks (SQLite + ADMIN_TELEGRAM_ID), lease checks, family isolation."""

from __future__ import annotations

import calendar
import datetime

import config
from sqlalchemy import select

from database import crud
from database.models import (
    Apartment,
    ApartmentUser,
    ELECTRICITY_METERS,
    Family,
    GAS_METERS,
    User,
    WATER_SUBMISSION_METERS,
    get_apartment_enabled_meters,
)


def is_admin(telegram_id: int) -> bool:
    return bool(config.ADMIN_TELEGRAM_ID) and telegram_id == config.ADMIN_TELEGRAM_ID


def _role_from_user(telegram_id: int, user: User | None) -> str:
    if is_admin(telegram_id):
        return "admin"
    if user and not user.is_blocked and user.role in ("landlord", "tenant"):
        return user.role
    return "unknown"


def is_lease_active_for_apartment(apt: Apartment | None) -> bool:
    """Tenant has access regardless of agreement_date_start; blocked only after finish date."""
    if not apt:
        return False
    if apt.agreement_date_finish is None:
        return True
    return apt.agreement_date_finish >= config.local_today()


async def get_user_role(telegram_id: int) -> str:
    if is_admin(telegram_id):
        return "admin"
    user = await crud.get_user_by_telegram_id(telegram_id)
    return _role_from_user(telegram_id, user)


async def is_tenant_lease_active(telegram_id: int, apartment_id: int) -> bool:
    apt = await crud.get_apartment(apartment_id)
    return is_lease_active_for_apartment(apt)


async def resolve_user_access(telegram_id: int) -> tuple[bool, str | None, str, User | None]:
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
        if user and user.family_id is not None:
            apts = [a for a in apts if a.family_id == user.family_id]
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
    if role == "tenant" and user:
        linked = await crud.get_apartments_for_user(telegram_id)
        return [
            apt for apt in linked
            if (user.family_id is None or apt.family_id == user.family_id)
            and is_lease_active_for_apartment(apt)
        ]
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

    if role == "tenant" and user:
        if user.family_id is not None and user.family_id != apt.family_id:
            return False
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


def _is_active_owner_recipient(owner: User, family_id: int) -> bool:
    """P1-02: ensure revoked/stale admins and mismatched landlords never receive notices."""
    if owner.is_blocked:
        return False
    if is_admin(owner.telegram_id):
        return True
    return owner.role == "landlord" and owner.family_id == family_id


def is_active_tenant_recipient(tenant: User, family_id: int) -> bool:
    """Ensure only active, unblocked tenants belonging to the property's family receive notices."""
    if tenant.is_blocked or tenant.role != "tenant":
        return False
    return tenant.family_id is None or tenant.family_id == family_id


async def assert_can_access_apartment_in_session(
    s,
    actor_tg_id: int,
    apartment_id: int,
    *,
    allow_tenant: bool = False,
) -> tuple[Apartment, str]:
    """P1-03: verify current actor permissions inside an active DB transaction. Returns (apt, role)."""
    if is_admin(actor_tg_id):
        apt = await s.get(Apartment, apartment_id)
        if not apt:
            raise LookupError("Объект недвижимости не найден")
        return apt, "admin"

    res_u = await s.execute(select(User).where(User.telegram_id == actor_tg_id))
    user = res_u.scalar_one_or_none()
    role = _role_from_user(actor_tg_id, user)
    if role == "unknown" or user is None:
        raise PermissionError("Нет доступа")
    if role == "tenant" and not allow_tenant:
        raise PermissionError("Недостаточно прав")

    apt = await s.get(Apartment, apartment_id)
    if not apt:
        raise PermissionError("Нет доступа к объекту недвижимости")

    if role == "landlord":
        if user.family_id != apt.family_id:
            raise PermissionError("Нет доступа к объекту недвижимости")
        return apt, "landlord"

    if role == "tenant":
        if user.family_id is not None and user.family_id != apt.family_id:
            raise PermissionError("Нет доступа к объекту недвижимости")
        res_link = await s.execute(
            select(ApartmentUser.id).where(
                ApartmentUser.apartment_id == apartment_id,
                ApartmentUser.user_id == user.id,
            ).limit(1)
        )
        if res_link.scalar_one_or_none() is None:
            raise PermissionError("Нет доступа к объекту недвижимости")
        if not is_lease_active_for_apartment(apt):
            raise PermissionError("Срок действия договора найма истёк. Доступ прекращён.")
        return apt, "tenant"

    raise PermissionError("Нет доступа")


async def assert_can_manage_family_in_session(
    s,
    actor_tg_id: int,
    family_id: int,
) -> tuple[Family, str]:
    """P1-03: verify family management rights inside an active DB transaction. Returns (family, role)."""
    if is_admin(actor_tg_id):
        fam = await s.get(Family, family_id)
        if not fam:
            raise LookupError("Семья не найдена")
        return fam, "admin"

    res_u = await s.execute(select(User).where(User.telegram_id == actor_tg_id))
    user = res_u.scalar_one_or_none()
    role = _role_from_user(actor_tg_id, user)
    if role != "landlord" or user is None or user.family_id != family_id:
        raise PermissionError("Нет доступа")

    fam = await s.get(Family, family_id)
    if not fam:
        raise LookupError("Семья не найдена")
    return fam, "landlord"


async def assert_active_actor_in_session(
    s,
    actor_tg_id: int,
    allowed_roles: tuple[str, ...] = ("admin", "landlord", "tenant"),
) -> tuple[str, User | None]:
    """P1-03: verify actor is currently active (not blocked/revoked/expired) inside transaction."""
    res_u = await s.execute(select(User).where(User.telegram_id == actor_tg_id))
    user = res_u.scalar_one_or_none()
    if is_admin(actor_tg_id):
        if "admin" not in allowed_roles:
            raise PermissionError("Недостаточно прав")
        return "admin", user

    role = _role_from_user(actor_tg_id, user)
    if role not in allowed_roles or user is None:
        raise PermissionError("Нет доступа")

    if role == "tenant":
        res_apts = await s.execute(
            select(Apartment)
            .join(ApartmentUser, ApartmentUser.apartment_id == Apartment.id)
            .where(ApartmentUser.user_id == user.id)
        )
        apts = list(res_apts.scalars().all())
        if user.family_id is not None:
            apts = [a for a in apts if a.family_id == user.family_id]
        if apts and not any(is_lease_active_for_apartment(a) for a in apts):
            raise PermissionError("Срок действия договора найма истёк. Доступ прекращён.")

    return role, user


async def get_notification_recipients(apartment_id: int) -> list[int]:
    """Return active property landlords/owners only (admin receives notices ONLY if explicitly added to the family)."""
    recipients: list[int] = []
    apt = await crud.get_apartment(apartment_id)
    if not apt:
        return recipients

    for owner in await crud.get_family_owners(apt.family_id):
        if owner.telegram_id not in recipients and _is_active_owner_recipient(owner, apt.family_id):
            recipients.append(owner.telegram_id)

    for owner in await crud.get_apartment_owners(apartment_id):
        if owner.telegram_id not in recipients and _is_active_owner_recipient(owner, apt.family_id):
            recipients.append(owner.telegram_id)

    return recipients


async def has_overdue_meters_for_apartment(
    apt: Apartment,
    today: datetime.date | None = None,
) -> bool:
    """Return True if at least one active meter group passed its submission day without readings this month."""
    cur = today or config.local_today()
    enabled = set(get_apartment_enabled_meters(apt))
    last_day = calendar.monthrange(cur.year, cur.month)[1]

    groups = (
        (apt.water_submission_day, [m for m in WATER_SUBMISSION_METERS if m in enabled]),
        (apt.electricity_submission_day, [m for m in ELECTRICITY_METERS if m in enabled]),
        (apt.gas_submission_day, [m for m in GAS_METERS if m in enabled]),
    )
    for configured_day, active_meters in groups:
        if not configured_day or not active_meters:
            continue
        eff_day = min(configured_day, last_day)
        if cur.day >= eff_day:
            if not await crud.has_readings_this_month(apt.id, active_meters, today=cur):
                return True
    return False

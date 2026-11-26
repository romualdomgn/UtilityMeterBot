"""Database CRUD operations and serialized UnitOfWork."""

from __future__ import annotations

import asyncio
import datetime
from decimal import Decimal
from typing import Iterable
from weakref import WeakKeyDictionary

from sqlalchemy import and_, desc, extract, func, or_, select

import config
from database.models import (
    ALL_METERS,
    ALL_TARIFF_TYPES,
    Apartment,
    ApartmentUser,
    AuditLog,
    Family,
    FamilyOwner,
    InitialReading,
    MeterDevice,
    MeterReading,
    MeterReplacement,
    SchedulerRun,
    Tariff,
    User,
    serialize_enabled_meters,
)
from database.session import async_session_factory

ALLOWED_APARTMENT_FIELDS = frozenset({
    "number",
    "property_type",
    "enabled_meters",
    "irrigation_mode",
    "has_tenant",
    "agreement_no",
    "agreement_date_start",
    "agreement_date_finish",
    "rent_payment_day",
    "rent_reminder_enabled",
    "rent_reminder_mode",
    "rent_reminder_days_after",
    "water_submission_day",
    "electricity_submission_day",
    "gas_submission_day",
    "sort_order",
})

_LOOP_WRITE_LOCKS: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = WeakKeyDictionary()


def _get_write_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _LOOP_WRITE_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _LOOP_WRITE_LOCKS[loop] = lock
    return lock


class UnitOfWork:
    """Async context manager ensuring sessions always close in finally and serializing writes."""

    def __init__(self, write: bool = False) -> None:
        self.session = None
        self._write = write
        self._lock: asyncio.Lock | None = None

    async def __aenter__(self):
        if self._write:
            self._lock = _get_write_lock()
            await self._lock.acquire()
        try:
            self.session = async_session_factory()
            return self.session
        except Exception:
            if self._lock is not None:
                self._lock.release()
                self._lock = None
            raise

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        try:
            if self.session is not None:
                try:
                    if exc_type:
                        await self.session.rollback()
                    else:
                        await self.session.commit()
                finally:
                    await self.session.close()
        finally:
            if self._lock is not None:
                self._lock.release()
                self._lock = None


async def get_all_families() -> list[Family]:
    async with UnitOfWork() as s:
        res = await s.execute(select(Family).order_by(Family.id))
        return list(res.scalars().all())


async def get_family(family_id: int) -> Family | None:
    async with UnitOfWork() as s:
        return await s.get(Family, family_id)


async def create_family(name: str) -> Family:
    async with UnitOfWork(write=True) as s:
        fam = Family(name=name)
        s.add(fam)
        await s.flush()
        await s.refresh(fam)
        return fam


async def rename_family(family_id: int, new_name: str) -> bool:
    async with UnitOfWork(write=True) as s:
        fam = await s.get(Family, family_id)
        if not fam:
            return False
        fam.name = new_name
        return True


async def get_family_owners(family_id: int) -> list[User]:
    """Return family landlords plus any explicit FamilyOwner members (including admin)."""
    async with UnitOfWork() as s:
        subq = select(FamilyOwner.user_id).where(FamilyOwner.family_id == family_id)
        res = await s.execute(
            select(User)
            .where(
                or_(
                    and_(User.family_id == family_id, User.role == "landlord"),
                    User.id.in_(subq),
                )
            )
            .order_by(User.id)
        )
        return list(res.scalars().all())


async def get_users_by_family(family_id: int) -> list[User]:
    async with UnitOfWork() as s:
        res = await s.execute(select(User).where(User.family_id == family_id).order_by(User.id))
        return list(res.scalars().all())


async def get_user_by_telegram_id(telegram_id: int) -> User | None:
    async with UnitOfWork() as s:
        res = await s.execute(select(User).where(User.telegram_id == telegram_id))
        return res.scalar_one_or_none()


async def get_all_users() -> list[User]:
    async with UnitOfWork() as s:
        res = await s.execute(select(User).order_by(User.role, User.id))
        return list(res.scalars().all())


async def get_family_scoped_users(family_id: int) -> list[User]:
    """Return users belonging to a family (owners, linked tenants, and attached FamilyOwner)."""
    async with UnitOfWork() as s:
        owner_subq = select(FamilyOwner.user_id).where(FamilyOwner.family_id == family_id)
        tenant_subq = (
            select(ApartmentUser.user_id)
            .join(Apartment, Apartment.id == ApartmentUser.apartment_id)
            .where(Apartment.family_id == family_id)
        )
        res = await s.execute(
            select(User)
            .where(
                or_(
                    User.family_id == family_id,
                    User.id.in_(owner_subq),
                    User.id.in_(tenant_subq),
                )
            )
            .order_by(User.role, User.id)
        )
        return list(res.scalars().all())


async def get_or_create_user(
    telegram_id: int,
    name: str | None = None,
    phone: str | None = None,
    role: str = "tenant",
    family_id: int | None = None,
) -> tuple[User, bool]:
    async with UnitOfWork(write=True) as s:
        res = await s.execute(select(User).where(User.telegram_id == telegram_id))
        user = res.scalar_one_or_none()
        if user:
            if name and not user.name:
                user.name = name
            if phone and not user.phone:
                user.phone = phone
            if family_id is not None and user.family_id is None:
                user.family_id = family_id
            return user, False

        user = User(
            telegram_id=telegram_id,
            name=name,
            phone=phone,
            role=role,
            family_id=family_id,
        )
        s.add(user)
        await s.flush()
        await s.refresh(user)
        return user, True


async def update_user_name(telegram_id: int, name: str) -> User | None:
    """Allow an authenticated user (admin/landlord/tenant) to update their own display name."""
    async with UnitOfWork(write=True) as s:
        res = await s.execute(select(User).where(User.telegram_id == telegram_id))
        user = res.scalar_one_or_none()
        if not user:
            if config.ADMIN_TELEGRAM_ID and telegram_id == config.ADMIN_TELEGRAM_ID:
                user = User(telegram_id=telegram_id, name=name, role="admin")
                s.add(user)
                await s.flush()
                await s.refresh(user)
                return user
            return None
        user.name = name
        await s.flush()
        await s.refresh(user)
        return user


async def update_family_owner(
    family_id: int,
    owner_tg_id: int,
    name: str,
    phone: str | None = None,
    update_phone: bool = False,
) -> User | None:
    """Update a landlord or linked admin owner's name (and optionally phone) within a family."""
    async with UnitOfWork(write=True) as s:
        subq = select(FamilyOwner.user_id).where(FamilyOwner.family_id == family_id)
        res = await s.execute(
            select(User).where(
                User.telegram_id == owner_tg_id,
                or_(
                    and_(User.family_id == family_id, User.role == "landlord"),
                    User.id.in_(subq),
                ),
            )
        )
        target = res.scalar_one_or_none()
        if not target:
            return None
        target.name = name
        if update_phone:
            target.phone = phone
        await s.flush()
        await s.refresh(target)
        return target


async def sync_admin_user(admin_tg_id: int) -> None:
    """Revoke old admin roles in DB; set only current ADMIN_TELEGRAM_ID."""
    if not admin_tg_id:
        return
    async with UnitOfWork(write=True) as s:
        res = await s.execute(select(User).where(User.role == "admin", User.telegram_id != admin_tg_id))
        for old in res.scalars().all():
            old.role = "revoked"

        res_cur = await s.execute(select(User).where(User.telegram_id == admin_tg_id))
        cur = res_cur.scalar_one_or_none()
        if cur:
            cur.role = "admin"
            cur.is_blocked = False
        else:
            s.add(User(telegram_id=admin_tg_id, name="Администратор", role="admin"))


async def get_apartment(apartment_id: int) -> Apartment | None:
    async with UnitOfWork() as s:
        return await s.get(Apartment, apartment_id)


async def get_apartments_by_family(family_id: int) -> list[Apartment]:
    async with UnitOfWork() as s:
        res = await s.execute(
            select(Apartment)
            .where(Apartment.family_id == family_id)
            .order_by(Apartment.sort_order, Apartment.id)
        )
        return list(res.scalars().all())


async def get_all_apartments() -> list[Apartment]:
    async with UnitOfWork() as s:
        res = await s.execute(select(Apartment).order_by(Apartment.sort_order, Apartment.id))
        return list(res.scalars().all())


async def create_apartment(
    number: str,
    family_id: int,
    property_type: str = "apartment",
    enabled_meters: Iterable[str] | None = None,
    irrigation_mode: str = "parallel",
) -> Apartment:
    meters_str = serialize_enabled_meters(enabled_meters) if enabled_meters is not None else None
    async with UnitOfWork(write=True) as s:
        max_order_res = await s.execute(select(func.coalesce(func.max(Apartment.sort_order), 0)))
        next_order = int(max_order_res.scalar() or 0) + 1
        apt = Apartment(
            number=number,
            family_id=family_id,
            property_type=property_type,
            enabled_meters=meters_str or None,
            irrigation_mode=irrigation_mode,
            sort_order=next_order,
        )
        s.add(apt)
        await s.flush()
        await s.refresh(apt)
        return apt


async def update_apartment(apartment_id: int, **kwargs) -> Apartment | None:
    async with UnitOfWork(write=True) as s:
        apt = await s.get(Apartment, apartment_id)
        if not apt:
            return None
        for k, v in kwargs.items():
            if k in ALLOWED_APARTMENT_FIELDS:
                setattr(apt, k, v)
        await s.flush()
        await s.refresh(apt)
        return apt


async def is_user_linked_to_apartment(apartment_id: int, user_id: int) -> bool:
    async with UnitOfWork() as s:
        res = await s.execute(
            select(ApartmentUser.id).where(
                ApartmentUser.apartment_id == apartment_id,
                ApartmentUser.user_id == user_id,
            ).limit(1)
        )
        return res.scalar_one_or_none() is not None


async def link_user_to_apartment(apartment_id: int, user_id: int, role: str = "tenant") -> None:
    async with UnitOfWork(write=True) as s:
        res = await s.execute(
            select(ApartmentUser).where(
                ApartmentUser.apartment_id == apartment_id,
                ApartmentUser.user_id == user_id,
            )
        )
        existing = res.scalar_one_or_none()
        if existing:
            existing.role = role
        else:
            s.add(ApartmentUser(apartment_id=apartment_id, user_id=user_id, role=role))


async def get_apartments_for_user(telegram_id: int) -> list[Apartment]:
    async with UnitOfWork() as s:
        res = await s.execute(
            select(Apartment)
            .join(ApartmentUser, ApartmentUser.apartment_id == Apartment.id)
            .join(User, User.id == ApartmentUser.user_id)
            .where(User.telegram_id == telegram_id)
            .order_by(Apartment.sort_order, Apartment.id)
        )
        return list(res.scalars().all())


async def get_apartment_tenants(apartment_id: int) -> list[User]:
    async with UnitOfWork() as s:
        res = await s.execute(
            select(User)
            .join(ApartmentUser, ApartmentUser.user_id == User.id)
            .where(ApartmentUser.apartment_id == apartment_id, ApartmentUser.role == "tenant")
            .order_by(User.id)
        )
        return list(res.scalars().all())


async def get_tenants_for_families(family_ids: list[int]) -> list[tuple[User, Apartment]]:
    """Return (User, Apartment) pairs for all tenants linked to apartments in the given families."""
    if not family_ids:
        return []
    async with UnitOfWork() as s:
        res = await s.execute(
            select(User, Apartment)
            .join(ApartmentUser, ApartmentUser.user_id == User.id)
            .join(Apartment, Apartment.id == ApartmentUser.apartment_id)
            .where(
                Apartment.family_id.in_(family_ids),
                ApartmentUser.role == "tenant",
                User.role == "tenant",
            )
            .order_by(User.id, Apartment.sort_order, Apartment.id)
        )
        return list(res.all())


async def get_apartment_ids_with_tenants() -> set[int]:
    """Return IDs of properties that have at least one linked tenant."""
    async with UnitOfWork() as s:
        res = await s.execute(
            select(ApartmentUser.apartment_id)
            .where(ApartmentUser.role == "tenant")
            .distinct()
        )
        return {int(row) for row in res.scalars().all()}


async def get_apartment_owners(apartment_id: int) -> list[User]:
    async with UnitOfWork() as s:
        res = await s.execute(
            select(User)
            .join(ApartmentUser, ApartmentUser.user_id == User.id)
            .where(ApartmentUser.apartment_id == apartment_id, ApartmentUser.role == "owner")
            .order_by(User.id)
        )
        return list(res.scalars().all())


async def set_tariff(
    apartment_id: int,
    tariff_type: str,
    rate: Decimal,
    effective_from: datetime.date,
    changed_by: int,
) -> Tariff:
    async with UnitOfWork(write=True) as s:
        tariff = Tariff(
            apartment_id=apartment_id,
            tariff_type=tariff_type,
            rate=rate,
            effective_from=effective_from,
            changed_by=changed_by,
        )
        s.add(tariff)
        await s.flush()
        await s.refresh(tariff)
        return tariff


async def get_latest_tariff(
    apartment_id: int,
    tariff_type: str,
    as_of_date: datetime.date | None = None,
) -> Tariff | None:
    """Query historical tariff effective as of target date."""
    target_date = as_of_date or config.local_today()
    async with UnitOfWork() as s:
        res = await s.execute(
            select(Tariff)
            .where(
                Tariff.apartment_id == apartment_id,
                Tariff.tariff_type == tariff_type,
                Tariff.effective_from <= target_date,
            )
            .order_by(desc(Tariff.effective_from), desc(Tariff.id))
            .limit(1)
        )
        return res.scalar_one_or_none()


async def get_all_tariffs(
    apartment_id: int,
    as_of_date: datetime.date | None = None,
) -> dict[str, Tariff | None]:
    """Fetch latest effective tariff per meter in a single session and query."""
    target_date = as_of_date or config.local_today()
    result: dict[str, Tariff | None] = {mt: None for mt in ALL_TARIFF_TYPES}
    async with UnitOfWork() as s:
        res = await s.execute(
            select(Tariff)
            .where(
                Tariff.apartment_id == apartment_id,
                Tariff.effective_from <= target_date,
            )
            .order_by(desc(Tariff.effective_from), desc(Tariff.id))
        )
        for row in res.scalars().all():
            if row.tariff_type in result and result[row.tariff_type] is None:
                result[row.tariff_type] = row
    return result


async def get_tariff_history(apartment_id: int, limit: int = 50) -> list[Tariff]:
    async with UnitOfWork() as s:
        res = await s.execute(
            select(Tariff)
            .where(Tariff.apartment_id == apartment_id)
            .order_by(desc(Tariff.effective_from), desc(Tariff.id))
            .limit(limit)
        )
        return list(res.scalars().all())


async def get_initial_reading(apartment_id: int, meter_type: str) -> InitialReading | None:
    async with UnitOfWork() as s:
        res = await s.execute(
            select(InitialReading).where(
                InitialReading.apartment_id == apartment_id,
                InitialReading.meter_type == meter_type,
            )
        )
        return res.scalar_one_or_none()


async def get_all_initial_readings(apartment_id: int) -> dict[str, int | None]:
    """Missing initial reading is None, not 0."""
    async with UnitOfWork() as s:
        res = await s.execute(
            select(InitialReading).where(InitialReading.apartment_id == apartment_id)
        )
        rows = res.scalars().all()
        result: dict[str, int | None] = {mt: None for mt in ALL_METERS}
        for r in rows:
            if r.meter_type in result:
                result[r.meter_type] = r.value
        return result


async def get_latest_reading(apartment_id: int, meter_type: str) -> MeterReading | None:
    async with UnitOfWork() as s:
        res = await s.execute(
            select(MeterReading)
            .where(
                MeterReading.apartment_id == apartment_id,
                MeterReading.meter_type == meter_type,
            )
            .order_by(desc(MeterReading.reading_date), desc(MeterReading.id))
            .limit(1)
        )
        return res.scalar_one_or_none()


async def get_reading_by_id(reading_id: int) -> MeterReading | None:
    async with UnitOfWork() as s:
        return await s.get(MeterReading, reading_id)


async def update_reading(reading_id: int, new_value: int) -> MeterReading | None:
    async with UnitOfWork(write=True) as s:
        rec = await s.get(MeterReading, reading_id)
        if not rec:
            return None
        rec.value = new_value
        await s.flush()
        await s.refresh(rec)
        return rec


async def get_all_readings(apartment_id: int, limit: int = 200) -> list[MeterReading]:
    async with UnitOfWork() as s:
        res = await s.execute(
            select(MeterReading)
            .where(MeterReading.apartment_id == apartment_id)
            .order_by(desc(MeterReading.reading_date), desc(MeterReading.id))
            .limit(limit)
        )
        return list(res.scalars().all())


async def get_readings_count(apartment_id: int) -> int:
    async with UnitOfWork() as s:
        res = await s.execute(
            select(func.count(MeterReading.id)).where(MeterReading.apartment_id == apartment_id)
        )
        return int(res.scalar() or 0)


async def get_reading_months(apartment_id: int) -> list[tuple[int, int]]:
    async with UnitOfWork() as s:
        res = await s.execute(
            select(
                extract("year", MeterReading.reading_date).label("y"),
                extract("month", MeterReading.reading_date).label("m"),
            )
            .where(MeterReading.apartment_id == apartment_id)
            .distinct()
            .order_by(desc("y"), desc("m"))
        )
        return [(int(row.y), int(row.m)) for row in res.all()]


async def get_readings_by_month(
    apartment_id: int, year: int, month: int
) -> list[MeterReading]:
    first_day = datetime.date(year, month, 1)
    next_month = (
        datetime.date(year + 1, 1, 1)
        if month == 12
        else datetime.date(year, month + 1, 1)
    )
    async with UnitOfWork() as s:
        res = await s.execute(
            select(MeterReading)
            .where(
                and_(
                    MeterReading.apartment_id == apartment_id,
                    MeterReading.reading_date >= first_day,
                    MeterReading.reading_date < next_month,
                )
            )
            .order_by(desc(MeterReading.reading_date), desc(MeterReading.id))
        )
        return list(res.scalars().all())


async def get_latest_reading_before(
    apartment_id: int, meter_type: str, before_date: datetime.date
) -> MeterReading | None:
    async with UnitOfWork() as s:
        res = await s.execute(
            select(MeterReading)
            .where(
                MeterReading.apartment_id == apartment_id,
                MeterReading.meter_type == meter_type,
                MeterReading.reading_date < before_date,
            )
            .order_by(desc(MeterReading.reading_date), desc(MeterReading.id))
            .limit(1)
        )
        return res.scalar_one_or_none()


async def has_readings_this_month(
    apartment_id: int, meter_types: Iterable[str], today: datetime.date | None = None
) -> bool:
    """LOW-04: bounded strictly to the current calendar month and excludes replacement-init rows."""
    cur = today or config.local_today()
    first_day = cur.replace(day=1)
    next_month = (
        datetime.date(cur.year + 1, 1, 1)
        if cur.month == 12
        else datetime.date(cur.year, cur.month + 1, 1)
    )
    types_list = list(meter_types)
    if not types_list:
        return True
    async with UnitOfWork() as s:
        res = await s.execute(
            select(MeterReading.meter_type)
            .where(
                MeterReading.apartment_id == apartment_id,
                MeterReading.meter_type.in_(types_list),
                MeterReading.reading_date >= first_day,
                MeterReading.reading_date < next_month,
                MeterReading.is_replacement_init.is_(False),
            )
            .distinct()
        )
        found = set(res.scalars().all())
        return all(mt in found for mt in types_list)


async def get_meter_devices(apartment_id: int) -> dict[str, MeterDevice]:
    async with UnitOfWork() as s:
        res = await s.execute(
            select(MeterDevice).where(MeterDevice.apartment_id == apartment_id)
        )
        return {d.meter_type: d for d in res.scalars().all()}


async def get_all_meter_devices_with_apartments() -> list[tuple[MeterDevice, Apartment]]:
    async with UnitOfWork() as s:
        res = await s.execute(
            select(MeterDevice, Apartment)
            .join(Apartment, Apartment.id == MeterDevice.apartment_id)
            .where(MeterDevice.verification_due_date.isnot(None))
        )
        return list(res.all())


async def get_meter_replacements_by_month(
    apartment_id: int, year: int, month: int
) -> list[MeterReplacement]:
    first_day = datetime.date(year, month, 1)
    next_month = (
        datetime.date(year + 1, 1, 1)
        if month == 12
        else datetime.date(year, month + 1, 1)
    )
    async with UnitOfWork() as s:
        res = await s.execute(
            select(MeterReplacement)
            .where(
                MeterReplacement.apartment_id == apartment_id,
                MeterReplacement.replacement_date >= first_day,
                MeterReplacement.replacement_date < next_month,
            )
            .order_by(MeterReplacement.replacement_date, MeterReplacement.id)
        )
        return list(res.scalars().all())


async def get_meter_replacements(apartment_id: int, limit: int = 50) -> list[MeterReplacement]:
    async with UnitOfWork() as s:
        res = await s.execute(
            select(MeterReplacement)
            .where(MeterReplacement.apartment_id == apartment_id)
            .order_by(desc(MeterReplacement.replacement_date), desc(MeterReplacement.id))
            .limit(limit)
        )
        return list(res.scalars().all())


async def has_scheduler_run(job_key: str, run_date: datetime.date) -> bool:
    async with UnitOfWork() as s:
        res = await s.execute(
            select(SchedulerRun.id)
            .where(SchedulerRun.job_key == job_key, SchedulerRun.run_date == run_date)
            .limit(1)
        )
        return res.scalar_one_or_none() is not None


async def record_scheduler_run(job_key: str, run_date: datetime.date) -> bool:
    """Record job execution for run_date; return False if already recorded."""
    async with UnitOfWork(write=True) as s:
        res = await s.execute(
            select(SchedulerRun.id)
            .where(SchedulerRun.job_key == job_key, SchedulerRun.run_date == run_date)
            .limit(1)
        )
        if res.scalar_one_or_none() is not None:
            return False
        s.add(SchedulerRun(job_key=job_key, run_date=run_date))
        return True


async def add_audit_log(
    user_telegram_id: int,
    action: str,
    details: str | None = None,
) -> AuditLog:
    async with UnitOfWork(write=True) as s:
        log = AuditLog(
            user_telegram_id=user_telegram_id,
            action=action,
            details=details,
        )
        s.add(log)
        await s.flush()
        await s.refresh(log)
        return log


async def get_audit_logs(limit: int = 50) -> list[AuditLog]:
    async with UnitOfWork() as s:
        res = await s.execute(
            select(AuditLog).order_by(desc(AuditLog.created_at), desc(AuditLog.id)).limit(limit)
        )
        return list(res.scalars().all())


async def get_family_audit_logs(family_id: int, limit: int = 50) -> list[AuditLog]:
    """Return audit log entries performed strictly by users whose primary family_id matches."""
    async with UnitOfWork() as s:
        tg_subq = select(User.telegram_id).where(User.family_id == family_id)
        res = await s.execute(
            select(AuditLog)
            .where(AuditLog.user_telegram_id.in_(tg_subq))
            .order_by(desc(AuditLog.created_at), desc(AuditLog.id))
            .limit(limit)
        )
        return list(res.scalars().all())

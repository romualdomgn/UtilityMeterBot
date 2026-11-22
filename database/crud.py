"""Database CRUD operations."""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Iterable

from sqlalchemy import and_, desc, extract, func, select

import config
from database.models import (
    ALL_METERS,
    Apartment,
    ApartmentUser,
    AuditLog,
    Family,
    InitialReading,
    MeterReading,
    Tariff,
    User,
)
from database.session import async_session_factory


class UnitOfWork:
    """Async context manager ensuring sessions always close in finally (MEDIUM-08)."""

    def __init__(self) -> None:
        self.session = None

    async def __aenter__(self):
        self.session = async_session_factory()
        return self.session

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        try:
            if exc_type:
                await self.session.rollback()
            else:
                await self.session.commit()
        finally:
            await self.session.close()


async def get_all_families() -> list[Family]:
    async with UnitOfWork() as s:
        res = await s.execute(select(Family).order_by(Family.id))
        return list(res.scalars().all())


async def get_family(family_id: int) -> Family | None:
    async with UnitOfWork() as s:
        return await s.get(Family, family_id)


async def create_family(name: str) -> Family:
    async with UnitOfWork() as s:
        fam = Family(name=name)
        s.add(fam)
        await s.flush()
        await s.refresh(fam)
        return fam


async def rename_family(family_id: int, new_name: str) -> None:
    async with UnitOfWork() as s:
        fam = await s.get(Family, family_id)
        if fam:
            fam.name = new_name


async def get_family_owners(family_id: int) -> list[User]:
    async with UnitOfWork() as s:
        res = await s.execute(
            select(User)
            .where(User.family_id == family_id, User.role == "landlord")
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


async def get_or_create_user(
    telegram_id: int,
    name: str | None = None,
    phone: str | None = None,
    role: str = "tenant",
    family_id: int | None = None,
) -> tuple[User, bool]:
    async with UnitOfWork() as s:
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


async def sync_admin_user(admin_tg_id: int) -> None:
    """CRITICAL-02: revoke old admin roles in DB; set only current ADMIN_TELEGRAM_ID."""
    if not admin_tg_id:
        return
    async with UnitOfWork() as s:
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
            select(Apartment).where(Apartment.family_id == family_id).order_by(Apartment.id)
        )
        return list(res.scalars().all())


async def get_all_apartments() -> list[Apartment]:
    async with UnitOfWork() as s:
        res = await s.execute(select(Apartment).order_by(Apartment.id))
        return list(res.scalars().all())


async def create_apartment(number: str, family_id: int) -> Apartment:
    async with UnitOfWork() as s:
        apt = Apartment(number=number, family_id=family_id)
        s.add(apt)
        await s.flush()
        await s.refresh(apt)
        return apt


async def update_apartment(apartment_id: int, **kwargs) -> Apartment | None:
    async with UnitOfWork() as s:
        apt = await s.get(Apartment, apartment_id)
        if not apt:
            return None
        for k, v in kwargs.items():
            if hasattr(apt, k):
                setattr(apt, k, v)
        await s.flush()
        await s.refresh(apt)
        return apt


async def link_user_to_apartment(apartment_id: int, user_id: int, role: str = "tenant") -> None:
    async with UnitOfWork() as s:
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
            .order_by(Apartment.id)
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
    async with UnitOfWork() as s:
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
    """HIGH-02: query historical tariff effective as of target date."""
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
    return {
        mt: await get_latest_tariff(apartment_id, mt, as_of_date=as_of_date)
        for mt in ALL_METERS
    }


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
    """HIGH-05: missing initial reading is None, not 0."""
    async with UnitOfWork() as s:
        res = await s.execute(
            select(InitialReading).where(InitialReading.apartment_id == apartment_id)
        )
        rows = res.scalars().all()
        result: dict[str, int | None] = {mt: None for mt in ALL_METERS}
        for r in rows:
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
    async with UnitOfWork() as s:
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
    cur = today or config.local_today()
    first_day = cur.replace(day=1)
    types_list = list(meter_types)
    async with UnitOfWork() as s:
        res = await s.execute(
            select(MeterReading.meter_type)
            .where(
                MeterReading.apartment_id == apartment_id,
                MeterReading.meter_type.in_(types_list),
                MeterReading.reading_date >= first_day,
            )
            .distinct()
        )
        found = set(res.scalars().all())
        return all(mt in found for mt in types_list)


async def add_audit_log(
    user_telegram_id: int,
    action: str,
    details: str | None = None,
) -> AuditLog:
    async with UnitOfWork() as s:
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

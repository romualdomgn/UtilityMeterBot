"""Multi-entity atomic operations (P0 isolation and P1 group consistency)."""

from __future__ import annotations

import datetime
from decimal import Decimal

from sqlalchemy import delete, desc, select

import config
from database.crud import UnitOfWork
from database.models import (
    Apartment,
    ApartmentUser,
    AuditLog,
    Family,
    InitialReading,
    MeterReading,
    Tariff,
    User,
)


async def save_meter_group_readings(
    apartment_id: int,
    meters: dict[str, int],
    submitted_by: int,
    reading_date: datetime.date,
    action: str,
    audit_details: str,
    dedup_window_seconds: int = 10,
) -> bool:
    """HIGH-03 & HIGH-06: save meter readings group and audit log atomically with deduplication."""
    now = datetime.datetime.utcnow()
    window_start = now - datetime.timedelta(seconds=dedup_window_seconds)

    async with UnitOfWork() as s:
        res_recent = await s.execute(
            select(MeterReading)
            .where(
                MeterReading.apartment_id == apartment_id,
                MeterReading.submitted_by == submitted_by,
                MeterReading.created_at >= window_start,
            )
            .order_by(desc(MeterReading.id))
            .limit(len(meters))
        )
        recent_rows = res_recent.scalars().all()
        if recent_rows and len(recent_rows) == len(meters):
            recent_map = {r.meter_type: r.value for r in recent_rows}
            if all(recent_map.get(mt) == val for mt, val in meters.items()):
                return False

        for mt, val in meters.items():
            s.add(MeterReading(
                apartment_id=apartment_id,
                meter_type=mt,
                value=val,
                submitted_by=submitted_by,
                reading_date=reading_date,
            ))

        s.add(AuditLog(
            user_telegram_id=submitted_by,
            action=action,
            details=audit_details,
        ))
        return True


async def update_apartment_settings_atomic(
    apartment_id: int,
    water_submission_day: int | None,
    electricity_submission_day: int | None,
    tariffs: dict[str, Decimal],
    changed_by: int,
    effective_from: datetime.date,
) -> None:
    """MEDIUM-10: validate and save days and tariffs in one transaction."""
    async with UnitOfWork() as s:
        apt = await s.get(Apartment, apartment_id)
        if not apt:
            raise LookupError("Квартира не найдена")

        apt.water_submission_day = water_submission_day
        apt.electricity_submission_day = electricity_submission_day

        for mt, rate in tariffs.items():
            s.add(Tariff(
                apartment_id=apartment_id,
                tariff_type=mt,
                rate=rate,
                effective_from=effective_from,
                changed_by=changed_by,
            ))

        s.add(AuditLog(
            user_telegram_id=changed_by,
            action="update_settings",
            details=f"Кв. id={apartment_id}, тарифов обновлено: {len(tariffs)}",
        ))


async def update_initial_readings_atomic(
    apartment_id: int,
    readings: dict[str, int],
    set_by: int,
) -> None:
    """HIGH-05 & MEDIUM-10: atomically update initial readings."""
    now = datetime.datetime.utcnow()
    async with UnitOfWork() as s:
        apt = await s.get(Apartment, apartment_id)
        if not apt:
            raise LookupError("Квартира не найдена")

        for mt, val in readings.items():
            res = await s.execute(
                select(InitialReading).where(
                    InitialReading.apartment_id == apartment_id,
                    InitialReading.meter_type == mt,
                )
            )
            rec = res.scalar_one_or_none()
            if rec:
                rec.value = val
                rec.set_by = set_by
                rec.updated_at = now
            else:
                s.add(InitialReading(
                    apartment_id=apartment_id,
                    meter_type=mt,
                    value=val,
                    set_by=set_by,
                ))

        s.add(AuditLog(
            user_telegram_id=set_by,
            action="set_initial_readings",
            details=f"Кв. id={apartment_id}, счётчиков: {len(readings)}",
        ))


async def delete_family_owner_atomic(
    family_id: int,
    owner_tg_id: int,
    actor_tg_id: int,
) -> None:
    """CRITICAL-01: verify family membership and landlord role before delete."""
    if owner_tg_id == config.ADMIN_TELEGRAM_ID:
        raise PermissionError("Нельзя удалить администратора")

    async with UnitOfWork() as s:
        res = await s.execute(select(User).where(User.telegram_id == owner_tg_id))
        target = res.scalar_one_or_none()
        if not target or target.family_id != family_id or target.role != "landlord":
            raise LookupError("Владелец не найден в этой семье")

        await s.delete(target)
        s.add(AuditLog(
            user_telegram_id=actor_tg_id,
            action="delete_family_owner",
            details=f"Владелец ID={owner_tg_id} удалён из семьи id={family_id}",
        ))


async def delete_family_atomic(family_id: int, actor_tg_id: int) -> None:
    """MEDIUM-01: delete family and cascade-revoke its orphaned users."""
    async with UnitOfWork() as s:
        fam = await s.get(Family, family_id)
        if not fam:
            raise LookupError("Семья не найдена")

        res_users = await s.execute(select(User).where(User.family_id == family_id))
        fam_users = list(res_users.scalars().all())

        for u in fam_users:
            if u.telegram_id == config.ADMIN_TELEGRAM_ID:
                continue
            await s.execute(delete(ApartmentUser).where(ApartmentUser.user_id == u.id))
            await s.delete(u)

        await s.delete(fam)
        s.add(AuditLog(
            user_telegram_id=actor_tg_id,
            action="delete_family",
            details=f"Семья id={family_id} удалена",
        ))


async def remove_tenant_from_apartment_atomic(
    apartment_id: int,
    tenant_tg_id: int,
    actor_tg_id: int,
) -> tuple[str, bool]:
    """CRITICAL-03 & MEDIUM-04: verify tenant link to apt_id, unlink atomically.

    Returns (apartment_number, fully_removed).
    """
    if tenant_tg_id == config.ADMIN_TELEGRAM_ID:
        raise PermissionError("Нельзя удалить администратора")

    async with UnitOfWork() as s:
        apt = await s.get(Apartment, apartment_id)
        if not apt:
            raise LookupError("Квартира не найдена")

        res_u = await s.execute(select(User).where(User.telegram_id == tenant_tg_id))
        target = res_u.scalar_one_or_none()
        if not target or target.role != "tenant":
            raise LookupError("Наниматель не найден в этой квартире")

        res_link = await s.execute(
            select(ApartmentUser).where(
                ApartmentUser.apartment_id == apartment_id,
                ApartmentUser.user_id == target.id,
                ApartmentUser.role == "tenant",
            )
        )
        link = res_link.scalar_one_or_none()
        if not link:
            raise LookupError("Наниматель не привязан к этой квартире")

        await s.delete(link)
        await s.flush()

        res_other = await s.execute(
            select(ApartmentUser.id).where(ApartmentUser.user_id == target.id).limit(1)
        )
        fully_removed = res_other.scalar_one_or_none() is None
        if fully_removed:
            await s.delete(target)

        res_rem = await s.execute(
            select(ApartmentUser.id)
            .where(ApartmentUser.apartment_id == apartment_id, ApartmentUser.role == "tenant")
            .limit(1)
        )
        if res_rem.scalar_one_or_none() is None:
            apt.has_tenant = False

        s.add(AuditLog(
            user_telegram_id=actor_tg_id,
            action="remove_tenant",
            details=f"Наниматель ID={tenant_tg_id} удалён из кв. №{apt.number} (full={fully_removed})",
        ))
        return apt.number, fully_removed

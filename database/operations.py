"""Multi-entity atomic operations (P0 isolation and P1 group consistency)."""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Iterable

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
    format_property_short,
    serialize_enabled_meters,
    utcnow_naive,
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
    now = utcnow_naive()
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
    gas_submission_day: int | None = None,
    number: str | None = None,
    property_type: str | None = None,
    enabled_meters: Iterable[str] | None = None,
) -> int:
    """MEDIUM-10: validate and save property settings, days, and changed tariffs in one transaction.

    Returns the number of tariff rows actually inserted (unchanged rates are skipped).
    """
    async with UnitOfWork() as s:
        apt = await s.get(Apartment, apartment_id)
        if not apt:
            raise LookupError("Объект недвижимости не найден")

        if number is not None:
            apt.number = number
        if property_type is not None:
            apt.property_type = property_type
        if enabled_meters is not None:
            apt.enabled_meters = serialize_enabled_meters(enabled_meters)

        apt.water_submission_day = water_submission_day
        apt.electricity_submission_day = electricity_submission_day
        apt.gas_submission_day = gas_submission_day

        inserted_tariffs = 0
        for mt, rate in tariffs.items():
            res_latest = await s.execute(
                select(Tariff)
                .where(
                    Tariff.apartment_id == apartment_id,
                    Tariff.tariff_type == mt,
                )
                .order_by(desc(Tariff.effective_from), desc(Tariff.id))
                .limit(1)
            )
            latest = res_latest.scalar_one_or_none()
            if latest is not None and Decimal(latest.rate) == rate:
                continue

            s.add(Tariff(
                apartment_id=apartment_id,
                tariff_type=mt,
                rate=rate,
                effective_from=effective_from,
                changed_by=changed_by,
            ))
            inserted_tariffs += 1

        s.add(AuditLog(
            user_telegram_id=changed_by,
            action="update_settings",
            details=f"{format_property_short(apt)} (id={apartment_id}), тарифов обновлено: {inserted_tariffs}",
        ))
        return inserted_tariffs


async def update_initial_readings_atomic(
    apartment_id: int,
    readings: dict[str, int],
    set_by: int,
) -> None:
    """HIGH-05 & MEDIUM-10: atomically update initial readings."""
    now = utcnow_naive()
    async with UnitOfWork() as s:
        apt = await s.get(Apartment, apartment_id)
        if not apt:
            raise LookupError("Объект недвижимости не найден")

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
            details=f"{format_property_short(apt)} (id={apartment_id}), счётчиков: {len(readings)}",
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
                u.family_id = None
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

    Returns (property_label, fully_removed).
    """
    if tenant_tg_id == config.ADMIN_TELEGRAM_ID:
        raise PermissionError("Нельзя удалить администратора")

    async with UnitOfWork() as s:
        apt = await s.get(Apartment, apartment_id)
        if not apt:
            raise LookupError("Объект недвижимости не найден")

        res_u = await s.execute(select(User).where(User.telegram_id == tenant_tg_id))
        target = res_u.scalar_one_or_none()
        if not target or target.role != "tenant":
            raise LookupError("Наниматель не найден в этом объекте")

        res_link = await s.execute(
            select(ApartmentUser).where(
                ApartmentUser.apartment_id == apartment_id,
                ApartmentUser.user_id == target.id,
                ApartmentUser.role == "tenant",
            )
        )
        link = res_link.scalar_one_or_none()
        if not link:
            raise LookupError("Наниматель не привязан к этому объекту")

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

        prop_label = format_property_short(apt)
        s.add(AuditLog(
            user_telegram_id=actor_tg_id,
            action="remove_tenant",
            details=f"Наниматель ID={tenant_tg_id} удалён из {prop_label} (full={fully_removed})",
        ))
        return prop_label, fully_removed


async def delete_apartment_atomic(
    apartment_id: int,
    actor_tg_id: int,
) -> tuple[str, list[tuple[int, bool]]]:
    """Delete a property atomically with all its readings/tariffs/links and clean up orphaned tenants.

    Returns (property_label, [(tenant_telegram_id, fully_removed), ...]).
    """
    async with UnitOfWork() as s:
        apt = await s.get(Apartment, apartment_id)
        if not apt:
            raise LookupError("Объект недвижимости не найден")

        prop_label = format_property_short(apt)

        res_tenants = await s.execute(
            select(User)
            .join(ApartmentUser, ApartmentUser.user_id == User.id)
            .where(
                ApartmentUser.apartment_id == apartment_id,
                User.role == "tenant",
            )
        )
        linked_tenants = list(res_tenants.scalars().all())

        await s.delete(apt)
        await s.flush()

        affected_tenants: list[tuple[int, bool]] = []
        for tenant in linked_tenants:
            if tenant.telegram_id == config.ADMIN_TELEGRAM_ID:
                continue
            res_other = await s.execute(
                select(ApartmentUser.id).where(ApartmentUser.user_id == tenant.id).limit(1)
            )
            fully_removed = res_other.scalar_one_or_none() is None
            if fully_removed:
                await s.delete(tenant)
            affected_tenants.append((tenant.telegram_id, fully_removed))

        s.add(AuditLog(
            user_telegram_id=actor_tg_id,
            action="delete_apartment",
            details=(
                f"Удалён объект {prop_label} (id={apartment_id}), "
                f"откреплено нанимателей: {len(affected_tenants)}"
            ),
        ))
        return prop_label, affected_tenants


async def reorder_apartments_atomic(
    apartment_ids: list[int],
    actor_tg_id: int,
) -> list[int]:
    """Persist custom display order for properties in a single transaction."""
    unique_ids: list[int] = list(dict.fromkeys(apartment_ids))
    if not unique_ids:
        return []

    async with UnitOfWork() as s:
        res = await s.execute(select(Apartment).where(Apartment.id.in_(unique_ids)))
        apt_map = {a.id: a for a in res.scalars().all()}

        updated_ids: list[int] = []
        for idx, apt_id in enumerate(unique_ids, start=1):
            apt = apt_map.get(apt_id)
            if apt is not None:
                apt.sort_order = idx
                updated_ids.append(apt_id)

        s.add(AuditLog(
            user_telegram_id=actor_tg_id,
            action="reorder_apartments",
            details=f"Порядок объектов обновлён ({len(updated_ids)} шт.)",
        ))
        return updated_ids

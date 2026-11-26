"""Multi-entity atomic operations with serialized writes (P0 isolation, idempotency, atomicity)."""

from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

from sqlalchemy import and_, delete, desc, func, or_, select
from sqlalchemy.exc import IntegrityError

import config
from database.crud import ALLOWED_APARTMENT_FIELDS, UnitOfWork
from database.models import (
    ALL_METERS,
    ELECTRICITY_METERS,
    SINGLE_ELECTRICITY_METER,
    Apartment,
    ApartmentUser,
    AuditLog,
    Family,
    FamilyOwner,
    InitialReading,
    METER_PLAIN_LABELS,
    MeterDevice,
    MeterReading,
    MeterReplacement,
    MeterSubmission,
    Tariff,
    User,
    format_property_short,
    get_apartment_enabled_meters,
    get_apartment_irrigation_mode,
    serialize_enabled_meters,
    serialize_notify_intervals,
    utcnow_naive,
)


class ConflictError(Exception):
    """Raised on uniqueness, family-isolation, or idempotency payload conflicts (HTTP 409)."""


@dataclass
class SubmissionOutcome:
    status: str  # "saved" | "deduplicated" | "rejected_decrease" | "needs_confirm_repeat"
    prev_meters: dict[str, int | None] = field(default_factory=dict)
    decreased: list[str] = field(default_factory=list)
    repeated: list[str] = field(default_factory=list)
    property_label: str = ""


def _compute_submission_hash(
    meters: dict[str, int],
    reading_date: datetime.date,
    action: str,
) -> str:
    payload = {
        "action": action,
        "reading_date": reading_date.isoformat(),
        "meters": sorted((k, int(v)) for k, v in meters.items()),
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _get_prev_value_in_session(s, apartment_id: int, meter_type: str) -> int | None:
    res = await s.execute(
        select(MeterReading)
        .where(
            MeterReading.apartment_id == apartment_id,
            MeterReading.meter_type == meter_type,
        )
        .order_by(desc(MeterReading.reading_date), desc(MeterReading.id))
        .limit(1)
    )
    latest = res.scalar_one_or_none()
    if latest is not None:
        return latest.value
    res_init = await s.execute(
        select(InitialReading).where(
            InitialReading.apartment_id == apartment_id,
            InitialReading.meter_type == meter_type,
        )
    )
    init = res_init.scalar_one_or_none()
    return init.value if init is not None else None


async def submit_meter_group_atomic(
    apartment_id: int,
    meters: dict[str, int],
    submitted_by: int,
    reading_date: datetime.date,
    action: str,
    force: bool = False,
    allow_decrease: bool = False,
    submission_id: str | None = None,
    dedup_window_seconds: int = 10,
) -> SubmissionOutcome:
    """HIGH-07 & MEDIUM-02: validate prev values, decrease/repeat rules, and save atomically."""
    if not meters:
        raise ValueError("Укажите хотя бы одно показание счётчика")

    clean_sub_id = (submission_id or "").strip() or None
    payload_hash = _compute_submission_hash(meters, reading_date, action)
    now = utcnow_naive()
    first_day = reading_date.replace(day=1)
    next_month = (
        datetime.date(reading_date.year + 1, 1, 1)
        if reading_date.month == 12
        else datetime.date(reading_date.year, reading_date.month + 1, 1)
    )

    async with UnitOfWork(write=True) as s:
        if clean_sub_id:
            res_sub = await s.execute(
                select(MeterSubmission).where(MeterSubmission.submission_id == clean_sub_id)
            )
            existing_sub = res_sub.scalar_one_or_none()
            if existing_sub is not None:
                if (
                    existing_sub.apartment_id != apartment_id
                    or existing_sub.submitted_by != submitted_by
                    or existing_sub.payload_hash != payload_hash
                ):
                    raise ConflictError(
                        "Идентификатор отправки уже использован с другими данными"
                    )
                prev_map = {
                    mt: await _get_prev_value_in_session(s, apartment_id, mt)
                    for mt in meters
                }
                return SubmissionOutcome(status="deduplicated", prev_meters=prev_map)

        apt = await s.get(Apartment, apartment_id)
        if not apt:
            raise LookupError("Объект недвижимости не найден")

        enabled = set(get_apartment_enabled_meters(apt))
        for mt in meters:
            if mt not in enabled:
                label = METER_PLAIN_LABELS.get(mt, mt)
                raise ValueError(f"Счётчик «{label}» не подключён для этого объекта")

        prev_meters = {
            mt: await _get_prev_value_in_session(s, apartment_id, mt)
            for mt in meters
        }

        decreased = [
            mt for mt, val in meters.items()
            if prev_meters[mt] is not None and val < prev_meters[mt]
        ]
        if decreased and not allow_decrease:
            return SubmissionOutcome(
                status="rejected_decrease",
                prev_meters=prev_meters,
                decreased=decreased,
                property_label=format_property_short(apt),
            )

        res_month = await s.execute(
            select(MeterReading.meter_type)
            .where(
                MeterReading.apartment_id == apartment_id,
                MeterReading.meter_type.in_(list(meters.keys())),
                MeterReading.reading_date >= first_day,
                MeterReading.reading_date < next_month,
                MeterReading.is_replacement_init.is_(False),
            )
            .distinct()
        )
        already_in_month = set(res_month.scalars().all())
        repeated = [mt for mt in meters if mt in already_in_month]
        if repeated and not force:
            return SubmissionOutcome(
                status="needs_confirm_repeat",
                prev_meters=prev_meters,
                repeated=repeated,
                property_label=format_property_short(apt),
            )

        if not clean_sub_id and dedup_window_seconds > 0:
            window_start = now - datetime.timedelta(seconds=dedup_window_seconds)
            res_recent = await s.execute(
                select(MeterReading)
                .where(
                    MeterReading.apartment_id == apartment_id,
                    MeterReading.submitted_by == submitted_by,
                    MeterReading.reading_date == reading_date,
                    MeterReading.is_replacement_init.is_(False),
                    MeterReading.created_at >= window_start,
                )
                .order_by(desc(MeterReading.id))
                .limit(len(meters))
            )
            recent_rows = res_recent.scalars().all()
            if recent_rows and len(recent_rows) == len(meters):
                recent_map = {r.meter_type: r.value for r in recent_rows}
                if all(recent_map.get(mt) == val for mt, val in meters.items()):
                    return SubmissionOutcome(
                        status="deduplicated",
                        prev_meters=prev_meters,
                        property_label=format_property_short(apt),
                    )

        if clean_sub_id:
            s.add(MeterSubmission(
                submission_id=clean_sub_id,
                apartment_id=apartment_id,
                submitted_by=submitted_by,
                payload_hash=payload_hash,
            ))

        for mt, val in meters.items():
            s.add(MeterReading(
                apartment_id=apartment_id,
                meter_type=mt,
                value=val,
                submitted_by=submitted_by,
                reading_date=reading_date,
                is_replacement_init=False,
            ))

        prop_label = format_property_short(apt)
        parts = ", ".join(f"{METER_PLAIN_LABELS.get(mt, mt)}={val}" for mt, val in meters.items())
        suffix = " (корректировка)" if (decreased and allow_decrease) else ""
        s.add(AuditLog(
            user_telegram_id=submitted_by,
            action=action,
            details=f"{prop_label} (id={apartment_id}): {parts}{suffix}",
        ))
        return SubmissionOutcome(
            status="saved",
            prev_meters=prev_meters,
            property_label=prop_label,
        )


async def save_meter_group_readings(
    apartment_id: int,
    meters: dict[str, int],
    submitted_by: int,
    reading_date: datetime.date,
    action: str,
    audit_details: str,
    dedup_window_seconds: int = 10,
    submission_id: str | None = None,
) -> bool:
    """Save meter readings group and audit log atomically with optional submission_id deduplication."""
    now = utcnow_naive()
    window_start = now - datetime.timedelta(seconds=dedup_window_seconds)
    clean_sub_id = (submission_id or "").strip() or None
    payload_hash = _compute_submission_hash(meters, reading_date, action)

    async with UnitOfWork(write=True) as s:
        if clean_sub_id:
            res_sub = await s.execute(
                select(MeterSubmission).where(MeterSubmission.submission_id == clean_sub_id)
            )
            existing_sub = res_sub.scalar_one_or_none()
            if existing_sub is not None:
                if (
                    existing_sub.apartment_id != apartment_id
                    or existing_sub.submitted_by != submitted_by
                    or existing_sub.payload_hash != payload_hash
                ):
                    raise ConflictError(
                        "Идентификатор отправки уже использован с другими данными"
                    )
                return False
            s.add(MeterSubmission(
                submission_id=clean_sub_id,
                apartment_id=apartment_id,
                submitted_by=submitted_by,
                payload_hash=payload_hash,
            ))
        else:
            res_recent = await s.execute(
                select(MeterReading)
                .where(
                    MeterReading.apartment_id == apartment_id,
                    MeterReading.submitted_by == submitted_by,
                    MeterReading.reading_date == reading_date,
                    MeterReading.is_replacement_init.is_(False),
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
                is_replacement_init=False,
            ))

        s.add(AuditLog(
            user_telegram_id=submitted_by,
            action=action,
            details=audit_details,
        ))
        return True


async def replace_meter_atomic(
    apartment_id: int,
    meter_type: str,
    old_final_value: int,
    new_initial_value: int,
    actor_tg_id: int,
    replacement_date: datetime.date,
    new_serial_number: str | None = None,
    new_verification_due_date: datetime.date | None = None,
) -> MeterReplacement:
    """HIGH-02: record physical meter replacement preserving old-meter residual consumption."""
    if meter_type not in ALL_METERS:
        raise ValueError("Неизвестный тип счётчика")

    async with UnitOfWork(write=True) as s:
        apt = await s.get(Apartment, apartment_id)
        if not apt:
            raise LookupError("Объект недвижимости не найден")

        enabled = set(get_apartment_enabled_meters(apt))
        if meter_type not in enabled:
            label = METER_PLAIN_LABELS.get(meter_type, meter_type)
            raise ValueError(f"Счётчик «{label}» не подключён для этого объекта")

        prev_val = await _get_prev_value_in_session(s, apartment_id, meter_type)
        if prev_val is not None and old_final_value < prev_val:
            raise ValueError(
                f"Конечное показание старого счётчика ({old_final_value}) не может быть меньше "
                f"последнего зафиксированного значения ({prev_val})"
            )

        device_meter_type = SINGLE_ELECTRICITY_METER if meter_type in ELECTRICITY_METERS else meter_type
        res_dev = await s.execute(
            select(MeterDevice).where(
                MeterDevice.apartment_id == apartment_id,
                MeterDevice.meter_type == device_meter_type,
            )
        )
        device = res_dev.scalar_one_or_none()
        old_serial = device.serial_number if device else None

        init_reading = MeterReading(
            apartment_id=apartment_id,
            meter_type=meter_type,
            value=new_initial_value,
            submitted_by=actor_tg_id,
            reading_date=replacement_date,
            is_replacement_init=True,
        )
        s.add(init_reading)
        await s.flush()

        clean_new_serial = (new_serial_number or "").strip() or None
        rep = MeterReplacement(
            apartment_id=apartment_id,
            meter_type=meter_type,
            replacement_date=replacement_date,
            old_final_value=old_final_value,
            new_initial_value=new_initial_value,
            init_reading_id=init_reading.id,
            old_serial_number=old_serial,
            new_serial_number=clean_new_serial,
            replaced_by=actor_tg_id,
        )
        s.add(rep)

        if clean_new_serial is not None or new_verification_due_date is not None:
            now = utcnow_naive()
            if device is None:
                device = MeterDevice(
                    apartment_id=apartment_id,
                    meter_type=device_meter_type,
                    serial_number=clean_new_serial,
                    verification_due_date=new_verification_due_date,
                    updated_at=now,
                )
                s.add(device)
            else:
                if clean_new_serial is not None:
                    device.serial_number = clean_new_serial
                if new_verification_due_date is not None:
                    device.verification_due_date = new_verification_due_date
                device.updated_at = now

        label = METER_PLAIN_LABELS.get(meter_type, meter_type)
        s.add(AuditLog(
            user_telegram_id=actor_tg_id,
            action="replace_meter",
            details=(
                f"{format_property_short(apt)} (id={apartment_id}), {label}: "
                f"старый финал={old_final_value}, новый старт={new_initial_value}"
            ),
        ))
        await s.flush()
        await s.refresh(rep)
        return rep


async def replace_meter_batch_atomic(
    apartment_id: int,
    entries: list[dict],
    actor_tg_id: int,
    replacement_date: datetime.date,
    new_serial_number: str | None = None,
    new_verification_due_date: datetime.date | None = None,
) -> list[MeterReplacement]:
    """Atomically replace a multi-tariff electricity meter (T1/T2/T3) in a single transaction."""
    if not entries:
        raise ValueError("Укажите показания счётчика для замены")

    async with UnitOfWork(write=True) as s:
        apt = await s.get(Apartment, apartment_id)
        if not apt:
            raise LookupError("Объект недвижимости не найден")

        enabled = set(get_apartment_enabled_meters(apt))
        res_dev = await s.execute(
            select(MeterDevice).where(
                MeterDevice.apartment_id == apartment_id,
                MeterDevice.meter_type == SINGLE_ELECTRICITY_METER,
            )
        )
        device = res_dev.scalar_one_or_none()
        old_serial = device.serial_number if device else None
        clean_new_serial = (new_serial_number or "").strip() or None

        reps: list[MeterReplacement] = []
        audit_parts: list[str] = []
        for entry in entries:
            mt = entry["meter_type"]
            old_final = int(entry["old_final_value"])
            new_init = int(entry["new_initial_value"])
            if mt not in ELECTRICITY_METERS or mt not in enabled:
                label = METER_PLAIN_LABELS.get(mt, mt)
                raise ValueError(f"Тариф «{label}» не подключён для этого объекта")

            prev_val = await _get_prev_value_in_session(s, apartment_id, mt)
            if prev_val is not None and old_final < prev_val:
                label = METER_PLAIN_LABELS.get(mt, mt)
                raise ValueError(
                    f"Конечное показание старого счётчика ({label}: {old_final}) не может быть меньше "
                    f"последнего зафиксированного значения ({prev_val})"
                )

            init_reading = MeterReading(
                apartment_id=apartment_id,
                meter_type=mt,
                value=new_init,
                submitted_by=actor_tg_id,
                reading_date=replacement_date,
                is_replacement_init=True,
            )
            s.add(init_reading)
            await s.flush()

            rep = MeterReplacement(
                apartment_id=apartment_id,
                meter_type=mt,
                replacement_date=replacement_date,
                old_final_value=old_final,
                new_initial_value=new_init,
                init_reading_id=init_reading.id,
                old_serial_number=old_serial,
                new_serial_number=clean_new_serial,
                replaced_by=actor_tg_id,
            )
            s.add(rep)
            reps.append(rep)
            audit_parts.append(f"{METER_PLAIN_LABELS.get(mt, mt)}: {old_final}→{new_init}")

        if clean_new_serial is not None or new_verification_due_date is not None:
            now = utcnow_naive()
            if device is None:
                device = MeterDevice(
                    apartment_id=apartment_id,
                    meter_type=SINGLE_ELECTRICITY_METER,
                    serial_number=clean_new_serial,
                    verification_due_date=new_verification_due_date,
                    updated_at=now,
                )
                s.add(device)
            else:
                if clean_new_serial is not None:
                    device.serial_number = clean_new_serial
                if new_verification_due_date is not None:
                    device.verification_due_date = new_verification_due_date
                device.updated_at = now

        s.add(AuditLog(
            user_telegram_id=actor_tg_id,
            action="replace_meter",
            details=(
                f"{format_property_short(apt)} (id={apartment_id}), Электрический счётчик: "
                f"{', '.join(audit_parts)}"
            ),
        ))
        await s.flush()
        for r in reps:
            await s.refresh(r)
        return reps


async def upsert_meter_devices_atomic(
    apartment_id: int,
    items: list[dict],
    actor_tg_id: int,
) -> int:
    """Atomically save serial numbers, verification due dates, and notify intervals for meters."""
    now = utcnow_naive()
    async with UnitOfWork(write=True) as s:
        apt = await s.get(Apartment, apartment_id)
        if not apt:
            raise LookupError("Объект недвижимости не найден")

        enabled = set(get_apartment_enabled_meters(apt))
        updated = 0
        has_any_elec = any(em in enabled for em in ELECTRICITY_METERS)
        for item in items:
            mt = item["meter_type"]
            is_unified_elec = mt == SINGLE_ELECTRICITY_METER and has_any_elec
            if mt not in ALL_METERS or (mt not in enabled and not is_unified_elec):
                raise ValueError(f"Счётчик «{METER_PLAIN_LABELS.get(mt, mt)}» не подключён для этого объекта")

            serial = (item.get("serial_number") or "").strip() or None
            due_date = item.get("verification_due_date")
            intervals_str = serialize_notify_intervals(item.get("notify_intervals"))

            res = await s.execute(
                select(MeterDevice).where(
                    MeterDevice.apartment_id == apartment_id,
                    MeterDevice.meter_type == mt,
                )
            )
            rec = res.scalar_one_or_none()
            if rec:
                rec.serial_number = serial
                rec.verification_due_date = due_date
                rec.notify_intervals = intervals_str
                rec.updated_at = now
            else:
                s.add(MeterDevice(
                    apartment_id=apartment_id,
                    meter_type=mt,
                    serial_number=serial,
                    verification_due_date=due_date,
                    notify_intervals=intervals_str,
                    updated_at=now,
                ))
            updated += 1

        s.add(AuditLog(
            user_telegram_id=actor_tg_id,
            action="update_meter_devices",
            details=f"{format_property_short(apt)} (id={apartment_id}), обновлено счётчиков: {updated}",
        ))
        return updated


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
    irrigation_mode: str | None = None,
) -> int:
    """Validate and save property settings, days, and changed tariffs in one transaction."""
    async with UnitOfWork(write=True) as s:
        apt = await s.get(Apartment, apartment_id)
        if not apt:
            raise LookupError("Объект недвижимости не найден")

        old_irr_mode = get_apartment_irrigation_mode(apt)
        if number is not None:
            apt.number = number
        if property_type is not None:
            apt.property_type = property_type
        if enabled_meters is not None:
            apt.enabled_meters = serialize_enabled_meters(enabled_meters)
        if irrigation_mode is not None:
            apt.irrigation_mode = irrigation_mode

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

        extra = ""
        new_irr_mode = get_apartment_irrigation_mode(apt)
        if old_irr_mode != new_irr_mode:
            extra = f", режим полива: {old_irr_mode} → {new_irr_mode}"

        s.add(AuditLog(
            user_telegram_id=changed_by,
            action="update_settings",
            details=(
                f"{format_property_short(apt)} (id={apartment_id}), "
                f"тарифов обновлено: {inserted_tariffs}{extra}"
            ),
        ))
        return inserted_tariffs


async def update_initial_readings_atomic(
    apartment_id: int,
    readings: dict[str, int],
    set_by: int,
) -> None:
    """HIGH-06: atomically update initial readings with detailed old -> new audit."""
    now = utcnow_naive()
    async with UnitOfWork(write=True) as s:
        apt = await s.get(Apartment, apartment_id)
        if not apt:
            raise LookupError("Объект недвижимости не найден")

        changes: list[str] = []
        for mt, val in readings.items():
            res = await s.execute(
                select(InitialReading).where(
                    InitialReading.apartment_id == apartment_id,
                    InitialReading.meter_type == mt,
                )
            )
            rec = res.scalar_one_or_none()
            label = METER_PLAIN_LABELS.get(mt, mt)
            if rec:
                old_val = rec.value
                rec.value = val
                rec.set_by = set_by
                rec.updated_at = now
                changes.append(f"{label}: {old_val} → {val}")
            else:
                s.add(InitialReading(
                    apartment_id=apartment_id,
                    meter_type=mt,
                    value=val,
                    set_by=set_by,
                ))
                changes.append(f"{label}: — → {val}")

        s.add(AuditLog(
            user_telegram_id=set_by,
            action="set_initial_readings",
            details=f"{format_property_short(apt)} (id={apartment_id}): {', '.join(changes)}",
        ))


async def create_family_atomic(name: str, actor_tg_id: int) -> Family:
    async with UnitOfWork(write=True) as s:
        fam = Family(name=name)
        s.add(fam)
        await s.flush()
        s.add(AuditLog(
            user_telegram_id=actor_tg_id,
            action="create_family",
            details=f"Семья '{name}' (id={fam.id})",
        ))
        await s.refresh(fam)
        return fam


async def rename_family_atomic(family_id: int, new_name: str, actor_tg_id: int) -> None:
    """LOW-05 & MEDIUM-07: raise LookupError when family does not exist and log atomically."""
    async with UnitOfWork(write=True) as s:
        fam = await s.get(Family, family_id)
        if not fam:
            raise LookupError("Семья не найдена")
        old_name = fam.name
        fam.name = new_name
        s.add(AuditLog(
            user_telegram_id=actor_tg_id,
            action="rename_family",
            details=f"Семья id={family_id}: '{old_name}' → '{new_name}'",
        ))


async def add_family_owner_atomic(
    family_id: int,
    telegram_id: int,
    name: str,
    phone: str | None,
    actor_tg_id: int,
) -> User:
    """HIGH-03 & Admin-as-owner: atomically add a landlord or attach ADMIN_TELEGRAM_ID to a family."""
    async with UnitOfWork(write=True) as s:
        fam = await s.get(Family, family_id)
        if not fam:
            raise LookupError("Семья не найдена")

        if config.ADMIN_TELEGRAM_ID and telegram_id == config.ADMIN_TELEGRAM_ID:
            res_adm = await s.execute(select(User).where(User.telegram_id == telegram_id))
            admin_user = res_adm.scalar_one_or_none()
            if not admin_user:
                admin_user = User(
                    telegram_id=telegram_id,
                    name=name,
                    phone=phone,
                    role="admin",
                )
                s.add(admin_user)
                await s.flush()
            else:
                if name and (not admin_user.name or admin_user.name == "Администратор"):
                    admin_user.name = name
                if phone and not admin_user.phone:
                    admin_user.phone = phone

            res_link = await s.execute(
                select(FamilyOwner).where(
                    FamilyOwner.family_id == family_id,
                    FamilyOwner.user_id == admin_user.id,
                )
            )
            if res_link.scalar_one_or_none() is not None:
                raise ConflictError("Администратор уже добавлен как владелец в эту семью")

            s.add(FamilyOwner(family_id=family_id, user_id=admin_user.id))
            s.add(AuditLog(
                user_telegram_id=actor_tg_id,
                action="add_family_owner",
                details=f"Администратор {admin_user.name or name} (ID: {telegram_id}) добавлен как владелец в семью id={family_id}",
            ))
            await s.flush()
            await s.refresh(admin_user)
            return admin_user

        res_ex = await s.execute(select(User).where(User.telegram_id == telegram_id))
        existing = res_ex.scalar_one_or_none()
        if existing:
            raise ConflictError(
                f"Пользователь ID {telegram_id} уже зарегистрирован ({existing.role})"
            )

        user = User(
            telegram_id=telegram_id,
            name=name,
            phone=phone,
            role="landlord",
            family_id=family_id,
        )
        s.add(user)
        try:
            await s.flush()
        except IntegrityError as exc:
            raise ConflictError(f"Пользователь ID {telegram_id} уже зарегистрирован") from exc

        s.add(FamilyOwner(family_id=family_id, user_id=user.id))
        s.add(AuditLog(
            user_telegram_id=actor_tg_id,
            action="add_family_owner",
            details=f"Владелец {name} (ID: {telegram_id}) добавлен в семью id={family_id}",
        ))
        await s.refresh(user)
        return user


async def update_family_owner_atomic(
    family_id: int,
    owner_tg_id: int,
    name: str,
    phone: str | None,
    update_phone: bool,
    actor_tg_id: int,
) -> User:
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
            raise LookupError("Член семьи не найден")

        target.name = name
        if update_phone:
            target.phone = phone

        s.add(AuditLog(
            user_telegram_id=actor_tg_id,
            action="update_family_owner",
            details=f"Владелец ID={owner_tg_id} (семья id={family_id}) обновлён: имя='{name}'",
        ))
        await s.flush()
        await s.refresh(target)
        return target


async def delete_family_owner_atomic(
    family_id: int,
    owner_tg_id: int,
    actor_tg_id: int,
) -> None:
    """Verify family membership; detach admin from FamilyOwner without deleting admin User."""
    async with UnitOfWork(write=True) as s:
        if config.ADMIN_TELEGRAM_ID and owner_tg_id == config.ADMIN_TELEGRAM_ID:
            res_adm = await s.execute(select(User).where(User.telegram_id == owner_tg_id))
            adm = res_adm.scalar_one_or_none()
            if not adm:
                raise PermissionError("Нельзя удалить администратора")
            res_link = await s.execute(
                select(FamilyOwner).where(
                    FamilyOwner.family_id == family_id,
                    FamilyOwner.user_id == adm.id,
                )
            )
            link = res_link.scalar_one_or_none()
            if not link:
                raise PermissionError("Нельзя удалить администратора")
            await s.delete(link)
            s.add(AuditLog(
                user_telegram_id=actor_tg_id,
                action="delete_family_owner",
                details=f"Администратор ID={owner_tg_id} откреплён от владельцев семьи id={family_id}",
            ))
            return

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
    """Delete family and cascade-revoke its orphaned users."""
    async with UnitOfWork(write=True) as s:
        fam = await s.get(Family, family_id)
        if not fam:
            raise LookupError("Семья не найдена")

        res_users = await s.execute(select(User).where(User.family_id == family_id))
        fam_users = list(res_users.scalars().all())

        for u in fam_users:
            if config.ADMIN_TELEGRAM_ID and u.telegram_id == config.ADMIN_TELEGRAM_ID:
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


async def add_tenant_atomic(
    apartment_id: int,
    telegram_id: int,
    name: str,
    phone: str | None,
    actor_tg_id: int,
    force_no_contract: bool = False,
) -> tuple[bool, Apartment, str]:
    """HIGH-03: serialize tenant lookup/creation/linking/audit in one transaction."""
    if config.ADMIN_TELEGRAM_ID and telegram_id == config.ADMIN_TELEGRAM_ID:
        raise ValueError("Нельзя добавить администратора в качестве нанимателя")

    async with UnitOfWork(write=True) as s:
        apt = await s.get(Apartment, apartment_id)
        if not apt:
            raise LookupError("Объект недвижимости не найден")

        res_ex = await s.execute(select(User).where(User.telegram_id == telegram_id))
        existing = res_ex.scalar_one_or_none()
        if existing:
            if existing.role != "tenant":
                role_map = {"admin": "администратор", "landlord": "владелец", "revoked": "отозван"}
                raise ConflictError(
                    f"Пользователь ID {telegram_id} уже добавлен ({role_map.get(existing.role, existing.role)})"
                )
            if existing.is_blocked:
                raise ConflictError(f"Пользователь ID {telegram_id} заблокирован")
            if existing.family_id is not None and existing.family_id != apt.family_id:
                raise ConflictError("Этот наниматель уже привязан к другой семье")

            res_link = await s.execute(
                select(ApartmentUser.id).where(
                    ApartmentUser.apartment_id == apartment_id,
                    ApartmentUser.user_id == existing.id,
                ).limit(1)
            )
            if res_link.scalar_one_or_none() is not None:
                raise ValueError("Этот наниматель уже привязан к данному объекту")

        has_contract = all([apt.agreement_no, apt.agreement_date_start, apt.agreement_date_finish])
        if not has_contract and not force_no_contract:
            return True, apt, name

        if existing:
            target_user = existing
            if name and not target_user.name:
                target_user.name = name
            if phone and not target_user.phone:
                target_user.phone = phone
            if target_user.family_id is None:
                target_user.family_id = apt.family_id
        else:
            target_user = User(
                telegram_id=telegram_id,
                name=name,
                phone=phone,
                role="tenant",
                family_id=apt.family_id,
            )
            s.add(target_user)
            try:
                await s.flush()
            except IntegrityError as exc:
                raise ConflictError("Этот пользователь уже добавлен в другом запросе") from exc

        s.add(ApartmentUser(apartment_id=apartment_id, user_id=target_user.id, role="tenant"))
        apt.has_tenant = True
        prop_label = format_property_short(apt)
        display_name = target_user.name or name
        s.add(AuditLog(
            user_telegram_id=actor_tg_id,
            action="add_tenant",
            details=f"Наниматель {display_name} (ID: {telegram_id}) добавлен в {prop_label}",
        ))
        return False, apt, display_name


async def update_tenant_profile_atomic(
    tenant_tg_id: int,
    name: str,
    phone: str | None,
    update_phone: bool,
    actor_tg_id: int,
    actor_family_id: int | None,
    is_actor_admin: bool,
) -> User:
    async with UnitOfWork(write=True) as s:
        res = await s.execute(
            select(User).where(User.telegram_id == tenant_tg_id, User.role == "tenant")
        )
        target = res.scalar_one_or_none()
        if not target:
            raise LookupError("Наниматель не найден")
        if not is_actor_admin and target.family_id != actor_family_id:
            raise PermissionError("Нет доступа к этому нанимателю")

        target.name = name
        if update_phone:
            target.phone = phone

        s.add(AuditLog(
            user_telegram_id=actor_tg_id,
            action="update_tenant",
            details=f"Наниматель ID={tenant_tg_id} обновлён: имя='{name}'",
        ))
        await s.flush()
        await s.refresh(target)
        return target


async def remove_tenant_from_apartment_atomic(
    apartment_id: int,
    tenant_tg_id: int,
    actor_tg_id: int,
) -> tuple[str, bool]:
    """Verify tenant link to apt_id, unlink atomically. Returns (property_label, fully_removed)."""
    if config.ADMIN_TELEGRAM_ID and tenant_tg_id == config.ADMIN_TELEGRAM_ID:
        raise PermissionError("Нельзя удалить администратора")

    async with UnitOfWork(write=True) as s:
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


async def update_contract_atomic(
    apartment_id: int,
    updates: dict,
    actor_tg_id: int,
) -> Apartment:
    async with UnitOfWork(write=True) as s:
        apt = await s.get(Apartment, apartment_id)
        if not apt:
            raise LookupError("Объект недвижимости не найден")
        for k, v in updates.items():
            if k in ALLOWED_APARTMENT_FIELDS:
                setattr(apt, k, v)
        s.add(AuditLog(
            user_telegram_id=actor_tg_id,
            action="update_contract",
            details=f"Объект id={apartment_id}",
        ))
        await s.flush()
        await s.refresh(apt)
        return apt


async def delete_contract_atomic(
    apartment_id: int,
    actor_tg_id: int,
    unlink_tenants: bool = False,
) -> tuple[str, list[tuple[int, bool]]]:
    """HIGH-04 & MEDIUM-07: clear lease contract and optionally unlink all tenants atomically."""
    async with UnitOfWork(write=True) as s:
        apt = await s.get(Apartment, apartment_id)
        if not apt:
            raise LookupError("Объект недвижимости не найден")

        apt.agreement_no = None
        apt.agreement_date_start = None
        apt.agreement_date_finish = None
        prop_label = format_property_short(apt)
        affected_tenants: list[tuple[int, bool]] = []

        if unlink_tenants:
            res_links = await s.execute(
                select(ApartmentUser, User)
                .join(User, User.id == ApartmentUser.user_id)
                .where(
                    ApartmentUser.apartment_id == apartment_id,
                    ApartmentUser.role == "tenant",
                )
            )
            pairs = list(res_links.all())
            for link, tenant in pairs:
                await s.delete(link)
            await s.flush()

            for _, tenant in pairs:
                if config.ADMIN_TELEGRAM_ID and tenant.telegram_id == config.ADMIN_TELEGRAM_ID:
                    continue
                res_other = await s.execute(
                    select(ApartmentUser.id).where(ApartmentUser.user_id == tenant.id).limit(1)
                )
                fully_removed = res_other.scalar_one_or_none() is None
                if fully_removed:
                    await s.delete(tenant)
                affected_tenants.append((tenant.telegram_id, fully_removed))

            apt.has_tenant = False

        s.add(AuditLog(
            user_telegram_id=actor_tg_id,
            action="delete_contract",
            details=(
                f"Договор объекта {prop_label} (id={apartment_id}) удалён"
                + (f", откреплено нанимателей: {len(affected_tenants)}" if unlink_tenants else "")
            ),
        ))
        return prop_label, affected_tenants


async def create_apartment_atomic(
    number: str,
    family_id: int,
    property_type: str,
    enabled_meters: Iterable[str] | None,
    irrigation_mode: str,
    actor_tg_id: int,
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
        s.add(AuditLog(
            user_telegram_id=actor_tg_id,
            action="create_apartment",
            details=(
                f"Создан объект {format_property_short(apt)} "
                f"(id={apt.id}, type={apt.property_type}, family_id={family_id})"
            ),
        ))
        await s.refresh(apt)
        return apt


async def correct_reading_atomic(
    reading_id: int,
    new_value: int,
    actor_tg_id: int,
) -> tuple[int, int, int]:
    """MEDIUM-07: update reading value and write audit log atomically. Returns (apt_id, old, new)."""
    async with UnitOfWork(write=True) as s:
        rec = await s.get(MeterReading, reading_id)
        if not rec:
            raise LookupError("Показание не найдено")
        old_val = rec.value
        apt_id = rec.apartment_id
        rec.value = new_value
        s.add(AuditLog(
            user_telegram_id=actor_tg_id,
            action="correct_reading",
            details=f"Показание #{reading_id}: {old_val} → {new_value}",
        ))
        return apt_id, old_val, new_value


async def update_own_profile_atomic(telegram_id: int, name: str) -> User:
    async with UnitOfWork(write=True) as s:
        res = await s.execute(select(User).where(User.telegram_id == telegram_id))
        user = res.scalar_one_or_none()
        if not user:
            if config.ADMIN_TELEGRAM_ID and telegram_id == config.ADMIN_TELEGRAM_ID:
                user = User(telegram_id=telegram_id, name=name, role="admin")
                s.add(user)
                await s.flush()
            else:
                raise LookupError("Профиль пользователя не найден")
        else:
            user.name = name
        s.add(AuditLog(
            user_telegram_id=telegram_id,
            action="update_own_name",
            details=f"Пользователь ID={telegram_id} изменил имя на '{name}'",
        ))
        await s.flush()
        await s.refresh(user)
        return user


async def delete_apartment_atomic(
    apartment_id: int,
    actor_tg_id: int,
) -> tuple[str, list[tuple[int, bool]]]:
    """Delete a property atomically with all its readings/tariffs/links and clean up orphaned tenants."""
    async with UnitOfWork(write=True) as s:
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
            if config.ADMIN_TELEGRAM_ID and tenant.telegram_id == config.ADMIN_TELEGRAM_ID:
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

    async with UnitOfWork(write=True) as s:
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

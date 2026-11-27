"""Multi-entity atomic operations with serialized writes (P0 isolation, idempotency, atomicity)."""

from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

from sqlalchemy import and_, asc, delete, desc, func, or_, select, update
from sqlalchemy.exc import IntegrityError

import config
from database.crud import ALLOWED_APARTMENT_FIELDS, UnitOfWork, bump_business_revision_in_session
from database.models import (
    ALL_METERS,
    ELECTRICITY_METERS,
    IRRIGATION_METERS,
    IRRIGATION_MODE_AFTER_COLD,
    MULTI_ELECTRICITY_METERS,
    RENT_REMINDER_AFTER_DAYS,
    RENT_REMINDER_MODES,
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
from services.access import (
    assert_active_actor_in_session,
    assert_can_access_apartment_in_session,
    assert_can_manage_family_in_session,
    is_admin,
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


def validate_apartment_configuration(
    property_type: str,
    enabled_meters: list[str],
    irrigation_mode: str,
) -> list[str]:
    """Validate property type, meter combination, and irrigation mode; returns canonical meters list."""
    valid = [m for m in ALL_METERS if m in set(enabled_meters)]
    if not valid:
        raise ValueError("Выберите хотя бы один счётчик для объекта")
    if SINGLE_ELECTRICITY_METER in valid and any(m in valid for m in MULTI_ELECTRICITY_METERS):
        raise ValueError("Нельзя одновременно выбрать «Один тариф (Т)» и тарифы Т1, Т2, Т3")
    if property_type != "house" and any(m in valid for m in IRRIGATION_METERS):
        raise ValueError("Счётчик «Полив» доступен только для типа недвижимости «Дом»")
    if (
        any(m in valid for m in IRRIGATION_METERS)
        and irrigation_mode == IRRIGATION_MODE_AFTER_COLD
        and "cold_water" not in valid
    ):
        raise ValueError("Режим «Полив после ХВС» требует включённого счётчика ХВС")
    return valid


def _compute_submission_hash(
    meters: dict[str, int],
    reading_date: datetime.date | None,
    action: str,
) -> str:
    """P2-09: hash immutable submission payload without server date so cross-midnight retries deduplicate."""
    payload = {
        "action": action,
        "meters": sorted((k, int(v)) for k, v in meters.items()),
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _make_replacement_ledger_key(replacement_id: str) -> str:
    clean = replacement_id.strip()
    raw = f"rep:{clean}"
    if len(raw) <= 64:
        return raw
    return f"rep:{hashlib.sha256(clean.encode('utf-8')).hexdigest()[:56]}"


def _compute_replacement_hash(
    meter_type: str,
    entries: list[tuple[str, int, int]],
    new_serial: str | None,
    new_due_date: datetime.date | None,
) -> str:
    payload = {
        "action": "replace_meter",
        "meter_type": meter_type,
        "entries": sorted((mt, int(of), int(ni)) for mt, of, ni in entries),
        "new_serial": new_serial or "",
        "new_due_date": new_due_date.isoformat() if new_due_date else "",
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
    """HIGH-07, MEDIUM-02, P1-03, P2-09: verify rights, validate rules, and save atomically."""
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
        apt, actor_role = await assert_can_access_apartment_in_session(
            s, submitted_by, apartment_id, allow_tenant=True
        )
        effective_allow_decrease = bool(allow_decrease and actor_role in ("admin", "landlord"))

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
        if decreased and not effective_allow_decrease:
            return SubmissionOutcome(
                status="rejected_decrease",
                prev_meters=prev_meters,
                decreased=decreased,
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
        suffix = " (корректировка)" if (decreased and effective_allow_decrease) else ""
        s.add(AuditLog(
            user_telegram_id=submitted_by,
            family_id=apt.family_id,
            action=action,
            details=f"{prop_label} (id={apartment_id}): {parts}{suffix}",
        ))
        await bump_business_revision_in_session(s)
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
        apt = await s.get(Apartment, apartment_id)
        if not apt:
            raise LookupError("Объект недвижимости не найден")

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
            family_id=apt.family_id,
            action=action,
            details=audit_details,
        ))
        await bump_business_revision_in_session(s)
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
    replacement_id: str | None = None,
) -> tuple[MeterReplacement, bool]:
    """HIGH-02, P1-06, P2-01: record physical meter replacement atomically. Returns (replacement, deduplicated)."""
    if meter_type not in ALL_METERS:
        raise ValueError("Неизвестный тип счётчика")
    if meter_type in MULTI_ELECTRICITY_METERS:
        raise ValueError(
            "Для многотарифного электросчётчика необходимо заменить все активные тарифы (Т1/Т2/Т3) одновременно"
        )

    clean_new_serial = (new_serial_number or "").strip() or None
    clean_rep_id = (replacement_id or "").strip() or None
    ledger_key = _make_replacement_ledger_key(clean_rep_id) if clean_rep_id else None
    payload_hash = _compute_replacement_hash(
        meter_type, [(meter_type, old_final_value, new_initial_value)], clean_new_serial, new_verification_due_date
    )

    async with UnitOfWork(write=True) as s:
        apt, _ = await assert_can_access_apartment_in_session(
            s, actor_tg_id, apartment_id, allow_tenant=False
        )

        if ledger_key:
            res_sub = await s.execute(
                select(MeterSubmission).where(MeterSubmission.submission_id == ledger_key)
            )
            existing_sub = res_sub.scalar_one_or_none()
            if existing_sub is not None:
                if (
                    existing_sub.apartment_id != apartment_id
                    or existing_sub.submitted_by != actor_tg_id
                    or existing_sub.payload_hash != payload_hash
                ):
                    raise ConflictError("Идентификатор замены уже использован с другими данными")
                res_last = await s.execute(
                    select(MeterReplacement)
                    .where(
                        MeterReplacement.apartment_id == apartment_id,
                        MeterReplacement.meter_type == meter_type,
                    )
                    .order_by(desc(MeterReplacement.id))
                    .limit(1)
                )
                existing_rep = res_last.scalar_one_or_none()
                if existing_rep is not None:
                    return existing_rep, True

        enabled = set(get_apartment_enabled_meters(apt))
        if meter_type == SINGLE_ELECTRICITY_METER and any(m in enabled for m in MULTI_ELECTRICITY_METERS):
            raise ValueError(
                "Для многотарифного электросчётчика передайте показания по всем активным тарифам (Т1/Т2/Т3)"
            )
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

        if ledger_key:
            s.add(MeterSubmission(
                submission_id=ledger_key,
                apartment_id=apartment_id,
                submitted_by=actor_tg_id,
                payload_hash=payload_hash,
            ))

        # P2-01: always replace physical device metadata (clear stale serial/verification date when omitted)
        now = utcnow_naive()
        if device is None:
            if clean_new_serial is not None or new_verification_due_date is not None:
                s.add(MeterDevice(
                    apartment_id=apartment_id,
                    meter_type=device_meter_type,
                    serial_number=clean_new_serial,
                    verification_due_date=new_verification_due_date,
                    updated_at=now,
                ))
        else:
            device.serial_number = clean_new_serial
            device.verification_due_date = new_verification_due_date
            device.updated_at = now

        label = METER_PLAIN_LABELS.get(meter_type, meter_type)
        s.add(AuditLog(
            user_telegram_id=actor_tg_id,
            family_id=apt.family_id,
            action="replace_meter",
            details=(
                f"{format_property_short(apt)} (id={apartment_id}), {label}: "
                f"старый финал={old_final_value}, новый старт={new_initial_value}"
            ),
        ))
        await bump_business_revision_in_session(s)
        await s.flush()
        await s.refresh(rep)
        return rep, False


async def replace_meter_batch_atomic(
    apartment_id: int,
    entries: list[dict],
    actor_tg_id: int,
    replacement_date: datetime.date,
    meter_type: str = SINGLE_ELECTRICITY_METER,
    new_serial_number: str | None = None,
    new_verification_due_date: datetime.date | None = None,
    replacement_id: str | None = None,
) -> tuple[list[MeterReplacement], bool]:
    """P1-06 & P2-01: atomically replace a multi-tariff electricity meter (exact active tariff set)."""
    if meter_type != SINGLE_ELECTRICITY_METER:
        raise ValueError("Групповая замена тарифов поддерживается только для электрического счётчика ('t')")
    if not entries:
        raise ValueError("Укажите показания счётчика для замены")

    entry_mts = [e["meter_type"] for e in entries]
    if len(entry_mts) != len(set(entry_mts)):
        raise ValueError("В списке тарифов для замены обнаружены дубликаты")

    clean_new_serial = (new_serial_number or "").strip() or None
    clean_rep_id = (replacement_id or "").strip() or None
    ledger_key = _make_replacement_ledger_key(clean_rep_id) if clean_rep_id else None
    payload_hash = _compute_replacement_hash(
        meter_type,
        [(e["meter_type"], int(e["old_final_value"]), int(e["new_initial_value"])) for e in entries],
        clean_new_serial,
        new_verification_due_date,
    )

    async with UnitOfWork(write=True) as s:
        apt, _ = await assert_can_access_apartment_in_session(
            s, actor_tg_id, apartment_id, allow_tenant=False
        )

        if ledger_key:
            res_sub = await s.execute(
                select(MeterSubmission).where(MeterSubmission.submission_id == ledger_key)
            )
            existing_sub = res_sub.scalar_one_or_none()
            if existing_sub is not None:
                if (
                    existing_sub.apartment_id != apartment_id
                    or existing_sub.submitted_by != actor_tg_id
                    or existing_sub.payload_hash != payload_hash
                ):
                    raise ConflictError("Идентификатор замены уже использован с другими данными")
                res_last = await s.execute(
                    select(MeterReplacement)
                    .where(
                        MeterReplacement.apartment_id == apartment_id,
                        MeterReplacement.meter_type.in_(entry_mts),
                    )
                    .order_by(desc(MeterReplacement.id))
                    .limit(len(entry_mts))
                )
                return list(res_last.scalars().all()), True

        enabled = set(get_apartment_enabled_meters(apt))
        active_multi = {m for m in MULTI_ELECTRICITY_METERS if m in enabled}
        if not active_multi:
            raise ValueError("Для этого объекта не подключён многотарифный электросчётчик")
        if set(entry_mts) != active_multi:
            missing = [METER_PLAIN_LABELS.get(m, m) for m in MULTI_ELECTRICITY_METERS if m in (active_multi - set(entry_mts))]
            extra = [METER_PLAIN_LABELS.get(m, m) for m in entry_mts if m not in active_multi]
            parts = []
            if missing:
                parts.append(f"отсутствуют тарифы: {', '.join(missing)}")
            if extra:
                parts.append(f"неподключённые тарифы: {', '.join(extra)}")
            raise ValueError(f"Укажите показания для всех активных тарифов электросчётчика ({'; '.join(parts)})")

        res_dev = await s.execute(
            select(MeterDevice).where(
                MeterDevice.apartment_id == apartment_id,
                MeterDevice.meter_type == SINGLE_ELECTRICITY_METER,
            )
        )
        device = res_dev.scalar_one_or_none()
        old_serial = device.serial_number if device else None

        reps: list[MeterReplacement] = []
        audit_parts: list[str] = []
        for entry in entries:
            mt = entry["meter_type"]
            old_final = int(entry["old_final_value"])
            new_init = int(entry["new_initial_value"])

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

        if ledger_key:
            s.add(MeterSubmission(
                submission_id=ledger_key,
                apartment_id=apartment_id,
                submitted_by=actor_tg_id,
                payload_hash=payload_hash,
            ))

        # P2-01: always replace physical electricity device metadata & clear stale legacy T1/T2/T3 metadata
        now = utcnow_naive()
        if device is None:
            if clean_new_serial is not None or new_verification_due_date is not None:
                s.add(MeterDevice(
                    apartment_id=apartment_id,
                    meter_type=SINGLE_ELECTRICITY_METER,
                    serial_number=clean_new_serial,
                    verification_due_date=new_verification_due_date,
                    updated_at=now,
                ))
        else:
            device.serial_number = clean_new_serial
            device.verification_due_date = new_verification_due_date
            device.updated_at = now

        res_legacy_elec = await s.execute(
            select(MeterDevice).where(
                MeterDevice.apartment_id == apartment_id,
                MeterDevice.meter_type.in_(list(MULTI_ELECTRICITY_METERS)),
            )
        )
        for leg_dev in res_legacy_elec.scalars().all():
            leg_dev.serial_number = None
            leg_dev.verification_due_date = None
            leg_dev.updated_at = now

        s.add(AuditLog(
            user_telegram_id=actor_tg_id,
            family_id=apt.family_id,
            action="replace_meter",
            details=(
                f"{format_property_short(apt)} (id={apartment_id}), Электрический счётчик: "
                f"{', '.join(audit_parts)}"
            ),
        ))
        await bump_business_revision_in_session(s)
        await s.flush()
        for r in reps:
            await s.refresh(r)
        return reps, False


async def upsert_meter_devices_atomic(
    apartment_id: int,
    items: list[dict],
    actor_tg_id: int,
) -> int:
    """P2-02: atomically save canonical meter devices (T1/T2/T3 unified into 't')."""
    now = utcnow_naive()
    async with UnitOfWork(write=True) as s:
        apt, _ = await assert_can_access_apartment_in_session(
            s, actor_tg_id, apartment_id, allow_tenant=False
        )

        enabled = set(get_apartment_enabled_meters(apt))
        has_any_elec = any(em in enabled for em in ELECTRICITY_METERS)
        seen_canonical: set[str] = set()
        updated = 0

        for item in items:
            raw_mt = item["meter_type"]
            if raw_mt not in ALL_METERS:
                raise ValueError(f"Неизвестный тип счётчика «{raw_mt}»")
            mt = SINGLE_ELECTRICITY_METER if raw_mt in ELECTRICITY_METERS else raw_mt
            if mt == SINGLE_ELECTRICITY_METER:
                if not has_any_elec:
                    raise ValueError("Электрический счётчик не подключён для этого объекта")
            elif mt not in enabled:
                raise ValueError(f"Счётчик «{METER_PLAIN_LABELS.get(mt, mt)}» не подключён для этого объекта")

            if mt in seen_canonical:
                raise ValueError("В запросе обнаружены дублирующиеся записи для одного физического счётчика")
            seen_canonical.add(mt)

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
            family_id=apt.family_id,
            action="update_meter_devices",
            details=f"{format_property_short(apt)} (id={apartment_id}), обновлено счётчиков: {updated}",
        ))
        await bump_business_revision_in_session(s)
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
    fields_set: set[str] | None = None,
) -> int:
    """P1-08: merge partial fields and validate effective configuration inside the write transaction."""
    async with UnitOfWork(write=True) as s:
        apt, _ = await assert_can_access_apartment_in_session(
            s, changed_by, apartment_id, allow_tenant=False
        )

        old_irr_mode = get_apartment_irrigation_mode(apt)
        use_partial = fields_set is not None

        eff_number = (
            number
            if (use_partial and "number" in fields_set) or (not use_partial and number is not None)
            else apt.number
        )
        eff_ptype = (
            (property_type or apt.property_type or "apartment")
            if (use_partial and "property_type" in fields_set) or (not use_partial and property_type is not None)
            else (apt.property_type or "apartment")
        )
        eff_irr_mode = (
            (irrigation_mode or old_irr_mode)
            if (use_partial and "irrigation_mode" in fields_set) or (not use_partial and irrigation_mode is not None)
            else old_irr_mode
        )

        meters_provided = (use_partial and "enabled_meters" in fields_set and enabled_meters is not None) or (
            not use_partial and enabled_meters is not None
        )
        candidate_meters = list(enabled_meters) if meters_provided else get_apartment_enabled_meters(apt)
        validated_meters = validate_apartment_configuration(eff_ptype, candidate_meters, eff_irr_mode)

        if eff_number is not None:
            apt.number = eff_number
        apt.property_type = eff_ptype
        if meters_provided:
            apt.enabled_meters = serialize_enabled_meters(validated_meters)
        apt.irrigation_mode = eff_irr_mode

        if not use_partial or "water_submission_day" in fields_set:
            apt.water_submission_day = water_submission_day
        if not use_partial or "electricity_submission_day" in fields_set:
            apt.electricity_submission_day = electricity_submission_day
        if not use_partial or "gas_submission_day" in fields_set:
            apt.gas_submission_day = gas_submission_day

        inserted_tariffs = 0
        for mt, rate in tariffs.items():
            if mt in IRRIGATION_METERS and eff_irr_mode == IRRIGATION_MODE_AFTER_COLD:
                continue
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
            family_id=apt.family_id,
            action="update_settings",
            details=(
                f"{format_property_short(apt)} (id={apartment_id}), "
                f"тарифов обновлено: {inserted_tariffs}{extra}"
            ),
        ))
        await bump_business_revision_in_session(s)
        return inserted_tariffs


async def update_initial_readings_atomic(
    apartment_id: int,
    readings: dict[str, int],
    set_by: int,
) -> None:
    """HIGH-06 & P1-05: atomically update initial readings for enabled meters with detailed audit."""
    now = utcnow_naive()
    async with UnitOfWork(write=True) as s:
        apt, _ = await assert_can_access_apartment_in_session(
            s, set_by, apartment_id, allow_tenant=False
        )
        enabled = set(get_apartment_enabled_meters(apt))
        for mt in readings:
            if mt not in enabled:
                label = METER_PLAIN_LABELS.get(mt, mt)
                raise ValueError(f"Счётчик «{label}» не подключён для этого объекта")

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
            family_id=apt.family_id,
            action="set_initial_readings",
            details=f"{format_property_short(apt)} (id={apartment_id}): {', '.join(changes)}",
        ))
        await bump_business_revision_in_session(s)


async def create_family_atomic(name: str, actor_tg_id: int) -> Family:
    async with UnitOfWork(write=True) as s:
        await assert_active_actor_in_session(s, actor_tg_id, ("admin",))
        fam = Family(name=name, admin_name=name)
        s.add(fam)
        await s.flush()
        s.add(AuditLog(
            user_telegram_id=actor_tg_id,
            family_id=fam.id,
            action="create_family",
            details=f"Семья '{name}' (id={fam.id})",
        ))
        await bump_business_revision_in_session(s)
        await s.refresh(fam)
        return fam


async def create_family_with_owner_atomic(
    family_name: str,
    owner_telegram_id: int,
    owner_name: str,
    actor_tg_id: int,
) -> tuple[Family, User]:
    """Create a new isolated family with one initial landlord owner; reject already-registered Telegram IDs."""
    if config.ADMIN_TELEGRAM_ID and owner_telegram_id == config.ADMIN_TELEGRAM_ID:
        raise ConflictError("Такой пользователь уже зарегистрирован")

    async with UnitOfWork(write=True) as s:
        await assert_active_actor_in_session(s, actor_tg_id, ("admin", "landlord"))

        res_ex = await s.execute(select(User).where(User.telegram_id == owner_telegram_id))
        if res_ex.scalar_one_or_none() is not None:
            raise ConflictError("Такой пользователь уже зарегистрирован")

        fam = Family(name=family_name, admin_name=family_name)
        s.add(fam)
        await s.flush()

        new_owner = User(
            telegram_id=owner_telegram_id,
            name=owner_name,
            admin_name=owner_name,
            role="landlord",
            family_id=fam.id,
        )
        s.add(new_owner)
        try:
            await s.flush()
        except IntegrityError as exc:
            raise ConflictError("Такой пользователь уже зарегистрирован") from exc

        s.add(FamilyOwner(family_id=fam.id, user_id=new_owner.id))
        # Admin-scoped audit (family_id=None) so neither creator's family nor new family leaks cross-family metadata
        s.add(AuditLog(
            user_telegram_id=actor_tg_id,
            family_id=None,
            action="create_family_with_owner",
            details=(
                f"Создана семья '{family_name}' (id={fam.id}), "
                f"новый владелец {owner_name} (ID: {owner_telegram_id})"
            ),
        ))
        await bump_business_revision_in_session(s)
        await s.flush()
        await s.refresh(fam)
        await s.refresh(new_owner)
        return fam, new_owner


async def rename_family_atomic(
    family_id: int,
    new_name: str,
    actor_tg_id: int,
    is_actor_admin: bool = False,
) -> None:
    """Rename family preserving admin_name vs landlord's own family name."""
    async with UnitOfWork(write=True) as s:
        fam, actor_role = await assert_can_manage_family_in_session(s, actor_tg_id, family_id)
        effective_admin = is_actor_admin and actor_role == "admin"
        if effective_admin:
            old_name = fam.admin_name or fam.name
            if fam.admin_name is None or fam.name == fam.admin_name:
                fam.name = new_name
            fam.admin_name = new_name
        else:
            old_name = fam.name
            if fam.admin_name is None:
                fam.admin_name = fam.name
            fam.name = new_name
        s.add(AuditLog(
            user_telegram_id=actor_tg_id,
            family_id=family_id,
            action="rename_family",
            details=f"Семья id={family_id}: '{old_name}' → '{new_name}'",
        ))
        await bump_business_revision_in_session(s)


async def add_family_owner_atomic(
    family_id: int,
    telegram_id: int,
    name: str,
    phone: str | None,
    actor_tg_id: int,
) -> User:
    """HIGH-03 & Admin-as-owner: atomically add a landlord or attach ADMIN_TELEGRAM_ID to a family."""
    async with UnitOfWork(write=True) as s:
        await assert_can_manage_family_in_session(s, actor_tg_id, family_id)

        if config.ADMIN_TELEGRAM_ID and telegram_id == config.ADMIN_TELEGRAM_ID:
            res_adm = await s.execute(select(User).where(User.telegram_id == telegram_id))
            admin_user = res_adm.scalar_one_or_none()
            if not admin_user:
                admin_user = User(
                    telegram_id=telegram_id,
                    name=name,
                    admin_name=name,
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
                family_id=family_id,
                action="add_family_owner",
                details=f"Администратор {admin_user.name or name} (ID: {telegram_id}) добавлен как владелец в семью id={family_id}",
            ))
            await bump_business_revision_in_session(s)
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
            admin_name=name,
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
            family_id=family_id,
            action="add_family_owner",
            details=f"Владелец {name} (ID: {telegram_id}) добавлен в семью id={family_id}",
        ))
        await bump_business_revision_in_session(s)
        await s.refresh(user)
        return user


async def update_family_owner_atomic(
    family_id: int,
    owner_tg_id: int,
    name: str,
    phone: str | None,
    update_phone: bool,
    actor_tg_id: int,
    is_actor_admin: bool = False,
) -> User:
    async with UnitOfWork(write=True) as s:
        _, actor_role = await assert_can_manage_family_in_session(s, actor_tg_id, family_id)
        effective_admin = is_actor_admin and actor_role == "admin"

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

        if config.ADMIN_TELEGRAM_ID and target.telegram_id == config.ADMIN_TELEGRAM_ID and not effective_admin:
            raise PermissionError("Нельзя редактировать данные администратора")

        if effective_admin:
            if target.telegram_id == actor_tg_id:
                target.name = name
                target.admin_name = name
            else:
                if target.admin_name is None or target.name == target.admin_name:
                    target.name = name
                target.admin_name = name
        else:
            if target.admin_name is None and target.name:
                target.admin_name = target.name
            target.name = name

        if update_phone:
            target.phone = phone

        s.add(AuditLog(
            user_telegram_id=actor_tg_id,
            family_id=family_id,
            action="update_family_owner",
            details=f"Владелец ID={owner_tg_id} (семья id={family_id}) обновлён: имя='{name}'",
        ))
        await bump_business_revision_in_session(s)
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
        await assert_can_manage_family_in_session(s, actor_tg_id, family_id)

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
                family_id=family_id,
                action="delete_family_owner",
                details=f"Администратор ID={owner_tg_id} откреплён от владельцев семьи id={family_id}",
            ))
            await bump_business_revision_in_session(s)
            return

        res = await s.execute(select(User).where(User.telegram_id == owner_tg_id))
        target = res.scalar_one_or_none()
        if not target or target.family_id != family_id or target.role != "landlord":
            raise LookupError("Владелец не найден в этой семье")

        await s.delete(target)
        s.add(AuditLog(
            user_telegram_id=actor_tg_id,
            family_id=family_id,
            action="delete_family_owner",
            details=f"Владелец ID={owner_tg_id} удалён из семьи id={family_id}",
        ))
        await bump_business_revision_in_session(s)


async def delete_family_atomic(family_id: int, actor_tg_id: int) -> None:
    """Delete family, revoke orphaned users, and unlink family audit scope (prevents ROWID reuse leak)."""
    async with UnitOfWork(write=True) as s:
        await assert_active_actor_in_session(s, actor_tg_id, ("admin",))
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

        # Detach audit logs from deleted family_id so future SQLite ROWID reuse can never expose them
        await s.execute(
            update(AuditLog).where(AuditLog.family_id == family_id).values(family_id=None)
        )

        await s.delete(fam)
        s.add(AuditLog(
            user_telegram_id=actor_tg_id,
            family_id=None,
            action="delete_family",
            details=f"Семья id={family_id} удалена",
        ))
        await bump_business_revision_in_session(s)


async def add_tenant_atomic(
    apartment_id: int,
    telegram_id: int,
    name: str,
    phone: str | None,
    actor_tg_id: int,
    force_no_contract: bool = False,
) -> tuple[bool, Apartment, str]:
    """HIGH-03 & P1-03: serialize permission check, tenant creation, linking, and audit in one transaction."""
    if config.ADMIN_TELEGRAM_ID and telegram_id == config.ADMIN_TELEGRAM_ID:
        raise ValueError("Нельзя добавить администратора в качестве нанимателя")

    async with UnitOfWork(write=True) as s:
        apt, _ = await assert_can_access_apartment_in_session(
            s, actor_tg_id, apartment_id, allow_tenant=False
        )

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
            if name and not target_user.admin_name:
                target_user.admin_name = name
            if name and not target_user.landlord_name:
                target_user.landlord_name = name
            if phone and not target_user.phone:
                target_user.phone = phone
            if target_user.family_id is None:
                target_user.family_id = apt.family_id
        else:
            target_user = User(
                telegram_id=telegram_id,
                name=name,
                admin_name=name,
                landlord_name=name,
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
            family_id=apt.family_id,
            action="add_tenant",
            details=f"Наниматель {display_name} (ID: {telegram_id}) добавлен в {prop_label}",
        ))
        await bump_business_revision_in_session(s)
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
        actor_role, actor_user = await assert_active_actor_in_session(
            s, actor_tg_id, ("admin", "landlord")
        )
        effective_admin = is_actor_admin and actor_role == "admin"
        effective_family_id = actor_user.family_id if actor_user else actor_family_id

        res = await s.execute(
            select(User).where(User.telegram_id == tenant_tg_id, User.role == "tenant")
        )
        target = res.scalar_one_or_none()
        if not target:
            raise LookupError("Наниматель не найден")
        if not effective_admin and target.family_id != effective_family_id:
            raise PermissionError("Нет доступа к этому нанимателю")

        if effective_admin:
            if target.landlord_name is None and target.name:
                target.landlord_name = target.name
            target.admin_name = name
        else:
            if target.admin_name is None and target.name:
                target.admin_name = target.name
            target.landlord_name = name
        if update_phone:
            target.phone = phone

        s.add(AuditLog(
            user_telegram_id=actor_tg_id,
            family_id=target.family_id,
            action="update_tenant",
            details=f"Наниматель ID={tenant_tg_id} обновлён: имя='{name}'",
        ))
        await bump_business_revision_in_session(s)
        await s.flush()
        await s.refresh(target)
        return target


async def remove_tenant_from_apartment_atomic(
    apartment_id: int,
    tenant_tg_id: int,
    actor_tg_id: int,
) -> tuple[str, bool]:
    """Verify access and tenant link, unlink atomically. Returns (property_label, fully_removed)."""
    if config.ADMIN_TELEGRAM_ID and tenant_tg_id == config.ADMIN_TELEGRAM_ID:
        raise PermissionError("Нельзя удалить администратора")

    async with UnitOfWork(write=True) as s:
        apt, _ = await assert_can_access_apartment_in_session(
            s, actor_tg_id, apartment_id, allow_tenant=False
        )

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
            family_id=apt.family_id,
            action="remove_tenant",
            details=f"Наниматель ID={tenant_tg_id} удалён из {prop_label} (full={fully_removed})",
        ))
        await bump_business_revision_in_session(s)
        return prop_label, fully_removed


async def update_contract_atomic(
    apartment_id: int,
    updates: dict,
    actor_tg_id: int,
) -> Apartment:
    """P1-03 & P2-17: merge only provided contract fields and validate final state inside transaction."""
    async with UnitOfWork(write=True) as s:
        apt, _ = await assert_can_access_apartment_in_session(
            s, actor_tg_id, apartment_id, allow_tenant=False
        )
        for k, v in updates.items():
            if k in ALLOWED_APARTMENT_FIELDS:
                setattr(apt, k, v)

        if apt.agreement_date_start and apt.agreement_date_finish:
            if apt.agreement_date_start > apt.agreement_date_finish:
                raise ValueError("Дата начала договора не может быть позже даты окончания")

        if apt.rent_payment_day is not None and not (1 <= apt.rent_payment_day <= 31):
            raise ValueError("Дата ежемесячного платежа должна быть числом от 1 до 31")

        if apt.rent_reminder_enabled:
            if apt.rent_payment_day is None:
                raise ValueError("Укажите дату ежемесячного платежа, чтобы включить напоминание об оплате")
            if apt.rent_reminder_mode not in RENT_REMINDER_MODES:
                raise ValueError("Выберите вариант напоминания об оплате")
            if apt.rent_reminder_mode == RENT_REMINDER_AFTER_DAYS:
                if apt.rent_reminder_days_after is None or not (1 <= apt.rent_reminder_days_after <= 20):
                    raise ValueError("Укажите количество дней после даты платежа (от 1 до 20)")
            else:
                apt.rent_reminder_days_after = None
        else:
            apt.rent_reminder_mode = None
            apt.rent_reminder_days_after = None

        s.add(AuditLog(
            user_telegram_id=actor_tg_id,
            family_id=apt.family_id,
            action="update_contract",
            details=f"Объект id={apartment_id}",
        ))
        await bump_business_revision_in_session(s)
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
        apt, _ = await assert_can_access_apartment_in_session(
            s, actor_tg_id, apartment_id, allow_tenant=False
        )

        apt.agreement_no = None
        apt.agreement_date_start = None
        apt.agreement_date_finish = None
        apt.rent_payment_day = None
        apt.rent_reminder_enabled = False
        apt.rent_reminder_mode = None
        apt.rent_reminder_days_after = None
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
            family_id=apt.family_id,
            action="delete_contract",
            details=(
                f"Договор объекта {prop_label} (id={apartment_id}) удалён"
                + (f", откреплено нанимателей: {len(affected_tenants)}" if unlink_tenants else "")
            ),
        ))
        await bump_business_revision_in_session(s)
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
        await assert_can_manage_family_in_session(s, actor_tg_id, family_id)
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
            family_id=family_id,
            action="create_apartment",
            details=(
                f"Создан объект {format_property_short(apt)} "
                f"(id={apt.id}, type={apt.property_type}, family_id={family_id})"
            ),
        ))
        await bump_business_revision_in_session(s)
        await s.refresh(apt)
        return apt


async def correct_reading_atomic(
    reading_id: int,
    new_value: int,
    actor_tg_id: int,
) -> tuple[int, int, int]:
    """P1-03, P1-04, P1-05: forbid replacement-init edits, enforce meter-generation neighbor bounds, audit."""
    async with UnitOfWork(write=True) as s:
        rec = await s.get(MeterReading, reading_id)
        if not rec:
            raise LookupError("Показание не найдено")

        apt, _ = await assert_can_access_apartment_in_session(
            s, actor_tg_id, rec.apartment_id, allow_tenant=False
        )

        if getattr(rec, "is_replacement_init", False):
            raise ValueError(
                "Нельзя редактировать стартовое показание замены счётчика через обычную корректировку"
            )

        # Previous bound in the same meter generation (stops at any replacement-init row)
        res_prev = await s.execute(
            select(MeterReading)
            .where(
                MeterReading.apartment_id == rec.apartment_id,
                MeterReading.meter_type == rec.meter_type,
                or_(
                    MeterReading.reading_date < rec.reading_date,
                    and_(MeterReading.reading_date == rec.reading_date, MeterReading.id < rec.id),
                ),
            )
            .order_by(desc(MeterReading.reading_date), desc(MeterReading.id))
            .limit(1)
        )
        prev_rec = res_prev.scalar_one_or_none()
        prev_bound: int | None = None
        prev_desc = ""
        if prev_rec is not None:
            prev_bound = prev_rec.value
            prev_desc = f"предыдущего показания ({prev_rec.value} от {prev_rec.reading_date.strftime('%d.%m.%Y')})"
        else:
            res_init = await s.execute(
                select(InitialReading).where(
                    InitialReading.apartment_id == rec.apartment_id,
                    InitialReading.meter_type == rec.meter_type,
                )
            )
            init_rec = res_init.scalar_one_or_none()
            if init_rec is not None:
                prev_bound = init_rec.value
                prev_desc = f"начального показания ({init_rec.value})"

        if prev_bound is not None and new_value < prev_bound:
            raise ValueError(
                f"Новое значение ({new_value}) меньше {prev_desc}. "
                f"Исправление нарушит расчёт расхода."
            )

        # Next bound in the same meter generation
        res_next = await s.execute(
            select(MeterReading)
            .where(
                MeterReading.apartment_id == rec.apartment_id,
                MeterReading.meter_type == rec.meter_type,
                or_(
                    MeterReading.reading_date > rec.reading_date,
                    and_(MeterReading.reading_date == rec.reading_date, MeterReading.id > rec.id),
                ),
            )
            .order_by(asc(MeterReading.reading_date), asc(MeterReading.id))
            .limit(1)
        )
        next_rec = res_next.scalar_one_or_none()
        next_bound: int | None = None
        next_desc = ""
        if next_rec is not None:
            if getattr(next_rec, "is_replacement_init", False):
                res_rep = await s.execute(
                    select(MeterReplacement).where(MeterReplacement.init_reading_id == next_rec.id).limit(1)
                )
                rep_row = res_rep.scalar_one_or_none()
                if rep_row is not None:
                    next_bound = rep_row.old_final_value
                    next_desc = (
                        f"конечного показания при замене счётчика "
                        f"({rep_row.old_final_value} от {rep_row.replacement_date.strftime('%d.%m.%Y')})"
                    )
            else:
                next_bound = next_rec.value
                next_desc = f"следующего показания ({next_rec.value} от {next_rec.reading_date.strftime('%d.%m.%Y')})"

        if next_bound is not None and new_value > next_bound:
            raise ValueError(
                f"Новое значение ({new_value}) больше {next_desc}. "
                f"Исправление обнулит расход следующего периода."
            )

        old_val = rec.value
        apt_id = rec.apartment_id
        rec.value = new_value
        s.add(AuditLog(
            user_telegram_id=actor_tg_id,
            family_id=apt.family_id,
            action="correct_reading",
            details=f"Показание #{reading_id}: {old_val} → {new_value}",
        ))
        await bump_business_revision_in_session(s)
        return apt_id, old_val, new_value


async def update_own_profile_atomic(
    telegram_id: int,
    name: str,
    is_actor_admin: bool = False,
) -> User:
    async with UnitOfWork(write=True) as s:
        actor_role, user = await assert_active_actor_in_session(
            s, telegram_id, ("admin", "landlord", "tenant")
        )
        if not user:
            if actor_role == "admin":
                user = User(telegram_id=telegram_id, name=name, admin_name=name, role="admin")
                s.add(user)
                await s.flush()
            else:
                raise LookupError("Профиль пользователя не найден")
        else:
            if actor_role == "admin" or is_actor_admin:
                user.name = name
                user.admin_name = name
            else:
                if user.admin_name is None and user.name:
                    user.admin_name = user.name
                if user.role == "tenant" and user.landlord_name is None and user.name:
                    user.landlord_name = user.name
                user.name = name
        s.add(AuditLog(
            user_telegram_id=telegram_id,
            family_id=user.family_id if actor_role != "admin" else None,
            action="update_own_name",
            details=f"Пользователь ID={telegram_id} изменил имя на '{name}'",
        ))
        await bump_business_revision_in_session(s)
        await s.flush()
        await s.refresh(user)
        return user


async def delete_apartment_atomic(
    apartment_id: int,
    actor_tg_id: int,
) -> tuple[str, list[tuple[int, bool]]]:
    """Delete a property atomically with all its readings/tariffs/links and clean up orphaned tenants."""
    async with UnitOfWork(write=True) as s:
        apt, _ = await assert_can_access_apartment_in_session(
            s, actor_tg_id, apartment_id, allow_tenant=False
        )

        prop_label = format_property_short(apt)
        apt_family_id = apt.family_id

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
            family_id=apt_family_id,
            action="delete_apartment",
            details=(
                f"Удалён объект {prop_label} (id={apartment_id}), "
                f"откреплено нанимателей: {len(affected_tenants)}"
            ),
        ))
        await bump_business_revision_in_session(s)
        return prop_label, affected_tenants


async def reorder_apartments_atomic(
    apartment_ids: list[int],
    actor_tg_id: int,
) -> list[int]:
    """Persist custom display order for properties in a single transaction with permission check."""
    unique_ids: list[int] = list(dict.fromkeys(apartment_ids))
    if not unique_ids:
        return []

    async with UnitOfWork(write=True) as s:
        actor_role, actor_user = await assert_active_actor_in_session(
            s, actor_tg_id, ("admin", "landlord")
        )
        res = await s.execute(select(Apartment).where(Apartment.id.in_(unique_ids)))
        apt_map = {a.id: a for a in res.scalars().all()}

        if actor_role == "landlord":
            actor_fam = actor_user.family_id if actor_user else None
            if any(apt_map.get(aid) is None or apt_map[aid].family_id != actor_fam for aid in unique_ids):
                raise PermissionError("Нет доступа к одному из объектов недвижимости")

        updated_ids: list[int] = []
        family_ids: set[int] = set()
        for idx, apt_id in enumerate(unique_ids, start=1):
            apt = apt_map.get(apt_id)
            if apt is not None:
                apt.sort_order = idx
                updated_ids.append(apt_id)
                family_ids.add(apt.family_id)

        audit_fam_id = next(iter(family_ids)) if len(family_ids) == 1 else None
        s.add(AuditLog(
            user_telegram_id=actor_tg_id,
            family_id=audit_fam_id,
            action="reorder_apartments",
            details=f"Порядок объектов обновлён ({len(updated_ids)} шт.)",
        ))
        await bump_business_revision_in_session(s)
        return updated_ids

"""SQLAlchemy ORM models, property types, and meter constants."""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Iterable

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow_naive() -> datetime.datetime:
    """UTC timestamp without tzinfo (Python 3.12+ replacement for utcnow())."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


WATER_METERS = ("cold_water", "hot_water")
IRRIGATION_METERS = ("irrigation",)
WATER_SUBMISSION_METERS = WATER_METERS + IRRIGATION_METERS
SINGLE_ELECTRICITY_METER = "t"
MULTI_ELECTRICITY_METERS = ("t1", "t2", "t3")
ELECTRICITY_METERS = (SINGLE_ELECTRICITY_METER,) + MULTI_ELECTRICITY_METERS
GAS_METERS = ("gas",)
LEGACY_METERS = WATER_METERS + MULTI_ELECTRICITY_METERS
ALL_METERS = WATER_METERS + IRRIGATION_METERS + ELECTRICITY_METERS + GAS_METERS
SEWERAGE_TARIFF = "sewerage"
ALL_TARIFF_TYPES = ALL_METERS + (SEWERAGE_TARIFF,)

IRRIGATION_MODE_AFTER_COLD = "after_cold"
IRRIGATION_MODE_PARALLEL = "parallel"
IRRIGATION_MODES = (IRRIGATION_MODE_AFTER_COLD, IRRIGATION_MODE_PARALLEL)

RENT_REMINDER_DAY_BEFORE = "day_before"
RENT_REMINDER_ON_DAY = "on_day"
RENT_REMINDER_AFTER_DAYS = "after_days"
RENT_REMINDER_MODES = (
    RENT_REMINDER_DAY_BEFORE,
    RENT_REMINDER_ON_DAY,
    RENT_REMINDER_AFTER_DAYS,
)

VERIFICATION_NOTIFY_INTERVALS = ("6m", "3m", "1m", "1w")
VERIFICATION_INTERVAL_LABELS = {
    "6m": "Полгода",
    "3m": "3 месяца",
    "1m": "Месяц",
    "1w": "Неделя",
}

PROPERTY_TYPES = {
    "apartment": "Квартира",
    "house": "Дом",
    "dacha": "Дача",
}

PROPERTY_SHORT_LABELS = {
    "apartment": "Кв.",
    "house": "Дом",
    "dacha": "Дача",
}

PROPERTY_ICONS = {
    "apartment": "🏢",
    "house": "🏡",
    "dacha": "🌿",
}

METER_LABELS = {
    "cold_water": "💧 ХВС",
    "hot_water": "♨️ ГВС",
    "irrigation": "🌱 Полив",
    "t": "⚡ Т",
    "t1": "⚡ Т1",
    "t2": "⚡ Т2",
    "t3": "⚡ Т3",
    "gas": "🔥 Газ",
}

METER_PLAIN_LABELS = {
    "cold_water": "ХВС",
    "hot_water": "ГВС",
    "irrigation": "Полив",
    "t": "Т",
    "t1": "Т1",
    "t2": "Т2",
    "t3": "Т3",
    "gas": "Газ",
}

METER_UNITS = {
    "cold_water": "м³",
    "hot_water": "м³",
    "irrigation": "м³",
    "t": "кВт·ч",
    "t1": "кВт·ч",
    "t2": "кВт·ч",
    "t3": "кВт·ч",
    "gas": "м³",
}

TARIFF_LABELS = {
    **METER_LABELS,
    SEWERAGE_TARIFF: "🚰 Водоотведение",
}

TARIFF_PLAIN_LABELS = {
    **METER_PLAIN_LABELS,
    SEWERAGE_TARIFF: "Водоотведение",
}


def serialize_enabled_meters(meters: Iterable[str]) -> str:
    """Validate and serialize enabled meter keys in canonical order."""
    chosen = set(meters)
    ordered = [m for m in ALL_METERS if m in chosen]
    return ",".join(ordered)


def serialize_notify_intervals(intervals: Iterable[str] | None) -> str | None:
    """Serialize verification reminder intervals in canonical order."""
    if not intervals:
        return None
    chosen = set(intervals)
    ordered = [iv for iv in VERIFICATION_NOTIFY_INTERVALS if iv in chosen]
    return ",".join(ordered) or None


def parse_notify_intervals(raw: str | None) -> list[str]:
    if not raw:
        return []
    chosen = {part.strip() for part in raw.split(",") if part.strip()}
    return [iv for iv in VERIFICATION_NOTIFY_INTERVALS if iv in chosen]


def get_apartment_enabled_meters(apt) -> list[str]:
    """Return active meter keys for a property; NULL defaults to legacy 5 meters."""
    raw = getattr(apt, "enabled_meters", None)
    if raw is None:
        return list(LEGACY_METERS)
    chosen = {part.strip() for part in raw.split(",") if part.strip()}
    ordered = [m for m in ALL_METERS if m in chosen]
    return ordered or list(LEGACY_METERS)


def get_apartment_irrigation_mode(apt) -> str:
    """Return irrigation meter connection mode ('after_cold' or 'parallel')."""
    mode = getattr(apt, "irrigation_mode", None)
    if mode in IRRIGATION_MODES:
        return mode
    return IRRIGATION_MODE_PARALLEL


def format_property_short(apt) -> str:
    """Compact human-readable property label, e.g. 'Кв. №371', 'Дом 12', 'Дача уч. 5'."""
    ptype = getattr(apt, "property_type", None) or "apartment"
    number = str(getattr(apt, "number", "") or "").strip()
    if ptype == "apartment":
        return f"Кв. №{number}"
    prefix = PROPERTY_SHORT_LABELS.get(ptype, "Кв.")
    return f"{prefix} {number}".strip()


def format_property_title(apt) -> str:
    """Full human-readable property title, e.g. 'Квартира №371', 'Дом 12', 'Дача уч. 5'."""
    ptype = getattr(apt, "property_type", None) or "apartment"
    number = str(getattr(apt, "number", "") or "").strip()
    if ptype == "apartment":
        return f"Квартира №{number}"
    label = PROPERTY_TYPES.get(ptype, "Квартира")
    return f"{label} {number}".strip()


def format_property_genitive(apt) -> str:
    """Genitive property title for Russian phrases, e.g. 'Квартиры №371', 'Дома 12', 'Дачи уч. 5'."""
    ptype = getattr(apt, "property_type", None) or "apartment"
    number = str(getattr(apt, "number", "") or "").strip()
    if ptype == "apartment":
        return f"Квартиры №{number}"
    if ptype == "house":
        return f"Дома {number}".strip()
    if ptype == "dacha":
        return f"Дачи {number}".strip()
    return f"Квартиры №{number}"


def has_apartment_contract(apt) -> bool:
    """Return True if any lease contract or monthly payment field is populated."""
    return bool(
        (getattr(apt, "agreement_no", None) or "").strip()
        or getattr(apt, "agreement_date_start", None)
        or getattr(apt, "agreement_date_finish", None)
        or getattr(apt, "rent_payment_day", None)
    )


def is_apartment_rented(apt, has_linked_tenants: bool = False) -> bool:
    """Property is considered rented when a contract, a tenant, or both are present."""
    return bool(
        getattr(apt, "has_tenant", False)
        or has_linked_tenants
        or has_apartment_contract(apt)
    )


def resolve_family_name(fam: "Family", viewer_role: str) -> str:
    """Resolve family name for viewer: admin sees admin_name; landlords see family's own name."""
    if viewer_role == "admin":
        return getattr(fam, "admin_name", None) or fam.name
    return fam.name


def resolve_user_display_name(
    user: "User",
    viewer_role: str,
    viewer_tg_id: int | None = None,
) -> str | None:
    """Resolve user display name respecting per-role naming isolation.

    - Any user viewing themselves sees their own `user.name`.
    - Admin viewing another user sees `user.admin_name` (fallback `user.name`).
    - Landlord viewing a tenant sees `user.landlord_name` (fallback `user.name`).
    - Landlord viewing another co-owner sees that owner's own `user.name`.
    """
    if viewer_tg_id is not None and user.telegram_id == viewer_tg_id:
        return user.name
    if viewer_role == "admin":
        return getattr(user, "admin_name", None) or user.name
    if viewer_role == "landlord" and user.role == "tenant":
        return getattr(user, "landlord_name", None) or user.name
    return user.name


class Base(DeclarativeBase):
    pass


class Family(Base):
    __tablename__ = "families"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    admin_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow_naive, nullable=False
    )

    apartments: Mapped[list["Apartment"]] = relationship(
        back_populates="family", cascade="all, delete-orphan"
    )
    users: Mapped[list["User"]] = relationship(back_populates="family")
    owner_links: Mapped[list["FamilyOwner"]] = relationship(
        back_populates="family", cascade="all, delete-orphan"
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    admin_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    landlord_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="tenant")
    family_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("families.id", ondelete="SET NULL"), nullable=True
    )
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow_naive, nullable=False
    )

    family: Mapped[Family | None] = relationship(back_populates="users")
    apartment_links: Mapped[list["ApartmentUser"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    family_owner_links: Mapped[list["FamilyOwner"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class FamilyOwner(Base):
    """Explicit owner membership in a family (allows attaching admin to any family)."""

    __tablename__ = "family_owners"
    __table_args__ = (
        UniqueConstraint("family_id", "user_id", name="uq_family_owner"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    family_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("families.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow_naive, nullable=False
    )

    family: Mapped[Family] = relationship(back_populates="owner_links")
    user: Mapped[User] = relationship(back_populates="family_owner_links")


class Apartment(Base):
    __tablename__ = "apartments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    number: Mapped[str] = mapped_column(String(50), nullable=False)
    property_type: Mapped[str] = mapped_column(String(20), nullable=False, default="apartment")
    enabled_meters: Mapped[str | None] = mapped_column(Text, nullable=True)
    irrigation_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default=IRRIGATION_MODE_PARALLEL
    )
    family_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("families.id", ondelete="CASCADE"), nullable=False
    )
    has_tenant: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    agreement_no: Mapped[str | None] = mapped_column(String(100), nullable=True)
    agreement_date_start: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    agreement_date_finish: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    rent_payment_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rent_reminder_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    rent_reminder_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    rent_reminder_days_after: Mapped[int | None] = mapped_column(Integer, nullable=True)
    water_submission_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    electricity_submission_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gas_submission_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow_naive, nullable=False
    )

    family: Mapped[Family] = relationship(back_populates="apartments")
    user_links: Mapped[list["ApartmentUser"]] = relationship(
        back_populates="apartment", cascade="all, delete-orphan"
    )
    tariffs: Mapped[list["Tariff"]] = relationship(
        back_populates="apartment", cascade="all, delete-orphan"
    )
    initial_readings: Mapped[list["InitialReading"]] = relationship(
        back_populates="apartment", cascade="all, delete-orphan"
    )
    readings: Mapped[list["MeterReading"]] = relationship(
        back_populates="apartment", cascade="all, delete-orphan"
    )
    meter_devices: Mapped[list["MeterDevice"]] = relationship(
        back_populates="apartment", cascade="all, delete-orphan"
    )
    meter_replacements: Mapped[list["MeterReplacement"]] = relationship(
        back_populates="apartment", cascade="all, delete-orphan"
    )


class ApartmentUser(Base):
    __tablename__ = "apartment_users"
    __table_args__ = (
        UniqueConstraint("apartment_id", "user_id", name="uq_apartment_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    apartment_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("apartments.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="tenant")

    apartment: Mapped[Apartment] = relationship(back_populates="user_links")
    user: Mapped[User] = relationship(back_populates="apartment_links")


class Tariff(Base):
    __tablename__ = "tariffs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    apartment_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("apartments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tariff_type: Mapped[str] = mapped_column(String(20), nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    effective_from: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    changed_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow_naive, nullable=False
    )

    apartment: Mapped[Apartment] = relationship(back_populates="tariffs")


class InitialReading(Base):
    __tablename__ = "initial_readings"
    __table_args__ = (
        UniqueConstraint("apartment_id", "meter_type", name="uq_initial_apt_meter"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    apartment_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("apartments.id", ondelete="CASCADE"), nullable=False
    )
    meter_type: Mapped[str] = mapped_column(String(20), nullable=False)
    value: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    set_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow_naive, onupdate=utcnow_naive, nullable=False
    )

    apartment: Mapped[Apartment] = relationship(back_populates="initial_readings")


class MeterReading(Base):
    __tablename__ = "meter_readings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    apartment_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("apartments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    meter_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    value: Mapped[int] = mapped_column(Integer, nullable=False)
    submitted_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reading_date: Mapped[datetime.date] = mapped_column(Date, nullable=False, index=True)
    is_replacement_init: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow_naive, nullable=False
    )

    apartment: Mapped[Apartment] = relationship(back_populates="readings")


class MeterDevice(Base):
    """Meter metadata: serial number, verification due date, and reminder intervals."""

    __tablename__ = "meter_devices"
    __table_args__ = (
        UniqueConstraint("apartment_id", "meter_type", name="uq_meter_device_apt_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    apartment_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("apartments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    meter_type: Mapped[str] = mapped_column(String(20), nullable=False)
    serial_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    verification_due_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    notify_intervals: Mapped[str | None] = mapped_column(String(50), nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow_naive, onupdate=utcnow_naive, nullable=False
    )

    apartment: Mapped[Apartment] = relationship(back_populates="meter_devices")


class MeterReplacement(Base):
    """History of physical meter replacements to preserve mid-month consumption continuity."""

    __tablename__ = "meter_replacements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    apartment_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("apartments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    meter_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    replacement_date: Mapped[datetime.date] = mapped_column(Date, nullable=False, index=True)
    old_final_value: Mapped[int] = mapped_column(Integer, nullable=False)
    new_initial_value: Mapped[int] = mapped_column(Integer, nullable=False)
    init_reading_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    old_serial_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    new_serial_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    replaced_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow_naive, nullable=False
    )

    apartment: Mapped[Apartment] = relationship(back_populates="meter_replacements")


class MeterSubmission(Base):
    """Idempotency ledger for meter reading submissions (HIGH-07)."""

    __tablename__ = "meter_submissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    submission_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    apartment_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("apartments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    submitted_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow_naive, nullable=False
    )


class SchedulerRun(Base):
    """Daily execution markers for scheduled jobs to prevent missed or duplicate runs (MEDIUM-09)."""

    __tablename__ = "scheduler_runs"
    __table_args__ = (
        UniqueConstraint("job_key", "run_date", name="uq_scheduler_run_job_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_key: Mapped[str] = mapped_column(String(80), nullable=False)
    run_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    completed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow_naive, nullable=False
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=utcnow_naive, nullable=False
    )

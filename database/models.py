"""SQLAlchemy ORM models and meter constants."""

from __future__ import annotations

import datetime
from decimal import Decimal

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

WATER_METERS = ("cold_water", "hot_water")
ELECTRICITY_METERS = ("t1", "t2", "t3")
ALL_METERS = WATER_METERS + ELECTRICITY_METERS

METER_LABELS = {
    "cold_water": "💧 ХВС",
    "hot_water": "🔥 ГВС",
    "t1": "⚡ Т1",
    "t2": "⚡ Т2",
    "t3": "⚡ Т3",
}

METER_PLAIN_LABELS = {
    "cold_water": "ХВС",
    "hot_water": "ГВС",
    "t1": "Т1",
    "t2": "Т2",
    "t3": "Т3",
}

METER_UNITS = {
    "cold_water": "м³",
    "hot_water": "м³",
    "t1": "кВт·ч",
    "t2": "кВт·ч",
    "t3": "кВт·ч",
}


class Base(DeclarativeBase):
    pass


class Family(Base):
    __tablename__ = "families"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow, nullable=False
    )

    apartments: Mapped[list["Apartment"]] = relationship(
        back_populates="family", cascade="all, delete-orphan"
    )
    users: Mapped[list["User"]] = relationship(back_populates="family")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="tenant")
    family_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("families.id", ondelete="SET NULL"), nullable=True
    )
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow, nullable=False
    )

    family: Mapped[Family | None] = relationship(back_populates="users")
    apartment_links: Mapped[list["ApartmentUser"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Apartment(Base):
    __tablename__ = "apartments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    number: Mapped[str] = mapped_column(String(50), nullable=False)
    family_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("families.id", ondelete="CASCADE"), nullable=False
    )
    has_tenant: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    agreement_no: Mapped[str | None] = mapped_column(String(100), nullable=True)
    agreement_date_start: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    agreement_date_finish: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    water_submission_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    electricity_submission_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow, nullable=False
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
        DateTime, default=datetime.datetime.utcnow, nullable=False
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
        DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow, nullable=False
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
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow, nullable=False
    )

    apartment: Mapped[Apartment] = relationship(back_populates="readings")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow, nullable=False
    )

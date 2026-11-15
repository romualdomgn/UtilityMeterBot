from datetime import date, datetime
from decimal import Decimal
from sqlalchemy import (
    BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Family(Base):
    __tablename__ = "families"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Apartment(Base):
    __tablename__ = "apartments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    family_id: Mapped[int | None] = mapped_column(ForeignKey("families.id", ondelete="CASCADE"), nullable=True)
    number: Mapped[str] = mapped_column(String(32), nullable=False)
    has_tenant: Mapped[bool] = mapped_column(Boolean, default=False)

    water_day: Mapped[int] = mapped_column(Integer, default=20)
    elec_day: Mapped[int] = mapped_column(Integer, default=25)

    init_hvs: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=Decimal("0"))
    init_gvs: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=Decimal("0"))
    init_t1: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=Decimal("0"))
    init_t2: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=Decimal("0"))
    init_t3: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=Decimal("0"))

    tariff_hvs: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0.00"))
    tariff_gvs: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0.00"))
    tariff_t1: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0.00"))
    tariff_t2: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0.00"))
    tariff_t3: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0.00"))

    agreement_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agreement_date_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    agreement_date_finish: Mapped[date | None] = mapped_column(Date, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class User(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # 'admin', 'landlord', 'tenant'
    family_id: Mapped[int | None] = mapped_column(ForeignKey("families.id", ondelete="SET NULL"), nullable=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    apartment_id: Mapped[int | None] = mapped_column(ForeignKey("apartments.id", ondelete="SET NULL"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    panel_msg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    selected_apt_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TariffHistory(Base):
    __tablename__ = "tariff_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    apartment_id: Mapped[int] = mapped_column(ForeignKey("apartments.id", ondelete="CASCADE"), nullable=False)
    tariff_hvs: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    tariff_gvs: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    tariff_t1: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    tariff_t2: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    tariff_t3: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    changed_by: Mapped[int] = mapped_column(BigInteger, nullable=False)


class MeterReading(Base):
    __tablename__ = "meter_readings"
    __table_args__ = (UniqueConstraint("apartment_id", "period", name="uq_apt_period"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    apartment_id: Mapped[int] = mapped_column(ForeignKey("apartments.id", ondelete="CASCADE"), nullable=False)
    period: Mapped[str] = mapped_column(String(7), nullable=False)
    reading_date: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    submitted_by: Mapped[int] = mapped_column(BigInteger, nullable=False)

    prev_hvs: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    prev_gvs: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    prev_t1: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    prev_t2: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    prev_t3: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)

    hvs: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    gvs: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    t1: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    t2: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    t3: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)

    diff_hvs: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    diff_gvs: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    diff_t1: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    diff_t2: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    diff_t3: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)

    tariff_hvs: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    tariff_gvs: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    tariff_t1: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    tariff_t2: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    tariff_t3: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    cost_hvs: Mapped[Decimal] = mapped_column(Numeric(12, 0), nullable=False)
    cost_gvs: Mapped[Decimal] = mapped_column(Numeric(12, 0), nullable=False)
    cost_water_total: Mapped[Decimal] = mapped_column(Numeric(12, 0), nullable=False)
    cost_t1: Mapped[Decimal] = mapped_column(Numeric(12, 0), nullable=False)
    cost_t2: Mapped[Decimal] = mapped_column(Numeric(12, 0), nullable=False)
    cost_t3: Mapped[Decimal] = mapped_column(Numeric(12, 0), nullable=False)
    cost_elec_total: Mapped[Decimal] = mapped_column(Numeric(12, 0), nullable=False)
    cost_total: Mapped[Decimal] = mapped_column(Numeric(12, 0), nullable=False)

    water_submitted: Mapped[bool] = mapped_column(Boolean, default=False)
    elec_submitted: Mapped[bool] = mapped_column(Boolean, default=False)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    apartment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    details: Mapped[str] = mapped_column(Text, nullable=False)

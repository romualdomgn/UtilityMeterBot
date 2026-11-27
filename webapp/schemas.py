"""Pydantic schemas with strict SQLite integer bounds (MEDIUM-03) and shared serialization helpers."""

from __future__ import annotations

import datetime
from typing import Annotated, Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field, Strict

from database.models import (
    PROPERTY_ICONS,
    PROPERTY_TYPES,
    format_property_short,
    format_property_title,
    get_apartment_enabled_meters,
    get_apartment_irrigation_mode,
    is_apartment_rented,
    parse_notify_intervals,
)
from webapp.auth import WebAppUser

MAX_READING_VALUE = 999_999_999_999
MAX_SQLITE_INT64 = 9_223_372_036_854_775_807

StrictReadingInt = Annotated[int, Strict(), Field(ge=0, le=MAX_READING_VALUE)]
StrictTelegramId = Annotated[int, Strict(), Field(ge=1, le=MAX_SQLITE_INT64)]

PropertyTypeLiteral = Literal["apartment", "house", "dacha"]
IrrigationModeLiteral = Literal["after_cold", "parallel"]
NotifyIntervalLiteral = Literal["6m", "3m", "1m", "1w"]
RentReminderModeLiteral = Literal["day_before", "on_day", "after_days"]


class ProfileUpdateBody(BaseModel):
    name: str


class ApartmentCreateBody(BaseModel):
    number: str
    property_type: PropertyTypeLiteral = "apartment"
    enabled_meters: list[str] | None = None
    irrigation_mode: IrrigationModeLiteral = "parallel"
    family_id: int | None = None


class ApartmentReorderBody(BaseModel):
    apartment_ids: list[int]


class ApartmentSettingsBody(BaseModel):
    number: str | None = None
    property_type: PropertyTypeLiteral | None = None
    enabled_meters: list[str] | None = None
    irrigation_mode: IrrigationModeLiteral | None = None
    water_submission_day: int | None = None
    electricity_submission_day: int | None = None
    gas_submission_day: int | None = None
    tariffs: dict[str, str | float | int | None] = {}


class InitialReadingsBody(BaseModel):
    cold_water: StrictReadingInt | None = None
    hot_water: StrictReadingInt | None = None
    irrigation: StrictReadingInt | None = None
    t: StrictReadingInt | None = None
    t1: StrictReadingInt | None = None
    t2: StrictReadingInt | None = None
    t3: StrictReadingInt | None = None
    gas: StrictReadingInt | None = None


class WaterReadingBody(BaseModel):
    cold_water: StrictReadingInt | None = None
    hot_water: StrictReadingInt | None = None
    irrigation: StrictReadingInt | None = None
    force: bool = False
    allow_decrease: bool = False
    submission_id: str | None = Field(default=None, max_length=64)


class ElectricityReadingBody(BaseModel):
    t: StrictReadingInt | None = None
    t1: StrictReadingInt | None = None
    t2: StrictReadingInt | None = None
    t3: StrictReadingInt | None = None
    force: bool = False
    allow_decrease: bool = False
    submission_id: str | None = Field(default=None, max_length=64)


class GasReadingBody(BaseModel):
    gas: StrictReadingInt
    force: bool = False
    allow_decrease: bool = False
    submission_id: str | None = Field(default=None, max_length=64)


class ReadingCorrectionBody(BaseModel):
    value: StrictReadingInt


class MeterDeviceItemBody(BaseModel):
    meter_type: str
    serial_number: str | None = Field(default=None, max_length=100)
    verification_due_date: str | None = None
    notify_intervals: list[NotifyIntervalLiteral] = []


class MeterDevicesUpdateBody(BaseModel):
    items: list[MeterDeviceItemBody]


class MeterReplacementTariffEntry(BaseModel):
    meter_type: str
    old_final_value: StrictReadingInt
    new_initial_value: StrictReadingInt


class MeterReplacementBody(BaseModel):
    meter_type: str
    old_final_value: StrictReadingInt | None = None
    new_initial_value: StrictReadingInt | None = None
    tariff_entries: list[MeterReplacementTariffEntry] | None = None
    new_serial_number: str | None = Field(default=None, max_length=100)
    new_verification_due_date: str | None = None


class ContractBody(BaseModel):
    has_tenant: bool | None = None
    agreement_no: str | None = None
    agreement_date_start: str | None = None
    agreement_date_finish: str | None = None
    rent_payment_day: int | None = None
    rent_reminder_enabled: bool | None = None
    rent_reminder_mode: RentReminderModeLiteral | None = None
    rent_reminder_days_after: int | None = None


class TenantCreateBody(BaseModel):
    telegram_id: StrictTelegramId
    name: str
    force_no_contract: bool = False


class TenantUpdateBody(BaseModel):
    name: str


class FamilyBody(BaseModel):
    name: str


class FamilyInviteBody(BaseModel):
    family_name: str
    owner_telegram_id: StrictTelegramId
    owner_name: str


class FamilyOwnerBody(BaseModel):
    telegram_id: StrictTelegramId
    name: str


class FamilyOwnerUpdateBody(BaseModel):
    name: str


class ExportBody(BaseModel):
    format: Literal["excel", "pdf"]
    apartment_id: int | None = None


def parse_iso_date(raw: str | None) -> datetime.date | None:
    if not raw or not raw.strip():
        return None
    raw = raw.strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise HTTPException(400, f"Неверный формат даты: {raw} (ожидается ДД.ММ.ГГГГ)")


def serialize_meter_device(meter_type: str, device) -> dict:
    return {
        "meter_type": meter_type,
        "serial_number": device.serial_number if device else None,
        "verification_due_date": (
            device.verification_due_date.isoformat()
            if (device and device.verification_due_date)
            else None
        ),
        "notify_intervals": parse_notify_intervals(device.notify_intervals) if device else [],
    }


def serialize_apartment(
    apt,
    has_linked_tenants: bool = False,
    has_overdue_meters: bool = False,
) -> dict:
    ptype = getattr(apt, "property_type", None) or "apartment"
    return {
        "id": apt.id,
        "number": apt.number,
        "property_type": ptype,
        "property_type_label": PROPERTY_TYPES.get(ptype, "Квартира"),
        "property_icon": PROPERTY_ICONS.get(ptype, "🏢"),
        "display_name": format_property_short(apt),
        "full_title": format_property_title(apt),
        "enabled_meters": get_apartment_enabled_meters(apt),
        "irrigation_mode": get_apartment_irrigation_mode(apt),
        "family_id": apt.family_id,
        "has_tenant": apt.has_tenant,
        "is_rented": is_apartment_rented(apt, has_linked_tenants=has_linked_tenants),
        "has_overdue_meters": bool(has_overdue_meters),
        "agreement_no": apt.agreement_no,
        "agreement_date_start": apt.agreement_date_start.isoformat() if apt.agreement_date_start else None,
        "agreement_date_finish": apt.agreement_date_finish.isoformat() if apt.agreement_date_finish else None,
        "rent_payment_day": getattr(apt, "rent_payment_day", None),
        "rent_reminder_enabled": bool(getattr(apt, "rent_reminder_enabled", False)),
        "rent_reminder_mode": getattr(apt, "rent_reminder_mode", None),
        "rent_reminder_days_after": getattr(apt, "rent_reminder_days_after", None),
        "water_submission_day": apt.water_submission_day,
        "electricity_submission_day": apt.electricity_submission_day,
        "gas_submission_day": getattr(apt, "gas_submission_day", None),
        "sort_order": getattr(apt, "sort_order", 0),
    }


def require_role(user: WebAppUser, roles: tuple[str, ...]) -> None:
    if user.role not in roles:
        raise HTTPException(403, "Недостаточно прав")

"""Pydantic schemas and shared serialization helpers for Web App API."""

from __future__ import annotations

import datetime
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel

from database.models import (
    PROPERTY_ICONS,
    PROPERTY_TYPES,
    format_property_short,
    format_property_title,
    get_apartment_enabled_meters,
)
from webapp.auth import WebAppUser

PropertyTypeLiteral = Literal["apartment", "house", "dacha"]


class ProfileUpdateBody(BaseModel):
    name: str


class ApartmentCreateBody(BaseModel):
    number: str
    property_type: PropertyTypeLiteral = "apartment"
    enabled_meters: list[str] | None = None
    family_id: int | None = None


class ApartmentReorderBody(BaseModel):
    apartment_ids: list[int]


class ApartmentSettingsBody(BaseModel):
    number: str | None = None
    property_type: PropertyTypeLiteral | None = None
    enabled_meters: list[str] | None = None
    water_submission_day: int | None = None
    electricity_submission_day: int | None = None
    gas_submission_day: int | None = None
    tariffs: dict[str, str | float | int | None] = {}


class InitialReadingsBody(BaseModel):
    cold_water: int | None = None
    hot_water: int | None = None
    t1: int | None = None
    t2: int | None = None
    t3: int | None = None
    gas: int | None = None


class WaterReadingBody(BaseModel):
    cold_water: int | None = None
    hot_water: int | None = None
    force: bool = False


class ElectricityReadingBody(BaseModel):
    t1: int | None = None
    t2: int | None = None
    t3: int | None = None
    force: bool = False


class GasReadingBody(BaseModel):
    gas: int
    force: bool = False


class ReadingCorrectionBody(BaseModel):
    value: int


class ContractBody(BaseModel):
    has_tenant: bool | None = None
    agreement_no: str | None = None
    agreement_date_start: str | None = None
    agreement_date_finish: str | None = None


class TenantCreateBody(BaseModel):
    telegram_id: int
    name: str
    phone: str | None = None
    force_no_contract: bool = False


class FamilyBody(BaseModel):
    name: str


class FamilyOwnerBody(BaseModel):
    telegram_id: int
    name: str
    phone: str | None = None


class FamilyOwnerUpdateBody(BaseModel):
    name: str
    phone: str | None = None


class ExportBody(BaseModel):
    format: Literal["excel", "pdf"]
    apartment_id: int | None = None


def parse_iso_date(raw: str | None) -> datetime.date | None:
    if not raw or not raw.strip():
        return None
    raw = raw.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise HTTPException(400, f"Неверный формат даты: {raw}")


def serialize_apartment(apt) -> dict:
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
        "family_id": apt.family_id,
        "has_tenant": apt.has_tenant,
        "agreement_no": apt.agreement_no,
        "agreement_date_start": apt.agreement_date_start.isoformat() if apt.agreement_date_start else None,
        "agreement_date_finish": apt.agreement_date_finish.isoformat() if apt.agreement_date_finish else None,
        "water_submission_day": apt.water_submission_day,
        "electricity_submission_day": apt.electricity_submission_day,
        "gas_submission_day": getattr(apt, "gas_submission_day", None),
        "sort_order": getattr(apt, "sort_order", 0),
    }


def require_role(user: WebAppUser, roles: tuple[str, ...]) -> None:
    if user.role not in roles:
        raise HTTPException(403, "Недостаточно прав")

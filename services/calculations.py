"""Consumption and Decimal billing calculations."""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

from database import crud
from database.models import (
    ALL_METERS,
    ELECTRICITY_METERS,
    METER_LABELS,
    METER_UNITS,
    WATER_METERS,
)

MONTH_NAMES_RU = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}


def fmt_date(dt: datetime.date | None) -> str:
    return dt.strftime("%d.%m.%Y") if dt else "—"


def _round_to_rubles(amount: Decimal) -> int:
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass
class MeterResult:
    meter_type: str
    previous: int
    current: int
    consumption: int
    tariff: Decimal
    cost: int

    def to_dict(self) -> dict:
        return {
            "meter_type": self.meter_type,
            "label": METER_LABELS.get(self.meter_type, self.meter_type),
            "unit": METER_UNITS.get(self.meter_type, ""),
            "previous": self.previous,
            "current": self.current,
            "consumption": self.consumption,
            "tariff": float(self.tariff),
            "cost": self.cost,
        }


@dataclass
class BillingResult:
    apartment_id: int
    period_label: str
    meters: dict[str, MeterResult] = field(default_factory=dict)

    @property
    def water_consumption_total(self) -> int:
        return sum(self.meters[m].consumption for m in WATER_METERS if m in self.meters)

    @property
    def water_total(self) -> int:
        return sum(self.meters[m].cost for m in WATER_METERS if m in self.meters)

    @property
    def electricity_consumption_total(self) -> int:
        return sum(self.meters[m].consumption for m in ELECTRICITY_METERS if m in self.meters)

    @property
    def electricity_total(self) -> int:
        return sum(self.meters[m].cost for m in ELECTRICITY_METERS if m in self.meters)

    @property
    def grand_total(self) -> int:
        return self.water_total + self.electricity_total

    def to_dict(self) -> dict:
        return {
            "apartment_id": self.apartment_id,
            "period_label": self.period_label,
            "meters": {k: v.to_dict() for k, v in self.meters.items()},
            "water_consumption_total": self.water_consumption_total,
            "water_total": self.water_total,
            "electricity_consumption_total": self.electricity_consumption_total,
            "electricity_total": self.electricity_total,
            "grand_total": self.grand_total,
        }


async def get_prev_value(apartment_id: int, meter_type: str) -> int:
    latest = await crud.get_latest_reading(apartment_id, meter_type)
    if latest:
        return latest.value
    initial = await crud.get_initial_reading(apartment_id, meter_type)
    return initial.value if initial else 0


async def calculate_billing(
    apartment_id: int,
    current_readings: dict[str, int] | None = None,
    year: int | None = None,
    month: int | None = None,
) -> BillingResult:
    today = datetime.date.today()
    tariffs = await crud.get_all_tariffs(apartment_id)
    initials = await crud.get_all_initial_readings(apartment_id)

    if year and month:
        period_label = f"{MONTH_NAMES_RU.get(month, month)} {year}"
        month_rows = await crud.get_readings_by_month(apartment_id, year, month)
        latest_in_month: dict[str, int] = {}
        for r in month_rows:
            if r.meter_type not in latest_in_month:
                latest_in_month[r.meter_type] = r.value
        first_day = datetime.date(year, month, 1)
    else:
        period_label = f"{MONTH_NAMES_RU.get(today.month, today.month)} {today.year}"
        latest_in_month = {}
        first_day = None

    res = BillingResult(apartment_id=apartment_id, period_label=period_label)

    for mt in ALL_METERS:
        rate = Decimal(tariffs[mt].rate) if tariffs.get(mt) else Decimal("0")
        init_val = initials.get(mt, 0)

        if current_readings is not None:
            if mt not in current_readings:
                continue
            prev_val = await get_prev_value(apartment_id, mt)
            curr_val = current_readings[mt]
        elif year and month and first_day:
            prev_rec = await crud.get_latest_reading_before(apartment_id, mt, first_day)
            prev_val = prev_rec.value if prev_rec else init_val
            curr_val = latest_in_month.get(mt, prev_val)
        else:
            last_two = await crud.get_last_two_readings(apartment_id, mt)
            if len(last_two) >= 2:
                curr_val = last_two[0].value
                prev_val = last_two[1].value
            elif len(last_two) == 1:
                curr_val = last_two[0].value
                prev_val = init_val
            else:
                curr_val = init_val
                prev_val = init_val

        consumption = max(0, curr_val - prev_val)
        cost = _round_to_rubles(Decimal(consumption) * rate)
        res.meters[mt] = MeterResult(
            meter_type=mt,
            previous=prev_val,
            current=curr_val,
            consumption=consumption,
            tariff=rate,
            cost=cost,
        )

    return res


def format_reading_notification(
    apartment_number: str,
    meter_type: str,
    prev_val: int,
    new_val: int,
) -> str:
    label = METER_LABELS.get(meter_type, meter_type)
    unit = METER_UNITS.get(meter_type, "")
    diff = max(0, new_val - prev_val)
    return f"{label}: {prev_val} → <b>{new_val}</b> {unit} (расход: {diff} {unit})"

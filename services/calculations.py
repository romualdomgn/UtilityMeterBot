"""Calendar-month consumption and historical-tariff billing calculations."""

from __future__ import annotations

import calendar
import datetime
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

from config import local_today
from database import crud
from database.models import (
    ALL_METERS,
    ELECTRICITY_METERS,
    GAS_METERS,
    METER_LABELS,
    METER_UNITS,
    WATER_METERS,
    get_apartment_enabled_meters,
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
    previous: int | None
    current: int | None
    consumption: int
    tariff: Decimal | None
    cost: int
    has_base: bool = True
    has_tariff: bool = True
    submitted: bool = True

    def to_dict(self) -> dict:
        return {
            "meter_type": self.meter_type,
            "label": METER_LABELS.get(self.meter_type, self.meter_type),
            "unit": METER_UNITS.get(self.meter_type, ""),
            "previous": self.previous,
            "current": self.current,
            "consumption": self.consumption,
            "tariff": float(self.tariff) if self.tariff is not None else None,
            "cost": self.cost,
            "has_base": self.has_base,
            "has_tariff": self.has_tariff,
            "submitted": self.submitted,
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
    def gas_consumption_total(self) -> int:
        return sum(self.meters[m].consumption for m in GAS_METERS if m in self.meters)

    @property
    def gas_total(self) -> int:
        return sum(self.meters[m].cost for m in GAS_METERS if m in self.meters)

    @property
    def grand_total(self) -> int:
        return self.water_total + self.electricity_total + self.gas_total

    def to_dict(self) -> dict:
        return {
            "apartment_id": self.apartment_id,
            "period_label": self.period_label,
            "meters": {k: v.to_dict() for k, v in self.meters.items()},
            "water_consumption_total": self.water_consumption_total,
            "water_total": self.water_total,
            "electricity_consumption_total": self.electricity_consumption_total,
            "electricity_total": self.electricity_total,
            "gas_consumption_total": self.gas_consumption_total,
            "gas_total": self.gas_total,
            "grand_total": self.grand_total,
        }


async def get_prev_value(apartment_id: int, meter_type: str) -> int | None:
    """Latest saved reading or explicit initial reading; None if uninitialized."""
    latest = await crud.get_latest_reading(apartment_id, meter_type)
    if latest:
        return latest.value
    initial = await crud.get_initial_reading(apartment_id, meter_type)
    return initial.value if initial else None


async def calculate_billing(
    apartment_id: int,
    current_readings: dict[str, int] | None = None,
    year: int | None = None,
    month: int | None = None,
) -> BillingResult:
    """Calculate consumption and cost for a calendar month or live reading submission.

    HIGH-01: calendar month always spans latest reading before 1st of month -> latest in month.
    HIGH-02: tariffs resolved as of end of period (or today for live submission).
    HIGH-05: missing initial reading is kept as None (no fake billing from 0).
    """
    today = local_today()
    target_year = year or today.year
    target_month = month or today.month
    first_day = datetime.date(target_year, target_month, 1)
    last_day = calendar.monthrange(target_year, target_month)[1]
    end_of_month = datetime.date(target_year, target_month, last_day)

    period_label = f"{MONTH_NAMES_RU.get(target_month, target_month)} {target_year}"
    tariff_date = today if current_readings is not None else end_of_month

    apt = await crud.get_apartment(apartment_id)
    enabled_set = set(get_apartment_enabled_meters(apt))

    tariffs = await crud.get_all_tariffs(apartment_id, as_of_date=tariff_date)
    initials = await crud.get_all_initial_readings(apartment_id)

    latest_in_month: dict[str, int] = {}
    if current_readings is None:
        for r in await crud.get_readings_by_month(apartment_id, target_year, target_month):
            if r.meter_type not in latest_in_month:
                latest_in_month[r.meter_type] = r.value

    res = BillingResult(apartment_id=apartment_id, period_label=period_label)

    for mt in ALL_METERS:
        if current_readings is not None:
            if mt not in current_readings:
                continue
        else:
            if mt not in enabled_set and mt not in latest_in_month:
                continue

        tariff_obj = tariffs.get(mt)
        has_tariff = tariff_obj is not None
        rate = Decimal(tariff_obj.rate) if has_tariff else None
        init_val = initials.get(mt)

        if current_readings is not None:
            prev_val = await get_prev_value(apartment_id, mt)
            curr_val: int | None = current_readings[mt]
            submitted = True
        else:
            prev_rec = await crud.get_latest_reading_before(apartment_id, mt, first_day)
            prev_val = prev_rec.value if prev_rec else init_val
            curr_val = latest_in_month.get(mt)
            submitted = curr_val is not None

        has_base = prev_val is not None
        if submitted and has_base and curr_val is not None and prev_val is not None:
            consumption = max(0, curr_val - prev_val)
            cost = _round_to_rubles(Decimal(consumption) * rate) if (has_tariff and rate is not None) else 0
        else:
            consumption = 0
            cost = 0

        res.meters[mt] = MeterResult(
            meter_type=mt,
            previous=prev_val,
            current=curr_val,
            consumption=consumption,
            tariff=rate,
            cost=cost,
            has_base=has_base,
            has_tariff=has_tariff,
            submitted=submitted,
        )

    return res


def format_reading_notification(
    apartment_number: str,
    meter_type: str,
    prev_val: int | None,
    new_val: int,
) -> str:
    label = METER_LABELS.get(meter_type, meter_type)
    unit = METER_UNITS.get(meter_type, "")
    if prev_val is None:
        return f"{label}: <b>{new_val}</b> {unit} (начальное показание не задано)"
    diff = max(0, new_val - prev_val)
    return f"{label}: {prev_val} → <b>{new_val}</b> {unit} (расход: {diff} {unit})"

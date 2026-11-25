"""Calendar-month consumption, meter replacement continuity, and historical-tariff billing calculations."""

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
    IRRIGATION_METERS,
    IRRIGATION_MODE_AFTER_COLD,
    IRRIGATION_MODE_PARALLEL,
    METER_LABELS,
    METER_UNITS,
    SEWERAGE_TARIFF,
    TARIFF_LABELS,
    WATER_METERS,
    get_apartment_enabled_meters,
    get_apartment_irrigation_mode,
)

MONTH_NAMES_RU = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}


def fmt_date(dt: datetime.date | None) -> str:
    return dt.strftime("%d.%m.%Y") if dt else "—"


def fmt_num(val: int | float | Decimal) -> str:
    if isinstance(val, int):
        return f"{val:,}".replace(",", " ")
    raw = str(val)
    if "." in raw:
        int_part, frac_part = raw.split(".", 1)
        return f"{int(int_part):,}".replace(",", " ") + "." + frac_part
    return f"{int(raw):,}".replace(",", " ")


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
    replaced: bool = False

    def to_dict(self) -> dict:
        return {
            "meter_type": self.meter_type,
            "label": TARIFF_LABELS.get(self.meter_type, self.meter_type),
            "unit": METER_UNITS.get(self.meter_type, "м³" if self.meter_type == SEWERAGE_TARIFF else ""),
            "previous": self.previous,
            "current": self.current,
            "consumption": self.consumption,
            "tariff": float(self.tariff) if self.tariff is not None else None,
            "cost": self.cost,
            "has_base": self.has_base,
            "has_tariff": self.has_tariff,
            "submitted": self.submitted,
            "replaced": self.replaced,
        }


@dataclass
class BillingResult:
    apartment_id: int
    period_label: str
    meters: dict[str, MeterResult] = field(default_factory=dict)
    sewerage: MeterResult | None = None
    irrigation_mode: str = IRRIGATION_MODE_PARALLEL

    @property
    def water_consumption_total(self) -> int:
        return sum(self.meters[m].consumption for m in WATER_METERS if m in self.meters)

    @property
    def water_total(self) -> int:
        return sum(self.meters[m].cost for m in WATER_METERS if m in self.meters)

    @property
    def sewerage_consumption_total(self) -> int:
        return self.sewerage.consumption if self.sewerage else 0

    @property
    def sewerage_total(self) -> int:
        return self.sewerage.cost if self.sewerage else 0

    @property
    def irrigation_consumption_total(self) -> int:
        return sum(self.meters[m].consumption for m in IRRIGATION_METERS if m in self.meters)

    @property
    def irrigation_total(self) -> int:
        return sum(self.meters[m].cost for m in IRRIGATION_METERS if m in self.meters)

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
        return (
            self.water_total
            + self.sewerage_total
            + self.irrigation_total
            + self.electricity_total
            + self.gas_total
        )

    @property
    def incomplete_reasons(self) -> list[str]:
        reasons: list[str] = []
        if not self.meters:
            return reasons
        if any(not m.submitted for m in self.meters.values()):
            reasons.append("не все показания поданы")
        if any(m.submitted and not m.has_base for m in self.meters.values()):
            reasons.append("не задано начальное показание")
        if any(m.submitted and m.has_base and not m.has_tariff for m in self.meters.values()):
            reasons.append("не задан тариф")
        return reasons

    @property
    def is_complete(self) -> bool:
        return bool(self.meters) and not self.incomplete_reasons

    def to_dict(self) -> dict:
        return {
            "apartment_id": self.apartment_id,
            "period_label": self.period_label,
            "meters": {k: v.to_dict() for k, v in self.meters.items()},
            "sewerage": self.sewerage.to_dict() if self.sewerage else None,
            "irrigation_mode": self.irrigation_mode,
            "water_consumption_total": self.water_consumption_total,
            "water_total": self.water_total,
            "sewerage_consumption_total": self.sewerage_consumption_total,
            "sewerage_total": self.sewerage_total,
            "irrigation_consumption_total": self.irrigation_consumption_total,
            "irrigation_total": self.irrigation_total,
            "electricity_consumption_total": self.electricity_consumption_total,
            "electricity_total": self.electricity_total,
            "gas_consumption_total": self.gas_consumption_total,
            "gas_total": self.gas_total,
            "grand_total": self.grand_total,
            "is_complete": self.is_complete,
            "incomplete_reasons": self.incomplete_reasons,
        }


def sanitize_billing_for_tenant(billing_dict: dict) -> dict:
    """MEDIUM-01: strip tariffs, sewerage financials, and monetary totals from API payloads for tenants."""
    clean_meters: dict[str, dict] = {}
    has_unsubmitted = False
    has_missing_base = False
    for mt, m in (billing_dict.get("meters") or {}).items():
        cm = {
            "meter_type": m["meter_type"],
            "label": m["label"],
            "unit": m["unit"],
            "previous": m["previous"],
            "current": m["current"],
            "consumption": m["consumption"],
            "has_base": m["has_base"],
            "submitted": m["submitted"],
            "replaced": m.get("replaced", False),
        }
        if not cm["submitted"]:
            has_unsubmitted = True
        elif not cm["has_base"]:
            has_missing_base = True
        clean_meters[mt] = cm

    reasons: list[str] = []
    if has_unsubmitted:
        reasons.append("не все показания поданы")
    if has_missing_base:
        reasons.append("не задано начальное показание")

    return {
        "apartment_id": billing_dict["apartment_id"],
        "period_label": billing_dict["period_label"],
        "meters": clean_meters,
        "irrigation_mode": billing_dict.get("irrigation_mode", IRRIGATION_MODE_PARALLEL),
        "water_consumption_total": billing_dict.get("water_consumption_total", 0),
        "irrigation_consumption_total": billing_dict.get("irrigation_consumption_total", 0),
        "electricity_consumption_total": billing_dict.get("electricity_consumption_total", 0),
        "gas_consumption_total": billing_dict.get("gas_consumption_total", 0),
        "is_complete": bool(clean_meters) and not reasons,
        "incomplete_reasons": reasons,
    }


async def get_prev_value(apartment_id: int, meter_type: str) -> int | None:
    latest = await crud.get_latest_reading(apartment_id, meter_type)
    if latest:
        return latest.value
    initial = await crud.get_initial_reading(apartment_id, meter_type)
    return initial.value if initial else None


async def get_month_base_info(
    apartment_id: int,
    year: int | None = None,
    month: int | None = None,
) -> dict[str, dict]:
    """HIGH-01: per-meter calendar month baseline and replacement offset for accurate live previews."""
    today = local_today()
    target_year = year or today.year
    target_month = month or today.month
    first_day = datetime.date(target_year, target_month, 1)

    apt = await crud.get_apartment(apartment_id)
    enabled = get_apartment_enabled_meters(apt)
    initials = await crud.get_all_initial_readings(apartment_id)
    month_readings = await crud.get_readings_by_month(apartment_id, target_year, target_month)
    month_replacements = await crud.get_meter_replacements_by_month(apartment_id, target_year, target_month)

    reps_by_mt: dict[str, list] = {}
    for rep in month_replacements:
        reps_by_mt.setdefault(rep.meter_type, []).append(rep)

    has_normal_in_month: set[str] = {
        r.meter_type for r in month_readings if not getattr(r, "is_replacement_init", False)
    }

    info: dict[str, dict] = {}
    for mt in enabled:
        prev_rec = await crud.get_latest_reading_before(apartment_id, mt, first_day)
        base_before = prev_rec.value if prev_rec else initials.get(mt)
        reps = reps_by_mt.get(mt, [])
        if reps:
            offset = 0
            if base_before is not None:
                offset += max(0, reps[0].old_final_value - base_before)
            for idx in range(1, len(reps)):
                offset += max(0, reps[idx].old_final_value - reps[idx - 1].new_initial_value)
            info[mt] = {
                "month_base": reps[-1].new_initial_value,
                "replacement_offset": offset,
                "replaced_this_month": True,
                "submitted_this_month": mt in has_normal_in_month,
            }
        else:
            info[mt] = {
                "month_base": base_before,
                "replacement_offset": 0,
                "replaced_this_month": False,
                "submitted_this_month": mt in has_normal_in_month,
            }
    return info


async def calculate_billing(
    apartment_id: int,
    current_readings: dict[str, int] | None = None,
    year: int | None = None,
    month: int | None = None,
) -> BillingResult:
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
    irrigation_mode = get_apartment_irrigation_mode(apt)

    tariffs = await crud.get_all_tariffs(apartment_id, as_of_date=tariff_date)
    initials = await crud.get_all_initial_readings(apartment_id)

    month_readings: list = []
    latest_in_month: dict[str, int] = {}
    readings_by_mt: dict[str, list] = {}
    reps_by_mt: dict[str, list] = {}
    if current_readings is None:
        month_readings = await crud.get_readings_by_month(apartment_id, target_year, target_month)
        for r in month_readings:
            readings_by_mt.setdefault(r.meter_type, []).append(r)
            if not getattr(r, "is_replacement_init", False) and r.meter_type not in latest_in_month:
                latest_in_month[r.meter_type] = r.value
        for rep in await crud.get_meter_replacements_by_month(apartment_id, target_year, target_month):
            reps_by_mt.setdefault(rep.meter_type, []).append(rep)

    res = BillingResult(
        apartment_id=apartment_id,
        period_label=period_label,
        irrigation_mode=irrigation_mode,
    )

    for mt in ALL_METERS:
        if current_readings is not None:
            if mt not in current_readings:
                continue
        else:
            if mt not in enabled_set and mt not in latest_in_month and mt not in reps_by_mt:
                continue

        is_irrigation_after_cold = (
            mt in IRRIGATION_METERS and irrigation_mode == IRRIGATION_MODE_AFTER_COLD
        )
        if is_irrigation_after_cold:
            has_tariff = True
            rate = None
        else:
            tariff_obj = tariffs.get(mt)
            has_tariff = tariff_obj is not None
            rate = Decimal(tariff_obj.rate) if has_tariff else None
        init_val = initials.get(mt)
        replaced = False

        if current_readings is not None:
            prev_val = await get_prev_value(apartment_id, mt)
            curr_val: int | None = current_readings[mt]
            submitted = True
            has_base = prev_val is not None
            if has_base and curr_val is not None and prev_val is not None:
                consumption = max(0, curr_val - prev_val)
            else:
                consumption = 0
        else:
            prev_rec = await crud.get_latest_reading_before(apartment_id, mt, first_day)
            prev_val = prev_rec.value if prev_rec else init_val
            reps = reps_by_mt.get(mt, [])
            if reps:
                replaced = True
                total_cons = 0
                has_any_base = prev_val is not None
                if prev_val is not None:
                    total_cons += max(0, reps[0].old_final_value - prev_val)
                for idx in range(1, len(reps)):
                    total_cons += max(0, reps[idx].old_final_value - reps[idx - 1].new_initial_value)
                    has_any_base = True

                last_rep = reps[-1]
                mt_rows = readings_by_mt.get(mt, [])
                latest_after = None
                for r in mt_rows:
                    if getattr(r, "is_replacement_init", False):
                        continue
                    if last_rep.init_reading_id is not None:
                        if r.id > last_rep.init_reading_id:
                            latest_after = r
                            break
                    elif r.reading_date >= last_rep.replacement_date:
                        latest_after = r
                        break

                if latest_after is not None:
                    total_cons += max(0, latest_after.value - last_rep.new_initial_value)
                    curr_val = latest_after.value
                    has_any_base = True
                else:
                    curr_val = last_rep.new_initial_value

                consumption = total_cons
                submitted = True
                has_base = has_any_base
                if prev_val is None:
                    prev_val = last_rep.new_initial_value
            else:
                curr_val = latest_in_month.get(mt)
                submitted = curr_val is not None
                has_base = prev_val is not None
                if submitted and has_base and curr_val is not None and prev_val is not None:
                    consumption = max(0, curr_val - prev_val)
                else:
                    consumption = 0

        if submitted and has_base:
            if is_irrigation_after_cold:
                cost = 0
            else:
                cost = _round_to_rubles(Decimal(consumption) * rate) if (has_tariff and rate is not None) else 0
        else:
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
            replaced=replaced,
        )

    water_in_res = [res.meters[m] for m in WATER_METERS if m in res.meters]
    sew_obj = tariffs.get(SEWERAGE_TARIFF)
    sew_rate = Decimal(sew_obj.rate) if sew_obj is not None else None
    if water_in_res and sew_rate is not None and sew_rate > 0:
        sew_submitted = any(m.submitted for m in water_in_res)
        sew_has_base = any(m.submitted and m.has_base for m in water_in_res)
        if sew_has_base:
            if (
                irrigation_mode == IRRIGATION_MODE_AFTER_COLD
                and "irrigation" in res.meters
                and res.meters["irrigation"].submitted
                and res.meters["irrigation"].has_base
            ):
                sew_consumption = max(0, res.water_consumption_total - res.irrigation_consumption_total)
            else:
                sew_consumption = res.water_consumption_total
            sew_cost = _round_to_rubles(Decimal(sew_consumption) * sew_rate)
        else:
            sew_consumption = 0
            sew_cost = 0
        res.sewerage = MeterResult(
            meter_type=SEWERAGE_TARIFF,
            previous=None,
            current=None,
            consumption=sew_consumption,
            tariff=sew_rate,
            cost=sew_cost,
            has_base=sew_has_base,
            has_tariff=True,
            submitted=sew_submitted,
        )

    return res


def format_reading_notification(
    apartment_number: str,
    meter_type: str,
    prev_val: int | None,
    new_val: int,
) -> str:
    label = "⚡ Электричество" if meter_type == "t" else METER_LABELS.get(meter_type, meter_type)
    unit = METER_UNITS.get(meter_type, "")
    if prev_val is None:
        return f"{label}: <b>{fmt_num(new_val)}</b> {unit} (начальное показание не задано)"
    diff = max(0, new_val - prev_val)
    return f"{label}: {fmt_num(prev_val)} → <b>{fmt_num(new_val)}</b> {unit} (расход: {fmt_num(diff)} {unit})"

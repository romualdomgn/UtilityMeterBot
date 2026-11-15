from decimal import Decimal
import pytest
from database import calculate_consumption_and_cost, parse_decimal
from scheduler import is_reminder_due


def test_standard_monthly_calculation_and_rounding():
    prev = {
        "hvs": Decimal("100.000"),
        "gvs": Decimal("50.000"),
        "t1": Decimal("1200.00"),
        "t2": Decimal("800.00"),
        "t3": Decimal("400.00"),
    }
    curr = {
        "hvs": Decimal("105.350"),  # расход 5.35 м³
        "gvs": Decimal("53.120"),   # расход 3.12 м³
        "t1": Decimal("1285.40"),   # расход 85.40 кВт·ч
        "t2": Decimal("842.10"),    # расход 42.10 кВт·ч
        "t3": Decimal("419.50"),    # расход 19.50 кВт·ч
    }
    tariffs = {
        "hvs": Decimal("42.50"),
        "gvs": Decimal("198.40"),
        "t1": Decimal("5.65"),
        "t2": Decimal("2.85"),
        "t3": Decimal("4.70"),
    }

    res = calculate_consumption_and_cost(prev, curr, tariffs)

    assert res.decreased_fields == []
    assert res.diff_hvs == Decimal("5.350")
    assert res.diff_gvs == Decimal("3.120")
    assert res.diff_water_total == Decimal("8.470")

    assert res.diff_t1 == Decimal("85.400")
    assert res.diff_t2 == Decimal("42.100")
    assert res.diff_t3 == Decimal("19.500")
    assert res.diff_elec_total == Decimal("147.000")

    # Проверка стоимости с округлением до целых рублей (ROUND_HALF_UP):
    # ХВС: 5.35 * 42.50 = 227.375 -> 227 ₽
    # ГВС: 3.12 * 198.40 = 619.008 -> 619 ₽
    assert res.cost_hvs == Decimal("227")
    assert res.cost_gvs == Decimal("619")
    assert res.cost_water_total == Decimal("846")

    # Т1: 85.40 * 5.65 = 482.51 -> 483 ₽
    # Т2: 42.10 * 2.85 = 119.985 -> 120 ₽
    # Т3: 19.50 * 4.70 = 91.65 -> 92 ₽
    assert res.cost_t1 == Decimal("483")
    assert res.cost_t2 == Decimal("120")
    assert res.cost_t3 == Decimal("92")
    assert res.cost_elec_total == Decimal("695")

    # Итого: 846 + 695 = 1541 ₽
    assert res.cost_total == Decimal("1541")


def test_meter_replacement_decrease_detection():
    prev = {"hvs": Decimal("250.000"), "gvs": Decimal("10.000"), "t1": Decimal("0"), "t2": Decimal("0"), "t3": Decimal("0")}
    curr = {"hvs": Decimal("3.500"), "gvs": Decimal("12.000"), "t1": Decimal("0"), "t2": Decimal("0"), "t3": Decimal("0")}
    tariffs = {"hvs": Decimal("40.00"), "gvs": Decimal("150.00"), "t1": Decimal("5"), "t2": Decimal("3"), "t3": Decimal("4")}

    res_unconfirmed = calculate_consumption_and_cost(prev, curr, tariffs, allow_decrease=False)
    assert "ХВС" in res_unconfirmed.decreased_fields

    res_confirmed = calculate_consumption_and_cost(prev, curr, tariffs, allow_decrease=True)
    assert res_confirmed.diff_hvs == Decimal("3.500")
    assert res_confirmed.cost_hvs == Decimal("140")


def test_parse_decimal_comma_and_negative():
    assert parse_decimal("42,50", "0.01") == Decimal("42.50")
    assert parse_decimal(" 105.350 ", "0.001") == Decimal("105.350")
    with pytest.raises(ValueError):
        parse_decimal("-5.00", "0.01")


def test_reminder_schedule_logic():
    # День подачи 20-е число:
    assert is_reminder_due(20, 20) == (True, False)   # В сам день подачи
    assert is_reminder_due(21, 20) == (False, False)  # На следующий день — тишина
    assert is_reminder_due(23, 20) == (True, True)    # Через 3 дня — повтор о просрочке
    assert is_reminder_due(26, 20) == (True, True)    # Через 6 дней — повтор о просрочке
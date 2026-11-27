"""Tests for property types, gas meter, custom meters, profile/family editing, and API endpoints."""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import config
from database import crud, operations
from database.models import Base, get_apartment_enabled_meters
from database.session import _run_sqlite_migrations, attach_sqlite_pragmas
from services import calculations, scheduler
from webapp.auth import WebAppUser, validate_init_data
from webapp.routers import apartments as apt_router
from webapp.routers import families as fam_router
from webapp.routers import readings as read_router
from webapp.routers import tenants as ten_router
from webapp.schemas import (
    ApartmentCreateBody,
    ApartmentReorderBody,
    ApartmentSettingsBody,
    ElectricityReadingBody,
    FamilyOwnerUpdateBody,
    GasReadingBody,
    ProfileUpdateBody,
    TenantCreateBody,
    WaterReadingBody,
)


class DummyBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_message(self, **kwargs) -> None:
        self.sent.append(kwargs)


def make_request(bot: DummyBot | None = None):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(bot=bot or DummyBot())))


@pytest_asyncio.fixture
async def temp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_features.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}", future=True)
    attach_sqlite_pragmas(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_run_sqlite_migrations)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(crud, "async_session_factory", factory)
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 100)
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123456:TEST_TOKEN")
    yield db_file
    await engine.dispose()


def _sign_init_data(payload: dict[str, str], token: str) -> str:
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(payload.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    full = dict(payload)
    full["hash"] = digest
    return urlencode(full)


def test_validate_init_data_valid_and_invalid(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "999:SECRET")
    monkeypatch.setattr(config, "INIT_DATA_MAX_AGE_SECONDS", 3600)

    good = _sign_init_data(
        {"auth_date": str(int(time.time())), "user": json.dumps({"id": 42, "first_name": "Roman"})},
        "999:SECRET",
    )
    fields = validate_init_data(good)
    assert json.loads(fields["user"])["id"] == 42

    bad_sig = _sign_init_data(
        {"auth_date": str(int(time.time())), "user": json.dumps({"id": 42})},
        "999:WRONG",
    )
    with pytest.raises(HTTPException) as exc_sig:
        validate_init_data(bad_sig)
    assert exc_sig.value.status_code == 401

    expired = _sign_init_data(
        {"auth_date": str(int(time.time()) - 7200), "user": json.dumps({"id": 42})},
        "999:SECRET",
    )
    with pytest.raises(HTTPException) as exc_exp:
        validate_init_data(expired)
    assert exc_exp.value.status_code == 401


@pytest.mark.asyncio
async def test_sqlite_migration_adds_new_columns_to_legacy_table(tmp_path):
    db_file = tmp_path / "legacy.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}", future=True)
    attach_sqlite_pragmas(engine)
    async with engine.begin() as conn:
        await conn.execute(text(
            "CREATE TABLE apartments ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "number VARCHAR(50) NOT NULL, "
            "family_id INTEGER NOT NULL, "
            "has_tenant BOOLEAN NOT NULL DEFAULT 0, "
            "water_submission_day INTEGER, "
            "electricity_submission_day INTEGER, "
            "created_at DATETIME NOT NULL)"
        ))
        await conn.run_sync(_run_sqlite_migrations)
        rows = (await conn.execute(text("PRAGMA table_info(apartments)"))).fetchall()
        cols = {r[1] for r in rows}
    await engine.dispose()
    assert {"property_type", "enabled_meters", "gas_submission_day", "sort_order", "irrigation_mode"}.issubset(cols)


@pytest.mark.asyncio
async def test_property_types_custom_meters_gas_and_partial_readings(temp_db):
    admin = WebAppUser(telegram_id=100, role="admin", family_id=None, name="Admin")
    bot = DummyBot()
    req = make_request(bot)

    res = await apt_router.api_create_apartment(
        ApartmentCreateBody(
            number="Лесная, 12",
            property_type="house",
            enabled_meters=["cold_water", "t1", "gas"],
        ),
        user=admin,
    )
    apt_id = res["apartment"]["id"]
    assert res["apartment"]["property_type"] == "house"
    assert res["apartment"]["display_name"] == "Дом Лесная, 12"
    assert res["apartment"]["enabled_meters"] == ["cold_water", "t1", "gas"]

    await operations.update_initial_readings_atomic(apt_id, {"cold_water": 100, "t1": 500, "gas": 200}, 100)
    upd1 = await apt_router.api_update_settings(
        apt_id,
        ApartmentSettingsBody(
            water_submission_day=20,
            electricity_submission_day=22,
            gas_submission_day=25,
            tariffs={"cold_water": "50", "t1": "5", "gas": "8.5"},
        ),
        user=admin,
    )
    assert upd1["updated_tariffs"] == 3

    upd2 = await apt_router.api_update_settings(
        apt_id,
        ApartmentSettingsBody(
            water_submission_day=21,
            electricity_submission_day=22,
            gas_submission_day=25,
            tariffs={"cold_water": "50", "t1": "5", "gas": "8.5"},
        ),
        user=admin,
    )
    assert upd2["updated_tariffs"] == 0
    assert len(await crud.get_tariff_history(apt_id)) == 3

    await read_router.api_submit_water(apt_id, WaterReadingBody(cold_water=110), req, user=admin)
    with pytest.raises(HTTPException) as exc_dis:
        await read_router.api_submit_water(apt_id, WaterReadingBody(hot_water=10), req, user=admin)
    assert exc_dis.value.status_code == 400

    await read_router.api_submit_electricity(apt_id, ElectricityReadingBody(t1=520), req, user=admin)
    await read_router.api_submit_gas(apt_id, GasReadingBody(gas=210), req, user=admin)

    bill = await calculations.calculate_billing(apt_id)
    assert set(bill.meters.keys()) == {"cold_water", "t1", "gas"}
    assert bill.water_total == 500
    assert bill.electricity_total == 100
    assert bill.gas_consumption_total == 10
    assert bill.gas_total == 85
    assert bill.grand_total == 685


@pytest.mark.asyncio
async def test_gas_reminder_and_enabled_meters_in_scheduler(temp_db):
    fam = await crud.create_family("F")
    await crud.get_or_create_user(201, name="Владелец", role="landlord", family_id=fam.id)
    apt = await crud.create_apartment("Уч. 5", fam.id, property_type="dacha", enabled_meters=["t1", "gas"])
    await crud.update_apartment(apt.id, gas_submission_day=25, water_submission_day=25)

    bot = DummyBot()
    await scheduler.check_meter_reminders(bot, today=datetime.date(2026, 11, 25))
    assert len(bot.sent) == 1
    assert "газового счётчика" in bot.sent[0]["text"]
    assert "Дача Уч. 5" in bot.sent[0]["text"]


@pytest.mark.asyncio
async def test_reattach_existing_tenant_to_second_property(temp_db):
    fam = await crud.create_family("Family")
    apt1 = await crud.create_apartment("1", fam.id)
    apt2 = await crud.create_apartment("2", fam.id)
    admin = WebAppUser(telegram_id=100, role="admin", family_id=None, name="Admin")
    req = make_request()

    await ten_router.api_add_tenant(
        apt1.id,
        TenantCreateBody(telegram_id=555, name="Иван", force_no_contract=True),
        req,
        user=admin,
    )
    res2 = await ten_router.api_add_tenant(
        apt2.id,
        TenantCreateBody(telegram_id=555, name="Иван", force_no_contract=True),
        req,
        user=admin,
    )
    assert res2["ok"] is True

    with pytest.raises(HTTPException) as exc_dup:
        await ten_router.api_add_tenant(
            apt2.id,
            TenantCreateBody(telegram_id=555, name="Иван", force_no_contract=True),
            req,
            user=admin,
        )
    assert exc_dup.value.status_code == 400


@pytest.mark.asyncio
async def test_family_member_rename_and_self_rename(temp_db):
    fam = await crud.create_family("Семья")
    await crud.get_or_create_user(201, name="Старое имя", role="landlord", family_id=fam.id)
    await crud.get_or_create_user(301, name="Арендатор", role="tenant", family_id=fam.id)

    admin = WebAppUser(telegram_id=100, role="admin", family_id=None, name="Admin")
    landlord = WebAppUser(telegram_id=201, role="landlord", family_id=fam.id, name="Старое имя")
    tenant = WebAppUser(telegram_id=301, role="tenant", family_id=fam.id, name="Арендатор")

    await fam_router.api_update_family_owner(
        fam.id,
        201,
        FamilyOwnerUpdateBody(name="Новое имя владельца"),
        user=admin,
    )
    u201 = await crud.get_user_by_telegram_id(201)
    assert u201.name == "Новое имя владельца"

    await apt_router.api_update_own_profile(ProfileUpdateBody(name="Алексей"), user=landlord)
    assert (await crud.get_user_by_telegram_id(201)).name == "Алексей"

    await apt_router.api_update_own_profile(ProfileUpdateBody(name="Галина"), user=tenant)
    assert (await crud.get_user_by_telegram_id(301)).name == "Галина"


@pytest.mark.asyncio
async def test_update_apartment_whitelist_and_delete_family_admin_unlink(temp_db):
    fam = await crud.create_family("Fam")
    apt = await crud.create_apartment("10", fam.id)
    await crud.update_apartment(apt.id, id=999, family_id=999, number="11")
    reloaded = await crud.get_apartment(apt.id)
    assert reloaded.id == apt.id
    assert reloaded.family_id == fam.id
    assert reloaded.number == "11"
    assert get_apartment_enabled_meters(reloaded) == ["cold_water", "hot_water", "t1", "t2", "t3"]

    await crud.get_or_create_user(100, name="Admin", role="admin", family_id=fam.id)
    await operations.delete_family_atomic(fam.id, actor_tg_id=100)
    admin_row = await crud.get_user_by_telegram_id(100)
    assert admin_row is not None
    assert admin_row.family_id is None


@pytest.mark.asyncio
async def test_delete_apartment_cascade_tenant_cleanup_and_permissions(temp_db):
    fam_a = await crud.create_family("Family A")
    fam_b = await crud.create_family("Family B")
    apt_a1 = await crud.create_apartment("101", fam_a.id, property_type="apartment")
    apt_a2 = await crud.create_apartment("102", fam_a.id, property_type="house", enabled_meters=["cold_water", "t1", "gas"])
    apt_b1 = await crud.create_apartment("201", fam_b.id, property_type="dacha")

    await crud.get_or_create_user(201, name="Owner A", role="landlord", family_id=fam_a.id)
    landlord_a = WebAppUser(telegram_id=201, role="landlord", family_id=fam_a.id, name="Owner A")
    admin = WebAppUser(telegram_id=100, role="admin", family_id=None, name="Admin")

    bot = DummyBot()
    req = make_request(bot)

    # Add readings, tariffs, initial readings to apt_a1
    await operations.update_initial_readings_atomic(apt_a1.id, {"cold_water": 10}, 100)
    await operations.update_apartment_settings_atomic(
        apt_a1.id, 20, 20, {"cold_water": __import__("decimal").Decimal("50")}, 100, datetime.date(2026, 11, 1)
    )
    await operations.save_meter_group_readings(
        apt_a1.id, {"cold_water": 20}, 100, datetime.date(2026, 11, 20), "submit_water", "test"
    )

    # Tenant 1 linked ONLY to apt_a1; Tenant 2 linked to BOTH apt_a1 and apt_a2
    u_t1, _ = await crud.get_or_create_user(301, name="Only A1", role="tenant", family_id=fam_a.id)
    u_t2, _ = await crud.get_or_create_user(302, name="Both A1 A2", role="tenant", family_id=fam_a.id)
    await crud.link_user_to_apartment(apt_a1.id, u_t1.id, role="tenant")
    await crud.link_user_to_apartment(apt_a1.id, u_t2.id, role="tenant")
    await crud.link_user_to_apartment(apt_a2.id, u_t2.id, role="tenant")

    tenant_user = WebAppUser(telegram_id=301, role="tenant", family_id=fam_a.id, name="Only A1")

    # Tenant cannot delete a property (403)
    with pytest.raises(HTTPException) as exc_tenant:
        await apt_router.api_delete_apartment(apt_a1.id, req, user=tenant_user)
    assert exc_tenant.value.status_code == 403

    # Landlord A cannot delete Family B's property (403)
    with pytest.raises(HTTPException) as exc_cross:
        await apt_router.api_delete_apartment(apt_b1.id, req, user=landlord_a)
    assert exc_cross.value.status_code == 403

    # Detail includes readings_count for landlord
    detail = await apt_router.api_apartment_detail(apt_a1.id, user=landlord_a)
    assert detail["readings_count"] == 1

    # Landlord A deletes apt_a1
    res = await apt_router.api_delete_apartment(apt_a1.id, req, user=landlord_a)
    assert res["ok"] is True
    assert res["affected_tenants"] == 2
    assert await crud.get_apartment(apt_a1.id) is None
    assert await crud.get_readings_count(apt_a1.id) == 0
    assert await crud.get_tariff_history(apt_a1.id) == []

    # Tenant 1 (only apt_a1) must be fully removed; Tenant 2 (also apt_a2) must remain
    assert await crud.get_user_by_telegram_id(301) is None
    assert await crud.get_user_by_telegram_id(302) is not None
    assert len(bot.sent) == 2

    # Deleting already deleted property returns 404 for admin
    with pytest.raises(HTTPException) as exc_404:
        await apt_router.api_delete_apartment(apt_a1.id, req, user=admin)
    assert exc_404.value.status_code == 404


@pytest.mark.asyncio
async def test_reorder_apartments_persistence_and_permissions(temp_db):
    fam_a = await crud.create_family("Family A")
    fam_b = await crud.create_family("Family B")
    a1 = await crud.create_apartment("75", fam_a.id)
    a2 = await crud.create_apartment("188", fam_a.id, property_type="house")
    a3 = await crud.create_apartment("17", fam_a.id)
    b1 = await crud.create_apartment("99", fam_b.id)

    await crud.get_or_create_user(201, name="Owner A", role="landlord", family_id=fam_a.id)
    await crud.get_or_create_user(301, name="Tenant A", role="tenant", family_id=fam_a.id)
    landlord_a = WebAppUser(telegram_id=201, role="landlord", family_id=fam_a.id, name="Owner A")
    tenant_a = WebAppUser(telegram_id=301, role="tenant", family_id=fam_a.id, name="Tenant A")
    admin = WebAppUser(telegram_id=100, role="admin", family_id=None, name="Admin")

    # Default order follows creation sort_order
    initial = await crud.get_apartments_by_family(fam_a.id)
    assert [a.id for a in initial] == [a1.id, a2.id, a3.id]

    # Tenant cannot reorder (403)
    with pytest.raises(HTTPException) as exc_t:
        await apt_router.api_reorder_apartments(
            ApartmentReorderBody(apartment_ids=[a3.id, a1.id, a2.id]), user=tenant_a
        )
    assert exc_t.value.status_code == 403

    # Landlord A cannot include Family B's property (403)
    with pytest.raises(HTTPException) as exc_cross:
        await apt_router.api_reorder_apartments(
            ApartmentReorderBody(apartment_ids=[a1.id, b1.id]), user=landlord_a
        )
    assert exc_cross.value.status_code == 403

    # Landlord A reorders their properties: [17, 75, 188]
    res = await apt_router.api_reorder_apartments(
        ApartmentReorderBody(apartment_ids=[a3.id, a1.id, a2.id]), user=landlord_a
    )
    assert res["ok"] is True
    assert res["apartment_ids"] == [a3.id, a1.id, a2.id]

    reordered = await crud.get_apartments_by_family(fam_a.id)
    assert [a.id for a in reordered] == [a3.id, a1.id, a2.id]

    # New property added afterwards appends to the end
    a4 = await crud.create_apartment("371", fam_a.id)
    after_add = await crud.get_apartments_by_family(fam_a.id)
    assert [a.id for a in after_add] == [a3.id, a1.id, a2.id, a4.id]

    # Admin can reorder across all properties
    res_adm = await apt_router.api_reorder_apartments(
        ApartmentReorderBody(apartment_ids=[a4.id, b1.id, a1.id, a2.id, a3.id]), user=admin
    )
    assert res_adm["apartment_ids"] == [a4.id, b1.id, a1.id, a2.id, a3.id]
    all_apts = await crud.get_all_apartments()
    assert [a.id for a in all_apts] == [a4.id, b1.id, a1.id, a2.id, a3.id]


def test_index_html_injects_asset_version_hashes():
    from webapp.api import render_index_html

    html = render_index_html()
    assert '/style.css?v=' in html
    assert '/app.js?v=' in html


def test_webapp_url_versioning_and_arenda_label():
    from pathlib import Path
    from services.notifications import build_webapp_url

    url_root = build_webapp_url()
    assert "?v=" in url_root
    url_tab = build_webapp_url("water")
    assert "?v=" in url_tab and "&tab=water" in url_tab

    app_js = (Path(__file__).resolve().parent.parent / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    assert "📋 Аренда" in app_js
    assert "📋 Договор" not in app_js


@pytest.mark.asyncio
async def test_contract_date_dd_mm_yyyy_parsing_and_update(temp_db):
    from webapp.schemas import ContractBody, parse_iso_date

    assert parse_iso_date("25/11/2026") == datetime.date(2026, 11, 25)
    assert parse_iso_date("25.11.2026") == datetime.date(2026, 11, 25)
    assert parse_iso_date("2026-11-25") == datetime.date(2026, 11, 25)
    assert parse_iso_date("") is None

    with pytest.raises(HTTPException) as exc_bad:
        parse_iso_date("31/02/2026")
    assert exc_bad.value.status_code == 400

    fam = await crud.create_family("F")
    apt = await crud.create_apartment("371", fam.id)
    admin = WebAppUser(telegram_id=100, role="admin", family_id=None, name="Admin")

    res = await ten_router.api_update_contract(
        apt.id,
        ContractBody(
            has_tenant=True,
            agreement_no="1",
            agreement_date_start="25/11/2026",
            agreement_date_finish="25/12/2027",
        ),
        user=admin,
    )
    assert res["ok"] is True

    updated = await crud.get_apartment(apt.id)
    assert updated.agreement_date_start == datetime.date(2026, 11, 25)
    assert updated.agreement_date_finish == datetime.date(2027, 12, 25)

    with pytest.raises(HTTPException) as exc_inv:
        await ten_router.api_update_contract(
            apt.id,
            ContractBody(
                agreement_date_start="26/12/2027",
                agreement_date_finish="25/12/2027",
            ),
            user=admin,
        )
    assert exc_inv.value.status_code == 400


@pytest.mark.asyncio
async def test_single_tariff_electricity_mutual_exclusion_billing_and_scheduler(temp_db):
    from pathlib import Path
    from webapp.schemas import InitialReadingsBody

    admin = WebAppUser(telegram_id=100, role="admin", family_id=None, name="Admin")
    bot = DummyBot()
    req = make_request(bot)

    # Reject combining single tariff "t" with T1/T2/T3
    with pytest.raises(HTTPException) as exc_mix:
        await apt_router.api_create_apartment(
            ApartmentCreateBody(
                number="55",
                property_type="apartment",
                enabled_meters=["cold_water", "t", "t1"],
            ),
            user=admin,
        )
    assert exc_mix.value.status_code == 400

    # Create property with single electricity tariff "t"
    res = await apt_router.api_create_apartment(
        ApartmentCreateBody(
            number="55",
            property_type="apartment",
            enabled_meters=["cold_water", "hot_water", "t", "gas"],
        ),
        user=admin,
    )
    apt_id = res["apartment"]["id"]
    assert res["apartment"]["enabled_meters"] == ["cold_water", "hot_water", "t", "gas"]

    # Reject updating settings to mix "t" and "t2"
    with pytest.raises(HTTPException) as exc_mix_upd:
        await apt_router.api_update_settings(
            apt_id,
            ApartmentSettingsBody(enabled_meters=["t", "t2"]),
            user=admin,
        )
    assert exc_mix_upd.value.status_code == 400

    # Set initial reading & tariff for "t"
    await apt_router.api_update_initial_readings(
        apt_id,
        InitialReadingsBody(t=1000),
        user=admin,
    )
    await apt_router.api_update_settings(
        apt_id,
        ApartmentSettingsBody(
            electricity_submission_day=24,
            tariffs={"t": "5.5"},
        ),
        user=admin,
    )

    # Scheduler reminds landlord about single-tariff electricity meter
    apt_obj = await crud.get_apartment(apt_id)
    await crud.get_or_create_user(202, name="Владелец", role="landlord", family_id=apt_obj.family_id)
    await scheduler.check_meter_reminders(bot, today=datetime.date(2026, 11, 24))
    assert any("электрического счётчика" in msg["text"] for msg in bot.sent)

    # Submitting T1 when only "t" is enabled must fail
    with pytest.raises(HTTPException) as exc_t1:
        await read_router.api_submit_electricity(apt_id, ElectricityReadingBody(t1=1050), req, user=admin)
    assert exc_t1.value.status_code == 400

    # Submit single-tariff reading "t" and verify billing
    sub = await read_router.api_submit_electricity(apt_id, ElectricityReadingBody(t=1100), req, user=admin)
    assert sub["ok"] is True

    bill = await calculations.calculate_billing(apt_id)
    assert "t" in bill.meters
    assert "t1" not in bill.meters
    assert bill.electricity_consumption_total == 100
    assert bill.electricity_total == 550

    # Verify UI strings & labels in app.js
    app_js = (Path(__file__).resolve().parent.parent / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    assert "Один тариф" in app_js
    assert "Тарифы Т1, Т2, Т3" in app_js
    assert "Выберите день отправки показаний счётчиков (с 1 по 31 число)" in app_js
    assert "руб/кВт·ч" in app_js
    assert '📋 Аренда${isPropertyRented(apt) ? " ✅" : ""}' in app_js


def test_tenant_dashboard_and_photo_ui_updates():
    from pathlib import Path

    app_js = (Path(__file__).resolve().parent.parent / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    # Tenant nav button renamed from "Табло" to "Обзор"
    assert 'label: "Табло"' not in app_js
    # Tenant photo button & resource chips
    assert "📤 Отправить фото наймодателю" in app_js
    assert "getPhotoResourceOptions" in app_js
    assert 'label: "ХВС"' in app_js
    assert 'label: "ГВС"' in app_js
    assert 'label: "Электричество"' in app_js
    assert 'label: "Газ"' in app_js
    # Tenant contract format & consumption-only dashboard
    assert "formatTenantContractLine" in app_js
    assert "Договор ${numPart} от ${fmtDate(apt.agreement_date_start)} до ${fmtDate(apt.agreement_date_finish)}" in app_js
    assert "billing.water_consumption_total" in app_js
    assert "billing.electricity_consumption_total" in app_js
    assert "billing.gas_consumption_total" in app_js


@pytest.mark.asyncio
async def test_sewerage_tariff_equals_cold_plus_hot_water_and_dashboard_ui(temp_db):
    from pathlib import Path
    from webapp.schemas import InitialReadingsBody

    admin = WebAppUser(telegram_id=100, role="admin", family_id=None, name="Admin")
    bot = DummyBot()
    req = make_request(bot)

    res = await apt_router.api_create_apartment(
        ApartmentCreateBody(
            number="371",
            property_type="apartment",
            enabled_meters=["cold_water", "hot_water", "t"],
        ),
        user=admin,
    )
    apt_id = res["apartment"]["id"]

    await apt_router.api_update_initial_readings(
        apt_id,
        InitialReadingsBody(cold_water=100, hot_water=50, t=1000),
        user=admin,
    )
    upd = await apt_router.api_update_settings(
        apt_id,
        ApartmentSettingsBody(
            tariffs={"cold_water": "40", "hot_water": "150", "sewerage": "35", "t": "5"},
        ),
        user=admin,
    )
    assert upd["updated_tariffs"] == 4

    detail = await apt_router.api_apartment_detail(apt_id, user=admin)
    assert detail["tariffs"]["sewerage"] == 35.0

    await read_router.api_submit_water(apt_id, WaterReadingBody(cold_water=107, hot_water=53), req, user=admin)
    await read_router.api_submit_electricity(apt_id, ElectricityReadingBody(t=1020), req, user=admin)

    bill = await calculations.calculate_billing(apt_id)
    # ХВС = 7 м³ * 40 = 280 ₽, ГВС = 3 м³ * 150 = 450 ₽ => water_total = 730 ₽
    assert bill.water_consumption_total == 10
    assert bill.water_total == 730
    # Водоотведение = ХВС + ГВС = 10 м³ * 35 = 350 ₽
    assert bill.sewerage is not None
    assert bill.sewerage_consumption_total == 10
    assert bill.sewerage_total == 350
    # Свет = 20 * 5 = 100 ₽ => grand_total = 730 + 350 + 100 = 1180 ₽
    assert bill.electricity_total == 100
    assert bill.grand_total == 1180

    # Zeroing sewerage disables sewerage billing
    await apt_router.api_update_settings(
        apt_id,
        ApartmentSettingsBody(tariffs={"sewerage": "0"}),
        user=admin,
    )
    bill_no_sew = await calculations.calculate_billing(apt_id)
    assert bill_no_sew.sewerage is None
    assert bill_no_sew.sewerage_total == 0
    assert bill_no_sew.grand_total == 830

    # Tariff history shows Russian label for sewerage
    hist = await apt_router.api_tariff_history(apt_id, user=admin)
    assert any(i["tariff_type"] == "sewerage" and "Водоотведение" in i["label"] for i in hist["items"])

    # UI checks: sewerage input, dashboard chips, tenant 👤 in dropdown
    app_js = (Path(__file__).resolve().parent.parent / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'id="t-sewerage"' in app_js
    assert 'id="dash-apt-chips-list"' in app_js
    assert 'propShort(a))}${!isTenant && isPropertyRented(a) ? " 👤" : ""}</option>' in app_js


@pytest.mark.asyncio
async def test_house_irrigation_excluded_from_sewerage_and_rented_status_logic(temp_db):
    from pathlib import Path
    from webapp.schemas import ContractBody, InitialReadingsBody

    admin = WebAppUser(telegram_id=100, role="admin", family_id=None, name="Admin")
    bot = DummyBot()
    req = make_request(bot)

    # Irrigation is NOT allowed for apartments or dachas
    with pytest.raises(HTTPException) as exc_apt_irr:
        await apt_router.api_create_apartment(
            ApartmentCreateBody(
                number="12",
                property_type="apartment",
                enabled_meters=["cold_water", "irrigation"],
            ),
            user=admin,
        )
    assert exc_apt_irr.value.status_code == 400

    # Create a House with ХВС, ГВС, Полив, and single electricity tariff Т
    res = await apt_router.api_create_apartment(
        ApartmentCreateBody(
            number="188",
            property_type="house",
            enabled_meters=["cold_water", "hot_water", "irrigation", "t"],
        ),
        user=admin,
    )
    apt_id = res["apartment"]["id"]
    assert res["apartment"]["is_rented"] is False
    assert "irrigation" in res["apartment"]["enabled_meters"]

    # Set initial readings and tariffs (including sewerage and irrigation)
    await apt_router.api_update_initial_readings(
        apt_id,
        InitialReadingsBody(cold_water=100, hot_water=50, irrigation=200, t=1000),
        user=admin,
    )
    await apt_router.api_update_settings(
        apt_id,
        ApartmentSettingsBody(
            tariffs={
                "cold_water": "40",
                "hot_water": "150",
                "sewerage": "30",
                "irrigation": "25",
                "t": "5",
            },
        ),
        user=admin,
    )

    # Submit readings: ХВС +5 (200 ₽), ГВС +3 (450 ₽), Полив +20 (500 ₽), Т +100 (500 ₽)
    await read_router.api_submit_water(
        apt_id,
        WaterReadingBody(cold_water=105, hot_water=53, irrigation=220),
        req,
        user=admin,
    )
    await read_router.api_submit_electricity(apt_id, ElectricityReadingBody(t=1100), req, user=admin)

    bill = await calculations.calculate_billing(apt_id)
    # Domestic water = 5 + 3 = 8 m3 -> 650 ₽
    assert bill.water_consumption_total == 8
    assert bill.water_total == 650
    # Sewerage MUST equal ХВС + ГВС = 8 m3 (irrigation 20 m3 is EXCLUDED!) -> 8 * 30 = 240 ₽
    assert bill.sewerage is not None
    assert bill.sewerage_consumption_total == 8
    assert bill.sewerage_total == 240
    # Irrigation = 20 m3 * 25 = 500 ₽
    assert bill.irrigation_consumption_total == 20
    assert bill.irrigation_total == 500
    # Electricity = 100 * 5 = 500 ₽ -> grand total = 650 + 240 + 500 + 500 = 1890 ₽
    assert bill.grand_total == 1890

    # Rented status logic: contract alone (even with has_tenant=False) marks property as rented
    await ten_router.api_update_contract(
        apt_id,
        ContractBody(has_tenant=False, agreement_no="42", agreement_date_start="01/11/2026"),
        user=admin,
    )
    det_contract = await apt_router.api_apartment_detail(apt_id, user=admin)
    assert det_contract["apartment"]["is_rented"] is True

    # Removing contract leaves property unrented (no tenant and no contract)
    await ten_router.api_delete_contract(apt_id, request=req, unlink_tenants=False, user=admin)
    det_free = await apt_router.api_apartment_detail(apt_id, user=admin)
    assert det_free["apartment"]["is_rented"] is False

    # Adding tenant (even without contract) marks property as rented
    await ten_router.api_add_tenant(
        apt_id,
        TenantCreateBody(telegram_id=777, name="Жилец", force_no_contract=True),
        req,
        user=admin,
    )
    det_tenant = await apt_router.api_apartment_detail(apt_id, user=admin)
    assert det_tenant["apartment"]["is_rented"] is True

    # Number formatting & UI checks
    assert calculations.fmt_num(17653) == "17 653"
    base_dir = Path(__file__).resolve().parent.parent
    index_html = (base_dir / "webapp" / "static" / "index.html").read_text(encoding="utf-8")
    assert "Счётчики</h1>" not in index_html
    app_js = (base_dir / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    assert "function fmtNum(" in app_js
    assert "🚰 Водоотведение</div>" in app_js
    assert "⚡ Электричество</div>" in app_js
    assert "🚰 Водоотвед." not in app_js
    assert ">⚡ Свет<" not in app_js
    assert '"Свободен"' not in app_js


@pytest.mark.asyncio
async def test_irrigation_after_cold_vs_parallel_billing_and_ui(temp_db):
    from pathlib import Path
    from webapp.schemas import InitialReadingsBody

    admin = WebAppUser(telegram_id=100, role="admin", family_id=None, name="Admin")
    bot = DummyBot()
    req = make_request(bot)

    # Create a house with irrigation AFTER cold water meter (series sub-meter)
    res = await apt_router.api_create_apartment(
        ApartmentCreateBody(
            number="205",
            property_type="house",
            enabled_meters=["cold_water", "irrigation", "t"],
            irrigation_mode="after_cold",
        ),
        user=admin,
    )
    apt_id = res["apartment"]["id"]
    assert res["apartment"]["irrigation_mode"] == "after_cold"

    await apt_router.api_update_initial_readings(
        apt_id,
        InitialReadingsBody(cold_water=100, irrigation=50, t=1000),
        user=admin,
    )
    # Even if an irrigation tariff is passed in after_cold mode, backend ignores it
    upd = await apt_router.api_update_settings(
        apt_id,
        ApartmentSettingsBody(
            irrigation_mode="after_cold",
            tariffs={"cold_water": "40", "sewerage": "30", "irrigation": "999", "t": "5"},
        ),
        user=admin,
    )
    assert upd["updated_tariffs"] == 3

    # ХВС: 100 -> 130 (30 м³), Полив: 50 -> 60 (10 м³), Т: 1000 -> 1020 (20 кВт·ч)
    await read_router.api_submit_water(
        apt_id,
        WaterReadingBody(cold_water=130, irrigation=60),
        req,
        user=admin,
    )
    await read_router.api_submit_electricity(apt_id, ElectricityReadingBody(t=1020), req, user=admin)

    bill_after = await calculations.calculate_billing(apt_id)
    assert bill_after.irrigation_mode == "after_cold"
    # ХВС = 30 * 40 = 1200 ₽ (already includes 10 m³ irrigation water)
    assert bill_after.water_consumption_total == 30
    assert bill_after.water_total == 1200
    # Полив is NOT double-charged: cost = 0, consumption = 10
    assert bill_after.irrigation_consumption_total == 10
    assert bill_after.irrigation_total == 0
    # Водоотведение = ХВС - Полив = 30 - 10 = 20 м³ * 30 = 600 ₽
    assert bill_after.sewerage is not None
    assert bill_after.sewerage_consumption_total == 20
    assert bill_after.sewerage_total == 600
    # Grand total = 1200 + 600 + 100 = 1900 ₽
    assert bill_after.grand_total == 1900

    # Clamp sewerage to 0 if irrigation exceeds ХВС
    await read_router.api_submit_water(apt_id, WaterReadingBody(irrigation=95, force=True), req, user=admin)
    bill_clamped = await calculations.calculate_billing(apt_id)
    assert bill_clamped.sewerage_consumption_total == 0
    assert bill_clamped.sewerage_total == 0

    # Switch same house to "parallel" mode with separate irrigation tariff 25 ₽/м³
    await apt_router.api_update_settings(
        apt_id,
        ApartmentSettingsBody(
            irrigation_mode="parallel",
            tariffs={"irrigation": "25"},
        ),
        user=admin,
    )
    bill_parallel = await calculations.calculate_billing(apt_id)
    assert bill_parallel.irrigation_mode == "parallel"
    # ХВС = 30 м³ -> 1200 ₽; Водоотведение = 30 м³ * 30 = 900 ₽; Полив = 45 м³ * 25 = 1125 ₽; Т = 100 ₽
    assert bill_parallel.sewerage_consumption_total == 30
    assert bill_parallel.sewerage_total == 900
    assert bill_parallel.irrigation_consumption_total == 45
    assert bill_parallel.irrigation_total == 1125
    assert bill_parallel.grand_total == 1200 + 900 + 1125 + 100

    app_js = (Path(__file__).resolve().parent.parent / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    assert "Поливной водомер после водомера ХВС" in app_js
    assert "Поливной водомер параллельно водомеру ХВС" in app_js


def test_reading_inputs_use_nbsp_thousands_mask():
    from pathlib import Path

    app_js = (Path(__file__).resolve().parent.parent / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    assert "function formatIntSpaces(" in app_js
    assert "function parseIntSpaces(" in app_js
    assert "function attachIntMask(" in app_js
    assert 'class="int-mask" id="in-${m.id}"' in app_js
    assert 'class="int-mask" id="i-${m.id}"' in app_js
    assert 'class="int-mask" id="corr-val"' in app_js

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
from database.session import _run_sqlite_migrations
from services import calculations, scheduler
from webapp.auth import WebAppUser, validate_init_data
from webapp.routers import apartments as apt_router
from webapp.routers import families as fam_router
from webapp.routers import readings as read_router
from webapp.routers import tenants as ten_router
from webapp.schemas import (
    ApartmentCreateBody,
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
    assert {"property_type", "enabled_meters", "gas_submission_day"}.issubset(cols)


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
        TenantCreateBody(telegram_id=555, name="Иван", phone="+7-912-345-6789", force_no_contract=True),
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
        FamilyOwnerUpdateBody(name="Новое имя владельца", phone="+7-999-111-2233"),
        user=admin,
    )
    u201 = await crud.get_user_by_telegram_id(201)
    assert u201.name == "Новое имя владельца"
    assert u201.phone == "+7-999-111-2233"

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

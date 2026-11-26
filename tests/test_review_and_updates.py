"""Regression tests for code review fixes (HIGH/MEDIUM/LOW) and new product features."""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import hmac
import io
import json
import os
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import text

import config
from database import crud, operations
import bot_handlers
from services import access, backup, calculations, notifications, scheduler
from webapp.auth import WebAppUser, validate_init_data
from webapp.routers import admin as admin_router
from webapp.routers import apartments as apt_router
from webapp.routers import families as fam_router
from webapp.routers import readings as read_router
from webapp.routers import tenants as ten_router
from webapp.schemas import (
    ApartmentCreateBody,
    ApartmentSettingsBody,
    ContractBody,
    FamilyBody,
    FamilyOwnerBody,
    MeterDeviceItemBody,
    MeterDevicesUpdateBody,
    MeterReplacementBody,
    MeterReplacementTariffEntry,
    TenantCreateBody,
    TenantUpdateBody,
    WaterReadingBody,
)


class DummyBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.photos: list[dict] = []
        self.documents: list[dict] = []

    async def send_message(self, **kwargs) -> None:
        self.sent.append(kwargs)

    async def send_photo(self, **kwargs) -> None:
        self.photos.append(kwargs)

    async def send_document(self, **kwargs) -> None:
        self.documents.append(kwargs)


class DummyUploadFile:
    def __init__(self, content: bytes, filename: str = "photo.jpg") -> None:
        self._buf = io.BytesIO(content)
        self.filename = filename

    async def read(self, size: int = -1) -> bytes:
        return self._buf.read(size)


def make_request(bot: DummyBot | None = None):
    return SimpleNamespace(
        headers={},
        app=SimpleNamespace(state=SimpleNamespace(bot=bot or DummyBot(), bg_tasks=None)),
    )


def _sign_init_data(payload: dict[str, str], token: str) -> str:
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(payload.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    full = dict(payload)
    full["hash"] = digest
    return urlencode(full)


@pytest.mark.asyncio
async def test_medium_15_sqlite_pragmas_enabled_in_tests(temp_db):
    async with crud.UnitOfWork() as s:
        fk = (await s.execute(text("PRAGMA foreign_keys"))).scalar()
        jm = (await s.execute(text("PRAGMA journal_mode"))).scalar()
        bt = (await s.execute(text("PRAGMA busy_timeout"))).scalar()
    assert fk == 1
    assert str(jm).lower() == "wal"
    assert int(bt) >= 1000


def test_low_01_rejects_future_init_data(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "999:SECRET")
    future = _sign_init_data(
        {"auth_date": str(int(time.time()) + 3600), "user": json.dumps({"id": 42})},
        "999:SECRET",
    )
    with pytest.raises(HTTPException) as exc:
        validate_init_data(future)
    assert exc.value.status_code == 401


def test_medium_03_strict_integer_bounds_reject_overflow_and_bools():
    with pytest.raises(ValidationError):
        WaterReadingBody(cold_water=10**20)
    with pytest.raises(ValidationError):
        WaterReadingBody(cold_water=True)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        WaterReadingBody(cold_water=-1)
    with pytest.raises(ValidationError):
        TenantCreateBody(telegram_id=10**20, name="Bad")


@pytest.mark.asyncio
async def test_high_07_and_medium_02_idempotency_repeat_and_decrease(temp_db):
    fam = await crud.create_family("F")
    apt = await crud.create_apartment("1", fam.id, enabled_meters=["cold_water"])
    await operations.update_initial_readings_atomic(apt.id, {"cold_water": 100}, 100)
    admin = WebAppUser(telegram_id=100, role="admin", family_id=None, name="Admin")
    req = make_request()

    # First submission with submission_id
    r1 = await read_router.api_submit_water(
        apt.id, WaterReadingBody(cold_water=120, submission_id="sub-abc-1"), req, user=admin
    )
    assert r1["ok"] is True and r1["deduplicated"] is False

    # Exact retry returns deduplicated=True and does not duplicate rows
    r2 = await read_router.api_submit_water(
        apt.id, WaterReadingBody(cold_water=120, submission_id="sub-abc-1"), req, user=admin
    )
    assert r2["ok"] is True and r2["deduplicated"] is True
    assert await crud.get_readings_count(apt.id) == 1

    # Reusing same submission_id with different value -> HTTP 409
    with pytest.raises(HTTPException) as exc_conflict:
        await read_router.api_submit_water(
            apt.id, WaterReadingBody(cold_water=125, submission_id="sub-abc-1"), req, user=admin
        )
    assert exc_conflict.value.status_code == 409

    # Repeat in same month with higher value -> needs_confirm
    r_rep = await read_router.api_submit_water(
        apt.id, WaterReadingBody(cold_water=130, submission_id="sub-abc-2"), req, user=admin
    )
    assert r_rep["ok"] is False and r_rep["needs_confirm"] is True

    # Confirmed repeat saves cleanly
    r_rep_ok = await read_router.api_submit_water(
        apt.id, WaterReadingBody(cold_water=130, force=True, submission_id="sub-abc-2"), req, user=admin
    )
    assert r_rep_ok["ok"] is True and r_rep_ok["deduplicated"] is False
    assert await crud.get_readings_count(apt.id) == 2

    # Decreasing value without allow_decrease shows replacement + correction options for admin/landlord
    r_dec = await read_router.api_submit_water(
        apt.id, WaterReadingBody(cold_water=125, force=True, submission_id="sub-abc-3"), req, user=admin
    )
    assert r_dec["ok"] is False
    assert r_dec["rejected_decrease"] is True
    assert r_dec["can_open_meters"] is True
    assert r_dec["can_force_decrease"] is True
    assert r_dec["warning"] == (
        "Вы ввели значение меньше, чем уже зафиксировано.\n"
        "Если это корректировка ошибочно введённого значения, то нажмите «Сохранить».\n"
        "Если заменён счётчик, то перейдите в раздел «Недвижимость» -> «Настройки» "
        "и в разделе «Управление счётчиками» нажмите «Замена счётчика»."
    )

    # Confirming correction with allow_decrease=True saves the lower value for admin/landlord
    r_corr = await read_router.api_submit_water(
        apt.id,
        WaterReadingBody(cold_water=125, force=True, allow_decrease=True, submission_id="sub-abc-3"),
        req,
        user=admin,
    )
    assert r_corr["ok"] is True and r_corr["deduplicated"] is False
    latest_cw = await crud.get_latest_reading(apt.id, "cold_water")
    assert latest_cw is not None and latest_cw.value == 125
    logs = await crud.get_audit_logs(limit=5)
    assert any("(корректировка)" in (l.details or "") for l in logs)

    # Tenant CANNOT save a decreased reading even if crafting allow_decrease=True
    u_t, _ = await crud.get_or_create_user(399, name="Жилец", role="tenant", family_id=fam.id)
    await crud.link_user_to_apartment(apt.id, u_t.id, role="tenant")
    tenant = WebAppUser(telegram_id=399, role="tenant", family_id=fam.id, name="Жилец")
    r_ten_dec = await read_router.api_submit_water(
        apt.id,
        WaterReadingBody(cold_water=110, force=True, allow_decrease=True, submission_id="sub-ten-1"),
        req,
        user=tenant,
    )
    assert r_ten_dec["ok"] is False
    assert r_ten_dec["rejected_decrease"] is True
    assert r_ten_dec["can_open_meters"] is False
    assert r_ten_dec["can_force_decrease"] is False
    assert (await crud.get_latest_reading(apt.id, "cold_water")).value == 125


@pytest.mark.asyncio
async def test_high_02_meter_replacement_preserves_mid_month_consumption(temp_db, monkeypatch):
    fam = await crud.create_family("F")
    apt = await crud.create_apartment("12", fam.id, enabled_meters=["cold_water"])
    admin = WebAppUser(telegram_id=100, role="admin", family_id=None, name="Admin")
    req = make_request()

    await operations.update_initial_readings_atomic(apt.id, {"cold_water": 100}, 100)
    await crud.set_tariff(apt.id, "cold_water", Decimal("50"), datetime.date(2026, 1, 1), 100)
    await operations.save_meter_group_readings(
        apt.id, {"cold_water": 100}, 100, datetime.date(2026, 10, 31), "submit_water", "oct"
    )

    monkeypatch.setattr(config, "local_today", lambda: datetime.date(2026, 11, 15))
    monkeypatch.setattr(calculations, "local_today", lambda: datetime.date(2026, 11, 15))
    monkeypatch.setattr(apt_router, "local_today", lambda: datetime.date(2026, 11, 15))
    monkeypatch.setattr(read_router, "local_today", lambda: datetime.date(2026, 11, 15))

    # Replace meter: old reached 120 (+20), new starts at 5
    rep = await apt_router.api_replace_meter(
        apt.id,
        MeterReplacementBody(
            meter_type="cold_water",
            old_final_value=120,
            new_initial_value=5,
            new_serial_number="SN-999",
        ),
        user=admin,
    )
    assert rep["ok"] is True

    # Submit new meter reading 15 (+10) -> total month consumption MUST be 20 + 10 = 30 m3 (1500 rub)
    sub = await read_router.api_submit_water(
        apt.id, WaterReadingBody(cold_water=15, force=False), req, user=admin
    )
    assert sub["ok"] is True
    bill = await calculations.calculate_billing(apt.id, year=2026, month=11)
    assert bill.meters["cold_water"].consumption == 30
    assert bill.meters["cold_water"].cost == 1500
    assert bill.meters["cold_water"].replaced is True


@pytest.mark.asyncio
async def test_high_03_concurrent_cross_family_tenant_isolation(temp_db):
    fam_a = await crud.create_family("Family A")
    fam_b = await crud.create_family("Family B")
    apt_a = await crud.create_apartment("1", fam_a.id)
    apt_b = await crud.create_apartment("2", fam_b.id)
    await crud.get_or_create_user(201, name="Owner A", role="landlord", family_id=fam_a.id)
    await crud.get_or_create_user(202, name="Owner B", role="landlord", family_id=fam_b.id)

    u_a = WebAppUser(telegram_id=201, role="landlord", family_id=fam_a.id, name="Owner A")
    u_b = WebAppUser(telegram_id=202, role="landlord", family_id=fam_b.id, name="Owner B")
    req = make_request()

    results = await asyncio.gather(
        ten_router.api_add_tenant(
            apt_a.id, TenantCreateBody(telegram_id=777, name="T", force_no_contract=True), req, user=u_a
        ),
        ten_router.api_add_tenant(
            apt_b.id, TenantCreateBody(telegram_id=777, name="T", force_no_contract=True), req, user=u_b
        ),
        return_exceptions=True,
    )
    ok_count = sum(1 for r in results if isinstance(r, dict) and r.get("ok"))
    conflict_count = sum(1 for r in results if isinstance(r, HTTPException) and r.status_code == 409)
    assert ok_count == 1
    assert conflict_count == 1


@pytest.mark.asyncio
async def test_medium_01_tenant_cannot_see_tariffs_or_billing_money(temp_db):
    fam = await crud.create_family("F")
    apt = await crud.create_apartment("10", fam.id, enabled_meters=["cold_water", "t"])
    u_t, _ = await crud.get_or_create_user(301, name="Жилец", role="tenant", family_id=fam.id)
    await crud.link_user_to_apartment(apt.id, u_t.id, role="tenant")
    await operations.update_initial_readings_atomic(apt.id, {"cold_water": 100, "t": 500}, 100)
    await crud.set_tariff(apt.id, "cold_water", Decimal("45"), datetime.date(2026, 1, 1), 100)

    tenant = WebAppUser(telegram_id=301, role="tenant", family_id=fam.id, name="Жилец")
    detail = await apt_router.api_apartment_detail(apt.id, user=tenant)
    assert "tariffs" not in detail
    # Tenant Dashboard now mirrors owner Dashboard with ruble totals
    assert "water_total" in detail["billing"]
    assert "grand_total" in detail["billing"]
    assert "cost" in detail["billing"]["meters"]["cold_water"]

    hist = await read_router.api_history(apt.id, year=None, month=None, user=tenant)
    assert "grand_total" not in hist["billing"]

    with pytest.raises(HTTPException) as exc:
        await apt_router.api_tariff_history(apt.id, user=tenant)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_admin_as_family_owner_and_notification_routing(temp_db):
    fam1 = await crud.create_family("Семья 1")
    fam2 = await crud.create_family("Семья 2")
    apt1 = await crud.create_apartment("101", fam1.id)
    apt2 = await crud.create_apartment("202", fam2.id)
    await crud.sync_admin_user(100)
    await crud.get_or_create_user(201, name="Владелец 1", role="landlord", family_id=fam1.id)
    await crud.get_or_create_user(202, name="Владелец 2", role="landlord", family_id=fam2.id)

    # By default admin (100) does NOT receive notifications for fam1 or fam2
    assert await access.get_notification_recipients(apt1.id) == [201]
    assert await access.get_notification_recipients(apt2.id) == [202]

    # Admin can be added as owner to fam1
    admin = WebAppUser(telegram_id=100, role="admin", family_id=None, name="Администратор")
    res = await fam_router.api_add_family_owner(
        fam1.id, FamilyOwnerBody(telegram_id=100, name="Роман"), user=admin
    )
    assert res["ok"] is True
    assert set(await access.get_notification_recipients(apt1.id)) == {201, 100}
    assert await access.get_notification_recipients(apt2.id) == [202]

    # Removing admin from fam1 detaches FamilyOwner link without deleting admin User
    await fam_router.api_delete_family_owner(fam1.id, 100, user=admin)
    assert await access.get_notification_recipients(apt1.id) == [201]
    assert await crud.get_user_by_telegram_id(100) is not None


@pytest.mark.asyncio
async def test_high_04_delete_contract_options_and_future_start_access(temp_db, monkeypatch):
    monkeypatch.setattr(config, "local_today", lambda: datetime.date(2026, 11, 25))
    fam = await crud.create_family("F")
    apt = await crud.create_apartment("5", fam.id)
    await crud.update_apartment(
        apt.id,
        agreement_no="77",
        agreement_date_start=datetime.date(2026, 12, 1),  # future start
        agreement_date_finish=datetime.date(2027, 12, 1),
    )
    u_t, _ = await crud.get_or_create_user(305, name="Арендатор", role="tenant", family_id=fam.id)
    await crud.link_user_to_apartment(apt.id, u_t.id, role="tenant")

    # Tenant has access regardless of future agreement_date_start
    assert await access.user_can_access_apartment(305, apt.id) is True

    admin = WebAppUser(telegram_id=100, role="admin", family_id=None, name="Admin")
    req = make_request()

    # Option 1: delete contract only -> tenant remains linked and keeps access
    await ten_router.api_delete_contract(apt.id, request=req, unlink_tenants=False, user=admin)
    assert await access.user_can_access_apartment(305, apt.id) is True

    # Option 2: delete contract AND unlink tenants -> tenant removed
    await ten_router.api_delete_contract(apt.id, request=req, unlink_tenants=True, user=admin)
    assert await crud.get_user_by_telegram_id(305) is None


@pytest.mark.asyncio
async def test_meter_verification_reminders_and_lease_expiration_text(temp_db):
    fam = await crud.create_family("F")
    apt = await crud.create_apartment("371", fam.id, enabled_meters=["cold_water"])
    await crud.get_or_create_user(201, name="Владелец", role="landlord", family_id=fam.id)
    await crud.update_apartment(
        apt.id,
        agreement_no="123",
        agreement_date_start=datetime.date(2025, 11, 25),
        agreement_date_finish=datetime.date(2026, 11, 25),
    )

    admin = WebAppUser(telegram_id=100, role="admin", family_id=None, name="Admin")
    await apt_router.api_update_meter_devices(
        apt.id,
        MeterDevicesUpdateBody(
            items=[
                MeterDeviceItemBody(
                    meter_type="cold_water",
                    serial_number="CW-001",
                    verification_due_date="25.05.2027",
                    notify_intervals=["6m", "3m", "1m", "1w"],
                )
            ]
        ),
        user=admin,
    )

    bot = DummyBot()
    # Exactly 6 months before 2027-05-25 is 2026-11-25
    await scheduler.check_meter_verifications(bot, today=datetime.date(2026, 11, 25))
    assert len(bot.sent) == 1
    assert bot.sent[0]["chat_id"] == 201
    assert "через полгода" in bot.sent[0]["text"]
    assert "CW-001" in bot.sent[0]["text"]

    # Lease expiration exact text on end day
    bot2 = DummyBot()
    await scheduler.check_lease_expirations(bot2, today=datetime.date(2026, 11, 25))
    assert len(bot2.sent) == 1
    assert "заканчивается сегодня" in bot2.sent[0]["text"]
    assert "доступ к боту у нанимателя будет прекращён" in bot2.sent[0]["text"]


@pytest.mark.asyncio
async def test_backup_skip_if_unchanged_and_60_day_retention(temp_db, tmp_path, monkeypatch):
    bdir = tmp_path / "backups"
    bdir.mkdir()
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite+aiosqlite:///{temp_db}")
    monkeypatch.setattr(config, "BACKUP_DIR", bdir)
    monkeypatch.setattr(config, "BACKUP_RETENTION_DAYS", 60)

    p1 = await backup.create_backup(skip_if_unchanged=True)
    assert p1 is not None and p1.exists()

    # Without DB changes, second backup is skipped
    p2 = await backup.create_backup(skip_if_unchanged=True)
    assert p2 is None

    # Create an old backup (65 days ago) and touch DB -> new backup created & old one rotated
    old_b = bdir / "utility_meter_backup_20260101_000000_old123.db"
    old_b.write_bytes(b"old")
    old_t = time.time() - 65 * 86400
    os.utime(old_b, (old_t, old_t))

    future_t = time.time() + 5
    os.utime(temp_db, (future_t, future_t))
    p3 = await backup.create_backup(skip_if_unchanged=True)
    assert p3 is not None and p3.exists()
    assert not old_b.exists()


@pytest.mark.asyncio
async def test_medium_04_05_14_and_low_04_05_validations(temp_db):
    fam = await crud.create_family("F")
    apt = await crud.create_apartment("1", fam.id, property_type="house", enabled_meters=["cold_water", "irrigation"])
    admin = WebAppUser(telegram_id=100, role="admin", family_id=None, name="Admin")
    bot = DummyBot()
    req = make_request(bot)

    # MEDIUM-14: after_cold without cold_water is rejected
    with pytest.raises(HTTPException) as exc_irr:
        await apt_router.api_create_apartment(
            ApartmentCreateBody(
                number="Дом 2",
                property_type="house",
                enabled_meters=["irrigation", "t"],
                irrigation_mode="after_cold",
            ),
            user=admin,
        )
    assert exc_irr.value.status_code == 400

    # MEDIUM-04: invalid history period returns 400 instead of 500
    with pytest.raises(HTTPException) as exc_h1:
        await read_router.api_history(apt.id, year=9999, month=12, user=admin)
    assert exc_h1.value.status_code == 400
    with pytest.raises(HTTPException) as exc_h2:
        await read_router.api_history(apt.id, year=2026, month=None, user=admin)
    assert exc_h2.value.status_code == 400

    # MEDIUM-05: fake image rejected; real JPEG forwarded without saving to PHOTOS_DIR
    fake = DummyUploadFile(b"not an image", "evil.jpg")
    with pytest.raises(HTTPException) as exc_img:
        await read_router.api_upload_photo(apt.id, req, file=fake, comment="", user=admin)
    assert exc_img.value.status_code == 400

    # LOW-05: renaming nonexistent family returns 404
    with pytest.raises(HTTPException) as exc_fam:
        await fam_router.api_rename_family(999999, FamilyBody(name="Ghost"), user=admin)
    assert exc_fam.value.status_code == 404

    # LOW-04: future month reading does not satisfy current month check
    await operations.save_meter_group_readings(
        apt.id, {"cold_water": 50}, 100, datetime.date(2026, 12, 5), "submit_water", "dec"
    )
    assert await crud.has_readings_this_month(apt.id, ["cold_water"], today=datetime.date(2026, 11, 25)) is False


@pytest.mark.asyncio
async def test_people_section_tenants_edit_and_overdue_indicator(temp_db, monkeypatch):
    monkeypatch.setattr(config, "local_today", lambda: datetime.date(2026, 11, 26))
    fam = await crud.create_family("Семья")
    apt = await crud.create_apartment("77", fam.id, enabled_meters=["cold_water"])
    await crud.update_apartment(apt.id, water_submission_day=25)
    await crud.get_or_create_user(201, name="Владелец", role="landlord", family_id=fam.id)
    u_t, _ = await crud.get_or_create_user(301, name="Иван", role="tenant", family_id=fam.id)
    await crud.link_user_to_apartment(apt.id, u_t.id, role="tenant")

    landlord = WebAppUser(telegram_id=201, role="landlord", family_id=fam.id, name="Владелец")
    me = await apt_router.api_me(user=landlord)
    assert me["apartments"][0]["has_overdue_meters"] is True

    people = await fam_router.api_families(user=landlord)
    assert len(people["tenants"]) == 1
    assert people["tenants"][0]["telegram_id"] == 301

    upd = await ten_router.api_update_tenant(
        301, TenantUpdateBody(name="Иван Петров", phone="+7-999-000-1122"), user=landlord
    )
    assert upd["name"] == "Иван Петров"
    assert upd["phone"] == "+7-999-000-1122"


@pytest.mark.asyncio
async def test_minor_update_unified_electricity_device_partial_settings_and_batch_replace(temp_db, monkeypatch):
    fam = await crud.create_family("F")
    apt = await crud.create_apartment(
        "42",
        fam.id,
        property_type="house",
        enabled_meters=["cold_water", "hot_water", "t1", "t2", "t3"],
    )
    await crud.get_or_create_user(201, name="Владелец", role="landlord", family_id=fam.id)
    admin = WebAppUser(telegram_id=100, role="admin", family_id=None, name="Admin")

    # 1. Partial settings update preserves previously saved submission days
    await apt_router.api_update_settings(
        apt.id,
        ApartmentSettingsBody(water_submission_day=20, electricity_submission_day=25),
        user=admin,
    )
    await apt_router.api_update_settings(
        apt.id,
        ApartmentSettingsBody(tariffs={"t1": "6", "t2": "3", "t3": "4"}),
        user=admin,
    )
    reloaded = await crud.get_apartment(apt.id)
    assert reloaded.water_submission_day == 20
    assert reloaded.electricity_submission_day == 25

    # 2. T1/T2/T3 collapse into one physical electricity meter device ("t")
    meters_resp = await apt_router.api_get_meter_devices(apt.id, user=admin)
    dev_types = [d["meter_type"] for d in meters_resp["devices"]]
    assert dev_types == ["cold_water", "hot_water", "t"]

    # Save serial & verification date for unified electricity device ("t")
    await apt_router.api_update_meter_devices(
        apt.id,
        MeterDevicesUpdateBody(
            items=[
                MeterDeviceItemBody(
                    meter_type="t",
                    serial_number="EL-777",
                    verification_due_date="25.02.2027",
                    notify_intervals=["3m"],
                )
            ]
        ),
        user=admin,
    )
    bot = DummyBot()
    await scheduler.check_meter_verifications(bot, today=datetime.date(2026, 11, 25))
    assert len(bot.sent) == 1
    assert "Электрический счётчик" in bot.sent[0]["text"]
    assert "EL-777" in bot.sent[0]["text"]

    # 3. Multi-tariff electricity meter replacement preserves T1/T2/T3 residuals
    await operations.update_initial_readings_atomic(apt.id, {"t1": 100, "t2": 200, "t3": 300}, 100)
    monkeypatch.setattr(config, "local_today", lambda: datetime.date(2026, 11, 20))
    monkeypatch.setattr(calculations, "local_today", lambda: datetime.date(2026, 11, 20))
    monkeypatch.setattr(apt_router, "local_today", lambda: datetime.date(2026, 11, 20))
    monkeypatch.setattr(read_router, "local_today", lambda: datetime.date(2026, 11, 20))

    rep = await apt_router.api_replace_meter(
        apt.id,
        MeterReplacementBody(
            meter_type="t",
            tariff_entries=[
                MeterReplacementTariffEntry(meter_type="t1", old_final_value=110, new_initial_value=0),
                MeterReplacementTariffEntry(meter_type="t2", old_final_value=220, new_initial_value=0),
                MeterReplacementTariffEntry(meter_type="t3", old_final_value=305, new_initial_value=0),
            ],
            new_serial_number="EL-888",
        ),
        user=admin,
    )
    assert rep["ok"] is True
    bill = await calculations.calculate_billing(apt.id, year=2026, month=11)
    assert bill.meters["t1"].consumption == 10
    assert bill.meters["t2"].consumption == 20
    assert bill.meters["t3"].consumption == 5
    assert bill.electricity_consumption_total == 35



@pytest.mark.asyncio
async def test_medium_09_startup_catchup_runs_once_without_kwarg_collision(temp_db, tmp_path, monkeypatch):
    bdir = tmp_path / "backups_catchup"
    bdir.mkdir()
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite+aiosqlite:///{temp_db}")
    monkeypatch.setattr(config, "BACKUP_DIR", bdir)

    bot = DummyBot()
    now_after_15 = datetime.datetime(2026, 11, 25, 16, 35, tzinfo=config.BOT_TZ)
    executed1 = await scheduler.run_missed_jobs_on_startup(bot, now=now_after_15)
    assert set(executed1) == {"daily_backup", "meter_reminders", "lease_expirations", "meter_verifications", "rent_payment_reminders"}

    # Second startup on the same day must not duplicate jobs
    executed2 = await scheduler.run_missed_jobs_on_startup(bot, now=now_after_15)
    assert executed2 == []


@pytest.mark.asyncio
async def test_contract_over_one_year_warning(temp_db):
    fam = await crud.create_family("F")
    apt = await crud.create_apartment("101", fam.id)
    admin = WebAppUser(telegram_id=100, role="admin", family_id=None, name="Admin")

    # 11 months -> no warning
    r11 = await ten_router.api_update_contract(
        apt.id,
        ContractBody(agreement_no="1", agreement_date_start="01.01.2026", agreement_date_finish="01.12.2026"),
        user=admin,
    )
    assert r11["ok"] is True
    assert "long_term_warning" not in r11

    # Exactly 1 year (12 months) -> no warning
    r12 = await ten_router.api_update_contract(
        apt.id,
        ContractBody(agreement_no="2", agreement_date_start="01.01.2026", agreement_date_finish="01.01.2027"),
        user=admin,
    )
    assert r12["ok"] is True
    assert "long_term_warning" not in r12

    # More than 1 year -> warning returned with Rosreestr notice
    r13 = await ten_router.api_update_contract(
        apt.id,
        ContractBody(agreement_no="3", agreement_date_start="01.01.2026", agreement_date_finish="02.01.2027"),
        user=admin,
    )
    assert r13["ok"] is True
    assert "long_term_warning" in r13
    assert "Росреестре" in r13["long_term_warning"]
    assert "не более 11 месяцев" in r13["long_term_warning"]

    # Leap-year boundary: 29/02/2024 -> 28/02/2025 is 1 year (no warning), 01/03/2025 is > 1 year
    assert ten_router.is_contract_over_one_year(datetime.date(2024, 2, 29), datetime.date(2025, 2, 28)) is False
    assert ten_router.is_contract_over_one_year(datetime.date(2024, 2, 29), datetime.date(2025, 3, 1)) is True


def test_about_app_keyboard_and_clickable_links():
    kb = notifications.webapp_open_kb("📱 Открыть Mini App", include_about=True)
    assert len(kb.inline_keyboard) == 2
    assert kb.inline_keyboard[0][0].text == "📱 Открыть Mini App"
    assert kb.inline_keyboard[1][0].text == "ℹ️ О приложении"
    assert kb.inline_keyboard[1][0].callback_data == "about_app"

    text = bot_handlers.build_about_text(
        repository="https://github.com/roman/utility-meter-bot",
        tg_username="@roman_dev",
    )
    assert '<a href="https://github.com/roman/utility-meter-bot">https://github.com/roman/utility-meter-bot</a>' in text
    assert '<a href="https://t.me/roman_dev">@roman_dev</a>' in text
    assert "открытым исходным кодом" in text
    assert "UX/UI" in text


@pytest.mark.asyncio
async def test_api_me_exposes_app_info_from_config(temp_db, monkeypatch):
    monkeypatch.setattr(config, "REPOSITORY", "https://github.com/example/umb")
    monkeypatch.setattr(config, "TG_USERNAME", "@example_admin")
    admin = WebAppUser(telegram_id=100, role="admin", family_id=None, name="Admin")
    me = await apt_router.api_me(user=admin)
    assert me["app_info"]["repository"] == "https://github.com/example/umb"
    assert me["app_info"]["tg_username"] == "@example_admin"


@pytest.mark.asyncio
async def test_landlord_management_and_family_scoped_backup_isolation(temp_db, tmp_path, monkeypatch):
    import sqlite3 as _sqlite3

    exp_dir = tmp_path / "exports"
    exp_dir.mkdir()
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite+aiosqlite:///{temp_db}")
    monkeypatch.setattr(config, "EXPORT_DIR", exp_dir)

    fam_a = await crud.create_family("Семья Альфа")
    fam_b = await crud.create_family("Семья Бета СЕКРЕТ")
    apt_a = await crud.create_apartment("101", fam_a.id, enabled_meters=["cold_water"])
    apt_b = await crud.create_apartment("999_SECRET_APT", fam_b.id, enabled_meters=["cold_water"])

    await crud.get_or_create_user(201, name="Владелец А", role="landlord", family_id=fam_a.id)
    await crud.get_or_create_user(202, name="Владелец Б СЕКРЕТ", role="landlord", family_id=fam_b.id)
    u_ta, _ = await crud.get_or_create_user(301, name="Жилец А", role="tenant", family_id=fam_a.id)
    u_tb, _ = await crud.get_or_create_user(302, name="Жилец Б СЕКРЕТ", role="tenant", family_id=fam_b.id)
    await crud.link_user_to_apartment(apt_a.id, u_ta.id, role="tenant")
    await crud.link_user_to_apartment(apt_b.id, u_tb.id, role="tenant")

    await operations.save_meter_group_readings(
        apt_a.id, {"cold_water": 111}, 201, datetime.date(2026, 11, 20), "submit_water", "Alpha reading"
    )
    await operations.save_meter_group_readings(
        apt_b.id, {"cold_water": 999}, 202, datetime.date(2026, 11, 20), "submit_water", "Beta SECRET reading"
    )

    landlord_a = WebAppUser(telegram_id=201, role="landlord", family_id=fam_a.id, name="Владелец А")
    tenant_a = WebAppUser(telegram_id=301, role="tenant", family_id=fam_a.id, name="Жилец А")

    # Tenant is still blocked from all /api/admin/* endpoints
    with pytest.raises(HTTPException) as exc_tu:
        await admin_router.api_admin_users(user=tenant_a)
    assert exc_tu.value.status_code == 403
    with pytest.raises(HTTPException) as exc_tb:
        await admin_router.api_admin_backup(make_request(), user=tenant_a)
    assert exc_tb.value.status_code == 403

    # Landlord sees ONLY their family's users and audit logs
    users_res = await admin_router.api_admin_users(user=landlord_a)
    user_ids = {u["telegram_id"] for u in users_res["users"]}
    assert user_ids == {201, 301}

    audit_res = await admin_router.api_admin_audit(user=landlord_a)
    assert all(l["user_telegram_id"] in {201, 301} for l in audit_res["logs"])
    assert not any("Beta SECRET" in (l["details"] or "") for l in audit_res["logs"])

    # Family backup SQLite file contains ONLY Family A data and ZERO bytes from Family B
    fam_db_path = await backup.create_family_backup(fam_a.id)
    try:
        assert fam_db_path.exists()
        raw_bytes = fam_db_path.read_bytes()
        assert b"999_SECRET_APT" not in raw_bytes
        assert b"Beta SECRET" not in raw_bytes

        conn = _sqlite3.connect(str(fam_db_path))
        try:
            fams = conn.execute("SELECT id, name FROM families").fetchall()
            apts = conn.execute("SELECT id, number FROM apartments").fetchall()
            users = conn.execute("SELECT telegram_id FROM users").fetchall()
            readings = conn.execute("SELECT value FROM meter_readings").fetchall()
            fk_issues = conn.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            conn.close()

        assert fams == [(fam_a.id, "Семья Альфа")]
        assert apts == [(apt_a.id, "101")]
        assert {r[0] for r in users} == {201, 301}
        assert [r[0] for r in readings] == [111]
        assert fk_issues == []
    finally:
        fam_db_path.unlink(missing_ok=True)

    # Endpoint sends family backup and cleans up temp file afterwards
    bot = DummyBot()
    req = make_request(bot)
    res = await admin_router.api_admin_backup(req, user=landlord_a)
    assert res["ok"] is True
    assert res["scope"] == "family"
    assert len(bot.documents) == 1
    assert list(exp_dir.glob("*.db")) == []


@pytest.mark.asyncio
async def test_rent_payment_day_and_reminders_validation_and_scheduler(temp_db, monkeypatch):
    fam = await crud.create_family("Семья")
    apt = await crud.create_apartment("371", fam.id)
    await crud.get_or_create_user(201, name="Владелец", role="landlord", family_id=fam.id)
    u_t, _ = await crud.get_or_create_user(301, name="Наниматель", role="tenant", family_id=fam.id)
    await crud.link_user_to_apartment(apt.id, u_t.id, role="tenant")

    admin = WebAppUser(telegram_id=100, role="admin", family_id=None, name="Admin")

    # Validation: enabling reminder without payment day -> 400
    with pytest.raises(HTTPException) as exc_noday:
        await ten_router.api_update_contract(
            apt.id,
            ContractBody(
                agreement_no="10",
                rent_payment_day=None,
                rent_reminder_enabled=True,
                rent_reminder_mode="on_day",
            ),
            user=admin,
        )
    assert exc_noday.value.status_code == 400

    # Validation: after_days out of 1..20 -> 400
    with pytest.raises(HTTPException) as exc_rng:
        await ten_router.api_update_contract(
            apt.id,
            ContractBody(
                agreement_no="10",
                rent_payment_day=10,
                rent_reminder_enabled=True,
                rent_reminder_mode="after_days",
                rent_reminder_days_after=25,
            ),
            user=admin,
        )
    assert exc_rng.value.status_code == 400

    # 1) Mode: day_before (payment day = 1, check on Nov 30 -> reminds tenant & landlord about Dec 1)
    await ten_router.api_update_contract(
        apt.id,
        ContractBody(
            agreement_no="10",
            agreement_date_start="01.11.2026",
            agreement_date_finish="01.10.2027",
            rent_payment_day=1,
            rent_reminder_enabled=True,
            rent_reminder_mode="day_before",
        ),
        user=admin,
    )
    bot1 = DummyBot()
    monkeypatch.setattr(config, "local_today", lambda: datetime.date(2026, 11, 30))
    await scheduler.check_rent_payment_reminders(bot1, today=datetime.date(2026, 11, 30))
    assert {m["chat_id"] for m in bot1.sent} == {301, 201}
    assert "01.12.2026" in bot1.sent[0]["text"]
    assert "Квартиры №371" in bot1.sent[0]["text"]

    # 2) Mode: on_day (payment day = 31, short month Feb 2026 -> fires on Feb 28 for tenant & landlord)
    await ten_router.api_update_contract(
        apt.id,
        ContractBody(
            agreement_no="10",
            agreement_date_start="01.01.2026",
            agreement_date_finish="01.12.2026",
            rent_payment_day=31,
            rent_reminder_enabled=True,
            rent_reminder_mode="on_day",
        ),
        user=admin,
    )
    bot2 = DummyBot()
    monkeypatch.setattr(config, "local_today", lambda: datetime.date(2026, 2, 28))
    await scheduler.check_rent_payment_reminders(bot2, today=datetime.date(2026, 2, 28))
    assert {m["chat_id"] for m in bot2.sent} == {301, 201}
    assert "28.02.2026" in bot2.sent[0]["text"]

    # 3) Mode: after_days (payment day = 28, after 5 days -> fires on Dec 3 for BOTH tenant AND landlord)
    await ten_router.api_update_contract(
        apt.id,
        ContractBody(
            agreement_no="10",
            agreement_date_start="01.11.2026",
            agreement_date_finish="01.10.2027",
            rent_payment_day=28,
            rent_reminder_enabled=True,
            rent_reminder_mode="after_days",
            rent_reminder_days_after=5,
        ),
        user=admin,
    )
    bot3 = DummyBot()
    monkeypatch.setattr(config, "local_today", lambda: datetime.date(2026, 12, 3))
    await scheduler.check_rent_payment_reminders(bot3, today=datetime.date(2026, 12, 3))
    assert {m["chat_id"] for m in bot3.sent} == {301, 201}
    assert "ежемесячную плату за наём" in bot3.sent[0]["text"]
    assert "Квартиры №371" in bot3.sent[0]["text"]
    assert "28.11.2026" in bot3.sent[0]["text"]

    # Does NOT fire on non-matching day
    bot4 = DummyBot()
    await scheduler.check_rent_payment_reminders(bot4, today=datetime.date(2026, 12, 2))
    assert bot4.sent == []

    # Deleting contract resets payment day and reminder settings
    await ten_router.api_delete_contract(apt.id, request=make_request(), unlink_tenants=False, user=admin)
    reloaded = await crud.get_apartment(apt.id)
    assert reloaded.rent_payment_day is None
    assert reloaded.rent_reminder_enabled is False
    assert reloaded.rent_reminder_mode is None
    assert reloaded.rent_reminder_days_after is None

def test_ui_strings_and_removed_photo_storage_notice():
    import re as _re

    base_dir = Path(__file__).resolve().parent.parent
    app_js = (base_dir / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    style_css = (base_dir / "webapp" / "static" / "style.css").read_text(encoding="utf-8")

    assert "сохранено на сервере" not in app_js
    assert "Сохранить показания электроэнергии" in app_js
    assert "👤 Аренда" in app_js
    assert "👤 Сдан" not in app_js
    assert "🟡" in app_js
    assert "ХВС минус Полив" in app_js
    assert "ХВС − Полив" not in app_js
    assert 'ico: "👨‍👩‍👧‍👦", label: "Люди"' in app_js
    assert 'ico: "🌆", label: "Недвижимость"' in app_js
    assert "Зажмите и перетащите объект или нажмите ⇅" in app_js
    assert r"Удалить только договор\n(доступ нанимателя к боту сохранится)" in app_js
    assert "Удалить договор и открепить нанимателей" in app_js
    assert "Уведомить о поверке или замене счётчика" in app_js
    assert "Замена счётчика" in app_js
    assert "🔧 Управление счётчиками" in app_js
    assert "💧 Водомер холодной воды" in app_js
    assert "♨️ Водомер горячей воды" in app_js
    assert "⚡ Электрический счётчик" in app_js
    assert "🚰 Имеющиеся счётчики воды и газа" in app_js
    assert "📅 Дни подачи показаний" in app_js
    assert "💰 Тарифы" in app_js
    assert 'class="del-x">❌</span>' in app_js
    # No modal should have "Отмена" before primary action button
    assert not _re.findall(r'\[\s*\{\s*label:\s*"Отмена"', app_js)
    # Danger buttons are gray with red bold text
    assert ".btn-danger { background: var(--bg2); color: var(--danger);" in style_css
    assert "font-weight: 700;" in style_css
    assert "can_force_decrease" in app_js
    assert "allow_decrease" in app_js
    assert '<div class="mt-8 center"><button type="button" class="inline-link-btn" id="jump-to-meters-btn">' in app_js
    assert "LONG_TERM_CONTRACT_WARNING" in app_js
    assert "isContractOverOneYear" in app_js
    assert "Росреестре" in app_js
    assert "📏 Начальные<br>показания" in app_js
    assert "Объект сдаётся в аренду" in app_js
    assert 'id="c-rented"' in app_js
    assert 'id="c-has-contract"' in app_js
    assert 'id="rent-options-wrap"' in app_js
    assert 'id="rent-tenant-section"' in app_js
    assert 'id="rent-contract-section"' in app_js
    assert "align-items: center;" in style_css
    assert "ДД.ММ.ГГГГ" in app_js
    assert "ДД/ММ/ГГГГ" not in app_js
    assert "25.11.2026" in app_js
    assert "25/11/2026" not in app_js
    schemas_py = (base_dir / "webapp" / "schemas.py").read_text(encoding="utf-8")
    assert "ДД.ММ.ГГГГ" in schemas_py
    assert "ДД/ММ/ГГГГ" not in schemas_py
    # Dirty-state save buttons across modals (disabled initially and re-disabled on revert)
    assert 'id: "modal-save-meters"' in app_js
    assert "updateMetersDirty" in app_js
    assert 'id: "modal-replace-meter"' in app_js
    assert 'id: "modal-add-tenant"' in app_js
    assert 'id: "modal-add-owner"' in app_js
    assert 'id: "modal-create-fam"' in app_js
    assert 'id="btn-open-property-settings"' in app_js
    assert "🏠 Настройки объекта недвижимости" in app_js
    assert "function openPropertySettingsModal(" in app_js
    assert 'id: "modal-save-property-basic"' in app_js
    assert 'id="btn-admin-about"' in app_js
    assert 'id="btn-admin-help"' in app_js
    assert "📖 Как пользоваться приложением" in app_js
    assert "APP_GUIDE_TOPICS" in app_js
    assert "function openAboutAppModal(" in app_js
    assert "function openAppGuideModal(" in app_js
    assert "Выгрузка базы данных" in app_js
    assert "Быстрая выгрузка в чат" not in app_js
    assert "📊 Excel ${esc(fullLabel)}" in app_js
    assert "📄 PDF ${esc(fullLabel)}" in app_js
    # Tariffs button appears before Property Settings button in Settings tab
    assert app_js.index('id="btn-open-tariffs"') < app_js.index('id="btn-open-property-settings"')
    # Delete contract button is disabled when no saved contract exists
    assert 'id="btn-del-contract" ${initialHasContract ? "" : "disabled"}' in app_js
    # Landlord shares admin bottom nav (no separate History tab; Management tab present)
    assert 'if (r === "landlord")' not in app_js
    # Rent payment day & reminder controls
    assert 'id="c-pay-day"' in app_js
    assert 'id="c-pay-rem-en"' in app_js
    assert "Напоминание нанимателю об оплате" in app_js
    assert "Напомнить за день" in app_js
    assert "Напомнить в день оплаты" in app_js
    assert "Установить день напоминания об оплате" in app_js
    assert 'id="c-pay-after-days"' in app_js

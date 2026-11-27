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
    FamilyInviteBody,
    FamilyOwnerBody,
    FamilyOwnerUpdateBody,
    MeterDeviceItemBody,
    MeterDevicesUpdateBody,
    MeterReplacementBody,
    MeterReplacementTariffEntry,
    ProfileUpdateBody,
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

    await crud.create_family("Updated Family")
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
        301, TenantUpdateBody(name="Иван Петров"), user=landlord
    )
    assert upd["name"] == "Иван Петров"
    assert "phone" not in upd


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


@pytest.mark.asyncio
async def test_scoped_names_for_admin_landlord_and_tenant(temp_db, tmp_path, monkeypatch):
    from database.session import _run_sqlite_migrations
    from sqlalchemy.ext.asyncio import create_async_engine

    admin = WebAppUser(telegram_id=100, role="admin", family_id=None, name="Администратор")
    await crud.sync_admin_user(100)

    # Admin creates family, adds landlord and apartment, landlord adds tenant
    fam_res = await fam_router.api_create_family(FamilyBody(name="Семья от Админа"), user=admin)
    fam_id = fam_res["id"]
    await fam_router.api_add_family_owner(
        fam_id, FamilyOwnerBody(telegram_id=201, name="Владелец от Админа"), user=admin
    )
    apt = await crud.create_apartment("75", fam_id)

    landlord = WebAppUser(telegram_id=201, role="landlord", family_id=fam_id, name="Владелец от Админа")
    req = make_request()
    await ten_router.api_add_tenant(
        apt.id,
        TenantCreateBody(telegram_id=301, name="Наниматель от Владельца", force_no_contract=True),
        req,
        user=landlord,
    )
    tenant = WebAppUser(telegram_id=301, role="tenant", family_id=fam_id, name="Наниматель от Владельца")

    # 1. Landlord renames their OWN name and THEIR family's name
    await apt_router.api_update_own_profile(ProfileUpdateBody(name="Иван (своё имя)"), user=landlord)
    await fam_router.api_rename_family(fam_id, FamilyBody(name="Моя Семья"), user=landlord)

    # 2. Tenant renames their OWN name
    await apt_router.api_update_own_profile(ProfileUpdateBody(name="Сергей (своё имя)"), user=tenant)

    # 3. Admin gives their own label to the tenant
    upd_adm_t = await ten_router.api_update_tenant(
        301, TenantUpdateBody(name="Жилец 75 (от Админа)"), user=admin
    )
    assert upd_adm_t["name"] == "Жилец 75 (от Админа)"

    # Verify Admin's view: Admin sees ONLY the names Admin gave!
    adm_fams = await fam_router.api_families(user=admin)
    assert adm_fams["families"][0]["name"] == "Семья от Админа"
    assert adm_fams["families"][0]["owners"][0]["name"] == "Владелец от Админа"
    assert adm_fams["tenants"][0]["name"] == "Жилец 75 (от Админа)"

    adm_detail = await apt_router.api_apartment_detail(apt.id, user=admin)
    assert adm_detail["tenants"][0]["name"] == "Жилец 75 (от Админа)"

    adm_users = await admin_router.api_admin_users(user=admin)
    adm_map = {u["telegram_id"]: u["name"] for u in adm_users["users"]}
    assert adm_map[201] == "Владелец от Админа"
    assert adm_map[301] == "Жилец 75 (от Админа)"

    # Verify Landlord's view: Landlord sees own name, own family name, and Landlord's name for tenant!
    ll_fams = await fam_router.api_families(user=landlord)
    assert ll_fams["families"][0]["name"] == "Моя Семья"
    assert ll_fams["families"][0]["owners"][0]["name"] == "Иван (своё имя)"
    assert ll_fams["tenants"][0]["name"] == "Наниматель от Владельца"

    ll_detail = await apt_router.api_apartment_detail(apt.id, user=landlord)
    assert ll_detail["tenants"][0]["name"] == "Наниматель от Владельца"

    ll_users = await admin_router.api_admin_users(user=landlord)
    ll_map = {u["telegram_id"]: u["name"] for u in ll_users["users"]}
    assert ll_map[201] == "Иван (своё имя)"
    assert ll_map[301] == "Наниматель от Владельца"

    # Verify Tenant's own profile name is preserved
    db_t = await crud.get_user_by_telegram_id(301)
    assert db_t is not None and db_t.name == "Сергей (своё имя)"

    # 4. Admin renames family & owner later -> Landlord's custom names stay untouched!
    await fam_router.api_rename_family(fam_id, FamilyBody(name="Семья VIP (Админ)"), user=admin)
    await fam_router.api_update_family_owner(
        fam_id, 201, FamilyOwnerUpdateBody(name="Иван VIP (Админ)"), user=admin
    )
    assert (await fam_router.api_families(user=admin))["families"][0]["name"] == "Семья VIP (Админ)"
    assert (await fam_router.api_families(user=admin))["families"][0]["owners"][0]["name"] == "Иван VIP (Админ)"
    assert (await fam_router.api_families(user=landlord))["families"][0]["name"] == "Моя Семья"
    assert (await fam_router.api_families(user=landlord))["families"][0]["owners"][0]["name"] == "Иван (своё имя)"

    # 5. Landlord renames tenant -> Admin's label and Tenant's own name stay untouched!
    upd_ll_t = await ten_router.api_update_tenant(
        301, TenantUpdateBody(name="Сергей кв.75 (Владелец)"), user=landlord
    )
    assert upd_ll_t["name"] == "Сергей кв.75 (Владелец)"
    assert (await fam_router.api_families(user=landlord))["tenants"][0]["name"] == "Сергей кв.75 (Владелец)"
    assert (await fam_router.api_families(user=admin))["tenants"][0]["name"] == "Жилец 75 (от Админа)"
    assert (await crud.get_user_by_telegram_id(301)).name == "Сергей (своё имя)"

    # 6. Photo forwarding caption shows per-recipient name (admin sees admin_name, landlord sees landlord_name)
    await fam_router.api_add_family_owner(
        fam_id, FamilyOwnerBody(telegram_id=100, name="Администратор"), user=admin
    )
    photo_bot = DummyBot()
    sent = await notifications.forward_meter_photo_file(
        bot=photo_bot,
        apt_id=apt.id,
        filepath=None,
        sender_tg_id=301,
        sender_name="Сергей (своё имя)",
        comment="ХВС",
        photo_file_id="FILE_ID_1",
    )
    assert sent == 2
    by_uid = {p["chat_id"]: p["caption"] for p in photo_bot.photos}
    assert "Жилец 75 (от Админа)" in by_uid[100]
    assert "Сергей кв.75 (Владелец)" in by_uid[201]

    # 7. Family backup does not leak admin_name aliases
    exp_dir = tmp_path / "exp_names"
    exp_dir.mkdir()
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite+aiosqlite:///{temp_db}")
    monkeypatch.setattr(config, "EXPORT_DIR", exp_dir)
    fam_db = await backup.create_family_backup(fam_id)
    try:
        raw = fam_db.read_bytes()
        assert "Семья VIP (Админ)".encode("utf-8") not in raw
        assert "Иван VIP (Админ)".encode("utf-8") not in raw
        assert "Жилец 75 (от Админа)".encode("utf-8") not in raw
    finally:
        fam_db.unlink(missing_ok=True)

    # 8. Legacy DB migration backfills families.admin_name, users.admin_name, users.landlord_name
    leg_db = tmp_path / "legacy_names.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{leg_db}", future=True)
    async with eng.begin() as conn:
        await conn.execute(text("CREATE TABLE families (id INTEGER PRIMARY KEY, name VARCHAR(120) NOT NULL)"))
        await conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, telegram_id BIGINT NOT NULL, name VARCHAR(120), role VARCHAR(20) NOT NULL)"))
        await conn.execute(text("INSERT INTO families VALUES (1, 'Старая семья')"))
        await conn.execute(text("INSERT INTO users VALUES (1, 201, 'Старый владелец', 'landlord')"))
        await conn.execute(text("INSERT INTO users VALUES (2, 301, 'Старый жилец', 'tenant')"))
        await conn.run_sync(_run_sqlite_migrations)
        fam_row = (await conn.execute(text("SELECT name, admin_name FROM families WHERE id = 1"))).fetchone()
        u1_row = (await conn.execute(text("SELECT name, admin_name, landlord_name FROM users WHERE id = 1"))).fetchone()
        u2_row = (await conn.execute(text("SELECT name, admin_name, landlord_name FROM users WHERE id = 2"))).fetchone()
    await eng.dispose()
    assert fam_row == ("Старая семья", "Старая семья")
    assert u1_row == ("Старый владелец", "Старый владелец", None)
    assert u2_row == ("Старый жилец", "Старый жилец", "Старый жилец")


@pytest.mark.asyncio
async def test_landlord_creates_isolated_family_with_owner_and_notifies_admin(temp_db):
    await crud.sync_admin_user(100)
    fam1 = await crud.create_family("Семья 1")
    await crud.get_or_create_user(201, name="Владелец 1", role="landlord", family_id=fam1.id)
    landlord1 = WebAppUser(telegram_id=201, role="landlord", family_id=fam1.id, name="Владелец 1")

    bot = DummyBot()
    req = make_request(bot)

    # 1. Landlord 1 creates a new family + 1 new owner
    res = await fam_router.api_invite_family(
        FamilyInviteBody(
            family_name="Семья Друзей",
            owner_telegram_id=250,
            owner_name="Дмитрий",
        ),
        req,
        user=landlord1,
    )
    assert res["ok"] is True
    assert res["family_name"] == "Семья Друзей"
    assert res["owner_name"] == "Дмитрий"
    assert res["invite_text"] == (
        "Привет Дмитрий, приглашаю присоединиться к боту для управления недвижимостью "
        "(передавать показания счётчиков, следить за сроками поверки, отслеживать договоры найма, "
        "нанимателей и даты платежей, а так же многое другое). "
        "Ссылка на Телеграм бот - @TG_Utility_Meter_bot"
    )

    # 2. Admin receives notification with creator, new family name, new owner name and TG ID
    assert len(bot.sent) == 1
    assert bot.sent[0]["chat_id"] == 100
    adm_msg = bot.sent[0]["text"]
    assert "Владелец 1" in adm_msg
    assert "201" in adm_msg
    assert "Семья Друзей" in adm_msg
    assert "Дмитрий" in adm_msg
    assert "250" in adm_msg

    # 3. Duplicate Telegram ID (existing owner, creator, or admin) returns 409 with exact message
    for dup_id in (250, 201, 100):
        with pytest.raises(HTTPException) as exc_dup:
            await fam_router.api_invite_family(
                FamilyInviteBody(
                    family_name="Дубль",
                    owner_telegram_id=dup_id,
                    owner_name="Кто-то",
                ),
                req,
                user=landlord1,
            )
        assert exc_dup.value.status_code == 409
        assert "Такой пользователь уже зарегистрирован" in exc_dup.value.detail

    # 4. Strict isolation: creator (landlord1) has NO access to the new family or its owner
    new_owner_db = await crud.get_user_by_telegram_id(250)
    assert new_owner_db is not None and new_owner_db.role == "landlord"
    new_fam_id = new_owner_db.family_id
    assert new_fam_id is not None and new_fam_id != fam1.id

    ll1_fams = await fam_router.api_families(user=landlord1)
    assert [f["id"] for f in ll1_fams["families"]] == [fam1.id]
    assert ll1_fams["can_invite_family"] is True

    ll1_users = await admin_router.api_admin_users(user=landlord1)
    assert 250 not in {u["telegram_id"] for u in ll1_users["users"]}

    assert await access.user_can_manage_family(201, new_fam_id) is False
    with pytest.raises(HTTPException) as exc_forb:
        await fam_router.api_rename_family(new_fam_id, FamilyBody(name="Хак"), user=landlord1)
    assert exc_forb.value.status_code == 403

    # 5. New owner CAN access and manage their own new family
    new_owner_user = WebAppUser(telegram_id=250, role="landlord", family_id=new_fam_id, name="Дмитрий")
    new_owner_fams = await fam_router.api_families(user=new_owner_user)
    assert len(new_owner_fams["families"]) == 1
    assert new_owner_fams["families"][0]["name"] == "Семья Друзей"
    assert [o["telegram_id"] for o in new_owner_fams["families"][0]["owners"]] == [250]

    # 6. Tenant cannot create a family
    u_t, _ = await crud.get_or_create_user(390, name="Жилец", role="tenant", family_id=fam1.id)
    tenant_user = WebAppUser(telegram_id=390, role="tenant", family_id=fam1.id, name="Жилец")
    with pytest.raises(HTTPException) as exc_t:
        await fam_router.api_invite_family(
            FamilyInviteBody(family_name="Т", owner_telegram_id=299, owner_name="Х"),
            req,
            user=tenant_user,
        )
    assert exc_t.value.status_code == 403

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
    assert "Дата ежемесячного платежа (число месяца)" in app_js
    assert "Дата ежемесячного платежа (число месяца, опционально)" not in app_js
    assert "приходят только владельцам внутри семьи." in app_js
    assert "владелец может подтвердить корректировку ошибочного ввода" in app_js
    assert "владелец может отредактировать любую сохранённую запись" in app_js
    assert "нанимателю и владельцу в 15:00 UTC+5" in app_js
    assert "удалить нанимателя с прекращением доступа к боту" in app_js
    assert "укажите серийный номер счётчика, дату следующей поверки и выберите сроки уведомлений." in app_js
    assert "выберите заменяемый счётчик, введите конечное показание старого и начальное показание нового счётчика." in app_js
    assert "¡ Важно !" in app_js
    assert "не передаются в управляющую компанию или в компанию-поставщик энергетических ресурсов!" in app_js
    # New family + owner invite modal, copy box, clickable bot link, and guide without admin mention
    assert 'id="btn-invite-family"' in app_js
    assert 'id: "modal-invite-family"' in app_js
    assert "Скопируйте текст ниже и перешлите в Телеграм тому, кого вы только что добавили как владельца в новую семью" in app_js
    assert "@TG_Utility_Meter_bot" in app_js
    assert "https://t.me/TG_Utility_Meter_bot" in app_js
    assert "➕ Создание новой семьи и владельца" in app_js
    guide_slice = app_js[app_js.index("const APP_GUIDE_TOPICS = ["):app_js.index("function buildAboutAppHtml()")]
    assert "админ" not in guide_slice.lower()
    # Phone number inputs/masks removed everywhere from UI and schemas
    assert "phone" not in app_js.lower()
    assert "телефон" not in app_js.lower()
    assert "phone" not in schemas_py.lower()


@pytest.mark.asyncio
async def test_p1_01_audit_and_family_backup_strict_family_scope_isolation(temp_db, tmp_path, monkeypatch):
    import sqlite3 as _sqlite3

    exp_dir = tmp_path / "exp_p1_01"
    exp_dir.mkdir()
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite+aiosqlite:///{temp_db}")
    monkeypatch.setattr(config, "EXPORT_DIR", exp_dir)

    fam_a = await crud.create_family("Семья A")
    fam_b = await crud.create_family("Семья B")
    apt_a = await crud.create_apartment("101", fam_a.id, enabled_meters=["cold_water"])
    apt_b = await crud.create_apartment("202", fam_b.id, enabled_meters=["cold_water"])

    await crud.get_or_create_user(201, name="Владелец A", role="landlord", family_id=fam_a.id)
    await crud.get_or_create_user(202, name="Владелец B", role="landlord", family_id=fam_b.id)
    u_t, _ = await crud.get_or_create_user(555, name="Переезжающий", role="tenant", family_id=fam_a.id)
    await crud.link_user_to_apartment(apt_a.id, u_t.id, role="tenant")

    tenant_a = WebAppUser(telegram_id=555, role="tenant", family_id=fam_a.id, name="Переезжающий")
    admin = WebAppUser(telegram_id=100, role="admin", family_id=None, name="Admin")
    landlord_b = WebAppUser(telegram_id=202, role="landlord", family_id=fam_b.id, name="Владелец B")
    req = make_request()

    # Tenant submits reading in Family A
    await read_router.api_submit_water(apt_a.id, WaterReadingBody(cold_water=777), req, user=tenant_a)
    # Remove tenant from Family A and register the SAME Telegram ID in Family B
    await ten_router.api_remove_tenant(apt_a.id, 555, req, user=admin)
    await ten_router.api_add_tenant(
        apt_b.id, TenantCreateBody(telegram_id=555, name="Переезжающий B", force_no_contract=True), req, user=landlord_b
    )

    # Family B audit log MUST NOT contain Family A's reading event
    audit_b = await admin_router.api_admin_audit(user=landlord_b)
    assert not any("777" in (l["details"] or "") or "101" in (l["details"] or "") for l in audit_b["logs"])

    # Family B backup MUST NOT contain Family A's audit records or bytes
    bak_b = await backup.create_family_backup(fam_b.id)
    try:
        raw = bak_b.read_bytes()
        assert b"777" not in raw
        conn = _sqlite3.connect(str(bak_b))
        try:
            rows = conn.execute("SELECT details FROM audit_logs").fetchall()
        finally:
            conn.close()
        assert not any("101" in (r[0] or "") for r in rows)
    finally:
        bak_b.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_p1_02_revoked_admin_removed_from_family_owners_and_notifications(temp_db, monkeypatch):
    fam = await crud.create_family("Семья")
    apt = await crud.create_apartment("1", fam.id)
    await crud.sync_admin_user(100)
    await crud.get_or_create_user(201, name="Владелец", role="landlord", family_id=fam.id)

    admin_old = WebAppUser(telegram_id=100, role="admin", family_id=None, name="OldAdmin")
    await fam_router.api_add_family_owner(fam.id, FamilyOwnerBody(telegram_id=100, name="OldAdmin"), user=admin_old)
    assert set(await access.get_notification_recipients(apt.id)) == {201, 100}

    # Rotate ADMIN_TELEGRAM_ID to 999
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 999)
    await crud.sync_admin_user(999)

    assert await access.get_notification_recipients(apt.id) == [201]
    assert [o.telegram_id for o in await crud.get_family_owners(fam.id)] == [201]


@pytest.mark.asyncio
async def test_p1_03_toctou_revoked_access_blocked_inside_write_transaction(temp_db):
    fam = await crud.create_family("F")
    apt = await crud.create_apartment("1", fam.id, enabled_meters=["cold_water"])
    u_t, _ = await crud.get_or_create_user(310, name="Жилец", role="tenant", family_id=fam.id)
    await crud.link_user_to_apartment(apt.id, u_t.id, role="tenant")

    # Revoke tenant right before atomic write executes
    await operations.remove_tenant_from_apartment_atomic(apt.id, 310, actor_tg_id=100)
    with pytest.raises(PermissionError):
        await operations.submit_meter_group_atomic(
            apt.id, {"cold_water": 150}, 310, datetime.date(2026, 11, 20), "submit_water"
        )
    assert await crud.get_readings_count(apt.id) == 0


@pytest.mark.asyncio
async def test_p1_04_and_p1_05_replacement_init_locked_neighbor_bounds_and_enabled_initial(temp_db, monkeypatch):
    fam = await crud.create_family("F")
    apt = await crud.create_apartment("1", fam.id, enabled_meters=["cold_water"])
    admin = WebAppUser(telegram_id=100, role="admin", family_id=None, name="Admin")
    req = make_request()

    # P1-05: setting initial reading for disabled meter fails with 400
    from webapp.schemas import InitialReadingsBody, ReadingCorrectionBody
    with pytest.raises(HTTPException) as exc_dis:
        await apt_router.api_update_initial_readings(apt.id, InitialReadingsBody(hot_water=50), user=admin)
    assert exc_dis.value.status_code == 400

    await operations.update_initial_readings_atomic(apt.id, {"cold_water": 100}, 100)
    await operations.save_meter_group_readings(apt.id, {"cold_water": 110}, 100, datetime.date(2026, 10, 20), "s", "oct")
    await operations.save_meter_group_readings(apt.id, {"cold_water": 130}, 100, datetime.date(2026, 11, 20), "s", "nov")
    await operations.save_meter_group_readings(apt.id, {"cold_water": 150}, 100, datetime.date(2026, 12, 20), "s", "dec")

    rows = await crud.get_all_readings(apt.id)
    nov_rec = [r for r in rows if r.value == 130][0]

    # P1-05: editing Nov above Dec (160 > 150) or below Oct (105 < 110) is rejected with 400
    with pytest.raises(HTTPException) as exc_hi:
        await read_router.api_correct_reading(nov_rec.id, ReadingCorrectionBody(value=160), user=admin)
    assert exc_hi.value.status_code == 400
    with pytest.raises(HTTPException) as exc_lo:
        await read_router.api_correct_reading(nov_rec.id, ReadingCorrectionBody(value=105), user=admin)
    assert exc_lo.value.status_code == 400

    # Valid edit within [110, 150] succeeds
    ok = await read_router.api_correct_reading(nov_rec.id, ReadingCorrectionBody(value=135), user=admin)
    assert ok["ok"] is True and ok["new_value"] == 135

    # P1-04: editing replacement_init reading is rejected with 400
    monkeypatch.setattr(config, "local_today", lambda: datetime.date(2026, 12, 25))
    monkeypatch.setattr(apt_router, "local_today", lambda: datetime.date(2026, 12, 25))
    await apt_router.api_replace_meter(
        apt.id,
        MeterReplacementBody(meter_type="cold_water", old_final_value=155, new_initial_value=5),
        user=admin,
    )
    all_rows = await crud.get_all_readings(apt.id)
    rep_init = [r for r in all_rows if r.is_replacement_init][0]
    with pytest.raises(HTTPException) as exc_rep:
        await read_router.api_correct_reading(rep_init.id, ReadingCorrectionBody(value=10), user=admin)
    assert exc_rep.value.status_code == 400


@pytest.mark.asyncio
async def test_p1_06_p2_01_p2_02_replacement_validation_idempotency_metadata_reset_and_dedup(temp_db):
    fam = await crud.create_family("F")
    apt = await crud.create_apartment("42", fam.id, enabled_meters=["cold_water", "t1", "t2", "t3"])
    await crud.get_or_create_user(201, name="Владелец", role="landlord", family_id=fam.id)
    admin = WebAppUser(telegram_id=100, role="admin", family_id=None, name="Admin")

    # Save device metadata with old serial & verification date
    await apt_router.api_update_meter_devices(
        apt.id,
        MeterDevicesUpdateBody(
            items=[
                MeterDeviceItemBody(
                    meter_type="t",
                    serial_number="OLD-EL-1",
                    verification_due_date="25.02.2027",
                    notify_intervals=["3m"],
                )
            ]
        ),
        user=admin,
    )

    # P1-06: single tariff replacement of t1 is rejected
    with pytest.raises(HTTPException) as exc_s:
        await apt_router.api_replace_meter(
            apt.id,
            MeterReplacementBody(meter_type="t1", old_final_value=10, new_initial_value=0),
            user=admin,
        )
    assert exc_s.value.status_code == 400

    # P1-06: incomplete batch (only t1, t2) rejected
    with pytest.raises(HTTPException) as exc_inc:
        await apt_router.api_replace_meter(
            apt.id,
            MeterReplacementBody(
                meter_type="t",
                tariff_entries=[
                    MeterReplacementTariffEntry(meter_type="t1", old_final_value=10, new_initial_value=0),
                    MeterReplacementTariffEntry(meter_type="t2", old_final_value=20, new_initial_value=0),
                ],
            ),
            user=admin,
        )
    assert exc_inc.value.status_code == 400

    # P1-06: duplicate tariff rejected
    with pytest.raises(HTTPException) as exc_dup:
        await apt_router.api_replace_meter(
            apt.id,
            MeterReplacementBody(
                meter_type="t",
                tariff_entries=[
                    MeterReplacementTariffEntry(meter_type="t1", old_final_value=10, new_initial_value=0),
                    MeterReplacementTariffEntry(meter_type="t1", old_final_value=10, new_initial_value=0),
                    MeterReplacementTariffEntry(meter_type="t3", old_final_value=30, new_initial_value=0),
                ],
            ),
            user=admin,
        )
    assert exc_dup.value.status_code == 400

    # Valid batch replacement WITHOUT new serial/due date -> P2-01 clears old serial & verification date!
    batch_body = MeterReplacementBody(
        meter_type="t",
        tariff_entries=[
            MeterReplacementTariffEntry(meter_type="t1", old_final_value=10, new_initial_value=0),
            MeterReplacementTariffEntry(meter_type="t2", old_final_value=20, new_initial_value=0),
            MeterReplacementTariffEntry(meter_type="t3", old_final_value=30, new_initial_value=0),
        ],
        replacement_id="rep-elec-1",
    )
    r1 = await apt_router.api_replace_meter(apt.id, batch_body, user=admin)
    assert r1["ok"] is True and r1["deduplicated"] is False

    # P1-06: identical retry returns deduplicated=True and does not create extra replacement rows
    r2 = await apt_router.api_replace_meter(apt.id, batch_body, user=admin)
    assert r2["ok"] is True and r2["deduplicated"] is True
    assert len(await crud.get_meter_replacements(apt.id)) == 3

    # P2-01: old verification date cleared -> no stale verification reminder sent
    bot = DummyBot()
    await scheduler.check_meter_verifications(bot, today=datetime.date(2026, 11, 25))
    assert bot.sent == []

    # P2-02: legacy t1/t2 MeterDevice rows alongside canonical t produce only ONE verification alert
    from database.models import MeterDevice
    async with crud.UnitOfWork(write=True) as s:
        s.add(MeterDevice(apartment_id=apt.id, meter_type="t1", serial_number="L1", verification_due_date=datetime.date(2027, 2, 25), notify_intervals="3m"))
        s.add(MeterDevice(apartment_id=apt.id, meter_type="t2", serial_number="L2", verification_due_date=datetime.date(2027, 2, 25), notify_intervals="3m"))
    await apt_router.api_update_meter_devices(
        apt.id,
        MeterDevicesUpdateBody(items=[MeterDeviceItemBody(meter_type="t", serial_number="CANON", verification_due_date="25.02.2027", notify_intervals=["3m"])]),
        user=admin,
    )
    bot2 = DummyBot()
    await scheduler.check_meter_verifications(bot2, today=datetime.date(2026, 11, 25))
    assert len(bot2.sent) == 1
    assert "CANON" in bot2.sent[0]["text"]


@pytest.mark.asyncio
async def test_p1_08_and_p2_17_concurrent_settings_and_partial_contract_preservation(temp_db, monkeypatch):
    monkeypatch.setattr(config, "local_today", lambda: datetime.date(2026, 11, 25))
    fam = await crud.create_family("F")
    apt = await crud.create_apartment("10", fam.id, property_type="house", enabled_meters=["cold_water", "t"])
    u_t, _ = await crud.get_or_create_user(320, name="Истёкший", role="tenant", family_id=fam.id)
    await crud.link_user_to_apartment(apt.id, u_t.id, role="tenant")
    admin = WebAppUser(telegram_id=100, role="admin", family_id=None, name="Admin")

    # P1-08: parallel partial settings updates preserve both days
    await asyncio.gather(
        apt_router.api_update_settings(apt.id, ApartmentSettingsBody(water_submission_day=18), user=admin),
        apt_router.api_update_settings(apt.id, ApartmentSettingsBody(electricity_submission_day=24), user=admin),
    )
    reloaded = await crud.get_apartment(apt.id)
    assert reloaded.water_submission_day == 18
    assert reloaded.electricity_submission_day == 24

    # P1-08: concurrent house->apartment and adding irrigation cannot produce invalid state
    results = await asyncio.gather(
        apt_router.api_update_settings(apt.id, ApartmentSettingsBody(property_type="apartment"), user=admin),
        apt_router.api_update_settings(apt.id, ApartmentSettingsBody(enabled_meters=["cold_water", "irrigation", "t"]), user=admin),
        return_exceptions=True,
    )
    assert sum(1 for r in results if isinstance(r, dict) and r.get("ok")) == 1
    assert sum(1 for r in results if isinstance(r, HTTPException) and r.status_code == 400) == 1

    # P2-17: partial PUT contract with only has_tenant MUST NOT wipe expired finish date
    await ten_router.api_update_contract(
        apt.id,
        ContractBody(agreement_no="99", agreement_date_start="01.01.2025", agreement_date_finish="01.01.2026"),
        user=admin,
    )
    assert await access.user_can_access_apartment(320, apt.id) is False

    await ten_router.api_update_contract(apt.id, ContractBody(has_tenant=True), user=admin)
    after = await crud.get_apartment(apt.id)
    assert after.agreement_no == "99"
    assert after.agreement_date_finish == datetime.date(2026, 1, 1)
    assert await access.user_can_access_apartment(320, apt.id) is False


@pytest.mark.asyncio
async def test_p1_09_p2_20_p2_12_backup_revision_ignores_service_writes_and_cleans_photos(temp_db, tmp_path, monkeypatch):
    bdir = tmp_path / "b_rev"
    pdir = tmp_path / "p_old"
    bdir.mkdir()
    pdir.mkdir()
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite+aiosqlite:///{temp_db}")
    monkeypatch.setattr(config, "BACKUP_DIR", bdir)
    monkeypatch.setattr(backup, "PHOTOS_DIR", pdir)

    old_photo = pdir / "stale.jpg"
    old_photo.write_bytes(b"img")
    old_t = time.time() - 40 * 86400
    os.utime(old_photo, (old_t, old_t))

    p1 = await backup.create_backup(skip_if_unchanged=True)
    assert p1 is not None

    # P2-20: service writes (scheduler_runs & manual_backup audit) do NOT force a new backup
    await crud.record_scheduler_run("test_job", datetime.date(2026, 11, 25))
    await crud.add_audit_log(100, "manual_backup", "noop")
    p2 = await backup.create_backup(skip_if_unchanged=True)
    assert p2 is None

    # P1-09: real business write increments business_revision -> next backup is created
    await crud.create_family("Real Change")
    p3 = await backup.create_backup(skip_if_unchanged=True)
    assert p3 is not None and p3 != p1

    # P2-12: daily backup job cleans up old legacy photos
    await scheduler._run_daily_backup_and_cleanup()
    assert not old_photo.exists()


@pytest.mark.asyncio
async def test_p2_05_p2_08_p2_09_p2_11_deeplink_irrigation_midnight_and_heic(temp_db, monkeypatch):
    fam = await crud.create_family("F")
    apt = await crud.create_apartment(
        "188", fam.id, property_type="house",
        enabled_meters=["cold_water", "hot_water", "irrigation"],
        irrigation_mode="after_cold",
    )
    admin = WebAppUser(telegram_id=100, role="admin", family_id=None, name="Admin")
    req = make_request()

    # P2-05: build_webapp_url includes apt_id
    url = notifications.build_webapp_url(tab="water", apt_id=apt.id)
    assert f"tab=water&apt_id={apt.id}" in url

    # P2-08: irrigation > cold_water does NOT deduct excess from hot_water and flags incomplete_reasons
    await operations.update_initial_readings_atomic(apt.id, {"cold_water": 100, "hot_water": 50, "irrigation": 10}, 100)
    await crud.set_tariff(apt.id, "cold_water", Decimal("40"), datetime.date(2026, 1, 1), 100)
    await crud.set_tariff(apt.id, "hot_water", Decimal("150"), datetime.date(2026, 1, 1), 100)
    await crud.set_tariff(apt.id, "sewerage", Decimal("30"), datetime.date(2026, 1, 1), 100)

    monkeypatch.setattr(read_router, "local_today", lambda: datetime.date(2026, 11, 25))
    monkeypatch.setattr(calculations, "local_today", lambda: datetime.date(2026, 11, 25))
    await read_router.api_submit_water(
        apt.id,
        WaterReadingBody(cold_water=110, hot_water=55, irrigation=22, submission_id="mid-sub-1"),
        req,
        user=admin,
    )
    bill = await calculations.calculate_billing(apt.id, year=2026, month=11)
    # ХВС=10, ГВС=5, Полив=12 -> sewerage = max(0, 10 - 12) + 5 = 5 м³ (NOT 3 м³!)
    assert bill.sewerage is not None
    assert bill.sewerage.consumption == 5
    assert bill.sewerage.cost == 150
    assert bill.is_complete is False
    assert "полив превышает расход ХВС" in bill.incomplete_reasons

    # P2-09: retrying same submission_id after midnight deduplicates cleanly instead of 409
    monkeypatch.setattr(read_router, "local_today", lambda: datetime.date(2026, 11, 26))
    retry_after_midnight = await read_router.api_submit_water(
        apt.id,
        WaterReadingBody(cold_water=110, hot_water=55, irrigation=22, submission_id="mid-sub-1"),
        req,
        user=admin,
    )
    assert retry_after_midnight["ok"] is True
    assert retry_after_midnight["deduplicated"] is True

    # P2-11: HEIC file is rejected (not silently renamed to .jpg)
    heic = DummyUploadFile(b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00", "photo.heic")
    with pytest.raises(HTTPException) as exc_heic:
        await read_router.api_upload_photo(apt.id, req, file=heic, comment="", user=admin)
    assert exc_heic.value.status_code == 400

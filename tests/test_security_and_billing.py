"""Regression tests for P0 security, P1 billing/atomicity, and P2 edge cases."""

from __future__ import annotations

import datetime
import os
import time
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import config
from database import crud, operations
from database.models import Base
from services import access, backup, calculations, notifications, scheduler


@pytest_asyncio.fixture
async def temp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(crud, "async_session_factory", factory)
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 100)
    yield db_file
    await engine.dispose()


@pytest.mark.asyncio
async def test_critical_01_cross_family_owner_delete_blocked(temp_db):
    fam_a = await crud.create_family("Family A")
    fam_b = await crud.create_family("Family B")
    await crud.get_or_create_user(201, name="Owner A", role="landlord", family_id=fam_a.id)
    await crud.get_or_create_user(202, name="Owner B", role="landlord", family_id=fam_b.id)
    await crud.sync_admin_user(100)

    with pytest.raises(LookupError):
        await operations.delete_family_owner_atomic(fam_a.id, 202, actor_tg_id=201)
    assert await crud.get_user_by_telegram_id(202) is not None

    with pytest.raises(PermissionError):
        await operations.delete_family_owner_atomic(fam_a.id, 100, actor_tg_id=201)
    assert await crud.get_user_by_telegram_id(100) is not None

    await operations.delete_family_owner_atomic(fam_b.id, 202, actor_tg_id=100)
    assert await crud.get_user_by_telegram_id(202) is None


@pytest.mark.asyncio
async def test_critical_02_admin_rotation_revokes_old_admin(temp_db, monkeypatch):
    await crud.sync_admin_user(100)
    assert await access.get_user_role(100) == "admin"

    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 999)
    await crud.sync_admin_user(999)
    assert await access.get_user_role(100) == "unknown"
    assert await access.get_user_role(999) == "admin"


@pytest.mark.asyncio
async def test_critical_03_and_medium_04_remove_tenant_isolation(temp_db):
    fam_a = await crud.create_family("A")
    fam_b = await crud.create_family("B")
    apt_a1 = await crud.create_apartment("1", fam_a.id)
    apt_a2 = await crud.create_apartment("2", fam_a.id)
    apt_b1 = await crud.create_apartment("3", fam_b.id)

    u_b, _ = await crud.get_or_create_user(301, name="Tenant B", role="tenant", family_id=fam_b.id)
    await crud.link_user_to_apartment(apt_b1.id, u_b.id, role="tenant")

    with pytest.raises(LookupError):
        await operations.remove_tenant_from_apartment_atomic(apt_a1.id, 301, actor_tg_id=100)
    assert await crud.get_user_by_telegram_id(301) is not None

    u_a, _ = await crud.get_or_create_user(302, name="Multi Tenant", role="tenant", family_id=fam_a.id)
    await crud.link_user_to_apartment(apt_a1.id, u_a.id, role="tenant")
    await crud.link_user_to_apartment(apt_a2.id, u_a.id, role="tenant")

    _, full1 = await operations.remove_tenant_from_apartment_atomic(apt_a1.id, 302, actor_tg_id=100)
    assert full1 is False
    assert await crud.get_user_by_telegram_id(302) is not None

    _, full2 = await operations.remove_tenant_from_apartment_atomic(apt_a2.id, 302, actor_tg_id=100)
    assert full2 is True
    assert await crud.get_user_by_telegram_id(302) is None


@pytest.mark.asyncio
async def test_high_01_02_05_billing_calendar_historical_tariff_and_no_base(temp_db, monkeypatch):
    fam = await crud.create_family("F")
    apt = await crud.create_apartment("371", fam.id)

    await crud.set_tariff(apt.id, "cold_water", Decimal("50"), datetime.date(2026, 1, 1), 100)
    await operations.save_meter_group_readings(
        apt.id, {"cold_water": 12450}, 100, datetime.date(2026, 1, 20), "submit", "init"
    )
    jan_no_base = await calculations.calculate_billing(apt.id, year=2026, month=1)
    assert jan_no_base.meters["cold_water"].has_base is False
    assert jan_no_base.meters["cold_water"].consumption == 0
    assert jan_no_base.water_total == 0

    await operations.update_initial_readings_atomic(apt.id, {"hot_water": 90}, 100)
    await crud.set_tariff(apt.id, "hot_water", Decimal("50"), datetime.date(2026, 9, 1), 100)
    await crud.set_tariff(apt.id, "hot_water", Decimal("70"), datetime.date(2026, 11, 1), 100)

    await operations.save_meter_group_readings(apt.id, {"hot_water": 100}, 100, datetime.date(2026, 9, 30), "s", "sep")
    await operations.save_meter_group_readings(apt.id, {"hot_water": 110}, 100, datetime.date(2026, 10, 31), "s", "oct")
    await operations.save_meter_group_readings(apt.id, {"hot_water": 120}, 100, datetime.date(2026, 11, 15), "s", "nov1")
    await operations.save_meter_group_readings(apt.id, {"hot_water": 125}, 100, datetime.date(2026, 11, 20), "s", "nov2")

    oct_bill = await calculations.calculate_billing(apt.id, year=2026, month=10)
    assert oct_bill.meters["hot_water"].consumption == 10
    assert oct_bill.meters["hot_water"].cost == 500

    monkeypatch.setattr(calculations, "local_today", lambda: datetime.date(2026, 11, 22))
    nov_bill = await calculations.calculate_billing(apt.id)
    assert nov_bill.meters["hot_water"].consumption == 15
    assert nov_bill.meters["hot_water"].cost == 1050

    monkeypatch.setattr(calculations, "local_today", lambda: datetime.date(2026, 12, 5))
    dec_bill = await calculations.calculate_billing(apt.id)
    assert dec_bill.meters["hot_water"].submitted is False
    assert dec_bill.meters["hot_water"].consumption == 0
    assert dec_bill.meters["hot_water"].cost == 0


@pytest.mark.asyncio
async def test_medium_01_delete_family_revokes_users(temp_db):
    fam = await crud.create_family("ToDelete")
    await crud.get_or_create_user(401, name="Landlord", role="landlord", family_id=fam.id)
    await operations.delete_family_atomic(fam.id, actor_tg_id=100)
    assert await access.get_user_role(401) == "unknown"


def test_medium_03_effective_submission_day():
    assert scheduler.effective_submission_day(31, 2026, 2) == 28
    assert scheduler.effective_submission_day(31, 2024, 2) == 29
    assert scheduler.effective_submission_day(31, 2026, 4) == 30
    assert scheduler.effective_submission_day(15, 2026, 2) == 15


def test_high_09_photo_retention_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "PHOTOS_DIR", tmp_path)
    old_photo = tmp_path / "old.jpg"
    new_photo = tmp_path / "new.jpg"
    old_photo.write_bytes(b"old")
    new_photo.write_bytes(b"new")
    old_mtime = time.time() - 31 * 86400
    os.utime(old_photo, (old_mtime, old_mtime))

    removed = backup._cleanup_old_photos_sync(retention_days=30)
    assert removed == 1
    assert not old_photo.exists()
    assert new_photo.exists()


@pytest.mark.asyncio
async def test_medium_05_html_escaping_in_notifications():
    class DummyBot:
        def __init__(self):
            self.sent = []
        async def send_message(self, **kwargs):
            self.sent.append(kwargs["text"])

    class DummyApt:
        number = "371 <test>&"
        agreement_no = "№6 <tag>"
        agreement_date_start = datetime.date(2026, 1, 1)
        agreement_date_finish = datetime.date(2026, 12, 31)

    bot = DummyBot()
    ok = await notifications.notify_tenant_added(bot, DummyApt(), 500, "Иван <script>&")
    assert ok is True
    assert "&lt;script&gt;&amp;" in bot.sent[0]
    assert "&lt;test&gt;&amp;" in bot.sent[0]

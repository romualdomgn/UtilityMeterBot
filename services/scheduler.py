"""APScheduler background jobs: reminders, lease expiration notices, DB backup, photo cleanup."""

from __future__ import annotations

import calendar
import datetime
import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from config import BOT_TZ, local_today
from database import crud
from database.models import Apartment, ELECTRICITY_METERS, WATER_METERS
from services.access import get_notification_recipients, is_tenant_lease_active
from services.backup import cleanup_old_photos, create_backup
from services.calculations import fmt_date
from services.notifications import webapp_open_kb

logger = logging.getLogger(__name__)


def effective_submission_day(configured_day: int, year: int, month: int) -> int:
    """MEDIUM-03: clamp 29/30/31 to the last day of short months."""
    return min(configured_day, calendar.monthrange(year, month)[1])


async def _get_apartment_reminder_recipients(apt: Apartment) -> list[int]:
    recipients = await get_notification_recipients(apt.id)
    for tenant in await crud.get_apartment_tenants(apt.id):
        if tenant.is_blocked:
            continue
        if not await is_tenant_lease_active(tenant.telegram_id, apt.id):
            continue
        if tenant.telegram_id not in recipients:
            recipients.append(tenant.telegram_id)
    return recipients


async def check_meter_reminders(bot: Bot, today: datetime.date | None = None) -> None:
    cur = today or local_today()
    async with crud.UnitOfWork() as s:
        result = await s.execute(select(Apartment))
        apartments = result.scalars().all()

    for apt in apartments:
        if apt.water_submission_day:
            if not await crud.has_readings_this_month(apt.id, WATER_METERS, today=cur):
                eff_day = effective_submission_day(apt.water_submission_day, cur.year, cur.month)
                text = None
                if cur.day == eff_day:
                    text = f"💧 Пора подавать показания водомеров (Кв. №{apt.number})."
                elif cur.day > eff_day and (cur.day - eff_day) % 3 == 0:
                    text = f"⚠️ Внимание! Показания водомеров не были поданы в срок (Кв. №{apt.number})."
                if text:
                    kb = webapp_open_kb("💧 Внести показания воды", tab="water")
                    for uid in await _get_apartment_reminder_recipients(apt):
                        try:
                            await bot.send_message(chat_id=uid, text=text, reply_markup=kb)
                        except Exception as e:
                            logger.warning("Water reminder failed for %s: %s", uid, e)

        if apt.electricity_submission_day:
            if not await crud.has_readings_this_month(apt.id, ELECTRICITY_METERS, today=cur):
                eff_day = effective_submission_day(apt.electricity_submission_day, cur.year, cur.month)
                text = None
                if cur.day == eff_day:
                    text = f"⚡ Пора подавать показания электрического счётчика (Кв. №{apt.number})."
                elif cur.day > eff_day and (cur.day - eff_day) % 3 == 0:
                    text = f"⚠️ Внимание! Показания электрического счётчика не были поданы в срок (Кв. №{apt.number})."
                if text:
                    kb = webapp_open_kb("⚡ Внести показания электричества", tab="electricity")
                    for uid in await _get_apartment_reminder_recipients(apt):
                        try:
                            await bot.send_message(chat_id=uid, text=text, reply_markup=kb)
                        except Exception as e:
                            logger.warning("Electricity reminder failed for %s: %s", uid, e)


async def check_lease_expirations(bot: Bot, today: datetime.date | None = None) -> None:
    cur = today or local_today()
    in_seven_days = cur + datetime.timedelta(days=7)

    async with crud.UnitOfWork() as s:
        result = await s.execute(
            select(Apartment).where(Apartment.agreement_date_finish.isnot(None))
        )
        apartments = result.scalars().all()

    for apt in apartments:
        agr_no = apt.agreement_no or "б/н"
        date_start = fmt_date(apt.agreement_date_start)
        msg_text = None

        if apt.agreement_date_finish == in_seven_days:
            msg_text = (
                f"⏳ Срок найма квартиры №{apt.number} по договору №{agr_no} от {date_start} "
                f"завершится через неделю. Не забудьте продлить договор либо удалить нанимателя."
            )
        elif apt.agreement_date_finish == cur:
            msg_text = (
                f"⚠️ Срок найма квартиры №{apt.number} по договору №{agr_no} от {date_start} "
                f"завершился. Не забудьте продлить договор либо удалить нанимателя."
            )

        if msg_text:
            for uid in await get_notification_recipients(apt.id):
                try:
                    await bot.send_message(
                        chat_id=uid,
                        text=msg_text,
                        reply_markup=webapp_open_kb("📱 Открыть управление договором", tab="apartments"),
                    )
                except Exception as e:
                    logger.warning("Lease notice failed for %s: %s", uid, e)


def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=BOT_TZ)
    scheduler.add_job(
        check_meter_reminders,
        trigger="cron", hour=15, minute=0,
        kwargs={"bot": bot}, id="meter_reminders", replace_existing=True,
    )
    scheduler.add_job(
        check_lease_expirations,
        trigger="cron", hour=10, minute=0,
        kwargs={"bot": bot}, id="lease_expirations", replace_existing=True,
    )
    scheduler.add_job(
        create_backup,
        trigger="cron", hour=3, minute=0,
        id="daily_backup", replace_existing=True,
    )
    scheduler.add_job(
        cleanup_old_photos,
        trigger="cron", hour=3, minute=15,
        id="daily_photo_cleanup", replace_existing=True,
    )
    return scheduler

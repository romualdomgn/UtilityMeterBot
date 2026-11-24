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
from database.models import (
    Apartment,
    ELECTRICITY_METERS,
    GAS_METERS,
    WATER_SUBMISSION_METERS,
    format_property_short,
    get_apartment_enabled_meters,
)
from services.access import get_notification_recipients, is_lease_active_for_apartment
from services.backup import cleanup_old_photos, create_backup
from services.calculations import fmt_date
from services.notifications import webapp_open_kb

logger = logging.getLogger(__name__)


def effective_submission_day(configured_day: int, year: int, month: int) -> int:
    """MEDIUM-03: clamp 29/30/31 to the last day of short months."""
    return min(configured_day, calendar.monthrange(year, month)[1])


async def _get_apartment_reminder_recipients(apt: Apartment) -> list[int]:
    recipients = await get_notification_recipients(apt.id)
    if is_lease_active_for_apartment(apt):
        for tenant in await crud.get_apartment_tenants(apt.id):
            if tenant.is_blocked:
                continue
            if tenant.telegram_id not in recipients:
                recipients.append(tenant.telegram_id)
    return recipients


async def _maybe_send_group_reminder(
    bot: Bot,
    apt: Apartment,
    cur: datetime.date,
    configured_day: int | None,
    active_group_meters: list[str],
    on_day_text: str,
    overdue_text: str,
    button_label: str,
    tab: str,
) -> None:
    if not configured_day or not active_group_meters:
        return
    if await crud.has_readings_this_month(apt.id, active_group_meters, today=cur):
        return

    eff_day = effective_submission_day(configured_day, cur.year, cur.month)
    text = None
    if cur.day == eff_day:
        text = on_day_text
    elif cur.day > eff_day and (cur.day - eff_day) % 3 == 0:
        text = overdue_text

    if not text:
        return

    kb = webapp_open_kb(button_label, tab=tab)
    for uid in await _get_apartment_reminder_recipients(apt):
        try:
            await bot.send_message(chat_id=uid, text=text, reply_markup=kb)
        except Exception as e:
            logger.warning("%s reminder failed for %s: %s", tab, uid, e)


async def check_meter_reminders(bot: Bot, today: datetime.date | None = None) -> None:
    cur = today or local_today()
    async with crud.UnitOfWork() as s:
        result = await s.execute(select(Apartment))
        apartments = result.scalars().all()

    for apt in apartments:
        enabled = set(get_apartment_enabled_meters(apt))
        prop_label = format_property_short(apt)

        await _maybe_send_group_reminder(
            bot=bot,
            apt=apt,
            cur=cur,
            configured_day=apt.water_submission_day,
            active_group_meters=[m for m in WATER_SUBMISSION_METERS if m in enabled],
            on_day_text=f"💧 Пора подавать показания водомеров ({prop_label}).",
            overdue_text=f"⚠️ Внимание! Показания водомеров не были поданы в срок ({prop_label}).",
            button_label="💧 Внести показания воды",
            tab="water",
        )

        await _maybe_send_group_reminder(
            bot=bot,
            apt=apt,
            cur=cur,
            configured_day=apt.electricity_submission_day,
            active_group_meters=[m for m in ELECTRICITY_METERS if m in enabled],
            on_day_text=f"⚡ Пора подавать показания электрического счётчика ({prop_label}).",
            overdue_text=f"⚠️ Внимание! Показания электрического счётчика не были поданы в срок ({prop_label}).",
            button_label="⚡ Внести показания электричества",
            tab="electricity",
        )

        await _maybe_send_group_reminder(
            bot=bot,
            apt=apt,
            cur=cur,
            configured_day=apt.gas_submission_day,
            active_group_meters=[m for m in GAS_METERS if m in enabled],
            on_day_text=f"🔥 Пора подавать показания газового счётчика ({prop_label}).",
            overdue_text=f"⚠️ Внимание! Показания газового счётчика не были поданы в срок ({prop_label}).",
            button_label="🔥 Внести показания газа",
            tab="gas",
        )


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
        prop_label = format_property_short(apt)
        msg_text = None

        if apt.agreement_date_finish == in_seven_days:
            msg_text = (
                f"⏳ Срок найма объекта {prop_label} по договору №{agr_no} от {date_start} "
                f"завершится через неделю. Не забудьте продлить договор либо удалить нанимателя."
            )
        elif apt.agreement_date_finish == cur:
            msg_text = (
                f"⚠️ Срок найма объекта {prop_label} по договору №{agr_no} от {date_start} "
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

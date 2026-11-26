"""APScheduler background jobs: reminders, lease expirations, meter verification alerts, DB backup, catch-up."""

from __future__ import annotations

import calendar
import datetime
import logging
from html import escape as h

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from config import BOT_TZ, local_now, local_today
from database import crud
from database.models import (
    Apartment,
    ELECTRICITY_METERS,
    GAS_METERS,
    METER_PLAIN_LABELS,
    RENT_REMINDER_AFTER_DAYS,
    RENT_REMINDER_DAY_BEFORE,
    RENT_REMINDER_ON_DAY,
    WATER_SUBMISSION_METERS,
    format_property_genitive,
    format_property_short,
    get_apartment_enabled_meters,
    parse_notify_intervals,
)
from services.access import get_notification_recipients, is_lease_active_for_apartment
from services.backup import create_backup
from services.calculations import fmt_date
from services.notifications import webapp_open_kb

logger = logging.getLogger(__name__)


def effective_submission_day(configured_day: int, year: int, month: int) -> int:
    return min(configured_day, calendar.monthrange(year, month)[1])


def subtract_months(dt: datetime.date, months: int) -> datetime.date:
    total = dt.year * 12 + (dt.month - 1) - months
    year, month_zero = divmod(total, 12)
    month = month_zero + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return datetime.date(year, month, day)


def verification_reminder_dates(due_date: datetime.date, intervals: list[str]) -> dict[str, datetime.date]:
    mapping: dict[str, datetime.date] = {}
    for iv in intervals:
        if iv == "6m":
            mapping[iv] = subtract_months(due_date, 6)
        elif iv == "3m":
            mapping[iv] = subtract_months(due_date, 3)
        elif iv == "1m":
            mapping[iv] = subtract_months(due_date, 1)
        elif iv == "1w":
            mapping[iv] = due_date - datetime.timedelta(days=7)
    return mapping


VERIFICATION_REMINDER_PHRASES = {
    "6m": "через полгода",
    "3m": "через 3 месяца",
    "1m": "через месяц",
    "1w": "через неделю",
}


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
            await bot.send_message(chat_id=uid, text=text, reply_markup=kb, parse_mode="HTML")
        except Exception as e:
            logger.warning("%s reminder failed for %s: %s", tab, uid, e)


async def check_meter_reminders(bot: Bot, today: datetime.date | None = None) -> None:
    cur = today or local_today()
    async with crud.UnitOfWork() as s:
        result = await s.execute(select(Apartment))
        apartments = result.scalars().all()

    for apt in apartments:
        enabled = set(get_apartment_enabled_meters(apt))
        prop_label = h(format_property_short(apt))

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
        agr_no = h(apt.agreement_no or "б/н")
        date_start = fmt_date(apt.agreement_date_start)
        prop_label = h(format_property_short(apt))
        msg_text = None

        if apt.agreement_date_finish == in_seven_days:
            msg_text = (
                f"⏳ Срок найма объекта {prop_label} по договору №{agr_no} от {date_start} "
                f"завершится через неделю. Не забудьте продлить договор либо удалить нанимателя."
            )
        elif apt.agreement_date_finish == cur:
            msg_text = (
                f"⚠️ {prop_label}: Договор найма №{agr_no} от {date_start} заканчивается сегодня. "
                f"В разделе управления недвижимостью укажите новые данные договора найма либо удалите "
                f"нанимателя, если он больше не проживает (доступ к боту у нанимателя будет прекращён)."
            )

        if msg_text:
            for uid in await get_notification_recipients(apt.id):
                try:
                    await bot.send_message(
                        chat_id=uid,
                        text=msg_text,
                        reply_markup=webapp_open_kb("📱 Открыть управление договором", tab="apartments"),
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.warning("Lease notice failed for %s: %s", uid, e)


async def check_meter_verifications(bot: Bot, today: datetime.date | None = None) -> None:
    cur = today or local_today()
    pairs = await crud.get_all_meter_devices_with_apartments()
    for dev, apt in pairs:
        if not dev.verification_due_date:
            continue
        enabled = set(get_apartment_enabled_meters(apt))
        is_unified_elec = dev.meter_type == "t" and any(em in enabled for em in ELECTRICITY_METERS)
        if dev.meter_type not in enabled and not is_unified_elec:
            continue
        intervals = parse_notify_intervals(dev.notify_intervals)
        if not intervals:
            continue

        targets = verification_reminder_dates(dev.verification_due_date, intervals)
        matched_phrase = None
        for iv, target_dt in targets.items():
            if target_dt == cur:
                matched_phrase = VERIFICATION_REMINDER_PHRASES[iv]
                break
        if matched_phrase is None and dev.verification_due_date == cur:
            matched_phrase = "сегодня"
        if matched_phrase is None:
            continue

        device_names = {
            "cold_water": "Водомер холодной воды",
            "hot_water": "Водомер горячей воды",
            "irrigation": "Поливной водомер",
            "t": "Электрический счётчик",
            "gas": "Газовый счётчик",
        }
        meter_name = h(device_names.get(dev.meter_type, METER_PLAIN_LABELS.get(dev.meter_type, dev.meter_type)))
        prop_label = h(format_property_short(apt))
        serial_part = f" (№ {h(dev.serial_number)})" if dev.serial_number else ""
        due_str = fmt_date(dev.verification_due_date)
        msg_text = (
            f"🔧 <b>Напоминание о поверке/замене счётчика</b>\n"
            f"Объект: <b>{prop_label}</b>\n"
            f"Счётчик: <b>{meter_name}</b>{serial_part}\n"
            f"Срок поверки истекает {matched_phrase} (<b>{due_str}</b>)."
        )
        for uid in await get_notification_recipients(apt.id):
            try:
                await bot.send_message(
                    chat_id=uid,
                    text=msg_text,
                    reply_markup=webapp_open_kb("🔧 Управление счётчиками", tab="apartments"),
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning("Verification reminder failed for %s: %s", uid, e)


def effective_payment_date(configured_day: int, year: int, month: int) -> datetime.date:
    """Resolve monthly payment date clamping 29/30/31 to the last day of short months."""
    return datetime.date(year, month, min(configured_day, calendar.monthrange(year, month)[1]))


def match_rent_payment_reminder(
    configured_day: int | None,
    mode: str | None,
    days_after: int | None,
    cur: datetime.date,
) -> datetime.date | None:
    """Return the matched monthly payment date if a reminder should be sent on `cur`, else None."""
    if not configured_day or not (1 <= configured_day <= 31) or not mode:
        return None
    if mode == RENT_REMINDER_DAY_BEFORE:
        tomorrow = cur + datetime.timedelta(days=1)
        pay_dt = effective_payment_date(configured_day, tomorrow.year, tomorrow.month)
        return pay_dt if pay_dt == tomorrow else None
    if mode == RENT_REMINDER_ON_DAY:
        pay_dt = effective_payment_date(configured_day, cur.year, cur.month)
        return pay_dt if pay_dt == cur else None
    if mode == RENT_REMINDER_AFTER_DAYS:
        if not days_after or not (1 <= days_after <= 20):
            return None
        candidate = cur - datetime.timedelta(days=days_after)
        pay_dt = effective_payment_date(configured_day, candidate.year, candidate.month)
        return pay_dt if pay_dt == candidate else None
    return None


async def check_rent_payment_reminders(bot: Bot, today: datetime.date | None = None) -> None:
    """Send monthly rent payment reminders at 15:00 (BOT_TZ)."""
    cur = today or local_today()
    async with crud.UnitOfWork() as s:
        result = await s.execute(
            select(Apartment).where(
                Apartment.rent_reminder_enabled.is_(True),
                Apartment.rent_payment_day.isnot(None),
            )
        )
        apartments = result.scalars().all()

    for apt in apartments:
        if not is_lease_active_for_apartment(apt):
            continue
        pay_dt = match_rent_payment_reminder(
            apt.rent_payment_day,
            apt.rent_reminder_mode,
            apt.rent_reminder_days_after,
            cur,
        )
        if pay_dt is None:
            continue

        tenants = [t for t in await crud.get_apartment_tenants(apt.id) if not t.is_blocked]
        recipients: list[int] = [t.telegram_id for t in tenants]

        for uid in await get_notification_recipients(apt.id):
            if uid not in recipients:
                recipients.append(uid)

        if not recipients:
            continue

        prop_gen = h(format_property_genitive(apt))
        pay_str = fmt_date(pay_dt)

        if apt.rent_reminder_mode == RENT_REMINDER_DAY_BEFORE:
            msg_text = (
                f"💳 <b>Напоминание об оплате найма</b>\n"
                f"Завтра (<b>{pay_str}</b>) наступает дата ежемесячного платежа "
                f"за наём <b>{prop_gen}</b>. Пожалуйста, не забудьте произвести оплату."
            )
        elif apt.rent_reminder_mode == RENT_REMINDER_ON_DAY:
            msg_text = (
                f"💳 <b>Напоминание об оплате найма</b>\n"
                f"Сегодня (<b>{pay_str}</b>) наступает дата ежемесячного платежа "
                f"за наём <b>{prop_gen}</b>. Пожалуйста, произведите оплату."
            )
        else:
            msg_text = (
                f"⚠️ <b>Напоминание об оплате найма</b>\n"
                f"Напоминаем о необходимости произвести ежемесячную плату "
                f"за наём <b>{prop_gen}</b> (дата ежемесячного платежа: <b>{pay_str}</b>, "
                f"прошло дней: <b>{apt.rent_reminder_days_after}</b>)."
            )

        for uid in recipients:
            try:
                await bot.send_message(chat_id=uid, text=msg_text, parse_mode="HTML")
            except Exception as e:
                logger.warning("Rent payment reminder failed for %s: %s", uid, e)


async def _run_job_once_today(job_key: str, fn, *args, run_date: datetime.date | None = None, **kwargs) -> bool:
    cur = run_date or local_today()
    if await crud.has_scheduler_run(job_key, cur):
        return False
    await fn(*args, **kwargs)
    await crud.record_scheduler_run(job_key, cur)
    return True


async def run_daily_meter_reminders(bot: Bot) -> None:
    await _run_job_once_today("meter_reminders", check_meter_reminders, bot)


async def run_daily_lease_expirations(bot: Bot) -> None:
    await _run_job_once_today("lease_expirations", check_lease_expirations, bot)


async def run_daily_meter_verifications(bot: Bot) -> None:
    await _run_job_once_today("meter_verifications", check_meter_verifications, bot)


async def run_daily_rent_payment_reminders(bot: Bot) -> None:
    await _run_job_once_today("rent_payment_reminders", check_rent_payment_reminders, bot)


async def run_daily_backup() -> None:
    await _run_job_once_today("daily_backup", create_backup, skip_if_unchanged=True)


async def run_missed_jobs_on_startup(bot: Bot, now: datetime.datetime | None = None) -> list[str]:
    current = now or local_now()
    today = current.date()
    executed: list[str] = []

    schedule_plan = (
        ("daily_backup", 3, 0, create_backup, (), {"skip_if_unchanged": True}),
        ("meter_reminders", 15, 0, check_meter_reminders, (bot,), {"today": today}),
        ("lease_expirations", 15, 0, check_lease_expirations, (bot,), {"today": today}),
        ("meter_verifications", 15, 0, check_meter_verifications, (bot,), {"today": today}),
        ("rent_payment_reminders", 15, 0, check_rent_payment_reminders, (bot,), {"today": today}),
    )
    for job_key, hour, minute, fn, args, kwargs in schedule_plan:
        if (current.hour, current.minute) >= (hour, minute):
            try:
                ran = await _run_job_once_today(job_key, fn, *args, run_date=today, **kwargs)
                if ran:
                    executed.append(job_key)
            except Exception as e:
                logger.warning("Startup catch-up failed for %s: %s", job_key, e)
    if executed:
        logger.info("Executed startup catch-up jobs: %s", ", ".join(executed))
    return executed


def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=BOT_TZ)
    scheduler.add_job(
        run_daily_meter_reminders,
        trigger="cron", hour=15, minute=0, misfire_grace_time=3600,
        kwargs={"bot": bot}, id="meter_reminders", replace_existing=True,
    )
    scheduler.add_job(
        run_daily_lease_expirations,
        trigger="cron", hour=15, minute=0, misfire_grace_time=3600,
        kwargs={"bot": bot}, id="lease_expirations", replace_existing=True,
    )
    scheduler.add_job(
        run_daily_meter_verifications,
        trigger="cron", hour=15, minute=0, misfire_grace_time=3600,
        kwargs={"bot": bot}, id="meter_verifications", replace_existing=True,
    )
    scheduler.add_job(
        run_daily_rent_payment_reminders,
        trigger="cron", hour=15, minute=0, misfire_grace_time=3600,
        kwargs={"bot": bot}, id="rent_payment_reminders", replace_existing=True,
    )
    scheduler.add_job(
        run_daily_backup,
        trigger="cron", hour=3, minute=0, misfire_grace_time=3600,
        id="daily_backup", replace_existing=True,
    )
    return scheduler

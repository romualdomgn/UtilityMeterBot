from datetime import date, datetime
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from config import TZ
from database import SessionLocal, create_db_backup
from models import Apartment, MeterReading, User


def is_reminder_due(today_day: int, target_day: int) -> tuple[bool, bool]:
    if today_day == target_day:
        return True, False
    if today_day > target_day and (today_day - target_day) % 3 == 0:
        return True, True
    return False, False


def build_lease_expiry_notice(apt: Apartment, today: date) -> str | None:
    if not apt.has_tenant or not apt.agreement_date_finish:
        return None

    days_left = (apt.agreement_date_finish - today).days
    agr_no = apt.agreement_no or "б/н"
    agr_start = apt.agreement_date_start.strftime("%d.%m.%Y") if apt.agreement_date_start else "не указана"

    if days_left == 7:
        return (
            f"⏳ Срок найма квартиры №{apt.number} по договору №{agr_no} от {agr_start} "
            "завершится через неделю. Не забудьте продлить договор либо удалить нанимателя из бота"
        )
    if days_left == 0:
        return (
            f"⚠️ Срок найма квартиры №{apt.number} по договору №{agr_no} от {agr_start} "
            "завершился. Не забудьте продлить договор либо удалить нанимателя из бота"
        )
    return None


def get_family_owner_ids(all_users: list[User], family_id: int | None) -> set[int]:
    """Возвращает ID администраторов и владельцев ТОЛЬКО указанной семьи."""
    result: set[int] = set()
    for u in all_users:
        if not u.is_active:
            continue
        if u.role == "admin":
            result.add(u.telegram_id)
        elif u.role == "landlord" and u.family_id == family_id:
            result.add(u.telegram_id)
    return result


async def check_and_send_reminders(bot: Bot) -> None:
    now = datetime.now(TZ)
    today = now.date()
    period = now.strftime("%Y-%m")

    async with SessionLocal() as session:
        apts = (await session.execute(select(Apartment))).scalars().all()
        all_users = list((await session.execute(select(User).where(User.is_active.is_(True)))).scalars().all())

        for apt in apts:
            family_owners = get_family_owner_ids(all_users, apt.family_id)

            # 1. Уведомление об окончании договора (только админу и владельцам этой семьи)
            lease_notice = build_lease_expiry_notice(apt, today)
            if lease_notice and family_owners:
                lease_kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="🔄 Продлить / Изменить договор",
                                callback_data=f"usr:edit:{apt.id}:wiz_agreement",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="👥 Договор и жильцы",
                                callback_data=f"usr:open:{apt.id}",
                            )
                        ],
                    ]
                )
                for tg_id in family_owners:
                    try:
                        await bot.send_message(chat_id=tg_id, text=lease_notice, reply_markup=lease_kb)
                    except Exception:
                        pass

            # 2. Проверка подачи показаний счётчиков
            reading = (
                await session.execute(
                    select(MeterReading).where(
                        MeterReading.apartment_id == apt.id,
                        MeterReading.period == period,
                    )
                )
            ).scalar_one_or_none()

            water_ok = bool(reading and reading.water_submitted)
            elec_ok = bool(reading and reading.elec_submitted)

            water_due, water_overdue = is_reminder_due(today.day, apt.water_day)
            elec_due, elec_overdue = is_reminder_due(today.day, apt.elec_day)

            messages_to_send: list[str] = []
            if not water_ok and water_due:
                if water_overdue:
                    messages_to_send.append(
                        f"⚠️ <b>Внимание! Показания водомеров (кв. №{apt.number}) не были поданы в срок!</b>"
                    )
                else:
                    messages_to_send.append(
                        f"💧 <b>Пора подавать показания водомеров (кв. №{apt.number})!</b>"
                    )

            if not elec_ok and elec_due:
                if elec_overdue:
                    messages_to_send.append(
                        f"⚠️ <b>Внимание! Показания электрического счётчика (кв. №{apt.number}) не были поданы в срок!</b>"
                    )
                else:
                    messages_to_send.append(
                        f"⚡ <b>Пора подавать показания электрического счётчика (кв. №{apt.number})!</b>"
                    )

            if not messages_to_send:
                continue

            recipients: set[int] = set(family_owners)
            for u in all_users:
                if u.role == "tenant" and u.apartment_id == apt.id and apt.has_tenant:
                    if not apt.agreement_date_finish or today <= apt.agreement_date_finish:
                        recipients.add(u.telegram_id)

            btn_rows = []
            if not water_ok and water_due:
                btn_rows.append(
                    [InlineKeyboardButton(text="💧 Подать показания водомеров", callback_data=f"read:open:{apt.id}:water")]
                )
            if not elec_ok and elec_due:
                btn_rows.append(
                    [InlineKeyboardButton(text="⚡ Подать показания электроэнергии", callback_data=f"read:open:{apt.id}:elec")]
                )
            kb = InlineKeyboardMarkup(inline_keyboard=btn_rows)

            full_text = "\n\n".join(messages_to_send)
            for tg_id in recipients:
                try:
                    await bot.send_message(chat_id=tg_id, text=full_text, reply_markup=kb, parse_mode="HTML")
                except Exception:
                    pass


def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(check_and_send_reminders, "cron", hour=15, minute=0, args=[bot], id="daily_meter_reminders")
    scheduler.add_job(create_db_backup, "cron", hour=3, minute=30, id="nightly_db_backup")
    return scheduler

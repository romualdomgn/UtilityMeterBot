"""Telegram bot notification helpers called from Web App API and handlers."""

from __future__ import annotations

import logging
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from config import WEBAPP_URL
from database import crud
from services.access import get_notification_recipients
from services.calculations import fmt_date, format_reading_notification

logger = logging.getLogger(__name__)


def webapp_open_kb(label: str = "📱 Открыть панель счётчиков", tab: str | None = None) -> InlineKeyboardMarkup:
    url = f"{WEBAPP_URL}/?tab={tab}" if tab else WEBAPP_URL
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, web_app=WebAppInfo(url=url))],
    ])


async def notify_new_readings(
    bot: Bot,
    apt_id: int,
    meters: dict[str, int],
    prev_meters: dict[str, int],
    submitted_by: int,
) -> None:
    apt = await crud.get_apartment(apt_id)
    if not apt:
        return
    recipients = [r for r in await get_notification_recipients(apt_id) if r != submitted_by]
    if not recipients:
        return

    lines = [f"📊 <b>Новые показания — Кв. №{apt.number}</b>\n"]
    for mt, new_val in meters.items():
        prev_val = prev_meters.get(mt, 0)
        lines.append(format_reading_notification(apt.number, mt, prev_val, new_val))
    text = "\n".join(lines)

    for uid in recipients:
        try:
            await bot.send_message(chat_id=uid, text=text, parse_mode="HTML")
        except Exception as e:
            logger.warning("Failed to notify %s: %s", uid, e)


async def notify_tenant_added(bot: Bot, apt, tenant_tg_id: int, tenant_name: str) -> None:
    agr_no = apt.agreement_no or "б/н"
    date_start = fmt_date(apt.agreement_date_start)
    date_finish = fmt_date(apt.agreement_date_finish)
    try:
        await bot.send_message(
            chat_id=tenant_tg_id,
            text=(
                f"👋 Здравствуйте, <b>{tenant_name}</b>!\n\n"
                f"Вы добавлены как наниматель квартиры №<b>{apt.number}</b> "
                f"(договор №<b>{agr_no}</b> от <b>{date_start}</b> "
                f"со сроком действия до <b>{date_finish}</b>).\n"
                f"Откройте Mini App кнопкой ниже для подачи показаний."
            ),
            reply_markup=webapp_open_kb("📱 Открыть панель счётчиков"),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("Failed to send tenant invite to %s: %s", tenant_tg_id, e)


async def notify_tenant_removed(bot: Bot, tenant_tg_id: int) -> None:
    try:
        await bot.send_message(
            chat_id=tenant_tg_id,
            text="Ваш Telegram ID был удалён из бота и доступ к боту прекращён.",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("Failed to send removal notice to %s: %s", tenant_tg_id, e)


async def forward_meter_photo_file(
    bot: Bot,
    apt_id: int,
    filepath: Path,
    sender_tg_id: int,
    sender_name: str,
    comment: str = "",
) -> int:
    apt = await crud.get_apartment(apt_id)
    if not apt:
        return 0
    recipients = [r for r in await get_notification_recipients(apt_id) if r != sender_tg_id]
    caption = f"📷 Фото счётчиков — <b>Кв. №{apt.number}</b>\nОт: {sender_name} (ID: {sender_tg_id})"
    if comment.strip():
        caption += f"\n💬 {comment.strip()}"

    sent = 0
    for uid in recipients:
        try:
            await bot.send_photo(
                chat_id=uid,
                photo=FSInputFile(filepath),
                caption=caption,
                parse_mode="HTML",
            )
            sent += 1
        except Exception as e:
            logger.warning("Failed to send meter photo to %s: %s", uid, e)
    return sent


async def send_document_to_user(bot: Bot, telegram_id: int, filepath: Path, caption: str) -> None:
    await bot.send_document(
        chat_id=telegram_id,
        document=FSInputFile(filepath),
        caption=caption,
        parse_mode="HTML",
    )

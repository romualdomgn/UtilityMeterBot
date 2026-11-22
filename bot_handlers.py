"""Minimal Telegram chat handlers: Mini App launcher + direct meter photo forwarding."""

from __future__ import annotations

import datetime
import logging

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import MenuButtonWebApp, Message, WebAppInfo

from config import PHOTOS_DIR, WEBAPP_URL
from database import crud
from services.access import check_user_access, get_accessible_apartments, get_user_role
from services.notifications import forward_meter_photo_file, webapp_open_kb

logger = logging.getLogger(__name__)
router = Router()

ROLE_TITLES = {
    "admin": "Администратор",
    "landlord": "Владелец",
    "tenant": "Наниматель",
}


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    tid = message.from_user.id
    allowed, reason = await check_user_access(tid)
    if not allowed:
        await message.answer(reason or "⛔ Доступ запрещён.", parse_mode="HTML")
        return

    role = await get_user_role(tid)
    try:
        await message.bot.set_chat_menu_button(
            chat_id=message.chat.id,
            menu_button=MenuButtonWebApp(
                text="Счётчики",
                web_app=WebAppInfo(url=WEBAPP_URL),
            ),
        )
    except Exception as e:
        logger.debug("Could not set chat menu button: %s", e)

    role_label = ROLE_TITLES.get(role, role)
    await message.answer(
        f"<b>🏠 Учёт показаний счётчиков</b>\n\n"
        f"Ваша роль: <b>{role_label}</b>\n"
        f"Нажмите кнопку ниже, чтобы открыть панель управления.\n\n"
        f"<i>В этот чат будут приходить напоминания, уведомления, фото счётчиков и выгрузки (Excel/PDF/бэкапы).</i>",
        reply_markup=webapp_open_kb("📱 Открыть Mini App"),
        parse_mode="HTML",
    )


@router.message(F.photo)
async def on_direct_photo(message: Message) -> None:
    tid = message.from_user.id
    allowed, reason = await check_user_access(tid)
    if not allowed:
        await message.answer(reason or "⛔ Доступ запрещён.", parse_mode="HTML")
        return

    apts = await get_accessible_apartments(tid)
    if not apts:
        await message.answer("У вас нет привязанных квартир.", parse_mode="HTML")
        return

    apt = apts[0]
    photo = message.photo[-1]
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = PHOTOS_DIR / f"apt{apt.id}_{tid}_{ts}.jpg"
    await message.bot.download(photo, destination=filepath)

    user = await crud.get_user_by_telegram_id(tid)
    sender_name = (user.name if user and user.name else message.from_user.full_name) or str(tid)
    sent = await forward_meter_photo_file(
        bot=message.bot,
        apt_id=apt.id,
        filepath=filepath,
        sender_tg_id=tid,
        sender_name=sender_name,
        comment=message.caption or "",
    )
    await crud.add_audit_log(tid, "upload_photo_chat", f"Кв. №{apt.number}, получателей: {sent}")
    await message.answer(f"✅ Фото счётчика для кв. №{apt.number} отправлено владельцам ({sent}).")

"""Minimal Telegram chat handlers: Mini App launcher, About info, and direct meter photo forwarding."""

from __future__ import annotations

import logging
from html import escape as h

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, LinkPreviewOptions, MenuButtonWebApp, Message, WebAppInfo

import config
from database import crud
from database.models import format_property_short
from services.access import get_accessible_apartments, resolve_user_access
from services.notifications import build_webapp_url, forward_meter_photo_file, webapp_open_kb

logger = logging.getLogger(__name__)
router = Router()

ROLE_TITLES = {
    "admin": "Администратор",
    "landlord": "Владелец",
    "tenant": "Наниматель",
}


def build_about_text(
    repository: str | None = None,
    tg_username: str | None = None,
) -> str:
    """Format 'About app' message with clickable repository and Telegram creator links."""
    repo_raw = (config.REPOSITORY if repository is None else repository).strip()
    if repo_raw:
        repo_href = repo_raw if repo_raw.startswith(("http://", "https://")) else f"https://{repo_raw}"
        repo_html = f'<a href="{h(repo_href, quote=True)}">{h(repo_raw)}</a>'
    else:
        repo_html = "не указан"

    tg_raw = (config.TG_USERNAME if tg_username is None else tg_username).strip()
    if tg_raw:
        handle = tg_raw
        for prefix in ("https://t.me/", "http://t.me/", "t.me/", "@"):
            if handle.lower().startswith(prefix):
                handle = handle[len(prefix):]
        handle = handle.strip().lstrip("@").strip("/")
        if handle:
            tg_html = f'<a href="https://t.me/{h(handle, quote=True)}">@{h(handle)}</a>'
        else:
            tg_html = h(tg_raw)
    else:
        tg_html = "не указан"

    return (
        f"Данный бот это приложение с открытым исходным кодом. "
        f"Репозиторий находится по адресу {repo_html}.\n"
        f"Предложения по улучшению дизайна и юзабилити (UX/UI) направлять создателю и администратору бота {tg_html}"
    )


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    tid = message.from_user.id
    allowed, reason, role, _ = await resolve_user_access(tid)
    if not allowed:
        await message.answer(reason or "⛔ Доступ запрещён.", parse_mode="HTML")
        return

    try:
        await message.bot.set_chat_menu_button(
            chat_id=message.chat.id,
            menu_button=MenuButtonWebApp(
                text="Счётчики",
                web_app=WebAppInfo(url=build_webapp_url()),
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
        reply_markup=webapp_open_kb("📱 Открыть Mini App", include_about=True),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "about_app")
async def on_about_app(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            build_about_text(),
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )


@router.message(F.photo)
async def on_direct_photo(message: Message) -> None:
    tid = message.from_user.id
    allowed, reason, _, user = await resolve_user_access(tid)
    if not allowed:
        await message.answer(reason or "⛔ Доступ запрещён.", parse_mode="HTML")
        return

    apts = await get_accessible_apartments(tid)
    if not apts:
        await message.answer("У вас нет привязанных объектов недвижимости.", parse_mode="HTML")
        return

    if len(apts) > 1:
        await message.answer(
            "У вас доступно несколько объектов недвижимости. Откройте Mini App, чтобы выбрать нужный объект перед отправкой фото:",
            reply_markup=webapp_open_kb("📷 Выбрать объект и отправить фото", tab="photo"),
            parse_mode="HTML",
        )
        return

    apt = apts[0]
    prop_label = format_property_short(apt)
    photo = message.photo[-1]

    sender_name = (user.name if user and user.name else message.from_user.full_name) or str(tid)
    sent = await forward_meter_photo_file(
        bot=message.bot,
        apt_id=apt.id,
        filepath=None,
        sender_tg_id=tid,
        sender_name=sender_name,
        comment=message.caption or "",
        photo_file_id=photo.file_id,
    )
    await crud.add_audit_log(tid, "upload_photo_chat", f"{prop_label}, получателей: {sent}")
    if sent > 0:
        await message.answer(f"✅ Фото счётчика ({h(prop_label)}) отправлено владельцам ({sent}).", parse_mode="HTML")
    else:
        await message.answer(
            f"⚠️ Получателей уведомлений для {h(prop_label)} не найдено — фото не было доставлено.",
            parse_mode="HTML",
        )

"""Telegram bot notification helpers with HTML escaping, direct photo forwarding, and managed background tasks."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from html import escape as h
from pathlib import Path
from typing import Coroutine

from aiogram import Bot
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

import config
from config import BASE_DIR, WEBAPP_URL
from database import crud
from database.models import format_property_short, format_property_title, resolve_user_display_name
from services.access import get_notification_recipients, is_admin
from services.calculations import fmt_date, format_reading_notification

logger = logging.getLogger(__name__)


class BackgroundTaskManager:
    """MEDIUM-08: tracked background task runner with error logging and graceful shutdown."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()

    def spawn(self, coro: Coroutine, name: str = "bg_notification") -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)

        def _done_cb(t: asyncio.Task) -> None:
            self._tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                logger.warning("Background task %s failed: %s", t.get_name(), exc)

        task.add_done_callback(_done_cb)
        return task

    async def wait_all(self, timeout: float = 10.0) -> None:
        if not self._tasks:
            return
        pending = list(self._tasks)
        _, still_pending = await asyncio.wait(pending, timeout=timeout)
        if still_pending:
            for t in still_pending:
                t.cancel()
            await asyncio.gather(*still_pending, return_exceptions=True)


async def dispatch_notification(request, coro: Coroutine, name: str = "notification") -> None:
    mgr = getattr(getattr(getattr(request, "app", None), "state", None), "bg_tasks", None)
    if isinstance(mgr, BackgroundTaskManager):
        mgr.spawn(coro, name=name)
    else:
        await coro


def get_webapp_version() -> str:
    static_dir = BASE_DIR / "webapp" / "static"
    hasher = hashlib.sha256()
    for name in ("index.html", "style.css", "app.js"):
        p = static_dir / name
        try:
            hasher.update(p.read_bytes())
        except OSError:
            pass
    return hasher.hexdigest()[:10]


def build_webapp_url(tab: str | None = None, apt_id: int | None = None) -> str:
    ver = get_webapp_version()
    params = [f"v={ver}"]
    if tab:
        params.append(f"tab={tab}")
    if apt_id is not None:
        params.append(f"apt_id={int(apt_id)}")
    return f"{WEBAPP_URL}/?{'&'.join(params)}"


def webapp_open_kb(
    label: str = "📱 Открыть панель счётчиков",
    tab: str | None = None,
    include_about: bool = False,
    apt_id: int | None = None,
) -> InlineKeyboardMarkup:
    url = build_webapp_url(tab=tab, apt_id=apt_id)
    rows = [
        [InlineKeyboardButton(text=label, web_app=WebAppInfo(url=url))],
    ]
    if include_about:
        rows.append([InlineKeyboardButton(text="ℹ️ О приложении", callback_data="about_app")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def notify_new_readings(
    bot: Bot,
    apt_id: int,
    meters: dict[str, int],
    prev_meters: dict[str, int | None],
    submitted_by: int,
) -> int:
    apt = await crud.get_apartment(apt_id)
    if not apt:
        return 0
    recipients = [r for r in await get_notification_recipients(apt_id) if r != submitted_by]
    if not recipients:
        return 0

    prop_label = format_property_short(apt)
    lines = [f"📊 <b>Новые показания — {h(prop_label)}</b>\n"]
    for mt, new_val in meters.items():
        prev_val = prev_meters.get(mt)
        lines.append(format_reading_notification(apt.number, mt, prev_val, new_val))
    text = "\n".join(lines)

    sent = 0
    for uid in recipients:
        try:
            await bot.send_message(chat_id=uid, text=text, parse_mode="HTML")
            sent += 1
        except Exception as e:
            logger.warning("Failed to notify %s: %s", uid, e)
    return sent


async def notify_tenant_added(bot: Bot, apt, tenant_tg_id: int, tenant_name: str) -> bool:
    agr_no = h(apt.agreement_no or "б/н")
    date_start = fmt_date(apt.agreement_date_start)
    date_finish = fmt_date(apt.agreement_date_finish)
    prop_title = h(format_property_title(apt))
    try:
        await bot.send_message(
            chat_id=tenant_tg_id,
            text=(
                f"👋 Здравствуйте, <b>{h(tenant_name)}</b>!\n\n"
                f"Вы добавлены как наниматель объекта <b>{prop_title}</b> "
                f"(договор №<b>{agr_no}</b> от <b>{date_start}</b> "
                f"со сроком действия до <b>{date_finish}</b>).\n"
                f"Откройте Mini App кнопкой ниже для подачи показаний."
            ),
            reply_markup=webapp_open_kb("📱 Открыть панель счётчиков", apt_id=getattr(apt, "id", None)),
            parse_mode="HTML",
        )
        return True
    except Exception as e:
        logger.warning("Failed to send tenant invite to %s: %s", tenant_tg_id, e)
        return False


async def notify_tenant_removed(
    bot: Bot,
    tenant_tg_id: int,
    apt_number: str | None = None,
    fully_removed: bool = True,
) -> bool:
    if fully_removed:
        text = "Ваш Telegram ID был удалён из бота и доступ к боту прекращён."
    else:
        text = f"Вы откреплены от объекта <b>{h(apt_number or '')}</b>. Доступ к остальным привязанным объектам сохранён."
    try:
        await bot.send_message(chat_id=tenant_tg_id, text=text, parse_mode="HTML")
        return True
    except Exception as e:
        logger.warning("Failed to send removal notice to %s: %s", tenant_tg_id, e)
        return False


async def forward_meter_photo_detailed(
    bot: Bot,
    apt_id: int,
    filepath: Path | None,
    sender_tg_id: int,
    sender_name: str,
    comment: str = "",
    photo_file_id: str | None = None,
) -> dict[str, int]:
    """P2-11: forward meter photo directly to owners and report attempted/sent/failed counts."""
    apt = await crud.get_apartment(apt_id)
    if not apt:
        return {"attempted": 0, "sent": 0, "failed": 0}
    recipients = [r for r in await get_notification_recipients(apt_id) if r != sender_tg_id]
    if not recipients:
        return {"attempted": 0, "sent": 0, "failed": 0}

    safe_comment = h(comment.strip()[:500])
    sender_user = await crud.get_user_by_telegram_id(sender_tg_id)
    prop_short = h(format_property_short(apt))

    sent = 0
    failed = 0
    for uid in recipients:
        viewer_role = "admin" if is_admin(uid) else "landlord"
        resolved_sender = (
            resolve_user_display_name(sender_user, viewer_role=viewer_role, viewer_tg_id=uid)
            if sender_user
            else None
        ) or sender_name
        caption = (
            f"📷 Фото счётчиков — <b>{prop_short}</b>\n"
            f"От: {h(resolved_sender)} (ID: {sender_tg_id})"
        )
        if safe_comment:
            caption += f"\n💬 {safe_comment}"
        try:
            photo_payload = photo_file_id if photo_file_id else FSInputFile(filepath)
            await bot.send_photo(
                chat_id=uid,
                photo=photo_payload,
                caption=caption,
                parse_mode="HTML",
            )
            sent += 1
        except Exception as e:
            failed += 1
            logger.warning("Failed to send meter photo to %s: %s", uid, e)
    return {"attempted": len(recipients), "sent": sent, "failed": failed}


async def forward_meter_photo_file(
    bot: Bot,
    apt_id: int,
    filepath: Path | None,
    sender_tg_id: int,
    sender_name: str,
    comment: str = "",
    photo_file_id: str | None = None,
) -> int:
    result = await forward_meter_photo_detailed(
        bot=bot,
        apt_id=apt_id,
        filepath=filepath,
        sender_tg_id=sender_tg_id,
        sender_name=sender_name,
        comment=comment,
        photo_file_id=photo_file_id,
    )
    return result["sent"]


async def send_document_to_user(bot: Bot, telegram_id: int, filepath: Path, caption: str) -> None:
    await bot.send_document(
        chat_id=telegram_id,
        document=FSInputFile(filepath),
        caption=caption,
        parse_mode="HTML",
    )


async def notify_admin_new_family_created(
    bot: Bot,
    creator_tg_id: int,
    creator_name: str,
    family_name: str,
    owner_name: str,
    owner_tg_id: int,
) -> bool:
    """Notify ADMIN_TELEGRAM_ID whenever a new family and initial owner are created."""
    admin_id = config.ADMIN_TELEGRAM_ID
    if not admin_id:
        return False

    actor_user = await crud.get_user_by_telegram_id(creator_tg_id)
    resolved_creator = (
        resolve_user_display_name(actor_user, viewer_role="admin", viewer_tg_id=admin_id)
        if actor_user
        else None
    ) or creator_name

    text = (
        f"🆕 <b>Добавлена новая семья и новый владелец</b>\n\n"
        f"👤 Кто добавил: <b>{h(resolved_creator)}</b> (ID: <code>{creator_tg_id}</code>)\n"
        f"👨‍👩‍👧‍👦 Новая семья: <b>{h(family_name)}</b>\n"
        f"🔑 Новый владелец: <b>{h(owner_name)}</b> (ID: <code>{owner_tg_id}</code>)"
    )
    try:
        await bot.send_message(chat_id=admin_id, text=text, parse_mode="HTML")
        return True
    except Exception as e:
        logger.warning("Failed to notify admin %s about new family: %s", admin_id, e)
        return False

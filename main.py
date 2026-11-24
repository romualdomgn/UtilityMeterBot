"""Unified entry point: FastAPI Web App + aiogram Bot + APScheduler."""

from __future__ import annotations

import asyncio
import logging
import sys

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, MenuButtonWebApp, WebAppInfo

from bot_handlers import router as bot_router
from config import (
    ADMIN_TELEGRAM_ID,
    TELEGRAM_BOT_TOKEN,
    WEBAPP_HOST,
    WEBAPP_PORT,
    WEBAPP_URL,
)
from database import crud
from database.session import init_db
from services.notifications import build_webapp_url
from services.scheduler import setup_scheduler
from webapp.api import create_web_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


async def seed_admin() -> None:
    """CRITICAL-02 & MEDIUM-07: sync only ADMIN_TELEGRAM_ID and revoke stale admins."""
    if not ADMIN_TELEGRAM_ID:
        logger.warning(
            "ADMIN_TELEGRAM_ID не задан (равен 0) — административные функции будут недоступны."
        )
        return
    await crud.sync_admin_user(ADMIN_TELEGRAM_ID)


async def setup_bot_ui(bot: Bot) -> None:
    await bot.set_my_commands([
        BotCommand(command="start", description="Открыть панель счётчиков"),
    ])
    menu_url = build_webapp_url()
    menu_btn = MenuButtonWebApp(
        text="Счётчики",
        web_app=WebAppInfo(url=menu_url),
    )
    try:
        await bot.set_chat_menu_button(menu_button=menu_btn)
    except Exception as e:
        logger.warning("Could not set default menu button: %s", e)

    # Also refresh per-chat menu buttons so existing users immediately get the new ?v= URL
    chat_ids: set[int] = set()
    if ADMIN_TELEGRAM_ID:
        chat_ids.add(ADMIN_TELEGRAM_ID)
    try:
        for u in await crud.get_all_users():
            if not u.is_blocked and u.role in ("admin", "landlord", "tenant"):
                chat_ids.add(u.telegram_id)
    except Exception as e:
        logger.debug("Could not fetch users for menu button sync: %s", e)

    for cid in chat_ids:
        try:
            await bot.set_chat_menu_button(chat_id=cid, menu_button=menu_btn)
        except Exception as e:
            logger.debug("Could not refresh menu button for chat %s: %s", cid, e)


async def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN в файле .env")

    await init_db()
    await seed_admin()

    bot = Bot(
        token=TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(bot_router)

    await setup_bot_ui(bot)
    scheduler = setup_scheduler(bot)
    scheduler.start()

    web_app = create_web_app(bot)
    uvicorn_cfg = uvicorn.Config(
        web_app,
        host=WEBAPP_HOST,
        port=WEBAPP_PORT,
        log_level="info",
    )
    server = uvicorn.Server(uvicorn_cfg)

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Utility Meter Web App + Bot started on %s:%s (%s)", WEBAPP_HOST, WEBAPP_PORT, WEBAPP_URL)

    polling_task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    server_task = asyncio.create_task(server.serve())

    try:
        done, pending = await asyncio.wait(
            {polling_task, server_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            if exc := task.exception():
                raise exc
    finally:
        server.should_exit = True
        for task in (polling_task, server_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(polling_task, server_task, return_exceptions=True)
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

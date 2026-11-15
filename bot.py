import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeDefault, MenuButtonCommands

from config import BOT_TOKEN
from database import init_db
from handlers import router, sync_all_users_menus
from scheduler import setup_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


async def setup_default_menu(bot: Bot) -> None:
    """Активирует синюю кнопку Menu и задаёт базовый список опций."""
    default_commands = [
        BotCommand(command="start", description="🏠 Главное табло (Текущий расход)"),
        BotCommand(command="readings", description="📝 Внести показания счётчиков"),
        BotCommand(command="history", description="📅 История и сравнение месяцев"),
        BotCommand(command="info", description="ℹ️ Общая информация"),
    ]
    await bot.set_my_commands(commands=default_commands, scope=BotCommandScopeDefault())
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    await sync_all_users_menus(bot)


async def main() -> None:
    await init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await setup_default_menu(bot)

    scheduler = setup_scheduler(bot)
    scheduler.start()

    logging.info("Utility Meter Bot v.2 запущен (Single-Message UI + Menu Button)")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

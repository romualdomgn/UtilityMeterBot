from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable
from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    Message,
    TelegramObject,
)
from sqlalchemy import desc, func, select

from config import TZ
from database import (
    SessionLocal,
    calculate_consumption_and_cost,
    create_db_backup,
    get_authorized_user,
    get_previous_baseline,
    log_audit,
    parse_decimal,
    parse_int_reading,
)
from exports import generate_excel_report, generate_pdf_report
from models import Apartment, AuditLog, Family, MeterReading, TariffHistory, User
from ui import (
    WIZARD_CFG_ORDER,
    build_main_dashboard,
    fmt_dec,
    render_admin_panel,
    render_apartment_settings,
    render_apartments_menu,
    render_families_menu,
    render_history_panel,
    render_owner_info,
    render_photo_panel,
    render_reading_panel,
    render_tenant_info,
    render_to_panel,
    render_users_panel,
    safe_delete_message,
)

router = Router()


class PanelStates(StatesGroup):
    managing_family = State()
    creating_apartment = State()
    editing_cfg_field = State()
    editing_usr_field = State()
    entering_reading = State()
    sending_photo = State()


def _get_commands_for_role(role: str) -> list[BotCommand]:
    if role == "admin":
        return [
            BotCommand(command="start", description="🏠 Главное меню"),
            BotCommand(command="families", description="👨‍👩‍👧‍👦 Семьи"),
            BotCommand(command="apartments", description="🏢 Квартиры"),
            BotCommand(command="water", description="💧 Подать показания водомеров"),
            BotCommand(command="elec", description="⚡ Подать показания электроэнергии"),
            BotCommand(command="history", description="📅 История и сравнение месяцев"),
            BotCommand(command="users", description="👥 Договор и жильцы"),
            BotCommand(command="export", description="📥 Выгрузка в Excel / PDF"),
            BotCommand(command="admin", description="🛡 Администрирование и аудит"),
            BotCommand(command="info", description="ℹ️ Общая информация"),
        ]
    if role == "landlord":
        return [
            BotCommand(command="start", description="🏠 Главное меню"),
            BotCommand(command="apartments", description="🏢 Квартиры"),
            BotCommand(command="water", description="💧 Подать показания водомеров"),
            BotCommand(command="elec", description="⚡ Подать показания электроэнергии"),
            BotCommand(command="history", description="📅 История и сравнение месяцев"),
            BotCommand(command="users", description="👥 Договор и жильцы"),
            BotCommand(command="export", description="📥 Выгрузка в Excel / PDF"),
            BotCommand(command="info", description="ℹ️ Общая информация"),
        ]
    return [
        BotCommand(command="start", description="🏠 Главное меню"),
        BotCommand(command="water", description="💧 Подать показания водомеров"),
        BotCommand(command="elec", description="⚡ Подать показания электроэнергии"),
        BotCommand(command="photo", description="📷 Отправить фото счётчиков"),
        BotCommand(command="history", description="📅 История потребления по месяцам"),
        BotCommand(command="info", description="ℹ️ Общая информация"),
    ]


async def set_user_menu_commands(bot: Bot, user_id: int, role: str) -> None:
    try:
        await bot.set_my_commands(
            commands=_get_commands_for_role(role),
            scope=BotCommandScopeChat(chat_id=user_id),
        )
        await bot.set_chat_menu_button(chat_id=user_id, menu_button=MenuButtonCommands())
    except Exception:
        pass


async def sync_all_users_menus(bot: Bot) -> None:
    async with SessionLocal() as session:
        users = (await session.execute(select(User).where(User.is_active.is_(True)))).scalars().all()
        for u in users:
            await set_user_menu_commands(bot, u.telegram_id, u.role)


def _can_access_apartment(user: User, apt: Apartment | None) -> bool:
    """Строгая серверная проверка изоляции квартир между семьями."""
    if not apt:
        return False
    if user.role == "admin":
        return True
    if user.role == "landlord":
        return user.family_id is not None and apt.family_id == user.family_id
    if user.role == "tenant":
        return user.apartment_id == apt.id
    return False


async def _get_family_recipient_ids(session, family_id: int | None, exclude_user_id: int | None = None) -> set[int]:
    """Возвращает Telegram ID администраторов и владельцев ТОЛЬКО указанной семьи."""
    users = (
        await session.execute(
            select(User).where(User.role.in_(["admin", "landlord"]), User.is_active.is_(True))
        )
    ).scalars().all()
    ids: set[int] = set()
    for u in users:
        if u.role == "admin" or (u.role == "landlord" and u.family_id == family_id):
            if exclude_user_id is None or u.telegram_id != exclude_user_id:
                ids.add(u.telegram_id)
    return ids


class AuthMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        if not tg_user:
            return await handler(event, data)

        if isinstance(event, Message) and event.text and event.text.startswith("/start apt_"):
            user, _ = await get_authorized_user(tg_user.id)
            if user:
                data["current_user"] = user
                return await handler(event, data)

        user, deny_reason = await get_authorized_user(tg_user.id)
        if not user:
            if isinstance(event, CallbackQuery):
                await event.answer(deny_reason, show_alert=True)
            elif isinstance(event, Message):
                await safe_delete_message(event)
                await event.answer(deny_reason)
            return None

        data["current_user"] = user
        return await handler(event, data)


router.message.middleware(AuthMiddleware())
router.callback_query.middleware(AuthMiddleware())


async def _get_active_apartment(session, user: User) -> tuple[Apartment | None, list[Apartment]]:
    """Возвращает активную квартиру и список квартир ТОЛЬКО текущей семьи пользователя."""
    if user.role == "tenant":
        apt = await session.get(Apartment, user.apartment_id) if user.apartment_id else None
        return apt, ([apt] if apt else [])

    fam_id = user.family_id
    if not fam_id and user.role == "admin":
        first_fam = (await session.execute(select(Family).order_by(Family.id).limit(1))).scalar_one_or_none()
        if first_fam:
            fam_id = first_fam.id
            u_db = await session.get(User, user.telegram_id)
            if u_db:
                u_db.family_id = fam_id
                await session.commit()
                user.family_id = fam_id

    if not fam_id:
        return None, []

    all_apts = list(
        (
            await session.execute(
                select(Apartment).where(Apartment.family_id == fam_id).order_by(Apartment.id)
            )
        )
        .scalars()
        .all()
    )

    if user.selected_apt_id:
        for a in all_apts:
            if a.id == user.selected_apt_id:
                return a, all_apts
    return (all_apts[0] if all_apts else None), all_apts


async def show_home_panel(
    bot: Bot,
    state: FSMContext,
    user: User,
    chat_id: int,
    show_welcome: bool = False,
    force_new: bool = False,
) -> None:
    await state.set_state(None)
    async with SessionLocal() as session:
        apt, all_apts = await _get_active_apartment(session, user)
        latest = None
        if apt:
            latest = (
                await session.execute(
                    select(MeterReading)
                    .where(MeterReading.apartment_id == apt.id)
                    .order_by(desc(MeterReading.period), desc(MeterReading.id))
                    .limit(1)
                )
            ).scalar_one_or_none()
        text, kb = build_main_dashboard(user, apt, all_apts, latest, show_welcome=show_welcome)
    await render_to_panel(bot, state, user.telegram_id, chat_id, text, kb, force_new=force_new)


# ---------------------------------------------------------------------------
# УПРАВЛЕНИЕ СЕМЬЯМИ (Только для Администратора: /families и fam:*)
# ---------------------------------------------------------------------------
async def _render_families_screen(
    bot: Bot,
    state: FSMContext,
    user: User,
    chat_id: int,
    active_field: str | None = None,
    error_msg: str | None = None,
    info_msg: str | None = None,
    draft_info: str | None = None,
    confirm_delete: bool = False,
    force_new: bool = False,
) -> None:
    async with SessionLocal() as session:
        u_db = await session.get(User, user.telegram_id)
        all_families = list((await session.execute(select(Family).order_by(Family.id))).scalars().all())
        current_fam = await session.get(Family, u_db.family_id) if (u_db and u_db.family_id) else None
        if not current_fam and all_families:
            current_fam = all_families[0]
            if u_db:
                u_db.family_id = current_fam.id
                await session.commit()

        counts_rows = (
            await session.execute(
                select(Apartment.family_id, func.count(Apartment.id)).group_by(Apartment.family_id)
            )
        ).all()
        apt_counts = {r[0]: r[1] for r in counts_rows if r[0] is not None}

        owners: list[User] = []
        if current_fam:
            owners = list(
                (
                    await session.execute(
                        select(User).where(User.family_id == current_fam.id, User.role == "landlord")
                    )
                )
                .scalars()
                .all()
            )

        text, kb = render_families_menu(
            current_fam,
            all_families,
            apt_counts,
            owners,
            active_field=active_field,
            error_msg=error_msg,
            info_msg=info_msg,
            draft_info=draft_info,
            confirm_delete=confirm_delete,
        )
    await render_to_panel(bot, state, user.telegram_id, chat_id, text, kb, force_new=force_new)


@router.message(Command("families"))
async def cmd_menu_families(message: Message, state: FSMContext, bot: Bot, current_user: User) -> None:
    await safe_delete_message(message)
    if current_user.role != "admin":
        return
    await state.set_state(None)
    await _render_families_screen(bot, state, current_user, message.chat.id, force_new=True)


@router.callback_query(F.data == "fam:menu")
async def cb_families_menu(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    if current_user.role != "admin":
        await call.answer("Раздел управления семьями доступен только администратору", show_alert=True)
        return
    await call.answer()
    await state.set_state(None)
    await _render_families_screen(bot, state, current_user, call.message.chat.id)


@router.callback_query(F.data.startswith("fam:sel:"))
async def cb_family_select(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    if current_user.role != "admin":
        await call.answer("Недостаточно прав", show_alert=True)
        return
    fam_id = int(call.data.split(":")[2])
    async with SessionLocal() as session:
        fam = await session.get(Family, fam_id)
        if not fam:
            await call.answer("Семья не найдена", show_alert=True)
            return
        u_db = await session.get(User, current_user.telegram_id)
        first_apt = (
            await session.execute(
                select(Apartment).where(Apartment.family_id == fam_id).order_by(Apartment.id).limit(1)
            )
        ).scalar_one_or_none()
        if u_db:
            u_db.family_id = fam_id
            u_db.selected_apt_id = first_apt.id if first_apt else None
            await session.commit()
            current_user = u_db

    await call.answer(f"Выбрана семья: {fam.name}")
    await _render_families_screen(
        bot, state, current_user, call.message.chat.id, info_msg=f"Активная семья переключена на «<b>{fam.name}</b>»."
    )


@router.callback_query(F.data == "fam:create")
async def cb_family_create(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    if current_user.role != "admin":
        await call.answer("Недостаточно прав", show_alert=True)
        return
    await call.answer()
    await state.set_state(PanelStates.managing_family)
    await state.update_data(fam_action="create_family", fam_target_id=None)
    await _render_families_screen(bot, state, current_user, call.message.chat.id, active_field="create_family")


@router.callback_query(F.data.startswith("fam:rename:"))
async def cb_family_rename(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    if current_user.role != "admin":
        await call.answer("Недостаточно прав", show_alert=True)
        return
    fam_id = int(call.data.split(":")[2])
    await call.answer()
    await state.set_state(PanelStates.managing_family)
    await state.update_data(fam_action="rename_family", fam_target_id=fam_id)
    await _render_families_screen(bot, state, current_user, call.message.chat.id, active_field="rename_family")


@router.callback_query(F.data.startswith("fam:add_owner:"))
async def cb_family_add_owner(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    if current_user.role != "admin":
        await call.answer("Недостаточно прав", show_alert=True)
        return
    fam_id = int(call.data.split(":")[2])
    await call.answer()
    await state.set_state(PanelStates.managing_family)
    await state.update_data(fam_action="owner_id", fam_target_id=fam_id, draft_owner_id=None)
    await _render_families_screen(bot, state, current_user, call.message.chat.id, active_field="owner_id")


@router.callback_query(F.data.startswith("fam:del_ask:"))
async def cb_family_delete_ask(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    if current_user.role != "admin":
        await call.answer("Недостаточно прав", show_alert=True)
        return
    await call.answer()
    await state.set_state(None)
    await _render_families_screen(bot, state, current_user, call.message.chat.id, confirm_delete=True)


@router.callback_query(F.data.startswith("fam:del_confirm:"))
async def cb_family_delete_confirm(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    if current_user.role != "admin":
        await call.answer("Недостаточно прав", show_alert=True)
        return
    fam_id = int(call.data.split(":")[2])
    deleted_users_ids: list[tuple[int, int | None]] = []

    async with SessionLocal() as session:
        fam = await session.get(Family, fam_id)
        if not fam:
            await call.answer("Семья уже удалена", show_alert=True)
            await _render_families_screen(bot, state, current_user, call.message.chat.id)
            return

        fam_name = fam.name

        # Удаляем не-админов этой семьи (владельцев и нанимателей)
        fam_users = (
            await session.execute(
                select(User).where(User.family_id == fam_id, User.role != "admin")
            )
        ).scalars().all()
        for u in fam_users:
            deleted_users_ids.append((u.telegram_id, u.panel_msg_id))
            await session.delete(u)

        # Удаляем квартиры этой семьи
        fam_apts = (
            await session.execute(select(Apartment).where(Apartment.family_id == fam_id))
        ).scalars().all()
        for a in fam_apts:
            await session.delete(a)

        await session.delete(fam)
        await session.flush()

        # Проверяем, остались ли другие семьи; если нет — создаём новую базовую семью
        next_fam = (await session.execute(select(Family).order_by(Family.id).limit(1))).scalar_one_or_none()
        if not next_fam:
            next_fam = Family(name="Основная семья")
            session.add(next_fam)
            await session.flush()

        next_apt = (
            await session.execute(
                select(Apartment).where(Apartment.family_id == next_fam.id).order_by(Apartment.id).limit(1)
            )
        ).scalar_one_or_none()

        admin_db = await session.get(User, current_user.telegram_id)
        if admin_db:
            admin_db.family_id = next_fam.id
            admin_db.selected_apt_id = next_apt.id if next_apt else None
            current_user = admin_db

        await log_audit(
            session,
            current_user.telegram_id,
            "DELETE_FAMILY",
            f"Удалена семья «{fam_name}» (ID {fam_id})",
        )
        await session.commit()

    for tg_id, p_id in deleted_users_ids:
        if p_id:
            try:
                await bot.delete_message(chat_id=tg_id, message_id=p_id)
            except Exception:
                pass
        try:
            await bot.send_message(
                chat_id=tg_id,
                text="Ваш Telegram ID был удалён из бота и доступ к боту прекращён",
            )
        except Exception:
            pass

    await state.set_state(None)
    await call.answer(f"Семья «{fam_name}» удалена")
    await _render_families_screen(
        bot,
        state,
        current_user,
        call.message.chat.id,
        info_msg=f"Семья «<b>{fam_name}</b>» и связанные с ней квартиры удалены.",
    )


@router.message(PanelStates.managing_family)
async def msg_family_input(message: Message, state: FSMContext, bot: Bot, current_user: User) -> None:
    await safe_delete_message(message)
    if current_user.role != "admin":
        return

    data = await state.get_data()
    action: str = data.get("fam_action", "")
    fam_id: int | None = data.get("fam_target_id")
    raw = (message.text or "").strip()

    async with SessionLocal() as session:
        if action == "create_family":
            if not raw or len(raw) > 60:
                await _render_families_screen(
                    bot,
                    state,
                    current_user,
                    message.chat.id,
                    active_field="create_family",
                    error_msg="укажите название семьи (до 60 символов)",
                )
                return
            dup = (await session.execute(select(Family).where(Family.name == raw))).scalar_one_or_none()
            if dup:
                await _render_families_screen(
                    bot,
                    state,
                    current_user,
                    message.chat.id,
                    active_field="create_family",
                    error_msg=f"семья «{raw}» уже существует",
                )
                return

            new_fam = Family(name=raw)
            session.add(new_fam)
            await session.flush()
            u_db = await session.get(User, current_user.telegram_id)
            if u_db:
                u_db.family_id = new_fam.id
                u_db.selected_apt_id = None
                current_user = u_db
            await log_audit(session, current_user.telegram_id, "CREATE_FAMILY", f"Создана семья «{raw}» (ID {new_fam.id})")
            await session.commit()
            await state.set_state(None)
            await _render_families_screen(
                bot,
                state,
                current_user,
                message.chat.id,
                info_msg=f"Семья «<b>{raw}</b>» создана и выбрана активной! Теперь вы можете добавить в неё владельца или квартиры.",
            )
            return

        if action == "rename_family" and fam_id:
            if not raw or len(raw) > 60:
                await _render_families_screen(
                    bot,
                    state,
                    current_user,
                    message.chat.id,
                    active_field="rename_family",
                    error_msg="укажите корректное название семьи",
                )
                return
            fam = await session.get(Family, fam_id)
            if fam:
                old_name = fam.name
                fam.name = raw
                await log_audit(session, current_user.telegram_id, "RENAME_FAMILY", f"{old_name} -> {raw}")
                await session.commit()
            await state.set_state(None)
            await _render_families_screen(
                bot, state, current_user, message.chat.id, info_msg=f"Название семьи изменено на «<b>{raw}</b>»."
            )
            return

        if action == "owner_id" and fam_id:
            if not raw.isdigit():
                await _render_families_screen(
                    bot,
                    state,
                    current_user,
                    message.chat.id,
                    active_field="owner_id",
                    error_msg="укажите числовой Telegram ID (только цифры)",
                )
                return
            o_id = int(raw)
            existing = await session.get(User, o_id)
            if existing and existing.role == "admin":
                await state.set_state(None)
                await _render_families_screen(
                    bot,
                    state,
                    current_user,
                    message.chat.id,
                    info_msg=f"Пользователь {o_id} является администратором и уже имеет доступ ко всем семьям.",
                )
                return

            await state.update_data(draft_owner_id=o_id, fam_action="owner_name")
            await _render_families_screen(
                bot,
                state,
                current_user,
                message.chat.id,
                active_field="owner_name",
                draft_info=f"  • Telegram ID владельца: <code>{o_id}</code>",
            )
            return

        if action == "owner_name" and fam_id:
            if len(raw) < 2:
                o_id = data.get("draft_owner_id")
                await _render_families_screen(
                    bot,
                    state,
                    current_user,
                    message.chat.id,
                    active_field="owner_name",
                    error_msg="укажите имя владельца",
                    draft_info=f"  • Telegram ID владельца: <code>{o_id}</code>",
                )
                return

            o_id = int(data["draft_owner_id"])
            o_name = raw
            fam = await session.get(Family, fam_id)
            fam_name = fam.name if fam else f"ID {fam_id}"

            first_apt = (
                await session.execute(
                    select(Apartment).where(Apartment.family_id == fam_id).order_by(Apartment.id).limit(1)
                )
            ).scalar_one_or_none()

            existing = await session.get(User, o_id)
            if existing:
                existing.role = "landlord"
                existing.family_id = fam_id
                existing.name = o_name
                existing.apartment_id = None
                existing.selected_apt_id = first_apt.id if first_apt else None
                existing.is_active = True
            else:
                session.add(
                    User(
                        telegram_id=o_id,
                        role="landlord",
                        family_id=fam_id,
                        name=o_name,
                        selected_apt_id=first_apt.id if first_apt else None,
                        is_active=True,
                    )
                )

            await log_audit(
                session,
                current_user.telegram_id,
                "ADD_FAMILY_OWNER",
                f"Владелец {o_name} (ID {o_id}) -> семья «{fam_name}»",
            )
            await session.commit()
            await set_user_menu_commands(bot, o_id, "landlord")

            try:
                await bot.send_message(
                    chat_id=o_id,
                    text=(
                        f"👋 Здравствуйте, <b>{o_name}</b>!\n"
                        f"Вы добавлены как владелец в семью «<b>{fam_name}</b>».\n"
                        "Нажмите /start для открытия меню управления квартирами."
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                pass

            await state.set_state(None)
            await _render_families_screen(
                bot,
                state,
                current_user,
                message.chat.id,
                info_msg=f"Владелец <b>{o_name}</b> (ID <code>{o_id}</code>) привязан к семье «<b>{fam_name}</b>».",
            )


# ---------------------------------------------------------------------------
# КОМАНДЫ ИЗ КНОПКИ «MENU»
# ---------------------------------------------------------------------------
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, bot: Bot, current_user: User) -> None:
    await safe_delete_message(message)
    await set_user_menu_commands(bot, current_user.telegram_id, current_user.role)

    parts = (message.text or "").split()
    if len(parts) > 1 and parts[1].startswith("apt_"):
        apt_id_str = parts[1].replace("apt_", "")
        if apt_id_str.isdigit() and current_user.role == "tenant":
            async with SessionLocal() as session:
                u = await session.get(User, current_user.telegram_id)
                apt = await session.get(Apartment, int(apt_id_str))
                if u and apt:
                    u.apartment_id = apt.id
                    u.family_id = apt.family_id
                    await session.commit()
                    current_user = u

    await show_home_panel(bot, state, current_user, message.chat.id, show_welcome=False, force_new=True)


@router.message(Command("water"))
async def cmd_menu_water(message: Message, state: FSMContext, bot: Bot, current_user: User) -> None:
    await safe_delete_message(message)
    async with SessionLocal() as session:
        apt, _ = await _get_active_apartment(session, current_user)
    if not apt:
        await show_home_panel(bot, state, current_user, message.chat.id, force_new=True)
        return
    await state.update_data(panel_msg_id=None)
    await _open_reading_session(bot, state, current_user, message.chat.id, apt.id, mode="water")


@router.message(Command("elec"))
async def cmd_menu_elec(message: Message, state: FSMContext, bot: Bot, current_user: User) -> None:
    await safe_delete_message(message)
    async with SessionLocal() as session:
        apt, _ = await _get_active_apartment(session, current_user)
    if not apt:
        await show_home_panel(bot, state, current_user, message.chat.id, force_new=True)
        return
    await state.update_data(panel_msg_id=None)
    await _open_reading_session(bot, state, current_user, message.chat.id, apt.id, mode="elec")


@router.message(Command("readings"))
async def cmd_menu_readings(message: Message, state: FSMContext, bot: Bot, current_user: User) -> None:
    await safe_delete_message(message)
    async with SessionLocal() as session:
        apt, _ = await _get_active_apartment(session, current_user)
    if not apt:
        await show_home_panel(bot, state, current_user, message.chat.id, force_new=True)
        return
    text = (
        f"📝 <b>ПОДАЧА ПОКАЗАНИЙ — Кв. №{apt.number}</b>\n"
        "────────────────────────\n"
        "Выберите, какие показания вы хотите передать:"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💧 Подать показания водомеров", callback_data=f"read:open:{apt.id}:water")],
            [InlineKeyboardButton(text="⚡ Подать показания электроэнергии", callback_data=f"read:open:{apt.id}:elec")],
            [InlineKeyboardButton(text="⬅️ На главную", callback_data="main:home")],
        ]
    )
    await render_to_panel(bot, state, current_user.telegram_id, message.chat.id, text, kb, force_new=True)


@router.message(Command("history"))
async def cmd_menu_history(message: Message, state: FSMContext, bot: Bot, current_user: User) -> None:
    await safe_delete_message(message)
    await state.set_state(None)
    async with SessionLocal() as session:
        apt, _ = await _get_active_apartment(session, current_user)
        if not apt:
            await show_home_panel(bot, state, current_user, message.chat.id, force_new=True)
            return
        readings = list(
            (
                await session.execute(
                    select(MeterReading)
                    .where(MeterReading.apartment_id == apt.id)
                    .order_by(desc(MeterReading.period))
                )
            )
            .scalars()
            .all()
        )
        text, kb = render_history_panel(apt, readings, idx=0)
    await render_to_panel(bot, state, current_user.telegram_id, message.chat.id, text, kb, force_new=True)


@router.message(Command("settings"))
async def cmd_menu_settings(message: Message, state: FSMContext, bot: Bot, current_user: User) -> None:
    await safe_delete_message(message)
    if current_user.role not in ("admin", "landlord"):
        return
    await state.set_state(None)
    async with SessionLocal() as session:
        apt, _ = await _get_active_apartment(session, current_user)
        if not apt:
            await show_apartments_panel(bot, state, current_user, message.chat.id, force_new=True)
            return
        text, kb = render_apartment_settings(apt)
    await render_to_panel(bot, state, current_user.telegram_id, message.chat.id, text, kb, force_new=True)


@router.message(Command("users"))
async def cmd_menu_users(message: Message, state: FSMContext, bot: Bot, current_user: User) -> None:
    await safe_delete_message(message)
    if current_user.role not in ("admin", "landlord"):
        return
    await state.set_state(None)
    async with SessionLocal() as session:
        apt, _ = await _get_active_apartment(session, current_user)
    if not apt:
        await show_apartments_panel(bot, state, current_user, message.chat.id, force_new=True)
        return
    await state.update_data(panel_msg_id=None)
    await _render_usr_screen(bot, state, current_user.telegram_id, message.chat.id, apt.id)


@router.message(Command("export"))
async def cmd_menu_export(message: Message, state: FSMContext, bot: Bot, current_user: User) -> None:
    await safe_delete_message(message)
    if current_user.role not in ("admin", "landlord"):
        return
    await state.set_state(None)
    async with SessionLocal() as session:
        apt, _ = await _get_active_apartment(session, current_user)
    if not apt:
        await show_apartments_panel(bot, state, current_user, message.chat.id, force_new=True)
        return
    text, kb = _build_export_menu_view(apt.id, apt.number)
    await render_to_panel(bot, state, current_user.telegram_id, message.chat.id, text, kb, force_new=True)


@router.message(Command("new_apt"))
async def cmd_menu_new_apt(message: Message, state: FSMContext, bot: Bot, current_user: User) -> None:
    await safe_delete_message(message)
    if current_user.role not in ("admin", "landlord"):
        return
    await state.set_state(PanelStates.creating_apartment)
    text = (
        "🏢 <b>СОЗДАНИЕ НОВОЙ КВАРТИРЫ</b>\n"
        "────────────────────────\n"
        "👉 Введите номер квартиры (например: <code>75</code>):"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀ Отмена", callback_data="apt:menu")]]
    )
    await render_to_panel(bot, state, current_user.telegram_id, message.chat.id, text, kb, force_new=True)


@router.message(Command("admin"))
async def cmd_menu_admin(message: Message, state: FSMContext, bot: Bot, current_user: User) -> None:
    await safe_delete_message(message)
    if current_user.role != "admin":
        return
    await state.set_state(None)
    async with SessionLocal() as session:
        apt, _ = await _get_active_apartment(session, current_user)
    if not apt:
        await _render_families_screen(bot, state, current_user, message.chat.id, force_new=True)
        return
    await state.update_data(panel_msg_id=None)
    await _open_admin_view(bot, state, current_user, message.chat.id, apt.id, "overview")


@router.message(Command("info"))
async def cmd_menu_info(message: Message, state: FSMContext, bot: Bot, current_user: User) -> None:
    await safe_delete_message(message)
    await state.set_state(None)
    if current_user.role in ("admin", "landlord"):
        text, kb = render_owner_info()
        await render_to_panel(bot, state, current_user.telegram_id, message.chat.id, text, kb, force_new=True)
        return
    async with SessionLocal() as session:
        apt, _ = await _get_active_apartment(session, current_user)
        if not apt:
            await show_home_panel(bot, state, current_user, message.chat.id, force_new=True)
            return
        text, kb = render_tenant_info(apt)
    await render_to_panel(bot, state, current_user.telegram_id, message.chat.id, text, kb, force_new=True)


# ---------------------------------------------------------------------------
# INLINE-НАВИГАЦИЯ ГЛАВНОГО МЕНЮ И РАЗДЕЛА «КВАРТИРЫ»
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "main:home")
async def cb_main_home(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    await call.answer()
    await show_home_panel(bot, state, current_user, call.message.chat.id, show_welcome=False)


@router.callback_query(F.data == "main:welcome")
async def cb_main_welcome(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    await call.answer()
    await state.set_state(None)
    text, kb = render_owner_info()
    await render_to_panel(bot, state, current_user.telegram_id, call.message.chat.id, text, kb)


@router.callback_query(F.data == "main:tenant_info")
async def cb_main_tenant_info(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    await call.answer()
    await state.set_state(None)
    async with SessionLocal() as session:
        apt, _ = await _get_active_apartment(session, current_user)
        if not apt:
            await show_home_panel(bot, state, current_user, call.message.chat.id)
            return
        text, kb = render_tenant_info(apt)
    await render_to_panel(bot, state, current_user.telegram_id, call.message.chat.id, text, kb)


async def show_apartments_panel(
    bot: Bot,
    state: FSMContext,
    user: User,
    chat_id: int,
    force_new: bool = False,
) -> None:
    await state.set_state(None)
    async with SessionLocal() as session:
        apt, all_apts = await _get_active_apartment(session, user)
        fam = await session.get(Family, user.family_id) if user.family_id else None
        tenants: list[User] = []
        if apt:
            tenants = list(
                (
                    await session.execute(
                        select(User).where(User.apartment_id == apt.id, User.role == "tenant")
                    )
                )
                .scalars()
                .all()
            )
        text, kb = render_apartments_menu(
            apt,
            all_apts,
            tenants=tenants,
            family_name=fam.name if fam else None,
            is_admin=(user.role == "admin"),
        )
    await render_to_panel(bot, state, user.telegram_id, chat_id, text, kb, force_new=force_new)


@router.message(Command("apartments"))
async def cmd_menu_apartments(message: Message, state: FSMContext, bot: Bot, current_user: User) -> None:
    await safe_delete_message(message)
    if current_user.role not in ("admin", "landlord"):
        return
    await show_apartments_panel(bot, state, current_user, message.chat.id, force_new=True)


@router.callback_query(F.data == "apt:menu")
async def cb_apartments_menu(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    if current_user.role not in ("admin", "landlord"):
        await call.answer("Недостаточно прав", show_alert=True)
        return
    await call.answer()
    await show_apartments_panel(bot, state, current_user, call.message.chat.id)


@router.callback_query(F.data.startswith("apt:sel:"))
async def cb_select_apartment(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    if current_user.role not in ("admin", "landlord"):
        await call.answer("Недостаточно прав", show_alert=True)
        return
    apt_id = int(call.data.split(":")[2])
    async with SessionLocal() as session:
        apt = await session.get(Apartment, apt_id)
        if not _can_access_apartment(current_user, apt):
            await call.answer("⛔ Доступ к квартире другой семьи запрещён", show_alert=True)
            return
        u = await session.get(User, current_user.telegram_id)
        if u:
            u.selected_apt_id = apt_id
            if u.role == "admin" and apt.family_id:
                u.family_id = apt.family_id
            await session.commit()
            current_user = u
    await call.answer("Квартира выбрана")
    await show_apartments_panel(bot, state, current_user, call.message.chat.id)


# ---------------------------------------------------------------------------
# СОЗДАНИЕ КВАРТИРЫ И НАСТРОЙКА ПАРАМЕТРОВ (В пределах семьи)
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "apt:create")
async def cb_create_apartment(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    if current_user.role not in ("admin", "landlord"):
        await call.answer("Недостаточно прав", show_alert=True)
        return
    await call.answer()
    await state.set_state(PanelStates.creating_apartment)
    text = (
        "🏢 <b>СОЗДАНИЕ НОВОЙ КВАРТИРЫ</b>\n"
        "────────────────────────\n"
        "👉 Введите номер квартиры (например: <code>75</code>):"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀ Отмена", callback_data="apt:menu")]]
    )
    await render_to_panel(bot, state, current_user.telegram_id, call.message.chat.id, text, kb)


@router.message(PanelStates.creating_apartment)
async def msg_create_apartment(message: Message, state: FSMContext, bot: Bot, current_user: User) -> None:
    await safe_delete_message(message)
    if current_user.role not in ("admin", "landlord"):
        return

    num = (message.text or "").strip()
    if not num or len(num) > 20:
        text = (
            "🏢 <b>СОЗДАНИЕ НОВОЙ КВАРТИРЫ</b>\n"
            "⚠️ <b>Ошибка:</b> укажите корректный номер квартиры (до 20 символов).\n\n"
            "👉 Введите номер квартиры:"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="◀ Отмена", callback_data="apt:menu")]]
        )
        await render_to_panel(bot, state, current_user.telegram_id, message.chat.id, text, kb)
        return

    async with SessionLocal() as session:
        u = await session.get(User, current_user.telegram_id)
        fam_id = u.family_id if u else None
        if not fam_id:
            first_fam = (await session.execute(select(Family).order_by(Family.id).limit(1))).scalar_one_or_none()
            fam_id = first_fam.id if first_fam else None
            if u and fam_id:
                u.family_id = fam_id

        existing = (
            await session.execute(
                select(Apartment).where(Apartment.number == num, Apartment.family_id == fam_id)
            )
        ).scalar_one_or_none()
        if existing:
            text = (
                "🏢 <b>СОЗДАНИЕ НОВОЙ КВАРТИРЫ</b>\n"
                f"⚠️ <b>Ошибка:</b> квартира №{num} уже существует в вашей семье!\n\n"
                "👉 Введите другой номер квартиры:"
            )
            kb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="◀ Отмена", callback_data="apt:menu")]]
            )
            await render_to_panel(bot, state, current_user.telegram_id, message.chat.id, text, kb)
            return

        apt = Apartment(number=num, has_tenant=False, family_id=fam_id)
        session.add(apt)
        await session.flush()
        if u:
            u.selected_apt_id = apt.id
        await log_audit(
            session,
            current_user.telegram_id,
            "CREATE_APARTMENT",
            f"Создана кв. №{num} (семья ID {fam_id})",
            apt.id,
        )
        apt_id = apt.id
        await session.commit()

    await state.set_state(None)
    text = (
        f"✅ <b>Квартира №{num} успешно создана!</b>\n"
        "────────────────────────\n"
        "Проживает ли сейчас в квартире наниматель?"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👤 Да, добавить нанимателя", callback_data=f"usr:open:{apt_id}"),
                InlineKeyboardButton(text="⚙️ Нет, ввести тарифы и счётчики", callback_data=f"cfg:wiz:{apt_id}"),
            ],
            [InlineKeyboardButton(text="🏢 К списку квартир", callback_data="apt:menu")],
        ]
    )
    await render_to_panel(bot, state, current_user.telegram_id, message.chat.id, text, kb)


@router.callback_query(F.data.startswith("cfg:open:"))
async def cb_cfg_open(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    if current_user.role not in ("admin", "landlord"):
        await call.answer("Недостаточно прав", show_alert=True)
        return
    apt_id = int(call.data.split(":")[2])
    await state.set_state(None)
    async with SessionLocal() as session:
        apt = await session.get(Apartment, apt_id)
        if not _can_access_apartment(current_user, apt):
            await call.answer("⛔ Доступ к квартире другой семьи запрещён", show_alert=True)
            return
        text, kb = render_apartment_settings(apt)
    await call.answer()
    await render_to_panel(bot, state, current_user.telegram_id, call.message.chat.id, text, kb)


@router.callback_query(F.data.startswith("cfg:edit:"))
async def cb_cfg_edit_single(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    if current_user.role not in ("admin", "landlord"):
        await call.answer("Недостаточно прав", show_alert=True)
        return
    _, _, apt_id_s, field = call.data.split(":")
    apt_id = int(apt_id_s)
    async with SessionLocal() as session:
        apt = await session.get(Apartment, apt_id)
        if not _can_access_apartment(current_user, apt):
            await call.answer("⛔ Доступ запрещён", show_alert=True)
            return
        text, kb = render_apartment_settings(apt, active_field=field)
    await call.answer()
    await state.set_state(PanelStates.editing_cfg_field)
    await state.update_data(cfg_apt_id=apt_id, cfg_field=field, cfg_wizard=False, cfg_step=None)
    await render_to_panel(bot, state, current_user.telegram_id, call.message.chat.id, text, kb)


@router.callback_query(F.data.startswith("cfg:wiz:"))
async def cb_cfg_wizard_start(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    if current_user.role not in ("admin", "landlord"):
        await call.answer("Недостаточно прав", show_alert=True)
        return
    apt_id = int(call.data.split(":")[2])
    first_field = WIZARD_CFG_ORDER[0]
    async with SessionLocal() as session:
        apt = await session.get(Apartment, apt_id)
        if not _can_access_apartment(current_user, apt):
            await call.answer("⛔ Доступ запрещён", show_alert=True)
            return
        text, kb = render_apartment_settings(apt, active_field=first_field, wizard_step=1)
    await call.answer("Режим пошагового заполнения")
    await state.set_state(PanelStates.editing_cfg_field)
    await state.update_data(cfg_apt_id=apt_id, cfg_field=first_field, cfg_wizard=True, cfg_step=1)
    await render_to_panel(bot, state, current_user.telegram_id, call.message.chat.id, text, kb)


@router.message(PanelStates.editing_cfg_field)
async def msg_cfg_field_input(message: Message, state: FSMContext, bot: Bot, current_user: User) -> None:
    await safe_delete_message(message)
    if current_user.role not in ("admin", "landlord"):
        return

    data = await state.get_data()
    apt_id: int = data["cfg_apt_id"]
    field: str = data["cfg_field"]
    is_wizard: bool = data.get("cfg_wizard", False)
    step: int | None = data.get("cfg_step")
    raw = (message.text or "").strip()

    async with SessionLocal() as session:
        apt = await session.get(Apartment, apt_id)
        if not _can_access_apartment(current_user, apt):
            return

        try:
            if field == "number":
                if not raw or len(raw) > 20:
                    raise ValueError("укажите номер квартиры от 1 до 20 символов")
                new_val: Any = raw
            elif field in ("water_day", "elec_day"):
                if not raw.isdigit() or not (1 <= int(raw) <= 31):
                    raise ValueError("укажите целое число от 1 до 31")
                new_val = int(raw)
            elif field.startswith("tariff_"):
                new_val = parse_decimal(raw, "0.01")
            elif field.startswith("init_"):
                new_val = parse_int_reading(raw)
            else:
                raise ValueError("неизвестное поле")
        except (ValueError, InvalidOperation) as e:
            err_text = str(e) if isinstance(e, ValueError) else "введите корректное неотрицательное число (например: 42.50)"
            text, kb = render_apartment_settings(apt, active_field=field, wizard_step=step, error_msg=err_text)
            await render_to_panel(bot, state, current_user.telegram_id, message.chat.id, text, kb)
            return

        old_val = getattr(apt, field)
        setattr(apt, field, new_val)

        if field.startswith("tariff_"):
            session.add(
                TariffHistory(
                    apartment_id=apt.id,
                    tariff_hvs=apt.tariff_hvs,
                    tariff_gvs=apt.tariff_gvs,
                    tariff_t1=apt.tariff_t1,
                    tariff_t2=apt.tariff_t2,
                    tariff_t3=apt.tariff_t3,
                    effective_from=datetime.now(TZ).replace(tzinfo=None),
                    changed_by=current_user.telegram_id,
                )
            )

        await log_audit(
            session,
            current_user.telegram_id,
            f"EDIT_{field.upper()}",
            f"{old_val} -> {new_val}",
            apt.id,
        )
        await session.commit()

        apt_updated = await session.get(Apartment, apt_id)

        if is_wizard and step and step < len(WIZARD_CFG_ORDER):
            next_step = step + 1
            next_field = WIZARD_CFG_ORDER[next_step - 1]
            await state.update_data(cfg_field=next_field, cfg_step=next_step)
            text, kb = render_apartment_settings(apt_updated, active_field=next_field, wizard_step=next_step)
        else:
            await state.set_state(None)
            text, kb = render_apartment_settings(apt_updated)

    await render_to_panel(bot, state, current_user.telegram_id, message.chat.id, text, kb)


# ---------------------------------------------------------------------------
# УПРАВЛЕНИЕ ДОГОВОРОМ, НАНИМАТЕЛЯМИ И СОВЛАДЕЛЬЦАМИ (В пределах семьи)
# ---------------------------------------------------------------------------
async def _render_usr_screen(
    bot: Bot,
    state: FSMContext,
    user_id: int,
    chat_id: int,
    apt_id: int,
    active_field: str | None = None,
    error_msg: str | None = None,
    info_msg: str | None = None,
    draft_info: str | None = None,
    awaiting_no_agr_confirm: bool = False,
) -> None:
    me = await bot.get_me()
    async with SessionLocal() as session:
        apt = await session.get(Apartment, apt_id)
        fam = await session.get(Family, apt.family_id) if (apt and apt.family_id) else None
        tenants = list(
            (await session.execute(select(User).where(User.apartment_id == apt_id, User.role == "tenant")))
            .scalars()
            .all()
        )
        landlords = list(
            (
                await session.execute(
                    select(User).where(User.role == "landlord", User.family_id == apt.family_id)
                )
            )
            .scalars()
            .all()
        )
        # Список квартир внутри этой семьи, в которых проживает арендатор
        rented_apts = list(
            (
                await session.execute(
                    select(Apartment)
                    .where(Apartment.family_id == apt.family_id, Apartment.has_tenant.is_(True))
                    .order_by(Apartment.id)
                )
            )
            .scalars()
            .all()
        )
        latest_reading = (
            await session.execute(
                select(MeterReading)
                .where(MeterReading.apartment_id == apt.id)
                .order_by(desc(MeterReading.period), desc(MeterReading.id))
                .limit(1)
            )
        ).scalar_one_or_none()

        text, kb = render_users_panel(
            apt,
            tenants,
            landlords,
            me.username or "",
            family_name=fam.name if fam else None,
            rented_apts=rented_apts,
            latest_reading=latest_reading,
            active_field=active_field,
            error_msg=error_msg,
            info_msg=info_msg,
            draft_info=draft_info,
            awaiting_no_agr_confirm=awaiting_no_agr_confirm,
        )
    await render_to_panel(bot, state, user_id, chat_id, text, kb)


async def _finalize_tenant_invite(
    session,
    bot: Bot,
    apt: Apartment,
    actor_id: int,
    t_id: int,
    phone: str,
    t_name: str,
) -> str:
    existing = await session.get(User, t_id)
    if existing:
        existing.phone = phone
        existing.name = t_name
        existing.apartment_id = apt.id
        existing.family_id = apt.family_id
        existing.is_active = True
        apt.has_tenant = True
        await log_audit(
            session,
            actor_id,
            "UPDATE_TENANT",
            f"ID {t_id} (имя={t_name}, тел={phone})",
            apt.id,
        )
        info_msg = (
            f"Данные нанимателя обновлены: <b>{t_name}</b>, "
            f"тел. <code>{phone}</code> (ID <code>{t_id}</code>). Приглашение отправлено в Телеграм на указанный ID"
        )
    else:
        session.add(
            User(
                telegram_id=t_id,
                role="tenant",
                family_id=apt.family_id,
                name=t_name,
                phone=phone,
                apartment_id=apt.id,
                is_active=True,
            )
        )
        apt.has_tenant = True
        await log_audit(
            session,
            actor_id,
            "ADD_TENANT",
            f"ID {t_id} (имя={t_name}, тел={phone})",
            apt.id,
        )
        await set_user_menu_commands(bot, t_id, "tenant")
        info_msg = (
            f"Добавлен наниматель - <b>{t_name}</b> (ID <code>{t_id}</code>), "
            f"телефон <code>{phone}</code>. Приглашение отправлено в Телеграм на указанный ID"
        )

    agr_start = (
        apt.agreement_date_start.strftime("%d.%m.%Y")
        if apt.agreement_date_start
        else "не указана"
    )
    agr_finish = (
        apt.agreement_date_finish.strftime("%d.%m.%Y")
        if apt.agreement_date_finish
        else "не указана"
    )
    try:
        await bot.send_message(
            chat_id=t_id,
            text=(
                f"👋 Здравствуйте, <b>{t_name}</b>!\n"
                f"Вы добавлены как наниматель квартиры <b>№{apt.number}</b> "
                f"(договор №{apt.agreement_no or 'б/н'} от {agr_start} со сроком действия до {agr_finish}).\n"
                "Нажмите /start для открытия меню подачи показаний."
            ),
            parse_mode="HTML",
        )
    except Exception:
        info_msg += " (Перешлите жильцу ссылку-приглашение с экрана ниже, чтобы он нажал /start)."

    await session.commit()
    return info_msg


@router.callback_query(F.data.startswith("usr:open:"))
async def cb_usr_open(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    if current_user.role not in ("admin", "landlord"):
        await call.answer("Недостаточно прав", show_alert=True)
        return
    apt_id = int(call.data.split(":")[2])
    async with SessionLocal() as session:
        apt = await session.get(Apartment, apt_id)
        if not _can_access_apartment(current_user, apt):
            await call.answer("⛔ Доступ к квартире другой семьи запрещён", show_alert=True)
            return
    await call.answer()
    await state.set_state(None)
    await _render_usr_screen(bot, state, current_user.telegram_id, call.message.chat.id, apt_id)


@router.callback_query(F.data.startswith("usr:sel:"))
async def cb_usr_select_rented_apt(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    if current_user.role not in ("admin", "landlord"):
        await call.answer("Недостаточно прав", show_alert=True)
        return
    apt_id = int(call.data.split(":")[2])
    async with SessionLocal() as session:
        apt = await session.get(Apartment, apt_id)
        if not _can_access_apartment(current_user, apt):
            await call.answer("⛔ Доступ к квартире другой семьи запрещён", show_alert=True)
            return
        u = await session.get(User, current_user.telegram_id)
        if u:
            u.selected_apt_id = apt_id
            await session.commit()
            current_user = u
        apt_num = apt.number

    await call.answer(f"Выбрана квартира №{apt_num}")
    await state.set_state(None)
    await _render_usr_screen(bot, state, current_user.telegram_id, call.message.chat.id, apt_id)


@router.callback_query(F.data.startswith("usr:toggle:"))
async def cb_usr_toggle(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    if current_user.role not in ("admin", "landlord"):
        await call.answer("Недостаточно прав", show_alert=True)
        return
    apt_id = int(call.data.split(":")[2])
    async with SessionLocal() as session:
        apt = await session.get(Apartment, apt_id)
        if not _can_access_apartment(current_user, apt):
            await call.answer("⛔ Доступ запрещён", show_alert=True)
            return
        apt.has_tenant = not apt.has_tenant
        await log_audit(
            session, current_user.telegram_id, "TOGGLE_TENANT", f"has_tenant={apt.has_tenant}", apt_id
        )
        await session.commit()
    await call.answer("Статус изменён")
    await _render_usr_screen(bot, state, current_user.telegram_id, call.message.chat.id, apt_id)


@router.callback_query(F.data.startswith("usr:edit:"))
async def cb_usr_edit(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    if current_user.role not in ("admin", "landlord"):
        await call.answer("Недостаточно прав", show_alert=True)
        return
    _, _, apt_id_s, field = call.data.split(":")
    apt_id = int(apt_id_s)
    async with SessionLocal() as session:
        apt = await session.get(Apartment, apt_id)
        if not _can_access_apartment(current_user, apt):
            await call.answer("⛔ Доступ запрещён", show_alert=True)
            return
    await call.answer()
    await state.set_state(PanelStates.editing_usr_field)

    if field == "add_tenant":
        active_field = "tenant_id"
        await state.update_data(
            usr_apt_id=apt_id,
            usr_field=active_field,
            usr_wiz_agreement=False,
            draft_t_id=None,
            draft_t_phone=None,
            draft_t_name=None,
        )
    elif field == "add_landlord":
        active_field = "landlord_id"
        await state.update_data(
            usr_apt_id=apt_id,
            usr_field=active_field,
            usr_wiz_agreement=False,
            draft_l_id=None,
        )
    elif field == "wiz_agreement":
        active_field = "agreement_no"
        await state.update_data(
            usr_apt_id=apt_id,
            usr_field=active_field,
            usr_wiz_agreement=True,
        )
    else:
        active_field = field
        await state.update_data(
            usr_apt_id=apt_id,
            usr_field=active_field,
            usr_wiz_agreement=False,
        )

    await _render_usr_screen(
        bot, state, current_user.telegram_id, call.message.chat.id, apt_id, active_field=active_field
    )


@router.callback_query(F.data.startswith("usr:del:"))
async def cb_usr_delete(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    if current_user.role not in ("admin", "landlord"):
        await call.answer("Недостаточно прав", show_alert=True)
        return
    _, _, apt_id_s, tg_id_s = call.data.split(":")
    apt_id, target_tg_id = int(apt_id_s), int(tg_id_s)
    deleted_name: str | None = None
    old_panel_id: int | None = None
    was_deleted = False

    async with SessionLocal() as session:
        apt = await session.get(Apartment, apt_id)
        if not _can_access_apartment(current_user, apt):
            await call.answer("⛔ Доступ запрещён", show_alert=True)
            return
        u = await session.get(User, target_tg_id)
        if u and u.role == "tenant" and u.apartment_id == apt_id:
            deleted_name = u.name
            old_panel_id = u.panel_msg_id
            await session.delete(u)
            await log_audit(
                session,
                current_user.telegram_id,
                "DELETE_TENANT",
                f"ID {target_tg_id} ({deleted_name or 'без имени'})",
                apt_id,
            )
            await session.commit()
            was_deleted = True

    if was_deleted:
        if old_panel_id:
            try:
                await bot.delete_message(chat_id=target_tg_id, message_id=old_panel_id)
            except Exception:
                pass
        try:
            await bot.send_message(
                chat_id=target_tg_id,
                text="Ваш Telegram ID был удалён из бота и доступ к боту прекращён",
            )
        except Exception:
            pass

    await call.answer("Наниматель удалён")
    label = f"<b>{deleted_name}</b> (ID <code>{target_tg_id}</code>)" if deleted_name else f"ID <code>{target_tg_id}</code>"
    await _render_usr_screen(
        bot,
        state,
        current_user.telegram_id,
        call.message.chat.id,
        apt_id,
        info_msg=f"Наниматель {label} удалён. Уведомление о прекращении доступа отправлено.",
    )


@router.callback_query(F.data.startswith("usr:del_landlord:"))
async def cb_usr_delete_landlord(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    if current_user.role not in ("admin", "landlord"):
        await call.answer("Недостаточно прав", show_alert=True)
        return
    _, _, apt_id_s, tg_id_s = call.data.split(":")
    apt_id, target_tg_id = int(apt_id_s), int(tg_id_s)
    if target_tg_id == current_user.telegram_id:
        await call.answer("Вы не можете удалить сами себя", show_alert=True)
        return

    deleted_name: str | None = None
    old_panel_id: int | None = None
    was_deleted = False

    async with SessionLocal() as session:
        apt = await session.get(Apartment, apt_id)
        if not _can_access_apartment(current_user, apt):
            await call.answer("⛔ Доступ запрещён", show_alert=True)
            return
        u = await session.get(User, target_tg_id)
        if u and u.role == "landlord" and u.family_id == apt.family_id:
            deleted_name = u.name
            old_panel_id = u.panel_msg_id
            await session.delete(u)
            await log_audit(
                session,
                current_user.telegram_id,
                "DELETE_LANDLORD",
                f"ID {target_tg_id} ({deleted_name or 'без имени'})",
                apt_id,
            )
            await session.commit()
            was_deleted = True

    if was_deleted:
        if old_panel_id:
            try:
                await bot.delete_message(chat_id=target_tg_id, message_id=old_panel_id)
            except Exception:
                pass
        try:
            await bot.send_message(
                chat_id=target_tg_id,
                text="Ваш Telegram ID был удалён из бота и доступ к боту прекращён",
            )
        except Exception:
            pass

    await call.answer("Совладелец удалён")
    label = f"<b>{deleted_name}</b> (ID <code>{target_tg_id}</code>)" if deleted_name else f"ID <code>{target_tg_id}</code>"
    await _render_usr_screen(
        bot,
        state,
        current_user.telegram_id,
        call.message.chat.id,
        apt_id,
        info_msg=f"Совладелец {label} удалён из семьи.",
    )


@router.callback_query(F.data.startswith("usr:del_agr:"))
async def cb_usr_delete_agreement(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    if current_user.role not in ("admin", "landlord"):
        await call.answer("Недостаточно прав", show_alert=True)
        return
    apt_id = int(call.data.split(":")[2])
    async with SessionLocal() as session:
        apt = await session.get(Apartment, apt_id)
        if not _can_access_apartment(current_user, apt):
            await call.answer("⛔ Доступ запрещён", show_alert=True)
            return
        if not (apt.agreement_no or apt.agreement_date_start or apt.agreement_date_finish):
            await call.answer("Данные договора уже пусты", show_alert=True)
            return

        old_start = apt.agreement_date_start.strftime("%d.%m.%Y") if apt.agreement_date_start else "—"
        old_finish = apt.agreement_date_finish.strftime("%d.%m.%Y") if apt.agreement_date_finish else "—"
        old_info = f"№{apt.agreement_no or 'б/н'} ({old_start} – {old_finish})"

        apt.agreement_no = None
        apt.agreement_date_start = None
        apt.agreement_date_finish = None
        await log_audit(session, current_user.telegram_id, "DELETE_AGREEMENT", old_info, apt_id)
        await session.commit()

    await state.set_state(None)
    await call.answer("Договор аренды удалён")
    await _render_usr_screen(
        bot,
        state,
        current_user.telegram_id,
        call.message.chat.id,
        apt_id,
        info_msg=f"Договор аренды <b>{old_info}</b> удалён.",
    )


@router.callback_query(F.data.startswith("usr:no_agr_cancel:"))
async def cb_usr_no_agr_cancel(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    if current_user.role not in ("admin", "landlord"):
        await call.answer("Недостаточно прав", show_alert=True)
        return
    apt_id = int(call.data.split(":")[2])
    await state.set_state(None)
    await state.update_data(draft_t_id=None, draft_t_phone=None, draft_t_name=None)
    await call.answer("Отправка приглашения отменена")
    await _render_usr_screen(
        bot,
        state,
        current_user.telegram_id,
        call.message.chat.id,
        apt_id,
        info_msg="Отправка приглашения отменена.",
    )


@router.callback_query(F.data.startswith("usr:no_agr_confirm:"))
async def cb_usr_no_agr_confirm(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    if current_user.role not in ("admin", "landlord"):
        await call.answer("Недостаточно прав", show_alert=True)
        return
    apt_id = int(call.data.split(":")[2])
    data = await state.get_data()
    if not data.get("draft_t_id") or not data.get("draft_t_name"):
        await call.answer("Данные нанимателя уже обработаны", show_alert=True)
        await _render_usr_screen(bot, state, current_user.telegram_id, call.message.chat.id, apt_id)
        return

    t_id = int(data["draft_t_id"])
    phone = str(data.get("draft_t_phone") or "—")
    t_name = str(data["draft_t_name"])

    async with SessionLocal() as session:
        apt = await session.get(Apartment, apt_id)
        if not _can_access_apartment(current_user, apt):
            return
        info_msg = await _finalize_tenant_invite(
            session, bot, apt, current_user.telegram_id, t_id, phone, t_name
        )

    await state.set_state(None)
    await state.update_data(draft_t_id=None, draft_t_phone=None, draft_t_name=None)
    await call.answer("Приглашение отправлено")
    await _render_usr_screen(
        bot, state, current_user.telegram_id, call.message.chat.id, apt_id, info_msg=info_msg
    )


@router.message(PanelStates.editing_usr_field)
async def msg_usr_field_input(message: Message, state: FSMContext, bot: Bot, current_user: User) -> None:
    await safe_delete_message(message)
    if current_user.role not in ("admin", "landlord"):
        return

    data = await state.get_data()
    apt_id: int = data["usr_apt_id"]
    field: str = data["usr_field"]
    wiz_agreement: bool = data.get("usr_wiz_agreement", False)
    raw = (message.text or "").strip()
    info_msg: str | None = None

    def _current_draft_info() -> str | None:
        d_id = data.get("draft_t_id")
        d_phone = data.get("draft_t_phone")
        l_id = data.get("draft_l_id")
        parts_info = []
        if d_id:
            parts_info.append(f"  • Telegram ID нанимателя: <code>{d_id}</code>")
        if d_phone:
            parts_info.append(f"  • Телефон: <code>{d_phone}</code>")
        if l_id:
            parts_info.append(f"  • Telegram ID совладельца: <code>{l_id}</code>")
        return "\n".join(parts_info) if parts_info else None

    async with SessionLocal() as session:
        try:
            apt = await session.get(Apartment, apt_id)
            if not _can_access_apartment(current_user, apt):
                return

            if field == "tenant_id":
                if not raw.isdigit():
                    raise ValueError("укажите числовой Telegram ID (только цифры, например: 987654321)")
                t_id = int(raw)
                existing = await session.get(User, t_id)
                if existing and existing.role in ("admin", "landlord"):
                    role_names = {"admin": "администратор", "landlord": "владелец"}
                    await state.set_state(None)
                    await _render_usr_screen(
                        bot,
                        state,
                        current_user.telegram_id,
                        message.chat.id,
                        apt_id,
                        info_msg=(
                            f"Пользователь {t_id} уже есть в базе и имеет права: "
                            f"«{role_names.get(existing.role, existing.role)}»."
                        ),
                    )
                    return

                await state.update_data(draft_t_id=t_id, usr_field="tenant_phone")
                await _render_usr_screen(
                    bot,
                    state,
                    current_user.telegram_id,
                    message.chat.id,
                    apt_id,
                    active_field="tenant_phone",
                    draft_info=f"  • Telegram ID: <code>{t_id}</code>",
                )
                return

            if field == "tenant_phone":
                if len(raw) < 3:
                    raise ValueError("укажите корректный номер телефона (например: +79001234567)")
                t_id = data["draft_t_id"]
                await state.update_data(draft_t_phone=raw, usr_field="tenant_name")
                await _render_usr_screen(
                    bot,
                    state,
                    current_user.telegram_id,
                    message.chat.id,
                    apt_id,
                    active_field="tenant_name",
                    draft_info=f"  • Telegram ID: <code>{t_id}</code>\n  • Телефон: <code>{raw}</code>",
                )
                return

            if field == "tenant_name":
                if len(raw) < 2:
                    raise ValueError("укажите имя нанимателя (например: Иван Петров)")
                t_id = int(data["draft_t_id"])
                phone = str(data["draft_t_phone"])
                t_name = raw

                if not (apt.agreement_no and apt.agreement_date_start and apt.agreement_date_finish):
                    await state.set_state(None)
                    await state.update_data(draft_t_name=t_name)
                    await _render_usr_screen(
                        bot,
                        state,
                        current_user.telegram_id,
                        message.chat.id,
                        apt_id,
                        draft_info=(
                            f"  • Telegram ID: <code>{t_id}</code>\n"
                            f"  • Телефон: <code>{phone}</code>\n"
                            f"  • Имя: <b>{t_name}</b>"
                        ),
                        awaiting_no_agr_confirm=True,
                    )
                    return

                info_msg = await _finalize_tenant_invite(
                    session, bot, apt, current_user.telegram_id, t_id, phone, t_name
                )
                await state.set_state(None)
                await _render_usr_screen(
                    bot, state, current_user.telegram_id, message.chat.id, apt_id, info_msg=info_msg
                )
                return

            if field == "landlord_id":
                if not raw.isdigit():
                    raise ValueError("укажите числовой Telegram ID совладельца (только цифры)")
                l_id = int(raw)
                existing = await session.get(User, l_id)
                if existing and existing.role == "landlord" and existing.family_id != apt.family_id:
                    await state.set_state(None)
                    await _render_usr_screen(
                        bot,
                        state,
                        current_user.telegram_id,
                        message.chat.id,
                        apt_id,
                        error_msg="этот пользователь уже является владельцем в другой семье.",
                    )
                    return
                if existing and existing.role == "admin":
                    await state.set_state(None)
                    await _render_usr_screen(
                        bot,
                        state,
                        current_user.telegram_id,
                        message.chat.id,
                        apt_id,
                        info_msg=f"Пользователь {l_id} уже является администратором.",
                    )
                    return

                await state.update_data(draft_l_id=l_id, usr_field="landlord_name")
                await _render_usr_screen(
                    bot,
                    state,
                    current_user.telegram_id,
                    message.chat.id,
                    apt_id,
                    active_field="landlord_name",
                    draft_info=f"  • Telegram ID совладельца: <code>{l_id}</code>",
                )
                return

            if field == "landlord_name":
                if len(raw) < 2:
                    raise ValueError("укажите имя совладельца (например: Елена)")
                l_id = int(data["draft_l_id"])
                l_name = raw
                fam = await session.get(Family, apt.family_id) if apt.family_id else None
                fam_name = fam.name if fam else "Ваша семья"

                existing = await session.get(User, l_id)
                if existing:
                    existing.role = "landlord"
                    existing.family_id = apt.family_id
                    existing.name = l_name
                    existing.selected_apt_id = apt.id
                    existing.is_active = True
                else:
                    session.add(
                        User(
                            telegram_id=l_id,
                            role="landlord",
                            family_id=apt.family_id,
                            name=l_name,
                            selected_apt_id=apt.id,
                            is_active=True,
                        )
                    )
                await log_audit(
                    session,
                    current_user.telegram_id,
                    "ADD_LANDLORD",
                    f"Совладелец {l_name} (ID {l_id}) в семью ID {apt.family_id}",
                    apt_id,
                )
                await session.commit()
                await set_user_menu_commands(bot, l_id, "landlord")
                info_msg = f"Совладелец <b>{l_name}</b> (ID <code>{l_id}</code>) добавлен в семью «<b>{fam_name}</b>»."

                try:
                    await bot.send_message(
                        chat_id=l_id,
                        text=(
                            f"👋 Здравствуйте, <b>{l_name}</b>!\n"
                            f"Вы добавлены как совладелец в семью «<b>{fam_name}</b>».\n"
                            "Нажмите /start для открытия меню управления квартирами."
                        ),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

                await state.set_state(None)
                await _render_usr_screen(
                    bot, state, current_user.telegram_id, message.chat.id, apt_id, info_msg=info_msg
                )
                return

            if field == "agreement_no":
                if not raw:
                    raise ValueError("укажите номер договора")
                apt.agreement_no = raw
                await log_audit(session, current_user.telegram_id, "SET_AGREEMENT_NO", raw, apt_id)
                await session.commit()
                if wiz_agreement:
                    await state.update_data(usr_field="agreement_date_start")
                    await _render_usr_screen(
                        bot,
                        state,
                        current_user.telegram_id,
                        message.chat.id,
                        apt_id,
                        active_field="agreement_date_start",
                        draft_info=f"  • Шаг 1/3 (№ договора): <b>{raw}</b>",
                    )
                    return
                info_msg = f"Номер договора изменён на <b>№{raw}</b> (наниматель без изменений)."

            elif field == "agreement_date_start":
                dt = datetime.strptime(raw, "%d.%m.%Y").date()
                apt.agreement_date_start = dt
                await log_audit(session, current_user.telegram_id, "SET_AGREEMENT_DATE_START", raw, apt_id)
                await session.commit()
                if wiz_agreement:
                    await state.update_data(usr_field="agreement_date_finish")
                    await _render_usr_screen(
                        bot,
                        state,
                        current_user.telegram_id,
                        message.chat.id,
                        apt_id,
                        active_field="agreement_date_finish",
                        draft_info=(
                            f"  • Шаг 1/3 (№ договора): <b>{apt.agreement_no or '—'}</b>\n"
                            f"  • Шаг 2/3 (Дата договора): <b>{raw}</b>"
                        ),
                    )
                    return
                info_msg = f"Дата начала договора изменена на <b>{raw}</b> (наниматель без изменений)."

            elif field == "agreement_date_finish":
                dt = datetime.strptime(raw, "%d.%m.%Y").date()
                if apt.agreement_date_start and dt < apt.agreement_date_start:
                    raise ValueError(
                        f"дата окончания ({raw}) не может быть раньше даты начала договора "
                        f"({apt.agreement_date_start.strftime('%d.%m.%Y')})"
                    )
                apt.agreement_date_finish = dt
                await log_audit(session, current_user.telegram_id, "SET_AGREEMENT_DATE_FINISH", raw, apt_id)
                await session.commit()
                start_s = apt.agreement_date_start.strftime("%d.%m.%Y") if apt.agreement_date_start else "—"
                info_msg = (
                    f"Данные договора обновлены: <b>№{apt.agreement_no or 'б/н'}</b> "
                    f"от <b>{start_s}</b> до <b>{raw}</b>. Наниматель сохранён без изменений."
                )

        except ValueError as e:
            err = (
                str(e)
                if "strptime" not in str(e)
                else "неверный формат даты. Используйте ДД.ММ.ГГГГ (например: 01.09.2026)"
            )
            await _render_usr_screen(
                bot,
                state,
                current_user.telegram_id,
                message.chat.id,
                apt_id,
                active_field=field,
                error_msg=err,
                draft_info=_current_draft_info(),
            )
            return

    await state.set_state(None)
    await _render_usr_screen(bot, state, current_user.telegram_id, message.chat.id, apt_id, info_msg=info_msg)


# ---------------------------------------------------------------------------
# ПОДАЧА И КОРРЕКТИРОВКА ПОКАЗАНИЙ СЧЁТЧИКОВ (Вода и Электричество раздельно)
# ---------------------------------------------------------------------------
WIZARD_SEQUENCES = {
    "water": ["hvs", "gvs"],
    "elec": ["t1", "t2", "t3"],
}


async def _open_reading_session(
    bot: Bot,
    state: FSMContext,
    user: User,
    chat_id: int,
    apt_id: int,
    mode: str = "water",
    target_period: str | None = None,
) -> None:
    period = target_period or datetime.now(TZ).strftime("%Y-%m")
    async with SessionLocal() as session:
        apt = await session.get(Apartment, apt_id)
        if not _can_access_apartment(user, apt):
            return
        prev = await get_previous_baseline(session, apt, period)
        existing = (
            await session.execute(
                select(MeterReading).where(MeterReading.apartment_id == apt_id, MeterReading.period == period)
            )
        ).scalar_one_or_none()

        if existing:
            draft = {
                "hvs": Decimal(str(existing.hvs)).quantize(Decimal("1")),
                "gvs": Decimal(str(existing.gvs)).quantize(Decimal("1")),
                "t1": Decimal(str(existing.t1)).quantize(Decimal("1")),
                "t2": Decimal(str(existing.t2)).quantize(Decimal("1")),
                "t3": Decimal(str(existing.t3)).quantize(Decimal("1")),
            }
        else:
            draft = dict(prev)

        tariffs = {
            "hvs": Decimal(str(apt.tariff_hvs)),
            "gvs": Decimal(str(apt.tariff_gvs)),
            "t1": Decimal(str(apt.tariff_t1)),
            "t2": Decimal(str(apt.tariff_t2)),
            "t3": Decimal(str(apt.tariff_t3)),
        }
        calc = calculate_consumption_and_cost(prev, draft, tariffs)
        text, kb = render_reading_panel(apt, period, prev, draft, calc, mode=mode)

    await state.set_state(None)
    await state.update_data(
        read_apt_id=apt_id,
        read_period=period,
        read_mode=mode,
        read_prev={k: str(v) for k, v in prev.items()},
        read_draft={k: str(v) for k, v in draft.items()},
        read_touched_fields=[],
        read_water_touched=False,
        read_elec_touched=False,
    )
    await render_to_panel(bot, state, user.telegram_id, chat_id, text, kb)


@router.callback_query(F.data.startswith("read:open:"))
async def cb_read_open(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    parts = call.data.split(":")
    apt_id = int(parts[2])
    mode = parts[3] if len(parts) > 3 else None

    async with SessionLocal() as session:
        apt = await session.get(Apartment, apt_id)
        if not _can_access_apartment(current_user, apt):
            await call.answer("⛔ Доступ к этой квартире запрещён", show_alert=True)
            return
        apt_num = apt.number

    await call.answer()

    if mode not in ("water", "elec"):
        text = (
            f"📝 <b>ПОДАЧА ПОКАЗАНИЙ — Кв. №{apt_num}</b>\n"
            "────────────────────────\n"
            "Выберите, какие показания вы хотите передать:"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💧 Подать показания водомеров", callback_data=f"read:open:{apt_id}:water")],
                [InlineKeyboardButton(text="⚡ Подать показания электроэнергии", callback_data=f"read:open:{apt_id}:elec")],
                [InlineKeyboardButton(text="⬅️ На главную", callback_data="main:home")],
            ]
        )
        await render_to_panel(bot, state, current_user.telegram_id, call.message.chat.id, text, kb)
        return

    await _open_reading_session(bot, state, current_user, call.message.chat.id, apt_id, mode=mode)


@router.callback_query(F.data.startswith("read:fix:"))
async def cb_read_fix_period(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    if current_user.role not in ("admin", "landlord"):
        await call.answer("Исправлять прошлые периоды может только администратор или владелец", show_alert=True)
        return
    parts = call.data.split(":")
    apt_id = int(parts[2])
    period = parts[3]
    mode = parts[4] if len(parts) > 4 else None

    async with SessionLocal() as session:
        apt = await session.get(Apartment, apt_id)
        if not _can_access_apartment(current_user, apt):
            await call.answer("⛔ Доступ запрещён", show_alert=True)
            return

    if mode not in ("water", "elec"):
        await call.answer()
        text = (
            f"✏️ <b>ИСПРАВЛЕНИЕ ПОКАЗАНИЙ ЗА {period}</b>\n"
            "────────────────────────\n"
            "Выберите раздел для корректировки:"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💧 Подать показания водомеров", callback_data=f"read:fix:{apt_id}:{period}:water")],
                [InlineKeyboardButton(text="⚡ Подать показания электроэнергии", callback_data=f"read:fix:{apt_id}:{period}:elec")],
                [InlineKeyboardButton(text="⬅️ Назад в администрирование", callback_data=f"adm:menu:{apt_id}")],
            ]
        )
        await render_to_panel(bot, state, current_user.telegram_id, call.message.chat.id, text, kb)
        return

    await call.answer(f"Редактирование за {period}")
    await _open_reading_session(
        bot, state, current_user, call.message.chat.id, apt_id, mode=mode, target_period=period
    )


@router.callback_query(F.data.startswith("read:back:"))
async def cb_read_back(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    await call.answer()
    data = await state.get_data()
    apt_id = int(call.data.split(":")[2])
    period = data.get("read_period", datetime.now(TZ).strftime("%Y-%m"))
    mode = data.get("read_mode", "water")
    prev = {k: Decimal(v) for k, v in data["read_prev"].items()}
    draft = {k: Decimal(v) for k, v in data["read_draft"].items()}
    await state.set_state(None)

    async with SessionLocal() as session:
        apt = await session.get(Apartment, apt_id)
        if not _can_access_apartment(current_user, apt):
            return
        tariffs = {
            "hvs": Decimal(str(apt.tariff_hvs)),
            "gvs": Decimal(str(apt.tariff_gvs)),
            "t1": Decimal(str(apt.tariff_t1)),
            "t2": Decimal(str(apt.tariff_t2)),
            "t3": Decimal(str(apt.tariff_t3)),
        }
        calc = calculate_consumption_and_cost(prev, draft, tariffs)
        text, kb = render_reading_panel(apt, period, prev, draft, calc, mode=mode)
    await render_to_panel(bot, state, current_user.telegram_id, call.message.chat.id, text, kb)


@router.callback_query(F.data.startswith("read:field:"))
async def cb_read_single_field(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    await call.answer()
    _, _, apt_id_s, field = call.data.split(":")
    apt_id = int(apt_id_s)
    data = await state.get_data()
    prev = {k: Decimal(v) for k, v in data["read_prev"].items()}
    draft = {k: Decimal(v) for k, v in data["read_draft"].items()}
    period = data["read_period"]
    mode = data.get("read_mode", "water")

    await state.set_state(PanelStates.entering_reading)
    await state.update_data(read_active_field=field, read_seq=None, read_seq_idx=None)

    async with SessionLocal() as session:
        apt = await session.get(Apartment, apt_id)
        if not _can_access_apartment(current_user, apt):
            return
        tariffs = {
            "hvs": Decimal(str(apt.tariff_hvs)),
            "gvs": Decimal(str(apt.tariff_gvs)),
            "t1": Decimal(str(apt.tariff_t1)),
            "t2": Decimal(str(apt.tariff_t2)),
            "t3": Decimal(str(apt.tariff_t3)),
        }
        calc = calculate_consumption_and_cost(prev, draft, tariffs)
        text, kb = render_reading_panel(apt, period, prev, draft, calc, mode=mode, active_field=field)
    await render_to_panel(bot, state, current_user.telegram_id, call.message.chat.id, text, kb)


@router.callback_query(F.data.startswith("read:wiz:"))
async def cb_read_wizard(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    await call.answer("Пошаговый ввод показаний")
    _, _, apt_id_s, wiz_mode = call.data.split(":")
    apt_id = int(apt_id_s)
    mode = "elec" if wiz_mode == "elec" else "water"
    seq = WIZARD_SEQUENCES[mode]
    first_field = seq[0]

    data = await state.get_data()
    prev = {k: Decimal(v) for k, v in data["read_prev"].items()}
    draft = {k: Decimal(v) for k, v in data["read_draft"].items()}
    period = data["read_period"]

    await state.set_state(PanelStates.entering_reading)
    await state.update_data(read_mode=mode, read_active_field=first_field, read_seq=seq, read_seq_idx=0)

    async with SessionLocal() as session:
        apt = await session.get(Apartment, apt_id)
        if not _can_access_apartment(current_user, apt):
            return
        tariffs = {
            "hvs": Decimal(str(apt.tariff_hvs)),
            "gvs": Decimal(str(apt.tariff_gvs)),
            "t1": Decimal(str(apt.tariff_t1)),
            "t2": Decimal(str(apt.tariff_t2)),
            "t3": Decimal(str(apt.tariff_t3)),
        }
        calc = calculate_consumption_and_cost(prev, draft, tariffs)
        text, kb = render_reading_panel(
            apt, period, prev, draft, calc, mode=mode, active_field=first_field, step_info=f"Шаг 1 из {len(seq)}"
        )
    await render_to_panel(bot, state, current_user.telegram_id, call.message.chat.id, text, kb)


@router.message(PanelStates.entering_reading)
async def msg_reading_input(message: Message, state: FSMContext, bot: Bot, current_user: User) -> None:
    await safe_delete_message(message)
    data = await state.get_data()
    apt_id: int = data["read_apt_id"]
    period: str = data["read_period"]
    mode: str = data.get("read_mode", "water")
    field: str = data["read_active_field"]
    seq: list[str] | None = data.get("read_seq")
    seq_idx: int | None = data.get("read_seq_idx")
    prev = {k: Decimal(v) for k, v in data["read_prev"].items()}
    draft = {k: Decimal(v) for k, v in data["read_draft"].items()}
    touched_fields: list[str] = list(data.get("read_touched_fields", []))

    async with SessionLocal() as session:
        apt = await session.get(Apartment, apt_id)
        if not _can_access_apartment(current_user, apt):
            return
        tariffs = {
            "hvs": Decimal(str(apt.tariff_hvs)),
            "gvs": Decimal(str(apt.tariff_gvs)),
            "t1": Decimal(str(apt.tariff_t1)),
            "t2": Decimal(str(apt.tariff_t2)),
            "t3": Decimal(str(apt.tariff_t3)),
        }

        try:
            val = parse_int_reading(message.text or "")
        except ValueError as e:
            calc = calculate_consumption_and_cost(prev, draft, tariffs)
            step_str = f"Шаг {(seq_idx or 0) + 1} из {len(seq)}" if seq else None
            text, kb = render_reading_panel(
                apt,
                period,
                prev,
                draft,
                calc,
                mode=mode,
                active_field=field,
                step_info=step_str,
                error_msg=str(e),
            )
            await render_to_panel(bot, state, current_user.telegram_id, message.chat.id, text, kb)
            return

        draft[field] = val
        if field not in touched_fields:
            touched_fields.append(field)

        water_touched = data.get("read_water_touched", False) or (field in ("hvs", "gvs"))
        elec_touched = data.get("read_elec_touched", False) or (field in ("t1", "t2", "t3"))
        await state.update_data(
            read_draft={k: str(v) for k, v in draft.items()},
            read_touched_fields=touched_fields,
            read_water_touched=water_touched,
            read_elec_touched=elec_touched,
        )

        if seq and seq_idx is not None:
            if seq_idx + 1 < len(seq):
                next_idx = seq_idx + 1
                next_field = seq[next_idx]
                await state.update_data(read_active_field=next_field, read_seq_idx=next_idx)
                calc = calculate_consumption_and_cost(prev, draft, tariffs)
                text, kb = render_reading_panel(
                    apt,
                    period,
                    prev,
                    draft,
                    calc,
                    mode=mode,
                    active_field=next_field,
                    step_info=f"Шаг {next_idx + 1} из {len(seq)}",
                )
            else:
                await state.set_state(None)
                calc = calculate_consumption_and_cost(prev, draft, tariffs)
                text, kb = render_reading_panel(apt, period, prev, draft, calc, mode=mode, awaiting_confirm=True)
        else:
            await state.set_state(None)
            calc = calculate_consumption_and_cost(prev, draft, tariffs)
            text, kb = render_reading_panel(apt, period, prev, draft, calc, mode=mode, awaiting_confirm=False)

    await render_to_panel(bot, state, current_user.telegram_id, message.chat.id, text, kb)


@router.callback_query(F.data.startswith("read:preview:"))
async def cb_read_preview(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    await call.answer()
    apt_id = int(call.data.split(":")[2])
    data = await state.get_data()
    prev = {k: Decimal(v) for k, v in data["read_prev"].items()}
    draft = {k: Decimal(v) for k, v in data["read_draft"].items()}
    period = data["read_period"]
    mode = data.get("read_mode", "water")

    async with SessionLocal() as session:
        apt = await session.get(Apartment, apt_id)
        if not _can_access_apartment(current_user, apt):
            return
        tariffs = {
            "hvs": Decimal(str(apt.tariff_hvs)),
            "gvs": Decimal(str(apt.tariff_gvs)),
            "t1": Decimal(str(apt.tariff_t1)),
            "t2": Decimal(str(apt.tariff_t2)),
            "t3": Decimal(str(apt.tariff_t3)),
        }
        calc = calculate_consumption_and_cost(prev, draft, tariffs)
        text, kb = render_reading_panel(apt, period, prev, draft, calc, mode=mode, awaiting_confirm=True)
    await render_to_panel(bot, state, current_user.telegram_id, call.message.chat.id, text, kb)


@router.callback_query(F.data.startswith("read:commit:"))
async def cb_read_commit(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    _, _, apt_id_s, commit_mode = call.data.split(":")
    apt_id = int(apt_id_s)
    allow_decrease = commit_mode == "force"

    data = await state.get_data()
    if not data.get("read_draft"):
        await call.answer("Показания уже сохранены!", show_alert=True)
        return

    prev = {k: Decimal(v) for k, v in data["read_prev"].items()}
    draft = {k: Decimal(v) for k, v in data["read_draft"].items()}
    period = data["read_period"]
    mode = data.get("read_mode", "water")
    touched_fields: list[str] = list(data.get("read_touched_fields", []))
    water_touched = (mode == "water") or data.get("read_water_touched", False)
    elec_touched = (mode == "elec") or data.get("read_elec_touched", False)

    async with SessionLocal() as session:
        apt = await session.get(Apartment, apt_id)
        if not _can_access_apartment(current_user, apt):
            return
        tariffs = {
            "hvs": Decimal(str(apt.tariff_hvs)),
            "gvs": Decimal(str(apt.tariff_gvs)),
            "t1": Decimal(str(apt.tariff_t1)),
            "t2": Decimal(str(apt.tariff_t2)),
            "t3": Decimal(str(apt.tariff_t3)),
        }
        calc = calculate_consumption_and_cost(prev, draft, tariffs, allow_decrease=allow_decrease)
        relevant_decreased = (
            [f for f in calc.decreased_fields if f in ("ХВС", "ГВС")]
            if mode == "water"
            else [f for f in calc.decreased_fields if f in ("Т1", "Т2", "Т3")]
        )
        if relevant_decreased and not allow_decrease:
            await call.answer("Подтвердите замену/сброс счётчика специальной кнопкой!", show_alert=True)
            return

        existing = (
            await session.execute(
                select(MeterReading).where(
                    MeterReading.apartment_id == apt_id,
                    MeterReading.period == period,
                )
            )
        ).scalar_one_or_none()

        now_dt = datetime.now(TZ)
        if existing:
            r = existing
            r.reading_date = now_dt.date()
            r.submitted_by = current_user.telegram_id
            r.prev_hvs, r.prev_gvs = calc.prev_hvs, calc.prev_gvs
            r.prev_t1, r.prev_t2, r.prev_t3 = calc.prev_t1, calc.prev_t2, calc.prev_t3
            r.hvs, r.gvs = calc.hvs, calc.gvs
            r.t1, r.t2, r.t3 = calc.t1, calc.t2, calc.t3
            r.diff_hvs, r.diff_gvs = calc.diff_hvs, calc.diff_gvs
            r.diff_t1, r.diff_t2, r.diff_t3 = calc.diff_t1, calc.diff_t2, calc.diff_t3
            r.tariff_hvs, r.tariff_gvs = tariffs["hvs"], tariffs["gvs"]
            r.tariff_t1, r.tariff_t2, r.tariff_t3 = tariffs["t1"], tariffs["t2"], tariffs["t3"]
            r.cost_hvs, r.cost_gvs, r.cost_water_total = calc.cost_hvs, calc.cost_gvs, calc.cost_water_total
            r.cost_t1, r.cost_t2, r.cost_t3 = calc.cost_t1, calc.cost_t2, calc.cost_t3
            r.cost_elec_total, r.cost_total = calc.cost_elec_total, calc.cost_total
            r.water_submitted = r.water_submitted or water_touched
            r.elec_submitted = r.elec_submitted or elec_touched
            action_name = f"UPDATE_READING_{mode.upper()}"
        else:
            r = MeterReading(
                apartment_id=apt_id,
                period=period,
                reading_date=now_dt.date(),
                created_at=now_dt.replace(tzinfo=None),
                submitted_by=current_user.telegram_id,
                prev_hvs=calc.prev_hvs,
                prev_gvs=calc.prev_gvs,
                prev_t1=calc.prev_t1,
                prev_t2=calc.prev_t2,
                prev_t3=calc.prev_t3,
                hvs=calc.hvs,
                gvs=calc.gvs,
                t1=calc.t1,
                t2=calc.t2,
                t3=calc.t3,
                diff_hvs=calc.diff_hvs,
                diff_gvs=calc.diff_gvs,
                diff_t1=calc.diff_t1,
                diff_t2=calc.diff_t2,
                diff_t3=calc.diff_t3,
                tariff_hvs=tariffs["hvs"],
                tariff_gvs=tariffs["gvs"],
                tariff_t1=tariffs["t1"],
                tariff_t2=tariffs["t2"],
                tariff_t3=tariffs["t3"],
                cost_hvs=calc.cost_hvs,
                cost_gvs=calc.cost_gvs,
                cost_water_total=calc.cost_water_total,
                cost_t1=calc.cost_t1,
                cost_t2=calc.cost_t2,
                cost_t3=calc.cost_t3,
                cost_elec_total=calc.cost_elec_total,
                cost_total=calc.cost_total,
                water_submitted=water_touched,
                elec_submitted=elec_touched,
            )
            session.add(r)
            action_name = f"CREATE_READING_{mode.upper()}"

        await log_audit(
            session,
            current_user.telegram_id,
            action_name,
            f"Период {period}: ХВС={fmt_dec(calc.hvs, 0)}, ГВС={fmt_dec(calc.gvs, 0)}, Т1={fmt_dec(calc.t1, 0)}, Т2={fmt_dec(calc.t2, 0)}, Т3={fmt_dec(calc.t3, 0)}",
            apt_id,
        )

        # Уведомляем ТОЛЬКО владельцев этой семьи и администратора
        notify_ids = await _get_family_recipient_ids(
            session, apt.family_id, exclude_user_id=current_user.telegram_id
        )
        apt_num = apt.number
        await session.commit()

    await state.update_data(read_draft=None)
    await call.answer("✅ Показания успешно сохранены!")

    default_fields = ["hvs", "gvs"] if mode == "water" else ["t1", "t2", "t3"]
    fields_to_report = touched_fields if touched_fields else default_fields

    indicator_lines: list[str] = []
    if "hvs" in fields_to_report:
        indicator_lines.append(
            f"💧 Вода: ХВС - <b>{fmt_dec(calc.hvs, 0)} м³</b> ({fmt_dec(calc.prev_hvs, 0)} м³ -> {fmt_dec(calc.hvs, 0)} м³)"
        )
    if "gvs" in fields_to_report:
        indicator_lines.append(
            f"💧 Вода: ГВС - <b>{fmt_dec(calc.gvs, 0)} м³</b> ({fmt_dec(calc.prev_gvs, 0)} м³ -> {fmt_dec(calc.gvs, 0)} м³)"
        )
    if "t1" in fields_to_report:
        indicator_lines.append(
            f"⚡ Свет: Т1 - <b>{fmt_dec(calc.t1, 0)} кВт·ч</b> ({fmt_dec(calc.prev_t1, 0)} кВт·ч -> {fmt_dec(calc.t1, 0)} кВт·ч)"
        )
    if "t2" in fields_to_report:
        indicator_lines.append(
            f"⚡ Свет: Т2 - <b>{fmt_dec(calc.t2, 0)} кВт·ч</b> ({fmt_dec(calc.prev_t2, 0)} кВт·ч -> {fmt_dec(calc.t2, 0)} кВт·ч)"
        )
    if "t3" in fields_to_report:
        indicator_lines.append(
            f"⚡ Свет: Т3 - <b>{fmt_dec(calc.t3, 0)} кВт·ч</b> ({fmt_dec(calc.prev_t3, 0)} кВт·ч -> {fmt_dec(calc.t3, 0)} кВт·ч)"
        )

    notify_text = (
        f"🔔 <b>Новые показания счётчиков в квартире №{apt_num} (период {period})!</b>\n"
        f"Автор: <b>{current_user.name or '—'}</b> (<code>{current_user.telegram_id}</code>)\n"
        + "\n".join(indicator_lines)
    )
    for target_id in notify_ids:
        try:
            await bot.send_message(chat_id=target_id, text=notify_text, parse_mode="HTML")
        except Exception:
            pass

    await show_home_panel(bot, state, current_user, call.message.chat.id)


# ---------------------------------------------------------------------------
# ИСТОРИЯ И СРАВНЕНИЕ МЕСЯЦЕВ
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("hist:view:"))
async def cb_hist_view(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    _, _, apt_id_s, idx_s = call.data.split(":")
    apt_id, idx = int(apt_id_s), int(idx_s)
    async with SessionLocal() as session:
        apt = await session.get(Apartment, apt_id)
        if not _can_access_apartment(current_user, apt):
            await call.answer("⛔ Доступ запрещён", show_alert=True)
            return
        readings = list(
            (
                await session.execute(
                    select(MeterReading)
                    .where(MeterReading.apartment_id == apt_id)
                    .order_by(desc(MeterReading.period))
                )
            )
            .scalars()
            .all()
        )
        text, kb = render_history_panel(apt, readings, idx=idx)
    await call.answer()
    await render_to_panel(bot, state, current_user.telegram_id, call.message.chat.id, text, kb)


@router.callback_query(F.data.startswith("hist:cmp:"))
async def cb_hist_compare(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    _, _, apt_id_s, idx_s, cmp_s = call.data.split(":")
    apt_id, idx, cmp_idx = int(apt_id_s), int(idx_s), int(cmp_s)
    async with SessionLocal() as session:
        apt = await session.get(Apartment, apt_id)
        if not _can_access_apartment(current_user, apt):
            await call.answer("⛔ Доступ запрещён", show_alert=True)
            return
        readings = list(
            (
                await session.execute(
                    select(MeterReading)
                    .where(MeterReading.apartment_id == apt_id)
                    .order_by(desc(MeterReading.period))
                )
            )
            .scalars()
            .all()
        )
        text, kb = render_history_panel(apt, readings, idx=idx, cmp_idx=cmp_idx)
    await call.answer("Сравнение периодов")
    await render_to_panel(bot, state, current_user.telegram_id, call.message.chat.id, text, kb)


# ---------------------------------------------------------------------------
# ВЫГРУЗКА БАЗЫ В MS EXCEL И PDF (В пределах семьи)
# ---------------------------------------------------------------------------
def _build_export_menu_view(apt_id: int, apt_num: str) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "📥 <b>ВЫГРУЗКА ДАННЫХ (MS Excel / PDF)</b>\n"
        "────────────────────────\n"
        "Выберите формат файла и охват выгрузки (только выбранная квартира или все квартиры вашей семьи):"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f"📊 Excel (Кв. №{apt_num})", callback_data=f"exp:run:xlsx:{apt_id}"),
                InlineKeyboardButton(text="📊 Excel (Все квартиры семьи)", callback_data="exp:run:xlsx:0"),
            ],
            [
                InlineKeyboardButton(text=f"📄 PDF (Кв. №{apt_num})", callback_data=f"exp:run:pdf:{apt_id}"),
                InlineKeyboardButton(text="📄 PDF (Все квартиры семьи)", callback_data="exp:run:pdf:0"),
            ],
            [InlineKeyboardButton(text="⬅️ На главную", callback_data="main:home")],
        ]
    )
    return text, kb


@router.callback_query(F.data.startswith("exp:menu:"))
async def cb_export_menu(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    if current_user.role not in ("admin", "landlord"):
        await call.answer("Недостаточно прав", show_alert=True)
        return
    apt_id = int(call.data.split(":")[2])
    async with SessionLocal() as session:
        apt = await session.get(Apartment, apt_id)
        if not _can_access_apartment(current_user, apt):
            await call.answer("⛔ Доступ запрещён", show_alert=True)
            return
        apt_num = apt.number if apt else str(apt_id)
    await call.answer()
    text, kb = _build_export_menu_view(apt_id, apt_num)
    await render_to_panel(bot, state, current_user.telegram_id, call.message.chat.id, text, kb)


@router.callback_query(F.data.startswith("exp:run:"))
async def cb_export_run(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    if current_user.role not in ("admin", "landlord"):
        await call.answer("Недостаточно прав", show_alert=True)
        return
    _, _, fmt, apt_id_s = call.data.split(":")
    target_apt_id = int(apt_id_s) if int(apt_id_s) > 0 else None

    if target_apt_id:
        async with SessionLocal() as session:
            apt = await session.get(Apartment, target_apt_id)
            if not _can_access_apartment(current_user, apt):
                await call.answer("⛔ Доступ запрещён", show_alert=True)
                return

    await call.answer("Формирую файл выгрузки...")

    if fmt == "xlsx":
        path = await generate_excel_report(target_apt_id, family_id=current_user.family_id)
    else:
        path = await generate_pdf_report(target_apt_id, family_id=current_user.family_id)

    await call.message.answer_document(
        document=FSInputFile(str(path)),
        caption=f"📥 Выгрузка базы данных ({fmt.upper()}) от {datetime.now(TZ).strftime('%d.%m.%Y %H:%M')}",
    )
    path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# АДМИНИСТРИРОВАНИЕ
# ---------------------------------------------------------------------------
async def _open_admin_view(
    bot: Bot, state: FSMContext, user: User, chat_id: int, apt_id: int, subview: str = "overview"
) -> None:
    async with SessionLocal() as session:
        apt = await session.get(Apartment, apt_id)
        readings = list(
            (
                await session.execute(
                    select(MeterReading)
                    .where(MeterReading.apartment_id == apt_id)
                    .order_by(desc(MeterReading.period))
                )
            )
            .scalars()
            .all()
        )
        tariffs = list(
            (
                await session.execute(
                    select(TariffHistory)
                    .where(TariffHistory.apartment_id == apt_id)
                    .order_by(desc(TariffHistory.effective_from))
                )
            )
            .scalars()
            .all()
        )
        logs = list((await session.execute(select(AuditLog).order_by(desc(AuditLog.id)).limit(20))).scalars().all())
        users = list((await session.execute(select(User).order_by(User.role))).scalars().all())
        text, kb = render_admin_panel(apt, readings, tariffs, logs, users, subview=subview)
    await render_to_panel(bot, state, user.telegram_id, chat_id, text, kb)


@router.callback_query(F.data.startswith("adm:menu:"))
async def cb_adm_menu(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    if current_user.role != "admin":
        await call.answer("Раздел доступен исключительно администратору", show_alert=True)
        return
    await call.answer()
    apt_id = int(call.data.split(":")[2])
    await _open_admin_view(bot, state, current_user, call.message.chat.id, apt_id, "overview")


@router.callback_query(F.data.startswith("adm:sub:"))
async def cb_adm_sub(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    if current_user.role != "admin":
        await call.answer("Раздел доступен исключительно администратору", show_alert=True)
        return
    await call.answer()
    _, _, apt_id_s, sub = call.data.split(":")
    await _open_admin_view(bot, state, current_user, call.message.chat.id, int(apt_id_s), sub)


@router.callback_query(F.data.startswith("adm:backup:"))
async def cb_adm_backup(call: CallbackQuery, current_user: User) -> None:
    if current_user.role != "admin":
        await call.answer("Недостаточно прав", show_alert=True)
        return
    b_path = create_db_backup()
    await call.answer(f"✅ Резервная копия создана: {b_path.name}", show_alert=True)


# ---------------------------------------------------------------------------
# ОТПРАВКА ФОТОГРАФИЙ СЧЁТЧИКОВ НАНИМАТЕЛЕМ ВЛАДЕЛЬЦУ КВАРТИРЫ (В пределах семьи)
# ---------------------------------------------------------------------------
@router.message(Command("photo"))
async def cmd_menu_photo(message: Message, state: FSMContext, bot: Bot, current_user: User) -> None:
    await safe_delete_message(message)
    async with SessionLocal() as session:
        apt, _ = await _get_active_apartment(session, current_user)
    if not apt:
        await show_home_panel(bot, state, current_user, message.chat.id, force_new=True)
        return
    await state.set_state(PanelStates.sending_photo)
    await state.update_data(photo_apt_id=apt.id)
    text, kb = render_photo_panel(apt)
    await render_to_panel(bot, state, current_user.telegram_id, message.chat.id, text, kb, force_new=True)


@router.callback_query(F.data.startswith("photo:open:"))
async def cb_photo_open(call: CallbackQuery, state: FSMContext, bot: Bot, current_user: User) -> None:
    apt_id = int(call.data.split(":")[2])
    async with SessionLocal() as session:
        apt = await session.get(Apartment, apt_id)
        if not _can_access_apartment(current_user, apt):
            await call.answer("Доступ разрешён только к вашей квартире", show_alert=True)
            return
        text, kb = render_photo_panel(apt)
    await call.answer()
    await state.set_state(PanelStates.sending_photo)
    await state.update_data(photo_apt_id=apt_id)
    await render_to_panel(bot, state, current_user.telegram_id, call.message.chat.id, text, kb)


@router.message(F.photo)
async def msg_handle_meter_photo(message: Message, state: FSMContext, bot: Bot, current_user: User) -> None:
    await safe_delete_message(message)
    data = await state.get_data()
    apt_id = data.get("photo_apt_id") or current_user.apartment_id

    async with SessionLocal() as session:
        if apt_id:
            apt = await session.get(Apartment, int(apt_id))
        else:
            apt, _ = await _get_active_apartment(session, current_user)

        if not _can_access_apartment(current_user, apt):
            return

        target_ids = await _get_family_recipient_ids(
            session, apt.family_id, exclude_user_id=current_user.telegram_id
        )
        if not target_ids:
            target_ids = await _get_family_recipient_ids(session, apt.family_id)

        await log_audit(
            session,
            current_user.telegram_id,
            "SEND_METER_PHOTO",
            f"Отправлено фото счётчика по кв. №{apt.number}",
            apt.id,
        )
        await session.commit()

    file_id = message.photo[-1].file_id
    user_caption = (message.caption or "").strip()
    caption_lines = [
        f"📷 <b>Фото счётчика — Квартира №{apt.number}</b>",
        f"От нанимателя: <b>{current_user.name or '—'}</b> (ID <code>{current_user.telegram_id}</code>)",
    ]
    if user_caption:
        caption_lines.append(f"Комментарий: <i>{user_caption}</i>")
    caption_text = "\n".join(caption_lines)

    for target_id in target_ids:
        try:
            await bot.send_photo(chat_id=target_id, photo=file_id, caption=caption_text, parse_mode="HTML")
        except Exception:
            pass

    await state.set_state(PanelStates.sending_photo)
    await state.update_data(photo_apt_id=apt.id)
    text, kb = render_photo_panel(
        apt,
        info_msg="Фотография счётчика успешно отправлена владельцу квартиры! Вы можете отправить ещё одно фото или вернуться в меню.",
    )
    await render_to_panel(bot, state, current_user.telegram_id, message.chat.id, text, kb)


@router.message(F.document)
async def msg_handle_meter_document_image(message: Message, state: FSMContext, bot: Bot, current_user: User) -> None:
    await safe_delete_message(message)
    mime = (message.document.mime_type or "") if message.document else ""
    if not mime.startswith("image/"):
        return

    data = await state.get_data()
    apt_id = data.get("photo_apt_id") or current_user.apartment_id

    async with SessionLocal() as session:
        if apt_id:
            apt = await session.get(Apartment, int(apt_id))
        else:
            apt, _ = await _get_active_apartment(session, current_user)
        if not _can_access_apartment(current_user, apt):
            return

        target_ids = await _get_family_recipient_ids(
            session, apt.family_id, exclude_user_id=current_user.telegram_id
        )
        if not target_ids:
            target_ids = await _get_family_recipient_ids(session, apt.family_id)

        await log_audit(
            session,
            current_user.telegram_id,
            "SEND_METER_PHOTO_DOC",
            f"Отправлено фото (файлом) по кв. №{apt.number}",
            apt.id,
        )
        await session.commit()

    file_id = message.document.file_id
    user_caption = (message.caption or "").strip()
    caption_lines = [
        f"📷 <b>Фото счётчика — Квартира №{apt.number}</b>",
        f"От нанимателя: <b>{current_user.name or '—'}</b> (ID <code>{current_user.telegram_id}</code>)",
    ]
    if user_caption:
        caption_lines.append(f"Комментарий: <i>{user_caption}</i>")

    for target_id in target_ids:
        try:
            await bot.send_document(
                chat_id=target_id,
                document=file_id,
                caption="\n".join(caption_lines),
                parse_mode="HTML",
            )
        except Exception:
            pass

    await state.set_state(PanelStates.sending_photo)
    await state.update_data(photo_apt_id=apt.id)
    text, kb = render_photo_panel(
        apt,
        info_msg="Фотография счётчика успешно отправлена владельцу квартиры! Вы можете отправить ещё одно фото или вернуться в меню.",
    )
    await render_to_panel(bot, state, current_user.telegram_id, message.chat.id, text, kb)


@router.message(PanelStates.sending_photo)
async def msg_sending_photo_wrong_input(message: Message, state: FSMContext, bot: Bot, current_user: User) -> None:
    await safe_delete_message(message)
    data = await state.get_data()
    apt_id = data.get("photo_apt_id") or current_user.apartment_id
    async with SessionLocal() as session:
        apt = await session.get(Apartment, int(apt_id)) if apt_id else None
        if not apt:
            return
        text, kb = render_photo_panel(
            apt,
            error_msg="пожалуйста, прикрепите и отправьте именно фотографию (через значок 📎 или камеру).",
        )
    await render_to_panel(bot, state, current_user.telegram_id, message.chat.id, text, kb)


@router.message()
async def msg_cleanup_fallback(message: Message) -> None:
    await safe_delete_message(message)

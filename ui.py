from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from database import CalculationResult, SessionLocal
from models import Apartment, AuditLog, Family, MeterReading, TariffHistory, User


def fmt_dec(val: Decimal | float | int, places: int = 0) -> str:
    d = Decimal(str(val)).quantize(
        Decimal("1" if places == 0 else f"0.{'0' * places}"),
        rounding=ROUND_HALF_UP,
    )
    if places == 0:
        return f"{int(d)}"
    return f"{d:.{places}f}"


def fmt_date(d: date | None) -> str:
    return d.strftime("%d.%m.%Y") if d else "не указана"


async def safe_delete_message(msg: Message) -> None:
    try:
        await msg.delete()
    except TelegramBadRequest:
        pass


async def render_to_panel(
    bot: Bot,
    state: FSMContext,
    user_id: int,
    chat_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup,
    force_new: bool = False,
) -> int:
    data = await state.get_data()
    panel_msg_id = data.get("panel_msg_id")

    if not panel_msg_id and not force_new:
        async with SessionLocal() as session:
            u = await session.get(User, user_id)
            if u and u.panel_msg_id:
                panel_msg_id = u.panel_msg_id

    if force_new:
        async with SessionLocal() as session:
            u = await session.get(User, user_id)
            old_id = panel_msg_id or (u.panel_msg_id if u else None)
        if old_id:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=old_id)
            except TelegramBadRequest:
                pass

    if panel_msg_id and not force_new:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=panel_msg_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
            await state.update_data(panel_msg_id=panel_msg_id)
            return panel_msg_id
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                return panel_msg_id
            try:
                await bot.delete_message(chat_id=chat_id, message_id=panel_msg_id)
            except TelegramBadRequest:
                pass

    sent = await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
        parse_mode="HTML",
    )
    await state.update_data(panel_msg_id=sent.message_id)
    async with SessionLocal() as session:
        async with session.begin():
            u = await session.get(User, user_id)
            if u:
                u.panel_msg_id = sent.message_id
    return sent.message_id


# ---------------------------------------------------------------------------
# 1. ГЛАВНОЕ МЕНЮ (/start — только кнопки одна под другой)
# ---------------------------------------------------------------------------
def build_main_dashboard(
    user: User,
    apt: Apartment | None,
    all_apts: list[Apartment],
    latest_reading: MeterReading | None,
    show_welcome: bool = False,
) -> tuple[str, InlineKeyboardMarkup]:
    empty_header = "📋 <b>Меню</b>"

    if not apt:
        rows_empty: list[list[InlineKeyboardButton]] = []
        if user.role == "admin":
            rows_empty.append([InlineKeyboardButton(text="👨‍👩‍👧‍👦 Семьи", callback_data="fam:menu")])
        if user.role in ("admin", "landlord"):
            rows_empty.append([InlineKeyboardButton(text="➕ Создать квартиру", callback_data="apt:create")])
            rows_empty.append([InlineKeyboardButton(text="ℹ️ Общая информация", callback_data="main:welcome")])
        else:
            rows_empty.append([InlineKeyboardButton(text="ℹ️ Общая информация", callback_data="main:tenant_info")])
        return empty_header, InlineKeyboardMarkup(inline_keyboard=rows_empty)

    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="💧 Подать показания водомеров", callback_data=f"read:open:{apt.id}:water")],
        [InlineKeyboardButton(text="⚡ Подать показания электроэнергии", callback_data=f"read:open:{apt.id}:elec")],
        [InlineKeyboardButton(text="📅 История и сравнение месяцев", callback_data=f"hist:view:{apt.id}:0")],
    ]

    if user.role == "admin":
        rows.append([InlineKeyboardButton(text="👨‍👩‍👧‍👦 Семьи", callback_data="fam:menu")])

    if user.role in ("admin", "landlord"):
        rows.append([InlineKeyboardButton(text="🏢 Квартиры", callback_data="apt:menu")])
        rows.append([InlineKeyboardButton(text="👥 Договор и жильцы", callback_data=f"usr:open:{apt.id}")])
        rows.append([InlineKeyboardButton(text="📥 Выгрузка Excel / PDF", callback_data=f"exp:menu:{apt.id}")])

    if user.role == "admin":
        rows.append([InlineKeyboardButton(text="🛡 Администрирование и аудит", callback_data=f"adm:menu:{apt.id}")])

    if user.role in ("admin", "landlord"):
        rows.append([InlineKeyboardButton(text="ℹ️ Общая информация", callback_data="main:welcome")])
    elif user.role == "tenant":
        rows.insert(
            2,
            [InlineKeyboardButton(text="📷 Отправить фото счётчиков", callback_data=f"photo:open:{apt.id}")],
        )
        rows.append([InlineKeyboardButton(text="ℹ️ Общая информация", callback_data="main:tenant_info")])

    return empty_header, InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# 1.1. УПРАВЛЕНИЕ СЕМЬЯМИ (/families — все кнопки одна под другой + удаление семьи)
# ---------------------------------------------------------------------------
FAM_FIELD_LABELS = {
    "create_family": ("Название новой семьи", "например: Семья Литвиновых"),
    "rename_family": ("Новое название семьи", "например: Семья Ивановых"),
    "owner_id": ("Шаг 1 из 2 — Telegram ID владельца квартиры в этой семье", "только цифры, например: 123456789"),
    "owner_name": ("Шаг 2 из 2 — Имя владельца квартиры", "например: Сергей"),
}


def render_families_menu(
    current_family: Family | None,
    all_families: list[Family],
    apt_counts: dict[int, int],
    owners: list[User],
    active_field: str | None = None,
    error_msg: str | None = None,
    info_msg: str | None = None,
    draft_info: str | None = None,
    confirm_delete: bool = False,
) -> tuple[str, InlineKeyboardMarkup]:
    lines = [
        "👨‍👩‍👧‍👦 <b>УПРАВЛЕНИЕ СЕМЬЯМИ (Доступ администратора)</b>",
        "────────────────────────",
    ]
    if current_family:
        lines.append(f"Активная семья: <b>{current_family.name}</b> (ID: {current_family.id})")
        lines.append(f"🏢 Квартир в семье: <b>{apt_counts.get(current_family.id, 0)}</b>")
        lines.append("\n🔑 <b>Владельцы / Совладельцы этой семьи:</b>")
        if owners:
            for o in owners:
                lines.append(f"  • <b>{o.name or 'Без имени'}</b> — ID: <code>{o.telegram_id}</code>")
        else:
            lines.append("  • Владельцы ещё не добавлены")
    else:
        lines.append("Семья не выбрана.")

    if draft_info:
        lines.append(f"\n────────────────────────\n📝 <b>Введённые данные:</b>\n{draft_info}")
    if info_msg:
        lines.append(f"\nℹ️ {info_msg}")
    if error_msg:
        lines.append(f"\n⚠️ <b>Ошибка:</b> {error_msg}")

    if confirm_delete and current_family:
        lines.append(
            "\n────────────────────────\n"
            f"⚠️ <b>Внимание! Вы действительно хотите удалить семью «{current_family.name}»?</b>\n"
            "Будут удалены все квартиры этой семьи, показания счётчиков и привязанные к ней пользователи."
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"⚠️ Да, удалить семью «{current_family.name}»",
                        callback_data=f"fam:del_confirm:{current_family.id}",
                    )
                ],
                [InlineKeyboardButton(text="◀ Отмена", callback_data="fam:menu")],
            ]
        )
        return "\n".join(lines), kb

    if active_field and active_field in FAM_FIELD_LABELS:
        label, ex = FAM_FIELD_LABELS[active_field]
        lines.append(
            "\n────────────────────────\n"
            f"👉 <b>Сейчас вводится: {label}</b>\n"
            f"Отправьте значение в чат ({ex}):"
        )

    # Все кнопки строго одна под другой
    rows: list[list[InlineKeyboardButton]] = []
    if all_families:
        lines.append("\nСписок семей (нажмите для переключения):")
        for f in all_families:
            prefix = "✅ " if (current_family and f.id == current_family.id) else "👨‍👩‍👧‍👦 "
            cnt = apt_counts.get(f.id, 0)
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"{prefix}{f.name} (квартир: {cnt})",
                        callback_data=f"fam:sel:{f.id}",
                    )
                ]
            )

    rows.append([InlineKeyboardButton(text="➕ Создать семью", callback_data="fam:create")])
    if current_family:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔑 Добавить владельца в семью",
                    callback_data=f"fam:add_owner:{current_family.id}",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="✏️ Переименовать семью",
                    callback_data=f"fam:rename:{current_family.id}",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="🗑 Удалить семью",
                    callback_data=f"fam:del_ask:{current_family.id}",
                )
            ]
        )
        rows.append([InlineKeyboardButton(text="🏢 Квартиры этой семьи", callback_data="apt:menu")])

    if active_field:
        rows.append([InlineKeyboardButton(text="◀ Отмена ввода", callback_data="fam:menu")])
    rows.append([InlineKeyboardButton(text="⬅️ На главную", callback_data="main:home")])

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# 1.2. РАЗДЕЛ «КВАРТИРЫ» (/apartments — в пределах одной семьи)
# ---------------------------------------------------------------------------
def render_apartments_menu(
    apt: Apartment | None,
    all_apts: list[Apartment],
    tenants: list[User] | None = None,
    family_name: str | None = None,
    is_admin: bool = False,
) -> tuple[str, InlineKeyboardMarkup]:
    lines = [
        "🏢 <b>КВАРТИРЫ</b>",
        "────────────────────────",
    ]
    if family_name:
        lines.append(f"👨‍👩‍👧‍👦 Семья: <b>{family_name}</b>")

    if apt:
        lines.append(f"Выбрана квартира: <b>№{apt.number}</b>")
        if apt.has_tenant:
            t_names = ", ".join(t.name or str(t.telegram_id) for t in (tenants or []))
            t_suffix = f" ({t_names})" if t_names else ""
            lines.append(f"👤 Наниматель: <b>✅ Проживает{t_suffix}</b>")
            if apt.agreement_no or apt.agreement_date_start or apt.agreement_date_finish:
                lines.append(
                    f"📄 Договор: <b>№{apt.agreement_no or 'б/н'}</b> "
                    f"({fmt_date(apt.agreement_date_start)} – {fmt_date(apt.agreement_date_finish)})"
                )
        else:
            lines.append("👤 Наниматель: <b>🏠 Не проживает (свободна)</b>")

        lines.append(
            f"📅 Дни подачи: Вода — <b>{apt.water_day}-е число</b> | Свет — <b>{apt.elec_day}-е число</b>"
        )
        lines.append(
            f"💳 Тарифы: ХВС <code>{fmt_dec(apt.tariff_hvs, 2)}</code> | ГВС <code>{fmt_dec(apt.tariff_gvs, 2)}</code> | "
            f"Т1 <code>{fmt_dec(apt.tariff_t1, 2)}</code> | Т2 <code>{fmt_dec(apt.tariff_t2, 2)}</code> | Т3 <code>{fmt_dec(apt.tariff_t3, 2)}</code> ₽"
        )
    else:
        lines.append("В этой семье квартиры ещё не созданы.")

    rows: list[list[InlineKeyboardButton]] = []
    if all_apts:
        lines.append("\nСписок квартир семьи (нажмите для выбора):")
        for a in all_apts:
            prefix = "✅ " if (apt and a.id == apt.id) else "🏢 "
            t_badge = "👤 проживает наниматель" if a.has_tenant else "🏠 нет нанимателя"
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"{prefix}Кв. №{a.number} ({t_badge})",
                        callback_data=f"apt:sel:{a.id}",
                    )
                ]
            )

    if apt:
        rows.append(
            [InlineKeyboardButton(text="⚙️ Тарифы и настройки", callback_data=f"cfg:open:{apt.id}")]
        )
    rows.append([InlineKeyboardButton(text="➕ Новая квартира", callback_data="apt:create")])
    if is_admin:
        rows.append([InlineKeyboardButton(text="👨‍👩‍👧‍👦 Сменить / Создать семью", callback_data="fam:menu")])
    rows.append([InlineKeyboardButton(text="⬅️ На главную", callback_data="main:home")])

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def render_owner_info() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "ℹ️ <b>ОБЩАЯ ИНФОРМАЦИЯ</b>\n"
        "────────────────────────\n"
        "Бот для внесения показаний потреблённых энергоресурсов по одной или нескольким квартирам.\n\n"
        "Если в квартире проживает наниматель, то добавьте нового пользователя, указав его имя, телефон и Telegram ID, "
        "который можно узнать через бот @userinfobot. Наниматель получит приглашение в бот. "
        "После того, как он его примет у него будет доступ к боту, чтобы мог самостоятельно вносить показания.\n\n"
        "Так же укажите номер договора и срок его действия, чтобы после окончания срока найма квартиры "
        "наниматель не мог иметь доступ к боту.\n\n"
        "Укажите начальные показания для водомеров и электрического счётчика.\n"
        "Укажите тариф для ХВС, ГВС и трёх тарифов электроэнергии.\n\n"
        "Вы можете:\n"
        "- добавлять и удалять совладельцев квартир;\n"
        "- добавлять и удалять нанимателей.\n\n"
        "Владелец и совладельцы квартиры имеют полный доступ к данным в пределах своей семьи, в том числе, могут вносить "
        "корректировки в тарифы и показания, выгружать базу данных, видеть статистику показаний, "
        "просматривать историю изменения тарифов и т.д. Наниматели могут только вносить показания "
        "и видеть количество и стоимость потреблённых ресурсов."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад на главное табло", callback_data="main:home")]
        ]
    )
    return text, kb


def render_tenant_info(apt: Apartment) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"Привет! Это бот для внесения показаний потреблённых энергоресурсов по квартире <b>№{apt.number}</b>, "
        f"в которой Вы проживаете по договору №<b>{apt.agreement_no or 'б/н'}</b> "
        f"от <b>{fmt_date(apt.agreement_date_start)}</b> до <b>{fmt_date(apt.agreement_date_finish)}</b>.\n"
        "Пожалуйста, своевременно вносите показания.\n"
        "<i>Внесение показаний не является оплатой за коммунальные услуги, а имеет информационный характер и передаёт данные наймодателю.</i>\n"
        "Спасибо и хорошего дня!\n"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад на главное табло", callback_data="main:home")]
        ]
    )
    return text, kb


# ---------------------------------------------------------------------------
# 2. ЭКРАН НАСТРОЕК КВАРТИРЫ
# ---------------------------------------------------------------------------
FIELD_LABELS_CFG = {
    "number": ("Номер квартиры", "например: 75"),
    "water_day": ("День подачи ХВС/ГВС (1..31)", "например: 20"),
    "elec_day": ("День подачи электросчётчика (1..31)", "например: 25"),
    "tariff_hvs": ("Тариф ХВС (руб/м³)", "например: 42.50"),
    "tariff_gvs": ("Тариф ГВС (руб/м³)", "например: 198.40"),
    "tariff_t1": ("Тариф Электро Т1 (руб/кВт·ч)", "например: 5.65"),
    "tariff_t2": ("Тариф Электро Т2 (руб/кВт·ч)", "например: 2.85"),
    "tariff_t3": ("Тариф Электро Т3 (руб/кВт·ч)", "например: 4.70"),
    "init_hvs": ("Начальное показание ХВС (м³)", "целое число, например: 120"),
    "init_gvs": ("Начальное показание ГВС (м³)", "целое число, например: 85"),
    "init_t1": ("Начальное показание Т1 (кВт·ч)", "целое число, например: 3450"),
    "init_t2": ("Начальное показание Т2 (кВт·ч)", "целое число, например: 1290"),
    "init_t3": ("Начальное показание Т3 (кВт·ч)", "целое число, например: 890"),
}

WIZARD_CFG_ORDER = [
    "water_day",
    "elec_day",
    "tariff_hvs",
    "tariff_gvs",
    "tariff_t1",
    "tariff_t2",
    "tariff_t3",
    "init_hvs",
    "init_gvs",
    "init_t1",
    "init_t2",
    "init_t3",
]


def render_apartment_settings(
    apt: Apartment,
    active_field: str | None = None,
    wizard_step: int | None = None,
    error_msg: str | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    lines = [
        f"⚙️ <b>НАСТРОЙКИ КВАРТИРЫ №{apt.number}</b>",
        "────────────────────────",
        "📅 <b>Сроки подачи показаний (15:00 CET+5):</b>",
        f"  • Водомеры (ХВС/ГВС): <b>{apt.water_day}-е число месяца</b>",
        f"  • Электросчётчик: <b>{apt.elec_day}-е число месяца</b>",
        "",
        "💳 <b>Действующие тарифы:</b>",
        f"  • ХВС: <code>{fmt_dec(apt.tariff_hvs, 2)} ₽/м³</code> | ГВС: <code>{fmt_dec(apt.tariff_gvs, 2)} ₽/м³</code>",
        f"  • Т1: <code>{fmt_dec(apt.tariff_t1, 2)} ₽</code> | Т2: <code>{fmt_dec(apt.tariff_t2, 2)} ₽</code> | Т3: <code>{fmt_dec(apt.tariff_t3, 2)} ₽/кВт·ч</code>",
        "",
        "🔢 <b>Начальные (базовые) показания:</b>",
        f"  • ХВС: <code>{fmt_dec(apt.init_hvs, 0)} м³</code> | ГВС: <code>{fmt_dec(apt.init_gvs, 0)} м³</code>",
        f"  • Т1: <code>{fmt_dec(apt.init_t1, 0)}</code> | Т2: <code>{fmt_dec(apt.init_t2, 0)}</code> | Т3: <code>{fmt_dec(apt.init_t3, 0)} кВт·ч</code>",
    ]

    if error_msg:
        lines.append(f"\n⚠️ <b>Ошибка:</b> {error_msg}")

    if active_field and active_field in FIELD_LABELS_CFG:
        label, example = FIELD_LABELS_CFG[active_field]
        step_str = f" [Шаг {wizard_step}/{len(WIZARD_CFG_ORDER)}]" if wizard_step else ""
        lines.append(
            "\n────────────────────────\n"
            f"👉 <b>Сейчас вводится{step_str}: {label}</b>.\n"
            f"Отправьте значение в чат ({example}):"
        )

    rows = [
        [
            InlineKeyboardButton(text=f"📅 День ХВС/ГВС: {apt.water_day}", callback_data=f"cfg:edit:{apt.id}:water_day"),
            InlineKeyboardButton(text=f"📅 День Электро: {apt.elec_day}", callback_data=f"cfg:edit:{apt.id}:elec_day"),
        ],
        [
            InlineKeyboardButton(text=f"💧 ХВС: {fmt_dec(apt.tariff_hvs, 2)} ₽", callback_data=f"cfg:edit:{apt.id}:tariff_hvs"),
            InlineKeyboardButton(text=f"🔥 ГВС: {fmt_dec(apt.tariff_gvs, 2)} ₽", callback_data=f"cfg:edit:{apt.id}:tariff_gvs"),
        ],
        [
            InlineKeyboardButton(text=f"⚡ Т1: {fmt_dec(apt.tariff_t1, 2)} ₽", callback_data=f"cfg:edit:{apt.id}:tariff_t1"),
            InlineKeyboardButton(text=f"⚡ Т2: {fmt_dec(apt.tariff_t2, 2)} ₽", callback_data=f"cfg:edit:{apt.id}:tariff_t2"),
            InlineKeyboardButton(text=f"⚡ Т3: {fmt_dec(apt.tariff_t3, 2)} ₽", callback_data=f"cfg:edit:{apt.id}:tariff_t3"),
        ],
        [
            InlineKeyboardButton(text=f"🔢 Нач. ХВС: {fmt_dec(apt.init_hvs, 0)}", callback_data=f"cfg:edit:{apt.id}:init_hvs"),
            InlineKeyboardButton(text=f"🔢 Нач. ГВС: {fmt_dec(apt.init_gvs, 0)}", callback_data=f"cfg:edit:{apt.id}:init_gvs"),
        ],
        [
            InlineKeyboardButton(text=f"🔢 Нач. Т1: {fmt_dec(apt.init_t1, 0)}", callback_data=f"cfg:edit:{apt.id}:init_t1"),
            InlineKeyboardButton(text=f"🔢 Нач. Т2: {fmt_dec(apt.init_t2, 0)}", callback_data=f"cfg:edit:{apt.id}:init_t2"),
            InlineKeyboardButton(text=f"🔢 Нач. Т3: {fmt_dec(apt.init_t3, 0)}", callback_data=f"cfg:edit:{apt.id}:init_t3"),
        ],
        [
            InlineKeyboardButton(text="🔄 Заполнить всё по порядку", callback_data=f"cfg:wiz:{apt.id}"),
            InlineKeyboardButton(text="🏷 Номер кв.", callback_data=f"cfg:edit:{apt.id}:number"),
        ],
    ]

    bottom_row = []
    if active_field:
        bottom_row.append(InlineKeyboardButton(text="◀ Отмена ввода", callback_data=f"cfg:open:{apt.id}"))
    bottom_row.append(InlineKeyboardButton(text="⬅️ Назад в Квартиры", callback_data="apt:menu"))
    bottom_row.append(InlineKeyboardButton(text="🏠 На главную", callback_data="main:home"))
    rows.append(bottom_row)

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# 3. ОДНОЭКРАННЫЙ ПУЛЬТ ПОДАЧИ ПОКАЗАНИЙ (Вода / Электричество)
# ---------------------------------------------------------------------------
READING_LABELS = {
    "hvs": ("Холодная вода ХВС (м³)", "м³", 0),
    "gvs": ("Горячая вода ГВС (м³)", "м³", 0),
    "t1": ("Электроэнергия Т1 (кВт·ч)", "кВт·ч", 0),
    "t2": ("Электроэнергия Т2 (кВт·ч)", "кВт·ч", 0),
    "t3": ("Электроэнергия Т3 (кВт·ч)", "кВт·ч", 0),
}


def render_reading_panel(
    apt: Apartment,
    period: str,
    prev: dict[str, Decimal],
    draft: dict[str, Decimal],
    calc: CalculationResult,
    mode: str = "water",
    active_field: str | None = None,
    step_info: str | None = None,
    error_msg: str | None = None,
    awaiting_confirm: bool = False,
) -> tuple[str, InlineKeyboardMarkup]:
    if mode == "water":
        lines = [
            f"💧 <b>ПОДАЧА ПОКАЗАНИЙ ВОДОМЕРОВ — Кв. №{apt.number} (за {period})</b>",
            "────────────────────────",
            f"  • ХВС: <code>{fmt_dec(prev['hvs'], 0)}</code> → <b>{fmt_dec(draft['hvs'], 0)} м³</b> "
            f"(расход: <code>{fmt_dec(calc.diff_hvs, 0)} м³</code> | <b>{fmt_dec(calc.cost_hvs, 0)} ₽</b>)",
            f"  • ГВС: <code>{fmt_dec(prev['gvs'], 0)}</code> → <b>{fmt_dec(draft['gvs'], 0)} м³</b> "
            f"(расход: <code>{fmt_dec(calc.diff_gvs, 0)} м³</code> | <b>{fmt_dec(calc.cost_gvs, 0)} ₽</b>)",
            "────────────────────────",
            f"🚰 Общий расход воды: <b>{fmt_dec(calc.diff_water_total, 0)} м³</b>",
            f"💰 <b>ИТОГО ПО ВОДЕ: {fmt_dec(calc.cost_water_total, 0)} ₽</b>",
        ]
        relevant_decreased = [f for f in calc.decreased_fields if f in ("ХВС", "ГВС")]
    else:
        lines = [
            f"⚡ <b>ПОДАЧА ПОКАЗАНИЙ ЭЛЕКТРОЭНЕРГИИ — Кв. №{apt.number} (за {period})</b>",
            "────────────────────────",
            f"  • Т1: <code>{fmt_dec(prev['t1'], 0)}</code> → <b>{fmt_dec(draft['t1'], 0)} кВт·ч</b> "
            f"(расход: <code>{fmt_dec(calc.diff_t1, 0)} кВт·ч</code> | <b>{fmt_dec(calc.cost_t1, 0)} ₽</b>)",
            f"  • Т2: <code>{fmt_dec(prev['t2'], 0)}</code> → <b>{fmt_dec(draft['t2'], 0)} кВт·ч</b> "
            f"(расход: <code>{fmt_dec(calc.diff_t2, 0)} кВт·ч</code> | <b>{fmt_dec(calc.cost_t2, 0)} ₽</b>)",
            f"  • Т3: <code>{fmt_dec(prev['t3'], 0)}</code> → <b>{fmt_dec(draft['t3'], 0)} кВт·ч</b> "
            f"(расход: <code>{fmt_dec(calc.diff_t3, 0)} кВт·ч</code> | <b>{fmt_dec(calc.cost_t3, 0)} ₽</b>)",
            "────────────────────────",
            f"🔌 Общий расход электроэнергии: <b>{fmt_dec(calc.diff_elec_total, 0)} кВт·ч</b>",
            f"💰 <b>ИТОГО ПО ЭЛЕКТРОЭНЕРГИИ: {fmt_dec(calc.cost_elec_total, 0)} ₽</b>",
        ]
        relevant_decreased = [f for f in calc.decreased_fields if f in ("Т1", "Т2", "Т3")]

    if relevant_decreased:
        joined = ", ".join(relevant_decreased)
        lines.append(
            f"\n⚠️ <b>Внимание:</b> по счётчикам (<b>{joined}</b>) введённое значение меньше предыдущего! "
            "Если произошла замена или переполнение счётчика, подтвердите сохранение специальной кнопкой ниже."
        )

    if error_msg:
        lines.append(f"\n⚠️ <b>Ошибка:</b> {error_msg}")

    if active_field and active_field in READING_LABELS:
        label, unit, _ = READING_LABELS[active_field]
        step_prefix = f" [{step_info}]" if step_info else ""
        lines.append(
            "\n────────────────────────\n"
            f"👉 <b>Сейчас вводится{step_prefix}: {label}</b>\n"
            f"Предыдущее показание: <code>{fmt_dec(prev[active_field], 0)} {unit}</code>.\n"
            "Введите текущее показание целым числом:"
        )
    elif awaiting_confirm:
        lines.append(
            "\n────────────────────────\n"
            "✅ <b>Проверьте предварительный расчёт выше и подтвердите сохранение показаний в базу данных:</b>"
        )

    rows: list[list[InlineKeyboardButton]] = []
    if awaiting_confirm:
        if relevant_decreased:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="⚠️ Подтвердить (замена/сброс счётчика)",
                        callback_data=f"read:commit:{apt.id}:force",
                    )
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="✅ Подтвердить и сохранить в БД",
                        callback_data=f"read:commit:{apt.id}:normal",
                    )
                ]
            )
        rows.append([InlineKeyboardButton(text="✏️ Вернуться к редактированию", callback_data=f"read:back:{apt.id}")])
    else:
        if mode == "water":
            rows.extend(
                [
                    [
                        InlineKeyboardButton(
                            text=f"💧 ХВС: {fmt_dec(draft['hvs'], 0)}", callback_data=f"read:field:{apt.id}:hvs"
                        ),
                        InlineKeyboardButton(
                            text=f"🔥 ГВС: {fmt_dec(draft['gvs'], 0)}", callback_data=f"read:field:{apt.id}:gvs"
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            text="🔄 Ввести ХВС и ГВС по порядку", callback_data=f"read:wiz:{apt.id}:water"
                        ),
                    ],
                    [
                        InlineKeyboardButton(text="💾 Сохранить показания", callback_data=f"read:preview:{apt.id}"),
                    ],
                ]
            )
        else:
            rows.extend(
                [
                    [
                        InlineKeyboardButton(
                            text=f"⚡ Т1: {fmt_dec(draft['t1'], 0)}", callback_data=f"read:field:{apt.id}:t1"
                        ),
                        InlineKeyboardButton(
                            text=f"⚡ Т2: {fmt_dec(draft['t2'], 0)}", callback_data=f"read:field:{apt.id}:t2"
                        ),
                        InlineKeyboardButton(
                            text=f"⚡ Т3: {fmt_dec(draft['t3'], 0)}", callback_data=f"read:field:{apt.id}:t3"
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            text="🔄 Ввести Т1, Т2, Т3 по порядку", callback_data=f"read:wiz:{apt.id}:elec"
                        ),
                    ],
                    [
                        InlineKeyboardButton(text="💾 Сохранить показания", callback_data=f"read:preview:{apt.id}"),
                    ],
                ]
            )

    bottom_row = []
    if active_field:
        bottom_row.append(InlineKeyboardButton(text="◀ Отмена ввода", callback_data=f"read:back:{apt.id}"))
    bottom_row.append(InlineKeyboardButton(text="⬅️ На главную", callback_data="main:home"))
    rows.append(bottom_row)

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# 4. ИСТОРИЯ И СРАВНЕНИЕ МЕСЯЦЕВ
# ---------------------------------------------------------------------------
def render_history_panel(
    apt: Apartment,
    readings: list[MeterReading],
    idx: int = 0,
    cmp_idx: int | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    if not readings:
        text = f"📅 <b>ИСТОРИЯ ПОКАЗАНИЙ — Кв. №{apt.number}</b>\n\nЗаписей за расчётные периоды пока нет."
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ На главную", callback_data="main:home")]]
        )
        return text, kb

    idx = max(0, min(idx, len(readings) - 1))
    r = readings[idx]
    water_m3 = Decimal(str(r.diff_hvs)) + Decimal(str(r.diff_gvs))
    elec_kwh = Decimal(str(r.diff_t1)) + Decimal(str(r.diff_t2)) + Decimal(str(r.diff_t3))

    lines = [
        f"📅 <b>ИСТОРИЯ ПОТРЕБЛЕНИЯ — Кв. №{apt.number}</b>",
        f"Выбранный период: <b>{r.period}</b> (внесено {r.reading_date.strftime('%d.%m.%Y')}, ID автора: <code>{r.submitted_by}</code>)",
        "────────────────────────",
        "💧 <b>ВОДА (начало → конец | расход | сумма):</b>",
        f"  • ХВС: <code>{fmt_dec(r.prev_hvs, 0)} → {fmt_dec(r.hvs, 0)}</code> | <b>{fmt_dec(r.diff_hvs, 0)} м³</b> | <b>{fmt_dec(r.cost_hvs, 0)} ₽</b>",
        f"  • ГВС: <code>{fmt_dec(r.prev_gvs, 0)} → {fmt_dec(r.gvs, 0)}</code> | <b>{fmt_dec(r.diff_gvs, 0)} м³</b> | <b>{fmt_dec(r.cost_gvs, 0)} ₽</b>",
        f"  • Всего вода: <b>{fmt_dec(water_m3, 0)} м³</b> на <b>{fmt_dec(r.cost_water_total, 0)} ₽</b>",
        "",
        "⚡ <b>ЭЛЕКТРИЧЕСТВО (начало → конец | расход | сумма):</b>",
        f"  • Т1: <code>{fmt_dec(r.prev_t1, 0)} → {fmt_dec(r.t1, 0)}</code> | <b>{fmt_dec(r.diff_t1, 0)} кВт·ч</b> | <b>{fmt_dec(r.cost_t1, 0)} ₽</b>",
        f"  • Т2: <code>{fmt_dec(r.prev_t2, 0)} → {fmt_dec(r.t2, 0)}</code> | <b>{fmt_dec(r.diff_t2, 0)} кВт·ч</b> | <b>{fmt_dec(r.cost_t2, 0)} ₽</b>",
        f"  • Т3: <code>{fmt_dec(r.prev_t3, 0)} → {fmt_dec(r.t3, 0)}</code> | <b>{fmt_dec(r.diff_t3, 0)} кВт·ч</b> | <b>{fmt_dec(r.cost_t3, 0)} ₽</b>",
        f"  • Всего свет: <b>{fmt_dec(elec_kwh, 0)} кВт·ч</b> на <b>{fmt_dec(r.cost_elec_total, 0)} ₽</b>",
        "────────────────────────",
        f"💰 <b>ИТОГО ЗА {r.period}: {fmt_dec(r.cost_total, 0)} ₽</b>",
    ]

    if cmp_idx is not None and 0 <= cmp_idx < len(readings) and cmp_idx != idx:
        c = readings[cmp_idx]
        c_water = Decimal(str(c.diff_hvs)) + Decimal(str(c.diff_gvs))
        c_elec = Decimal(str(c.diff_t1)) + Decimal(str(c.diff_t2)) + Decimal(str(c.diff_t3))
        d_water = water_m3 - c_water
        d_elec = elec_kwh - c_elec
        d_cost = Decimal(str(r.cost_total)) - Decimal(str(c.cost_total))
        sign_w = "+" if d_water >= 0 else ""
        sign_e = "+" if d_elec >= 0 else ""
        sign_c = "+" if d_cost >= 0 else ""
        lines.append(
            "\n📊 <b>СРАВНЕНИЕ С ПЕРИОДОМ " + c.period + ":</b>\n"
            f"  • Вода: <code>{sign_w}{fmt_dec(d_water, 0)} м³</code> ({fmt_dec(c.cost_water_total, 0)} ₽ → {fmt_dec(r.cost_water_total, 0)} ₽)\n"
            f"  • Свет: <code>{sign_e}{fmt_dec(d_elec, 0)} кВт·ч</code> ({fmt_dec(c.cost_elec_total, 0)} ₽ → {fmt_dec(r.cost_elec_total, 0)} ₽)\n"
            f"  • Разница по сумме: <b>{sign_c}{fmt_dec(d_cost, 0)} ₽</b>"
        )

    rows: list[list[InlineKeyboardButton]] = []
    nav_row = []
    if idx + 1 < len(readings):
        nav_row.append(
            InlineKeyboardButton(text=f"⬅️ {readings[idx + 1].period}", callback_data=f"hist:view:{apt.id}:{idx + 1}")
        )
    if idx - 1 >= 0:
        nav_row.append(
            InlineKeyboardButton(text=f"{readings[idx - 1].period} ➡️", callback_data=f"hist:view:{apt.id}:{idx - 1}")
        )
    if nav_row:
        rows.append(nav_row)

    if len(readings) > 1:
        cmp_btns = []
        for i, item in enumerate(readings[:6]):
            if i == idx:
                continue
            mark = "🔍 " if i == cmp_idx else "⚖️ "
            cmp_btns.append(
                InlineKeyboardButton(text=f"{mark}{item.period}", callback_data=f"hist:cmp:{apt.id}:{idx}:{i}")
            )
        for i in range(0, len(cmp_btns), 3):
            rows.append(cmp_btns[i : i + 3])

    rows.append([InlineKeyboardButton(text="⬅️ На главную", callback_data="main:home")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# 5. УПРАВЛЕНИЕ ДОГОВОРОМ, НАНИМАТЕЛЯМИ И ПОЛНАЯ ИНФОРМАЦИЯ ПО КВАРТИРЕ (/users)
# ---------------------------------------------------------------------------
USR_FIELD_LABELS = {
    "agreement_no": ("Номер договора найма", "например: 15/2026"),
    "agreement_date_start": ("Дата начала договора (ДД.ММ.ГГГГ)", "например: 01.09.2026"),
    "agreement_date_finish": ("Дата окончания договора (ДД.ММ.ГГГГ)", "например: 31.08.2027"),
    "tenant_id": ("Шаг 1 из 3 — Telegram ID нанимателя", "только цифры, например: 987654321"),
    "tenant_phone": ("Шаг 2 из 3 — Номер телефона нанимателя", "например: +79001234567"),
    "tenant_name": ("Шаг 3 из 3 — Имя нанимателя", "например: Иван Петров"),
    "landlord_id": ("Шаг 1 из 2 — Telegram ID нового совладельца", "только цифры, например: 555666777"),
    "landlord_name": ("Шаг 2 из 2 — Имя совладельца", "например: Елена"),
}


def render_users_panel(
    apt: Apartment,
    tenants: list[User],
    landlords: list[User],
    bot_username: str,
    family_name: str | None = None,
    rented_apts: list[Apartment] | None = None,
    latest_reading: MeterReading | None = None,
    active_field: str | None = None,
    error_msg: str | None = None,
    info_msg: str | None = None,
    draft_info: str | None = None,
    awaiting_no_agr_confirm: bool = False,
) -> tuple[str, InlineKeyboardMarkup]:
    status_str = "✅ Проживает наниматель" if apt.has_tenant else "🏠 Нанимателя нет (свободна)"
    lines = [
        f"👥 <b>ДОГОВОР, ЖИЛЬЦЫ И ИНФОРМАЦИЯ — Кв. №{apt.number}</b>",
    ]
    if family_name:
        lines.append(f"👨‍👩‍👧‍👦 Семья: <b>{family_name}</b>")
    lines.extend(
        [
            f"Статус квартиры: <b>{status_str}</b>",
            "────────────────────────",
            "📄 <b>Данные договора аренды:</b>",
            f"  • Номер договора: <b>{apt.agreement_no or 'не указан'}</b>",
            f"  • Дата начала: <b>{fmt_date(apt.agreement_date_start)}</b>",
            f"  • Срок окончания: <b>{fmt_date(apt.agreement_date_finish)}</b>",
            "",
            "👤 <b>Наниматели этой квартиры:</b>",
        ]
    )
    if tenants:
        for t in tenants:
            lines.append(
                f"  • 👤 <b>{t.name or 'Без имени'}</b> | Тел.: <code>{t.phone or '—'}</code> | ID: <code>{t.telegram_id}</code>"
            )
    else:
        lines.append("  • Наниматели не добавлены")

    lines.append("\n🔑 <b>Владельцы / Совладельцы этой семьи:</b>")
    if landlords:
        for l_user in landlords:
            lines.append(f"  • <b>{l_user.name or 'Владелец'}</b> | ID: <code>{l_user.telegram_id}</code>")
    else:
        lines.append("  • Совладельцы не указаны")

    # Полная существующая информация по параметрам, тарифам и показаниям квартиры
    lines.extend(
        [
            "────────────────────────",
            "📅 <b>Сроки подачи и действующие тарифы:</b>",
            f"  • Дни подачи: Вода — <b>{apt.water_day}-е число</b> | Свет — <b>{apt.elec_day}-е число</b>",
            f"  • Тарифы Вода: ХВС <code>{fmt_dec(apt.tariff_hvs, 2)} ₽/м³</code> | ГВС <code>{fmt_dec(apt.tariff_gvs, 2)} ₽/м³</code>",
            f"  • Тарифы Свет: Т1 <code>{fmt_dec(apt.tariff_t1, 2)}</code> | Т2 <code>{fmt_dec(apt.tariff_t2, 2)}</code> | Т3 <code>{fmt_dec(apt.tariff_t3, 2)} ₽/кВт·ч</code>",
            f"  • Начальные показания: ХВС <code>{fmt_dec(apt.init_hvs, 0)}</code>, ГВС <code>{fmt_dec(apt.init_gvs, 0)}</code>, "
            f"Т1 <code>{fmt_dec(apt.init_t1, 0)}</code>, Т2 <code>{fmt_dec(apt.init_t2, 0)}</code>, Т3 <code>{fmt_dec(apt.init_t3, 0)}</code>",
        ]
    )

    if latest_reading:
        water_sum_m3 = Decimal(str(latest_reading.diff_hvs)) + Decimal(str(latest_reading.diff_gvs))
        elec_sum_kwh = (
            Decimal(str(latest_reading.diff_t1))
            + Decimal(str(latest_reading.diff_t2))
            + Decimal(str(latest_reading.diff_t3))
        )
        lines.extend(
            [
                "",
                f"📊 <b>Последние показания и расход (период {latest_reading.period}):</b>",
                f"  • Вода: ХВС <code>{fmt_dec(latest_reading.prev_hvs, 0)} → {fmt_dec(latest_reading.hvs, 0)}</code> "
                f"(расход <b>{fmt_dec(latest_reading.diff_hvs, 0)} м³</b> = <b>{fmt_dec(latest_reading.cost_hvs, 0)} ₽</b>) | "
                f"ГВС <code>{fmt_dec(latest_reading.prev_gvs, 0)} → {fmt_dec(latest_reading.gvs, 0)}</code> "
                f"(расход <b>{fmt_dec(latest_reading.diff_gvs, 0)} м³</b> = <b>{fmt_dec(latest_reading.cost_gvs, 0)} ₽</b>)",
                f"  • Свет: Т1 <code>{fmt_dec(latest_reading.prev_t1, 0)} → {fmt_dec(latest_reading.t1, 0)}</code> "
                f"(<b>{fmt_dec(latest_reading.diff_t1, 0)} кВт·ч</b>) | "
                f"Т2 <code>{fmt_dec(latest_reading.prev_t2, 0)} → {fmt_dec(latest_reading.t2, 0)}</code> "
                f"(<b>{fmt_dec(latest_reading.diff_t2, 0)} кВт·ч</b>) | "
                f"Т3 <code>{fmt_dec(latest_reading.prev_t3, 0)} → {fmt_dec(latest_reading.t3, 0)}</code> "
                f"(<b>{fmt_dec(latest_reading.diff_t3, 0)} кВт·ч</b>)",
                f"  • Итого Вода ({fmt_dec(water_sum_m3, 0)} м³): <b>{fmt_dec(latest_reading.cost_water_total, 0)} ₽</b> | "
                f"Итого Свет ({fmt_dec(elec_sum_kwh, 0)} кВт·ч): <b>{fmt_dec(latest_reading.cost_elec_total, 0)} ₽</b>",
                f"  • 💰 <b>Общая сумма к оплате: {fmt_dec(latest_reading.cost_total, 0)} ₽</b>",
            ]
        )

    if bot_username:
        lines.append(
            f"\n🔗 <b>Ссылка-приглашение для нанимателя:</b>\n"
            f"<code>https://t.me/{bot_username}?start=apt_{apt.id}</code>"
        )

    if draft_info:
        lines.append(f"\n────────────────────────\n📝 <b>Введённые данные:</b>\n{draft_info}")

    if info_msg:
        lines.append(f"\nℹ️ {info_msg}")
    if error_msg:
        lines.append(f"\n⚠️ <b>Ошибка:</b> {error_msg}")

    if awaiting_no_agr_confirm:
        missing = []
        if not apt.agreement_no:
            missing.append("номер договора")
        if not apt.agreement_date_start:
            missing.append("дата начала")
        if not apt.agreement_date_finish:
            missing.append("дата окончания")
        missing_str = ", ".join(missing)
        lines.append(
            "\n────────────────────────\n"
            f"⚠️ <b>Внимание! Не заполнены данные по договору аренды ({missing_str}).</b>\n"
            "Выберите действие:"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Отменить отправку приглашения",
                        callback_data=f"usr:no_agr_cancel:{apt.id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Отправить приглашение",
                        callback_data=f"usr:no_agr_confirm:{apt.id}",
                    )
                ],
            ]
        )
        return "\n".join(lines), kb

    if active_field and active_field in USR_FIELD_LABELS:
        label, ex = USR_FIELD_LABELS[active_field]
        lines.append(
            "\n────────────────────────\n"
            f"👉 <b>Сейчас вводится: {label}</b>\n"
            f"Отправьте значение в чат ({ex}):"
        )

    rows: list[list[InlineKeyboardButton]] = []

    # Квартиры внутри семьи, в которых проживает арендатор
    if rented_apts:
        lines.append("\n🏘 <b>Квартиры семьи с проживающим арендатором (выберите для просмотра):</b>")
        for r_apt in rented_apts:
            prefix = "✅ " if r_apt.id == apt.id else "👤 "
            agr_label = f"дог. №{r_apt.agreement_no}" if r_apt.agreement_no else "без № дог."
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"{prefix}Кв. №{r_apt.number} (проживает арендатор, {agr_label})",
                        callback_data=f"usr:sel:{r_apt.id}",
                    )
                ]
            )
    else:
        lines.append("\n🏘 <i>В текущей семье пока нет квартир со статусом «Проживает наниматель».</i>")

    toggle_text = (
        "🔄 Сменить статус: Есть наниматель -> Нет"
        if apt.has_tenant
        else "🔄 Сменить статус: Нет -> Есть наниматель"
    )
    rows.extend(
        [
            [InlineKeyboardButton(text=toggle_text, callback_data=f"usr:toggle:{apt.id}")],
            [
                InlineKeyboardButton(
                    text="🔄 Новый договор (по порядку)",
                    callback_data=f"usr:edit:{apt.id}:wiz_agreement",
                ),
                InlineKeyboardButton(
                    text="🗑 Удалить договор",
                    callback_data=f"usr:del_agr:{apt.id}",
                ),
            ],
            [
                InlineKeyboardButton(text="✏️ № договора", callback_data=f"usr:edit:{apt.id}:agreement_no"),
                InlineKeyboardButton(text="✏️ Дата договора", callback_data=f"usr:edit:{apt.id}:agreement_date_start"),
                InlineKeyboardButton(text="✏️ Дата окончания", callback_data=f"usr:edit:{apt.id}:agreement_date_finish"),
            ],
            [
                InlineKeyboardButton(text="➕ Добавить нанимателя", callback_data=f"usr:edit:{apt.id}:add_tenant"),
                InlineKeyboardButton(text="➕ Добавить совладельца", callback_data=f"usr:edit:{apt.id}:add_landlord"),
            ],
        ]
    )

    for t in tenants:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🗑 Удалить нанимателя: {t.name or t.telegram_id}",
                    callback_data=f"usr:del:{apt.id}:{t.telegram_id}",
                )
            ]
        )

    for l_user in landlords:
        if l_user.role == "landlord":
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"🗑 Удалить совладельца: {l_user.name or l_user.telegram_id}",
                        callback_data=f"usr:del_landlord:{apt.id}:{l_user.telegram_id}",
                    )
                ]
            )

    bottom_row = []
    if active_field:
        bottom_row.append(InlineKeyboardButton(text="◀ Отмена ввода", callback_data=f"usr:open:{apt.id}"))
    bottom_row.append(InlineKeyboardButton(text="⬅️ На главную", callback_data="main:home"))
    rows.append(bottom_row)

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# 6. РАЗДЕЛ Г. АДМИНИСТРИРОВАНИЕ
# ---------------------------------------------------------------------------
def render_admin_panel(
    apt: Apartment,
    readings: list[MeterReading],
    tariffs: list[TariffHistory],
    logs: list[AuditLog],
    users: list[User],
    subview: str = "overview",
) -> tuple[str, InlineKeyboardMarkup]:
    lines = [f"🛡 <b>АДМИНИСТРИРОВАНИЕ — Кв. №{apt.number}</b>", "────────────────────────"]

    if subview == "tariffs":
        lines.append("📜 <b>История изменения тарифов (последние 10):</b>")
        if not tariffs:
            lines.append("Записей об изменении тарифов пока нет.")
        for th in tariffs[:10]:
            dt_str = th.effective_from.strftime("%d.%m.%Y %H:%M")
            lines.append(
                f"• <b>{dt_str}</b> (ID <code>{th.changed_by}</code>):\n"
                f"  ХВС={fmt_dec(th.tariff_hvs, 2)} | ГВС={fmt_dec(th.tariff_gvs, 2)} | "
                f"Т1={fmt_dec(th.tariff_t1, 2)} | Т2={fmt_dec(th.tariff_t2, 2)} | Т3={fmt_dec(th.tariff_t3, 2)}"
            )
    elif subview == "users":
        lines.append("👥 <b>Список всех пользователей системы:</b>")
        for u in users:
            lines.append(
                f"• ID <code>{u.telegram_id}</code> (<b>{u.name or '—'}</b>) — роль: <b>{u.role}</b> | семья: {u.family_id or '—'} | кв. ID: {u.apartment_id or 'все'}"
            )
    elif subview == "logs":
        lines.append("📋 <b>Журнал административных изменений (последние 12):</b>")
        if not logs:
            lines.append("Журнал пуст.")
        for lg in logs[:12]:
            dt_str = lg.timestamp.strftime("%d.%m %H:%M")
            lines.append(f"• <code>{dt_str}</code> [ID {lg.user_id}] <b>{lg.action}</b>: {lg.details}")
    else:
        lines.append(
            "Выберите подраздел администрирования:\n"
            "• <b>Семьи:</b> создание новых семей и назначение владельцев.\n"
            "• <b>Исправление показаний:</b> выберите период ниже для корректировки ошибочно внесённых данных.\n"
            "• <b>История тарифов и журнал аудита.</b>"
        )
        if readings:
            lines.append("\n📅 <b>Последние расчётные периоды (нажмите для исправления):</b>")
            for r in readings[:5]:
                lines.append(
                    f"  • <b>{r.period}</b> — Итого: <b>{fmt_dec(r.cost_total, 0)} ₽</b> (автор: <code>{r.submitted_by}</code>)"
                )

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="👨‍👩‍👧‍👦 Управление семьями", callback_data="fam:menu"),
        ],
        [
            InlineKeyboardButton(text="📜 История тарифов", callback_data=f"adm:sub:{apt.id}:tariffs"),
            InlineKeyboardButton(text="👥 Пользователи", callback_data=f"adm:sub:{apt.id}:users"),
        ],
        [
            InlineKeyboardButton(text="📋 Журнал изменений", callback_data=f"adm:sub:{apt.id}:logs"),
            InlineKeyboardButton(text="💾 Сделать бэкап БД", callback_data=f"adm:backup:{apt.id}"),
        ],
        [
            InlineKeyboardButton(text="⚙️ Начальные показания и тарифы", callback_data=f"cfg:open:{apt.id}"),
        ],
    ]

    if readings:
        fix_btns = [
            InlineKeyboardButton(text=f"✏️ Исправить {r.period}", callback_data=f"read:fix:{apt.id}:{r.period}")
            for r in readings[:4]
        ]
        for i in range(0, len(fix_btns), 2):
            rows.append(fix_btns[i : i + 2])

    rows.append([InlineKeyboardButton(text="⬅️ На главную", callback_data="main:home")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# 7. ЭКРАН ОТПРАВКИ ФОТО СЧЁТЧИКОВ НАНИМАТЕЛЕМ
# ---------------------------------------------------------------------------
def render_photo_panel(
    apt: Apartment,
    info_msg: str | None = None,
    error_msg: str | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    lines = [
        f"📷 <b>ОТПРАВКА ФОТО СЧЁТЧИКОВ — Кв. №{apt.number}</b>",
        "────────────────────────",
        "Прикрепите и отправьте в чат фотографию счётчика (через иконку 📎 или камеру).",
        "При желании можно добавить подпись к фото (например: <i>ХВС</i>, <i>ГВС</i> или <i>Электро</i>).",
    ]
    if info_msg:
        lines.append(f"\n✅ <b>{info_msg}</b>")
    if error_msg:
        lines.append(f"\n⚠️ <b>Ошибка:</b> {error_msg}")

    lines.append("\n👉 <b>Отправьте фотографию в чат:</b>")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ На главную", callback_data="main:home")]
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows if "rows" in locals() else kb.inline_keyboard)

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from sqlalchemy import desc, event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import ADMIN_IDS, BACKUP_DIR, DB_PATH, LANDLORD_IDS, TENANT_IDS, TZ
from models import Apartment, AuditLog, Base, Family, MeterReading, User

engine = create_async_engine(
    f"sqlite+aiosqlite:///{DB_PATH}",
    connect_args={"timeout": 15},
)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.execute("PRAGMA foreign_keys=ON;")
    cursor.execute("PRAGMA busy_timeout=5000;")
    cursor.close()


SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


def parse_decimal(value_str: str, places: str = "0.01") -> Decimal:
    cleaned = value_str.strip().replace(" ", "").replace(",", ".")
    val = Decimal(cleaned)
    if val < Decimal("0"):
        raise ValueError("Значение не может быть отрицательным.")
    return val.quantize(Decimal(places), rounding=ROUND_HALF_UP)


def parse_int_reading(value_str: str) -> Decimal:
    cleaned = value_str.strip().replace(" ", "")
    if not cleaned.isdigit():
        raise ValueError("введите целое неотрицательное число без дробной части (например: 32)")
    return Decimal(int(cleaned))


def round_rub(val: Decimal) -> Decimal:
    return val.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


@dataclass
class CalculationResult:
    prev_hvs: Decimal
    prev_gvs: Decimal
    prev_t1: Decimal
    prev_t2: Decimal
    prev_t3: Decimal
    hvs: Decimal
    gvs: Decimal
    t1: Decimal
    t2: Decimal
    t3: Decimal
    diff_hvs: Decimal
    diff_gvs: Decimal
    diff_water_total: Decimal
    diff_t1: Decimal
    diff_t2: Decimal
    diff_t3: Decimal
    diff_elec_total: Decimal
    cost_hvs: Decimal
    cost_gvs: Decimal
    cost_water_total: Decimal
    cost_t1: Decimal
    cost_t2: Decimal
    cost_t3: Decimal
    cost_elec_total: Decimal
    cost_total: Decimal
    decreased_fields: list[str]


def calculate_consumption_and_cost(
    prev: dict[str, Decimal],
    curr: dict[str, Decimal],
    tariffs: dict[str, Decimal],
    allow_decrease: bool = False,
) -> CalculationResult:
    decreased: list[str] = []
    diffs: dict[str, Decimal] = {}
    prev_ints: dict[str, Decimal] = {}
    curr_ints: dict[str, Decimal] = {}

    for key, label in [
        ("hvs", "ХВС"),
        ("gvs", "ГВС"),
        ("t1", "Т1"),
        ("t2", "Т2"),
        ("t3", "Т3"),
    ]:
        p_val = Decimal(str(prev[key])).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        c_val = Decimal(str(curr[key])).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        prev_ints[key] = p_val
        curr_ints[key] = c_val
        if c_val < p_val:
            decreased.append(label)
            diffs[key] = c_val if allow_decrease else (c_val - p_val)
        else:
            diffs[key] = c_val - p_val

    diff_water_total = diffs["hvs"] + diffs["gvs"]
    diff_elec_total = diffs["t1"] + diffs["t2"] + diffs["t3"]

    cost_hvs = round_rub(diffs["hvs"] * Decimal(str(tariffs["hvs"])))
    cost_gvs = round_rub(diffs["gvs"] * Decimal(str(tariffs["gvs"])))
    cost_water_total = cost_hvs + cost_gvs

    cost_t1 = round_rub(diffs["t1"] * Decimal(str(tariffs["t1"])))
    cost_t2 = round_rub(diffs["t2"] * Decimal(str(tariffs["t2"])))
    cost_t3 = round_rub(diffs["t3"] * Decimal(str(tariffs["t3"])))
    cost_elec_total = cost_t1 + cost_t2 + cost_t3

    cost_total = cost_water_total + cost_elec_total

    return CalculationResult(
        prev_hvs=prev_ints["hvs"],
        prev_gvs=prev_ints["gvs"],
        prev_t1=prev_ints["t1"],
        prev_t2=prev_ints["t1"] if "t2" not in prev_ints else prev_ints["t2"],
        prev_t3=prev_ints["t3"],
        hvs=curr_ints["hvs"],
        gvs=curr_ints["gvs"],
        t1=curr_ints["t1"],
        t2=curr_ints["t2"],
        t3=curr_ints["t3"],
        diff_hvs=diffs["hvs"],
        diff_gvs=diffs["gvs"],
        diff_water_total=diff_water_total,
        diff_t1=diffs["t1"],
        diff_t2=diffs["t2"],
        diff_t3=diffs["t3"],
        diff_elec_total=diff_elec_total,
        cost_hvs=cost_hvs,
        cost_gvs=cost_gvs,
        cost_water_total=cost_water_total,
        cost_t1=cost_t1,
        cost_t2=cost_t2,
        cost_t3=cost_t3,
        cost_elec_total=cost_elec_total,
        cost_total=cost_total,
        decreased_fields=decreased,
    )


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # Автомиграция колонок для существующей базы данных
        res_users = await conn.exec_driver_sql("PRAGMA table_info(users);")
        u_cols = [row[1] for row in res_users.fetchall()]
        if "name" not in u_cols:
            await conn.exec_driver_sql("ALTER TABLE users ADD COLUMN name VARCHAR(128);")
        if "family_id" not in u_cols:
            await conn.exec_driver_sql("ALTER TABLE users ADD COLUMN family_id INTEGER REFERENCES families(id) ON DELETE SET NULL;")

        res_apts = await conn.exec_driver_sql("PRAGMA table_info(apartments);")
        a_cols = [row[1] for row in res_apts.fetchall()]
        if "family_id" not in a_cols:
            await conn.exec_driver_sql("ALTER TABLE apartments ADD COLUMN family_id INTEGER REFERENCES families(id) ON DELETE CASCADE;")

        # Если на таблице apartments остался старый глобальный UNIQUE индекс только по полю number —
        # пересоздаём таблицу без глобального уникального ограничения, чтобы разные семьи могли иметь одинаковые номера квартир
        idx_list = (await conn.exec_driver_sql("PRAGMA index_list(apartments);")).fetchall()
        has_single_num_unique = False
        for idx_row in idx_list:
            if idx_row[2] == 1:  # unique == 1
                idx_name = idx_row[1]
                idx_cols = (await conn.exec_driver_sql(f"PRAGMA index_info({idx_name});")).fetchall()
                if len(idx_cols) == 1 and idx_cols[0][2] == "number":
                    has_single_num_unique = True
                    break

        if has_single_num_unique:
            await conn.exec_driver_sql("PRAGMA foreign_keys=OFF;")
            await conn.exec_driver_sql(
                """
                CREATE TABLE apartments_new (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    family_id INTEGER REFERENCES families (id) ON DELETE CASCADE,
                    number VARCHAR(32) NOT NULL,
                    has_tenant BOOLEAN NOT NULL DEFAULT 0,
                    water_day INTEGER NOT NULL DEFAULT 20,
                    elec_day INTEGER NOT NULL DEFAULT 25,
                    init_hvs NUMERIC(12, 3) NOT NULL DEFAULT 0,
                    init_gvs NUMERIC(12, 3) NOT NULL DEFAULT 0,
                    init_t1 NUMERIC(12, 3) NOT NULL DEFAULT 0,
                    init_t2 NUMERIC(12, 3) NOT NULL DEFAULT 0,
                    init_t3 NUMERIC(12, 3) NOT NULL DEFAULT 0,
                    tariff_hvs NUMERIC(10, 2) NOT NULL DEFAULT 0,
                    tariff_gvs NUMERIC(10, 2) NOT NULL DEFAULT 0,
                    tariff_t1 NUMERIC(10, 2) NOT NULL DEFAULT 0,
                    tariff_t2 NUMERIC(10, 2) NOT NULL DEFAULT 0,
                    tariff_t3 NUMERIC(10, 2) NOT NULL DEFAULT 0,
                    agreement_no VARCHAR(64),
                    agreement_date_start DATE,
                    agreement_date_finish DATE,
                    created_at DATETIME NOT NULL
                );
                """
            )
            await conn.exec_driver_sql(
                """
                INSERT INTO apartments_new (
                    id, family_id, number, has_tenant, water_day, elec_day,
                    init_hvs, init_gvs, init_t1, init_t2, init_t3,
                    tariff_hvs, tariff_gvs, tariff_t1, tariff_t2, tariff_t3,
                    agreement_no, agreement_date_start, agreement_date_finish, created_at
                )
                SELECT
                    id, family_id, number, has_tenant, water_day, elec_day,
                    init_hvs, init_gvs, init_t1, init_t2, init_t3,
                    tariff_hvs, tariff_gvs, tariff_t1, tariff_t2, tariff_t3,
                    agreement_no, agreement_date_start, agreement_date_finish, created_at
                FROM apartments;
                """
            )
            await conn.exec_driver_sql("DROP TABLE apartments;")
            await conn.exec_driver_sql("ALTER TABLE apartments_new RENAME TO apartments;")
            await conn.exec_driver_sql("PRAGMA foreign_keys=ON;")

    async with SessionLocal() as session:
        # Создаём базовую семью, если семей ещё нет
        first_fam = (await session.execute(select(Family).order_by(Family.id).limit(1))).scalar_one_or_none()
        if not first_fam:
            first_fam = Family(name="Основная семья")
            session.add(first_fam)
            await session.flush()

        default_fam_id = first_fam.id

        # Привязываем существующие квартиры без семьи к базовой семье
        orphan_apts = (await session.execute(select(Apartment).where(Apartment.family_id.is_(None)))).scalars().all()
        for a in orphan_apts:
            a.family_id = default_fam_id

        for tg_id in ADMIN_IDS:
            u = await session.get(User, tg_id)
            if not u:
                session.add(User(telegram_id=tg_id, role="admin", family_id=default_fam_id, is_active=True))
            else:
                u.role = "admin"
                u.is_active = True
                if not u.family_id:
                    u.family_id = default_fam_id

        for tg_id in LANDLORD_IDS:
            if tg_id in ADMIN_IDS:
                continue
            u = await session.get(User, tg_id)
            if not u:
                session.add(User(telegram_id=tg_id, role="landlord", family_id=default_fam_id, is_active=True))
            elif not u.family_id:
                u.family_id = default_fam_id

        for tg_id in TENANT_IDS:
            if tg_id in ADMIN_IDS or tg_id in LANDLORD_IDS:
                continue
            u = await session.get(User, tg_id)
            if not u:
                session.add(User(telegram_id=tg_id, role="tenant", is_active=True))

        # Синхронизируем family_id арендаторов с их квартирами
        all_users = (await session.execute(select(User))).scalars().all()
        for u in all_users:
            if u.role == "tenant" and u.apartment_id:
                apt = await session.get(Apartment, u.apartment_id)
                if apt:
                    u.family_id = apt.family_id
            elif u.role in ("admin", "landlord") and not u.family_id:
                u.family_id = default_fam_id

        await session.commit()


async def get_authorized_user(telegram_id: int) -> tuple[User | None, str | None]:
    async with SessionLocal() as session:
        user = await session.get(User, telegram_id)
        if not user or not user.is_active:
            return None, "⛔ Доступ к боту запрещён. Ваш Telegram ID не зарегистрирован в системе."

        if user.role == "landlord" and not user.family_id:
            return None, "⛔ Ваш профиль владельца не привязан ни к одной семье. Обратитесь к администратору."

        if user.role == "tenant":
            if not user.apartment_id:
                return None, "⛔ За вашим профилем нанимателя ещё не закреплена квартира."
            apt = await session.get(Apartment, user.apartment_id)
            if not apt or not apt.has_tenant:
                return None, "⛔ Доступ приостановлен наймодателем."
            today = datetime.now(TZ).date()
            if apt.agreement_date_finish and today > apt.agreement_date_finish:
                return (
                    None,
                    f"⛔ Срок действия договора найма №{apt.agreement_no or 'б/н'} истёк "
                    f"({apt.agreement_date_finish.strftime('%d.%m.%Y')}). Доступ закрыт.",
                )
        return user, None


async def get_previous_baseline(session: AsyncSession, apt: Apartment, target_period: str) -> dict[str, Decimal]:
    stmt = (
        select(MeterReading)
        .where(MeterReading.apartment_id == apt.id, MeterReading.period < target_period)
        .order_by(desc(MeterReading.period), desc(MeterReading.id))
        .limit(1)
    )
    prev_row = (await session.execute(stmt)).scalar_one_or_none()
    if prev_row:
        return {
            "hvs": Decimal(str(prev_row.hvs)).quantize(Decimal("1"), rounding=ROUND_HALF_UP),
            "gvs": Decimal(str(prev_row.gvs)).quantize(Decimal("1"), rounding=ROUND_HALF_UP),
            "t1": Decimal(str(prev_row.t1)).quantize(Decimal("1"), rounding=ROUND_HALF_UP),
            "t2": Decimal(str(prev_row.t2)).quantize(Decimal("1"), rounding=ROUND_HALF_UP),
            "t3": Decimal(str(prev_row.t3)).quantize(Decimal("1"), rounding=ROUND_HALF_UP),
        }
    return {
        "hvs": Decimal(str(apt.init_hvs)).quantize(Decimal("1"), rounding=ROUND_HALF_UP),
        "gvs": Decimal(str(apt.init_gvs)).quantize(Decimal("1"), rounding=ROUND_HALF_UP),
        "t1": Decimal(str(apt.init_t1)).quantize(Decimal("1"), rounding=ROUND_HALF_UP),
        "t2": Decimal(str(apt.init_t2)).quantize(Decimal("1"), rounding=ROUND_HALF_UP),
        "t3": Decimal(str(apt.init_t3)).quantize(Decimal("1"), rounding=ROUND_HALF_UP),
    }


async def log_audit(
    session: AsyncSession, user_id: int, action: str, details: str, apartment_id: int | None = None
) -> None:
    session.add(
        AuditLog(
            timestamp=datetime.now(TZ).replace(tzinfo=None),
            user_id=user_id,
            apartment_id=apartment_id,
            action=action,
            details=details,
        )
    )


def create_db_backup() -> Path:
    ts = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
    backup_file = BACKUP_DIR / f"utility_bot_backup_{ts}.db"
    src = sqlite3.connect(str(DB_PATH))
    dst = sqlite3.connect(str(backup_file))
    with dst:
        src.backup(dst)
    dst.close()
    src.close()

    backups = sorted(BACKUP_DIR.glob("utility_bot_backup_*.db"))
    for old in backups[:-14]:
        old.unlink(missing_ok=True)
    return backup_file

"""Async SQLAlchemy engine, session factory, and safe SQLite schema migrations."""

from __future__ import annotations

import logging

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import DATABASE_URL, SQLITE_BUSY_TIMEOUT_MS
from database.models import Base

logger = logging.getLogger(__name__)


def attach_sqlite_pragmas(target_engine, busy_timeout_ms: int = SQLITE_BUSY_TIMEOUT_MS) -> None:
    """Attach production SQLite PRAGMAs (FK, WAL, busy_timeout) to an async engine."""
    @event.listens_for(target_engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _connection_record) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        cursor.close()


engine = create_async_engine(DATABASE_URL, echo=False, future=True)
attach_sqlite_pragmas(engine)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

APARTMENT_MIGRATIONS = (
    ("property_type", "ALTER TABLE apartments ADD COLUMN property_type VARCHAR(20) NOT NULL DEFAULT 'apartment'"),
    ("enabled_meters", "ALTER TABLE apartments ADD COLUMN enabled_meters TEXT"),
    ("gas_submission_day", "ALTER TABLE apartments ADD COLUMN gas_submission_day INTEGER"),
    ("sort_order", "ALTER TABLE apartments ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0"),
    ("irrigation_mode", "ALTER TABLE apartments ADD COLUMN irrigation_mode VARCHAR(20) NOT NULL DEFAULT 'parallel'"),
    ("rent_payment_day", "ALTER TABLE apartments ADD COLUMN rent_payment_day INTEGER"),
    ("rent_reminder_enabled", "ALTER TABLE apartments ADD COLUMN rent_reminder_enabled BOOLEAN NOT NULL DEFAULT 0"),
    ("rent_reminder_mode", "ALTER TABLE apartments ADD COLUMN rent_reminder_mode VARCHAR(20)"),
    ("rent_reminder_days_after", "ALTER TABLE apartments ADD COLUMN rent_reminder_days_after INTEGER"),
)

METER_READING_MIGRATIONS = (
    ("is_replacement_init", "ALTER TABLE meter_readings ADD COLUMN is_replacement_init BOOLEAN NOT NULL DEFAULT 0"),
)


def _apply_table_migrations(sync_conn, table_name: str, migrations: tuple[tuple[str, str], ...]) -> None:
    rows = sync_conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
    if not rows:
        return
    existing_cols = {row[1] for row in rows}
    for col_name, ddl in migrations:
        if col_name not in existing_cols:
            logger.info("Applying SQLite migration: adding column %s.%s", table_name, col_name)
            sync_conn.execute(text(ddl))
            if table_name == "apartments" and col_name == "sort_order":
                sync_conn.execute(text("UPDATE apartments SET sort_order = id"))


def _run_sqlite_migrations(sync_conn) -> None:
    """Safely add new columns to existing SQLite tables without data loss."""
    _apply_table_migrations(sync_conn, "apartments", APARTMENT_MIGRATIONS)
    _apply_table_migrations(sync_conn, "meter_readings", METER_READING_MIGRATIONS)


async def init_db() -> None:
    """Create all tables if they do not exist and apply safe column migrations."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_run_sqlite_migrations)

"""Async SQLAlchemy engine, session factory, and safe SQLite schema migrations."""

from __future__ import annotations

import logging

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import DATABASE_URL
from database.models import Base

logger = logging.getLogger(__name__)

engine = create_async_engine(DATABASE_URL, echo=False, future=True)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, _connection_record) -> None:
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


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
)


def _run_sqlite_migrations(sync_conn) -> None:
    """Safely add new columns to existing SQLite tables without data loss."""
    rows = sync_conn.execute(text("PRAGMA table_info(apartments)")).fetchall()
    existing_cols = {row[1] for row in rows}
    for col_name, ddl in APARTMENT_MIGRATIONS:
        if col_name not in existing_cols:
            logger.info("Applying SQLite migration: adding column apartments.%s", col_name)
            sync_conn.execute(text(ddl))
            if col_name == "sort_order":
                sync_conn.execute(text("UPDATE apartments SET sort_order = id"))


async def init_db() -> None:
    """Create all tables if they do not exist and apply safe column migrations."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_run_sqlite_migrations)

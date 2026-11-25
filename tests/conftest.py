"""Shared pytest fixtures configuring temporary SQLite DB with production PRAGMAs."""

from __future__ import annotations

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import config
from database import crud
from database.models import Base
from database.session import _run_sqlite_migrations, attach_sqlite_pragmas


@pytest_asyncio.fixture
async def temp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_prod_pragmas.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}", future=True)
    attach_sqlite_pragmas(engine, busy_timeout_ms=5000)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_run_sqlite_migrations)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(crud, "async_session_factory", factory)
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 100)
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123456:TEST_TOKEN")
    yield db_file
    await engine.dispose()

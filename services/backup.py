"""SQLite database backup service."""

from __future__ import annotations

import asyncio
import datetime
import sqlite3
from pathlib import Path

from config import BACKUP_DIR, BASE_DIR, DATABASE_URL


def _resolve_db_path() -> Path:
    prefix = "sqlite+aiosqlite:///"
    if DATABASE_URL.startswith(prefix):
        return Path(DATABASE_URL[len(prefix):])
    return BASE_DIR / "data" / "utility_meter.db"


def _create_backup_sync(keep_last: int = 14) -> Path:
    db_path = _resolve_db_path()
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUP_DIR / f"utility_meter_backup_{timestamp}.db"

    src = sqlite3.connect(str(db_path))
    dst = sqlite3.connect(str(backup_path))
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()

    backups = sorted(
        BACKUP_DIR.glob("utility_meter_backup_*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old_file in backups[keep_last:]:
        try:
            old_file.unlink()
        except OSError:
            pass

    return backup_path


async def create_backup(keep_last: int = 14) -> Path:
    return await asyncio.to_thread(_create_backup_sync, keep_last)

"""SQLite database backup and photo retention cleanup service."""

from __future__ import annotations

import asyncio
import datetime
import logging
import sqlite3
import time
from pathlib import Path

from config import BACKUP_DIR, DATABASE_URL, PHOTO_RETENTION_DAYS, PHOTOS_DIR

logger = logging.getLogger(__name__)


def _resolve_db_path() -> Path:
    prefix = "sqlite+aiosqlite:///"
    if not DATABASE_URL.startswith(prefix):
        raise ValueError(f"Резервное копирование поддерживается только для SQLite, получено: {DATABASE_URL}")
    db_path = Path(DATABASE_URL[len(prefix):])
    if not db_path.exists():
        raise FileNotFoundError(f"Файл базы данных не найден: {db_path}")
    return db_path


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


def _cleanup_old_photos_sync(retention_days: int = PHOTO_RETENTION_DAYS) -> int:
    """Delete meter photos older than retention_days."""
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for path in PHOTOS_DIR.iterdir():
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError as e:
            logger.warning("Failed to remove old photo %s: %s", path.name, e)
    return removed


async def create_backup(keep_last: int = 14) -> Path:
    return await asyncio.to_thread(_create_backup_sync, keep_last)


async def cleanup_old_photos(retention_days: int = PHOTO_RETENTION_DAYS) -> int:
    removed = await asyncio.to_thread(_cleanup_old_photos_sync, retention_days)
    if removed:
        logger.info("Cleaned up %d meter photo(s) older than %d days", removed, retention_days)
    return removed

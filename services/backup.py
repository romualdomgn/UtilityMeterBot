"""SQLite database backup service with atomic writes, 60-day retention, and change detection."""

from __future__ import annotations

import asyncio
import datetime
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from weakref import WeakKeyDictionary

import config

logger = logging.getLogger(__name__)
PHOTOS_DIR = config.PHOTOS_DIR
_BACKUP_LOCKS: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = WeakKeyDictionary()


def _get_backup_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _BACKUP_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _BACKUP_LOCKS[loop] = lock
    return lock


def _resolve_db_path() -> Path:
    prefix = "sqlite+aiosqlite:///"
    if not config.DATABASE_URL.startswith(prefix):
        raise ValueError(
            f"Резервное копирование поддерживается только для SQLite, получено: {config.DATABASE_URL}"
        )
    db_path = Path(config.DATABASE_URL[len(prefix):])
    if not db_path.exists():
        raise FileNotFoundError(f"Файл базы данных не найден: {db_path}")
    return db_path


def _db_last_modified(db_path: Path) -> float:
    """Return latest mtime across the SQLite DB file and its WAL sidecar."""
    candidates = [db_path, Path(f"{db_path}-wal")]
    mtimes = []
    for p in candidates:
        try:
            if p.exists():
                mtimes.append(p.stat().st_mtime)
        except OSError:
            continue
    return max(mtimes) if mtimes else 0.0


def _list_finished_backups(backup_dir: Path) -> list[Path]:
    return sorted(
        backup_dir.glob("utility_meter_backup_*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def _rotate_backups_sync(backup_dir: Path, retention_days: int) -> int:
    """Remove backups older than retention_days while always keeping the newest backup."""
    backups = _list_finished_backups(backup_dir)
    if len(backups) <= 1:
        return 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for old_file in backups[1:]:
        try:
            if old_file.stat().st_mtime < cutoff:
                old_file.unlink()
                removed += 1
        except OSError as e:
            logger.warning("Failed to remove expired backup %s: %s", old_file.name, e)
    return removed


def _create_backup_sync(
    retention_days: int | None = None,
    skip_if_unchanged: bool = True,
) -> Path | None:
    """Create an atomic SQLite backup; skip when DB has not changed since last backup."""
    ret_days = config.BACKUP_RETENTION_DAYS if retention_days is None else retention_days
    db_path = _resolve_db_path()
    backup_dir = config.BACKUP_DIR
    backup_dir.mkdir(parents=True, exist_ok=True)

    existing = _list_finished_backups(backup_dir)
    if skip_if_unchanged and existing:
        latest = existing[0]
        db_mtime = _db_last_modified(db_path)
        if db_mtime <= latest.stat().st_mtime:
            _rotate_backups_sync(backup_dir, ret_days)
            logger.info("Skipping backup: database has not changed since %s", latest.name)
            return None

    timestamp = datetime.datetime.now(config.BOT_TZ).strftime("%Y%m%d_%H%M%S")
    unique_suffix = uuid.uuid4().hex[:6]
    backup_path = backup_dir / f"utility_meter_backup_{timestamp}_{unique_suffix}.db"
    tmp_path = backup_dir / f".{backup_path.name}.tmp"

    src = sqlite3.connect(str(db_path))
    dst = sqlite3.connect(str(tmp_path))
    try:
        with dst:
            src.backup(dst, pages=100, sleep=0.01)
    except Exception:
        dst.close()
        src.close()
        tmp_path.unlink(missing_ok=True)
        raise
    else:
        dst.close()
        src.close()

    tmp_path.replace(backup_path)
    _rotate_backups_sync(backup_dir, ret_days)
    return backup_path


def _cleanup_old_photos_sync(retention_days: int = config.PHOTO_RETENTION_DAYS) -> int:
    """Legacy helper kept for cleanup of any pre-existing local photos."""
    if not PHOTOS_DIR.exists():
        return 0
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


async def create_backup(
    retention_days: int | None = None,
    skip_if_unchanged: bool = True,
) -> Path | None:
    async with _get_backup_lock():
        return await asyncio.to_thread(_create_backup_sync, retention_days, skip_if_unchanged)


async def get_or_create_manual_backup(retention_days: int | None = None) -> tuple[Path, bool]:
    """Return (backup_path, created_new) without duplicating backups if DB is unchanged."""
    async with _get_backup_lock():
        created = await asyncio.to_thread(_create_backup_sync, retention_days, True)
        if created is not None:
            return created, True
        existing = _list_finished_backups(config.BACKUP_DIR)
        if existing:
            return existing[0], False
        fresh = await asyncio.to_thread(_create_backup_sync, retention_days, False)
        assert fresh is not None
        return fresh, True


async def cleanup_old_photos(retention_days: int = config.PHOTO_RETENTION_DAYS) -> int:
    removed = await asyncio.to_thread(_cleanup_old_photos_sync, retention_days)
    if removed:
        logger.info("Cleaned up %d meter photo(s) older than %d days", removed, retention_days)
    return removed

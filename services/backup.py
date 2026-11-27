"""SQLite database backup service with revision-based change detection, integrity checks, and family isolation."""

from __future__ import annotations

import asyncio
import datetime
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from weakref import WeakKeyDictionary

import config

logger = logging.getLogger(__name__)
PHOTOS_DIR = config.PHOTOS_DIR
_BACKUP_LOCKS: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = WeakKeyDictionary()
_SYNC_BACKUP_LOCK = threading.Lock()


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


def _read_business_revision(db_file: Path) -> int | None:
    """Read durable business_revision from db_meta table inside a SQLite file."""
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=5.0)
        row = conn.execute(
            "SELECT value FROM db_meta WHERE key = 'business_revision'"
        ).fetchone()
        return int(row[0]) if row is not None else None
    except sqlite3.Error:
        return None
    finally:
        if conn is not None:
            conn.close()


def _verify_sqlite_quick_check(conn: sqlite3.Connection) -> None:
    rows = conn.execute("PRAGMA quick_check").fetchall()
    if rows != [("ok",)]:
        raise RuntimeError(f"Проверка целостности резервной копии не пройдена: {rows[:3]}")


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
    """P1-09, P2-13, P2-20: atomic SQLite backup using durable business_revision and PRAGMA quick_check."""
    with _SYNC_BACKUP_LOCK:
        ret_days = config.BACKUP_RETENTION_DAYS if retention_days is None else retention_days
        db_path = _resolve_db_path()
        backup_dir = config.BACKUP_DIR
        backup_dir.mkdir(parents=True, exist_ok=True)

        existing = _list_finished_backups(backup_dir)
        if skip_if_unchanged and existing:
            latest = existing[0]
            src_rev = _read_business_revision(db_path)
            bak_rev = _read_business_revision(latest)
            if src_rev is not None and bak_rev is not None and src_rev == bak_rev:
                _rotate_backups_sync(backup_dir, ret_days)
                logger.info(
                    "Skipping backup: business_revision=%s unchanged since %s",
                    src_rev,
                    latest.name,
                )
                return None

        timestamp = datetime.datetime.now(config.BOT_TZ).strftime("%Y%m%d_%H%M%S")
        unique_suffix = uuid.uuid4().hex[:6]
        backup_path = backup_dir / f"utility_meter_backup_{timestamp}_{unique_suffix}.db"
        tmp_path = backup_dir / f".{backup_path.name}.tmp"

        src: sqlite3.Connection | None = None
        dst: sqlite3.Connection | None = None
        try:
            src = sqlite3.connect(str(db_path), timeout=5.0)
            dst = sqlite3.connect(str(tmp_path), timeout=5.0)
            with dst:
                src.backup(dst, pages=100, sleep=0.01)
            _verify_sqlite_quick_check(dst)
        except Exception:
            if dst is not None:
                try:
                    dst.close()
                except Exception:
                    pass
            if src is not None:
                try:
                    src.close()
                except Exception:
                    pass
            tmp_path.unlink(missing_ok=True)
            raise
        else:
            dst.close()
            src.close()

        try:
            tmp_path.replace(backup_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

        _rotate_backups_sync(backup_dir, ret_days)
        return backup_path


def _cleanup_old_photos_sync(retention_days: int = config.PHOTO_RETENTION_DAYS) -> int:
    """Legacy helper for cleanup of any pre-existing local photos in data/photos."""
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
    """Return (backup_path, created_new) without duplicating backups if business data is unchanged."""
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


def _create_family_backup_sync(family_id: int) -> Path:
    """P1-01 & P2-13: self-contained SQLite backup containing ONLY records scoped to family_id."""
    with _SYNC_BACKUP_LOCK:
        db_path = _resolve_db_path()
        out_dir = config.EXPORT_DIR
        out_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.datetime.now(config.BOT_TZ).strftime("%Y%m%d_%H%M%S")
        unique_suffix = uuid.uuid4().hex[:6]
        out_path = out_dir / f"utility_meter_family_{family_id}_backup_{timestamp}_{unique_suffix}.db"
        tmp_path = out_dir / f".{out_path.name}.tmp"

        src: sqlite3.Connection | None = None
        dst: sqlite3.Connection | None = None
        try:
            src = sqlite3.connect(str(db_path), timeout=5.0)
            dst = sqlite3.connect(str(tmp_path), timeout=5.0)
            with dst:
                src.backup(dst, pages=100, sleep=0.01)
        except Exception:
            if dst is not None:
                try:
                    dst.close()
                except Exception:
                    pass
            if src is not None:
                try:
                    src.close()
                except Exception:
                    pass
            tmp_path.unlink(missing_ok=True)
            raise
        else:
            src.close()
            src = None

        try:
            existing_tables = {
                row[0]
                for row in dst.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            dst.execute("PRAGMA foreign_keys=OFF")

            if "scheduler_runs" in existing_tables:
                dst.execute("DELETE FROM scheduler_runs")

            # P1-01: strictly filter audit_logs by immutable audit_logs.family_id
            if "audit_logs" in existing_tables:
                al_cols = {r[1] for r in dst.execute("PRAGMA table_info(audit_logs)").fetchall()}
                if "family_id" in al_cols:
                    dst.execute(
                        "DELETE FROM audit_logs WHERE family_id IS NULL OR family_id != ?",
                        (family_id,),
                    )
                else:
                    dst.execute("DELETE FROM audit_logs")

            apt_scoped_tables = (
                "meter_submissions",
                "meter_replacements",
                "meter_devices",
                "meter_readings",
                "initial_readings",
                "tariffs",
                "apartment_users",
            )
            if "apartments" in existing_tables:
                for tbl in apt_scoped_tables:
                    if tbl in existing_tables:
                        dst.execute(
                            f'DELETE FROM "{tbl}" WHERE apartment_id NOT IN '
                            "(SELECT id FROM apartments WHERE family_id = ?)",
                            (family_id,),
                        )

            if "family_owners" in existing_tables:
                dst.execute("DELETE FROM family_owners WHERE family_id != ?", (family_id,))

            if "users" in existing_tables:
                union_parts = ["SELECT id FROM users WHERE family_id = ?"]
                params: list[int] = [family_id]
                if "family_owners" in existing_tables:
                    union_parts.append("SELECT user_id FROM family_owners WHERE family_id = ?")
                    params.append(family_id)
                if "apartment_users" in existing_tables and "apartments" in existing_tables:
                    union_parts.append(
                        "SELECT au.user_id FROM apartment_users au "
                        "JOIN apartments a ON a.id = au.apartment_id "
                        "WHERE a.family_id = ?"
                    )
                    params.append(family_id)
                dst.execute(
                    f"DELETE FROM users WHERE id NOT IN ({' UNION '.join(union_parts)})",
                    tuple(params),
                )

            if "apartments" in existing_tables:
                dst.execute("DELETE FROM apartments WHERE family_id != ?", (family_id,))

            if "families" in existing_tables:
                dst.execute("DELETE FROM families WHERE id != ?", (family_id,))
                fam_cols = {r[1] for r in dst.execute("PRAGMA table_info(families)").fetchall()}
                if "admin_name" in fam_cols:
                    dst.execute("UPDATE families SET admin_name = NULL")
            if "users" in existing_tables:
                usr_cols = {r[1] for r in dst.execute("PRAGMA table_info(users)").fetchall()}
                if "admin_name" in usr_cols:
                    dst.execute("UPDATE users SET admin_name = NULL")

            dst.commit()

            dst.execute("PRAGMA foreign_keys=ON")
            fk_errors = dst.execute("PRAGMA foreign_key_check").fetchall()
            if fk_errors:
                raise RuntimeError(f"Family backup FK integrity check failed: {fk_errors[:5]}")

            # Purge all freed pages so zero bytes from other families remain in the .db binary
            dst.isolation_level = None
            dst.execute("PRAGMA journal_mode=DELETE")
            dst.execute("VACUUM")
            _verify_sqlite_quick_check(dst)
        except Exception:
            try:
                dst.close()
            except Exception:
                pass
            tmp_path.unlink(missing_ok=True)
            raise
        else:
            dst.close()

        try:
            tmp_path.replace(out_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
        return out_path


async def create_family_backup(family_id: int) -> Path:
    """Create a family-scoped SQLite backup file in EXPORT_DIR (caller deletes after sending)."""
    async with _get_backup_lock():
        return await asyncio.to_thread(_create_family_backup_sync, family_id)

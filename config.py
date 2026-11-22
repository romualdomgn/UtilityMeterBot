"""Application configuration — loads .env and exposes typed settings."""

from __future__ import annotations

import datetime
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"

load_dotenv(ENV_FILE)


def _normalize_webapp_url(raw: str | None) -> str:
    url = (raw or "https://utility-meter.litvinov.uk").strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    return url


# ── Telegram & Web App ────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = (
    os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("BOT_TOKEN")
    or ""
).strip()

WEBAPP_URL: str = _normalize_webapp_url(os.getenv("WEBAPP_URL"))
WEBAPP_HOST: str = os.getenv("WEBAPP_HOST", "0.0.0.0")
WEBAPP_PORT: int = int(os.getenv("WEBAPP_PORT", "8000"))
INIT_DATA_MAX_AGE_SECONDS: int = int(os.getenv("INIT_DATA_MAX_AGE_SECONDS", str(24 * 3600)))
ADMIN_TELEGRAM_ID: int = int(os.getenv("ADMIN_TELEGRAM_ID", "0") or "0")

# ── Database ──────────────────────────────────────────────────────────
DATABASE_URL: str = os.getenv(
    "DATABASE_URL",
    f"sqlite+aiosqlite:///{BASE_DIR / 'data' / 'utility_meter.db'}",
)

# ── Timezone ──────────────────────────────────────────────────────────
TIMEZONE_OFFSET: int = int(os.getenv("TIMEZONE_OFFSET", "5"))
BOT_TZ: datetime.timezone = datetime.timezone(datetime.timedelta(hours=TIMEZONE_OFFSET))


def local_today() -> datetime.date:
    """HIGH-07: unified application date in configured bot timezone."""
    return datetime.datetime.now(BOT_TZ).date()


# ── Paths & Retention ─────────────────────────────────────────────────
DATA_DIR = BASE_DIR / "data"
BACKUP_DIR = DATA_DIR / "backups"
EXPORT_DIR = DATA_DIR / "exports"
PHOTOS_DIR = DATA_DIR / "photos"
PHOTO_RETENTION_DAYS: int = int(os.getenv("PHOTO_RETENTION_DAYS", "30"))
MAX_PHOTO_BYTES: int = 15 * 1024 * 1024

for _d in (DATA_DIR, BACKUP_DIR, EXPORT_DIR, PHOTOS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

"""Application configuration — loads .env and exposes typed settings."""

from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"

load_dotenv(ENV_FILE)


def _normalize_webapp_url(raw: str | None) -> str:
    """Ensure WEBAPP_URL always has https:// scheme and no trailing slash."""
    url = (raw or "https://utility_meter.litvinov.uk").strip().rstrip("/")
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


# ── User IDs ──────────────────────────────────────────────────────────
def _parse_ids(raw: str | None) -> list[int]:
    """Parse comma-separated Telegram IDs into a list of ints."""
    if not raw:
        return []
    result: list[int] = []
    for x in raw.split(","):
        x = x.strip()
        if x.isdigit():
            result.append(int(x))
    return result


ADMIN_TELEGRAM_ID: int = int(os.getenv("ADMIN_TELEGRAM_ID", "0") or "0")
LANDLORD_TELEGRAM_IDS: list[int] = _parse_ids(os.getenv("LANDLORD_TELEGRAM_ID"))
TENANT_IDS: list[int] = _parse_ids(os.getenv("TENANT_ID"))


def reload_env_ids() -> None:
    """Re-read .env and refresh the global ID lists."""
    global ADMIN_TELEGRAM_ID, LANDLORD_TELEGRAM_IDS, TENANT_IDS
    load_dotenv(ENV_FILE, override=True)
    ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0") or "0")
    LANDLORD_TELEGRAM_IDS = _parse_ids(os.getenv("LANDLORD_TELEGRAM_ID"))
    TENANT_IDS = _parse_ids(os.getenv("TENANT_ID"))


def _write_env_key_inplace(key: str, value: str) -> None:
    """Update or append a key in .env in-place (safe for Docker bind mounts)."""
    if not ENV_FILE.exists():
        return
    try:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
        updated = False
        new_lines: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
                new_lines.append(f"{key}={value}")
                updated = True
            else:
                new_lines.append(line)
        if not updated:
            new_lines.append(f"{key}={value}")
        ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def sync_id_to_env(role: str, telegram_id: int, remove: bool = False) -> None:
    """Add or remove a landlord/tenant Telegram ID in .env and refresh memory."""
    reload_env_ids()
    if role == "landlord":
        key = "LANDLORD_TELEGRAM_ID"
        current = set(LANDLORD_TELEGRAM_IDS)
    elif role == "tenant":
        key = "TENANT_ID"
        current = set(TENANT_IDS)
    else:
        return

    if remove:
        current.discard(telegram_id)
    else:
        current.add(telegram_id)

    new_val = ",".join(str(x) for x in sorted(current))
    _write_env_key_inplace(key, new_val)
    os.environ[key] = new_val
    reload_env_ids()


# ── Database ──────────────────────────────────────────────────────────
DATABASE_URL: str = os.getenv(
    "DATABASE_URL",
    f"sqlite+aiosqlite:///{BASE_DIR / 'data' / 'utility_meter.db'}",
)

# ── Timezone ──────────────────────────────────────────────────────────
TIMEZONE_OFFSET: int = int(os.getenv("TIMEZONE_OFFSET", "5"))

# ── Paths ─────────────────────────────────────────────────────────────
DATA_DIR = BASE_DIR / "data"
BACKUP_DIR = DATA_DIR / "backups"
EXPORT_DIR = DATA_DIR / "exports"
PHOTOS_DIR = DATA_DIR / "photos"

for _d in (DATA_DIR, BACKUP_DIR, EXPORT_DIR, PHOTOS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

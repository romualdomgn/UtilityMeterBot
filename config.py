import os
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _parse_id_list(env_val: str) -> set[int]:
    result: set[int] = set()
    for part in env_val.split(","):
        part = part.strip()
        if part.isdigit():
            result.add(int(part))
    return result


BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в .env")

ADMIN_ID_RAW = os.getenv("ADMIN_TELEGRAM_ID", "").strip()
ADMIN_IDS: set[int] = _parse_id_list(ADMIN_ID_RAW)
LANDLORD_IDS: set[int] = _parse_id_list(os.getenv("LANDLORD_TELEGRAM_ID", ""))
TENANT_IDS: set[int] = _parse_id_list(os.getenv("TENANT_ID", ""))

DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "data" / "utility_bot.db")))
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", str(BASE_DIR / "backups")))
EXPORT_DIR = BASE_DIR / "exports"

DB_PATH.parent.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
EXPORT_DIR.mkdir(parents=True, exist_ok=True)

TZ_NAME = os.getenv("TIMEZONE", "Asia/Yekaterinburg")
TZ = ZoneInfo(TZ_NAME)
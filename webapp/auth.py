"""Telegram Web App initData HMAC-SHA256 verification and role context."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

from fastapi import Header, HTTPException

import config
from services.access import resolve_user_access


@dataclass
class WebAppUser:
    telegram_id: int
    role: str
    family_id: int | None
    name: str


def validate_init_data(init_data: str) -> dict:
    if not init_data:
        raise HTTPException(status_code=401, detail="Missing X-Init-Data header")

    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = fields.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Missing hash in initData")

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", config.TELEGRAM_BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, received_hash):
        raise HTTPException(status_code=401, detail="Invalid initData signature")

    try:
        auth_date = int(fields.get("auth_date", "0"))
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid auth_date")

    if time.time() - auth_date > config.INIT_DATA_MAX_AGE_SECONDS:
        raise HTTPException(status_code=401, detail="initData expired")

    return fields


async def get_current_user(x_init_data: str = Header(default="")) -> WebAppUser:
    fields = validate_init_data(x_init_data)
    raw_user = fields.get("user")
    if not raw_user:
        raise HTTPException(status_code=401, detail="Missing user in initData")

    try:
        tg_user = json.loads(raw_user)
        tid = int(tg_user["id"])
    except (ValueError, KeyError, TypeError):
        raise HTTPException(status_code=401, detail="Malformed user payload")

    allowed, reason, role, db_user = await resolve_user_access(tid)
    if not allowed:
        raise HTTPException(status_code=403, detail=reason or "Access denied")

    family_id = db_user.family_id if db_user else None
    tg_name = " ".join(
        p for p in (tg_user.get("first_name"), tg_user.get("last_name")) if p
    ).strip()
    name = (db_user.name if db_user and db_user.name else tg_name) or str(tid)

    return WebAppUser(
        telegram_id=tid,
        role=role,
        family_id=family_id,
        name=name,
    )

"""Settings that admins change from the website (stored in the database).

Anything here used to live in .env. Values set on the website win; .env values are
only a fallback, so older installs keep working without changes.
"""
from __future__ import annotations

import time

from . import config, security
from .db import SessionLocal, get_setting, set_setting

_cache: dict = {"at": 0.0, "v": {}}

KEYS = ("tg_api_id", "tg_api_hash", "tg_main_session_enc", "tg_vip_chats", "tg_bot_token_enc",
        "tg_bot_username", "public_url", "tg_vip_topics", "vip_open")


def _load() -> dict:
    if time.time() - _cache["at"] < 5:
        return _cache["v"]
    v = {}
    try:
        with SessionLocal() as s:
            for k in KEYS:
                v[k] = get_setting(s, k)
    except Exception:  # noqa: BLE001  (database not ready yet)
        v = {}
    _cache.update(at=time.time(), v=v)
    return v


def invalidate() -> None:
    _cache["at"] = 0.0


def save(**values) -> None:
    with SessionLocal() as s:
        for k, val in values.items():
            set_setting(s, k, val)
        s.commit()
    invalidate()


def _dec(value) -> str:
    return security.decrypt(value) if value else ""


def tg_api() -> tuple[int, str]:
    v = _load()
    api_id = int(v.get("tg_api_id") or 0) or config.TG_API_ID
    api_hash = v.get("tg_api_hash") or config.TG_API_HASH
    return api_id, api_hash


def main_session() -> str:
    return _dec(_load().get("tg_main_session_enc")) or config.TG_MAIN_SESSION


def vip_chats() -> list:
    chats = _load().get("tg_vip_chats") or config.TG_VIP_CHATS
    out = []
    for c in chats:
        c = str(c).strip()
        if c:
            out.append(int(c) if c.lstrip("-").isdigit() else c)
    return out


def vip_topics() -> dict[str, dict[str, str]]:
    """Forum VIP groups read topic by topic: {"<chat id>": {"<topic id>": "<topic title>"}}.
    A chat that isn't here (or has no topics ticked) is read whole."""
    return {str(k): {str(t): str(n) for t, n in (v or {}).items()}
            for k, v in (_load().get("tg_vip_topics") or {}).items() if v}


def vip_open() -> set[str]:
    """VIP groups every customer gets (chat ids). The others are private: only customers an admin
    turned them on for, or who are in the group on their own Telegram."""
    return {str(c) for c in (_load().get("vip_open") or [])}


def bot_token() -> str:
    return _dec(_load().get("tg_bot_token_enc")) or config.TG_BOT_TOKEN


def bot_username() -> str:
    return _load().get("tg_bot_username") or ""


def public_url() -> str:
    return (_load().get("public_url") or "").rstrip("/")

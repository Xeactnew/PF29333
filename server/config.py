"""PipsFarmer server configuration.

Values come from environment variables, or from a `.env` file next to the
project root (one KEY=value per line). See `.env.example`.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env_file() -> None:
    env_path = Path(os.environ.get("PF_ENV_FILE", ROOT / ".env"))
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_env_file()


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _list(name: str) -> list[str]:
    return [v.strip() for v in _get(name).split(",") if v.strip()]


# --- core ---
SITE_NAME = _get("PF_SITE_NAME", "PipsFarmer")
BASE_URL = _get("PF_BASE_URL", "http://localhost:8000").rstrip("/")
SECRET_KEY = _get("PF_SECRET_KEY", "")            # signs login cookies
FERNET_KEY = _get("PF_FERNET_KEY", "")            # encrypts MT5 + Telegram secrets
DB_PATH = _get("PF_DB_PATH", str(ROOT / "data" / "pipsfarmer.db"))
HTTPS_ONLY_COOKIES = _get("PF_HTTPS_ONLY", "0") == "1"
HOST = _get("PF_HOST", "127.0.0.1")
PORT = int(_get("PF_PORT", "8000"))

# --- telegram (reader account that sits in the PipsFarmer VIP group) ---
TG_API_ID = int(_get("TG_API_ID", "0") or 0)
TG_API_HASH = _get("TG_API_HASH", "")
TG_MAIN_SESSION = _get("TG_MAIN_SESSION", "")     # StringSession from tools/telegram_login.py
TG_VIP_CHATS = _list("TG_VIP_CHATS")              # @username or numeric id of PipsFarmer VIP

# --- telegram admin bot (renewal questions, VPS alerts) ---
TG_BOT_TOKEN = _get("TG_BOT_TOKEN", "")
TG_ADMIN_CHAT_IDS = [int(x) for x in _list("TG_ADMIN_CHAT_IDS")]

# --- trading defaults ---
GOLD_PIP = float(_get("PF_GOLD_PIP", "0.1"))       # 1 pip on gold = $0.10 move
NEWS_URL = _get("PF_NEWS_URL", "https://nfs.faireconomy.media/ff_calendar_thisweek.json")
NODE_OFFLINE_SECONDS = int(_get("PF_NODE_OFFLINE_SECONDS", "90"))

# turn background tasks off for tests
DISABLE_BACKGROUND = _get("PF_DISABLE_BACKGROUND", "0") == "1"


def check() -> list[str]:
    """Return a list of problems with the configuration."""
    problems = []
    if len(SECRET_KEY) < 32:
        problems.append("PF_SECRET_KEY is missing or too short (run tools/make_keys.py)")
    if not FERNET_KEY:
        problems.append("PF_FERNET_KEY is missing (run tools/make_keys.py)")
    return problems

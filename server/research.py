"""Research data for backtesting the signal groups (read-only; changes nothing about trading).

* Telegram: the full recent history of every ticked VIP group (time, text, reply, last edit) →
  data/research/messages-<chat>.jsonl
* Prices: one MT5 worker reads M1 gold candles from its history (mt5.copy_rates_range: reading, never
  trading) and posts them in weekly chunks → data/research/prices-acc<id>.csv

Claude stages these files from the VPS and replays every call against real prices to find rules that pay.
Runs once by itself after install (setting research_v1) and again from Admin → System.
"""
from __future__ import annotations

import asyncio
import csv
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from . import config, runtime, services
from .db import Account, SessionLocal, get_setting, set_setting

log = logging.getLogger("pf.research")
DIR = config.ROOT / "data" / "research"
DAYS = 90
_state = {"messages": "", "prices": ""}


def status() -> dict:
    files = sorted(p.name for p in DIR.glob("*")) if DIR.exists() else []
    return {**_state, "files": files}


# ------------------------------------------------------------ Telegram history

async def export_messages(days: int = DAYS) -> int:
    """Writes each VIP group's messages from the last `days` days. Needs the reader connected."""
    from . import telegram_service
    client = telegram_service._main.get("client")
    if client is None or not await client.is_user_authorized():
        _state["messages"] = "waiting: the Telegram reader isn't connected"
        return 0
    DIR.mkdir(parents=True, exist_ok=True)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    total = 0
    for chat in runtime.vip_chats():
        n, path = 0, DIR / f"messages-{chat}.jsonl"
        try:
            title = getattr(await client.get_entity(chat), "title", str(chat))
            with path.open("w", encoding="utf-8") as f:
                async for m in client.iter_messages(chat, offset_date=None):
                    if m.date < since:
                        break
                    f.write(json.dumps({
                        "chat": chat, "title": title, "id": m.id, "date": m.date.isoformat(),
                        "edit_date": m.edit_date.isoformat() if m.edit_date else None,
                        "reply_to": m.reply_to.reply_to_msg_id if m.reply_to else None,
                        "text": m.message or ""}, ensure_ascii=False) + "\n")
                    n += 1
                    if n % 200 == 0:
                        await asyncio.sleep(1)          # gentle on Telegram's limits
        except Exception as e:  # noqa: BLE001
            log.warning("research export of %s: %s", chat, e)
        total += n
        log.info("research: %s messages from %s", n, chat)
    _state["messages"] = f"{total} messages from {len(runtime.vip_chats())} groups, last {days} days"
    return total


# ------------------------------------------------------------ prices from MT5 (via a worker)

def request_prices(days: int = DAYS) -> int | None:
    """Asks one online MT5 worker to send its M1 gold history. Returns the account id asked."""
    with SessionLocal() as s:
        accs = list(s.scalars(select(Account).where(Account.conn_status == "online", Account.removed.is_(False))))
        if not accs:
            _state["prices"] = "waiting: no MT5 account is online"
            return None
        acc = max(accs, key=lambda a: a.id)
        services.command(s, acc.id, "export_prices", days=days)
        s.commit()
    _state["prices"] = f"asked account {acc.id} for {days} days of M1 candles"
    return acc.id


def save_prices(account_id: int, symbol: str, rows: list[list]) -> int:
    """Appends candles a worker sent: [time, open, high, low, close, tick_volume, spread]."""
    DIR.mkdir(parents=True, exist_ok=True)
    path = DIR / f"prices-acc{account_id}.csv"
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "open", "high", "low", "close", "tick_volume", "spread", "symbol"])
        for r in rows:
            w.writerow([int(r[0]), *r[1:7], symbol])
    _state["prices"] = f"{path.name}: candles up to {datetime.fromtimestamp(max(int(r[0]) for r in rows), timezone.utc):%d %b %H:%M}" if rows else _state["prices"]
    return len(rows)


# ------------------------------------------------------------ once after install

async def run_once_after_install() -> None:
    await asyncio.sleep(240)          # let the reader and the workers connect first
    with SessionLocal() as s:
        if get_setting(s, "research_v1"):
            return
    try:
        if request_prices() is not None:
            n = await export_messages()
            if n:
                with SessionLocal() as s:
                    set_setting(s, "research_v1", time.time())
                    s.commit()
    except Exception:  # noqa: BLE001
        log.exception("research export")

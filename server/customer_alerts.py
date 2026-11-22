"""Trade alerts for customers, sent by the Telegram bot.

Built from what the MT5 workers already report to server/api.py:
* new deals (worker_deals): a trade opened (one message per signal, not per layer), partials, a trade closed
* the report (worker_poll): new notes (stop to breakeven, signal skipped) and the account's health
  (MT5 can't connect, wrong password, Algo Trading off), with a short grace period so blips don't alert.

Every alert has a unique key, so nothing is ever queued twice. The bot sends the queue a few at a time
per chat, retries failures with a growing wait and gives up on alerts that are hours old.
"""
from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import func, select

from . import bot_reports, config
from .db import Account, CustomerAlert, Deal, Signal, User, utcnow

log = logging.getLogger("pf.alerts")

KINDS = ("open", "close", "manage", "problems", "daily")
DEFAULT_PREFS = {"open": True, "close": True, "manage": True, "problems": True, "daily": False,
                 "daily_time": "21:00", "tz_offset": 0}     # tz_offset: browser getTimezoneOffset() (minutes)
HOURLY_CAP = 40               # most alerts one customer gets in an hour; the rest are dropped, not delayed
FRESH = timedelta(minutes=20)  # only deals this recent alert (a new account's history doesn't)
GIVE_UP = timedelta(hours=6)   # an alert still unsent after this is stale
MAX_TRIES = 6
PROBLEM_DELAY = {"password": 0, "algo": 120, "mt5": 300, "broker": 1500}   # broker: the VPS looks it up first   # seconds a problem must last before we say so
SKIP_QUIET = ("same trade as signal", "same idea as signal")  # the same call twice isn't news

_codes: dict[str, tuple[int, float]] = {}     # bot /start code -> (user_id, expires)


# ------------------------------------------------------------ linking and preferences

def new_link_code(user_id: int) -> str:
    import secrets
    for c, (_, exp) in list(_codes.items()):
        if exp < time.time():
            _codes.pop(c, None)
    code = "c" + secrets.token_urlsafe(18).replace("-", "x").replace("_", "y")
    _codes[code] = (user_id, time.time() + 3600)
    return code


def is_customer_code(code: str) -> bool:
    return code.startswith("c") and code in _codes


def link_chat(s, code: str, chat_id: int) -> User | None:
    item = _codes.pop(code, None)
    if not item or item[1] < time.time():
        return None
    user = s.get(User, item[0])
    if not user or user.status == "closed":
        return None
    for other in s.scalars(select(User).where(User.alert_chat_id == chat_id, User.id != user.id)):
        other.alert_chat_id = None              # one Telegram chat follows one login
    user.alert_chat_id = chat_id
    return user


def unlink_chat(s, chat_id: int) -> int:
    n = 0
    for u in s.scalars(select(User).where(User.alert_chat_id == chat_id)):
        u.alert_chat_id = None
        n += 1
    return n


def prefs(user: User) -> dict:
    return {**DEFAULT_PREFS, **(user.alert_prefs or {})}


def clean_prefs(form) -> dict:
    out = {k: form.get(k) == "on" for k in KINDS}
    t = str(form.get("daily_time", "") or "21:00").strip()
    out["daily_time"] = t if re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", t) else "21:00"
    try:
        out["tz_offset"] = max(-840, min(840, int(form.get("tz_offset", 0) or 0)))
    except ValueError:
        out["tz_offset"] = 0
    return out


def daily_minute_utc(p: dict) -> int:
    h, m = (int(x) for x in p["daily_time"].split(":"))
    return (h * 60 + m + int(p.get("tz_offset") or 0)) % 1440


# ------------------------------------------------------------ queue

def queue(s, user: User | None, kind: str, key: str, text: str) -> bool:
    """Queue one alert. False when the customer isn't linked, turned this kind off, or it was queued before."""
    if not user or not user.alert_chat_id or user.status == "closed":
        return False
    if kind in KINDS and not prefs(user).get(kind):
        return False
    key = key[:128]
    if s.scalar(select(CustomerAlert.id).where(CustomerAlert.key == key)):
        return False
    hour_ago = utcnow() - timedelta(hours=1)
    recent = s.scalar(select(func.count(CustomerAlert.id)).where(
        CustomerAlert.user_id == user.id, CustomerAlert.created_at >= hour_ago)) or 0
    status = "queued" if kind == "test" or recent < HOURLY_CAP else "dropped"
    s.add(CustomerAlert(user_id=user.id, chat_id=user.alert_chat_id, key=key, kind=kind, text=text[:3900],
                        status=status))
    s.flush()
    if status == "queued":
        s.info["alerts_queued"] = s.info.get("alerts_queued", 0) + 1
    return status == "queued"


def due(s, limit: int = 25, per_chat: int = 3) -> list[CustomerAlert]:
    now = utcnow()
    for a in s.scalars(select(CustomerAlert).where(CustomerAlert.status == "queued",
                                                   CustomerAlert.created_at < now - GIVE_UP)):
        a.status, a.last_error = "failed", "too old to send"
    rows = s.scalars(select(CustomerAlert).where(CustomerAlert.status == "queued", CustomerAlert.next_try <= now)
                     .order_by(CustomerAlert.id).limit(limit * 4))
    out, per = [], defaultdict(int)
    for a in rows:
        if per[a.chat_id] >= per_chat:
            continue
        per[a.chat_id] += 1
        out.append(a)
        if len(out) >= limit:
            break
    return out


GONE = ("blocked by the user", "chat not found", "user is deactivated", "bot can't initiate")


def mark(s, alert_id: int, error: str | None) -> None:
    a = s.get(CustomerAlert, alert_id)
    if not a:
        return
    if error is None:
        a.status, a.sent_at = "sent", utcnow()
        return
    a.tries += 1
    a.last_error = error[:300]
    if any(g in error.lower() for g in GONE):
        a.status = "failed"                     # they blocked the bot: stop sending to that chat
        unlink_chat(s, a.chat_id)
        return
    if a.tries >= MAX_TRIES:
        a.status = "failed"
    else:
        a.next_try = utcnow() + timedelta(seconds=30 * 2 ** a.tries)


# ------------------------------------------------------------ wording

def _acc_name(a: Account) -> str:
    return a.label or a.login


def _price(v) -> str:
    try:
        return f"{float(v):,.2f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return "–"


def _group(sig: Signal | None) -> str:
    if not sig:
        return "your signal group"
    return sig.channel or (sig.source.title if sig.source else "") or "your signal group"


def _today(s, a: Account) -> str:
    p = bot_reports.account_pnl(s, a, "today", utcnow())
    return bot_reports.pnl(p["total"], a.currency)


# ------------------------------------------------------------ from deals

def _broker_now(a: Account) -> datetime:
    return utcnow() + bot_reports.broker_offset(a)


def _live_sids(a: Account) -> set[str]:
    ls = a.live_state or {}
    return {str(x.get("signal_id")) for k in ("positions", "pendings", "waiting") for x in (ls.get(k) or [])
            if x.get("signal_id") is not None}


def _set_state(a: Account, st: dict) -> None:
    a.alert_state = dict(st)        # JSON columns only save when reassigned


def on_deals(s, a: Account, new: list[Deal]) -> None:
    """New deals just stored for this account."""
    user = a.user
    st = dict(a.alert_state or {})
    track = dict(st.get("track") or {})
    now_b = _broker_now(a)
    by_sig_in, by_sig_out = defaultdict(list), defaultdict(list)
    for d in new:
        if d.signal_id is None or d.entry == "balance":
            continue
        fresh = now_b - d.time < FRESH
        if d.entry == "in":
            track[str(d.signal_id)] = time.time()
            if fresh:
                by_sig_in[d.signal_id].append(d)
        elif d.entry in ("out", "inout") and fresh:
            by_sig_out[d.signal_id].append(d)
    live = _live_sids(a)
    for sid, deals in by_sig_in.items():
        sig = s.get(Signal, sid)
        lots = round(sum(d.volume for d in deals), 2)
        avg = sum(d.price * d.volume for d in deals) / (sum(d.volume for d in deals) or 1)
        side = deals[0].side.upper()
        lines = [f"🟢 {side} gold · {_group(sig)}",
                 f"{lots:g} lots at {_price(avg)}" + (f" ({len(deals)} entries)" if len(deals) > 1 else "")]
        if sig:
            lv = []
            if sig.sl:
                lv.append(f"SL {_price(sig.sl)}")
            if sig.tps:
                lv.append("TP " + " / ".join(_price(t) for t in sig.tps))
            if lv:
                lines.append(" · ".join(lv))
        lines.append(f"Account {_acc_name(a)}")
        queue(s, user, "open", f"open:{a.id}:{sid}", "\n".join(lines))
    for sid, deals in by_sig_out.items():
        if str(sid) in live:
            sig = s.get(Signal, sid)
            got = sum(d.profit for d in deals)
            lots = round(sum(d.volume for d in deals), 2)
            queue(s, user, "manage", f"part:{a.id}:{sid}:{min(d.ticket for d in deals)}",
                  f"💰 Partial profit · {_group(sig)}\nClosed {lots:g} lots: {bot_reports.pnl(got, a.currency)}\n"
                  f"The rest stays open · {_acc_name(a)}")
    st["track"] = track
    _set_state(a, st)
    s.flush()
    check_closed(s, a)


def check_closed(s, a: Account) -> None:
    """A tracked signal has nothing left open on the account: say how it went (once)."""
    st = dict(a.alert_state or {})
    track = dict(st.get("track") or {})
    if not track:
        return
    live = _live_sids(a)
    now_b = _broker_now(a)
    for sid, since in list(track.items()):
        if sid in live:
            continue
        if time.time() - float(since) > 7 * 86400:
            track.pop(sid)
            continue
        deals = list(s.scalars(select(Deal).where(Deal.account_id == a.id, Deal.signal_id == int(sid))))
        outs = [d for d in deals if d.entry in ("out", "inout")]
        if not outs:
            continue                    # filled entries not synced as closed yet (or orders never filled)
        track.pop(sid)
        if now_b - max(d.time for d in outs) > FRESH * 2:
            continue                    # long ago: nothing to tell
        sig = s.get(Signal, int(sid))
        result = sum(d.profit for d in deals)
        icon = "✅" if result > 0 else "➖" if abs(result) < 0.005 else "🔻"
        side = (outs[0].side or (sig.side if sig else "")).upper()
        queue(s, a.user, "close", f"close:{a.id}:{sid}",
              f"{icon} {side} gold closed · {_group(sig)}\nResult {bot_reports.pnl(result, a.currency)}\n"
              f"Today so far {_today(s, a)} · {_acc_name(a)}")
    st["track"] = track
    _set_state(a, st)


# ------------------------------------------------------------ from the worker's report

NOTE_RE = re.compile(r"^\d{2} \w{3} \d{2}:\d{2} (.*)$")
BE_RE = re.compile(r"signal #(\d+): stop moved to breakeven(?: \+([\d.]+) pips)?")
SKIP_RE = re.compile(r"signal #(\d+) skipped: (.+)$")


def _problem(rep: dict) -> str | None:
    status, error = rep.get("status"), str(rep.get("error") or "")
    if status == "error":
        if rep.get("unknown_server"):
            return "broker"
        return "password" if "wrong login, password" in error else "mt5"
    if status == "starting" and "lost connection" in error:
        return "mt5"
    if "Algo Trading" in str(rep.get("blocked") or ""):
        return "algo"
    return None


PROBLEM_TEXT = {
    "password": "🔑 {acc}: MT5 won't log in. The login, password or server looks wrong. Update them on the "
                "account's page and it reconnects by itself.",
    "mt5": "⚠️ {acc}: MT5 isn't connected to your broker right now, so no new trades are being placed. "
           "We keep trying; open trades keep their stop and targets.",
    "algo": "⚠️ {acc}: Algo Trading is switched off in MT5, so trades can't be placed. We're switching it back on.",
    "broker": "🔎 {acc}: we still can't find your broker's server. Check the server name on the account's page "
              "is exactly as your broker shows it (for example PUPrime-Live); it connects by itself once it's right.",
}


def on_report(s, a: Account, rep: dict) -> None:
    st = dict(a.alert_state or {})
    user = a.user
    # notes: only the ones we haven't seen (the first report after an update just remembers them)
    notes = [str(n) for n in (rep.get("notes") or [])]
    seen = st.get("notes")
    if seen is None:
        st["notes"] = notes[:30]
    else:
        fresh = [n for n in notes if n not in set(seen)]
        for n in reversed(fresh):                       # oldest first
            m = NOTE_RE.match(n)
            text = m.group(1) if m else n
            be = BE_RE.search(text)
            if be:
                sig = s.get(Signal, int(be.group(1)))
                lock = f" + {be.group(2)} pips" if be.group(2) else ""
                queue(s, user, "manage", f"be:{a.id}:{be.group(1)}",
                      f"🛡 Stop moved to entry{lock} · {_group(sig)}\nThat trade is protected now · {_acc_name(a)}")
                continue
            sk = SKIP_RE.search(text)
            if sk and not any(q in sk.group(2) for q in SKIP_QUIET):
                sig = s.get(Signal, int(sk.group(1)))
                what = f"{sig.side.upper()} call from {_group(sig)}" if sig else f"Signal #{sk.group(1)}"
                queue(s, user, "problems", f"skip:{a.id}:{sk.group(1)}",
                      f"⏭ Not taken on {_acc_name(a)}\n{what}\nWhy: {sk.group(2)}")
        st["notes"] = (fresh + list(seen))[:30]
    # account health, with a grace period
    kind = _problem(rep)
    prob = dict(st.get("prob") or {})
    now = time.time()
    if kind:
        if prob.get("kind") != kind:
            prob = {"kind": kind, "since": now, "sent": False}
        if not prob.get("sent") and now - prob["since"] >= PROBLEM_DELAY[kind]:
            prob["sent"] = True
            queue(s, user, "problems", f"prob:{a.id}:{kind}:{int(prob['since'])}",
                  PROBLEM_TEXT[kind].format(acc=_acc_name(a)))
    elif prob:
        if prob.get("sent") and rep.get("status") == "online":
            queue(s, user, "problems", f"ok:{a.id}:{int(prob['since'])}",
                  f"✅ {_acc_name(a)} is connected again and copying.")
        prob = {} if rep.get("status") == "online" else prob
    st["prob"] = prob
    _set_state(a, st)
    check_closed(s, a)


# ------------------------------------------------------------ daily summary

def summary_text(s, user: User, now: datetime) -> str:
    lines = [f"📒 Your day · {now:%a %d %b}"]
    total: dict[str, float] = defaultdict(float)
    for a in user.accounts:
        if a.removed:
            continue
        p = bot_reports.account_pnl(s, a, "today", now)
        total[a.currency] += p["total"]
        trades = f"{p['trades']} trade{'s' if p['trades'] != 1 else ''}" if p["trades"] else "no trades closed"
        lines.append(f"{_acc_name(a)}: {bot_reports.pnl(p['total'], a.currency)} · {trades} · "
                     f"balance {bot_reports.money(a.balance, a.currency)}")
    if len(lines) == 1:
        return ""
    if len([a for a in user.accounts if not a.removed]) > 1:
        lines.append("Total " + ", ".join(bot_reports.pnl(v, c) for c, v in total.items()))
    return "\n".join(lines)


def daily_tick(s, now: datetime | None = None) -> int:
    now = now or utcnow()
    minute = now.hour * 60 + now.minute
    today = now.strftime("%Y-%m-%d")
    n = 0
    for u in s.scalars(select(User).where(User.alert_chat_id.isnot(None), User.status != "closed")):
        p = prefs(u)
        st = dict(u.alert_state or {})
        if not p["daily"] or st.get("daily") == today or minute < daily_minute_utc(p):
            continue
        st["daily"] = today
        u.alert_state = st
        text = summary_text(s, u, now)
        if text and queue(s, u, "daily", f"daily:{u.id}:{today}", text):
            n += 1
    return n


def test_text(user: User) -> str:
    return (f"👋 Hi {user.username}, alerts from {config.SITE_NAME} reach you here. "
            "Change what you get on the website under Plan & login. Send /stop to turn them off.")

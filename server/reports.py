"""Account and signal reports for admins: the Accounts page, the signals log (page, Excel, CSV files)
and the bot. One place works the numbers out so every view agrees."""
from __future__ import annotations

import csv
import io
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select

from . import config, xlsx
from .bot_reports import account_pnl, broker_offset
from .db import Account, AccountSource, Deal, Signal, SignalUpdate, Source, User, utcnow
from .parser import parse_signal

SIGNALS_DIR = config.ROOT / "signals"
PERIODS = ("today", "week", "month", "all")
UPDATE_NAMES = {"tp_hit": "TP hit", "breakeven": "Breakeven", "close": "Close", "close_partial": "Close part",
                "sl_hit": "SL hit", "sl_move": "SL moved", "cancel": "Cancelled", "levels": "SL/TP set"}


# ------------------------------------------------------------ accounts

def groups_by_account(s) -> dict[int, list[str]]:
    out: dict[int, list[str]] = defaultdict(list)
    q = select(AccountSource.account_id, Source.title, Source.kind).join(Source, Source.id == AccountSource.source_id) \
        .where(Source.active.is_(True)).order_by(Source.kind != "main", Source.title)
    for acc_id, title, kind in s.execute(q):
        out[acc_id].append(title + (" (VIP)" if kind == "main" else "") if kind != "manual" else "Own manual signals")
    return out


def vip_groups(s) -> list[str]:
    """Names of the Telegram groups the VIP source reads (from the signals they've sent)."""
    from . import runtime, telegram_service
    names = []
    for cid in runtime.vip_chats():
        try:
            cid = int(cid)
        except (TypeError, ValueError):
            continue
        name = telegram_service._chat_titles.get(cid) or s.scalar(
            select(Signal.channel).where(Signal.tg_chat_id == cid, Signal.channel != "").order_by(Signal.id.desc()))
        names.append(name or str(cid))
    return names


def account_rows(s, now: datetime | None = None, term: str = "", status: str = "") -> list[dict]:
    now = now or utcnow()
    q = select(Account).where(Account.removed.is_(False)).order_by(Account.id)
    accs = list(s.scalars(q))
    groups = groups_by_account(s)
    rows = []
    for a in accs:
        u: User | None = a.user
        if term and term.lower() not in " ".join([a.login, a.server, a.label or "", u.username if u else "",
                                                   u.email if u else ""]).lower():
            continue
        if status and a.conn_status != status:
            continue
        ls = a.live_state or {}
        ours, others = list(ls.get("positions") or []), list(ls.get("other_positions") or [])
        rows.append({
            "account": a, "id": a.id, "user": u.username if u else "?", "user_id": a.user_id,
            "plan": u.plan.name if u and u.plan else "?", "login": a.login, "server": a.server, "broker": a.broker,
            "status": a.conn_status, "error": a.conn_error, "copy": a.copy_enabled, "cur": a.currency,
            "balance": a.balance, "equity": a.equity,
            "floating": round(sum(float(p.get("profit") or 0) for p in ours + others), 2),
            "open": len(ours), "own_open": len(others),
            "pnl": {p: account_pnl(s, a, p, now) for p in PERIODS},
            "groups": groups.get(a.id, []), "last_report": a.last_report,
        })
    return rows


def totals(rows: list[dict]) -> list[dict]:
    by: dict[str, dict] = {}
    for r in rows:
        t = by.setdefault(r["cur"] or "?", {"cur": r["cur"] or "?", "accounts": 0, "balance": 0.0, "equity": 0.0,
                                             "floating": 0.0, **{p: 0.0 for p in PERIODS},
                                             **{f"{p}_copied": 0.0 for p in PERIODS}})
        t["accounts"] += 1
        for k in ("balance", "equity", "floating"):
            t[k] += r[k]
        for p in PERIODS:
            t[p] += r["pnl"][p]["total"]
            t[f"{p}_copied"] += r["pnl"][p]["copied"]
    return list(by.values())


def accounts_xlsx(s) -> bytes:
    rows = account_rows(s)
    head = ["Customer", "Plan", "Login", "Server", "Broker", "Status", "Copying", "Currency", "Balance", "Equity",
            "Open P&L", "PipsFarmer trades open", "Own trades open", "Today", "Today copied", "7 days",
            "7 days copied", "30 days", "30 days copied", "All time", "All time copied", "Closed trades (all)",
            "Groups", "Problem", "Last update (UTC)"]
    out = [head]
    for r in rows:
        out.append([r["user"], r["plan"], r["login"], r["server"], r["broker"], r["status"], r["copy"], r["cur"],
                    r["balance"], r["equity"], r["floating"], r["open"], r["own_open"],
                    *[round(r["pnl"][p][k], 2) for p in PERIODS for k in ("total", "copied")],
                    r["pnl"]["all"]["trades"], ", ".join(r["groups"]), r["error"], r["last_report"]])
    tot = [["Currency", "Accounts", "Balance", "Equity", "Open P&L", "Today", "7 days", "30 days", "All time"]]
    for t in totals(rows):
        tot.append([t["cur"], t["accounts"], round(t["balance"], 2), round(t["equity"], 2), round(t["floating"], 2),
                    *[round(t[p], 2) for p in PERIODS]])
    return xlsx.workbook([("Accounts", out, [16, 10, 12, 20, 18, 9, 8, 8, 11, 11, 10, 9, 9, 10, 10, 10, 10, 10, 10,
                                             10, 10, 9, 40, 40, 17]),
                          ("Totals", tot, [10, 9, 12, 12, 11, 11, 11, 11, 11])])


# ------------------------------------------------------------ signals log

def _channel(sig: Signal, sources: dict[int, Source]) -> str:
    if sig.channel:
        return sig.channel
    src = sources.get(sig.source_id)
    return src.title if src else "?"


def _entry_text(sig: Signal) -> str:
    if not sig.entry_low:
        return "market (now)"
    if sig.entry_low == sig.entry_high:
        return f"{sig.entry_low:g}"
    return f"{sig.entry_low:g}-{sig.entry_high:g}"


def signal_rows(s, channel: str = "", taken_only: bool = False, limit: int | None = None,
                since: datetime | None = None) -> list[dict]:
    sources = {x.id: x for x in s.scalars(select(Source))}
    q = select(Signal).order_by(Signal.id.desc())
    if since:
        q = q.where(Signal.created_at >= since)
    sigs = list(s.scalars(q))
    ids = [x.id for x in sigs] or [-1]
    upd: dict[int, list[SignalUpdate]] = defaultdict(list)
    for u in s.scalars(select(SignalUpdate).where(SignalUpdate.signal_id.in_(ids)).order_by(SignalUpdate.id)):
        upd[u.signal_id].append(u)
    accounts = {a.id: a for a in s.scalars(select(Account))}
    per: dict[int, dict[int, dict]] = defaultdict(dict)       # signal -> account -> numbers
    for d in s.scalars(select(Deal).where(Deal.signal_id.in_(ids))):
        row = per[d.signal_id].setdefault(d.account_id, {"lots": 0.0, "out": 0.0, "pnl": 0.0, "closed": 0,
                                                          "first": d.time})
        if d.entry == "in":
            row["lots"] += d.volume
            row["first"] = min(row["first"], d.time)
        else:
            row["closed"] += 1
            row["out"] += d.volume
        row["pnl"] += d.profit or 0
    # one Telegram post that reached us through two sources is one signal in the log
    first: dict[tuple, int] = {}
    for sig in sigs:
        if sig.tg_chat_id is not None and sig.tg_msg_id:
            k = (sig.tg_chat_id, sig.tg_msg_id)
            first[k] = min(first.get(k, sig.id), sig.id)
    hidden = set()
    for sig in sigs:
        k = (sig.tg_chat_id, sig.tg_msg_id)
        main = first.get(k) if sig.tg_chat_id is not None and sig.tg_msg_id else None
        if main and main != sig.id:
            hidden.add(sig.id)
            for acc_id, row in per.pop(sig.id, {}).items():
                per[main].setdefault(acc_id, row)
            upd[main] = upd[main] or upd[sig.id]
    out = []
    for sig in sigs:
        if sig.id in hidden:
            continue
        ch = _channel(sig, sources)
        if channel and ch != channel:
            continue
        took = per.get(sig.id, {})
        if taken_only and not took:
            continue
        src = sources.get(sig.source_id)
        orig = parse_signal(sig.raw_text or "")
        orig_sl = orig.sl if orig and orig.side == sig.side else None
        changes = [(u.created_at, u.value, u.created_by) for u in upd[sig.id] if u.kind in ("sl_move", "levels") and u.value]
        if orig_sl is None and not changes:
            orig_sl = sig.sl
        pnl_by_cur: dict[str, float] = defaultdict(float)
        acc_lines = []
        for acc_id, row in sorted(took.items()):
            a = accounts.get(acc_id)
            cur = a.currency if a else ""
            pnl_by_cur[cur] += row["pnl"]
            who = f"{a.user.username if a and a.user else '?'} {a.login if a else acc_id}"
            acc_lines.append(f"{who}: {row['lots']:g} lots, {row['pnl']:+.2f} {cur}".strip())
        out.append({
            "signal": sig, "id": sig.id, "time": sig.created_at, "channel": ch,
            "type": "VIP group" if src and src.kind == "main" else
                    "Customer channel" if src and src.kind == "channel" else
                    "Manual" if sig.created_by != "telegram" else "?",
            "owner": (s.get(User, src.owner_user_id).username if src and src.owner_user_id and s.get(User, src.owner_user_id) else ""),
            "side": sig.side, "entry": _entry_text(sig), "orig_sl": orig_sl, "sl": sig.sl, "tps": list(sig.tps or []),
            "tp_reached": sig.tp_reached, "status": sig.status,
            "sl_changes": changes,
            "updates": [(u.created_at, UPDATE_NAMES.get(u.kind, u.kind), u.value) for u in upd[sig.id]],
            "accounts": len(took), "account_lines": acc_lines,
            "lots": round(sum(r["lots"] for r in took.values()), 2),
            # finished = every lot that was opened for this signal has been closed again
            "finished": bool(took) and all(r["out"] + 1e-9 >= r["lots"] > 0 for r in took.values()),
            "pnl": {k: round(v, 2) for k, v in pnl_by_cur.items()}, "raw": sig.raw_text or "",
        })
        if limit and len(out) >= limit:
            break
    return out


def channel_summary(rows: list[dict]) -> list[dict]:
    by: dict[str, dict] = {}
    for r in rows:
        c = by.setdefault(r["channel"], {"channel": r["channel"], "type": r["type"], "signals": 0, "taken": 0,
                                         "wins": 0, "losses": 0, "pnl": defaultdict(float), "last": r["time"]})
        c["signals"] += 1
        c["last"] = max(c["last"], r["time"])
        if r["accounts"]:
            c["taken"] += 1
            total = sum(r["pnl"].values())
            if r["finished"]:
                if total > 0:
                    c["wins"] += 1
                elif total < 0:
                    c["losses"] += 1
        for cur, v in r["pnl"].items():
            c["pnl"][cur] += v
    return sorted(by.values(), key=lambda c: c["last"], reverse=True)


def _fmt_changes(r: dict) -> str:
    how = {"telegram-edit": "edit", "telegram-reply": "reply", "telegram": "message"}
    return "\n".join(f"{t:%d %b %H:%M} → {v:g} ({how.get(b, b.split(':')[0])})" for t, v, b in r["sl_changes"])


def _fmt_pnl(p: dict) -> str:
    return ", ".join(f"{v:+.2f} {c}".strip() for c, v in p.items()) if p else ""


SIGNAL_HEAD = ["Signal #", "Received (UTC)", "Channel", "Type", "Side", "Entry", "Original SL", "Current SL",
               "SL changes", "TPs", "TPs reached", "Status", "Updates", "Accounts that took it", "Total lots",
               "Result", "Trades finished", "Who took it", "Message"]
SIGNAL_WIDTHS = [8, 16, 22, 15, 6, 13, 10, 10, 30, 22, 8, 9, 34, 10, 9, 14, 9, 40, 60]


def _signal_line(r: dict) -> list:
    return [r["id"], r["time"], r["channel"], r["type"] + (f" ({r['owner']})" if r["owner"] else ""),
            r["side"].upper(), r["entry"], r["orig_sl"], r["sl"], _fmt_changes(r),
            ", ".join(f"{x:g}" for x in r["tps"]), r["tp_reached"], r["status"],
            "\n".join(f"{t:%d %b %H:%M} {k}" + (f" {v:g}" if v and k not in ("Close part",) else "")
                      for t, k, v in r["updates"]),
            r["accounts"], r["lots"], _fmt_pnl(r["pnl"]), (r["finished"] if r["accounts"] else ""),
            "\n".join(r["account_lines"]), r["raw"][:2000]]


def signals_xlsx(s, taken_only: bool = False) -> bytes:
    rows = signal_rows(s, taken_only=taken_only)
    summary = [["Channel", "Type", "Signals", "Taken by at least one account", "Wins", "Losses", "Result",
                "Last signal (UTC)"]]
    for c in channel_summary(rows):
        summary.append([c["channel"], c["type"], c["signals"], c["taken"], c["wins"], c["losses"],
                        _fmt_pnl({k: round(v, 2) for k, v in c["pnl"].items()}), c["last"]])
    sheets = [("Summary", summary, [26, 16, 9, 14, 7, 7, 16, 17]),
              ("All signals", [SIGNAL_HEAD] + [_signal_line(r) for r in rows], SIGNAL_WIDTHS)]
    for c in channel_summary(rows):
        sheets.append((c["channel"], [SIGNAL_HEAD] + [_signal_line(r) for r in rows if r["channel"] == c["channel"]],
                       SIGNAL_WIDTHS))
    return xlsx.workbook(sheets)


def _safe_file(name: str) -> str:
    return re.sub(r"[^\w\- ]+", "", name).strip()[:60] or "channel"


def write_signal_files(s, folder: Path | None = None) -> Path:
    """Keep the signals log on disk: one Excel workbook (a tab per channel) and a CSV per channel."""
    folder = folder or SIGNALS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    data = signals_xlsx(s)
    target = folder / "PipsFarmer signals.xlsx"
    tmp = target.with_suffix(".tmp")
    tmp.write_bytes(data)
    try:
        os.replace(tmp, target)
    except PermissionError:          # someone has it open in Excel: keep the old copy, write next to it
        os.replace(tmp, folder / "PipsFarmer signals (new).xlsx")
    rows = signal_rows(s)
    wanted = set()
    for c in channel_summary(rows):
        name = folder / f"{_safe_file(c['channel'])}.csv"
        wanted.add(name.name)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(SIGNAL_HEAD)
        for r in rows:
            if r["channel"] == c["channel"]:
                w.writerow([x.strftime("%Y-%m-%d %H:%M") if isinstance(x, datetime) else x for x in _signal_line(r)])
        try:
            name.write_text(buf.getvalue(), encoding="utf-8-sig")
        except PermissionError:
            pass
    return target


def log_fingerprint(s) -> tuple:
    return (s.scalar(select(func.max(Signal.id))), s.scalar(select(func.max(SignalUpdate.id))),
            s.scalar(select(func.max(Deal.id))), s.scalar(select(func.count(Signal.id))))

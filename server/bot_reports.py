"""Account reports for the admin Telegram bot: account sizes, P&L and open positions.

Every function returns plain text (Telegram messages are split at 4096 characters by the caller).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import case, func, or_, select

from .db import Account, Deal, User
from .db import utcnow as _db_now

PERIODS = {"today": "Today", "week": "Last 7 days", "month": "Last 30 days", "all": "All time"}
STALE = timedelta(minutes=3)

HELP = (
    "📊 PipsFarmer reports\n\n"
    "/accounts: every account, its size and today's P&L\n"
    "/pnl: closed P&L today (or /pnl week, /pnl month, /pnl all)\n"
    "/positions: every open trade right now\n"
    "/account <login or username>: one account in detail\n"
    "/signals: the signals log as an Excel file (a tab per channel)\n\n"
    "Copied = trades PipsFarmer placed. Total = everything on the account, "
    "including trades the customer placed themselves. \"Today\" is the broker's trading day, like in MT5."
)

STATUS_ICON = {"online": "🟢", "starting": "🟡", "waiting": "⏳", "error": "🔴", "stopped": "⚪", "new": "⚪"}


def utcnow() -> datetime:
    return _db_now()


def money(v: float, cur: str = "") -> str:
    sign = "-" if v < 0 else ""
    sym = {"GBP": "£", "USD": "$", "EUR": "€"}.get(cur, "")
    return f"{sign}{sym}{abs(v):,.2f}" + ("" if sym or not cur else f" {cur}")


def pnl(v: float, cur: str = "") -> str:
    return ("+" if v > 0 else "") + money(v, cur)


def period_start(period: str, now: datetime | None = None) -> datetime | None:
    now = now or utcnow()
    if period == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        return now - timedelta(days=7)
    if period == "month":
        return now - timedelta(days=30)
    return None


def _accounts(s) -> list[Account]:
    return list(s.scalars(select(Account).where(Account.removed.is_(False)).order_by(Account.id)))


def _name(a: Account) -> str:
    who = a.user.username if a.user else "?"
    return f"{who} · {a.login} @ {a.server}"


def _open(a: Account) -> tuple[list[dict], list[dict]]:
    ls = a.live_state or {}
    return list(ls.get("positions") or []), list(ls.get("other_positions") or [])


def _floating(a: Account) -> float:
    ours, others = _open(a)
    return sum(float(p.get("profit") or 0) for p in ours + others)


def closed_pnl(s, account_ids: list[int], since: datetime | None) -> dict[int, dict[str, float]]:
    """{account_id: {"copied": x, "total": y, "trades": n}} from closed deals (deposits excluded)."""
    q = select(Deal.account_id, _copied(Deal), func.sum(Deal.profit),
               func.sum(case((or_(Deal.entry == "out", Deal.entry == "inout"), 1), else_=0))) \
        .where(Deal.account_id.in_(account_ids or [-1]), Deal.entry != "balance") \
        .group_by(Deal.account_id, _copied(Deal))
    if since is not None:
        q = q.where(Deal.time >= since)
    out: dict[int, dict[str, float]] = defaultdict(lambda: {"copied": 0.0, "total": 0.0, "trades": 0})
    for acc_id, ours, total, closes in s.execute(q):
        out[acc_id]["total"] += total or 0
        out[acc_id]["trades"] += int(closes or 0)
        if ours:
            out[acc_id]["copied"] += total or 0
    return out


def _copied(deal_cls):
    """A PipsFarmer trade: opened by us, or closed later (by hand or by the broker) but linked to a signal."""
    return or_(deal_cls.ours.is_(True), deal_cls.signal_id.isnot(None))


def broker_offset(a: Account) -> timedelta:
    """MT5 deal times are in the broker's server time (often UTC+2/+3). Work out the gap from the last report."""
    st = (a.live_state or {}).get("server_time")
    if not st or not a.last_report:
        return timedelta(0)
    from datetime import timezone
    gap = float(st) - a.last_report.replace(tzinfo=timezone.utc).timestamp()
    hours = round(gap / 3600)
    return timedelta(hours=hours) if -14 <= hours <= 14 else timedelta(0)


def account_pnl(s, a: Account, period: str, now: datetime) -> dict:
    since = period_start(period, now + broker_offset(a))
    return closed_pnl(s, [a.id], since).get(a.id, {"copied": 0.0, "total": 0.0, "trades": 0})


def _live(a: Account, now: datetime) -> bool:
    return a.conn_status == "online" and a.last_report is not None and now - a.last_report < STALE


def accounts_report(s, now: datetime | None = None) -> str:
    now = now or utcnow()
    accs = _accounts(s)
    if not accs:
        return "No customer accounts yet."
    lines = [f"👥 Accounts ({len(accs)})", ""]
    sizes: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
    for a in accs:
        icon = STATUS_ICON.get(a.conn_status, "⚪")
        ours, others = _open(a)
        lines.append(f"{icon} {_name(a)}")
        if a.balance or a.equity:
            cur = a.currency
            lines.append(f"   Balance {money(a.balance, cur)} · Equity {money(a.equity, cur)}")
            day = account_pnl(s, a, "today", now)["total"]
            lines.append(f"   Today {pnl(day, cur)} closed · {pnl(_floating(a), cur)} open"
                         f" · {len(ours) + len(others)} open trade{'s' if len(ours) + len(others) != 1 else ''}")
            tot = sizes[cur]
            tot[0] += a.balance
            tot[1] += a.equity
            tot[2] += day
        if a.conn_status != "online" or not _live(a, now):
            why = a.conn_error[:120] if a.conn_error else ("no report for a few minutes" if a.conn_status == "online"
                                                            else a.conn_status)
            lines.append(f"   ⚠️ {why}")
        lines.append("")
    if sizes:
        lines.append("Totals")
        for cur, (bal, eq, day) in sorted(sizes.items()):
            lines.append(f"   {cur or '?'}: balance {money(bal, cur)} · equity {money(eq, cur)} · today {pnl(day, cur)}")
    return "\n".join(lines).strip()


def pnl_report(s, period: str = "today", now: datetime | None = None) -> str:
    period = period if period in PERIODS else "today"
    now = now or utcnow()
    accs = _accounts(s)
    if not accs:
        return "No customer accounts yet."
    data = {a.id: account_pnl(s, a, period, now) for a in accs}
    lines = [f"💰 Closed P&L: {PERIODS[period]}", ""]
    totals: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0])
    rows = sorted(accs, key=lambda a: data.get(a.id, {"total": 0})["total"], reverse=True)
    for a in rows:
        d = data.get(a.id, {"copied": 0.0, "total": 0.0, "trades": 0})
        cur = a.currency
        pct = f" ({d['total'] / a.balance * 100:+.2f}%)" if a.balance else ""
        lines.append(f"{'📈' if d['total'] > 0 else '📉' if d['total'] < 0 else '➖'} {_name(a)}")
        lines.append(f"   Total {pnl(d['total'], cur)}{pct} · Copied {pnl(d['copied'], cur)} · {d['trades']} closed")
        t = totals[cur]
        t[0] += d["total"]
        t[1] += d["copied"]
        t[2] += d["trades"]
    lines.append("")
    for cur, (tot, cop, n) in sorted(totals.items()):
        lines.append(f"Total {cur or '?'}: {pnl(tot, cur)} · Copied {pnl(cop, cur)} · {int(n)} closed trades")
    return "\n".join(lines).strip()


def _pos_line(p: dict, cur: str, ours: bool) -> str:
    sl = f" SL {p['sl']:g}" if p.get("sl") else ""
    tp = f" TP {p['tp']:g}" if p.get("tp") else ""
    tag = "" if ours else " (own trade)"
    return (f"   {p.get('side', '?').upper()} {p.get('volume', 0):g} {p.get('symbol', '')} @ {p.get('price', 0):g}"
            f"{sl}{tp} → {pnl(float(p.get('profit') or 0), cur)}{tag}")


def positions_report(s, now: datetime | None = None) -> str:
    now = now or utcnow()
    accs = _accounts(s)
    lines, count = ["📌 Open trades", ""], 0
    floating: dict[str, float] = defaultdict(float)
    for a in accs:
        ours, others = _open(a)
        if not ours and not others:
            continue
        lines.append(f"{_name(a)}" + ("" if _live(a, now) else " (not live right now)"))
        for p in ours:
            lines.append(_pos_line(p, a.currency, True))
        for p in others:
            lines.append(_pos_line(p, a.currency, False))
        count += len(ours) + len(others)
        floating[a.currency] += _floating(a)
        lines.append("")
    if not count:
        return "📌 No open trades right now."
    lines.append(f"{count} open trade{'s' if count != 1 else ''} · floating " +
                 ", ".join(pnl(v, c) for c, v in sorted(floating.items())))
    return "\n".join(lines).strip()


def find_account(s, query: str) -> list[Account]:
    q = query.strip().lstrip("@")
    if not q:
        return []
    accs = _accounts(s)
    exact = [a for a in accs if a.login == q or (a.user and a.user.username.lower() == q.lower())]
    if exact:
        return exact
    ql = q.lower()
    return [a for a in accs if ql in a.login or (a.user and ql in a.user.username.lower())
            or ql in (a.label or "").lower()]


def account_report(s, query: str, now: datetime | None = None) -> str:
    now = now or utcnow()
    found = find_account(s, query)
    if not found:
        return f"No account matches “{query}”. Send /accounts to see them all."
    if len(found) > 1 and not all(a.user_id == found[0].user_id for a in found):
        return "More than one match:\n" + "\n".join(f"• {_name(a)}" for a in found[:15]) + \
            "\n\nSend /account <login> for one of them."
    out = []
    for a in found[:5]:
        cur = a.currency
        u: User | None = a.user
        plan = u.plan.name if u and u.plan else "?"
        from .reports import groups_by_account
        groups = groups_by_account(s).get(a.id, [])
        lines = [f"{STATUS_ICON.get(a.conn_status, '⚪')} {_name(a)}",
                 f"Plan: {plan} · Copying: {'on' if a.copy_enabled else 'off'}",
                 f"Groups: {', '.join(groups) if groups else 'none'}"]
        if a.broker:
            lines.append(f"Broker: {a.broker}")
        lines.append(f"Balance {money(a.balance, cur)} · Equity {money(a.equity, cur)}")
        if a.conn_error:
            lines.append(f"⚠️ {a.conn_error[:200]}")
        if a.last_report:
            mins = int((now - a.last_report).total_seconds() // 60)
            lines.append(f"Last update: {'just now' if mins < 1 else f'{mins} min ago'}")
        lines.append("")
        lines.append("Closed P&L (total · copied · trades)")
        for per in ("today", "week", "month", "all"):
            d = account_pnl(s, a, per, now)
            lines.append(f"   {PERIODS[per]}: {pnl(d['total'], cur)} · {pnl(d['copied'], cur)} · {d['trades']}")
        ours, others = _open(a)
        lines.append("")
        if ours or others:
            lines.append(f"Open trades ({len(ours) + len(others)}, {pnl(_floating(a), cur)})")
            lines += [_pos_line(p, cur, True) for p in ours] + [_pos_line(p, cur, False) for p in others]
        else:
            lines.append("No open trades.")
        pend = (a.live_state or {}).get("pendings") or []
        if pend:
            lines.append(f"Pending orders: {len(pend)}")
        out.append("\n".join(lines))
    return "\n\n".join(out)


def split_message(text: str, limit: int = 4000) -> list[str]:
    """Split on blank lines / line breaks so no Telegram message is over the limit."""
    parts, cur = [], ""
    for block in text.split("\n"):
        add = block if not cur else cur + "\n" + block
        if len(add) > limit:
            if cur:
                parts.append(cur)
            while len(block) > limit:
                parts.append(block[:limit])
                block = block[limit:]
            cur = block
        else:
            cur = add
    if cur:
        parts.append(cur)
    return parts or [""]

"""Business logic shared by the website, Telegram service and scheduler."""
from __future__ import annotations

import calendar
from datetime import datetime, timedelta

from sqlalchemy import and_, false, func, or_, select

from . import config, notify, runtime
from .db import (Account, AccountSource, Activity, Deal, Event, Node, Plan, Renewal, Signal,
                 SignalUpdate, Snapshot, Source, User, get_setting, main_source, set_setting, utcnow)
from .parser import ParsedSignal
from .plans import effective_settings, feature


# ------------------------------------------------------------ helpers

def add_months(d: datetime, months: int = 1) -> datetime:
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return d.replace(year=y, month=m, day=day)


def log(s, action: str, user_id: int | None = None, detail: str = "", actor: str = "", ip: str = "") -> None:
    s.add(Activity(user_id=user_id, action=action, detail=detail[:2000], actor=actor, ip=ip))


def plan_features(user: User) -> dict:
    return user.plan.features or {}


def user_can_trade(user: User) -> bool:
    return user.status in ("active", "payment_check")


# ------------------------------------------------------------ sources

def ensure_default_sources(s, account: Account) -> None:
    """New accounts follow the PipsFarmer VIP group by default."""
    main = main_source(s)
    if not s.scalar(select(AccountSource).where(AccountSource.account_id == account.id,
                                                AccountSource.source_id == main.id)):
        s.add(AccountSource(account_id=account.id, source_id=main.id))


def available_sources(s, user: User) -> list[Source]:
    q = select(Source).where(Source.active.is_(True), or_(
        Source.kind == "main", and_(Source.kind == "channel", Source.owner_user_id == user.id)))
    return list(s.scalars(q.order_by(Source.kind != "main", Source.title)))


def account_source_ids(s, account_id: int) -> list[int]:
    return list(s.scalars(select(AccountSource.source_id).where(AccountSource.account_id == account_id)))


def set_account_sources(s, account: Account, source_ids: list[int]) -> None:
    allowed = {src.id for src in available_sources(s, account.user)}
    wanted = {i for i in source_ids if i in allowed}
    current = {a.source_id: a for a in s.scalars(select(AccountSource).where(AccountSource.account_id == account.id))}
    for sid, row in current.items():
        if sid not in wanted:
            s.delete(row)
    for sid in wanted - current.keys():
        s.add(AccountSource(account_id=account.id, source_id=sid))


def user_channel_count(s, user: User) -> int:
    return s.scalar(select(func.count()).select_from(Source).where(
        Source.kind == "channel", Source.owner_user_id == user.id, Source.active.is_(True))) or 0


# ------------------------------------------------------------ signals

def create_signal(s, source: Source, p: ParsedSignal, raw: str = "", tg_msg_id: int | None = None,
                  created_by: str = "telegram", target_account_ids: list[int] | None = None,
                  tg_chat_id: int | None = None, channel: str = "", topic_id: int | None = None,
                  grp: str = "") -> Signal:
    sig = Signal(source_id=source.id, symbol=p.symbol, side=p.side, order_type=p.order_type,
                 entry_low=p.entry_low, entry_high=p.entry_high, sl=p.sl, tps=list(p.tps),
                 raw_text=raw[:4000], tg_msg_id=tg_msg_id, created_by=created_by,
                 target_account_ids=list(target_account_ids or []), tg_chat_id=tg_chat_id,
                 channel=(channel or source.title)[:128], tg_topic_id=topic_id, grp=grp[:48])
    s.add(sig)
    s.flush()
    payload = {"signal": {"id": sig.id, **p.as_dict(), "source": sig.channel or source.title,
                          "source_id": source.id, "group": group_key(sig)}}
    rule = group_rule(s, sig)
    if rule:
        payload["signal"]["rule"] = {k: rule[k] for k in ("tp_target", "wait_for_zone") if k in rule}
        if rule.get("entry_hours"):
            payload["signal"]["entry_expiry_min"] = int(float(rule["entry_hours"]) * 60)
    if target_account_ids:
        for aid in target_account_ids:
            s.add(Event(kind="signal", account_id=aid, signal_id=sig.id, payload=payload))
    else:
        s.add(Event(kind="signal", source_id=source.id, signal_id=sig.id, payload=payload))
    return sig


def add_update(s, sig: Signal, kind: str, value: float | None = None, raw: str = "",
               created_by: str = "telegram", tps: list[float] | None = None,
               zone: tuple[float, float] | None = None, zone_moved: bool = False) -> SignalUpdate:
    upd = SignalUpdate(signal_id=sig.id, kind=kind, value=value, raw_text=raw[:2000], created_by=created_by)
    s.add(upd)
    if kind == "levels":            # the group posted (or edited in) the stop loss / targets
        if value:
            sig.sl = value
        if tps:
            sig.tps = list(tps)
        if zone and zone[0] and (zone_moved or not sig.entry_low):  # a quick call's zone, or an edited zone
            sig.entry_low, sig.entry_high = zone
    if kind == "tp_hit" and value:
        sig.tp_reached = max(sig.tp_reached, int(value) if value < 99 else len(sig.tps or []) or 3)
        if value >= 99 or (sig.tps and int(value) >= len(sig.tps)):
            sig.status = "closed"
    elif kind in ("close", "sl_hit"):
        sig.status = "closed"
    elif kind == "cancel":
        sig.status = "cancelled"
    elif kind == "sl_move" and value:
        sig.sl = value
    payload = {"update": {"signal_id": sig.id, "kind": kind, "value": value,
                          "force": created_by.startswith("user:")}}
    if tps:
        payload["update"]["tps"] = list(tps)
    if zone and zone[0]:
        payload["update"]["zone"] = [float(zone[0]), float(zone[1])]
        if zone_moved:
            payload["update"]["zone_moved"] = True
    rule = group_rule(s, sig)
    if rule and kind in ("breakeven", "sl_move", "close_worst"):
        payload["update"]["be_lock_pips"] = rule.get("be_lock_pips", 0)
        payload["update"]["sl_move_as_be"] = bool(rule.get("sl_move_as_be"))
    if sig.target_account_ids:
        for aid in sig.target_account_ids:
            s.add(Event(kind="update", account_id=aid, signal_id=sig.id, payload=payload))
    else:
        s.add(Event(kind="update", source_id=sig.source_id, signal_id=sig.id, payload=payload))
    return upd


def _same_chat(q, chat_id: int | None, topic_id: int | None = None):
    """Updates only ever reach signals from the same Telegram chat, and in a forum the same topic
    (so chat in "Discussion" can't move a trade from "Signals")."""
    if chat_id is not None:
        q = q.where(Signal.tg_chat_id == chat_id)
    if topic_id is not None:
        q = q.where(or_(Signal.tg_topic_id == topic_id, Signal.tg_topic_id.is_(None)))
    return q


def signal_waiting_for_levels(s, source_id: int, side: str | None = None, minutes: int = 30,
                              chat_id: int | None = None, topic_id: int | None = None) -> Signal | None:
    """The latest 'BUY NOW' style signal from this group that still has no stop loss."""
    since = utcnow() - timedelta(minutes=minutes)
    q = _same_chat(select(Signal).where(Signal.source_id == source_id, Signal.status == "open",
                                        Signal.sl.is_(None), Signal.created_at >= since), chat_id, topic_id)
    if side:
        q = q.where(Signal.side == side)
    return s.scalar(q.order_by(Signal.id.desc()))


def latest_open_signal(s, source_id: int, hours: int = 8, chat_id: int | None = None,
                       topic_id: int | None = None) -> Signal | None:
    """The newest open signal from this group (updates without a reply apply to it)."""
    since = utcnow() - timedelta(hours=hours)
    q = _same_chat(select(Signal).where(Signal.source_id == source_id, Signal.status == "open",
                                        Signal.created_at >= since), chat_id, topic_id)
    return s.scalar(q.order_by(Signal.id.desc()))


def signal_for_message(s, source_id: int, msg_id: int | None, chat_id: int | None = None) -> Signal | None:
    if not msg_id:
        return None
    return s.scalar(_same_chat(select(Signal).where(Signal.source_id == source_id, Signal.tg_msg_id == msg_id),
                               chat_id).order_by(Signal.id.desc()))


def command(s, account_id: int, cmd: str, **kw) -> None:
    s.add(Event(kind="command", account_id=account_id, payload={"command": {"name": cmd, **kw}}))


def events_for_account(s, account: Account, limit: int = 50) -> list[Event]:
    if account.event_cursor < 0:
        account.event_cursor = s.scalar(select(func.max(Event.id))) or 0
        return []
    src = account_source_ids(s, account.id)
    q = select(Event).where(Event.id > account.event_cursor, or_(
        Event.account_id == account.id,
        and_(Event.account_id.is_(None), Event.source_id.in_(src) if src else false())))
    events = list(s.scalars(q.order_by(Event.id).limit(limit)))
    main_id = main_source(s).id
    vip = [e for e in events if e.account_id is None and e.source_id == main_id and e.signal_id]
    if not vip:
        return events
    keys = vip_keys(s, account.user)
    sigs = {x.id: x for x in s.scalars(select(Signal).where(Signal.id.in_({e.signal_id for e in vip})))}
    out, delivered = [], False
    for e in events:
        if e in vip and not vip_ok(sigs.get(e.signal_id), keys):
            if not delivered:                       # nothing before it to deliver: never look at it again
                account.event_cursor = max(account.event_cursor, e.id)
            continue
        delivered = True
        out.append(e)
    return out


# ------------------------------------------------------------ private VIP groups

def vip_keys(s, user: User | None) -> set[str]:
    """The VIP groups (chat ids) this customer gets: open to everyone, turned on for them by an admin,
    or a group they're in on their own Telegram."""
    vip = {str(c) for c in runtime.vip_chats()}
    keys = set(runtime.vip_open())
    if user is not None:
        keys |= {str(c) for c in (user.vip_grants or [])}
        keys |= {str(c) for c in (user.tg_member_chats or [])} & vip
    return keys


def vip_ok(sig: Signal | None, keys: set[str]) -> bool:
    """May a customer with these VIP groups see / copy this VIP signal? Admin's own manual VIP signals
    (no Telegram chat) go to everyone."""
    if sig is None or sig.tg_chat_id is None:
        return True
    return str(sig.tg_chat_id) in keys or group_key(sig) in keys


def visible_signal(s, user: User, sig: Signal, keys: set[str] | None = None, main_id: int | None = None) -> bool:
    if sig.source_id != (main_id if main_id is not None else main_source(s).id):
        return True
    return vip_ok(sig, vip_keys(s, user) if keys is None else keys)


# ------------------------------------------------------------ VPS capacity

def node_online(node: Node) -> bool:
    return bool(node.enabled and node.last_seen and
                (utcnow() - node.last_seen).total_seconds() < config.NODE_OFFLINE_SECONDS)


RAM_FULL = 90          # % memory in use: a VPS this full takes no new MT5s, whatever its slot limit


def memory_full(node: Node) -> bool:
    ram = (node.info or {}).get("ram")
    return ram is not None and float(ram) >= RAM_FULL


def capacity(s) -> dict:
    nodes = list(s.scalars(select(Node)))
    used = dict(s.execute(select(Account.node_id, func.count()).where(Account.node_id.is_not(None))
                          .group_by(Account.node_id)).all())
    online = [n for n in nodes if node_online(n)]
    total = sum(n.max_slots for n in online)
    in_use = sum(used.get(n.id, 0) for n in online)
    free_by_node = {n.id: 0 if memory_full(n) else max(0, n.max_slots - used.get(n.id, 0)) for n in online}
    waiting = s.scalar(select(func.count()).select_from(Account).where(
        Account.node_id.is_(None), Account.removed.is_(False), Account.waiting_since.is_not(None))) or 0
    return {"nodes": nodes, "used": used, "online": len(online), "total": total,
            "in_use": in_use, "free": sum(free_by_node.values()), "free_by_node": free_by_node,
            "memory_full": [n.name for n in online if memory_full(n)], "waiting": waiting}


def should_run(account: Account) -> bool:
    return (not account.removed) and user_can_trade(account.user)


def assign_accounts(s) -> list[Account]:
    """Give waiting accounts a VPS slot; free slots from closed/removed accounts.
    Returns accounts that are still waiting."""
    for acc in s.scalars(select(Account).where(Account.node_id.is_not(None))):
        if not should_run(acc):
            acc.node_id = None
            acc.conn_status = "stopped"
            acc.waiting_since = None
    s.flush()
    cap = capacity(s)
    free = dict(cap["free_by_node"])
    waiting = list(s.scalars(select(Account).join(User).where(
        Account.node_id.is_(None), Account.removed.is_(False),
        User.status.in_(("active", "payment_check"))).order_by(Account.waiting_since, Account.id)))
    still = []
    for acc in waiting:
        node_id = max(free, key=free.get) if free else None
        if node_id and free[node_id] > 0:
            acc.node_id = node_id
            acc.conn_status = "starting"
            acc.conn_error = ""
            acc.waiting_since = None
            free[node_id] -= 1
        else:
            if not acc.waiting_since:
                acc.waiting_since = utcnow()
            acc.conn_status = "waiting"
            still.append(acc)
    return still


# ------------------------------------------------------------ users / billing

def set_plan(s, user: User, plan: Plan, billing: str, paid_until: datetime | None, actor: str) -> None:
    old = f"{user.plan.name}/{user.billing}"
    user.plan_id = plan.id
    user.plan = plan
    user.billing = billing
    if billing == "monthly":
        user.paid_until = paid_until or add_months(utcnow())
    else:
        user.paid_until = None
    if user.status == "payment_check" and billing != "monthly":
        user.status = "active"
    for r in s.scalars(select(Renewal).where(Renewal.user_id == user.id, Renewal.status == "pending")):
        if billing != "monthly":
            r.status = "paid"
            r.answered_by = actor
            r.answered_at = utcnow()
    from . import vault
    if vault.restore(s, user, actor)["restored"]:    # a bigger plan can reconnect accounts kept in the archive
        s.flush()
        assign_accounts(s)
    trim_to_plan(s, user)
    log(s, "plan_changed", user.id, f"{old} -> {plan.name}/{billing}", actor)


def trim_to_plan(s, user: User) -> None:
    """When a plan shrinks, pause the newest accounts and channels over the limit."""
    feats = plan_features(user)
    live = [a for a in user.accounts if not a.removed]
    max_acc = int(feature(feats, "max_accounts") or 1)
    for a in sorted(live, key=lambda a: a.id)[max_acc:]:
        a.copy_enabled = False
    chans = list(s.scalars(select(Source).where(Source.kind == "channel", Source.owner_user_id == user.id,
                                                Source.active.is_(True)).order_by(Source.id)))
    for src in chans[int(feature(feats, "max_channels") or 0):]:
        src.active = False


def close_user(s, user: User, actor: str, reason: str = "") -> None:
    from . import vault
    vault.archive(s, user, "left")        # keep their logins apart so a return needs no re-entering
    user.status = "closed"
    user.closed_at = utcnow()
    user.session_version += 1
    for a in user.accounts:
        a.node_id = None
        a.conn_status = "stopped"
        a.waiting_since = None
    for src in s.scalars(select(Source).where(Source.owner_user_id == user.id)):
        src.active = False
    log(s, "account_closed", user.id, reason, actor)
    notify.users_changed()


def remove_account(s, acc: Account, actor: str, user_id: int | None = None) -> None:
    """Stop and remove an MT5 account (its MT5 on the VPS stops; trades already open stay on the broker).
    Its history is kept for reports; the saved password is wiped."""
    acc.removed = True
    acc.copy_enabled = False
    acc.node_id = None
    acc.conn_status = "stopped"
    acc.waiting_since = None
    acc.password_enc = ""
    log(s, "account_removed", user_id if user_id is not None else acc.user_id, acc.login, actor)
    notify.users_changed()


def delete_user(s, user: User, actor: str) -> str:
    """Delete a customer for good: their MT5 accounts with all their trades and history, their plan
    requests, renewals and alerts. Support tickets and the activity log are kept, without the name link.
    Groups they added stop and are kept, so signals other accounts copied aren't lost."""
    from .db import CustomerAlert, Ticket, UpgradeRequest
    from . import vault
    vault.archive(s, user, "deleted", keep_telegram=False)   # MT5 logins survive; the Telegram session does not
    name, uid = user.username, user.id
    acc_ids = [a.id for a in s.scalars(select(Account).where(Account.user_id == uid))]
    if acc_ids:
        for model in (Deal, Snapshot, Event, AccountSource):
            s.execute(model.__table__.delete().where(model.__table__.c.account_id.in_(acc_ids)))
    for src in s.scalars(select(Source).where(Source.owner_user_id == uid)):
        src.owner_user_id = None
        src.active = False
    for model in (Renewal, UpgradeRequest, CustomerAlert):
        s.execute(model.__table__.delete().where(model.__table__.c.user_id == uid))
    for model in (Ticket, Activity):
        s.execute(model.__table__.update().where(model.__table__.c.user_id == uid).values(user_id=None))
    for a in list(user.accounts):
        s.delete(a)
    s.flush()
    s.delete(user)
    s.flush()
    log(s, "customer_deleted", None, f"{name} (#{uid}), {len(acc_ids)} MT5 account(s)", actor)
    notify.users_changed()
    return name


def reopen_user(s, user: User, actor: str) -> None:
    from . import vault
    user.status = "active"
    user.closed_at = None
    if user.billing == "monthly" and (not user.paid_until or user.paid_until < utcnow()):
        user.paid_until = add_months(utcnow())
    vault.restore(s, user, actor)         # bring back any accounts cleared while they were away
    s.flush()
    for a in user.accounts:
        if not a.removed:
            a.conn_status = a.conn_status if a.conn_status not in ("", "stopped") else "new"
            a.waiting_since = utcnow()
    log(s, "account_reopened", user.id, "", actor)
    notify.users_changed()


def answer_renewal(s, renewal: Renewal, paid: bool, actor: str) -> str:
    if renewal.status != "pending":
        return f"Already answered: {renewal.status.replace('_', ' ')} by {renewal.answered_by}"
    user = renewal.user
    renewal.answered_by = actor
    renewal.answered_at = utcnow()
    if paid:
        renewal.status = "paid"
        base = max(renewal.due, user.paid_until or renewal.due)
        user.paid_until = add_months(base)
        if user.status == "payment_check":
            user.status = "active"
        log(s, "renewal_paid", user.id, f"paid until {user.paid_until:%d %b %Y}", actor)
        return f"✅ {user.username} marked PAID by {actor}. Active until {user.paid_until:%d %b %Y}."
    renewal.status = "not_paid"
    close_user(s, user, actor, "monthly payment not received")
    return f"❌ {user.username} marked NOT PAID by {actor}. Account closed and copying stopped."


def due_renewals(s) -> list[User]:
    """Monthly users whose paid period has ended and who haven't been asked yet."""
    now = utcnow()
    users = s.scalars(select(User).where(User.billing == "monthly", User.status == "active",
                                         User.paid_until.is_not(None), User.paid_until <= now))
    return list(users)


# ------------------------------------------------------------ stats

def period_starts(now: datetime | None = None) -> dict[str, datetime]:
    now = now or utcnow()
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return {"today": day, "week": day - timedelta(days=day.weekday()),
            "month": day.replace(day=1), "all": datetime(2000, 1, 1)}


def stats(s, account_ids: list[int]) -> dict:
    out = {"periods": {}, "trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
           "best": 0.0, "worst": 0.0, "avg_win": 0.0, "avg_loss": 0.0}
    if not account_ids:
        for k in period_starts():
            out["periods"][k] = 0.0
        return out
    for name, since in period_starts().items():
        total = s.scalar(select(func.coalesce(func.sum(Deal.profit), 0.0)).where(
            Deal.account_id.in_(account_ids), Deal.time >= since, Deal.entry.in_(("out", "inout", "in"))))
        out["periods"][name] = round(total or 0.0, 2)
    closes = list(s.scalars(select(Deal.profit).where(Deal.account_id.in_(account_ids), Deal.ours.is_(True),
                                                      Deal.entry.in_(("out", "inout")))))
    wins = [p for p in closes if p > 0]
    losses = [p for p in closes if p <= 0]
    out.update(trades=len(closes), wins=len(wins), losses=len(losses),
               win_rate=round(100 * len(wins) / len(closes), 1) if closes else 0.0,
               best=round(max(closes), 2) if closes else 0.0, worst=round(min(closes), 2) if closes else 0.0,
               avg_win=round(sum(wins) / len(wins), 2) if wins else 0.0,
               avg_loss=round(sum(losses) / len(losses), 2) if losses else 0.0)
    return out


def daily_pnl(s, account_ids: list[int], days: int = 30) -> list[tuple[str, float]]:
    since = (utcnow() - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = s.execute(select(func.date(Deal.time), func.sum(Deal.profit)).where(
        Deal.account_id.in_(account_ids or [-1]), Deal.time >= since,
        Deal.entry.in_(("out", "inout", "in"))).group_by(func.date(Deal.time))).all()
    by_day = {str(d): round(v or 0, 2) for d, v in rows}
    return [((since + timedelta(days=i)).strftime("%Y-%m-%d"),
             by_day.get((since + timedelta(days=i)).strftime("%Y-%m-%d"), 0.0)) for i in range(days)]


def equity_curve(s, account_id: int, days: int = 30) -> list[tuple[str, float, float]]:
    since = utcnow() - timedelta(days=days)
    rows = s.execute(select(Snapshot.time, Snapshot.balance, Snapshot.equity).where(
        Snapshot.account_id == account_id, Snapshot.time >= since).order_by(Snapshot.time)).all()
    step = max(1, len(rows) // 300)
    return [(t.strftime("%Y-%m-%d %H:%M"), b, e) for t, b, e in rows[::step]]


def worker_settings(account: Account) -> dict:
    return effective_settings(account.settings or {}, plan_features(account.user))


def activity_summary(s, user: User) -> dict:
    since = utcnow() - timedelta(days=30)
    ids = [a.id for a in user.accounts]
    logins = s.scalar(select(func.count()).select_from(Activity).where(
        Activity.user_id == user.id, Activity.action == "login", Activity.time >= since)) or 0
    trades = s.scalar(select(func.count()).select_from(Deal).where(
        Deal.account_id.in_(ids or [-1]), Deal.ours.is_(True), Deal.entry.in_(("out", "inout")),
        Deal.time >= since)) or 0
    pnl = s.scalar(select(func.coalesce(func.sum(Deal.profit), 0.0)).where(
        Deal.account_id.in_(ids or [-1]), Deal.time >= since, Deal.entry.in_(("out", "inout", "in")))) or 0
    online = sum(1 for a in user.accounts if a.conn_status == "online" and not a.removed)
    return {"logins_30d": logins, "trades_30d": trades, "pnl_30d": round(pnl, 2), "online": online,
            "accounts": sum(1 for a in user.accounts if not a.removed)}


def signup_allowed(s) -> bool:
    return bool(get_setting(s, "allow_signup"))


# ------------------------------------------------------------ VPS remote control

_node_cmds: dict[int, list[dict]] = {}


def queue_node_command(node_id: int, kind: str, **args) -> None:
    """Sent to the node with its next heartbeat (within ~10 s). Kept in memory only, so a
    Windows password for auto-login is never written to the database."""
    _node_cmds.setdefault(node_id, []).append({"kind": kind, **args})


def take_node_commands(node_id: int) -> list[dict]:
    return _node_cmds.pop(node_id, [])


def new_install_code(s, node: Node, token: str) -> str:
    """One-line installer code for a new VPS, valid 48 hours."""
    import secrets as _secrets
    import time as _time

    from . import security
    from .db import set_setting
    codes = {k: v for k, v in (get_setting(s, "install_codes") or {}).items() if v.get("exp", 0) > _time.time()}
    code = _secrets.token_urlsafe(18)
    codes[code] = {"node_id": node.id, "token_enc": security.encrypt(token), "exp": _time.time() + 48 * 3600}
    set_setting(s, "install_codes", codes)
    return code


def use_install_code(s, code: str) -> tuple[Node, str] | None:
    import time as _time

    from . import security
    item = (get_setting(s, "install_codes") or {}).get(code)
    if not item or item.get("exp", 0) < _time.time():
        return None
    node = s.get(Node, item["node_id"])
    if not node:
        return None
    return node, security.decrypt(item["token_enc"])



# ------------------------------------------------------------ broker servers

def learned_servers(s) -> list[str]:
    """Servers that have actually connected on our VPS (the worker filled in the broker name)."""
    rows = s.execute(select(Account.server, func.count()).where(Account.broker != "")
                     .group_by(Account.server).order_by(func.count().desc())).all()
    return [r[0] for r in rows]


# ------------------------------------------------------------ signal groups (one per Telegram chat)

def group_key(sig: Signal) -> str:
    """Groups are Telegram chats: the VIP source reads several, and each gets its own card and settings.
    A forum topic that was picked on its own is its own group ("<chat>:<topic>")."""
    if sig.grp:
        return sig.grp
    return str(sig.tg_chat_id) if sig.tg_chat_id is not None else f"s{sig.source_id}"


def chat_of(key: str) -> str:
    """The Telegram chat of a group key ("-100123:7" -> "-100123")."""
    return str(key).split(":", 1)[0]


def signal_groups(s, source_ids, user: User | None = None) -> list[dict]:
    """Every group a customer's followed sources have posted from, newest activity first.
    With a user, private VIP groups they don't get are left out."""
    source_ids = list(source_ids) or [-1]
    sources = {x.id: x for x in s.scalars(select(Source).where(Source.id.in_(source_ids)))}
    rows = s.execute(select(Signal.grp, Signal.tg_chat_id, Signal.source_id, func.max(Signal.id))
                     .where(Signal.source_id.in_(source_ids), Signal.tg_chat_id.is_not(None))
                     .group_by(Signal.grp, Signal.tg_chat_id, Signal.source_id)).all()
    main_id = main_source(s).id
    allowed = vip_keys(s, user) if user is not None else None
    out, seen_sources, keys = [], set(), {}
    for grp, chat, sid, last_id in sorted(rows, key=lambda r: -r[3]):
        key = grp or str(chat)
        if allowed is not None and sid == main_id and key not in allowed and str(chat) not in allowed:
            continue                                # a private VIP group this customer doesn't get
        seen_sources.add(sid)
        if key in keys:                             # the same chat followed through two sources
            continue
        keys[key] = True
        last = s.get(Signal, last_id)
        src = sources.get(sid)
        out.append({"key": key, "title": (last.channel if last and last.channel else (src.title if src else "?")),
                    "source_id": sid, "source": src.title if src else "", "last": last})
    for sid, src in sources.items():
        if sid not in seen_sources and src.kind != "manual":
            q = select(Signal).where(Signal.source_id == sid)
            if sid == main_id and allowed is not None:
                q = q.where(Signal.tg_chat_id.is_(None))       # only the admins' own VIP signals, never a hidden group's
            last = s.scalar(q.order_by(Signal.id.desc()).limit(1))
            out.append({"key": f"s{sid}", "title": src.title, "source_id": sid, "source": src.title, "last": last})
    out.sort(key=lambda g: g["last"].id if g["last"] else 0, reverse=True)
    return out


def group_stats(s, account_ids, since: datetime) -> dict:
    """Per group since a time: signals posted, trades closed, wins, profit (on these accounts)."""
    out: dict[str, dict] = {}
    blank = lambda: {"signals": 0, "trades": 0, "wins": 0, "pnl": 0.0}  # noqa: E731
    seen_posts = set()
    for sig in s.scalars(select(Signal).where(Signal.created_at >= since)):
        post = (sig.tg_chat_id, sig.tg_msg_id) if sig.tg_msg_id else ("id", sig.id)
        if post in seen_posts:
            continue                                     # one post that reached us twice
        seen_posts.add(post)
        out.setdefault(group_key(sig), blank())["signals"] += 1
    deals = list(s.scalars(select(Deal).where(Deal.account_id.in_(list(account_ids) or [-1]), Deal.time >= since,
                                              Deal.signal_id.is_not(None))))
    sig_keys = {x.id: group_key(x) for x in s.scalars(select(Signal).where(
        Signal.id.in_({d.signal_id for d in deals} or {-1})))}
    trades: dict[tuple, list] = {}
    for d in deals:
        if d.entry in ("out", "inout"):
            k = (d.account_id, d.position_id or d.ticket)
            row = trades.setdefault(k, [sig_keys.get(d.signal_id), 0.0])
            row[1] += d.profit or 0
    for key, pnl in trades.values():
        if not key:
            continue
        g = out.setdefault(key, blank())
        g["trades"] += 1
        g["wins"] += pnl > 0
        g["pnl"] = round(g["pnl"] + pnl, 2)
    for g in out.values():
        g["win_rate"] = round(100 * g["wins"] / g["trades"]) if g["trades"] else 0
    return out


OPT_KEYS = ("tp_target", "wait_for_zone")


def optimisation(s, key: str) -> list[str]:
    """What optimised copying does for one group (empty when the group isn't optimised)."""
    rules = get_setting(s, "group_rules") or {}
    r = rules.get(str(key)) or rules.get(chat_of(key)) or {}
    out = []
    tp = r.get("tp_target")
    if tp in ("tp1", "tp2"):
        out.append(f"Takes profit at {tp.upper()}")
    elif tp == "spread":
        out.append("Takes profit across the group's targets")
    if r.get("wait_for_zone"):
        out.append('A quick "BUY / SELL now" waits for the price range the group posts next, then enters')
    return out


def optimise_request(s, key: str) -> dict:
    return (get_setting(s, "optimise_requests") or {}).get(str(key)) or {}


def request_optimise(s, user, key: str, title: str) -> bool:
    """An Extreme customer asks us to optimise a group. False if it was already asked for (by anyone)."""
    reqs = dict(get_setting(s, "optimise_requests") or {})
    r = dict(reqs.get(key) or {})
    users = list(r.get("users") or [])
    new_user = user.id not in users
    if new_user:
        users.append(user.id)
    if not r:
        r = {"title": title[:120], "at": utcnow().isoformat(timespec="minutes"), "status": "requested", "note": ""}
    r["users"] = users[-50:]
    reqs[key] = r
    set_setting(s, "optimise_requests", reqs)
    log(s, "optimise_request", user.id, f"{title} ({key})", user.username)
    return new_user


def group_rule(s, sig: Signal) -> dict:
    """Admin's house rules for one VIP group, e.g. KD: risk-free = each entry + 15 pips."""
    if sig.tg_chat_id is None:
        return {}
    rules = get_setting(s, "group_rules") or {}
    return rules.get(group_key(sig)) or rules.get(str(sig.tg_chat_id)) or {}

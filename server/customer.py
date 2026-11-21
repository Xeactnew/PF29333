"""Customer website: sign up, log in, dashboard, MT5 accounts, signals, channels, plans."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, or_, select

from . import billing, mailer, notify, security, services, support
from . import telegram_service as tg
from .db import (Account, Deal, Plan, Signal, SignalUpdate, Source, Ticket, UpgradeRequest, User, get_setting,
                 main_source, utcnow)
from .parser import ParsedSignal
from .plans import DEFAULT_ACCOUNT_SETTINGS, clean_settings, effective_settings, feature, locked_keys
from .web import (FormExpired, client_ip, current_user, db_session, flash, form_data, form_expired_message,
                  redirect, render)

router = APIRouter()
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")


def _plans(s):
    return list(s.scalars(select(Plan).order_by(Plan.sort)))


# ------------------------------------------------------------ public

@router.get("/")
def home(request: Request, s=Depends(db_session)):
    if request.session.get("uid"):
        return redirect("/dashboard")
    return render(request, "home.html", {"plans": _plans(s), "allow_signup": services.signup_allowed(s)})


@router.get("/terms")
def terms(request: Request):
    return render(request, "terms.html", {"updated": "18 November 2026"})


@router.get("/signup")
def signup_page(request: Request, s=Depends(db_session)):
    if not services.signup_allowed(s):
        return render(request, "message.html", {"title": "Sign-ups are closed",
                      "text": get_setting(s, "contact_text")})
    expired = request.query_params.get("expired")
    return render(request, "auth.html", {"mode": "signup", "plans": _plans(s),
                                         "want_plan": request.query_params.get("plan", "standard"),
                                         "want_billing": request.query_params.get("billing", "monthly"),
                                         "error": form_expired_message(FormExpired(expired == "cookie"))
                                         if expired else None})


@router.post("/signup")
async def signup(request: Request, s=Depends(db_session)):
    try:
        form = await form_data(request)
    except FormExpired as e:
        return redirect("/signup?expired=" + ("cookie" if e.cookie_missing else "1"))
    if not services.signup_allowed(s):
        return redirect("/signup")
    username = str(form.get("username", "")).strip()
    email = str(form.get("email", "")).strip()[:255]
    password = str(form.get("password", ""))
    ip = client_ip(request)
    err = None
    if not USERNAME_RE.match(username):
        err = "Usernames are 3–32 letters, numbers, dots, dashes or underscores."
    elif s.scalar(select(User).where(func.lower(User.username) == username.lower())):
        err = "That username is taken. Pick another one."
    elif security.password_problem(password):
        err = security.password_problem(password)
    elif security.too_many_attempts("signup:" + ip, limit=5, window=3600):
        err = "Too many sign-ups from this connection. Try again in an hour."
    want = s.scalar(select(Plan).where(Plan.key == str(form.get("plan", "standard"))))
    want_billing = "lifetime" if form.get("billing") == "lifetime" else "monthly"
    if not err and not re.match(mailer.EMAIL_RE, email):
        err = "Enter your email. We use it for payment reminders and support replies, nothing else."
    if err:
        return render(request, "auth.html", {"mode": "signup", "error": err, "username": username, "email": email,
                                             "plans": _plans(s), "want_plan": want.key if want else "standard",
                                             "want_billing": want_billing}, 400)
    security.record_attempt("signup:" + ip)
    plan = s.scalar(select(Plan).where(Plan.key == "standard"))
    user = User(username=username, email=email, password_hash=security.hash_password(password),
                plan_id=plan.id, plan=plan, billing="free", created_by="signup")
    s.add(user)
    s.flush()
    services.log(s, "signup", user.id, "", username, ip)
    paid = want and (want.monthly_price or want.lifetime_price) and want.key != "standard"
    if not paid:
        notify.send_admins(f"🆕 New sign-up: {username} (Standard, free)")
    else:
        billing.create_request(s, user, want, want_billing)
    s.commit()
    _login(request, s, user, ip)
    if paid:
        flash(request, f"Account created. To start {want.name}, pay us using the details below; we switch it on "
                       f"as soon as the payment lands. You're on the free plan until then.")
        return redirect("/plan")
    flash(request, "Welcome to PipsFarmer. Add your MT5 account to start copying.")
    return redirect("/accounts")


@router.get("/login")
def login_page(request: Request):
    closed = request.query_params.get("closed")
    return render(request, "auth.html", {"mode": "login", "error":
                  "This account has been closed. Contact us to reopen it." if closed else None})


def _login(request: Request, s, user: User, ip: str) -> None:
    # keep an admin login and the form token in the same browser; only the customer part changes
    for k in ("uid", "sv"):
        request.session.pop(k, None)
    request.session["uid"] = user.id
    request.session["sv"] = user.session_version
    user.last_login = user.last_seen = utcnow()
    user.login_count += 1
    services.log(s, "login", user.id, "", user.username, ip)
    s.commit()


@router.post("/login")
async def login(request: Request, s=Depends(db_session)):
    try:
        form = await form_data(request)
    except FormExpired as e:     # show a fresh form instead of an error page
        return render(request, "auth.html", {"mode": "login", "error": form_expired_message(e)}, 400)
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    ip = client_ip(request)
    key = f"login:{ip}:{username.lower()}"
    if security.too_many_attempts(key):
        return render(request, "auth.html", {"mode": "login", "username": username,
                      "error": "Too many attempts. Wait 10 minutes and try again."}, 429)
    user = s.scalar(select(User).where(func.lower(User.username) == username.lower()))
    if not user or not security.verify_password(password, user.password_hash):
        security.record_attempt(key)
        return render(request, "auth.html", {"mode": "login", "username": username,
                      "error": "Wrong username or password."}, 400)
    if user.status == "closed":
        return render(request, "auth.html", {"mode": "login", "username": username,
                      "error": "This account has been closed. Contact us to reopen it."}, 403)
    security.clear_attempts(key)
    _login(request, s, user, ip)
    return redirect("/dashboard")


@router.post("/logout")
async def logout(request: Request):
    await form_data(request)
    request.session.pop("uid", None)
    request.session.pop("sv", None)
    return redirect("/")


# ------------------------------------------------------------ dashboard

def _live(user):
    return [a for a in user.accounts if not a.removed]


def _away_window(request: Request) -> tuple[datetime, datetime | None]:
    try:
        return (datetime.fromisoformat(request.session["away_from"]),
                datetime.fromisoformat(request.session["away_to"]))
    except (KeyError, ValueError, TypeError):
        return utcnow() - timedelta(hours=24), None


def since_summary(s, accounts, src_ids, since: datetime) -> dict:
    """What happened while the customer was away: closed trades, best and worst, new signals."""
    ids = [a.id for a in accounts] or [-1]
    deals = list(s.scalars(select(Deal).where(Deal.account_id.in_(ids), Deal.time >= since,
                                              Deal.entry.in_(("out", "inout")))))
    sig_ids = {d.signal_id for d in deals if d.signal_id}
    sigs = {x.id: x for x in s.scalars(select(Signal).where(Signal.id.in_(sig_ids or {-1})))}
    trades: dict[tuple, dict] = {}
    for d in deals:
        row = trades.setdefault((d.account_id, d.position_id or d.ticket),
                                {"pnl": 0.0, "side": d.side, "time": d.time, "signal": sigs.get(d.signal_id)})
        row["pnl"] += d.profit or 0
        row["time"] = max(row["time"], d.time)
    titles = {x.id: x.title for x in s.scalars(select(Source))}
    for r in trades.values():
        sg = r["signal"]
        r["group"] = (sg.channel or titles.get(sg.source_id) or "a signal") if sg else "Your trade"
    rows = sorted(trades.values(), key=lambda r: r["time"], reverse=True)
    posts = set()
    for x in s.scalars(select(Signal).where(Signal.created_at >= since, Signal.source_id.in_(src_ids or {-1}))):
        posts.add((x.tg_chat_id, x.tg_msg_id) if x.tg_msg_id else ("id", x.id))
    best = max(rows, key=lambda r: r["pnl"]) if rows else None
    worst = min(rows, key=lambda r: r["pnl"]) if rows else None
    return {"closed": len(rows), "won": sum(r["pnl"] > 0 for r in rows), "net": round(sum(r["pnl"] for r in rows), 2),
            "best": best if best and best["pnl"] > 0 else None, "worst": worst if worst and worst["pnl"] < 0 else None,
            "posts": len(posts), "rows": rows[:8]}


def live_trades(s, accounts) -> list[dict]:
    """Open trades grouped by signal, with the numbers for the SL / entry / targets bar."""
    out = []
    for a in accounts:
        live = a.live_state or {}
        by_sig: dict = {}
        for kind in ("positions", "pendings", "waiting"):
            for p in live.get(kind) or []:
                by_sig.setdefault(p.get("signal_id") or f"t{p.get('ticket')}", {"positions": [], "pendings": [], "waiting": []})[kind].append(p)
        price = live.get("bid")
        for sid, g in by_sig.items():
            sig = s.get(Signal, int(sid)) if str(sid).isdigit() else None
            first = (g["positions"] or g["pendings"] or g["waiting"])[0]
            side = first.get("side", "buy")
            entries = [p["price"] for k in ("positions", "pendings", "waiting") for p in g[k] if p.get("price")]
            sl = (sig.sl if sig else None) or first.get("sl") or None
            tps = list(sig.tps or []) if sig else [p.get("tp") for p in g["positions"] if p.get("tp")]
            pts = [x for x in [sl, price, *entries, *tps] if x]
            lo, hi = (min(pts), max(pts)) if pts else (0, 1)
            span = (hi - lo) or 1
            pos = lambda v: round(100 * (v - lo) / span, 1) if v else None  # noqa: E731
            flip = side == "sell"                      # sells: stop on the right, targets on the left
            at = (lambda v: None if v is None else round(100 - pos(v), 1)) if flip else pos
            filled, total = len(g["positions"]), len(g["positions"]) + len(g["pendings"]) + len(g["waiting"])
            me = [at(e) for e in entries]
            band = lambda a, b: (min(a, b), round(abs(b - a), 1)) if a is not None and b is not None else None  # noqa: E731
            z_risk = band(at(sl), min(me)) if me and sl else None
            z_entry = band(min(me), max(me)) if me else None
            if z_entry and z_entry[1] < 1.5:                       # one entry: give it a visible width
                z_entry = (max(0, z_entry[0] - 0.75), 1.5)
            z_reward = band(max(me), max(at(x) for x in tps)) if me and tps else None
            out.append({"account": a, "signal": sig, "side": side,
                        "channel": (sig.channel or "Signal") if sig else "Your trade",
                        "lots": round(sum(p.get("volume", 0) for p in g["positions"]), 2),
                        "profit": round(sum(p.get("profit", 0) for p in g["positions"]), 2),
                        "filled": filled, "total": total, "entries": sorted(entries),
                        "sl": sl, "tps": tps, "price": price,
                        "m_sl": at(sl), "m_price": at(price), "m_entries": [at(e) for e in entries],
                        "m_tps": [at(x) for x in tps], "z_risk": z_risk, "z_entry": z_entry, "z_reward": z_reward})
    return out


def market_sessions(now: datetime) -> dict:
    """Sydney / Tokyo / London / New York, in UTC hours, for the strip on the overview."""
    h = now.hour + now.minute / 60
    rows = [("Sydney", 22, 7), ("Tokyo", 0, 9), ("London", 8, 17), ("New York", 13, 22)]
    out = []
    for name, a, b in rows:
        segs = [(a, b)] if a < b else [(a, 24), (0, b)]
        out.append({"name": name, "segs": [(round(x / 24 * 100, 2), round((y - x) / 24 * 100, 2)) for x, y in segs],
                    "open": any(x <= h < y for x, y in segs)})
    return {"rows": out, "now": round(h / 24 * 100, 2)}


def greeting(now: datetime) -> str:
    try:
        from zoneinfo import ZoneInfo
        h = now.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Europe/London")).hour
    except Exception:
        h = now.hour
    return "Good morning" if 5 <= h < 12 else "Good afternoon" if 12 <= h < 18 else "Good evening"


@router.get("/dashboard")
def dashboard(request: Request, s=Depends(db_session)):
    user = current_user(request, s)
    accounts = _live(user)
    ids = [a.id for a in accounts]
    recent = list(s.scalars(select(Deal).where(Deal.account_id.in_(ids or [-1]),
                                               Deal.entry.in_(("out", "inout"))).order_by(Deal.time.desc()).limit(12)))
    acc_names = {a.id: a.label or a.broker or a.login for a in accounts}
    src_ids = {sid for a in accounts for sid in services.account_source_ids(s, a.id)}
    signals = list(s.scalars(select(Signal).where(or_(Signal.source_id.in_(src_ids or [-1]),
                                                      Signal.created_by == f"user:{user.id}"))
                             .order_by(Signal.id.desc()).limit(6)))
    open_positions = sum(len((a.live_state or {}).get("positions") or []) for a in accounts)
    floating = sum(p.get("profit", 0) for a in accounts for p in ((a.live_state or {}).get("positions") or []))
    away_from, away_to = _away_window(request)
    now = utcnow()
    groups = user_group_cards(s, accounts)
    trades_live = live_trades(s, accounts)
    in_trade = {t["signal"].tg_chat_id for t in trades_live if t["signal"] and t["signal"].tg_chat_id is not None}
    for g in groups:
        g["in_trade"] = g["key"].lstrip("-").isdigit() and int(g["key"]) in in_trade
    setup = [
        ("Connect an MT5 account", bool(accounts), "/accounts"),
        ("MT5 logged in and online", any(a.conn_status == "online" for a in accounts),
         f"/accounts/{accounts[0].id}" if accounts else "/accounts"),
        ("Pick the groups to copy", bool(src_ids), f"/accounts/{accounts[0].id}#groups" if accounts else "/accounts"),
        ("Set your lot size", any((a.settings or {}) != {} for a in accounts), f"/accounts/{accounts[0].id}" if accounts else "/accounts"),
        ("Copying switched on", any(a.copy_enabled for a in accounts), f"/accounts/{accounts[0].id}" if accounts else "/accounts"),
    ]
    return render(request, "dashboard.html", {
        "setup": setup if not all(x[1] for x in setup) else None,
        "hello": greeting(now), "away_from": away_from, "away_to": away_to,
        "away": (away_to - away_from) if away_to else None,
        "summary": since_summary(s, accounts, src_ids, away_from), "groups": groups,
        "trades_live": trades_live, "sessions": market_sessions(now),
        "waiting_entries": sum(len((a.live_state or {}).get("pendings") or []) + len((a.live_state or {}).get("waiting") or [])
                               for a in accounts),
        "user": user, "accounts": accounts, "stats": services.stats(s, ids),
        "daily": services.daily_pnl(s, ids, 30), "recent": recent, "acc_names": acc_names,
        "signals": signals, "open_positions": open_positions, "floating": floating,
        "currency": accounts[0].currency if accounts else "",
        "balance": sum(a.balance for a in accounts), "equity": sum(a.equity for a in accounts)})


# ------------------------------------------------------------ MT5 accounts

def _get_account(s, user: User, account_id: int) -> Account:
    acc = s.get(Account, account_id)
    if not acc or acc.user_id != user.id or acc.removed:
        raise HTTPException(404, "Account not found")
    return acc


@router.get("/accounts")
def accounts_page(request: Request, s=Depends(db_session)):
    user = current_user(request, s)
    accounts = _live(user)
    cap = services.capacity(s)
    max_acc = int(feature(user.plan.features, "max_accounts") or 1)
    from . import brokers
    learned = services.learned_servers(s)
    return render(request, "accounts.html", {"user": user, "accounts": accounts, "max_accounts": max_acc,
                                             "can_add": len(accounts) < max_acc, "free_slots": cap["free"],
                                             "broker_choices": brokers.choices(learned),
                                             "prop_firms": brokers.PROP_FIRMS})


@router.post("/accounts/add")
async def accounts_add(request: Request, s=Depends(db_session)):
    user = current_user(request, s)
    form = await form_data(request)
    max_acc = int(feature(user.plan.features, "max_accounts") or 1)
    if len(_live(user)) >= max_acc:
        flash(request, f"Your {user.plan.name} plan allows {max_acc} MT5 account(s). Upgrade to add more.", "err")
        return redirect("/accounts")
    from . import brokers
    login = re.sub(r"\D", "", str(form.get("login", "")))
    server = brokers.canonical(str(form.get("server", "")), services.learned_servers(s))[:128]
    password = str(form.get("password", ""))
    label = str(form.get("label", "")).strip()[:64]
    if not login or not server or not password:
        flash(request, "Fill in the MT5 login number, password and server.", "err")
        return redirect("/accounts")
    if s.scalar(select(Account).where(Account.login == login, Account.server == server,
                                      Account.removed.is_(False))):
        flash(request, "That MT5 account is already connected to PipsFarmer.", "err")
        return redirect("/accounts")
    acc = Account(user_id=user.id, login=login, server=server, label=label,
                  password_enc=security.encrypt(password), settings=dict(DEFAULT_ACCOUNT_SETTINGS),
                  waiting_since=utcnow(), conn_status="waiting")
    s.add(acc)
    s.flush()
    services.ensure_default_sources(s, acc)
    still_waiting = services.assign_accounts(s)
    services.log(s, "account_added", user.id, f"{login}@{server}", user.username, client_ip(request))
    s.commit()
    notify.users_changed()
    if acc in still_waiting:
        flash(request, "All our servers are full right now. Your account is in the queue and will connect "
                       "automatically as soon as a slot frees up.", "warn")
    else:
        flash(request, "Account added. It usually connects within a minute.")
    return redirect(f"/accounts/{acc.id}")


@router.get("/accounts/{account_id}")
def account_page(account_id: int, request: Request, s=Depends(db_session)):
    user = current_user(request, s)
    acc = _get_account(s, user, account_id)
    feats = user.plan.features
    deals = list(s.scalars(select(Deal).where(Deal.account_id == acc.id, Deal.entry.in_(("out", "inout")))
                           .order_by(Deal.time.desc()).limit(50)))
    sources = services.available_sources(s, user)
    queue_pos = None
    if acc.conn_status == "waiting" and acc.waiting_since:
        queue_pos = 1 + (s.scalar(select(func.count()).select_from(Account).where(
            Account.node_id.is_(None), Account.removed.is_(False), Account.waiting_since.is_not(None),
            Account.waiting_since < acc.waiting_since)) or 0)
    followed = set(services.account_source_ids(s, acc.id))
    groups = services.signal_groups(s, followed)
    gstats = services.group_stats(s, [acc.id], services.period_starts()["week"])
    settings = clean_settings(acc.settings)
    for g in groups:
        g["cfg"] = settings["groups"].get(g["key"], {})
        g["week"] = gstats.get(g["key"], {"signals": 0, "trades": 0, "wins": 0, "pnl": 0.0, "win_rate": 0})
        g["checks"] = group_checks(acc, g)
        g["opt"] = group_opt(s, g["key"], g["cfg"], feats)
    from . import brokers
    return render(request, "account.html", {
        "groups": groups, "broker_choices": brokers.choices(services.learned_servers(s)),
        "prop_firms": brokers.PROP_FIRMS,
        "user": user, "acc": acc, "settings": settings,
        "effective": effective_settings(acc.settings, feats), "locked": locked_keys(feats),
        "max_layers": int(feature(feats, "max_layers") or 1), "stats": services.stats(s, [acc.id]),
        "curve": services.equity_curve(s, acc.id), "deals": deals, "sources": sources,
        "followed": set(services.account_source_ids(s, acc.id)), "queue_pos": queue_pos,
        "live": acc.live_state or {}})


def group_opt(s, key: str, cfg: dict, plan_features: dict) -> dict:
    """Optimised copying for one group: what it does, whether it's on, and (for groups we haven't
    optimised) whether this customer's plan lets them ask us to."""
    summary = services.optimisation(s, key)
    return {"summary": summary, "on": bool(summary) and cfg.get("optimised") is not False,
            "telegram": bool(re.fullmatch(r"-?\d+", key)), "req": services.optimise_request(s, key),
            "can": bool(feature(plan_features, "group_optimise"))}


def group_checks(acc: Account, g: dict) -> list[tuple[bool, str, str]]:
    """The readiness list on a group card: (done, what, why not)."""
    live = acc.live_state or {}
    return [
        (acc.conn_status == "online", "MT5 online", acc.conn_error or "Waiting for MT5 to log in"),
        (bool(acc.copy_enabled), "Copying on", "Copying is paused on this account"),
        (not live.get("blocked"), "Trading allowed", live.get("blocked") or ""),
        (not g["cfg"].get("paused"), "Group on", "You switched this group off"),
    ]


def user_group_cards(s, accounts) -> list[dict]:
    """One card per signal group across all of a customer's accounts (overview + signals page)."""
    follows = {a.id: set(services.account_source_ids(s, a.id)) for a in accounts}
    groups = services.signal_groups(s, set().union(*follows.values()) if follows else set())
    ids = [a.id for a in accounts]
    starts = services.period_starts()
    today, week = services.group_stats(s, ids, starts["today"]), services.group_stats(s, ids, starts["week"])
    blank = {"signals": 0, "trades": 0, "wins": 0, "pnl": 0.0, "win_rate": 0}
    for g in groups:
        g["today"], g["week"] = today.get(g["key"], dict(blank)), week.get(g["key"], dict(blank))
        g["accounts"] = []
        best = None
        for a in accounts:
            if g["source_id"] not in follows[a.id]:
                continue
            cfg = clean_settings(a.settings)["groups"].get(g["key"], {})
            checks = group_checks(a, {"cfg": cfg})
            g["accounts"].append({"acc": a, "cfg": cfg, "ready": all(c[0] for c in checks)})
            if best is None or sum(c[0] for c in checks) > sum(c[0] for c in best):
                best = checks
        g["optimised"] = bool(services.optimisation(s, g["key"])) and any(
            x["cfg"].get("optimised") is not False for x in g["accounts"])
        g["checks"] = best or [(False, "Followed by an account", "No account copies this group")]
        g["ready"] = any(x["ready"] for x in g["accounts"])
        g["paused"] = bool(g["accounts"]) and all(x["cfg"].get("paused") for x in g["accounts"])
    return groups


@router.post("/accounts/{account_id}/groups/{key}")
async def account_group(account_id: int, key: str, request: Request, s=Depends(db_session)):
    """One signal group's own settings (or pause) on one account."""
    user = current_user(request, s)
    acc = _get_account(s, user, account_id)
    form = await form_data(request)
    settings = clean_settings(acc.settings)
    groups = dict(settings["groups"])
    if form.get("action") == "reset":
        groups.pop(key, None)
    else:
        groups[key] = {"paused": form.get("trading") != "on", "own": form.get("own") == "on",
                       "optimised": (groups.get(key) or {}).get("optimised", True),
                       **{k: form.get(k) for k in ("lot_mode", "fixed_lot", "risk_percent", "risk_amount",
                                                   "layers", "tp_target")}}
    settings["groups"] = groups
    acc.settings = clean_settings(settings)
    services.log(s, "group_settings", user.id, f"account {acc.login} group {key}", user.username, client_ip(request))
    s.commit()
    flash(request, "Group settings saved. They apply to this group's next signal.")
    return redirect(f"/accounts/{acc.id}#groups")


@router.post("/accounts/{account_id}/groups/{key}/optimised")
async def account_group_optimised(account_id: int, key: str, request: Request, s=Depends(db_session)):
    """Optimised copying on or off for one group on one account."""
    user = current_user(request, s)
    acc = _get_account(s, user, account_id)
    form = await form_data(request)
    on = form.get("optimised") == "on"
    settings = clean_settings(acc.settings)
    groups = dict(settings["groups"])
    g = dict(groups.get(key) or {})
    g["optimised"] = on
    groups[key] = g
    settings["groups"] = groups
    acc.settings = clean_settings(settings)
    services.log(s, "group_optimised", user.id, f"account {acc.login} group {key}: {'on' if on else 'off'}",
                 user.username, client_ip(request))
    s.commit()
    flash(request, "Optimised copying is on for this group." if on else
          "Optimised copying is off: this group is copied exactly as posted, with your own settings.")
    return redirect(f"/accounts/{acc.id}#g-{key}")


@router.post("/accounts/{account_id}/groups/{key}/optimise")
async def account_group_optimise(account_id: int, key: str, request: Request, s=Depends(db_session)):
    """Extreme: ask us to work out the best way to copy a group we haven't optimised yet."""
    user = current_user(request, s)
    acc = _get_account(s, user, account_id)
    await form_data(request)
    if not feature(user.plan.features, "group_optimise"):
        flash(request, "Asking us to optimise a group is part of the Extreme plan.", "err")
        return redirect("/plan")
    group = next((g for g in services.signal_groups(s, set(services.account_source_ids(s, acc.id)))
                  if g["key"] == key), None)
    if not group or not re.fullmatch(r"-?\d+", key):
        raise HTTPException(404, "Group not found")
    if services.optimisation(s, key):
        flash(request, "This group is already optimised.")
        return redirect(f"/accounts/{acc.id}#g-{key}")
    if services.request_optimise(s, user, key, group["title"]):
        notify.send_admins(f"✦ {user.username} ({user.plan.name}) asked us to optimise {group['title']} ({key}).\n"
                           f"Admin → Telegram → Optimisation requests.")
    s.commit()
    flash(request, "Thanks, we'll look at this group. You'll see the answer on this card.")
    return redirect(f"/accounts/{acc.id}#g-{key}")


@router.post("/accounts/{account_id}/settings")
async def account_settings(account_id: int, request: Request, s=Depends(db_session)):
    user = current_user(request, s)
    acc = _get_account(s, user, account_id)
    form = await form_data(request)
    raw = dict(clean_settings(acc.settings))
    checkbox = [k for k, v in DEFAULT_ACCOUNT_SETTINGS.items() if isinstance(v, bool)]
    for k in DEFAULT_ACCOUNT_SETTINGS:
        if k in checkbox:
            raw[k] = form.get(k) == "on"
        elif k in form:
            raw[k] = form[k]
    acc.settings = clean_settings(raw)
    ids = form.get("sources", [])
    ids = ids if isinstance(ids, list) else [ids]
    services.set_account_sources(s, acc, [int(i) for i in ids if str(i).isdigit()])
    acc.label = str(form.get("label", acc.label)).strip()[:64]
    services.log(s, "settings_saved", user.id, f"account {acc.login}", user.username, client_ip(request))
    s.commit()
    locked_used = [k for k in locked_keys(user.plan.features) if acc.settings.get(k) != DEFAULT_ACCOUNT_SETTINGS.get(k)]
    flash(request, "Settings saved. They apply to the next signal." +
          (" Some options aren't in your plan and stay off until you upgrade." if locked_used else ""))
    return redirect(f"/accounts/{acc.id}")


@router.post("/accounts/{account_id}/toggle")
async def account_toggle(account_id: int, request: Request, s=Depends(db_session)):
    user = current_user(request, s)
    acc = _get_account(s, user, account_id)
    await form_data(request)
    if not acc.copy_enabled:
        max_acc = int(feature(user.plan.features, "max_accounts") or 1)
        active = sum(1 for a in _live(user) if a.copy_enabled)
        if active >= max_acc:
            flash(request, "Your plan's account limit is reached. Pause another account or upgrade.", "err")
            return redirect(f"/accounts/{acc.id}")
    acc.copy_enabled = not acc.copy_enabled
    services.log(s, "copy_" + ("on" if acc.copy_enabled else "off"), user.id, acc.login, user.username)
    s.commit()
    flash(request, "Copying is on." if acc.copy_enabled else "Copying paused. Open trades are still managed.")
    return redirect(f"/accounts/{acc.id}")


@router.post("/accounts/{account_id}/close-all")
async def account_close_all(account_id: int, request: Request, s=Depends(db_session)):
    user = current_user(request, s)
    acc = _get_account(s, user, account_id)
    await form_data(request)
    services.command(s, acc.id, "close_all")
    services.log(s, "close_all", user.id, acc.login, user.username, client_ip(request))
    s.commit()
    flash(request, "Closing all PipsFarmer trades and pending orders on this account.")
    return redirect(f"/accounts/{acc.id}")


@router.post("/accounts/{account_id}/credentials")
async def account_credentials(account_id: int, request: Request, s=Depends(db_session)):
    user = current_user(request, s)
    acc = _get_account(s, user, account_id)
    form = await form_data(request)
    from . import brokers
    server = brokers.canonical(str(form.get("server", "")), services.learned_servers(s))[:128]
    password = str(form.get("password", ""))
    if server:
        acc.server = server
    if password:
        acc.password_enc = security.encrypt(password)
    acc.conn_error = ""
    if acc.node_id:
        acc.conn_status = "starting"
        services.command(s, acc.id, "restart")
    s.commit()
    flash(request, "Login details updated. The account will reconnect.")
    return redirect(f"/accounts/{acc.id}")


@router.post("/accounts/{account_id}/remove")
async def account_remove(account_id: int, request: Request, s=Depends(db_session)):
    user = current_user(request, s)
    acc = _get_account(s, user, account_id)
    await form_data(request)
    acc.removed = True
    acc.copy_enabled = False
    acc.node_id = None
    acc.conn_status = "stopped"
    acc.waiting_since = None
    acc.password_enc = ""
    services.log(s, "account_removed", user.id, acc.login, user.username, client_ip(request))
    s.commit()
    notify.users_changed()
    flash(request, "Account removed. Trades already open on it are left as they are.")
    return redirect("/accounts")


# ------------------------------------------------------------ signals

def _parse_manual(form: dict) -> ParsedSignal | str:
    try:
        side = form.get("side")
        if side not in ("buy", "sell"):
            return "Choose buy or sell."
        e1 = float(form.get("entry1") or 0)
        e2 = float(form.get("entry2") or e1)
        sl = float(form["sl"]) if form.get("sl") else None
        tps = [float(form[k]) for k in ("tp1", "tp2", "tp3", "tp4") if form.get(k)]
    except ValueError:
        return "Prices must be numbers, like 4037.5"
    if e1 <= 0:
        return "Enter the entry price."
    lo, hi = min(e1, e2), max(e1, e2)
    if sl is None:
        return "Enter a stop loss."
    if side == "buy" and not sl < lo:
        return "For a buy, the stop loss must be below the entry."
    if side == "sell" and not sl > hi:
        return "For a sell, the stop loss must be above the entry."
    if side == "buy" and any(tp <= lo for tp in tps):
        return "For a buy, take profits must be above the entry."
    if side == "sell" and any(tp >= hi for tp in tps):
        return "For a sell, take profits must be below the entry."
    order_type = form.get("order_type") if form.get("order_type") in ("market", "limit") else "market"
    tps = sorted(tps) if side == "buy" else sorted(tps, reverse=True)
    return ParsedSignal(side=side, entry_low=lo, entry_high=hi, sl=sl, tps=tps, order_type=order_type)


def manual_source(s, user: User) -> Source:
    src = s.scalar(select(Source).where(Source.kind == "manual", Source.owner_user_id == user.id))
    if not src:
        src = Source(kind="manual", owner_user_id=user.id, title="My manual signals")
        s.add(src)
        s.flush()
    return src


SKIP_RE = re.compile(r"signal #(\d+) skipped: (.+)$")


def _day_label(d: datetime) -> str:
    today = utcnow().date()
    if d.date() == today:
        return "Today"
    if d.date() == today - timedelta(days=1):
        return "Yesterday"
    return d.strftime("%a %d %b")


@router.get("/signals")
def signals_page(request: Request, s=Depends(db_session), g: str = ""):
    user = current_user(request, s)
    accounts = _live(user)
    src_ids = {sid for a in accounts for sid in services.account_source_ids(s, a.id)}
    src_ids.add(main_source(s).id)
    mine = s.scalar(select(Source.id).where(Source.kind == "manual", Source.owner_user_id == user.id))
    if mine:
        src_ids.add(mine)
    raw = list(s.scalars(select(Signal).where(Signal.source_id.in_(src_ids)).order_by(Signal.id.desc()).limit(150)))
    # one Telegram post that reached us through two groups lists once
    seen, signals, alias = {}, [], {}
    for x in sorted(raw, key=lambda x: x.id):
        k = (x.tg_chat_id, x.tg_msg_id) if x.tg_msg_id else ("id", x.id)
        if k in seen:
            alias[x.id] = seen[k]
            continue
        seen[k] = x.id
        signals.append(x)
    signals.reverse()
    groups = user_group_cards(s, accounts)
    if g:
        signals = [x for x in signals if services.group_key(x) == g]
    signals = signals[:60]
    ids = [x.id for x in signals] + list(alias)
    updates: dict[int, list] = {}
    for u in s.scalars(select(SignalUpdate).where(SignalUpdate.signal_id.in_(ids or [-1])).order_by(SignalUpdate.id)):
        lst = updates.setdefault(alias.get(u.signal_id, u.signal_id), [])
        if not any((v.kind, v.value) == (u.kind, u.value) for v in lst):
            lst.append(u)
    # what happened on this customer's accounts
    result: dict[int, dict] = {}
    for d in s.scalars(select(Deal).where(Deal.account_id.in_([a.id for a in accounts] or [-1]),
                                          Deal.signal_id.in_(ids or [-1]))):
        r = result.setdefault(alias.get(d.signal_id, d.signal_id), {"pnl": 0.0, "lots": 0.0, "closed": 0, "open": 0})
        if d.entry == "in":
            r["lots"] += d.volume
        else:
            r["closed"] += 1
        r["pnl"] += d.profit or 0
    skipped: dict[int, str] = {}
    for a in accounts:
        live = a.live_state or {}
        for kind in ("positions", "pendings", "waiting"):
            for p in live.get(kind) or []:
                sid = p.get("signal_id")
                if sid and str(sid).isdigit():
                    r = result.setdefault(alias.get(int(sid), int(sid)), {"pnl": 0.0, "lots": 0.0, "closed": 0, "open": 0})
                    r["open"] += 1
                    r["pnl"] += p.get("profit", 0) if kind == "positions" else 0
        for n in live.get("notes") or []:
            mm = SKIP_RE.search(n)
            if mm and "same trade as signal" not in mm.group(2):
                skipped.setdefault(alias.get(int(mm.group(1)), int(mm.group(1))), mm.group(2))
    from .web import signed
    cur = accounts[0].currency if accounts else ""
    now = utcnow()
    days, rows = [], {}
    for x in signals:
        r = result.get(x.id)
        if r and r["open"]:
            mine_txt, mine_cls = f"Live on your account · {signed(r['pnl'], cur)}", "live"
        elif r:
            mine_txt, mine_cls = (f"Taken · {r['lots']:g} lots · {signed(r['pnl'], cur)}",
                                  "up" if r["pnl"] > 0 else "down" if r["pnl"] < 0 else "")
        elif x.id in skipped:
            mine_txt, mine_cls = "Skipped: " + skipped[x.id], "skip"
        else:
            mine_txt, mine_cls = "Not taken on your accounts", "skip"
        if r and r["open"]:
            state = "Live"
        elif x.status == "open" and r and r["closed"]:
            state = "Done"
        elif x.status == "open" and (now - x.created_at).total_seconds() > 6 * 3600:
            state = "Ended"
        else:
            state = {"open": "Running", "closed": "Closed", "cancelled": "Cancelled"}.get(x.status, x.status)
        day = _day_label(x.created_at)
        if day not in rows:
            days.append(day)
            rows[day] = []
        rows[day].append({"sig": x, "mine": mine_txt, "mine_cls": mine_cls, "state": state})
    return render(request, "signals.html", {"user": user, "days": days, "rows": rows, "updates": updates,
                                            "accounts": accounts, "mine": mine, "groups": groups, "g": g,
                                            "group_title": next((x["title"] for x in groups if x["key"] == g), ""),
                                            "currency": accounts[0].currency if accounts else "",
                                            "can_manual": feature(user.plan.features, "manual_signals")})


@router.post("/signals/manual")
async def signals_manual(request: Request, s=Depends(db_session)):
    user = current_user(request, s)
    form = await form_data(request)
    if not feature(user.plan.features, "manual_signals"):
        flash(request, "Manual signals aren't in your plan.", "err")
        return redirect("/signals")
    p = _parse_manual(form)
    if isinstance(p, str):
        flash(request, p, "err")
        return redirect("/signals")
    ids = form.get("accounts", [])
    ids = ids if isinstance(ids, list) else [ids]
    mine = {a.id for a in _live(user)}
    targets = [int(i) for i in ids if str(i).isdigit() and int(i) in mine]
    if not targets:
        flash(request, "Pick at least one account to send the signal to.", "err")
        return redirect("/signals")
    sig = services.create_signal(s, manual_source(s, user), p, raw="manual", created_by=f"user:{user.id}",
                                 target_account_ids=targets)
    services.log(s, "manual_signal", user.id, f"#{sig.id} {p.side} {p.entry_low}-{p.entry_high}", user.username)
    s.commit()
    flash(request, f"Signal sent to {len(targets)} account(s).")
    return redirect("/signals")


@router.post("/signals/{signal_id}/update")
async def signals_update(signal_id: int, request: Request, s=Depends(db_session)):
    user = current_user(request, s)
    form = await form_data(request)
    sig = s.get(Signal, signal_id)
    if not sig or sig.created_by != f"user:{user.id}":
        raise HTTPException(404)
    kind = form.get("kind")
    if kind not in ("breakeven", "close", "close_partial", "cancel", "sl_move"):
        raise HTTPException(400)
    value = None
    if kind == "close_partial":
        value = 0.5
    if kind == "sl_move":
        try:
            value = float(form.get("value", ""))
        except ValueError:
            flash(request, "Enter the new stop loss price.", "err")
            return redirect("/signals")
    services.add_update(s, sig, kind, value, "manual", f"user:{user.id}")
    s.commit()
    flash(request, "Update sent.")
    return redirect("/signals")


# ------------------------------------------------------------ Telegram channels

@router.get("/channels")
async def channels_page(request: Request, s=Depends(db_session)):
    user = current_user(request, s)
    max_ch = int(feature(user.plan.features, "max_channels") or 0)
    mine = list(s.scalars(select(Source).where(Source.kind == "channel", Source.owner_user_id == user.id,
                                               Source.active.is_(True)).order_by(Source.title)))
    dialogs, tg_state = [], "none"
    if user.tg_session_enc and max_ch:
        # never wait on Telegram for long: cached chats straight away, a fresh list loads in the background
        dialogs, tg_state = await tg.dialogs_for_page(user.id, security.decrypt(user.tg_session_enc))
    have = {src.tg_chat_id for src in mine}
    main = main_source(s)
    accounts = _live(user)
    cards = _channel_cards(s, [main] + mine, accounts)
    linked = bool(user.tg_session_enc)
    steps = [("Link your Telegram", "#link", linked),
             ("Pick the channels to copy", "#pick", bool(mine)),
             ("Choose which accounts copy each one", "#copying", any(c["accounts"] for c in cards[1:]))]
    return render(request, "channels.html", {"user": user, "max_channels": max_ch, "mine": mine,
                                             "dialogs": [d for d in dialogs if d["id"] not in have],
                                             "tg_state": tg_state, "tg_error": tg.dialog_error(user.id),
                                             "reader_on": tg.reader_connected(user.id),
                                             "pending_phone": tg.pending_login_phone(user.id),
                                             "main": main, "cards": cards, "accounts": accounts,
                                             "steps": steps, "linked": linked})


def _channel_cards(s, sources: list[Source], accounts: list[Account]) -> list[dict]:
    """Status, last call, calls this week and which accounts copy it, for each channel (database only)."""
    ids = [src.id for src in sources]
    week = utcnow() - timedelta(days=7)
    counts = dict(s.execute(select(Signal.source_id, func.count(Signal.id))
                            .where(Signal.source_id.in_(ids or [-1]), Signal.created_at >= week)
                            .group_by(Signal.source_id)).all())
    acc_by_id = {a.id: a for a in accounts}
    follows: dict[int, list] = {i: [] for i in ids}
    from .db import AccountSource
    for acc_id, src_id in s.execute(select(AccountSource.account_id, AccountSource.source_id)
                                    .where(AccountSource.source_id.in_(ids or [-1]),
                                           AccountSource.account_id.in_(list(acc_by_id) or [-1]))):
        follows[src_id].append(acc_by_id[acc_id])
    out = []
    for src in sources:
        last = s.scalar(select(Signal).where(Signal.source_id == src.id).order_by(Signal.id.desc()).limit(1))
        out.append({"src": src, "last": last, "week": counts.get(src.id, 0),
                    "accounts": follows.get(src.id, []), "main": src.kind == "main"})
    return out


@router.post("/channels/copying")
async def channels_copying(request: Request, s=Depends(db_session)):
    """Which accounts copy each channel, from the Channels page (the VIP group included)."""
    user = current_user(request, s)
    form = await form_data(request)
    managed = [main_source(s).id] + [src.id for src in s.scalars(select(Source).where(
        Source.kind == "channel", Source.owner_user_id == user.id, Source.active.is_(True)))]
    for acc in _live(user):
        keep = set(services.account_source_ids(s, acc.id)) - set(managed)
        for sid in managed:
            vals = form.get(f"c{sid}", [])
            vals = vals if isinstance(vals, list) else [vals]
            if str(acc.id) in [str(v) for v in vals]:
                keep.add(sid)
        services.set_account_sources(s, acc, list(keep))
    services.log(s, "channel_accounts_saved", user.id, "", user.username)
    s.commit()
    flash(request, "Saved. The next call from each channel goes to the accounts you ticked.")
    return redirect("/channels#copying")


@router.post("/channels/telegram/start")
async def tg_start(request: Request, s=Depends(db_session)):
    user = current_user(request, s)
    form = await form_data(request)
    if not int(feature(user.plan.features, "max_channels") or 0):
        flash(request, "Extra channels come with Pro and Extreme.", "err")
        return redirect("/channels")
    phone = re.sub(r"[^\d+]", "", str(form.get("phone", "")))
    if len(phone) < 8:
        flash(request, "Enter your Telegram phone number with country code, like +447700900123.", "err")
        return redirect("/channels")
    try:
        await tg.start_user_login(user.id, phone)
        flash(request, "Telegram sent you a login code. Enter it below.")
    except Exception as e:  # noqa: BLE001
        flash(request, f"Telegram didn't accept that number: {e}", "err")
    return redirect("/channels")


@router.post("/channels/telegram/code")
async def tg_code(request: Request, s=Depends(db_session)):
    user = current_user(request, s)
    form = await form_data(request)
    try:
        session_str, name, phone = await tg.finish_user_login(user.id, str(form.get("code", "")).strip(),
                                                              str(form.get("password", "")))
    except Exception as e:  # noqa: BLE001
        flash(request, f"Couldn't link Telegram: {e}", "err")
        return redirect("/channels")
    user.tg_session_enc = security.encrypt(session_str)
    user.tg_name, user.tg_phone = name[:128], phone[:32]
    services.log(s, "telegram_linked", user.id, name, user.username)
    s.commit()
    tg.reload_soon()
    flash(request, f"Telegram linked as {name}. Pick the channels to copy.")
    return redirect("/channels")


@router.post("/channels/telegram/unlink")
async def tg_unlink(request: Request, s=Depends(db_session)):
    user = current_user(request, s)
    await form_data(request)
    user.tg_session_enc = ""
    user.tg_name = user.tg_phone = ""
    for src in s.scalars(select(Source).where(Source.kind == "channel", Source.owner_user_id == user.id)):
        src.active = False
    services.log(s, "telegram_unlinked", user.id, "", user.username)
    s.commit()
    tg.reload_soon()
    flash(request, "Telegram unlinked. Your extra channels stopped copying.")
    return redirect("/channels")


@router.post("/channels/add")
async def channels_add(request: Request, s=Depends(db_session)):
    user = current_user(request, s)
    form = await form_data(request)
    max_ch = int(feature(user.plan.features, "max_channels") or 0)
    if services.user_channel_count(s, user) >= max_ch:
        flash(request, f"Your plan allows {max_ch} extra channel(s).", "err")
        return redirect("/channels")
    try:
        chat_id = int(form.get("chat_id", ""))
    except ValueError:
        raise HTTPException(400)
    title = str(form.get("title", "Channel"))[:128]
    src = s.scalar(select(Source).where(Source.kind == "channel", Source.owner_user_id == user.id,
                                        Source.tg_chat_id == chat_id))
    if src:
        src.active, src.title = True, title
    else:
        src = Source(kind="channel", owner_user_id=user.id, tg_chat_id=chat_id, title=title)
        s.add(src)
    s.flush()
    for acc in _live(user):   # follow it on every account by default
        ids = set(services.account_source_ids(s, acc.id)) | {src.id}
        services.set_account_sources(s, acc, list(ids))
    services.log(s, "channel_added", user.id, title, user.username)
    s.commit()
    tg.reload_soon()
    flash(request, f"Copying gold signals from {title}. Turn it off per account in account settings.")
    return redirect("/channels")


@router.post("/channels/{source_id}/remove")
async def channels_remove(source_id: int, request: Request, s=Depends(db_session)):
    user = current_user(request, s)
    await form_data(request)
    src = s.get(Source, source_id)
    if not src or src.owner_user_id != user.id:
        raise HTTPException(404)
    src.active = False
    s.commit()
    tg.reload_soon()
    flash(request, f"Stopped copying {src.title}.")
    return redirect("/channels")


# ------------------------------------------------------------ plan + profile

@router.get("/plan")
def plan_page(request: Request, s=Depends(db_session)):
    user = current_user(request, s)
    pending = s.scalar(select(UpgradeRequest).where(UpgradeRequest.user_id == user.id,
                                                    UpgradeRequest.status == "pending"))
    plans = _plans(s)
    top = bool(plans) and user.plan_id == max(plans, key=lambda p: (p.monthly_price or 0, p.lifetime_price or 0)).id
    from . import customer_alerts, runtime
    return render(request, "plan.html", {"user": user, "plans": plans, "pending": pending, "choose": True, "top": top,
                                         "contact_text": get_setting(s, "contact_text"),
                                         "contact_link": get_setting(s, "contact_link"),
                                         "alerts_ready": bool(runtime.bot_username()),
                                         "alert_prefs": customer_alerts.prefs(user)})


@router.post("/profile/alerts/link")
async def alerts_link(request: Request, s=Depends(db_session)):
    """One tap: straight to the bot in Telegram with a code that links this login."""
    user = current_user(request, s)
    await form_data(request)
    from . import customer_alerts, runtime
    bot = runtime.bot_username()
    if not bot:
        flash(request, "Telegram alerts aren't available yet. Try again later.", "err")
        return redirect("/plan#alerts")
    from fastapi.responses import RedirectResponse
    return RedirectResponse(f"https://t.me/{bot}?start={customer_alerts.new_link_code(user.id)}", status_code=303)


@router.post("/profile/alerts")
async def alerts_save(request: Request, s=Depends(db_session)):
    user = current_user(request, s)
    form = await form_data(request)
    from . import customer_alerts
    action = form.get("action")
    if action == "unlink":
        user.alert_chat_id = None
        services.log(s, "alerts_telegram_unlinked", user.id, "", user.username)
        s.commit()
        flash(request, "Telegram alerts are off.")
        return redirect("/plan#alerts")
    if action == "test":
        if user.alert_chat_id and customer_alerts.queue(s, user, "test", f"test:{user.id}:{int(utcnow().timestamp())}",
                                                        customer_alerts.test_text(user)):
            s.commit()
            tg.wake_bot()
            flash(request, "Test sent. It should reach your Telegram in a few seconds.")
        else:
            flash(request, "Connect Telegram first.", "err")
        return redirect("/plan#alerts")
    user.alert_prefs = customer_alerts.clean_prefs(form)
    s.commit()
    flash(request, "Alert settings saved.")
    return redirect("/plan#alerts")


@router.post("/plan/request")
async def plan_request(request: Request, s=Depends(db_session)):
    user = current_user(request, s)
    form = await form_data(request)
    plan = s.get(Plan, int(form.get("plan_id", 0) or 0))
    bill = "lifetime" if form.get("billing") == "lifetime" else "monthly"
    if not plan or not (plan.monthly_price or plan.lifetime_price):
        raise HTTPException(400)
    if security.too_many_attempts(f"planreq:{user.id}", limit=6, window=3600):
        flash(request, "You've sent a few requests already. Message us on Telegram instead.", "err")
        return redirect("/plan")
    security.record_attempt(f"planreq:{user.id}")
    r = billing.create_request(s, user, plan, bill)
    s.commit()
    flash(request, f"Request PF-{r.id} sent for {plan.name} ({bill}). Pay using the details below and we'll "
                   f"switch it on straight away.")
    return redirect("/plan")


@router.post("/profile/email")
async def change_email(request: Request, s=Depends(db_session)):
    user = current_user(request, s)
    form = await form_data(request)
    email = str(form.get("email", "")).strip()[:255]
    if not re.match(mailer.EMAIL_RE, email):
        flash(request, "That email doesn't look right.", "err")
        return redirect("/plan")
    user.email = email
    services.log(s, "email_changed", user.id, "", user.username)
    s.commit()
    flash(request, "Email saved.")
    return redirect("/plan")


# ------------------------------------------------------------ support

@router.get("/support")
def support_page(request: Request, s=Depends(db_session)):
    user = current_user(request, s)
    tickets = list(s.scalars(select(Ticket).where(Ticket.user_id == user.id).order_by(Ticket.updated_at.desc())))
    return render(request, "support.html", {"user": user, "tickets": tickets,
                                            "accounts": [a for a in user.accounts if not a.removed],
                                            "contact_link": get_setting(s, "contact_link")})


@router.post("/support")
async def support_create(request: Request, s=Depends(db_session)):
    user = current_user(request, s)
    form = await form_data(request)
    subject = str(form.get("subject", "")).strip()
    body = str(form.get("message", "")).strip()[:5000]
    acc = str(form.get("account", "")).strip()
    if not subject or len(body) < 5:
        flash(request, "Add a subject and tell us what's going on.", "err")
        return redirect("/support")
    if security.too_many_attempts(f"support:{user.id}", limit=10, window=3600):
        flash(request, "That's a lot of messages in an hour. We'll get back to you on the ones you've sent.", "err")
        return redirect("/support")
    security.record_attempt(f"support:{user.id}")
    if acc:
        body += f"\n\n(MT5 account: {acc})"
    t = support.create_ticket(s, user, subject, body)
    s.commit()
    flash(request, f"Sent. Ticket #{t.id}: we'll reply here and by email.")
    return redirect(f"/support/{t.id}")


def _ticket(s, user, ticket_id) -> Ticket:
    t = s.get(Ticket, ticket_id)
    if not t or t.user_id != user.id:
        raise HTTPException(404, "Ticket not found")
    return t


@router.get("/support/{ticket_id}")
def support_ticket(ticket_id: int, request: Request, s=Depends(db_session)):
    user = current_user(request, s)
    return render(request, "support_ticket.html", {"user": user, "t": _ticket(s, user, ticket_id)})


@router.post("/support/{ticket_id}")
async def support_reply(ticket_id: int, request: Request, s=Depends(db_session)):
    user = current_user(request, s)
    form = await form_data(request)
    t = _ticket(s, user, ticket_id)
    body = str(form.get("message", "")).strip()[:5000]
    if body:
        support.reply(s, t, "customer", body, from_admin=False)
        s.commit()
        flash(request, "Reply sent.")
    return redirect(f"/support/{t.id}")


@router.get("/help")
def help_page(request: Request, s=Depends(db_session)):
    if request.session.get("uid"):
        return redirect("/support")
    return render(request, "help.html", {"contact_link": get_setting(s, "contact_link")})


@router.post("/help")
async def help_send(request: Request, s=Depends(db_session)):
    form = await form_data(request)
    ip = client_ip(request)
    name = str(form.get("name", "")).strip()[:128]
    email = str(form.get("email", "")).strip()[:255]
    body = str(form.get("message", "")).strip()[:5000]
    if form.get("website"):                 # bots fill in the hidden field
        return redirect("/help?sent=1")
    if not name or not re.match(mailer.EMAIL_RE, email) or len(body) < 5:
        return render(request, "help.html", {"error": "Add your name, email and a message.", "name": name,
                                             "email": email, "message": body}, 400)
    if security.too_many_attempts("help:" + ip, limit=5, window=3600):
        return render(request, "help.html", {"error": "Too many messages from this connection. Try again later."}, 429)
    security.record_attempt("help:" + ip)
    support.create_ticket(s, None, str(form.get("subject", "")).strip() or "Help request", body, name=name, email=email)
    s.commit()
    return redirect("/help?sent=1")


@router.post("/profile/password")
async def change_password(request: Request, s=Depends(db_session)):
    user = current_user(request, s)
    form = await form_data(request)
    if not security.verify_password(str(form.get("current", "")), user.password_hash):
        flash(request, "Your current password is wrong.", "err")
        return redirect("/plan")
    new = str(form.get("new", ""))
    if security.password_problem(new):
        flash(request, security.password_problem(new), "err")
        return redirect("/plan")
    user.password_hash = security.hash_password(new)
    user.session_version += 1
    request.session["sv"] = user.session_version
    s.commit()
    flash(request, "Password changed.")
    return redirect("/plan")

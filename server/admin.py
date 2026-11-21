"""Admin pages: only accounts in the `admins` table can log in (create them with manage.py)."""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, or_, select

from . import billing as billing_mod
from . import mailer, notify, reports, runtime, security, services, support, system
from .customer import USERNAME_RE, _parse_manual
from .db import (Account, Activity, Admin, Deal, Node, Plan, Renewal, Signal, SignalUpdate, Source, Ticket,
                 UpgradeRequest, User, get_setting, main_source, set_setting, utcnow)
from .plans import FEATURES, UNLIMITED, effective_settings
from .web import (FormExpired, client_ip, current_admin, db_session, flash, form_data, form_expired_message,
                  redirect, render)

router = APIRouter(prefix="/admin")


def _ctx(admin, **kw):
    kw["admin"] = admin
    return kw


@router.get("/login")
def login_page(request: Request):
    return render(request, "admin/login.html")


@router.post("/login")
async def login(request: Request, s=Depends(db_session)):
    try:
        form = await form_data(request)
    except FormExpired as e:
        return render(request, "admin/login.html", {"error": form_expired_message(e)}, 400)
    ip = client_ip(request)
    key = "admin:" + ip
    if security.too_many_attempts(key, limit=5, window=900):
        return render(request, "admin/login.html", {"error": "Too many attempts. Wait 15 minutes."}, 429)
    admin = s.scalar(select(Admin).where(Admin.username == str(form.get("username", "")).strip()))
    if not admin or not security.verify_password(str(form.get("password", "")), admin.password_hash):
        security.record_attempt(key)
        return render(request, "admin/login.html", {"error": "Wrong username or password."}, 400)
    security.clear_attempts(key)
    request.session.pop("aid", None)       # a customer login in the same browser stays logged in
    request.session["aid"] = admin.id
    admin.last_login = utcnow()
    services.log(s, "admin_login", None, "", admin.username, ip)
    s.commit()
    return redirect("/admin")


@router.post("/logout")
async def logout(request: Request):
    await form_data(request)
    request.session.pop("aid", None)
    return redirect("/admin/login")


# ------------------------------------------------------------ overview

@router.get("")
def overview(request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    now = utcnow()
    per_plan = s.execute(select(Plan.name, func.count(User.id)).join(User, User.plan_id == Plan.id)
                         .where(User.status != "closed").group_by(Plan.name, Plan.sort).order_by(Plan.sort)).all()
    counts = {
        "users": s.scalar(select(func.count()).select_from(User).where(User.status != "closed")),
        "active_today": s.scalar(select(func.count()).select_from(User).where(User.last_seen >= now - timedelta(days=1))),
        "active_week": s.scalar(select(func.count()).select_from(User).where(User.last_seen >= now - timedelta(days=7))),
        "monthly": s.scalar(select(func.count()).select_from(User).where(User.billing == "monthly", User.status != "closed")),
        "lifetime": s.scalar(select(func.count()).select_from(User).where(User.billing == "lifetime", User.status != "closed")),
        "accounts_online": s.scalar(select(func.count()).select_from(Account).where(Account.conn_status == "online", Account.removed.is_(False))),
        "accounts_error": s.scalar(select(func.count()).select_from(Account).where(Account.conn_status == "error", Account.removed.is_(False))),
        "signals_today": s.scalar(select(func.count()).select_from(Signal).where(Signal.created_at >= now.replace(hour=0, minute=0, second=0))),
    }
    renewals = list(s.scalars(select(Renewal).where(Renewal.status == "pending").order_by(Renewal.due)))
    upcoming = list(s.scalars(select(User).where(User.billing == "monthly", User.status == "active",
                                                 User.paid_until <= now + timedelta(days=7)).order_by(User.paid_until)))
    recent = list(s.scalars(select(Activity).order_by(Activity.id.desc()).limit(15)))
    cap = services.capacity(s)
    from . import telegram_service as tg
    st = tg.status()
    setup = [
        ("Telegram signal reader connected", st["main"] == "connected", "/admin/telegram"),
        ("Admin bot connected", st["bot"] == "connected", "/admin/telegram"),
        ("Your Telegram linked for alerts", bool(admin.telegram_chat_id), "/admin/telegram"),
        ("A VPS online", cap["online"] > 0, "/admin/nodes"),
        ("Every VPS logs itself in after a restart",
         bool(cap["nodes"]) and all((n.info or {}).get("autologon") for n in cap["nodes"] if services.node_online(n)),
         "/admin/nodes"),
        ("Website on your domain", system.tunnel_status() == "running" or not system.IS_WINDOWS, "/admin/system"),
    ]
    plan_requests = list(s.scalars(select(UpgradeRequest).where(UpgradeRequest.status == "pending")
                                   .order_by(UpgradeRequest.id)))
    open_tickets = s.scalar(select(func.count()).select_from(Ticket).where(Ticket.status == "open")) or 0
    setup.append(("Email set up (reminders + support)", mailer.configured(s), "/admin/settings"))
    return render(request, "admin/overview.html", _ctx(admin, per_plan=per_plan, counts=counts, cap=cap,
                  plan_requests=plan_requests, open_tickets=open_tickets,
                  renewals=renewals, upcoming=upcoming, recent=recent, notes=list(notify.recent)[:8],
                  setup=setup, setup_left=sum(1 for _, ok, _ in setup if not ok)))


# ------------------------------------------------------------ users

@router.get("/users")
def users(request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    q = select(User).order_by(User.id.desc())
    term = request.query_params.get("q", "").strip()
    status = request.query_params.get("status", "")
    plan = request.query_params.get("plan", "")
    if term:
        q = q.where(or_(User.username.ilike(f"%{term}%"), User.email.ilike(f"%{term}%")))
    if status:
        q = q.where(User.status == status)
    if plan:
        q = q.join(Plan).where(Plan.key == plan)
    rows = list(s.scalars(q.limit(500)))
    activity = {u.id: services.activity_summary(s, u) for u in rows}
    return render(request, "admin/users.html", _ctx(admin, users=rows, activity=activity,
                  plans=list(s.scalars(select(Plan).order_by(Plan.sort))), q=term, status=status, plan=plan,
                  time_limits=billing_mod.TIME_LIMITS))


def _billing_from_form(form) -> tuple[str, datetime | None]:
    billing = form.get("billing") if form.get("billing") in ("free", "monthly", "lifetime") else "free"
    paid_until = None
    if billing == "monthly" and form.get("paid_until"):
        try:
            paid_until = datetime.strptime(form["paid_until"], "%Y-%m-%d").replace(hour=12)
        except ValueError:
            paid_until = None
    return billing, paid_until


@router.post("/users/create")
async def users_create(request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    form = await form_data(request)
    username = str(form.get("username", "")).strip()
    if not USERNAME_RE.match(username):
        flash(request, "Usernames are 3–32 letters, numbers, dots, dashes or underscores.", "err")
        return redirect("/admin/users")
    if s.scalar(select(User).where(func.lower(User.username) == username.lower())):
        flash(request, f"{username} already exists.", "err")
        return redirect("/admin/users")
    plan = s.get(Plan, int(form.get("plan_id", 0) or 0))
    if not plan:
        raise HTTPException(400)
    password = str(form.get("password", "")).strip() or security.new_password()
    billing, paid_until = _billing_from_form(form)
    user = User(username=username, email=str(form.get("email", "")).strip()[:255],
                password_hash=security.hash_password(password), plan_id=plan.id, plan=plan,
                created_by=admin.username)
    changed, expires = billing_mod.limit_from_form(form)
    user.expires_at = expires if changed else None
    s.add(user)
    s.flush()
    services.set_plan(s, user, plan, billing, paid_until, admin.username)
    services.log(s, "user_created", user.id, f"{plan.name}/{billing}" + (f", until {expires:%d %b %Y}" if expires else ""),
                 admin.username, client_ip(request))
    s.commit()
    request.session["new_login"] = [username, password]
    return redirect(f"/admin/users/{user.id}")


def _user(s, user_id) -> User:
    u = s.get(User, user_id)
    if not u:
        raise HTTPException(404)
    return u


@router.get("/users/{user_id}")
def user_page(user_id: int, request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    user = _user(s, user_id)
    accounts = [a for a in user.accounts]
    ids = [a.id for a in accounts]
    acts = list(s.scalars(select(Activity).where(Activity.user_id == user.id).order_by(Activity.id.desc()).limit(40)))
    renewals = list(s.scalars(select(Renewal).where(Renewal.user_id == user.id).order_by(Renewal.id.desc())))
    channels = list(s.scalars(select(Source).where(Source.owner_user_id == user.id, Source.kind == "channel")))
    new_login = request.session.pop("new_login", None)
    reqs = list(s.scalars(select(UpgradeRequest).where(UpgradeRequest.user_id == user.id)
                          .order_by(UpgradeRequest.id.desc()).limit(10)))
    return render(request, "admin/user.html", _ctx(admin, user=user, accounts=accounts,
                  stats=services.stats(s, ids), summary=services.activity_summary(s, user), acts=acts,
                  renewals=renewals, channels=channels, new_login=new_login, requests=reqs,
                  time_limits=billing_mod.TIME_LIMITS, plans=list(s.scalars(select(Plan).order_by(Plan.sort)))))


@router.post("/users/{user_id}/plan")
async def user_plan(user_id: int, request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    form = await form_data(request)
    user = _user(s, user_id)
    plan = s.get(Plan, int(form.get("plan_id", 0) or 0))
    if not plan:
        raise HTTPException(400)
    billing, paid_until = _billing_from_form(form)
    services.set_plan(s, user, plan, billing, paid_until, admin.username)
    changed, expires = billing_mod.limit_from_form(form)
    if changed:
        user.expires_at = expires
        user.reminders_sent = {}
        if expires and user.status == "closed" and expires > utcnow():
            services.reopen_user(s, user, admin.username)
        services.log(s, "time_limit_set", user.id, f"until {expires:%d %b %Y}" if expires else "no limit", admin.username)
    s.commit()
    notify.users_changed()
    flash(request, f"{user.username} is now on {plan.name} ({billing})"
                   + (f", access until {user.expires_at:%d %b %Y}." if user.expires_at else "."))
    return redirect(f"/admin/users/{user.id}")


@router.post("/users/{user_id}/remind")
async def user_remind(user_id: int, request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    await form_data(request)
    user = _user(s, user_id)
    if billing_mod.send_reminder_now(s, user):
        services.log(s, "reminder_sent", user.id, "manual", admin.username)
        s.commit()
        flash(request, f"Payment reminder queued to {user.email}."
                       + ("" if mailer.configured(s) else " Set up email in Settings so it actually sends."))
    else:
        flash(request, "They have no email address.", "err")
    return redirect(f"/admin/users/{user.id}")


# ------------------------------------------------------------ plan requests

@router.post("/requests/{request_id}/{answer}")
async def request_answer(request_id: int, answer: str, request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    await form_data(request)
    r = s.get(UpgradeRequest, request_id)
    if not r or answer not in ("approve", "decline"):
        raise HTTPException(404)
    msg = billing_mod.apply_request(s, r, answer == "approve", admin.username)
    s.commit()
    notify.users_changed()
    flash(request, msg)
    return redirect(request.headers.get("referer") or "/admin")


# ------------------------------------------------------------ support

@router.get("/support")
def support_list(request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    show = request.query_params.get("show", "open")
    q = select(Ticket).order_by(Ticket.updated_at.desc())
    if show == "open":
        q = q.where(Ticket.status == "open")
    elif show == "answered":
        q = q.where(Ticket.status == "answered")
    return render(request, "admin/support.html", _ctx(admin, tickets=list(s.scalars(q.limit(300))), show=show,
                                                      support_email=support.support_address(s),
                                                      mail_ok=mailer.configured(s)))


@router.get("/support/{ticket_id}")
def support_view(ticket_id: int, request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    t = s.get(Ticket, ticket_id)
    if not t:
        raise HTTPException(404)
    return render(request, "admin/support_ticket.html", _ctx(admin, t=t, mail_ok=mailer.configured(s)))


@router.post("/support/{ticket_id}")
async def support_answer(ticket_id: int, request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    form = await form_data(request)
    t = s.get(Ticket, ticket_id)
    if not t:
        raise HTTPException(404)
    if form.get("action") == "close":
        t.status = "closed"
        flash(request, f"#{t.id} closed.")
    elif form.get("action") == "reopen":
        t.status = "open"
    else:
        body = str(form.get("message", "")).strip()[:8000]
        if body:
            support.reply(s, t, admin.username, body, from_admin=True)
            flash(request, "Reply sent" + (" and emailed." if t.email and mailer.configured(s) else "."))
        if form.get("close"):
            t.status = "closed"
    s.commit()
    return redirect(f"/admin/support/{t.id}")


@router.post("/users/{user_id}/password")
async def user_password(user_id: int, request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    form = await form_data(request)
    user = _user(s, user_id)
    password = str(form.get("password", "")).strip() or security.new_password()
    user.password_hash = security.hash_password(password)
    user.session_version += 1
    services.log(s, "password_reset", user.id, "", admin.username)
    s.commit()
    request.session["new_login"] = [user.username, password]
    return redirect(f"/admin/users/{user.id}")


@router.post("/users/{user_id}/close")
async def user_close(user_id: int, request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    form = await form_data(request)
    user = _user(s, user_id)
    services.close_user(s, user, admin.username, str(form.get("reason", "cancelled by admin")))
    for r in s.scalars(select(Renewal).where(Renewal.user_id == user.id, Renewal.status == "pending")):
        r.status, r.answered_by, r.answered_at = "not_paid", admin.username, utcnow()
    s.commit()
    flash(request, f"{user.username} closed. They can't log in and copying has stopped.")
    return redirect(f"/admin/users/{user.id}")


@router.post("/users/{user_id}/reopen")
async def user_reopen(user_id: int, request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    await form_data(request)
    user = _user(s, user_id)
    services.reopen_user(s, user, admin.username)
    services.assign_accounts(s)
    s.commit()
    flash(request, f"{user.username} reopened.")
    return redirect(f"/admin/users/{user.id}")


@router.post("/users/{user_id}/notes")
async def user_notes(user_id: int, request: Request, s=Depends(db_session)):
    current_admin(request, s)
    form = await form_data(request)
    user = _user(s, user_id)
    user.notes = str(form.get("notes", ""))[:5000]
    user.email = str(form.get("email", user.email)).strip()[:255]
    s.commit()
    flash(request, "Saved.")
    return redirect(f"/admin/users/{user.id}")


# ------------------------------------------------------------ customer MT5 accounts

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xlsx_response(data: bytes, name: str):
    from fastapi.responses import Response
    return Response(data, media_type=XLSX, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/accounts")
def accounts_list(request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    q = request.query_params.get("q", "").strip()
    status = request.query_params.get("status", "")
    sort = request.query_params.get("sort", "id")
    rows = reports.account_rows(s, term=q, status=status)
    keys = {"balance": lambda r: -r["balance"], "today": lambda r: -r["pnl"]["today"]["total"],
            "month": lambda r: -r["pnl"]["month"]["total"], "all": lambda r: -r["pnl"]["all"]["total"],
            "worst": lambda r: r["pnl"]["month"]["total"], "id": lambda r: -r["id"]}
    rows.sort(key=keys.get(sort, keys["id"]))
    return render(request, "admin/accounts.html", _ctx(admin, rows=rows, tots=reports.totals(rows), q=q,
                  status=status, sort=sort, vip=reports.vip_groups(s)))


@router.get("/accounts.xlsx")
def accounts_download(request: Request, s=Depends(db_session)):
    current_admin(request, s)
    return _xlsx_response(reports.accounts_xlsx(s), f"PipsFarmer accounts {utcnow():%Y-%m-%d}.xlsx")


@router.get("/accounts/{account_id}")
def account_page(account_id: int, request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    acc = s.get(Account, account_id)
    if not acc:
        raise HTTPException(404)
    deals = list(s.scalars(select(Deal).where(Deal.account_id == acc.id, Deal.entry.in_(("out", "inout")))
                           .order_by(Deal.time.desc()).limit(100)))
    return render(request, "admin/account.html", _ctx(admin, acc=acc, stats=services.stats(s, [acc.id]),
                  deals=deals, curve=services.equity_curve(s, acc.id), live=acc.live_state or {},
                  effective=effective_settings(acc.settings, acc.user.plan.features),
                  sources=[s.get(Source, i) for i in services.account_source_ids(s, acc.id)]))


@router.post("/accounts/{account_id}/action")
async def account_action(account_id: int, request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    form = await form_data(request)
    acc = s.get(Account, account_id)
    if not acc:
        raise HTTPException(404)
    action = form.get("action")
    if action == "toggle":
        acc.copy_enabled = not acc.copy_enabled
        flash(request, "Copying " + ("on." if acc.copy_enabled else "paused."))
    elif action == "close_all":
        services.command(s, acc.id, "close_all")
        flash(request, "Close-all sent.")
    elif action == "restart":
        services.command(s, acc.id, "restart")
        flash(request, "Restart sent.")
    elif action == "requeue":
        acc.node_id = None
        acc.waiting_since = utcnow()
        services.assign_accounts(s)
        flash(request, "Account moved back into the queue.")
    services.log(s, f"admin_{action}", acc.user_id, acc.login, admin.username)
    s.commit()
    notify.users_changed()
    return redirect(f"/admin/accounts/{acc.id}")


# ------------------------------------------------------------ renewals

@router.get("/renewals")
def renewals(request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    pending = list(s.scalars(select(Renewal).where(Renewal.status == "pending").order_by(Renewal.due)))
    done = list(s.scalars(select(Renewal).where(Renewal.status != "pending").order_by(Renewal.answered_at.desc()).limit(50)))
    upcoming = list(s.scalars(select(User).where(User.billing == "monthly", User.status == "active")
                              .order_by(User.paid_until).limit(100)))
    return render(request, "admin/renewals.html", _ctx(admin, pending=pending, done=done, upcoming=upcoming))


@router.post("/renewals/{renewal_id}/{answer}")
async def renewal_answer(renewal_id: int, answer: str, request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    await form_data(request)
    r = s.get(Renewal, renewal_id)
    if not r or answer not in ("paid", "not_paid"):
        raise HTTPException(404)
    msg = services.answer_renewal(s, r, answer == "paid", admin.username)
    s.commit()
    from . import telegram_service as tg
    tg.renewal_answered(r.id, msg)
    notify.users_changed()
    flash(request, msg)
    return redirect(request.headers.get("referer") or "/admin/renewals")


# ------------------------------------------------------------ VPS nodes

@router.get("/nodes")
def nodes(request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    cap = services.capacity(s)
    waiting = list(s.scalars(select(Account).where(Account.node_id.is_(None), Account.removed.is_(False),
                                                   Account.waiting_since.is_not(None)).order_by(Account.waiting_since)))
    new_token = request.session.pop("new_node_token", None)
    install = request.session.pop("new_node_install", None)
    base = runtime.public_url() or str(request.base_url).rstrip("/")
    _, pkg_hash = system.node_package()
    return render(request, "admin/nodes.html", _ctx(admin, cap=cap, waiting=waiting, new_token=new_token,
                                                     online=services.node_online, install=install, base=base,
                                                     pkg_hash=pkg_hash,
                                                     local_base=base.startswith("http://127.") or "localhost" in base))


@router.post("/nodes/create")
async def nodes_create(request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    form = await form_data(request)
    token = security.new_token()
    node = Node(name=str(form.get("name", "VPS")).strip()[:64] or "VPS", token_hash=security.hash_token(token),
                max_slots=max(1, min(200, int(form.get("max_slots", 10) or 10))))
    s.add(node)
    s.flush()
    code = services.new_install_code(s, node, token)
    services.log(s, "node_created", None, node.name, admin.username)
    s.commit()
    request.session["new_node_token"] = [node.name, token]
    request.session["new_node_install"] = [node.name, code]
    return redirect("/admin/nodes")


@router.post("/nodes/{node_id}")
async def nodes_edit(node_id: int, request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    form = await form_data(request)
    node = s.get(Node, node_id)
    if not node:
        raise HTTPException(404)
    action = form.get("action")
    if action == "token":
        token = security.new_token()
        node.token_hash = security.hash_token(token)
        request.session["new_node_token"] = [node.name, token]
        request.session["new_node_install"] = [node.name, services.new_install_code(s, node, token)]
    elif action in ("reboot", "restart_all", "restart_node"):
        services.queue_node_command(node.id, action)
        flash(request, {"reboot": f"{node.name} restarts Windows within a minute. MT5 comes back by itself.",
                        "restart_all": f"Restarting every MT5 on {node.name}.",
                        "restart_node": f"Restarting the node agent on {node.name}."}[action])
    elif action == "autologon":
        pw = str(form.get("win_password", ""))
        if not pw:
            flash(request, "Enter the VPS's Windows password.", "err")
            return redirect("/admin/nodes")
        services.queue_node_command(node.id, "autologon", user=str(form.get("win_user", "")).strip(), domain="",
                                    password=pw)
        flash(request, f"Sent to {node.name}. Its status updates within a minute.")
    else:
        node.name = str(form.get("name", node.name)).strip()[:64] or node.name
        node.max_slots = max(0, min(200, int(form.get("max_slots", node.max_slots) or 0)))
        node.enabled = form.get("enabled") == "on"
        if not node.enabled:
            for a in s.scalars(select(Account).where(Account.node_id == node.id)):
                a.node_id, a.waiting_since, a.conn_status = None, utcnow(), "waiting"
        flash(request, f"{node.name} saved.")
    services.assign_accounts(s)
    services.log(s, "node_edited", None, node.name, admin.username)
    s.commit()
    notify.users_changed()
    return redirect("/admin/nodes")


# ------------------------------------------------------------ plans

@router.get("/plans")
def plans(request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    return render(request, "admin/plans.html", _ctx(admin, plans=list(s.scalars(select(Plan).order_by(Plan.sort))),
                                                    UNLIMITED=UNLIMITED))


@router.post("/plans/{plan_id}")
async def plans_save(plan_id: int, request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    form = await form_data(request)
    plan = s.get(Plan, plan_id)
    if not plan:
        raise HTTPException(404)
    plan.name = str(form.get("name", plan.name)).strip()[:64] or plan.name
    for k in ("monthly_price", "lifetime_price"):
        try:
            setattr(plan, k, max(0.0, float(form.get(k, getattr(plan, k)) or 0)))
        except ValueError:
            pass
    plan.currency = str(form.get("currency", plan.currency))[:8]
    feats = dict(plan.features or {})
    for f in FEATURES:
        if f["type"] == "int":
            try:
                feats[f["key"]] = max(0, min(UNLIMITED, int(form.get(f["key"], feats.get(f["key"], 0)) or 0)))
            except ValueError:
                pass
        else:
            feats[f["key"]] = form.get(f["key"]) == "on"
    plan.features = feats
    for u in s.scalars(select(User).where(User.plan_id == plan.id)):
        services.trim_to_plan(s, u)
    services.log(s, "plan_edited", None, plan.name, admin.username)
    s.commit()
    flash(request, f"{plan.name} saved. Changes apply to every customer on it straight away.")
    return redirect("/admin/plans")


# ------------------------------------------------------------ signals

@router.get("/signals")
def signals(request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    channel = request.query_params.get("channel", "")
    taken = request.query_params.get("taken") == "1"
    since = utcnow() - timedelta(days=90)
    every = reports.signal_rows(s, since=since)
    rows = [r for r in every if (not channel or r["channel"] == channel) and (not taken or r["accounts"])][:150]
    updates: dict[int, list] = {}
    for u in s.scalars(select(SignalUpdate).where(SignalUpdate.signal_id.in_([r["id"] for r in rows] or [-1]))
                       .order_by(SignalUpdate.id)):
        updates.setdefault(u.signal_id, []).append(u)
    sources = list(s.scalars(select(Source).where(Source.kind.in_(("main", "channel")))))
    from . import telegram_service as tg
    return render(request, "admin/signals.html", _ctx(admin, rows=rows, updates=updates, sources=sources,
                  channels=reports.channel_summary(every), channel=channel, taken=taken,
                  main=main_source(s), tg_status=tg.status(), vip=reports.vip_groups(s)))


@router.get("/signals.xlsx")
def signals_download(request: Request, s=Depends(db_session)):
    current_admin(request, s)
    return _xlsx_response(reports.signals_xlsx(s), f"PipsFarmer signals {utcnow():%Y-%m-%d}.xlsx")


@router.post("/signals/manual")
async def signals_manual(request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    form = await form_data(request)
    p = _parse_manual(form)
    if isinstance(p, str):
        flash(request, p, "err")
        return redirect("/admin/signals")
    sig = services.create_signal(s, main_source(s), p, raw="manual (admin)", created_by=f"admin:{admin.username}")
    services.log(s, "admin_signal", None, f"#{sig.id} {p.side} {p.entry_low}-{p.entry_high}", admin.username)
    s.commit()
    flash(request, f"Signal #{sig.id} sent to every account following {main_source(s).title}.")
    return redirect("/admin/signals")


@router.post("/signals/{signal_id}/update")
async def signals_update(signal_id: int, request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    form = await form_data(request)
    sig = s.get(Signal, signal_id)
    if not sig:
        raise HTTPException(404)
    kind = form.get("kind")
    if kind not in ("tp_hit", "breakeven", "close", "close_partial", "cancel", "sl_move"):
        raise HTTPException(400)
    value = None
    try:
        if kind in ("sl_move", "tp_hit"):
            value = float(form.get("value", ""))
    except ValueError:
        flash(request, "Enter a number.", "err")
        return redirect("/admin/signals")
    if kind == "close_partial":
        value = 0.5
    services.add_update(s, sig, kind, value, "manual (admin)", f"admin:{admin.username}")
    s.commit()
    flash(request, f"Update sent for signal #{sig.id}.")
    return redirect("/admin/signals")


# ------------------------------------------------------------ activity + settings

@router.get("/activity")
def activity(request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    rows = list(s.scalars(select(Activity).order_by(Activity.id.desc()).limit(300)))
    names = dict(s.execute(select(User.id, User.username)).all())
    return render(request, "admin/activity.html", _ctx(admin, rows=rows, names=names))


@router.get("/settings")
def settings(request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    keys = ("allow_signup", "contact_text", "contact_link", "capacity_alert_free_slots", "support_email",
            "reminder_emails", "reminder_days", "smtp_host", "smtp_port", "smtp_user", "smtp_from_name")
    values = {k: get_setting(s, k) for k in keys}
    return render(request, "admin/settings.html", _ctx(admin, values=values, admins=list(s.scalars(select(Admin))),
                                                       has_smtp_pw=bool(get_setting(s, "smtp_password_enc")),
                                                       mail_ok=mailer.configured(s)))


@router.post("/settings/email")
async def settings_email(request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    form = await form_data(request)
    if form.get("action") == "test":
        to = str(form.get("test_to", "")).strip() or get_setting(s, "support_email")
        try:
            mailer.send_test(s, to)
            flash(request, f"Test email sent to {to}. Check the inbox (and spam).")
        except Exception as e:  # noqa: BLE001
            flash(request, f"Couldn't send: {e}", "err")
        return redirect("/admin/settings#email")
    set_setting(s, "smtp_host", str(form.get("smtp_host", "")).strip()[:128])
    try:
        set_setting(s, "smtp_port", int(form.get("smtp_port", 587) or 587))
    except ValueError:
        pass
    set_setting(s, "smtp_user", str(form.get("smtp_user", "")).strip()[:255])
    set_setting(s, "smtp_from_name", str(form.get("smtp_from_name", "")).strip()[:64])
    if form.get("smtp_password"):
        set_setting(s, "smtp_password_enc", security.encrypt(str(form["smtp_password"]).replace(" ", "")))
    sup = str(form.get("support_email", "")).strip()
    if sup and not re.match(mailer.EMAIL_RE, sup):
        flash(request, "The support email doesn't look right.", "err")
        return redirect("/admin/settings#email")
    set_setting(s, "support_email", sup)
    set_setting(s, "reminder_emails", form.get("reminder_emails") == "on")
    days = ",".join(d for d in re.split(r"[,\s]+", str(form.get("reminder_days", ""))) if d.isdigit())
    set_setting(s, "reminder_days", days or "3,1")
    services.log(s, "email_settings", None, "", admin.username)
    s.commit()
    flash(request, "Email settings saved.")
    return redirect("/admin/settings#email")


@router.post("/settings")
async def settings_save(request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    form = await form_data(request)
    set_setting(s, "allow_signup", form.get("allow_signup") == "on")
    set_setting(s, "contact_text", str(form.get("contact_text", ""))[:1000])
    link = str(form.get("contact_link", "")).strip()[:300]
    set_setting(s, "contact_link", link if re.match(r"^(https?://|tg://|$)", link) else "")
    try:
        set_setting(s, "capacity_alert_free_slots", max(0, int(form.get("capacity_alert_free_slots", 2))))
    except ValueError:
        pass
    if form.get("password_new"):
        if not security.verify_password(str(form.get("password_current", "")), admin.password_hash):
            flash(request, "Current password is wrong.", "err")
            return redirect("/admin/settings")
        if security.password_problem(form["password_new"]):
            flash(request, security.password_problem(form["password_new"]), "err")
            return redirect("/admin/settings")
        admin.password_hash = security.hash_password(form["password_new"])
    services.log(s, "settings_saved", None, "", admin.username)
    s.commit()
    flash(request, "Settings saved.")
    return redirect("/admin/settings")


# ------------------------------------------------------------ Telegram (reader account + admin bot)

@router.get("/telegram")
async def telegram_page(request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    from . import telegram_service as tg
    api_id, api_hash = runtime.tg_api()
    dialogs, dialog_error = [], ""
    if runtime.main_session() and tg.api_ready():
        try:
            dialogs = await tg.list_main_dialogs()
        except Exception as e:  # noqa: BLE001
            dialog_error = str(e)
    link = request.session.pop("tg_link", None)
    return render(request, "admin/telegram.html", _ctx(
        admin, st=tg.status(), api_id=api_id, has_hash=bool(api_hash), has_session=bool(runtime.main_session()),
        pending=tg.pending_login_phone(tg.MAIN_KEY), dialogs=dialogs, dialog_error=dialog_error,
        vip=[str(c) for c in runtime.vip_chats()], bot_user=runtime.bot_username(), has_bot=bool(runtime.bot_token()),
        admins=list(s.scalars(select(Admin))), link=link, rules=get_setting(s, "group_rules") or {},
        vip_titles=_vip_titles(s, dialogs)))


def _vip_titles(s, dialogs) -> dict:
    """Names for the ticked VIP groups: from Telegram when it answers, otherwise from their signals."""
    names = {str(d.id): d.title for d in dialogs}
    for chat, channel in s.execute(select(Signal.tg_chat_id, Signal.channel).where(Signal.tg_chat_id.is_not(None))
                                   .group_by(Signal.tg_chat_id)).all():
        names.setdefault(str(chat), channel)
    return {str(c): names.get(str(c), str(c)) for c in runtime.vip_chats()}


@router.post("/telegram/rules")
async def telegram_rules(request: Request, s=Depends(db_session)):
    """House rules per VIP group: what 'risk free' / 'move SL to <price>' means when copying it."""
    admin = current_admin(request, s)
    form = await form_data(request)
    rules = {}
    for chat in (str(c) for c in runtime.vip_chats()):
        try:
            lock = max(0.0, min(500.0, float(form.get(f"lock_{chat}") or 0)))
        except ValueError:
            lock = 0.0
        as_be = form.get(f"asbe_{chat}") == "on"
        if lock or as_be:
            rules[chat] = {"be_lock_pips": lock, "sl_move_as_be": as_be}
    set_setting(s, "group_rules", rules)
    services.log(s, "group_rules_set", None, str(rules)[:200], admin.username)
    s.commit()
    flash(request, "Group rules saved. They apply to the next breakeven or stop move.")
    return redirect("/admin/telegram#rules")


@router.post("/telegram/api")
async def telegram_api(request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    form = await form_data(request)
    api_id = re.sub(r"\D", "", str(form.get("api_id", "")))
    api_hash = str(form.get("api_hash", "")).strip()
    if not api_id or not re.fullmatch(r"[0-9a-f]{32}", api_hash):
        flash(request, "The API ID is a number and the API hash is 32 letters/numbers, both from my.telegram.org.", "err")
        return redirect("/admin/telegram")
    runtime.save(tg_api_id=int(api_id), tg_api_hash=api_hash)
    services.log(s, "telegram_api_saved", None, "", admin.username)
    s.commit()
    from . import telegram_service as tg
    tg.reload_main()
    tg.reload_soon()
    flash(request, "API keys saved. Now log in the Telegram account that's in your VIP group.")
    return redirect("/admin/telegram")


@router.post("/telegram/main/start")
async def telegram_main_start(request: Request, s=Depends(db_session)):
    current_admin(request, s)
    form = await form_data(request)
    from . import telegram_service as tg
    phone = re.sub(r"[^\d+]", "", str(form.get("phone", "")))
    if len(phone) < 8:
        flash(request, "Enter the phone number with country code, like +447700900123.", "err")
        return redirect("/admin/telegram")
    try:
        await tg.start_main_login(phone)
        flash(request, "Telegram sent a login code to that account (check the Telegram app). Enter it below.")
    except Exception as e:  # noqa: BLE001
        flash(request, f"Telegram didn't accept that: {e}", "err")
    return redirect("/admin/telegram")


@router.post("/telegram/main/code")
async def telegram_main_code(request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    form = await form_data(request)
    from . import telegram_service as tg
    try:
        name = await tg.finish_main_login(str(form.get("code", "")).strip(), str(form.get("password", "")))
    except Exception as e:  # noqa: BLE001
        flash(request, f"Couldn't log in: {e}", "err")
        return redirect("/admin/telegram")
    services.log(s, "telegram_reader_login", None, name, admin.username)
    s.commit()
    flash(request, f"Reader logged in as {name}. Now pick your VIP group.")
    return redirect("/admin/telegram")


@router.post("/telegram/main/logout")
async def telegram_main_logout(request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    await form_data(request)
    runtime.save(tg_main_session_enc="")
    services.log(s, "telegram_reader_logout", None, "", admin.username)
    s.commit()
    from . import telegram_service as tg
    tg.reload_main()
    flash(request, "Reader account disconnected.")
    return redirect("/admin/telegram")


@router.post("/telegram/vip")
async def telegram_vip(request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    form = await form_data(request)
    chosen = form.get("chat_id", [])
    chosen = chosen if isinstance(chosen, list) else [chosen]
    chats = [c for c in (str(x).strip() for x in chosen) if re.fullmatch(r"-?\d+", c)]
    if not chats:
        flash(request, "Pick at least one group.", "err")
        return redirect("/admin/telegram")
    runtime.save(tg_vip_chats=chats)
    services.log(s, "telegram_vip_set", None, ",".join(chats), admin.username)
    s.commit()
    from . import telegram_service as tg
    tg.reload_main()
    flash(request, "Saved. Signals posted there are copied from now on.")
    return redirect("/admin/telegram")


@router.post("/telegram/bot")
async def telegram_bot(request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    form = await form_data(request)
    from . import telegram_service as tg
    token = str(form.get("token", "")).strip()
    try:
        username = await tg.check_bot_token(token)
    except Exception as e:  # noqa: BLE001
        flash(request, f"Telegram didn't accept that bot token: {e}", "err")
        return redirect("/admin/telegram")
    runtime.save(tg_bot_token_enc=security.encrypt(token), tg_bot_username=username)
    services.log(s, "telegram_bot_saved", None, username, admin.username)
    s.commit()
    flash(request, f"Bot @{username} connected. Now link your Telegram to it below.")
    return redirect("/admin/telegram")


@router.post("/telegram/link")
async def telegram_link(request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    await form_data(request)
    from . import telegram_service as tg
    if not runtime.bot_username():
        flash(request, "Connect the bot first.", "err")
        return redirect("/admin/telegram")
    request.session["tg_link"] = f"https://t.me/{runtime.bot_username()}?start={tg.new_link_code(admin.id)}"
    return redirect("/admin/telegram")


@router.post("/telegram/test")
async def telegram_test(request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    await form_data(request)
    from . import telegram_service as tg
    if not admin.telegram_chat_id:
        flash(request, "Link your Telegram first.", "err")
        return redirect("/admin/telegram")
    notify.send_admins(f"✅ Test from PipsFarmer. Alerts reach you, {admin.username}.", chats=[admin.telegram_chat_id])
    tg.wake_bot()
    flash(request, "Test message sent. It should arrive in a few seconds.")
    return redirect("/admin/telegram")


# ------------------------------------------------------------ system: updates, domain, backups

@router.get("/system")
def system_page(request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    keys = ("daily_summary", "summary_hour_utc", "backup_to_telegram", "weekend_restarts")
    values = {k: get_setting(s, k) for k in keys}
    backups = sorted(system.BACKUP_DIR.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True)[:12] \
        if system.BACKUP_DIR.exists() else []
    from . import github_update as gh
    return render(request, "admin/system.html", _ctx(
        admin, version=system.VERSION, update_log=system.update_log(), tunnel=system.tunnel_status(),
        gh_hint=gh.token_hint(), gh_check=gh.last_check(), gh_error=gh.last_error(),
        gh_installed=gh.installed_commit(), gh_repo=gh.REPO, gh_mode=_update_mode(s),
        public_url=runtime.public_url(), values=values, backups=backups, is_windows=system.IS_WINDOWS,
        server_log=system.tail(system.LOG_DIR / "server.log", 80), admins=list(s.scalars(select(Admin))),
        icon_v=_icon_version(), icon_custom=_icon_custom(), protection=_protection(s), research=_research()))


def _research() -> dict:
    from . import research
    return research.status()


def _update_mode(s) -> str:
    from . import scheduler
    return scheduler.update_mode(s)


def _protection(s):
    from . import scheduler
    local = [n for n in s.scalars(select(Node)) if scheduler.is_this_vps(n)]
    autologon = (local[0].info or {}).get("autologon") if local else None
    return system.restart_protection(autologon)


def _icon_version() -> str:
    from . import site_icon
    return site_icon.version()


def _icon_custom() -> bool:
    from . import site_icon
    return site_icon.custom() is not None


@router.post("/system/update")
async def system_update(request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    form = await form_data(request)
    up = form.get("zip")
    data = await up.read() if hasattr(up, "read") else b""
    if not data:
        flash(request, "Choose the pipsfarmer zip first.", "err")
        return redirect("/admin/system")
    if len(data) > 50_000_000:
        flash(request, "That file is too big for a PipsFarmer update.", "err")
        return redirect("/admin/system")
    try:
        root = system.stage_update(data)
        system.backup_db("pre-update", keep=10)
        system.run_update(root)
    except Exception as e:  # noqa: BLE001
        flash(request, f"Update not started: {e}", "err")
        return redirect("/admin/system")
    services.log(s, "update_started", None, root.name, admin.username)
    s.commit()
    notify.send_admins(f"⬆️ {admin.username} started a PipsFarmer update. The site is back in about a minute; "
                       f"other VPS update themselves after that.")
    return render(request, "message.html", {"title": "Updating PipsFarmer",
                                            "text": "The site restarts in about a minute. Refresh this page then. "
                                                    "Other VPS update themselves within a few minutes after that."})


@router.post("/system/github/token")
async def system_github_token(request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    form = await form_data(request)
    from . import github_update as gh
    if form.get("action") == "remove":
        gh.remove_token()
        services.log(s, "github_token_removed", None, "", admin.username)
        s.commit()
        flash(request, "GitHub token removed.")
        return redirect("/admin/system")
    err = gh.save_token(str(form.get("token", "")))
    if err:
        flash(request, err, "err")
        return redirect("/admin/system")
    services.log(s, "github_token_saved", None, "", admin.username)
    s.commit()
    flash(request, "GitHub token saved. Press “Check for updates” to try it.")
    return redirect("/admin/system")


@router.post("/system/github/auto")
async def system_github_auto(request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    form = await form_data(request)
    from .scheduler import UPDATE_MODES
    mode = form.get("mode") if form.get("mode") in UPDATE_MODES else "auto"
    set_setting(s, "gh_update_mode", mode)
    services.log(s, "github_update_mode", None, mode, admin.username)
    s.commit()
    flash(request, "Updates you've agreed with Claude now install by themselves within about 5 minutes."
          if mode == "auto" else "GitHub isn't checked any more. Use “Check for updates” when you want one.")
    return redirect("/admin/system#github")


@router.post("/system/research")
async def system_research(request: Request, s=Depends(db_session)):
    """Export the groups' history and M1 prices again, for backtesting (read-only)."""
    admin = current_admin(request, s)
    await form_data(request)
    from . import research
    acc = research.request_prices()
    n = await research.export_messages()
    services.log(s, "research_export", None, f"{n} messages, prices from account {acc}", admin.username)
    s.commit()
    flash(request, f"Research export: {n} messages saved" + (f", prices asked from account {acc}." if acc else
          ". No MT5 account online for prices."))
    return redirect("/admin/system#research")


@router.post("/system/github/check")
async def system_github_check(request: Request, s=Depends(db_session)):
    current_admin(request, s)
    await form_data(request)
    from . import github_update as gh
    try:
        c = await asyncio.to_thread(gh.check)
        flash(request, f"Latest on GitHub: {c['short']}. " + ("A new version is ready." if c["new"] else "You're up to date."))
    except Exception as e:  # noqa: BLE001
        flash(request, f"Couldn't check GitHub: {e}", "err")
    return redirect("/admin/system")


@router.post("/system/github/install")
async def system_github_install(request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    form = await form_data(request)
    from . import github_update as gh
    sha = str(form.get("sha", "")).strip() or None
    try:
        sha = await asyncio.to_thread(gh.install, sha, admin.username)
    except Exception as e:  # noqa: BLE001
        flash(request, f"Update not started: {e}", "err")
        return redirect("/admin/system")
    services.log(s, "update_started", None, f"github {sha[:7]}", admin.username)
    s.commit()
    notify.send_admins(f"⬆️ {admin.username} started an update from GitHub (commit {sha[:7]}). The site is back "
                       f"in about a minute; other VPS update themselves after that.")
    return render(request, "message.html", {"title": "Updating PipsFarmer",
                                            "text": f"Installing commit {sha[:7]} from GitHub. The site restarts in "
                                                    "about a minute. Refresh this page then."})


@router.post("/system/icon")
async def system_icon(request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    form = await form_data(request)
    from . import site_icon
    if form.get("action") == "default":
        site_icon.clear()
        services.log(s, "site_icon_default", None, "", admin.username)
        s.commit()
        flash(request, "Back to the PipsFarmer icon.")
        return redirect("/admin/system")
    up = form.get("icon")
    data = await up.read() if hasattr(up, "read") else b""
    err = site_icon.save(data)
    if err:
        flash(request, err, "err")
        return redirect("/admin/system")
    services.log(s, "site_icon_uploaded", None, "", admin.username)
    s.commit()
    flash(request, "Icon saved. Browsers can take a few minutes to show the new one; phones may need the "
                   "home-screen shortcut adding again.")
    return redirect("/admin/system")


@router.post("/system/settings")
async def system_settings(request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    form = await form_data(request)
    for k in ("daily_summary", "backup_to_telegram", "weekend_restarts"):
        set_setting(s, k, form.get(k) == "on")
    try:
        set_setting(s, "summary_hour_utc", max(0, min(23, int(form.get("summary_hour_utc", 7)))))
    except ValueError:
        pass
    url = str(form.get("public_url", "")).strip().rstrip("/")
    if url and not re.match(r"^https?://[\w.-]+(:\d+)?$", url):
        flash(request, "The website address looks like https://pipsfarmer.com", "err")
        return redirect("/admin/system")
    set_setting(s, "public_url", url)
    services.log(s, "system_settings", None, "", admin.username)
    s.commit()
    runtime.invalidate()
    flash(request, "Saved.")
    return redirect("/admin/system")


@router.post("/system/tunnel")
async def system_tunnel(request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    form = await form_data(request)
    token = re.sub(r"\s", "", str(form.get("token", "")))
    token = token.split("install")[-1] if "install" in token else token     # pasted the whole command
    if len(token) < 60:
        flash(request, "Paste the tunnel token from Cloudflare (the long text after 'service install').", "err")
        return redirect("/admin/system")
    try:
        system.install_tunnel(token)
    except Exception as e:  # noqa: BLE001
        flash(request, str(e), "err")
        return redirect("/admin/system")
    services.log(s, "tunnel_install", None, "", admin.username)
    s.commit()
    flash(request, "Installing the Cloudflare tunnel. Refresh in a minute to see it running.")
    return redirect("/admin/system")


@router.post("/system/backup")
async def system_backup(request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    form = await form_data(request)
    from . import telegram_service as tg
    if form.get("what") == "keys":
        if not admin.telegram_chat_id:
            flash(request, "Link your Telegram in Admin → Telegram first.", "err")
            return redirect("/admin/system")
        notify.send_admins("🔑 PipsFarmer key backup. Save this file somewhere safe, away from the VPS.",
                           file=("pipsfarmer-keys.txt", system.keys_backup_text().encode()),
                           chats=[admin.telegram_chat_id])
        flash(request, "Key backup sent to your Telegram.")
    else:
        path = system.backup_db("manual", keep=10)
        if admin.telegram_chat_id:
            notify.send_admins("🗄 Database backup", file=(path.name + ".gz", system.gz(path)),
                               chats=[admin.telegram_chat_id])
        flash(request, f"Backup saved ({path.name})" + (" and sent to your Telegram." if admin.telegram_chat_id else "."))
    tg.wake_bot()
    services.log(s, "backup_" + str(form.get("what", "db")), None, "", admin.username)
    s.commit()
    return redirect("/admin/system")


@router.post("/system/admins")
async def system_admins(request: Request, s=Depends(db_session)):
    admin = current_admin(request, s)
    form = await form_data(request)
    name = str(form.get("username", "")).strip()
    pw = str(form.get("password", ""))
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,32}", name):
        flash(request, "Usernames are 3–32 letters or numbers.", "err")
    elif s.scalar(select(Admin).where(Admin.username == name)):
        flash(request, "That admin already exists.", "err")
    elif len(pw) < 10:
        flash(request, "Admin passwords need at least 10 characters.", "err")
    else:
        s.add(Admin(username=name, password_hash=security.hash_password(pw)))
        services.log(s, "admin_created", None, name, admin.username)
        s.commit()
        flash(request, f"Admin {name} added.")
    return redirect("/admin/system")

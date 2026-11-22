"""Background jobs: VPS slot assignment + alerts, monthly renewals, news calendar, cleanup."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import delete, func, select

from . import config, notify, runtime, services, system
from .db import (Account, Deal, Event, NewsEvent, Node, Renewal, SessionLocal, Signal, Snapshot, User,
                 get_setting, set_setting, utcnow)

log = logging.getLogger("pf.scheduler")
_alerts = {"capacity_at": 0.0, "capacity_waiting": -1, "offline": set(), "acc_err": set(), "silent": set(),
           "node_warn": {}, "tg_down_since": None, "tg_alerted": False, "news_ok_at": time.time()}


# ------------------------------------------------------------ capacity

ADD_VPS = ("To add one: buy another Windows VPS, then Admin → Servers → Add a VPS gives you one line to paste "
           "into PowerShell (as Administrator) on it. It sets itself up and starts taking accounts.")


def capacity_text(cap: dict, waiting: int, threshold: int) -> str:
    """The admin bot's message when the servers are full or nearly full ("" when there's room)."""
    if not cap["total"] and not waiting:
        return ""
    pct = round(100 * cap["in_use"] / cap["total"]) if cap["total"] else 100
    mem = (f"\n🧠 Out of memory (no new accounts go there): {', '.join(cap['memory_full'])}."
           if cap.get("memory_full") else "")
    used = f"{cap['in_use']}/{cap['total']} slots used on {cap['online']} online VPS ({pct}%)."
    if waiting or cap["free"] == 0:
        head = (f"🚨 Servers full: {waiting} customer account(s) waiting for a slot." if waiting
                else "🚨 Servers full: the next new account will have to wait.")
        return f"{head}\n{used}{mem}\nOpen a new VPS now. {ADD_VPS}"
    if cap["free"] <= threshold or pct >= 80:
        return (f"⚠️ Servers nearly full: {cap['free']} free slot(s) left. {used}{mem}\n"
                f"Time to open a new VPS. {ADD_VPS}")
    return ""


def assignment_tick() -> None:
    with SessionLocal() as s:
        still = services.assign_accounts(s)
        s.commit()
        cap = services.capacity(s)
        text = capacity_text(cap, len(still), int(get_setting(s, "capacity_alert_free_slots") or 0))
        now = time.time()
        waiting = len(still)
        if text and (now - _alerts["capacity_at"] > 6 * 3600 or waiting > _alerts["capacity_waiting"]):
            notify.send_admins(text)
            _alerts["capacity_at"] = now
            _alerts["capacity_waiting"] = waiting
        if waiting == 0:
            _alerts["capacity_waiting"] = 0
        # node went offline
        for n in cap["nodes"]:
            if not n.enabled or not n.last_seen:
                continue
            off = not services.node_online(n)
            local = is_this_vps(n)
            if off and local:
                heal_local_node(n.id, n.name)
            if off and n.id not in _alerts["offline"] and cap["used"].get(n.id):
                _alerts["offline"].add(n.id)
                count = cap["used"].get(n.id)
                if local:
                    notify.send_admins(f"🔌 The MT5 manager on '{n.name}' stopped checking in ({count} account(s) on it). "
                                       f"Open trades are safe on the broker. I'm restarting it automatically and will "
                                       f"tell you when it's back.")
                else:
                    notify.send_admins(f"🔌 VPS '{n.name}' stopped checking in ({count} account(s) on it). Open trades "
                                       f"are safe on the broker. Check the VPS is switched on (Contabo panel), or use "
                                       f"Restart VPS in Admin → Servers.")
            elif not off and n.id in _alerts["offline"]:
                _alerts["offline"].discard(n.id)
                notify.send_admins(f"✅ '{n.name}' is checking in again. All good.")


_heal: dict[int, float] = {}
_website_started = time.time()
HEAL_GRACE = 180      # after a website restart (e.g. an update) give the MT5 manager time to check in first


def is_this_vps(node) -> bool:
    """The website and this MT5 manager run on the same machine (so the website can restart it)."""
    import socket
    host = (node.info or {}).get("hostname") or ""
    return bool(host) and host.lower() == socket.gethostname().lower()


def heal_local_node(node_id: int, name: str, every: float = 300) -> bool:
    """Restart this VPS's MT5 manager in the background, at most every 5 minutes."""
    if time.time() - _heal.get(node_id, 0) < every or time.time() - _website_started < HEAL_GRACE:
        return False
    _heal[node_id] = time.time()

    def go():
        try:
            what = system.restart_local_node()
            log.warning("MT5 manager on %s was silent: %s", name, what)
            if what.startswith("couldn't"):
                notify.send_admins(f"⚠️ {what}. Restart the VPS from the Contabo panel; everything starts by itself.")
        except Exception:  # noqa: BLE001
            log.exception("restarting the MT5 manager")
    import threading
    threading.Thread(target=go, daemon=True).start()
    return True


async def run_assignment() -> None:
    last = 0.0
    while True:
        if notify._wake.is_set() or time.time() - last > 30:
            notify._wake.clear()
            last = time.time()
            try:
                await asyncio.to_thread(assignment_tick)
            except Exception:  # noqa: BLE001
                log.exception("assignment")
        await asyncio.sleep(3)


# ------------------------------------------------------------ renewals

def renewal_tick() -> None:
    with SessionLocal() as s:
        for user in services.due_renewals(s):
            r = Renewal(user_id=user.id, due=user.paid_until, asked_at=utcnow())
            s.add(r)
            user.status = "payment_check"
            services.log(s, "renewal_due", user.id, f"due {user.paid_until:%d %b %Y}", "system")
            s.flush()
            notify.send_admins(_renewal_text(user, r), _renewal_buttons(r.id), renewal_id=r.id)
        # daily reminder for unanswered ones
        for r in s.scalars(select(Renewal).where(Renewal.status == "pending")):
            if r.asked_at and utcnow() - r.asked_at > timedelta(hours=24):
                r.asked_at = utcnow()
                notify.send_admins("⏰ Reminder, still waiting for an answer:\n" + _renewal_text(r.user, r),
                                   _renewal_buttons(r.id), renewal_id=r.id)
        s.commit()


def _renewal_text(user, r) -> str:
    return (f"💳 Monthly renewal due: {user.username} ({user.plan.name}, "
            f"{user.plan.currency}{user.plan.monthly_price:g}/month)\n"
            f"Due {r.due:%d %b %Y}. Did they pay?\n"
            f"Copying keeps running until you answer. 'Not paid' closes their account.")


def _renewal_buttons(rid: int):
    return [[("✅ Paid", f"rn:{rid}:paid"), ("❌ Not paid", f"rn:{rid}:no")]]


async def run_renewals() -> None:
    from . import billing
    while True:
        for job in (renewal_tick, billing.billing_tick):
            try:
                await asyncio.to_thread(job)
            except Exception:  # noqa: BLE001
                log.exception("renewals/billing")
        await asyncio.sleep(600)


async def run_mail() -> None:
    from . import mailer
    while True:
        try:
            sent = await asyncio.to_thread(mailer.send_pending)
            if sent:
                log.info("sent %s email(s)", sent)
        except Exception:  # noqa: BLE001
            log.exception("mail")
        await asyncio.sleep(30)


# ------------------------------------------------------------ news

def store_news(items: list[dict]) -> int:
    n = 0
    with SessionLocal() as s:
        for it in items:
            try:
                t = datetime.fromisoformat(it["date"]).astimezone(timezone.utc).replace(tzinfo=None)
            except (KeyError, ValueError):
                continue
            title = str(it.get("title", ""))[:255]
            cur = str(it.get("country", ""))[:8].upper()
            if s.scalar(select(NewsEvent).where(NewsEvent.time == t, NewsEvent.currency == cur, NewsEvent.title == title)):
                continue
            s.add(NewsEvent(time=t, currency=cur, impact=str(it.get("impact", ""))[:16], title=title))
            n += 1
        s.execute(delete(NewsEvent).where(NewsEvent.time < utcnow() - timedelta(days=14)))
        s.commit()
    return n


async def run_news() -> None:
    async with httpx.AsyncClient(headers={"User-Agent": "PipsFarmer/1.0"}) as http:
        while True:
            try:
                r = await http.get(config.NEWS_URL, timeout=30)
                if r.status_code == 200:
                    added = await asyncio.to_thread(store_news, r.json())
                    log.info("news: %s new events", added)
                    await asyncio.sleep(3 * 3600)
                    continue
                log.warning("news fetch HTTP %s", r.status_code)
            except Exception:  # noqa: BLE001
                log.exception("news")
            await asyncio.sleep(15 * 60)


# ------------------------------------------------------------ cleanup

def cleanup_tick() -> None:
    with SessionLocal() as s:
        s.execute(delete(Event).where(Event.created_at < utcnow() - timedelta(days=14)))
        s.execute(delete(Snapshot).where(Snapshot.time < utcnow() - timedelta(days=180)))
        from .db import CustomerAlert          # keys are kept 30 days so nothing is ever sent twice
        s.execute(delete(CustomerAlert).where(CustomerAlert.created_at < utcnow() - timedelta(days=30)))
        s.commit()


async def run_cleanup() -> None:
    while True:
        await asyncio.sleep(6 * 3600)
        try:
            await asyncio.to_thread(cleanup_tick)
        except Exception:  # noqa: BLE001
            log.exception("cleanup")


# ------------------------------------------------------------ watching everything while you're away

def _warn_node(n, key: str, text: str, every_h: float = 24) -> None:
    k = (n.id, key)
    if time.time() - _alerts["node_warn"].get(k, 0) > every_h * 3600:
        _alerts["node_warn"][k] = time.time()
        notify.send_admins(text)


BROKER_HINT = ("\n   ↳ MT5 doesn't know this broker yet. The VPS is looking it up by itself (broker finder); "
               "you'll get another message if it can't find it.")
BROKER_MANUAL = ("If the server name is right, do it by hand once: on that VPS double-click 'PipsFarmer - add a "
                 "broker' on the desktop, File → Open an Account, search the broker, Cancel, close MT5. Every VPS "
                 "learns it within a few minutes and the account connects by itself.")


def watch_tick(now: datetime | None = None) -> None:
    """Every few minutes: tell the admins about anything that needs them, and restart VPS
    for Windows updates at the weekend when gold is closed."""
    now = now or utcnow()
    from . import telegram_service as tg
    with SessionLocal() as s:
        # customer MT5 accounts that can't log in
        errs = list(s.scalars(select(Account).where(Account.conn_status == "error", Account.removed.is_(False),
                                                    Account.node_id.is_not(None))))
        new = [a for a in errs if a.id not in _alerts["acc_err"]
               and a.created_at and now - a.created_at > timedelta(minutes=10)]
        if new:
            lines = [f"• {a.user.username}: {a.login} @ {a.server}: {a.conn_error[:140]}"
                     + (BROKER_HINT if (a.live_state or {}).get("unknown_server") else "") for a in new[:15]]
            notify.send_admins("❌ MT5 account(s) can't connect. The customer sees this on their dashboard too.\n"
                               + "\n".join(lines))
            _alerts["acc_err"] |= {a.id for a in new}
        _alerts["acc_err"] &= {a.id for a in errs}

        # brokers a VPS couldn't look up by itself
        for n in s.scalars(select(Node)):
            for b in (n.info or {}).get("broker_searches") or []:
                if b.get("gave_up"):
                    _warn_node(n, f"broker:{b.get('server')}",
                               f"🔎 {n.name} couldn't find the broker server \"{b.get('server')}\" by itself "
                               f"(searched MetaQuotes for {b.get('query')!r}"
                               + ("" if b.get("searched") else f"; the MT5 window step failed: {b.get('detail', '')[:160]}")
                               + f").\n{BROKER_MANUAL}")

        # workers that stopped reporting while their VPS is online
        nodes = {n.id: n for n in s.scalars(select(Node))}
        silent = []
        for a in s.scalars(select(Account).where(Account.node_id.is_not(None), Account.removed.is_(False))):
            n = nodes.get(a.node_id)
            if not n or not services.node_online(n) or not a.last_report:
                continue
            if now - a.last_report > timedelta(minutes=5):
                silent.append(a)
        fresh = [a for a in silent if a.id not in _alerts["silent"]]
        if fresh:
            notify.send_admins("⏸ These MT5 accounts stopped reporting (the VPS is up, so the node is restarting "
                               "them):\n" + "\n".join(f"• {a.user.username}: {a.login} on {a.node.name}" for a in fresh[:15]))
            _alerts["silent"] |= {a.id for a in fresh}
        _alerts["silent"] &= {a.id for a in silent}

        # VPS health
        for n in nodes.values():
            if not services.node_online(n):
                continue
            info = n.info or {}
            if info.get("autologon") is False:
                _warn_node(n, "autologon", f"⚠️ VPS '{n.name}' doesn't log in by itself after a restart, so MT5 "
                                           f"would stay off. Turn on auto-login in Admin → Servers.")
            if info.get("template_has_login"):
                _warn_node(n, "template", f"🔐 The base MT5 on VPS '{n.name}' has a saved login in it. PipsFarmer "
                                          f"doesn't copy it to customers, but log out of it in that MT5 "
                                          f"(File > Login, then remove the account) to be safe.")
            if info.get("disk_free_gb") is not None and info["disk_free_gb"] < 5:
                _warn_node(n, "disk", f"💾 VPS '{n.name}' has only {info['disk_free_gb']} GB disk left.", 12)
            if info.get("ram") is not None and info["ram"] >= 92:
                _warn_node(n, "ram", f"🧠 VPS '{n.name}' is using {info['ram']:.0f}% of its RAM. Lower its slots "
                                     f"or add a VPS.", 6)
            # weekend restart for Windows updates (gold is closed Saturday)
            if (get_setting(s, "weekend_restarts") is not False and now.weekday() == 5 and 4 <= now.hour < 20
                    and info.get("reboot_pending") and info.get("autologon")):
                last = (get_setting(s, "last_restart") or {}).get(str(n.id), 0)
                if time.time() - last > 20 * 3600:
                    services.queue_node_command(n.id, "reboot")
                    lr = dict(get_setting(s, "last_restart") or {})
                    lr[str(n.id)] = time.time()
                    set_setting(s, "last_restart", lr)
                    s.commit()
                    notify.send_admins(f"🔄 Restarting VPS '{n.name}' to finish Windows updates (market's closed). "
                                       f"MT5 comes back by itself in a few minutes.")

    # the Telegram reader that copies the VIP group
    st = tg.status()
    configured = bool(runtime.main_session() and runtime.vip_chats() and tg.api_ready())
    if configured and st["main"] != "connected":
        if not _alerts["tg_down_since"]:
            _alerts["tg_down_since"] = time.time()
        elif time.time() - _alerts["tg_down_since"] > 600 and not _alerts["tg_alerted"]:
            _alerts["tg_alerted"] = True
            notify.send_admins(f"📵 The Telegram reader has been disconnected for 10 minutes ({st['main']}). "
                               f"VIP signals aren't being copied. It keeps retrying by itself; check Admin → Telegram.")
    elif st["main"] == "connected":
        if _alerts["tg_alerted"]:
            notify.send_admins("✅ Telegram reader is connected again.")
        _alerts["tg_down_since"], _alerts["tg_alerted"] = None, False


def _boot_time() -> float | None:
    try:
        import psutil
        return psutil.boot_time()
    except Exception:  # noqa: BLE001
        return None


_boot = {"at": _boot_time(), "done": False}
BOOT_RECENT = 900        # the website started within 15 min of Windows starting = the VPS restarted


def boot_tick(now_ts: float | None = None) -> str | None:
    """After the VPS restarts, tell the admins once everything is back (or what isn't after 10 minutes)."""
    now_ts = now_ts or time.time()
    if _boot["done"] or not _boot["at"] or _website_started - _boot["at"] > BOOT_RECENT:
        return None
    with SessionLocal() as s:
        nodes = [n for n in s.scalars(select(Node).where(Node.enabled.is_(True))) if is_this_vps(n)]
        node_up = any(services.node_online(n) for n in nodes)
        accs = list(s.scalars(select(Account).where(Account.removed.is_(False), Account.node_id.in_([n.id for n in nodes] or [-1]))))
        online = [a for a in accs if a.conn_status == "online"]
        waited = now_ts - _website_started
        if not (node_up and len(online) == len(accs)) and waited < 600:
            return None
        _boot["done"] = True
        when = datetime.fromtimestamp(_boot["at"], timezone.utc).strftime("%H:%M UTC")
        if node_up and len(online) == len(accs):
            text = (f"🔄 The VPS restarted at {when} and everything came back by itself: website ✓, "
                    f"MT5 manager ✓, {len(online)}/{len(accs)} account(s) online.")
        else:
            bad = [f"• {a.user.username}: {a.login} ({a.conn_status})" for a in accs if a.conn_status != "online"][:10]
            text = (f"🔄 The VPS restarted at {when}. Website ✓, MT5 manager {'✓' if node_up else '✗ not running'}, "
                    f"{len(online)}/{len(accs)} account(s) online." + ("\nNot online yet:\n" + "\n".join(bad) if bad else ""))
        notify.send_admins(text)
        return text


async def run_watch() -> None:
    await asyncio.sleep(60)
    while True:
        try:
            await asyncio.to_thread(watch_tick)
        except Exception:  # noqa: BLE001
            log.exception("watch")
        try:
            await asyncio.to_thread(boot_tick)
        except Exception:  # noqa: BLE001
            log.exception("boot notice")
        await asyncio.sleep(120)


# ------------------------------------------------------------ daily summary + nightly backup

def summary_text(now: datetime | None = None) -> str:
    now = now or utcnow()
    since = now - timedelta(days=1)
    with SessionLocal() as s:
        cap = services.capacity(s)
        accs = list(s.scalars(select(Account).where(Account.removed.is_(False))))
        online = sum(1 for a in accs if a.conn_status == "online")
        errors = [a for a in accs if a.conn_status == "error"]
        signals = s.scalar(select(func.count()).select_from(Signal).where(Signal.created_at >= since)) or 0
        pnl = s.scalar(select(func.sum(Deal.profit)).where(Deal.time >= since, Deal.ours.is_(True))) or 0.0
        pending = s.scalar(select(func.count()).select_from(Renewal).where(Renewal.status == "pending")) or 0
        due_soon = s.scalar(select(func.count()).select_from(User).where(
            User.billing == "monthly", User.status == "active", User.paid_until <= now + timedelta(days=3))) or 0
        from .db import Ticket, UpgradeRequest
        plan_reqs = s.scalar(select(func.count()).select_from(UpgradeRequest).where(UpgradeRequest.status == "pending")) or 0
        tickets = s.scalar(select(func.count()).select_from(Ticket).where(Ticket.status == "open")) or 0
        users = s.scalar(select(func.count()).select_from(User).where(User.status != "closed")) or 0
        new_users = s.scalar(select(func.count()).select_from(User).where(User.created_at >= since)) or 0
        problems = "; ".join(f"{a.user.username} {a.login} ({a.conn_error[:60]})" for a in errors[:5])
    from . import telegram_service as tg
    st = tg.status()
    lines = [f"☀️ PipsFarmer daily summary ({now:%a %d %b})",
             f"Customers: {users} (+{new_users} new)",
             f"MT5 accounts: {online} online, {len(errors)} with a problem, {cap['waiting']} waiting for a slot",
             f"VPS: {cap['online']}/{len(cap['nodes'])} online, {cap['in_use']}/{cap['total']} slots used",
             f"Signals in the last 24 h: {signals}. Customers' closed P/L on PipsFarmer trades: {pnl:+.2f}",
             f"Telegram reader: {st['main']}",
             f"Renewals waiting for an answer: {pending}. Due in the next 3 days: {due_soon}",
             f"Plan requests waiting: {plan_reqs}. Support questions waiting: {tickets}"]
    if problems:
        lines.append("Problems: " + problems)
    return "\n".join(lines)


def daily_tick(now: datetime | None = None) -> None:
    now = now or utcnow()
    with SessionLocal() as s:
        done = dict(get_setting(s, "daily_done") or {})
        today = now.strftime("%Y-%m-%d")
        summary_hour = int(get_setting(s, "summary_hour_utc") if get_setting(s, "summary_hour_utc") is not None else 7)
        want_backup_tg = get_setting(s, "backup_to_telegram") is not False
        want_summary = get_setting(s, "daily_summary") is not False
        jobs = []
        if now.hour >= 2 and done.get("backup") != today:
            done["backup"] = today
            jobs.append("backup")
        if want_summary and now.hour >= summary_hour and done.get("summary") != today:
            done["summary"] = today
            jobs.append("summary")
        if jobs:
            set_setting(s, "daily_done", done)
            s.commit()
    if "backup" in jobs:
        path = system.backup_db("auto", keep=14)
        log.info("backup saved to %s", path)
        if want_backup_tg:
            data = system.gz(path)
            if len(data) < 45_000_000:
                notify.send_admins(f"🗄 Nightly database backup ({len(data) / 1e6:.1f} MB). Keep it with your "
                                   f"key backup; together they restore everything.", file=(path.name + ".gz", data))
    if "summary" in jobs:
        notify.send_admins(summary_text(now))


async def run_daily() -> None:
    while True:
        try:
            await asyncio.to_thread(daily_tick)
        except Exception:  # noqa: BLE001
            log.exception("daily")
        await asyncio.sleep(300)


def customer_daily_tick(now: datetime | None = None) -> int:
    """Each customer's daily summary on Telegram, at the time they picked."""
    from . import customer_alerts
    with SessionLocal() as s:
        n = customer_alerts.daily_tick(s, now)
        s.commit()
    if n:
        from . import telegram_service
        telegram_service.wake_bot()
    return n


async def run_customer_daily() -> None:
    while True:
        try:
            await asyncio.to_thread(customer_daily_tick)
        except Exception:  # noqa: BLE001
            log.exception("customer daily summaries")
        await asyncio.sleep(60)


# ------------------------------------------------------------ signals log on disk
_signals_seen: dict = {"fp": None}


def signals_log_tick() -> bool:
    """Rewrite the signals folder (Excel with a tab per channel, plus a CSV per channel) when anything changed."""
    from . import reports
    with SessionLocal() as s:
        fp = reports.log_fingerprint(s)
        if fp == _signals_seen["fp"]:
            return False
        reports.write_signal_files(s)
    _signals_seen["fp"] = fp
    return True


async def run_signals_log() -> None:
    await asyncio.sleep(30)
    while True:
        try:
            await asyncio.to_thread(signals_log_tick)
        except Exception:  # noqa: BLE001
            log.exception("signals log")
        await asyncio.sleep(300)


# ------------------------------------------------------------ automatic updates from GitHub

AUTO_EVERY = 300            # check GitHub every 5 minutes
AUTO_START_GRACE = 600      # never update in the first 10 minutes after the website starts
AUTO_QUIET = 180            # ...or within 3 minutes of a new signal (workers restart for ~a minute)
AUTO_MAX_TRIES = 2          # a commit that doesn't install twice is left for a person to look at
_auto = {"tries": {}, "announced": False}


UPDATE_MODES = ("auto", "off")   # the owner OKs each change with Claude before it's pushed; a push then goes live


def update_mode(s) -> str:
    return "off" if get_setting(s, "gh_update_mode") == "off" else "auto"


def github_auto_tick(now: float | None = None) -> str:
    """A new commit on main (the owner already said yes to it with Claude): install it when safe.
    Returns what it did (for the log and tests)."""
    from . import github_update as gh
    now = now or time.time()
    with SessionLocal() as s:
        mode = update_mode(s)
        recent_signal = s.scalar(select(func.max(Signal.created_at)))
    if mode == "off" or not gh.token_hint():
        return "off"
    if not system.IS_WINDOWS:
        return "not windows"
    if now - _website_started < AUTO_START_GRACE:
        return "just started"
    if recent_signal and (utcnow() - recent_signal).total_seconds() < AUTO_QUIET:
        return "signal just in"
    c = gh.check()
    if not c["new"]:
        return "up to date"
    ver = f"v{c['version']} " if c["version"] else ""
    tries = _auto["tries"].get(c["sha"], 0)
    if tries >= AUTO_MAX_TRIES:
        return "gave up"
    _auto["tries"][c["sha"]] = tries + 1
    if tries == 1:
        notify.send_admins(f"⚠️ Update {c['short']} didn't install the first time; trying once more.")
    gh.install(c["sha"], "auto")
    with SessionLocal() as s:
        services.log(s, "update_started", None, f"github {c['short']} (automatic)", "auto")
        s.commit()
    notify.send_admins(f"⬆️ Installing {ver}from GitHub automatically: “{c['message']}”. "
                       "Back in about a minute; open trades stay safe.")
    return "installing"


def announce_update() -> str | None:
    """Once after a restart: if a different GitHub commit is now installed, say so."""
    from . import github_update as gh
    inst = gh.installed_commit()
    with SessionLocal() as s:
        last = get_setting(s, "gh_announced") or ""
        if not inst or inst == last:
            return None
        set_setting(s, "gh_announced", inst)
        s.commit()
    if not last:                       # first time we know the commit: nothing to compare with
        return None
    text = f"✅ Updated: now running v{system.VERSION} (commit {inst[:7]})."
    notify.send_admins(text)
    return text


async def run_github_auto() -> None:
    await asyncio.sleep(90)
    try:
        await asyncio.to_thread(announce_update)
    except Exception:  # noqa: BLE001
        log.exception("announce update")
    while True:
        try:
            did = await asyncio.to_thread(github_auto_tick)
            if did not in ("off", "up to date", "not windows", "just started"):
                log.info("github auto-update: %s", did)
        except Exception as e:  # noqa: BLE001
            log.warning("github auto-update: %s", e)
        await asyncio.sleep(AUTO_EVERY)


async def run_all() -> None:
    await asyncio.gather(run_assignment(), run_renewals(), run_news(), run_cleanup(), run_watch(), run_daily(),
                         run_mail(), run_signals_log(), run_customer_daily(), run_github_auto())

"""Telegram side of PipsFarmer.

* Main reader: your Telegram account that sits in the PipsFarmer VIP group.
* Customer readers: paid customers link their own Telegram to copy extra channels.
* Admin bot: sends you both renewal questions (Paid / Not paid buttons) and VPS alerts.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx
from sqlalchemy import select

from . import bot_reports, config, notify, runtime, security, services
from .db import Renewal, SessionLocal, Signal, Source, User, main_source
from .parser import is_levels_only, parse_levels, parse_signal, parse_update
from .plans import feature

log = logging.getLogger("pf.telegram")

_state = {"main": "not configured", "bot": "not configured", "users": 0, "last_signal": None, "error": "",
          "main_since": None, "main_name": ""}
_main = {"client": None, "reload": False}
MAIN_KEY = -1                                   # _pending_logins key for the admins' reader account
_chat_titles: dict[int, str] = {}                # Telegram group id -> name, for the signals log
_link_codes: dict[str, tuple[int, float]] = {}  # bot /start code -> (admin_id, expires)
_bot_wake = {"v": False}
_staged_updates: dict[str, str] = {}             # bot button key -> staged update folder
_user_clients: dict[int, object] = {}          # user_id -> TelegramClient
_user_chats: dict[int, dict[int, int]] = {}    # user_id -> {chat_id: source_id}
_pending_logins: dict[int, dict] = {}          # user_id -> {client, phone, hash, at}
_dialog_cache: dict[int, tuple[float, list]] = {}
_reload_flag = {"v": True}
_seen: set[tuple[int, int]] = set()
_edits_pending: list[tuple[int, str]] = []


def status() -> dict:
    return dict(_state)


def reload_soon() -> None:
    _reload_flag["v"] = True


def _telethon():
    from telethon import TelegramClient, events  # noqa: F401
    from telethon.sessions import StringSession  # noqa: F401
    return TelegramClient, events, StringSession


def api_ready() -> bool:
    api_id, api_hash = runtime.tg_api()
    return bool(api_id and api_hash)


def _new_client(session_str: str = ""):
    TelegramClient, _, StringSession = _telethon()
    api_id, api_hash = runtime.tg_api()
    return TelegramClient(StringSession(session_str), api_id, api_hash,
                          device_model="PipsFarmer", app_version="1.0")


def reload_main() -> None:
    """Reconnect the reader account with the latest settings (called after an admin changes them)."""
    _main["reload"] = True
    c = _main.get("client")
    if c is not None:
        try:
            asyncio.get_running_loop().create_task(c.disconnect())
        except RuntimeError:
            pass


# ------------------------------------------------------------ message handling (sync, DB)

def _sane_levels(side: str, sl: float, tps: list[float]) -> bool:
    """SL below every target for a buy, above for a sell."""
    if not tps:
        return True
    return all(tp > sl for tp in tps) if side == "buy" else all(tp < sl for tp in tps)


_update_kinds: dict[tuple, set] = {}   # (source, chat, msg) -> updates already applied


def handle_message(source_id: int, msg_id: int, text: str, reply_to: int | None, edited: bool = False,
                   chat_id: int | None = None, chat_title: str = "") -> str:
    """Turn one Telegram message into a signal or an update. Returns what happened.

    chat_id keeps groups apart: message numbers are per group, and an update posted in one group
    must never move another group's trade."""
    if not text or not text.strip():
        return "empty"
    key = (source_id, chat_id, msg_id)
    with SessionLocal() as s:
        source = s.get(Source, source_id)
        if not source or not source.active:
            return "inactive"
        if edited:
            sig = services.signal_for_message(s, source_id, msg_id, chat_id)
            if sig:
                return _apply_new_levels(s, sig, parse_signal(text), text, "telegram-edit", "levels_edit")
            if key not in _seen:
                return "edit_ignored"          # an old message we never saw being edited
            # a message we saw (and ignored) was edited into a signal or an update: handle it as new below
        elif key in _seen:
            return "duplicate"
        _seen.add(key)
        if len(_seen) > 20000:
            _seen.clear()

        p = parse_signal(text)
        replied = services.signal_for_message(s, source_id, reply_to, chat_id) if reply_to else None
        if replied:
            # a reply to a signal: a re-post with new levels, an SL/TP change, or a management update
            if p and p.side == replied.side:
                res = _apply_new_levels(s, replied, p, text, "telegram-reply", "levels_reply")
                if res == "levels_reply":
                    return res
                p = None                        # same numbers again: commentary, not a new trade
            elif p:
                p = None
        if p and not p.instant and p.sl:
            # the full signal posted straight after a "BUY NOW": fill that one in, don't open twice
            waiting = services.signal_waiting_for_levels(s, source_id, p.side, chat_id=chat_id)
            if waiting:
                services.add_update(s, waiting, "levels", p.sl, text, tps=p.tps or None)
                s.commit()
                return f"levels:{waiting.id}"
        if p:
            sig = services.create_signal(s, source, p, text, msg_id, tg_chat_id=chat_id, channel=chat_title)
            s.commit()
            _state["last_signal"] = time.time()
            return f"signal:{sig.id}"

        updates = parse_update(text)
        target = replied or services.latest_open_signal(s, source_id, chat_id=chat_id)
        if not updates:
            # "SL 3740 TP 3760" on its own: levels for a call waiting for them, or a reply giving a new SL
            waiting = replied if replied and replied.status == "open" else \
                services.signal_waiting_for_levels(s, source_id, chat_id=chat_id)
            if not waiting and is_levels_only(text):
                waiting = target if target and target.status == "open" else None   # "SL 3790" on its own
            sl, tps = parse_levels(text)
            if waiting and sl is not None and _near(waiting, sl) and _sane_levels(waiting.side, sl, tps):
                if sl != waiting.sl or (tps and not waiting.tps):
                    services.add_update(s, waiting, "levels", sl if sl != waiting.sl else None, text,
                                        tps=tps if tps and not waiting.tps else None)
                    s.commit()
                    return f"levels:{waiting.id}"
            return "ignored"
        if not target or target.status != "open":
            return "update_no_signal"
        # an edited update message must not fire the same instruction twice
        done = _update_kinds.setdefault(key, set())
        fresh = [(k, v) for k, v in updates if (k, v) not in done]
        if not fresh:
            return "edit_same_update"
        for kind, value in fresh:
            done.add((kind, value))
            if kind == "sl_move" and value and not _near(target, value):
                continue
            services.add_update(s, target, kind, value, text)
        if len(_update_kinds) > 5000:
            _update_kinds.clear()
        s.commit()
        return "update:" + ",".join(k for k, _ in fresh)


def _near(sig: Signal, price: float) -> bool:
    """A new stop must be a gold price near the trade (not a pip count or a typo)."""
    ref = (sig.entry_low + sig.entry_high) / 2 if sig.entry_low else (sig.sl or (sig.tps or [price])[0])
    return abs(price - ref) <= ref * 0.05


def _apply_new_levels(s, sig: Signal, p, text: str, by: str, label: str) -> str:
    """The group edited (or re-posted in a reply) a signal: send any new stop / targets to every account."""
    if not p or sig.status != "open" or p.side != sig.side:
        return "edit_ignored"
    new_sl = p.sl if p.sl and p.sl != sig.sl else None
    old_tps = list(sig.tps or [])
    # targets change only when the edit lists at least as many (a shortened edit keeps the rest)
    new_tps = p.tps if p.tps and list(p.tps) != old_tps and len(p.tps) >= len(old_tps) else None
    if not (new_sl or new_tps):
        return "edit_ignored"
    services.add_update(s, sig, "levels", new_sl, text, by, tps=new_tps)
    s.commit()
    return label


# ------------------------------------------------------------ main reader

async def run_main_reader() -> None:
    """Reads the PipsFarmer VIP group. Set up from Admin -> Telegram; reconnects by itself."""
    _, events, _ = _telethon()
    while True:
        _main["reload"] = False
        session, chats = runtime.main_session(), runtime.vip_chats()
        if not api_ready():
            _state["main"] = "not set up: add your Telegram API keys in Admin → Telegram"
        elif not session:
            _state["main"] = "not set up: log in your reader account in Admin → Telegram"
        elif not chats:
            _state["main"] = "not set up: pick the VIP group in Admin → Telegram"
        else:
            client = _new_client(session)
            _main["client"] = client
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    _state["main"] = "logged out: log the reader account in again in Admin → Telegram"
                    runtime.save(tg_main_session_enc="")
                    notify.send_admins("⚠️ The Telegram reader account was logged out, so VIP signals aren't "
                                       "being read. Log it in again in Admin → Telegram.")
                else:
                    with SessionLocal() as s:
                        main_id = main_source(s).id

                    async def on_msg(event, edited=False):
                        m = event.message
                        reply = m.reply_to.reply_to_msg_id if m.reply_to else None
                        cid = event.chat_id
                        if cid not in _chat_titles:
                            try:
                                chat = await event.get_chat()
                                _chat_titles[cid] = getattr(chat, "title", None) or str(cid)
                            except Exception:  # noqa: BLE001
                                _chat_titles[cid] = str(cid)
                        res = await asyncio.to_thread(handle_message, main_id, m.id, m.message or "", reply, edited,
                                                      cid, _chat_titles[cid])
                        if res in ("ignored", "update_no_signal", "edit_ignored"):
                            # keep the start of messages we didn't act on, so a missed call can be checked later
                            snippet = " ".join((m.message or "").split())[:140]
                            log.info("VIP msg %s/%s -> %s: %s", _chat_titles[cid], m.id, res, snippet)
                        else:
                            log.info("VIP msg %s/%s -> %s", _chat_titles[cid], m.id, res)

                    client.add_event_handler(lambda e: on_msg(e), events.NewMessage(chats=chats))
                    client.add_event_handler(lambda e: on_msg(e, True), events.MessageEdited(chats=chats))
                    me = await client.get_me()
                    _state["main_name"] = " ".join(x for x in (me.first_name, me.last_name) if x) or (me.username or "")
                    _state["main"] = "connected"
                    _state["main_since"] = time.time()
                    await client.run_until_disconnected()
            except Exception as e:  # noqa: BLE001
                _state["main"] = f"error: {e}"
                log.exception("main reader")
            finally:
                _state["main_since"] = None
                _main["client"] = None
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
        for _ in range(15):          # retry every 15 s, or straight away after a settings change
            if _main["reload"]:
                break
            await asyncio.sleep(1)


async def start_main_login(phone: str) -> None:
    await start_user_login(MAIN_KEY, phone)


async def finish_main_login(code: str, password: str = "") -> str:
    session_str, name, _ = await finish_user_login(MAIN_KEY, code, password)
    runtime.save(tg_main_session_enc=security.encrypt(session_str))
    _dialog_cache.pop(MAIN_KEY, None)
    reload_main()
    return name


async def list_main_dialogs() -> list[dict]:
    session = runtime.main_session()
    if not session:
        return []
    return await list_user_dialogs(MAIN_KEY, session, client=_main.get("client"))


# ------------------------------------------------------------ customer readers

def _load_user_channels() -> dict[int, tuple[str, dict[int, int]]]:
    out = {}
    with SessionLocal() as s:
        users = s.scalars(select(User).where(User.tg_session_enc != "", User.status != "closed"))
        for u in users:
            if not int(feature(u.plan.features, "max_channels") or 0):
                continue
            chans = {src.tg_chat_id: src.id for src in s.scalars(select(Source).where(
                Source.kind == "channel", Source.owner_user_id == u.id, Source.active.is_(True)))}
            out[u.id] = (security.decrypt(u.tg_session_enc), chans)
    return out


async def _start_user_client(user_id: int, session_str: str):
    _, events, _ = _telethon()
    client = _new_client(session_str)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("Telegram session expired")

    async def on_msg(event, edited=False):
        src = _user_chats.get(user_id, {}).get(event.chat_id)
        if not src:
            return
        m = event.message
        reply = m.reply_to.reply_to_msg_id if m.reply_to else None
        # a customer's own channel: its name is the source's title
        await asyncio.to_thread(handle_message, src, m.id, m.message or "", reply, edited, event.chat_id)

    client.add_event_handler(lambda e: on_msg(e), events.NewMessage(incoming=True))
    client.add_event_handler(lambda e: on_msg(e, True), events.MessageEdited(incoming=True))
    return client


async def run_user_readers() -> None:
    while not api_ready():
        await asyncio.sleep(15)
    while True:
        if _reload_flag["v"]:
            _reload_flag["v"] = False
            try:
                wanted = await asyncio.to_thread(_load_user_channels)
            except Exception:  # noqa: BLE001
                log.exception("load user channels")
                wanted = {}
            for uid in list(_user_clients):
                if uid not in wanted:
                    await _user_clients.pop(uid).disconnect()
                    _user_chats.pop(uid, None)
            for uid, (sess, chans) in wanted.items():
                _user_chats[uid] = chans
                if uid not in _user_clients and chans:
                    try:
                        _user_clients[uid] = await _start_user_client(uid, sess)
                    except Exception as e:  # noqa: BLE001
                        log.warning("user %s telegram: %s", uid, e)
            _state["users"] = len(_user_clients)
        for uid, c in list(_user_clients.items()):
            if not c.is_connected():
                try:
                    await c.connect()
                except Exception:  # noqa: BLE001
                    pass
        await asyncio.sleep(10)


# ------------------------------------------------------------ customer Telegram login

def pending_login_phone(user_id: int) -> str | None:
    p = _pending_logins.get(user_id)
    if p and time.time() - p["at"] < 600:
        return p["phone"]
    return None


async def start_user_login(user_id: int, phone: str) -> None:
    if not api_ready():
        raise RuntimeError("Telegram linking isn't set up on this server yet")
    old = _pending_logins.pop(user_id, None)
    if old:
        await old["client"].disconnect()
    client = _new_client()
    await client.connect()
    sent = await client.send_code_request(phone)
    _pending_logins[user_id] = {"client": client, "phone": phone, "hash": sent.phone_code_hash, "at": time.time()}


async def finish_user_login(user_id: int, code: str, password: str = "") -> tuple[str, str, str]:
    from telethon.errors import SessionPasswordNeededError
    p = _pending_logins.get(user_id)
    if not p:
        raise RuntimeError("The code expired. Start again with your phone number.")
    client = p["client"]
    try:
        try:
            await client.sign_in(phone=p["phone"], code=code, phone_code_hash=p["hash"])
        except SessionPasswordNeededError:
            if not password:
                raise RuntimeError("Your Telegram has a 2-step password. Enter it with the code.")
            await client.sign_in(password=password)
        me = await client.get_me()
        name = " ".join(x for x in (me.first_name, me.last_name) if x) or (me.username or "Telegram")
        return client.session.save(), name, p["phone"]
    finally:
        _pending_logins.pop(user_id, None)
        await client.disconnect()


async def list_user_dialogs(user_id: int, session_str: str, client=None) -> list[dict]:
    cached = _dialog_cache.get(user_id)
    if cached and time.time() - cached[0] < 120:
        return cached[1]
    client = client or _user_clients.get(user_id)
    if client is not None and not client.is_connected():
        client = None
    temp = None
    if not client:
        temp = client = _new_client(session_str)
        await client.connect()
    try:
        out = []
        async for d in client.iter_dialogs(limit=300):
            if d.is_channel or d.is_group:
                out.append({"id": d.id, "title": d.name or str(d.id)})
        _dialog_cache[user_id] = (time.time(), out)
        return out
    finally:
        if temp:
            await temp.disconnect()


# ------------------------------------------------------------ admin bot (Bot API)

API = "https://api.telegram.org/bot{token}/{method}"


async def _bot(http: httpx.AsyncClient, method: str, token: str | None = None, files=None, **params):
    url = API.format(token=token or runtime.bot_token(), method=method)
    if files:
        r = await http.post(url, data={k: str(v) for k, v in params.items()}, files=files, timeout=120)
    else:
        r = await http.post(url, json=params, timeout=40)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "bot error"))
    return data["result"]


async def check_bot_token(token: str) -> str:
    """Returns the bot's @username, or raises if Telegram doesn't accept the token."""
    async with httpx.AsyncClient() as http:
        me = await _bot(http, "getMe", token=token)
    return me.get("username", "")


def wake_bot() -> None:
    _bot_wake["v"] = True


def new_link_code(admin_id: int) -> str:
    code = security.new_token()[:20].replace("-", "x").replace("_", "y")
    _link_codes[code] = (admin_id, time.time() + 3600)
    return code


def admin_chat_ids() -> list[int]:
    ids = set(config.TG_ADMIN_CHAT_IDS)
    from .db import Admin
    with SessionLocal() as s:
        ids |= {a.telegram_chat_id for a in s.scalars(select(Admin)) if a.telegram_chat_id}
    return sorted(ids)


def renewal_answered(renewal_id: int, text: str) -> None:
    _edits_pending.append((renewal_id, text))


async def _send_outbox(http) -> None:
    for item in notify.take_all():
        markup = None
        if item["buttons"]:
            markup = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in item["buttons"]]}
        sent = []
        for chat in item.get("chats") or admin_chat_ids():
            try:
                if item.get("file"):
                    name, data = item["file"]
                    res = await _bot(http, "sendDocument", files={"document": (name, data)},
                                     chat_id=chat, caption=item["text"][:1000])
                else:
                    params = {"chat_id": chat, "text": item["text"], "disable_web_page_preview": True}
                    if markup:
                        params["reply_markup"] = markup
                    res = await _bot(http, "sendMessage", **params)
                sent.append([chat, res["message_id"]])
            except Exception as e:  # noqa: BLE001
                log.warning("bot send to %s failed: %s", chat, e)
        if item["renewal_id"] and sent:
            with SessionLocal() as s:
                r = s.get(Renewal, item["renewal_id"])
                if r:
                    r.tg_messages = (r.tg_messages or []) + sent
                    s.commit()
    while _edits_pending:
        rid, text = _edits_pending.pop(0)
        with SessionLocal() as s:
            r = s.get(Renewal, rid)
            msgs = list(r.tg_messages or []) if r else []
        for chat, mid in msgs:
            try:
                await _bot(http, "editMessageText", chat_id=chat, message_id=mid, text=text)
            except Exception:  # noqa: BLE001
                pass


def _who(cb: dict) -> str:
    f = cb.get("from", {})
    return f.get("username") and "@" + f["username"] or f.get("first_name") or str(f.get("id"))


def link_admin_chat(code: str, chat_id: int) -> str | None:
    """Connects the admin who made `code` to this Telegram chat. Returns their username."""
    from .db import Admin
    item = _link_codes.pop(code, None)
    if not item or item[1] < time.time():
        return None
    with SessionLocal() as s:
        a = s.get(Admin, item[0])
        if not a:
            return None
        a.telegram_chat_id = chat_id
        services.log(s, "admin_telegram_linked", None, str(chat_id), a.username)
        s.commit()
        return a.username


REPORT_BUTTONS = [[("💰 P&L today", "rp:pnl:today"), ("📅 Week", "rp:pnl:week"), ("🗓 Month", "rp:pnl:month")],
                  [("👥 Accounts", "rp:accounts"), ("📌 Open trades", "rp:positions")],
                  [("📒 Signals log (Excel)", "rp:signals")],
                  [("⬆️ Update from GitHub", "gh:check")]]
BOT_COMMANDS = [("accounts", "Every account, its size and today's P&L"),
                ("update", "Check GitHub for a new version and install it"),
                ("pnl", "Closed P&L: today, week, month or all"),
                ("positions", "Every open trade right now"),
                ("account", "One account in detail: /account <login or username>"),
                ("signals", "Signals log as an Excel file, a tab per channel"),
                ("help", "What the bot can show")]


def _signals_file() -> bytes:
    from . import reports
    with SessionLocal() as s:
        return reports.signals_xlsx(s)


def report_for(text: str) -> str | None:
    """Answer an admin's /command with a report (None = not a report command)."""
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower().split("@")[0]
    arg = parts[1].strip() if len(parts) > 1 else ""
    with SessionLocal() as s:
        if cmd in ("/help", "/menu"):
            return bot_reports.HELP
        if cmd in ("/accounts", "/size", "/sizes"):
            return bot_reports.accounts_report(s)
        if cmd in ("/pnl", "/p&l", "/profit"):
            return bot_reports.pnl_report(s, (arg or "today").lower())
        if cmd in ("/positions", "/trades", "/open"):
            return bot_reports.positions_report(s)
        if cmd == "/account":
            return bot_reports.account_report(s, arg) if arg else "Send /account <login or username>."
    return None


async def _send_report(http, chat: int, text: str) -> None:
    chunks = bot_reports.split_message(text)
    markup = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in REPORT_BUTTONS]}
    for i, chunk in enumerate(chunks):
        params = {"chat_id": chat, "text": chunk, "disable_web_page_preview": True}
        if i == len(chunks) - 1:
            params["reply_markup"] = markup
        await _bot(http, "sendMessage", **params)


async def _handle_update(http, upd: dict) -> None:
    cb = upd.get("callback_query")
    msg = upd.get("message")
    if msg:
        chat = msg["chat"]["id"]
        text = msg.get("text", "")
        doc = msg.get("document")
        if doc and chat in set(admin_chat_ids()):
            await _bot_update_from_zip(http, chat, doc)
            return
        if text.startswith("/start"):
            parts = text.split()
            if len(parts) == 2:
                who = await asyncio.to_thread(link_admin_chat, parts[1], chat)
                if who:
                    await _bot(http, "sendMessage", chat_id=chat,
                               text=f"✅ Linked to admin {who}. You'll get renewal questions, VPS alerts, "
                                    f"the daily summary and backups here.")
                    return
                await _bot(http, "sendMessage", chat_id=chat,
                           text="That link has expired. Make a new one in Admin → Telegram.")
                return
            allowed = chat in set(admin_chat_ids())
            await _bot(http, "sendMessage", chat_id=chat,
                       text="✅ You're set up for PipsFarmer alerts. Send /help for account reports." if allowed else
                       "This is a private bot. Admins link it from Admin → Telegram.")
            return
        if text.startswith("/") and chat in set(admin_chat_ids()):
            if text.split()[0].split("@")[0].lower() == "/update":
                await _gh_check_reply(http, chat)
                return
            if text.split()[0].split("@")[0].lower() in ("/signals", "/log"):
                data = await asyncio.to_thread(_signals_file)
                await _bot(http, "sendDocument", files={"document": (f"PipsFarmer signals {time.strftime('%Y-%m-%d')}.xlsx", data)},
                           chat_id=chat, caption="📒 Signals log: a tab per channel, with who took each signal, "
                                                 "SL changes and results.")
                return
            reply = await asyncio.to_thread(report_for, text)
            if reply:
                await _send_report(http, chat, reply)
        return
    if not cb:
        return
    allowed = set(admin_chat_ids())
    if cb["from"]["id"] not in allowed and cb["message"]["chat"]["id"] not in allowed:
        await _bot(http, "answerCallbackQuery", callback_query_id=cb["id"], text="Not allowed")
        return
    if cb.get("data") == "rp:signals":
        await _bot(http, "answerCallbackQuery", callback_query_id=cb["id"], text="Making the file…")
        data = await asyncio.to_thread(_signals_file)
        await _bot(http, "sendDocument", files={"document": (f"PipsFarmer signals {time.strftime('%Y-%m-%d')}.xlsx", data)},
                   chat_id=cb["message"]["chat"]["id"], caption="📒 Signals log: a tab per channel.")
        return
    if (cb.get("data") or "").startswith("rp:"):
        await _bot(http, "answerCallbackQuery", callback_query_id=cb["id"])
        reply = await asyncio.to_thread(report_for, "/" + cb["data"][3:].replace(":", " "))
        if reply:
            await _send_report(http, cb["message"]["chat"]["id"], reply)
        return
    data = cb.get("data", "")
    if data.startswith("gh:"):
        await _gh_callback(http, cb, data)
        return
    if data.startswith("up:"):
        _, key, ans = data.split(":")
        root = _staged_updates.pop(key, None)
        chat = cb["message"]["chat"]["id"]
        if ans != "go" or not root:
            await _bot(http, "answerCallbackQuery", callback_query_id=cb["id"], text="Cancelled")
            await _bot(http, "editMessageText", chat_id=chat, message_id=cb["message"]["message_id"],
                       text="Update cancelled." if ans != "go" else "That update expired. Send the zip again.")
            return
        from pathlib import Path

        from . import system
        try:
            await asyncio.to_thread(system.backup_db, "pre-update", 10)
            await asyncio.to_thread(system.run_update, Path(root))
            text = (f"⬆️ Updating now (started by {_who(cb)}). The site is back in about a minute; "
                    f"other VPS update themselves after that.")
        except Exception as e:  # noqa: BLE001
            text = f"Update didn't start: {e}"
        await _bot(http, "answerCallbackQuery", callback_query_id=cb["id"], text="OK")
        await _bot(http, "editMessageText", chat_id=chat, message_id=cb["message"]["message_id"], text=text)
        return
    if data.startswith("ur:"):
        _, rid, ans = data.split(":")
        from . import billing
        text = await asyncio.to_thread(billing.answer_request, int(rid), ans == "ok", _who(cb))
        notify.users_changed()
        await _bot(http, "answerCallbackQuery", callback_query_id=cb["id"], text="Done")
        try:
            await _bot(http, "editMessageText", chat_id=cb["message"]["chat"]["id"],
                       message_id=cb["message"]["message_id"], text=text)
        except Exception:  # noqa: BLE001
            pass
        return
    if data.startswith("rn:"):
        _, rid, ans = data.split(":")

        def answer():
            with SessionLocal() as s:
                r = s.get(Renewal, int(rid))
                if not r:
                    return "Renewal not found"
                text = services.answer_renewal(s, r, ans == "paid", _who(cb))
                s.commit()
                return text
        text = await asyncio.to_thread(answer)
        notify.users_changed()
        renewal_answered(int(rid), text)
        await _bot(http, "answerCallbackQuery", callback_query_id=cb["id"], text="Done")


async def _gh_check_reply(http, chat: int, edit_message_id: int | None = None) -> None:
    """Admin asked to update from GitHub: show installed vs latest, then ask before installing."""
    from . import github_update as gh
    markup = None
    try:
        c = await asyncio.to_thread(gh.check)
        text = "⬆️ Update from GitHub\n\n" + gh.describe(c)
        if c["new"]:
            text += "\n\nInstall it? The database is backed up first and trades stay open."
            markup = {"inline_keyboard": [[{"text": "⬆️ Install", "callback_data": f"gh:go:{c['sha'][:12]}"},
                                           {"text": "Cancel", "callback_data": "gh:no"}]]}
    except Exception as e:  # noqa: BLE001
        text = f"Couldn't check GitHub: {e}\nSave a token in Admin → System → Update from GitHub."
    params = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
    if markup:
        params["reply_markup"] = markup
    if edit_message_id:
        await _bot(http, "editMessageText", message_id=edit_message_id, **params)
    else:
        await _bot(http, "sendMessage", **params)


async def _gh_callback(http, cb: dict, data: str) -> None:
    from . import github_update as gh
    chat, mid = cb["message"]["chat"]["id"], cb["message"]["message_id"]
    parts = data.split(":")
    if parts[1] == "check":
        await _bot(http, "answerCallbackQuery", callback_query_id=cb["id"], text="Checking GitHub…")
        await _gh_check_reply(http, chat)
        return
    if parts[1] != "go" or len(parts) < 3:
        await _bot(http, "answerCallbackQuery", callback_query_id=cb["id"], text="Cancelled")
        await _bot(http, "editMessageText", chat_id=chat, message_id=mid, text="Update cancelled.")
        return
    await _bot(http, "answerCallbackQuery", callback_query_id=cb["id"], text="Downloading…")
    try:
        latest = await asyncio.to_thread(gh.check)
        if not latest["sha"].startswith(parts[2]):
            await _gh_check_reply(http, chat, edit_message_id=mid)   # main moved on: ask again
            return
        sha = await asyncio.to_thread(gh.install, latest["sha"], _who(cb))
        with SessionLocal() as s:
            services.log(s, "update_started", None, f"github {sha[:7]}", _who(cb))
            s.commit()
        text = (f"⬆️ Installing commit {sha[:7]} from GitHub (started by {_who(cb)}). The site is back in about "
                f"a minute; other VPS update themselves after that.")
    except Exception as e:  # noqa: BLE001
        text = f"Update didn't start: {e}"
    await _bot(http, "editMessageText", chat_id=chat, message_id=mid, text=text)


async def _bot_update_from_zip(http, chat: int, doc: dict) -> None:
    """An admin sent a file to the bot: if it's a PipsFarmer zip, offer to install it."""
    from . import system
    name = doc.get("file_name", "")
    if not name.lower().endswith(".zip"):
        await _bot(http, "sendMessage", chat_id=chat, text="Send the PipsFarmer update as a .zip file.")
        return
    if doc.get("file_size", 0) > 20_000_000:
        await _bot(http, "sendMessage", chat_id=chat, text="That file is too big for the bot (20 MB max).")
        return
    f = await _bot(http, "getFile", file_id=doc["file_id"])
    r = await http.get(f"https://api.telegram.org/file/bot{runtime.bot_token()}/{f['file_path']}", timeout=120)
    try:
        root = await asyncio.to_thread(system.stage_update, r.content)
    except ValueError as e:
        await _bot(http, "sendMessage", chat_id=chat, text=f"Can't use that zip: {e}")
        return
    version = ""
    try:
        import re as _re
        m = _re.search(r'VERSION = "([^"]+)"', (root / "server" / "system.py").read_text(encoding="utf-8"))
        version = m.group(1) if m else ""
    except OSError:
        pass
    key = security.new_token()[:10].replace("-", "x").replace("_", "y")
    _staged_updates[key] = str(root)
    await _bot(http, "sendMessage", chat_id=chat,
               text=f"📦 {name}: PipsFarmer {version or 'update'} (running now: {system.VERSION}).\n"
                    f"Install it? The database is backed up first and trades stay open.",
               reply_markup={"inline_keyboard": [[{"text": "⬆️ Install", "callback_data": f"up:{key}:go"},
                                                  {"text": "Cancel", "callback_data": f"up:{key}:no"}]]})


async def run_bot() -> None:
    """Admin bot. Picks up a new token from Admin → Telegram without a restart."""
    offset, token_in_use = 0, None
    async with httpx.AsyncClient() as http:
        while True:
            token = runtime.bot_token()
            if not token:
                _state["bot"] = "not set up: add the bot token in Admin → Telegram"
                if notify.pending() > 200:          # don't let the outbox grow forever
                    notify.take_all()
                await asyncio.sleep(5)
                continue
            if token != token_in_use:
                if token_in_use is None:            # first connection: skip an old backlog of alerts
                    backlog = notify.take_all()
                    for item in backlog[-10:]:
                        notify.send_admins(item["text"], item["buttons"], item["renewal_id"], item.get("file"),
                                           item.get("chats"))
                offset, token_in_use = 0, token
                try:
                    await _bot(http, "setMyCommands",
                               commands=[{"command": c, "description": d} for c, d in BOT_COMMANDS])
                except Exception:  # noqa: BLE001
                    log.warning("couldn't set the bot's command menu")
            try:
                await _send_outbox(http)
                wait = 0 if _bot_wake["v"] else 5
                _bot_wake["v"] = False
                updates = await _bot(http, "getUpdates", offset=offset, timeout=wait,
                                     allowed_updates=["message", "callback_query"])
                _state["bot"] = "connected"
                for u in updates:
                    offset = u["update_id"] + 1
                    try:
                        await _handle_update(http, u)
                    except Exception:  # noqa: BLE001
                        log.exception("bot update")
            except Exception as e:  # noqa: BLE001
                _state["bot"] = f"error: {e}"
                await asyncio.sleep(10)


async def run_all() -> None:
    await asyncio.gather(run_main_reader(), run_user_readers(), run_bot())

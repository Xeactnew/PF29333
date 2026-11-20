"""Customer trade alerts through the Telegram bot: linking, each alert kind, no repeats, rate, retries."""
import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from server import customer_alerts as ca
from server import runtime, security, telegram_service
from server.db import Account, CustomerAlert, Deal, Node, Plan, SessionLocal, Signal, User, main_source, utcnow
from tests.conftest import Browser

CHAT = 424242


@pytest.fixture(autouse=True)
def _db(browser):
    """Starting the app (the browser fixture) sets up the database."""


def _user(name, chat=CHAT, prefs=None):
    with SessionLocal() as s:
        plan = s.query(Plan).first()
        u = User(username=name, email=f"{name}@example.com", password_hash=security.hash_password("password123"),
                 plan_id=plan.id, alert_chat_id=chat, alert_prefs=prefs or {})
        s.add(u)
        s.flush()
        a = Account(user_id=u.id, login=f"77{u.id:04d}", server="Fake-Live", password_enc="x", label="Main",
                    conn_status="online", currency="USD", balance=1000, live_state={})
        s.add(a)
        sig = Signal(source_id=main_source(s).id, side="buy", entry_low=4030, entry_high=4037, sl=4027,
                     tps=[4047, 4057], channel="Gold Room")
        s.add(sig)
        s.commit()
        return u.id, a.id, sig.id


def _deal(aid, ticket, sid, entry, volume=0.05, price=4036.2, profit=0.0, ago=timedelta(0)):
    return Deal(account_id=aid, ticket=ticket, position_id=ticket, time=utcnow() - ago, symbol="XAUUSD.s",
                side="buy", entry=entry, volume=volume, price=price, profit=profit, ours=True, signal_id=sid)


def _alerts(uid):
    with SessionLocal() as s:
        return list(s.scalars(select(CustomerAlert).where(CustomerAlert.user_id == uid).order_by(CustomerAlert.id)))


def _add_deals(aid, deals, live=None):
    with SessionLocal() as s:
        a = s.get(Account, aid)
        if live is not None:
            a.live_state = live
        for d in deals:
            s.add(d)
        s.flush()
        ca.on_deals(s, a, deals)
        s.commit()


def test_open_once_per_signal_then_partial_then_close():
    uid, aid, sid = _user("alertsam")
    pos = {"positions": [{"signal_id": str(sid), "volume": 0.05}]}
    _add_deals(aid, [_deal(aid, 1, sid, "in", price=4036), _deal(aid, 2, sid, "in", price=4032)], live=pos)
    _add_deals(aid, [_deal(aid, 3, sid, "in", price=4030)], live=pos)          # a later layer: no new message
    al = _alerts(uid)
    assert len(al) == 1 and al[0].kind == "open"
    assert "BUY gold · Gold Room" in al[0].text and "0.1 lots at 4,034 (2 entries)" in al[0].text
    assert "SL 4,027 · TP 4,047 / 4,057" in al[0].text and "Account Main" in al[0].text

    _add_deals(aid, [_deal(aid, 4, sid, "out", profit=54.0)], live=pos)         # TP1 layer: partial
    assert _alerts(uid)[-1].kind == "manage" and "+$54.00" in _alerts(uid)[-1].text

    _add_deals(aid, [_deal(aid, 5, sid, "out", profit=-9.0), _deal(aid, 6, sid, "out", profit=1.0)], live={})
    last = _alerts(uid)[-1]
    assert last.kind == "close" and "Result +$46.00" in last.text and "Today so far +$46.00" in last.text
    with SessionLocal() as s:                                                     # nothing twice
        ca.check_closed(s, s.get(Account, aid))
        s.commit()
    assert len(_alerts(uid)) == 3


def test_old_history_and_unlinked_and_switched_off_stay_quiet():
    uid, aid, sid = _user("alertold")
    _add_deals(aid, [_deal(aid, 11, sid, "in", ago=timedelta(days=3)),
                     _deal(aid, 12, sid, "out", profit=5, ago=timedelta(days=3))], live={})
    assert _alerts(uid) == []
    uid2, aid2, sid2 = _user("alertnochat", chat=None)
    _add_deals(aid2, [_deal(aid2, 13, sid2, "in")], live={})
    assert _alerts(uid2) == []
    uid3, aid3, sid3 = _user("alertoff", chat=CHAT + 3, prefs={"open": False})
    _add_deals(aid3, [_deal(aid3, 14, sid3, "in")], live={"positions": [{"signal_id": str(sid3)}]})
    assert _alerts(uid3) == []


def _report(aid, rep):
    with SessionLocal() as s:
        a = s.get(Account, aid)
        ca.on_report(s, a, rep)
        s.commit()


def test_notes_and_account_problems(monkeypatch):
    uid, aid, sid = _user("alertnotes")
    _report(aid, {"status": "online", "notes": ["20 Nov 10:00 connected"]})       # first report: just remembered
    assert _alerts(uid) == []
    _report(aid, {"status": "online", "notes": [
        f"20 Nov 10:05 signal #{sid}: stop moved to breakeven +15 pips",
        f"20 Nov 10:04 signal #{sid + 1000} skipped: same trade as signal #{sid} (posted in two of your groups)",
        f"20 Nov 10:03 signal #{sid} skipped: SL 120 pips is above your maximum", "20 Nov 10:00 connected"]})
    kinds = [(a.kind, a.text) for a in _alerts(uid)]
    assert [k for k, _ in kinds] == ["problems", "manage"]
    assert "Why: SL 120 pips is above your maximum" in kinds[0][1] and "BUY call from Gold Room" in kinds[0][1]
    assert "Stop moved to entry + 15 pips" in kinds[1][1]
    _report(aid, {"status": "online", "notes": [f"20 Nov 10:05 signal #{sid}: stop moved to breakeven +15 pips"]})
    assert len(_alerts(uid)) == 2                                                 # same notes: nothing new

    clock = [1_000_000.0]
    monkeypatch.setattr(ca.time, "time", lambda: clock[0])
    _report(aid, {"status": "error", "error": "wrong login, password or server name"})
    assert "won't log in" in _alerts(uid)[-1].text                               # straight away
    _report(aid, {"status": "online"})
    assert "connected again" in _alerts(uid)[-1].text
    n = len(_alerts(uid))
    _report(aid, {"status": "starting", "error": "MT5 lost connection, reconnecting"})
    clock[0] += 60
    _report(aid, {"status": "online"})                                            # a blip: no alert at all
    assert len(_alerts(uid)) == n
    _report(aid, {"status": "online", "blocked": "Algo Trading is switched off in MT5"})
    clock[0] += 121
    _report(aid, {"status": "online", "blocked": "Algo Trading is switched off in MT5"})
    assert "Algo Trading is switched off" in _alerts(uid)[-1].text


def test_rate_cap_retry_and_blocked_bot(monkeypatch):
    uid, aid, sid = _user("alertrate", chat=CHAT + 5)
    with SessionLocal() as s:
        u = s.get(User, uid)
        for i in range(ca.HOURLY_CAP + 5):
            ca.queue(s, u, "open", f"rate:{uid}:{i}", f"msg {i}")
        s.commit()
    al = _alerts(uid)
    assert sum(a.status == "queued" for a in al) == ca.HOURLY_CAP and al[-1].status == "dropped"
    with SessionLocal() as s:
        batch = [a for a in ca.due(s, limit=100) if a.chat_id == CHAT + 5]
        assert len(batch) == 3                                                    # a few per chat per pass
        ca.mark(s, batch[0].id, "Bad Gateway")
        ca.mark(s, batch[1].id, None)
        s.commit()
        a0, a1 = s.get(CustomerAlert, batch[0].id), s.get(CustomerAlert, batch[1].id)
        assert a0.status == "queued" and a0.tries == 1 and a0.next_try > utcnow() and a1.status == "sent"
        ca.mark(s, batch[2].id, "Forbidden: bot was blocked by the user")
        s.commit()
        assert s.get(User, uid).alert_chat_id is None


def test_bot_link_stop_and_sending(monkeypatch):
    uid, aid, sid = _user("alertlink", chat=None)
    sent = []

    async def fake_bot(http, method, token=None, files=None, **params):
        sent.append((method, params))
        return {"message_id": 1}
    monkeypatch.setattr(telegram_service, "_bot", fake_bot)
    monkeypatch.setattr(telegram_service, "admin_chat_ids", lambda: [111])
    code = ca.new_link_code(uid)
    asyncio.run(telegram_service._handle_update(None, {"message": {"chat": {"id": 5150}, "text": f"/start {code}"}}))
    assert "You're linked, alertlink" in sent[-1][1]["text"]
    with SessionLocal() as s:
        assert s.get(User, uid).alert_chat_id == 5150
        ca.queue(s, s.get(User, uid), "test", f"t:{uid}", "hello")
        s.commit()
    asyncio.run(telegram_service._handle_update(None, {"message": {"chat": {"id": 5150}, "text": f"/start {code}"}}))
    assert "expired" in sent[-1][1]["text"]                                       # codes work once
    n = asyncio.run(telegram_service._send_customer_alerts(None))
    assert n >= 1 and any(p.get("text") == "hello" and p.get("chat_id") == 5150 for _, p in sent)
    assert _alerts(uid)[-1].status == "sent"
    asyncio.run(telegram_service._handle_update(None, {"message": {"chat": {"id": 5150}, "text": "/stop"}}))
    assert "Alerts are off" in sent[-1][1]["text"]
    with SessionLocal() as s:
        assert s.get(User, uid).alert_chat_id is None
    asyncio.run(telegram_service._handle_update(None, {"message": {"chat": {"id": 999}, "text": "/start"}}))
    assert "Plan & login" in sent[-1][1]["text"] and "Admin" not in sent[-1][1]["text"]


def test_daily_summary_at_chosen_time():
    uid, aid, sid = _user("alertdaily", chat=CHAT + 7,
                          prefs={"daily": True, "daily_time": "21:00", "tz_offset": -60})   # UTC+1
    with SessionLocal() as s:
        s.add(_deal(aid, 31, sid, "out", profit=12.5))
        s.commit()
        day = utcnow().replace(hour=19, minute=59)
        assert ca.daily_tick(s, day) == 0                                       # 20:59 local: not yet
        assert ca.daily_tick(s, day.replace(hour=20, minute=0)) == 1             # 21:00 local
        assert ca.daily_tick(s, day.replace(hour=22, minute=0)) == 0             # once a day
        s.commit()
    text = _alerts(uid)[-1].text
    assert "Your day" in text and "Main: +$12.50 · 1 trade" in text


def test_profile_page_link_and_save(app):
    uid, aid, sid = _user("alertweb", chat=None)
    runtime.save(tg_bot_username="PFAlertsBot")
    try:
        b = Browser(app)
        b.get("/login")
        b.post("/login", {"username": "alertweb", "password": "password123"})
        assert "Connect Telegram" in b.get("/plan").text
        r = b.post("/profile/alerts/link", follow=False)
        assert r.status_code == 303 and r.headers["location"].startswith("https://t.me/PFAlertsBot?start=c")
        with SessionLocal() as s:
            s.get(User, uid).alert_chat_id = 6060
            s.commit()
        page = b.get("/plan").text
        assert "Save alerts" in page and "Daily summary" in page
        b.post("/profile/alerts", {"open": "on", "close": "on", "daily": "on", "daily_time": "07:30", "tz_offset": "0"})
        with SessionLocal() as s:
            p = ca.prefs(s.get(User, uid))
        assert p["open"] and p["close"] and not p["manage"] and p["daily"] and p["daily_time"] == "07:30"
        b.post("/profile/alerts", {"action": "unlink"})
        with SessionLocal() as s:
            assert s.get(User, uid).alert_chat_id is None
    finally:
        runtime.save(tg_bot_username="")


def test_worker_deals_endpoint_queues_alert(app):
    from fastapi.testclient import TestClient
    uid, aid, sid = _user("alertapi", chat=CHAT + 9)
    token = security.new_token()
    with SessionLocal() as s:
        n = Node(name="alerts-node", token_hash=security.hash_token(token), enabled=False)
        s.add(n)
        s.flush()
        n.enabled = True
        s.get(Account, aid).node_id = n.id
        s.commit()
        nid = n.id
    try:
        c = TestClient(app)
        now = (utcnow() - datetime(1970, 1, 1)).total_seconds()
        body = {"deals": [{"ticket": 9901, "position_id": 9901, "time": now, "symbol": "XAUUSD", "side": "buy",
                           "entry": "in", "volume": 0.02, "price": 4035, "profit": 0, "ours": True, "signal_id": sid}]}
        h = {"Authorization": f"Bearer {token}"}
        assert c.post(f"/api/worker/{aid}/deals", json=body, headers=h).json() == {"added": 1}
        assert c.post(f"/api/worker/{aid}/deals", json=body, headers=h).json() == {"added": 0}
        assert [a.kind for a in _alerts(uid)] == ["open"]
    finally:
        with SessionLocal() as s:
            s.get(Account, aid).node_id = None
            s.get(Node, nid).enabled = False
            s.commit()

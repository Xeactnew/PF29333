"""1.3.36: Telegram topics, private VIP groups, the login code, and the servers-full alert."""
import asyncio
import re
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import select

from server import notify, runtime, scheduler, security, services
from server import telegram_service as tg
from server.db import (Account, Event, Node, Plan, SessionLocal, Setting, Signal, Source, User, main_source, seed,
                       utcnow)
from tests.test_web import admin_browser

KD = -1004498117492


@pytest.fixture(autouse=True)
def _clean_vip(browser):
    yield
    runtime.save(tg_vip_chats=[], tg_vip_topics={}, vip_open=[])


def _msg(reply_to=None):
    return NS(reply_to=reply_to)


def _hdr(msg_id=None, top=None, forum=True):
    return NS(forum_topic=forum, reply_to_msg_id=msg_id, reply_to_top_id=top)


# ------------------------------------------------------------ topics

def test_topic_of_reads_telegram_forum_messages():
    assert tg.topic_of(_msg(), forum=False) == (None, None)                       # a normal group
    assert tg.topic_of(_msg(_hdr(77, forum=False)), forum=False) == (None, 77)    # a reply in a normal group
    assert tg.topic_of(_msg(), forum=True) == (1, None)                           # General topic
    assert tg.topic_of(_msg(_hdr(5)), forum=True) == (5, None)                    # posted in topic 5
    assert tg.topic_of(_msg(_hdr(900, top=5)), forum=True) == (5, 900)            # a reply inside topic 5


def test_topic_route_only_reads_the_picked_topics():
    picked = {"5": "Scalp trades", "9": "Fx by KD"}
    assert tg.topic_route(KD, 5, picked, "Fx by KD VIP") == (True, f"{KD}:5", "Fx by KD VIP › Scalp trades")
    assert tg.topic_route(KD, 12, picked, "Fx by KD VIP")[0] is False             # Discussion: never read
    assert tg.topic_route(KD, 12, {}, "Fx by KD VIP") == (True, "", "Fx by KD VIP")  # nothing picked: whole group


def test_topics_are_their_own_groups_and_chat_cant_move_trades():
    FORUM = -1003141592                                 # a forum no other test posts in
    h = tg.handle_message
    with SessionLocal() as s:
        src = main_source(s).id
    r1 = h(src, 7001, "XAUUSD BUY ZONE 4030-4025\nSL 4020\nTP1 4040", 5, chat_id=FORUM,
           chat_title="Fx by FORUM VIP › Scalp trades", topic_id=5, grp=f"{FORUM}:5")
    assert r1.startswith("signal:")
    sid = int(r1.split(":")[1])
    with SessionLocal() as s:
        sig = s.get(Signal, sid)
        assert services.group_key(sig) == f"{FORUM}:5" and sig.tg_topic_id == 5
    # "move SL to BE" in another topic must not touch it; in the same topic it does
    assert h(src, 7002, "move SL to breakeven", None, chat_id=FORUM, topic_id=12) == "update_no_signal"
    assert h(src, 7003, "move SL to breakeven", None, chat_id=FORUM, topic_id=5).startswith("update:")


def test_house_rules_for_a_chat_cover_its_topics():
    with SessionLocal() as s:
        from server.db import set_setting
        set_setting(s, "group_rules", {str(KD): {"tp_target": "tp1"}})
        sig = Signal(source_id=main_source(s).id, side="buy", entry_low=1, entry_high=1, tg_chat_id=KD,
                     grp=f"{KD}:5")
        assert services.group_rule(s, sig) == {"tp_target": "tp1"}
        assert services.optimisation(s, f"{KD}:5") == ["Takes profit at TP1"]
        set_setting(s, "group_rules", {})
        s.commit()


# ------------------------------------------------------------ private VIP groups

def _customer(name, plan="standard"):
    with SessionLocal() as s:
        u = User(username=name, email=f"{name}@example.com", password_hash=security.hash_password("password123"),
                 plan_id=s.query(Plan).filter_by(key=plan).one().id)
        s.add(u)
        s.flush()
        acc = Account(user_id=u.id, login=f"88{u.id}", server="Demo", password_enc="x", settings={},
                      conn_status="online", copy_enabled=True, currency="USD", event_cursor=0,
                      live_state={"positions": [], "pendings": [], "waiting": []})
        s.add(acc)
        s.flush()
        services.ensure_default_sources(s, acc)
        s.commit()
        return u.id, acc.id


def _vip_signal(chat, title, msg):
    with SessionLocal() as s:
        src = main_source(s)
        from server.parser import parse_signal
        sig = services.create_signal(s, src, parse_signal("XAUUSD BUY 4030-4025 SL 4020 TP 4040"), "x", msg,
                                     tg_chat_id=chat, channel=title)
        s.commit()
        return sig.id


def _events(acc_id):
    with SessionLocal() as s:
        acc = s.get(Account, acc_id)
        evs = services.events_for_account(s, acc, limit=500)
        if evs:
            acc.event_cursor = max(acc.event_cursor, evs[-1].id)     # the worker acknowledges what it got
        out = [e.signal_id for e in evs]
        s.commit()
        return out


def test_private_groups_are_hidden_and_not_copied(browser):
    runtime.save(tg_vip_chats=[str(KD), "-100777"], vip_open=["-100777"])
    uid, aid = _customer("privy")
    with SessionLocal() as s:
        s.get(Account, aid).event_cursor = s.scalar(select(Event.id).order_by(Event.id.desc()).limit(1)) or 0
        s.commit()
    kd = _vip_signal(KD, "Fx by KD VIP", 81001)
    pf = _vip_signal(-100777, "Pips Farmer VIP", 81002)
    assert _events(aid) == [pf]                                  # only the group open to everyone
    browser.get("/login")
    browser.post("/login", {"username": "privy", "password": "password123"})
    for page in ("/dashboard", "/signals", f"/accounts/{aid}"):
        html = browser.get(page).text
        assert "Fx by KD" not in html, page
        assert "Pips Farmer VIP" in html, page
    # an admin turns KD on for them: shown and copied from the next signal
    with SessionLocal() as s:
        s.get(User, uid).vip_grants = [str(KD)]
        s.commit()
    kd2 = _vip_signal(KD, "Fx by KD VIP", 81003)
    assert _events(aid) == [kd2]
    assert "Fx by KD" in browser.get("/signals").text
    assert kd not in _events(aid)


def test_members_of_a_group_on_their_own_telegram_get_it():
    runtime.save(tg_vip_chats=[str(KD)])
    uid, aid = _customer("member")
    with SessionLocal() as s:
        u = s.get(User, uid)
        assert services.vip_keys(s, u) == set()
        u.tg_member_chats = [KD, -1009999]                       # -1009999 isn't a VIP group: not given by this
        s.commit()
        assert services.vip_keys(s, s.get(User, uid)) == {str(KD)}
    tg.save_membership(uid, [-1001])
    with SessionLocal() as s:
        assert services.vip_keys(s, s.get(User, uid)) == set()  # they left KD


def test_customers_who_already_copied_keep_their_groups():
    with SessionLocal() as s:
        from server.db import set_setting
        set_setting(s, "tg_vip_chats", [str(KD)])
        s.delete(s.get(Setting, "vip_access_v1"))
        s.commit()
    uid, _ = _customer("oldtimer")
    with SessionLocal() as s:
        seed(s)
        assert str(KD) in s.get(User, uid).vip_grants
        assert s.get(Setting, "vip_access_v1")


def test_admin_sets_topics_everyone_and_customer_access(browser, monkeypatch):
    b = admin_browser(browser)
    dialogs = [{"id": KD, "title": "Fx by KD VIP", "forum": True,
                "topics": [{"id": 1, "title": "General"}, {"id": 5, "title": "Scalp trades"},
                           {"id": 12, "title": "Discussion"}]},
               {"id": -100777, "title": "Pips Farmer VIP", "topics": []}]

    async def fake_dialogs():
        return dialogs
    monkeypatch.setattr(tg, "list_main_dialogs", fake_dialogs)
    monkeypatch.setattr(runtime, "main_session", lambda: "sess")
    monkeypatch.setattr(tg, "api_ready", lambda: True)
    runtime.save(tg_vip_chats=[str(KD)])
    page = b.get("/admin/telegram").text                         # used to break on the chat list
    assert "Scalp trades" in page and "Everyone gets it" in page and "Search your chats" in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = b.c.post("/admin/telegram/vip", data={"csrf": csrf, "chat_id": [str(KD), "-100777"],
                                              f"topic_{KD}": ["5"], "open_-100777": "on"})
    assert r.status_code in (200, 303)
    runtime.invalidate()
    assert runtime.vip_topics() == {str(KD): {"5": "Scalp trades"}}
    assert runtime.vip_open() == {"-100777"}
    uid, _ = _customer("granted")
    up = b.get(f"/admin/users/{uid}").text
    assert "VIP groups they get" in up and "Everyone gets it" in up
    b.post(f"/admin/users/{uid}/vip", {"chat": [str(KD), "-12345"]})
    with SessionLocal() as s:
        assert s.get(User, uid).vip_grants == [str(KD)]            # only real VIP groups


def test_customer_picks_topics_and_vip_groups_arent_doubled(browser, monkeypatch):
    runtime.save(tg_vip_chats=[str(KD)])
    uid, _ = _customer("topicky", plan="pro")
    with SessionLocal() as s:
        s.get(User, uid).tg_session_enc = security.encrypt("sess")
        s.get(User, uid).vip_grants = [str(KD)]
        s.commit()

    async def fake_page(user_id, session, wait=2.5):
        return [{"id": KD, "title": "Fx by KD VIP", "forum": True, "topics": [{"id": 5, "title": "Scalp"}]},
                {"id": -100555, "title": "Ray VIP", "forum": True,
                 "topics": [{"id": 1, "title": "General"}, {"id": 3, "title": "Signals"}]}], "ok"
    monkeypatch.setattr(tg, "dialogs_for_page", fake_page)
    browser.get("/login")
    browser.post("/login", {"username": "topicky", "password": "password123"})
    page = browser.get("/channels").text
    assert "Already copied for you" in page and "Only some topics?" in page
    browser.post("/channels/add", {"chat_id": "-100555", "title": "Ray VIP", "topic": "3", "tt_3": "Signals"})
    browser.post("/channels/add", {"chat_id": str(KD), "title": "Fx by KD VIP"})
    with SessionLocal() as s:
        mine = list(s.scalars(select(Source).where(Source.owner_user_id == uid, Source.kind == "channel")))
        assert [(x.tg_chat_id, x.topics) for x in mine] == [(-100555, {"3": "Signals"})]
    assert "only Signals" in browser.get("/channels").text


# ------------------------------------------------------------ login code

def test_phone_numbers_get_their_country_code():
    assert tg.normalise_phone("07700 900123", "44") == "+447700900123"
    assert tg.normalise_phone("+44 7700 900123", "1") == "+447700900123"
    assert tg.normalise_phone("0044 7700900123") == "+447700900123"
    assert tg.normalise_phone("447700900123", "44") == "+447700900123"
    assert tg.normalise_phone("3001234567", "92") == "+923001234567"


class FakeClient:
    def __init__(self, error=None):
        self.error = error
        self.disconnected = False

    async def connect(self):
        pass

    async def disconnect(self):
        self.disconnected = True

    async def send_code_request(self, phone):
        if self.error:
            raise self.error
        return NS(type=type("SentCodeTypeApp", (), {})(), next_type=type("CodeTypeSms", (), {})(),
                  phone_code_hash="h1", timeout=60)

    async def __call__(self, req):                              # ResendCodeRequest
        return NS(type=type("SentCodeTypeSms", (), {})(), next_type=None, phone_code_hash="h2", timeout=None)


def test_code_page_says_where_the_code_went(monkeypatch):
    monkeypatch.setattr(tg, "api_ready", lambda: True)
    fc = FakeClient()
    monkeypatch.setattr(tg, "_new_client", lambda *a: fc)
    asyncio.run(tg.start_user_login(4242, "+447700900123"))
    p = tg.pending_login(4242)
    assert "blue tick" in p["where"] and "not a text message" in p["where"] and p["next"] == "text message"
    asyncio.run(tg.resend_user_code(4242))
    p = tg.pending_login(4242)
    assert p["where"].startswith("by text message") and tg._pending_logins[4242]["hash"] == "h2"
    asyncio.run(tg.cancel_login(4242))
    assert tg.pending_login(4242) is None and fc.disconnected


def test_telegram_errors_in_plain_words(monkeypatch):
    monkeypatch.setattr(tg, "api_ready", lambda: True)
    flood = type("FloodWaitError", (Exception,), {})("wait")
    flood.seconds = 300
    monkeypatch.setattr(tg, "_new_client", lambda *a: FakeClient(flood))
    with pytest.raises(RuntimeError, match="wait 5 minutes"):
        asyncio.run(tg.start_user_login(4243, "+447700900123"))
    bad = type("PhoneNumberInvalidError", (Exception,), {})("x")
    monkeypatch.setattr(tg, "_new_client", lambda *a: FakeClient(bad))
    with pytest.raises(RuntimeError, match="doesn't know that number"):
        asyncio.run(tg.start_user_login(4243, "+4407700"))


def test_customer_phone_form_adds_the_country(browser, monkeypatch):
    _customer("phoney", plan="pro")
    browser.get("/login")
    browser.post("/login", {"username": "phoney", "password": "password123"})
    sent = []

    async def fake_start(uid, phone):
        sent.append(phone)
        tg._pending_logins[uid] = {"client": FakeClient(), "phone": phone, "hash": "h", "at": __import__("time").time(),
                                   "where": "in the Telegram app (look for a message from \"Telegram\").", "next": ""}
    monkeypatch.setattr(tg, "start_user_login", fake_start)
    monkeypatch.setattr(tg, "api_ready", lambda: True)
    page = browser.get("/channels").text
    assert "United Kingdom (+44)" in page
    r = browser.post("/channels/telegram/start", {"country": "44", "phone": "07700 900123"})
    assert sent == ["+447700900123"]
    assert "Telegram sent the code for" in r.text and "Send it again" in r.text and "Wrong number?" in r.text


# ------------------------------------------------------------ servers full

def test_capacity_message():
    base = {"total": 10, "in_use": 5, "free": 5, "online": 1, "memory_full": []}
    assert scheduler.capacity_text(base, 0, 2) == ""
    near = scheduler.capacity_text({**base, "in_use": 8, "free": 2}, 0, 2)
    assert near.startswith("⚠️ Servers nearly full") and "Add a VPS" in near
    full = scheduler.capacity_text({**base, "in_use": 10, "free": 0}, 3, 2)
    assert full.startswith("🚨 Servers full: 3 customer account(s) waiting") and "Open a new VPS now" in full
    mem = scheduler.capacity_text({**base, "free": 0, "memory_full": ["VPS 1"]}, 0, 2)
    assert "Out of memory" in mem and "VPS 1" in mem


def test_a_vps_out_of_memory_takes_no_new_accounts():
    with SessionLocal() as s:
        n = Node(name="Full RAM VPS", token_hash="z", max_slots=50, last_seen=utcnow(), info={"ram": 95})
        s.add(n)
        s.commit()
        cap = services.capacity(s)
        assert cap["free_by_node"][n.id] == 0 and "Full RAM VPS" in cap["memory_full"]
        n.enabled = False
        s.commit()
    notify.take_all()


def test_a_topic_keeps_its_own_group_settings():
    from server.plans import clean_groups
    g = clean_groups({f"{KD}:5": {"paused": True}, "junk:key": {"paused": True}})
    assert list(g) == [f"{KD}:5"] and g[f"{KD}:5"]["paused"] is True

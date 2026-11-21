"""Channels page: loads at once whatever Telegram is doing, and shows each channel's status and accounts."""
import asyncio
import time

import pytest

from server import security, services, telegram_service as tg
from server.db import Account, AccountSource, Plan, SessionLocal, Signal, Source, User, main_source
from tests.conftest import Browser


@pytest.fixture
def pro(app, browser, monkeypatch):
    monkeypatch.setattr(tg, "api_ready", lambda: True)
    with SessionLocal() as s:
        plan = s.query(Plan).filter_by(key="pro").first()
        n = s.query(User).count()
        u = User(username=f"chan{n}", email=f"chan{n}@example.com", password_hash=security.hash_password("password123"),
                 plan_id=plan.id, billing="lifetime", tg_session_enc=security.encrypt("sess"), tg_name="Chan Tester")
        s.add(u)
        s.flush()
        a = Account(user_id=u.id, login=f"55{u.id:04d}", server="Fake-Live", password_enc="x", label="Gold acc")
        s.add(a)
        s.flush()
        src = Source(kind="channel", owner_user_id=u.id, tg_chat_id=-100777 - u.id, title="Bullion Calls")
        s.add(src)
        s.flush()
        s.add(AccountSource(account_id=a.id, source_id=main_source(s).id))
        s.add(Signal(source_id=src.id, side="sell", entry_low=4050, entry_high=4055, sl=4065, tps=[4040]))
        s.commit()
        uid, aid, sid, main_id = u.id, a.id, src.id, main_source(s).id
    tg._dialog_cache.pop(uid, None)
    tg._dialog_errors.pop(uid, None)
    tg._dialog_tasks.pop(uid, None)
    b = Browser(app)
    b.get("/login")
    b.post("/login", {"username": f"chan{n}", "password": "password123"})
    return b, uid, aid, sid, main_id


def test_slow_telegram_never_hangs_the_page(pro, monkeypatch):
    b, uid, *_ = pro

    async def slow(user_id, session_str, client=None):
        await asyncio.sleep(20)
        return []
    monkeypatch.setattr(tg, "list_user_dialogs", slow)
    t0 = time.time()
    r = b.get("/channels")
    assert time.time() - t0 < 6 and r.status_code == 200
    assert "Fetching your Telegram chats" in r.text
    assert "Bullion Calls" in r.text and "Signals this week" in r.text        # channels show from the database


def test_unreachable_telegram_shows_a_clear_state(pro, monkeypatch):
    b, uid, *_ = pro

    async def down(user_id, session_str, client=None):
        raise ConnectionError("network unreachable")
    monkeypatch.setattr(tg, "list_user_dialogs", down)
    r = b.get("/channels")
    assert "couldn&#39;t reach Telegram" in r.text or "couldn't reach Telegram" in r.text
    assert "Your channels keep copying" in r.text


def test_cached_chats_cards_steps_and_copying(pro):
    b, uid, aid, sid, main_id = pro
    tg._dialog_cache[uid] = (time.time(), [{"id": -100123, "title": "Gold Signals Pro"}])
    r = b.get("/channels")
    page = r.text
    assert "Gold Signals Pro" in page and "Copy</button>" in page
    assert page.count('class="gcard') == 2                                    # VIP group + own channel
    assert "Not copied" in page and "Last signal" in page and "Sell" in page
    assert 'class="steps"' in page and "Link your Telegram" in page and 'class="savebar"' in page
    # tick the own channel for the account, untick the VIP group
    b.post("/channels/copying", {f"c{sid}": str(aid)})
    with SessionLocal() as s:
        assert set(services.account_source_ids(s, aid)) == {sid}
    page = b.get("/channels").text
    assert page.count('class="tag go"') + page.count('class="tag hold"') == 2


def test_not_linked_and_free_plan_empty_states(app, browser):
    with SessionLocal() as s:                    # made directly: sign-ups are rate limited per address
        s.add(User(username="chanfree", email="cf@example.com", password_hash=security.hash_password("password123"),
                   plan_id=s.query(Plan).filter_by(key="standard").first().id))
        s.commit()
    b = Browser(app)
    b.get("/login")
    b.post("/login", {"username": "chanfree", "password": "password123"})
    page = b.get("/channels").text
    assert "Copy your own Telegram channels" in page and "See Pro and Extreme" in page
    assert "Add an MT5 account" in page                                       # no accounts yet

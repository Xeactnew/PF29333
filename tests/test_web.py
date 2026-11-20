import re
from datetime import timedelta

from sqlalchemy import select

from server import notify, security, services
from server.db import Account, Admin, Renewal, SessionLocal, Signal, User, main_source, utcnow


def make_admin(name="boss", pw="supersecret1"):
    with SessionLocal() as s:
        if not s.scalar(select(Admin).where(Admin.username == name)):
            s.add(Admin(username=name, password_hash=security.hash_password(pw)))
            s.commit()


def admin_browser(app_browser):
    make_admin()
    app_browser.get("/admin/login")
    r = app_browser.post("/admin/login", {"username": "boss", "password": "supersecret1"})
    assert r.status_code == 200 and "Overview" in r.text
    return app_browser


def test_public_and_signup_flow(browser):
    assert browser.get("/").status_code == 200
    browser.get("/signup")
    r = browser.post("/signup", {"username": "alice", "password": "password123", "email": "alice@example.com"})
    assert r.status_code == 200 and "MT5 accounts" in r.text
    r = browser.get("/dashboard")
    assert "Add your first MT5 account" in r.text
    # no VPS yet -> account waits in the queue
    r = browser.post("/accounts/add", {"login": "5551234", "password": "mt5pass", "server": "Demo-Server"})
    assert "in the queue" in r.text or "servers are full" in r.text
    # plan limit (Standard = 1 account)
    r = browser.post("/accounts/add", {"login": "5559999", "password": "x", "server": "Demo-Server"})
    assert "allows 1 MT5 account" in r.text
    for page in ("/signals", "/channels", "/plan", "/accounts"):
        assert browser.get(page).status_code == 200, page
    # wrong password rejected
    browser.post("/logout", {})
    r = browser.post("/login", {"username": "alice", "password": "nope"})
    assert "Wrong username or password" in r.text


def test_locked_settings_are_clamped(browser):
    browser.get("/signup")
    browser.post("/signup", {"username": "bob", "password": "password123", "email": "bob@example.com"})
    r = browser.post("/accounts/add", {"login": "777001", "password": "pw", "server": "S1"})
    acc_id = int(re.search(r"/accounts/(\d+)/settings", r.text).group(1))
    browser.post(f"/accounts/{acc_id}/settings", {"lot_mode": "risk_percent", "risk_percent": "2", "layers": "5",
                                                  "smart_entry": "on", "fixed_lot": "0.05", "breakeven_at_tp1": "on"})
    with SessionLocal() as s:
        acc = s.get(Account, acc_id)
        eff = services.worker_settings(acc)
    assert eff["lot_mode"] == "fixed" and eff["layers"] == 1 and eff["smart_entry"] is False
    assert eff["fixed_lot"] == 0.05 and eff["breakeven_at_tp1"] is True


def test_admin_node_worker_signal_flow(app, browser):
    from tests.conftest import Browser
    b = admin_browser(browser)
    for page in ("/admin", "/admin/users", "/admin/renewals", "/admin/nodes", "/admin/signals", "/admin/plans",
                 "/admin/activity", "/admin/settings"):
        assert b.get(page).status_code == 200, page

    # create a Pro monthly login
    r = b.post("/admin/users/create", {"username": "carl", "plan_id": "2", "billing": "monthly", "password": "carlpass1"})
    assert "Login details to send them" in r.text and "carlpass1" in r.text

    # add a VPS and grab its key
    r = b.post("/admin/nodes/create", {"name": "VPS 1", "max_slots": "1"})
    token = [t.strip() for t in re.findall(r'<div class="secret"[^>]*>([^<]+)</div>', r.text) if "powershell" not in t][0]

    # carl logs in and adds an account
    cb = Browser(app)
    cb.get("/login")
    cb.post("/login", {"username": "carl", "password": "carlpass1"})
    r = cb.post("/accounts/add", {"login": "880011", "password": "mt5pw", "server": "Broker-Live"})
    acc_id = int(re.search(r"/accounts/(\d+)/settings", r.text).group(1))

    # node checks in -> gets assignments (slot = 1)
    H = {"Authorization": f"Bearer {token}"}
    r = b.c.post("/api/node/heartbeat", json={"version": "1"}, headers=H)
    assert r.status_code == 200
    assigned = {a["account_id"]: a for a in r.json()["assignments"]}
    with SessionLocal() as s:
        mine = s.get(Account, acc_id)
        on_node = mine.node_id is not None
    # only one slot: either alice's earlier account or carl's got it
    assert len(assigned) == 1
    if not on_node:
        # free the slot by raising capacity
        node_id = r.json()["node_id"]
        b.post(f"/admin/nodes/{node_id}", {"name": "VPS 1", "max_slots": "5", "enabled": "on"})
        r = b.c.post("/api/node/heartbeat", json={"version": "1"}, headers=H)
        assigned = {a["account_id"]: a for a in r.json()["assignments"]}
    assert acc_id in assigned and assigned[acc_id]["password"] == "mt5pw"

    # bad token rejected
    assert b.c.post("/api/node/heartbeat", json={}, headers={"Authorization": "Bearer nope"}).status_code == 401

    # first poll sets the cursor, no old events
    r = b.c.post(f"/api/worker/{acc_id}/poll", json={"report": {"status": "online", "account":
                 {"balance": 1000, "equity": 1000, "currency": "USD", "company": "Broker"}, "snapshot": True}}, headers=H)
    assert r.json()["events"] == [] and r.json()["copy_enabled"] is True

    # admin posts a VIP signal -> worker receives it
    b.post("/admin/signals/manual", {"side": "buy", "order_type": "market", "entry1": "4037", "entry2": "4030",
                                     "sl": "4027", "tp1": "4047", "tp2": "4057", "tp3": "4067"})
    r = b.c.post(f"/api/worker/{acc_id}/poll", json={}, headers=H).json()
    sigs = [e for e in r["events"] if e["kind"] == "signal"]
    assert sigs and sigs[-1]["signal"]["entry_low"] == 4030 and sigs[-1]["signal"]["tps"] == [4047, 4057, 4067]
    last = r["events"][-1]["id"]
    sig_id = sigs[-1]["signal"]["id"]

    # admin sends TP1 hit -> update arrives once acked
    b.post(f"/admin/signals/{sig_id}/update", {"kind": "tp_hit", "value": "1"})
    r = b.c.post(f"/api/worker/{acc_id}/poll", json={"ack": last}, headers=H).json()
    assert [e["update"]["kind"] for e in r["events"]] == ["tp_hit"]

    # worker reports closed deals -> stats
    now = (utcnow() - utcnow().replace(year=1970, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)).total_seconds()
    deals = [{"ticket": 1, "position_id": 9, "time": now, "symbol": "XAUUSD", "side": "buy", "entry": "out",
              "volume": 0.1, "price": 4047, "profit": 100.0, "ours": True, "signal_id": sig_id},
             {"ticket": 2, "position_id": 10, "time": now, "symbol": "XAUUSD", "side": "buy", "entry": "out",
              "volume": 0.1, "price": 4027, "profit": -30.0, "ours": True, "signal_id": sig_id}]
    assert b.c.post(f"/api/worker/{acc_id}/deals", json={"deals": deals}, headers=H).json()["added"] == 2
    assert b.c.post(f"/api/worker/{acc_id}/deals", json={"deals": deals}, headers=H).json()["added"] == 0
    r = cb.get("/dashboard")
    assert "+$70.00" in r.text and "50.0%" in r.text
    assert cb.get(f"/accounts/{acc_id}").status_code == 200
    assert b.get(f"/admin/accounts/{acc_id}").status_code == 200
    with SessionLocal() as s:
        u = s.scalar(select(User).where(User.username == "carl"))
    assert b.get(f"/admin/users/{u.id}").status_code == 200

    # customer manual signal goes only to their account
    r = cb.post("/signals/manual", {"side": "sell", "entry1": "4100", "sl": "4110", "tp1": "4090", "accounts": str(acc_id)})
    assert "Signal sent to 1 account" in r.text
    r = cb.post("/signals/manual", {"side": "sell", "entry1": "4100", "sl": "4090", "tp1": "4080", "accounts": str(acc_id)})
    assert "stop loss must be above" in r.text


def test_renewal_not_paid_closes_account(app, browser):
    from tests.conftest import Browser
    from server import scheduler
    b = admin_browser(browser)
    b.post("/admin/users/create", {"username": "dave", "plan_id": "3", "billing": "monthly", "password": "davepass1"})
    with SessionLocal() as s:
        u = s.scalar(select(User).where(User.username == "dave"))
        u.paid_until = utcnow() - timedelta(minutes=1)
        s.commit()
        uid = u.id
    notify.take_all()
    scheduler.renewal_tick()
    msgs = notify.take_all()
    assert any("dave" in m["text"] and m["buttons"] for m in msgs)
    with SessionLocal() as s:
        r = s.scalar(select(Renewal).where(Renewal.user_id == uid))
        assert s.get(User, uid).status == "payment_check"
        rid = r.id
    # dave can still log in while we check
    db = Browser(app)
    db.get("/login")
    assert "checking your monthly payment" in db.post("/login", {"username": "dave", "password": "davepass1"}).text
    # admin says not paid
    b.get("/admin/renewals")
    r = b.post(f"/admin/renewals/{rid}/not_paid", {})
    assert "NOT PAID" in r.text
    assert db.get("/dashboard").url.path == "/login"
    r = db.post("/login", {"username": "dave", "password": "davepass1"})
    assert "has been closed" in r.text
    # reopen + paid path
    b.post(f"/admin/users/{uid}/reopen", {})
    with SessionLocal() as s:
        u = s.get(User, uid)
        assert u.status == "active" and u.paid_until > utcnow()
        u.paid_until = utcnow() - timedelta(minutes=1)
        s.commit()
    scheduler.renewal_tick()
    with SessionLocal() as s:
        r = s.scalar(select(Renewal).where(Renewal.user_id == uid, Renewal.status == "pending"))
        msg = services.answer_renewal(s, r, True, "@partner")
        s.commit()
        assert "PAID" in msg and s.get(User, uid).status == "active"
        assert s.get(User, uid).paid_until > utcnow() + timedelta(days=25)


def test_telegram_messages_become_signals_and_updates():
    from server.telegram_service import handle_message
    with SessionLocal() as s:
        src = main_source(s).id
    res = handle_message(src, 501, "XAUUSD BUY ZONE : 4037-4030\nSL: 4027\nTP1: 4047\nTP2: 4057\nTP3: 4067", None)
    assert res.startswith("signal:")
    sig_id = int(res.split(":")[1])
    assert handle_message(src, 501, "same again", None) == "duplicate"
    assert handle_message(src, 502, "TP2 HIT, SET BREAKEVEN", 501) == "update:tp_hit,breakeven"
    assert handle_message(src, 503, "good morning team", None) == "ignored"
    assert handle_message(src, 501, "XAUUSD BUY ZONE : 4037-4030\nSL: 4029\nTP1: 4047", None, edited=True) == "levels_edit"
    assert handle_message(src, 504, "close all now", None) == "update:close"
    with SessionLocal() as s:
        sig = s.get(Signal, sig_id)
        assert sig.tp_reached == 2 and sig.status == "closed" and sig.sl == 4029


def test_capacity_alert_when_full():
    from server import scheduler
    notify.take_all()
    scheduler._alerts["capacity_at"] = 0
    scheduler._alerts["capacity_waiting"] = -1
    scheduler.assignment_tick()
    texts = [m["text"] for m in notify.take_all()]
    assert any("VPS" in t for t in texts)

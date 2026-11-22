"""1.3.35, website side of the broker finder: hints to the node, admin alert, calm customer messages."""
import re
from datetime import timedelta

from sqlalchemy import select

from server import customer_alerts, notify, scheduler, security
from server.db import Account, Node, Plan, SessionLocal, User, utcnow
from tests.test_web import admin_browser


def _node_token(b, name):
    r = b.post("/admin/nodes/create", {"name": name, "max_slots": "5"})
    return [t.strip() for t in re.findall(r'<div class="secret"[^>]*>([^<]+)</div>', r.text) if "powershell" not in t][0]


def test_node_gets_broker_hints_and_reports_searches(browser):
    b = admin_browser(browser)
    H = {"Authorization": f"Bearer {_node_token(b, 'VPS Finder')}"}
    with SessionLocal() as s:
        node = s.scalar(select(Node).where(Node.name == "VPS Finder"))
        u = s.scalar(select(User))
        s.add(Account(user_id=u.id, login="106644", server="TradeQuo-Demo", password_enc=security.encrypt("pw"),
                      node_id=node.id, settings={}))
        s.commit()
    body = b.c.post("/api/node/heartbeat", headers=H, json={
        "broker_searches": [{"server": "TradeQuo-Demo", "tries": 1, "gave_up": False}], "finder_busy": True}).json()
    assert "TradeQuo" in body["broker_names"] and "Vantage" in body["broker_names"]
    a = [x for x in body["assignments"] if x["login"] == "106644"][0]
    assert a["broker"] == "TradeQuo"
    with SessionLocal() as s:
        info = s.scalar(select(Node).where(Node.name == "VPS Finder")).info
        assert info["broker_searches"][0]["server"] == "TradeQuo-Demo" and info["finder_busy"] is True


def test_admins_hear_when_a_lookup_gives_up(browser):
    notify.take_all()
    with SessionLocal() as s:
        s.add(Node(name="Finder VPS", token_hash="y", max_slots=5, last_seen=utcnow(),
                   info={"broker_searches": [{"server": "Weird-Live", "query": "Weird", "searched": True,
                                              "gave_up": True}]}))
        s.commit()
    scheduler.watch_tick()
    texts = " ".join(i["text"] for i in notify.take_all())
    assert "Weird-Live" in texts and "add a broker" in texts
    scheduler.watch_tick()
    assert not [i for i in notify.take_all() if "Weird-Live" in i["text"]]      # once a day, not every tick


def test_customer_sees_setting_up_not_an_error(browser):
    with SessionLocal() as s:
        u = User(username="newbroker", email="nb@example.com", password_hash=security.hash_password("password123"),
                 plan_id=s.query(Plan).filter_by(key="pro").one().id)
        s.add(u)
        s.flush()
        acc = Account(user_id=u.id, login="555", server="TradeQuo-Demo", password_enc="x", settings={},
                      conn_status="error", conn_error="We're looking up the server for you",
                      live_state={"unknown_server": True})
        s.add(acc)
        s.commit()
        aid = acc.id
    browser.get("/login")
    browser.post("/login", {"username": "newbroker", "password": "password123"})
    page = browser.get(f"/accounts/{aid}").text
    assert "Setting up your broker" in page and "Setting up</span>" in page
    assert "can't log in" not in page


def test_customer_alert_waits_for_the_lookup(browser):
    rep = {"status": "error", "error": "We're looking up the server", "unknown_server": True}
    assert customer_alerts._problem(rep) == "broker"
    assert customer_alerts.PROBLEM_DELAY["broker"] >= 20 * 60
    assert customer_alerts._problem({**rep, "unknown_server": False}) == "mt5"
    assert timedelta(seconds=customer_alerts.PROBLEM_DELAY["broker"]) > timedelta(minutes=20)

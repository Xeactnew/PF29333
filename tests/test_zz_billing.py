"""Broker list, plan requests, time limits, reminder emails, support tickets."""
import re
from datetime import timedelta

from sqlalchemy import select

from server import billing, brokers, mailer, notify
from server.db import Account, Email, Plan, SessionLocal, Ticket, UpgradeRequest, User, set_setting, utcnow
from tests.conftest import Browser
from tests.test_web import admin_browser


def _emails(to=None):
    with SessionLocal() as s:
        q = select(Email).order_by(Email.id)
        if to:
            q = q.where(Email.to == to)
        return list(s.scalars(q))


def test_broker_names_are_tidied():
    assert brokers.canonical("vantageinternational-live") == "VantageMarkets-Live"
    assert brokers.canonical(" icmarketssc mt5 2 ") == "ICMarketsSC-MT5-2"
    assert brokers.canonical("puprime-live") == "PUPrime-Live"
    assert brokers.canonical("tradequo-server") == "TradeQuo-Server"
    assert brokers.canonical("SomeNewBroker-Live") == "SomeNewBroker-Live"          # unknown: kept as typed
    assert brokers.canonical("mybroker live", learned=["MyBroker-Live"]) == "MyBroker-Live"
    assert brokers.broker_of("Exness-MT5Real7") == "Exness"
    assert len(brokers.all_servers()) > 200
    for name in ("Vantage", "PU Prime", "TradeQuo", "IC Markets"):
        assert brokers.BROKERS[name]


def test_signup_for_paid_plan_and_admin_marks_paid(app, browser):
    notify.take_all()
    browser.get("/signup?plan=pro&billing=monthly")
    r = browser.post("/signup", {"username": "paula", "password": "password123", "email": "paula@example.com",
                                 "plan": "pro", "billing": "monthly"})
    assert "Waiting for your payment" in r.text and "t.me/xaupipsfarmer" in r.text
    ref = re.search(r"PF-(\d+)", r.text).group(1)
    sent = notify.take_all()
    assert any(f"PF-{ref}" in i["text"] and i["buttons"] for i in sent)
    # missing email is refused
    other = Browser(app)
    other.get("/signup")
    r = other.post("/signup", {"username": "noemail", "password": "password123"})
    assert r.status_code == 400 and "email" in r.text.lower()

    b = admin_browser(Browser(app))
    assert f"PF-{ref}" in b.get("/admin").text
    b.post(f"/admin/requests/{ref}/approve", {})
    with SessionLocal() as s:
        u = s.scalar(select(User).where(User.username == "paula"))
        assert u.plan.key == "pro" and u.billing == "monthly" and u.paid_until > utcnow() + timedelta(days=27)
        assert s.get(UpgradeRequest, int(ref)).status == "approved"
    assert any("Pro" in e.subject for e in _emails("paula@example.com"))
    # the Telegram button path answers it too (already handled -> says so)
    assert "already approved" in billing.answer_request(int(ref), True, "@x")

    # customer asks for lifetime from the Plan page
    r = browser.post("/plan/request", {"plan_id": str(u.plan_id), "billing": "lifetime"})
    assert "Waiting for your payment" in r.text and "lifetime" in r.text


def test_time_limit_closes_account_and_reminders(app):
    b = admin_browser(Browser(app))
    with SessionLocal() as s:
        pro = s.scalar(select(Plan).where(Plan.key == "pro"))
    b.get("/admin/users")
    r = b.post("/admin/users/create", {"username": "timmy", "email": "timmy@example.com", "plan_id": str(pro.id),
                                       "billing": "lifetime", "time_limit": "7", "password": "timmypass1"})
    assert "Access until" in r.text
    with SessionLocal() as s:
        u = s.scalar(select(User).where(User.username == "timmy"))
        uid, exp = u.id, u.expires_at
    assert timedelta(days=6) < exp - utcnow() < timedelta(days=8)

    billing.billing_tick(exp - timedelta(days=3))
    billing.billing_tick(exp - timedelta(days=3))
    billing.billing_tick(exp - timedelta(days=2))
    mails = _emails("timmy@example.com")
    assert len(mails) == 1 and "3 days" in mails[0].subject and "xaupipsfarmer" in mails[0].body
    billing.billing_tick(exp - timedelta(days=1))
    assert len(_emails("timmy@example.com")) == 2

    notify.take_all()
    billing.billing_tick(exp + timedelta(minutes=1))
    with SessionLocal() as s:
        assert s.get(User, uid).status == "closed"
    assert any("time limit" in i["text"] for i in notify.take_all())
    assert "ended" in _emails("timmy@example.com")[-1].subject

    # admin gives them 30 more days: reopened
    b.get(f"/admin/users/{uid}")
    b.post(f"/admin/users/{uid}/plan", {"plan_id": str(pro.id), "billing": "lifetime", "time_limit": "30"})
    with SessionLocal() as s:
        u = s.get(User, uid)
        assert u.status == "active" and u.expires_at > utcnow() + timedelta(days=29)
    b.post(f"/admin/users/{uid}/plan", {"plan_id": str(pro.id), "billing": "lifetime", "time_limit": "none"})
    with SessionLocal() as s:
        assert s.get(User, uid).expires_at is None


def test_monthly_payment_reminder():
    with SessionLocal() as s:
        pro = s.scalar(select(Plan).where(Plan.key == "pro"))
        u = User(username="monty", email="monty@example.com", password_hash="x", plan_id=pro.id, plan=pro,
                 billing="monthly", paid_until=utcnow() + timedelta(days=1, hours=2))
        s.add(u)
        s.commit()
    billing.billing_tick()
    mails = _emails("monty@example.com")
    assert len(mails) == 1 and "renews" in mails[0].subject


def test_support_ticket_flow(app, browser):
    browser.get("/login")
    browser.post("/login", {"username": "paula", "password": "password123"})
    assert browser.get("/support").status_code == 200
    r = browser.post("/support", {"subject": "Account won't connect", "message": "It says wrong password."})
    tid = int(re.search(r"/support/(\d+)", str(r.url)).group(1))
    sup = _emails("bhatiiraza06@gmail.com")
    assert sup and f"#{tid}" in sup[-1].subject and sup[-1].reply_to == "paula@example.com"

    b = admin_browser(Browser(app))
    assert "Account won" in b.get("/admin/support").text
    b.get(f"/admin/support/{tid}")
    b.post(f"/admin/support/{tid}", {"message": "Check the server name is VantageMarkets-Live."})
    with SessionLocal() as s:
        t = s.get(Ticket, tid)
        assert t.status == "answered" and len(t.messages) == 2
    assert "VantageMarkets-Live" in _emails("paula@example.com")[-1].body
    assert "VantageMarkets-Live" in browser.get(f"/support/{tid}").text
    # someone else can't read it
    other = Browser(app)
    other.get("/login")
    other.post("/login", {"username": "bob", "password": "password123"})
    assert other.get(f"/support/{tid}").status_code == 404

    # public help form (can't log in)
    anon = Browser(app)
    anon.get("/help")
    r = anon.post("/help", {"name": "Sam", "email": "sam@example.com", "message": "Forgot my username"})
    assert "got your message" in r.text
    assert any("Sam" in e.body for e in _emails("bhatiiraza06@gmail.com"))


def test_email_queue_sends_and_retries():
    with SessionLocal() as s:
        set_setting(s, "smtp_host", "smtp.example.com")
        set_setting(s, "smtp_user", "me@example.com")
        from server import security
        set_setting(s, "smtp_password_enc", security.encrypt("app-pass"))
        mailer.queue(s, "x@example.com", "hello", "body")
        s.commit()
    calls = []

    def boom(c, e):
        raise TimeoutError("smtp down")
    assert mailer.send_pending(sender=boom) == 0
    n = mailer.send_pending(sender=lambda c, e: calls.append(e.to))
    assert n >= 1 and "x@example.com" in calls
    assert all(e.status == "sent" for e in _emails("x@example.com"))


def test_account_server_is_tidied_and_keep_screen_tool(app, browser):
    browser.get("/login")
    browser.post("/login", {"username": "paula", "password": "password123"})
    r = browser.get("/accounts")
    assert "VantageMarkets-Live" in r.text and "data-broker-filter" in r.text
    browser.post("/accounts/add", {"login": "8675309", "password": "pw", "server": "puprime-live"})
    with SessionLocal() as s:
        assert s.scalar(select(Account).where(Account.login == "8675309")).server == "PUPrime-Live"
    from server import system
    assert "tscon" in (system.ROOT / "tools" / "keep_screen_on.ps1").read_text()

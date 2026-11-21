"""1.3.30: the Accounts tab shows every customer (filters), plans change in the table, remove / delete."""
from sqlalchemy import select

from server import security
from server.db import Account, Activity, Deal, Plan, SessionLocal, User
from tests.conftest import Browser
from tests.test_web import admin_browser


def _make(name, with_acc):
    with SessionLocal() as s:
        u = User(username=name, email=f"{name}@example.com", password_hash=security.hash_password("password123"),
                 plan_id=s.query(Plan).filter_by(key="standard").one().id)
        s.add(u)
        s.flush()
        aid = None
        if with_acc:
            a = Account(user_id=u.id, login=f"88{u.id:04d}", server="PUPrime-Live", password_enc="x", settings={})
            s.add(a)
            s.flush()
            from datetime import datetime
            s.add(Deal(account_id=a.id, ticket=1, time=datetime(2026, 11, 1), entry="out", profit=5))
            aid = a.id
        s.commit()
        return u.id, aid


def test_accounts_tab_lists_every_customer_and_changes_plans(app):
    adm = admin_browser(Browser(app))
    uid, _ = _make("nomt5guy", False)
    uid2, aid2 = _make("hasmt5", True)
    assert "nomt5guy" not in adm.get("/admin/accounts").text                 # default: MT5 accounts
    page = adm.get("/admin/accounts?show=all").text
    assert "nomt5guy" in page and "hasmt5" in page and "No MT5 account" in page
    page = adm.get("/admin/accounts?show=none").text
    assert "nomt5guy" in page and ">hasmt5<" not in page
    with SessionLocal() as s:
        ext = s.query(Plan).filter_by(key="extreme").one().id
    r = adm.post(f"/admin/users/{uid}/plan", {"plan_id": str(ext), "billing": "lifetime", "time_limit": "keep",
                                              "paid_until": "", "next": "/admin/accounts?show=all"})
    assert "/admin/accounts?show=all" in str(r.url)
    with SessionLocal() as s:
        u = s.get(User, uid)
        assert u.plan.key == "extreme" and u.billing == "lifetime"
    r = adm.post(f"/admin/users/{uid}/plan", {"plan_id": str(ext), "billing": "free", "next": "https://evil.example"})
    assert "evil" not in str(r.url)
    assert "Extreme" in adm.get("/admin/accounts?show=all&plan=extreme").text


def test_remove_mt5_account_and_delete_customer(app):
    adm = admin_browser(Browser(app))
    uid, aid = _make("goner", True)
    adm.get(f"/admin/accounts/{aid}")
    adm.post(f"/admin/accounts/{aid}/action", {"action": "remove"})
    with SessionLocal() as s:
        a = s.get(Account, aid)
        assert a.removed and a.password_enc == "" and a.node_id is None
    adm.get(f"/admin/users/{uid}")
    adm.post(f"/admin/users/{uid}/delete", {"confirm": "wrong"})
    with SessionLocal() as s:
        assert s.get(User, uid)
    r = adm.post(f"/admin/users/{uid}/delete", {"confirm": "goner"})
    assert r.status_code == 200
    with SessionLocal() as s:
        assert s.get(User, uid) is None and s.get(Account, aid) is None
        assert not s.scalars(select(Deal).where(Deal.account_id == aid)).all()
        assert s.scalar(select(Activity).where(Activity.action == "customer_deleted"))

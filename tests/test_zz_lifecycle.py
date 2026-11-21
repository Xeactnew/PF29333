"""1.3.32: the customer lifecycle — join, settings, leave, rejoin, delete — and that MT5 and Telegram
logins stay reachable in the archive, with the Telegram session dropped only when access is withdrawn."""
from sqlalchemy import select

from server import security, services, telegram_service as ts, vault


def _allow_signups():
    security._attempts.clear()   # the per-IP signup limit is shared across the whole test session
from server.db import (Account, AccountSource, ArchivedCredential, Plan, SessionLocal, Source, User,
                       main_source, utcnow)
from server.plans import clean_settings, effective_settings, locked_keys
from tests.conftest import Browser
from tests.test_web import admin_browser


def _mk(name, plan="pro", email=None, tg=True, logins=(("700111", "PUPrime-Live"),)):
    with SessionLocal() as s:
        u = User(username=name, email=email or f"{name}@example.com",
                 password_hash=security.hash_password("password123"),
                 plan_id=s.query(Plan).filter_by(key=plan).one().id, billing="lifetime")
        if tg:
            u.tg_session_enc = security.encrypt(f"session-for-{name}")
            u.tg_name, u.tg_phone = f"{name} TG", "+447700900000"
        s.add(u)
        s.flush()
        src = main_source(s)
        if tg:
            ch = Source(kind="channel", owner_user_id=u.id, tg_chat_id=-100900 - u.id, title=f"{name}'s channel")
            s.add(ch)
            s.flush()
        for login, server in logins:
            a = Account(user_id=u.id, login=login, server=server, password_enc=security.encrypt("mt5pw"),
                        settings={"fixed_lot": 0.05}, conn_status="online", copy_enabled=True)
            s.add(a)
            s.flush()
            services.ensure_default_sources(s, a)
            if tg:
                s.add(AccountSource(account_id=a.id, source_id=ch.id))
        s.commit()
        return u.id


# ---- joining and settings -------------------------------------------------

def test_join_add_account_and_save_settings(app, browser):
    _allow_signups()
    browser.get("/signup")
    browser.post("/signup", {"username": "joiner", "password": "password123", "email": "joiner@example.com"})
    r = browser.post("/accounts/add", {"login": "12345678", "password": "pw", "server": "icmarketssc-mt5"})
    with SessionLocal() as s:
        acc = s.scalar(select(Account).where(Account.login == "12345678"))
        assert acc and acc.server == "ICMarketsSC-MT5" and acc.password_enc       # tidied + encrypted
    browser.post(f"/accounts/{acc.id}/settings", {"fixed_lot": "0.02", "lot_mode": "fixed", "layers": "1",
                                                  "tp_target": "spread", "breakeven_at_tp1": "on", "sources": [str(main_source_id())]})
    with SessionLocal() as s:
        assert clean_settings(s.get(Account, acc.id).settings)["fixed_lot"] == 0.02


def main_source_id():
    with SessionLocal() as s:
        return main_source(s).id


def test_settings_dont_clash():
    # plan-locked options can't be forced on by a low plan
    std = {"daily_limits": False, "signal_filters": False, "tp_control": False, "smart_entry": False,
           "risk_modes": False, "breakeven": True, "news_protection": False, "max_layers": 1}
    e = effective_settings({"max_total_loss": 500, "no_hedging": "skip_new", "runner_pct": 30,
                            "tp_target": "partials", "layers": 8, "lot_mode": "risk_percent",
                            "smart_entry": True, "news_protection": True}, std)
    assert e["max_total_loss"] == 0 and e["no_hedging"] == "off" and e["runner_pct"] == 0
    assert e["tp_target"] == "spread" and e["layers"] == 1 and e["lot_mode"] == "fixed"
    assert e["smart_entry"] is False and e["news_protection"] is False
    # a per-group override with its own size doesn't drop the group's other fields
    c = clean_settings({"groups": {"-5": {"own": True, "lot_mode": "fixed", "fixed_lot": 0.1, "layers": 2,
                                          "tp_target": "tp1", "optimised": False, "entry_expiry_min": "720"}}})
    g = c["groups"]["-5"]
    assert g["own"] and g["fixed_lot"] == 0.1 and g["tp_target"] == "tp1" and g["optimised"] is False
    assert g["entry_expiry_min"] == 720
    assert {"max_total_loss", "no_hedging", "runner_pct", "tp_partials"} <= locked_keys(std)


# ---- leaving and coming back ----------------------------------------------

def test_leaving_archives_and_reopening_restores():
    uid = _mk("leaver")
    with SessionLocal() as s:
        services.close_user(s, s.get(User, uid), "admin")
        s.commit()
        u = s.get(User, uid)
        assert u.status == "closed"
        # the live MT5 is stopped, but the password and Telegram link are still on the row (reopen needs them)
        assert all(a.conn_status == "stopped" and a.node_id is None for a in u.accounts)
        assert u.tg_session_enc
        arc = vault.has_archive(s, "leaver")
        assert arc and arc.reason == "left" and arc.tg_session_enc and arc.mt5
    # a closed customer's Telegram reader is not loaded (so we don't read their chats while they're gone)
    assert uid not in ts._load_user_channels()
    with SessionLocal() as s:
        services.reopen_user(s, s.get(User, uid), "admin")
        s.commit()
        u = s.get(User, uid)
        assert u.status == "active" and u.tg_session_enc
        assert all(a.waiting_since for a in u.accounts if not a.removed)
    assert uid in ts._load_user_channels()        # reader runs again, same session — never logged out


def test_telegram_stays_logged_in_across_close_and_reopen():
    uid = _mk("stayer")
    with SessionLocal() as s:
        before = s.get(User, uid).tg_session_enc
        services.close_user(s, s.get(User, uid), "admin"); s.commit()
        assert s.get(User, uid).tg_session_enc == before          # not wiped on leaving
        services.reopen_user(s, s.get(User, uid), "admin"); s.commit()
        assert s.get(User, uid).tg_session_enc == before          # still the same session


# ---- deleting -------------------------------------------------------------

def test_delete_keeps_mt5_archive_but_not_telegram_and_fresh_signup_restores(app):
    uid = _mk("gone", logins=(("700222", "PUPrime-Live"), ("700333", "ICMarketsSC-MT5")))
    with SessionLocal() as s:
        services.delete_user(s, s.get(User, uid), "admin")
        s.commit()
        assert s.get(User, uid) is None
        arc = vault.has_archive(s, "gone")
        assert arc and arc.reason == "deleted" and len(arc.mt5) == 2
        assert not arc.tg_session_enc                 # deleting removes our Telegram access
    # the same person signs up again -> their MT5 accounts come back, no Telegram
    _allow_signups()
    b = Browser(app)
    b.get("/signup")
    r = b.post("/signup", {"username": "gone", "password": "password123", "email": "gone@example.com"})
    assert r.status_code == 200
    with SessionLocal() as s:
        u = s.scalar(select(User).where(User.username == "gone"))
        accs = list(s.scalars(select(Account).where(Account.user_id == u.id)))
        # a fresh signup is on the free plan (1 account), so one comes back now, the other waits in the archive
        assert len(accs) == 1 and accs[0].login in ("700222", "700333") and accs[0].password_enc
        assert not u.tg_session_enc
        # upgrading to Pro brings the second one back from the archive
        services.set_plan(s, u, s.query(Plan).filter_by(key="pro").one(), "lifetime", None, "admin")
        s.commit()
        accs = list(s.scalars(select(Account).where(Account.user_id == u.id)))
        assert {a.login for a in accs} == {"700222", "700333"}


def test_delete_with_purge_leaves_nothing():
    uid = _mk("erase")
    with SessionLocal() as s:
        uname, uemail = s.get(User, uid).username, s.get(User, uid).email
        services.delete_user(s, s.get(User, uid), "admin")
        vault.purge(s, uname, uemail)
        s.commit()
        assert vault.has_archive(s, "erase") is None


def test_restore_respects_the_new_plans_account_limit():
    uid = _mk("downgrade", plan="extreme",
              logins=(("111", "A-Live"), ("222", "B-Live"), ("333", "C-Live")))
    with SessionLocal() as s:
        services.delete_user(s, s.get(User, uid), "admin")
        s.commit()
    with SessionLocal() as s:                      # comes back on the free plan: only 1 account reconnects
        u = User(username="downgrade", email="downgrade@example.com",
                 password_hash=security.hash_password("password123"),
                 plan_id=s.query(Plan).filter_by(key="standard").one().id)
        s.add(u)
        s.flush()
        out = vault.restore(s, u)
        s.commit()
        assert out["mt5"] == 1 and len(list(s.scalars(select(Account).where(Account.user_id == u.id)))) == 1


# ---- withdrawing Telegram -------------------------------------------------

def test_unlinking_telegram_wipes_it_everywhere(app):
    uid = _mk("unlinker")
    with SessionLocal() as s:                      # it's in the archive first (from a prior leave)
        services.close_user(s, s.get(User, uid), "admin")
        services.reopen_user(s, s.get(User, uid), "admin")
        s.commit()
        assert vault.has_archive(s, "unlinker").tg_session_enc
    b = Browser(app)
    b.get("/login")
    b.post("/login", {"username": "unlinker", "password": "password123"})
    b.post("/channels/telegram/unlink", {})
    with SessionLocal() as s:
        u = s.scalar(select(User).where(User.username == "unlinker"))
        assert not u.tg_session_enc and not u.tg_name
        assert not vault.has_archive(s, "unlinker").tg_session_enc      # gone from the archive too
    assert uid not in ts._load_user_channels()


def test_main_reader_session_is_not_cleared_on_disconnect():
    # the reader only forgets its login if Telegram itself logs it out; a normal disconnect keeps it
    from server import runtime
    import server.telegram_service as t
    src = __import__("inspect").getsource(t.run_main_reader)
    assert 'tg_main_session_enc=""' in src                      # cleared only inside the not-authorized branch
    assert "is_user_authorized" in src
    # restored credentials come back through runtime, which reads the saved (encrypted) session
    assert "main_session" in __import__("inspect").getsource(runtime)

"""1.3.29: optimised copying per group, "optimise this group" (Extreme), broker → server dropdowns."""
from server import brokers, notify, security, services
from server.db import Account, Plan, SessionLocal, Signal, User, get_setting, main_source, set_setting
from tests.test_web import admin_browser
from tests.conftest import Browser
from tests.test_worker import SIG, move, rig  # noqa: F401

KD, OTHER = "-1004498117492", "-1007770001"


def _login(browser, name, plan="standard"):
    with SessionLocal() as s:
        if not s.query(User).filter_by(username=name).first():
            s.add(User(username=name, email=f"{name}@example.com", password_hash=security.hash_password("password123"),
                       plan_id=s.query(Plan).filter_by(key=plan).one().id))
            s.commit()
    browser.get("/login")
    browser.post("/login", {"username": name, "password": "password123"})


def _customer(browser, name, plan="standard"):
    _login(browser, name, plan)
    with SessionLocal() as s:
        set_setting(s, "group_rules", {KD: {"be_lock_pips": 15.0, "sl_move_as_be": True, "tp_target": "tp1",
                                            "wait_for_zone": True}})
        u = s.query(User).filter_by(username=name).one()
        u.plan_id = s.query(Plan).filter_by(key=plan).one().id
        acc = Account(user_id=u.id, login=f"77{u.id}", server="Demo", password_enc="x", settings={},
                      conn_status="online", copy_enabled=True, currency="USD",
                      live_state={"positions": [], "pendings": [], "waiting": []})
        s.add(acc)
        s.flush()
        services.ensure_default_sources(s, acc)
        src = main_source(s)
        for chat, title, mid in ((int(KD), "Fx by KD", 1), (int(OTHER), "Some Group", 2)):
            s.add(Signal(source_id=src.id, side="buy", entry_low=4030, entry_high=4037, sl=4027, tps=[4047],
                         tg_chat_id=chat, tg_msg_id=9000 + u.id * 10 + mid, channel=title))
        s.commit()
        return acc.id


def test_optimised_copying_switch(browser):
    aid = _customer(browser, "opty")
    page = browser.get(f"/accounts/{aid}").text
    assert "✦ Optimised copying" in page and "Every layer takes profit at TP1" in page
    assert "Copy exactly as posted instead" in page
    assert "✦ Optimised" in browser.get("/dashboard").text
    browser.post(f"/accounts/{aid}/groups/{KD}/optimised", {"optimised": "off"})
    with SessionLocal() as s:
        assert s.get(Account, aid).settings["groups"][KD]["optimised"] is False
        assert services.worker_settings(s.get(Account, aid))["groups"][KD]["optimised"] is False
    # saving the group's other settings keeps the switch where the customer put it
    browser.post(f"/accounts/{aid}/groups/{KD}", {"trading": "on"})
    with SessionLocal() as s:
        assert s.get(Account, aid).settings["groups"][KD]["optimised"] is False
    assert "Turn optimised copying on" in browser.get(f"/accounts/{aid}").text
    browser.post(f"/accounts/{aid}/groups/{KD}/optimised", {"optimised": "on"})
    with SessionLocal() as s:
        assert "optimised" not in (s.get(Account, aid).settings["groups"].get(KD) or {})


def test_optimise_this_group_needs_extreme(browser, app):
    aid = _customer(browser, "standy")
    page = browser.get(f"/accounts/{aid}").text
    assert "✦ Optimise this group" in page and "Not possible for every group" in page
    assert "Needs Extreme" in page
    r = browser.post(f"/accounts/{aid}/groups/{OTHER}/optimise", {})
    assert "/plan" in str(r.url)
    with SessionLocal() as s:
        assert OTHER not in (get_setting(s, "optimise_requests") or {})

    b2 = Browser(app)
    aid2 = _customer(b2, "xtreme", plan="extreme")
    assert "Needs Extreme" not in b2.get(f"/accounts/{aid2}").text
    b2.post(f"/accounts/{aid2}/groups/{OTHER}/optimise", {})
    with SessionLocal() as s:
        req = get_setting(s, "optimise_requests")[OTHER]
        assert req["status"] == "requested" and req["title"] == "Some Group"
    assert any("asked us to optimise Some Group" in t for _, t in notify.recent)
    assert "We're looking at it" in b2.get(f"/accounts/{aid2}").text
    b2.post(f"/accounts/{aid2}/groups/{KD}/optimise", {})          # already optimised: no request
    with SessionLocal() as s:
        assert KD not in get_setting(s, "optimise_requests")

    adm = admin_browser(Browser(app))
    page = adm.get("/admin/telegram").text
    assert "Optimisation requests" in page and "Some Group" in page
    adm.post("/admin/telegram/optimise-requests", {f"st_{OTHER}": "not_possible", f"note_{OTHER}": "Too few calls."})
    card = b2.get(f"/accounts/{aid2}").text
    assert "it can't be optimised" in card and "Too few calls." in card


def test_worker_follows_the_optimised_switch(rig):
    mt5, api, w, clock = rig
    rule = {"tp_target": "tp1", "wait_for_zone": True}
    move(rig, 4045)
    # switched off: the quick call goes straight in and the account's own targets apply
    api.settings.update(layers=3, tp_target="spread", instant_entry=True, groups={KD: {"optimised": False}})
    api.push("signal", signal={**SIG, "id": 301, "group": KD, "rule": rule})
    w.step()
    assert len({o.tp for o in mt5.orders.values()} | {p.tp for p in mt5.positions.values()}) > 1
    # own size + optimised: own lots, optimised targets
    mt5.orders.clear(); mt5.positions.clear()
    api.settings.update(groups={KD: {"own": True, "lot_mode": "fixed", "fixed_lot": 0.04, "layers": 2,
                                     "tp_target": "spread"}})
    api.push("signal", signal={**SIG, "id": 302, "group": KD, "rule": rule})
    w.step()
    live = list(mt5.orders.values())
    assert len(live) == 2 and {o.tp for o in live} == {SIG["tps"][0]} and {o.volume_current for o in live} == {0.04}


def test_broker_then_server_dropdowns(browser):
    c = brokers.choices(["ICMarketsSC-MT5-9", "NewBroker-Live", "PUPrime-Live"])
    assert c["IC Markets"][-1] == "ICMarketsSC-MT5-9" and c["NewBroker"] == ["NewBroker-Live"]
    assert c["PU Prime"].count("PUPrime-Live") == 1
    _login(browser, "brokery")
    page = browser.get("/accounts").text
    assert "data-srvpick" in page and "Choose your broker" in page and "My broker isn't listed" in page
    assert '<optgroup label="Prop firms"><option value="FundingPips">' in page
    aid = _customer(Browser(browser.c.app), "brokery2")
    with SessionLocal() as s:                       # give the account to the customer looking at the page
        s.get(Account, aid).user_id = s.query(User).filter_by(username="brokery").one().id
        s.commit()
    acc_page = browser.get(f"/accounts/{aid}").text
    assert acc_page.count("data-srvpick") == 1 and 'value="Demo"' in acc_page


def test_new_plan_features_reach_existing_plans():
    from server.db import seed
    with SessionLocal() as s:
        p = s.query(Plan).filter_by(key="extreme").one()
        p.features = {k: v for k, v in p.features.items() if k != "group_optimise"} | {"max_accounts": 7}
        s.commit()
        seed(s)
        p = s.query(Plan).filter_by(key="extreme").one()
        assert p.features["group_optimise"] is True and p.features["max_accounts"] == 7

"""1.3.14: per-group settings, breakeven clean-up, edited updates, the new overview, and a quiet public face."""
import re

from server import services, telegram_service as ts
from server.db import Account, SessionLocal, Signal, User, main_source
from tests.test_worker import SIG, move, rig  # noqa: F401


def test_group_settings_override_and_pause(rig):
    mt5, api, w, clock = rig
    api.settings.update(layers=1, fixed_lot=0.01, groups={
        "-100": {"own": True, "lot_mode": "fixed", "fixed_lot": 0.05, "layers": 2, "tp_target": "spread"},
        "-200": {"paused": True}})
    api.push("signal", signal={**SIG, "id": 11, "group": "-100", "source": "Fx by KD"})
    w.step()
    lots = sorted(o.volume_current for o in mt5.orders.values()) + sorted(p.volume for p in mt5.positions.values())
    assert lots == [0.05, 0.05], lots
    assert w.settings["layers"] == 1                         # the account settings are untouched afterwards
    api.push("signal", signal={**SIG, "id": 12, "group": "-200", "source": "TECH"})
    w.step()
    assert "12" not in w.state["signals"] and any("you paused TECH" in n for n in w.notes)


def test_breakeven_call_cancels_unfilled_entries(rig):
    mt5, api, w, clock = rig
    api.settings.update(layers=3, max_lot=1.0)
    move(rig, 4045)
    api.push("signal", signal={**SIG, "id": 21})
    w.step()
    assert len(mt5.orders) == 3
    move(rig, 4036.5)                                         # top layer fills
    assert len(mt5.positions) == 1 and len(mt5.orders) == 2
    move(rig, 4040)
    api.push("update", update={"signal_id": 21, "kind": "breakeven", "value": None})
    w.step()
    assert not mt5.orders, "fresh entries at the old stop after the group went risk-free"
    assert any("moved the trade to breakeven" in n for n in w.notes)


def test_entries_cancelled_when_first_entry_closes_at_breakeven(rig):
    mt5, api, w, clock = rig
    api.settings.update(layers=3, max_lot=1.0, cancel_pending_on_be=False)
    move(rig, 4045)
    api.push("signal", signal={**SIG, "id": 31})
    w.step()
    move(rig, 4036.5)
    move(rig, 4040)
    api.push("update", update={"signal_id": 31, "kind": "breakeven", "value": None})
    w.step()
    assert len(mt5.orders) == 2                               # setting off: they stay
    api.settings["cancel_pending_on_be"] = True
    move(rig, 4036.0)                                         # back to entry: breakeven stop closes it
    move(rig, 4036.0)
    assert not mt5.positions and not mt5.orders


def _src():
    with SessionLocal() as s:
        return main_source(s).id


def test_edited_update_message_fires_once(browser):
    chat = -1009990001
    assert ts.handle_message(_src(), 99001, "XAUUSD BUY 4030-4037\nSL 4027\nTP 4047", None,
                             chat_id=chat, chat_title="G").startswith("signal:")
    assert ts.handle_message(_src(), 99002, "Set breakeven", None, chat_id=chat) == "update:breakeven"
    assert ts.handle_message(_src(), 99002, "Set breakeven ✅", None, edited=True, chat_id=chat) == "edit_same_update"


def _customer(browser, name="groupie"):
    browser.get("/signup")
    browser.post("/signup", {"username": name, "password": "password123", "email": f"{name}@example.com"})
    with SessionLocal() as s:
        u = s.query(User).filter_by(username=name).one()
        acc = Account(user_id=u.id, login="5551", server="Demo", password_enc="x", settings={},
                      conn_status="online", copy_enabled=True, currency="USD",
                      live_state={"positions": [], "pendings": [], "waiting": []})
        s.add(acc)
        s.flush()
        services.ensure_default_sources(s, acc)
        src = main_source(s)
        s.add(Signal(source_id=src.id, side="buy", entry_low=4030, entry_high=4037, sl=4027, tps=[4047],
                     tg_chat_id=-1004242, tg_msg_id=1, channel="Gold Group"))
        s.commit()
        return acc.id


def test_overview_and_group_cards(browser):
    aid = _customer(browser)
    d = browser.get("/dashboard").text
    assert "Good " in d and ("Your catch-up" in d or "Your day so far" in d) and "Market hours" in d
    page = browser.get(f"/accounts/{aid}").text
    assert "Signal groups" in page and "Gold Group" in page and "Armed" in page and "data-deck" in page
    r = browser.post(f"/accounts/{aid}/groups/-1004242", {"trading": "on", "own": "on", "lot_mode": "fixed", "fixed_lot": "0.07",
                                                         "layers": "1", "tp_target": "tp1"})
    assert r.status_code == 200
    with SessionLocal() as s:
        g = s.get(Account, aid).settings["groups"]["-1004242"]
        assert g["own"] and g["fixed_lot"] == 0.07 and not g.get("paused")
        assert services.worker_settings(s.get(Account, aid))["groups"]["-1004242"]["layers"] == 1
    browser.post(f"/accounts/{aid}/groups/-1004242", {"action": "reset"})
    with SessionLocal() as s:
        assert "-1004242" not in s.get(Account, aid).settings["groups"]


def test_public_face_gives_nothing_away(browser):
    c = browser.c
    r = c.get("/no-such-page")
    assert r.status_code == 404 and "Page not found" in r.text and "detail" not in r.text
    j = c.get("/api/no-such-thing")
    assert j.status_code == 404 and "detail" not in j.json()
    assert c.get("/docs").status_code == 404 and c.get("/openapi.json").status_code == 404
    assert "Disallow: /admin" in c.get("/robots.txt").text
    t = c.get("/terms")
    assert t.status_code == 200 and "reverse engineer" in t.text
    css, js = c.get("/static/style.css").text, c.get("/static/app.js").text
    assert "/*" not in css[:2000] and "\n" not in js.strip()           # served minified, no comments
    assert c.get("/static/../assets_src/app.js").status_code == 404
    sw = c.get("/sw.js").text
    assert "//" not in sw.replace("://", "") and "1.3" not in sw
    head = c.get("/")
    assert "x-powered-by" not in head.headers and "permissions-policy" in head.headers


def test_swipe_decks_and_signals_feed(browser):
    aid = _customer(browser, "swiper")
    d = browser.get("/dashboard").text
    assert "data-deck" in d and "Gold Group" in d and "Finish setting up" not in d or "data-deck" in d
    sig = browser.get("/signals").text
    assert "data-deck" in sig and 'class="chip' in sig and "Gold Group" in sig and "Today" in sig
    one = browser.get("/signals?g=-1004242").text
    assert "Gold Group" in one and "Show all" in one
    acc = browser.get(f"/accounts/{aid}").text
    assert acc.count('class="sec"') >= 7 and "savebar" in acc
    assert "data-deck" in browser.get("/accounts/%d" % aid).text


def test_safety_filters(rig):
    mt5, api, w, clock = rig
    move(rig, 4045)
    api.push("signal", signal={**SIG, "id": 81})
    api.push("signal", signal={**SIG, "id": 82, "entry_low": 4031, "entry_high": 4036, "sl": 4025, "group": "-9"})
    w.step()
    assert "81" in w.state["signals"] and "82" not in w.state["signals"]
    assert any("same idea as signal #81" in n for n in w.notes)
    api.push("signal", signal={**SIG, "id": 83, "side": "sell", "entry_low": 4050, "entry_high": 4050, "sl": 4051.5, "tps": [4040]})
    api.push("signal", signal={**SIG, "id": 84, "entry_low": 3930, "entry_high": 3935, "sl": 3925, "tps": [4040]})
    w.step()
    assert "83" not in w.state["signals"] and any("only 15 pips" in n for n in w.notes)
    assert "84" not in w.state["signals"] and any("a typo in the call" in n for n in w.notes)


def test_parser_go_be_and_highlights():
    from server.parser import parse_signal, parse_update
    assert parse_update("go BE for TP or BOOK") == [("breakeven", None)]
    assert parse_update("BE HIT HERE") == []
    assert parse_signal("🏆 HIGHLIGHT\nBuy GOLD @ 3729-3733\nTP 3738 ✅\nTP 3743 ✅\nSL 3723") is None
    p = parse_signal("XAUUSD SELL ZONE: 3736-42\nSL: 4047\nTP1: 3726\nTP2: 37216")
    assert p and p.sl is None and p.tps == [3726.0]


def test_risk_free_locks_pips_past_each_entry(rig):
    mt5, api, w, clock = rig
    api.settings.update(layers=1, instant_entry=True)
    move(rig, 4040)
    api.push("signal", signal={**SIG, "id": 91, "entry_low": 0, "entry_high": 0, "sl": 4030, "tps": [4060], "instant": True})
    w.step()
    (p,) = mt5.positions.values()
    move(rig, 4046)
    api.push("update", update={"signal_id": 91, "kind": "breakeven", "value": None, "be_lock_pips": 15})
    w.step()
    assert abs(p.sl - (p.price_open + 1.5)) < 0.011, p.sl
    # "move SL to 4041" from a group whose rule says that means risk free: a later entry keeps entry + 15
    api.push("signal", signal={**SIG, "id": 92, "side": "sell", "entry_low": 0, "entry_high": 0, "sl": 4056, "tps": [4020], "instant": True})
    w.step()
    q = [x for x in mt5.positions.values() if x is not p][0]
    move(rig, 4040)
    api.push("update", update={"signal_id": 92, "kind": "sl_move", "value": 4049, "be_lock_pips": 15, "sl_move_as_be": True})
    w.step()
    assert abs(q.sl - (q.price_open - 1.5)) < 0.011, q.sl


def test_group_rule_rides_on_updates(browser):
    from server.db import Event
    chat = -1004498117492
    assert ts.handle_message(_src(), 77001, "XAUUSD BUY 4030-4037\nSL 4027\nTP 4047", None, chat_id=chat).startswith("signal:")
    assert ts.handle_message(_src(), 77002, "Running 40 pips, go risk free", None, chat_id=chat) == "update:breakeven"
    with SessionLocal() as s:
        ev = s.query(Event).filter(Event.kind == "update").order_by(Event.id.desc()).first()
        assert ev.payload["update"]["be_lock_pips"] == 15 and ev.payload["update"]["sl_move_as_be"] is True


def test_icons_follow_the_theme():
    """Changing the theme colours must change the favicon/app icons too (tools/build_assets.py does both)."""
    from pathlib import Path
    from server.brand import THEME
    root = Path(__file__).resolve().parent.parent / "server"
    svg = (root / "static/icons/icon.svg").read_text()
    css = (root / "assets_src/style.css").read_text()
    for colour in (*THEME["grad"], THEME["slash"], THEME["tile"]):
        assert colour in svg, colour
    assert f"--c1: {THEME['grad'][0]}" in css and f"--pine: {THEME['bg']}" in css


def test_ignored_update_leaves_a_note(rig):
    mt5, api, w, clock = rig
    api.settings.update(follow_updates=False)
    move(rig, 4045)
    api.push("signal", signal={**SIG, "id": 95})
    w.step()
    api.push("update", update={"signal_id": 95, "kind": "breakeven", "value": None})
    w.step()
    assert any("not applied because" in n and "Follow the group's updates" in n for n in w.notes)

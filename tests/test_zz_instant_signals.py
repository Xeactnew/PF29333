"""'BUY NOW' calls: enter straight away, then take the SL/TPs the group edits in or posts after."""
from server import telegram_service as ts
from server.db import Signal, SignalUpdate, Event, SessionLocal, main_source
from server.parser import parse_levels, parse_signal
from tests.test_worker import rig, move  # noqa: F401


def test_parser_calls_and_chat():
    for text in ("XAUUSD BUY NOW", "GOLD SELL NOW 🔥", "🚀 Gold: buy now", "Buy gold now", "SELL NOW"):
        p = parse_signal(text)
        assert p and p.instant and p.entry_low == 0 and p.sl is None, text
    for text in ("Should I buy now?", "dont buy now guys", "I will buy now if it breaks 3760",
                 "buy now if it breaks 3760", "BUY NOW?"):
        assert parse_signal(text) is None, text
    p = parse_signal("XAUUSD SELL NOW\nSL 3765\nTP1 3745\nTP2 3735")
    assert p.side == "sell" and p.entry_low == 0 and p.sl == 3765 and p.tps == [3745, 3735]
    full = parse_signal("XAUUSD BUY 3750-3746\nSL 3740\nTP1 3760\nTP2 3770")
    assert not full.instant and full.sl == 3740
    assert parse_levels("SL: 3740\nTP1 3760\nTP2 3770") == (3740, [3760, 3770])


def _main_id():
    with SessionLocal() as s:
        return main_source(s).id


def _updates(sig_id):
    with SessionLocal() as s:
        return [(u.kind, u.value) for u in s.query(SignalUpdate).filter_by(signal_id=sig_id)], \
            [e.payload for e in s.query(Event).filter_by(signal_id=sig_id, kind="update")]


def test_edit_adds_levels(browser):
    src = _main_id()
    res = ts.handle_message(src, 77001, "XAUUSD BUY NOW", None)
    assert res.startswith("signal:")
    sid = int(res.split(":")[1])
    res = ts.handle_message(src, 77001, "XAUUSD BUY NOW\nSL 3740\nTP1 3760\nTP2 3770", None, edited=True)
    assert res == "levels_edit"
    ups, payloads = _updates(sid)
    assert ups == [("levels", 3740)] and payloads[-1]["update"]["tps"] == [3760, 3770]
    with SessionLocal() as s:
        sig = s.get(Signal, sid)
        assert sig.sl == 3740 and sig.tps == [3760, 3770]


def test_follow_up_message_fills_the_call_instead_of_a_second_trade(browser):
    src = _main_id()
    sid = int(ts.handle_message(src, 77101, "GOLD SELL NOW", None).split(":")[1])
    assert ts.handle_message(src, 77102, "SL 3765\nTP1 3745\nTP2 3735", None) == f"levels:{sid}"
    sid2 = int(ts.handle_message(src, 77103, "BUY NOW", None).split(":")[1])
    # the full signal posted right after the call fills it in
    assert ts.handle_message(src, 77104, "XAUUSD BUY 3750-3746\nSL 3740\nTP1 3760", None) == f"levels:{sid2}"
    with SessionLocal() as s:
        assert s.query(Signal).filter(Signal.tg_msg_id == 77104).count() == 0
    # an ignored chat message later edited into a signal is picked up
    assert ts.handle_message(src, 77200, "get ready guys", None) == "ignored"
    assert ts.handle_message(src, 77200, "XAUUSD SELL 3770-3774\nSL 3780\nTP 3760", None, edited=True).startswith("signal:")


def test_worker_enters_now_with_temp_stop_then_takes_group_levels(rig):  # noqa: F811
    mt5, api, w, clock = rig
    api.settings.update(layers=3, fixed_lot=0.01, default_sl_pips=50)
    mt5.move(4040)
    api.push("signal", signal={"id": 7, "side": "buy", "entry_low": 0, "entry_high": 0, "sl": None, "tps": [],
                               "order_type": "market", "instant": True})
    w.step()
    pos = list(mt5.positions.values())
    assert len(pos) == 3 and all(abs(p.sl - (mt5.ask - 5.0)) < 1e-6 for p in pos) and all(p.tp == 0 for p in pos)
    assert w.state["signals"]["7"]["temp_sl"]
    api.push("update", update={"signal_id": 7, "kind": "levels", "value": 4030, "tps": [4050, 4060, 4070]})
    w.step()
    pos = sorted(mt5.positions.values(), key=lambda p: p.comment)
    assert all(p.sl == 4030 for p in pos) and [p.tp for p in pos] == [4050, 4060, 4070]
    assert not w.state["signals"]["7"]["temp_sl"]


def test_worker_waits_for_stop_when_instant_entry_off(rig):  # noqa: F811
    mt5, api, w, clock = rig
    api.settings.update(instant_entry=False, layers=1)
    mt5.move(4040)
    api.push("signal", signal={"id": 8, "side": "sell", "entry_low": 0, "entry_high": 0, "sl": None, "tps": [],
                               "order_type": "market", "instant": True})
    w.step()
    assert not mt5.positions and "8" in w.state["await_sl"]
    api.push("update", update={"signal_id": 8, "kind": "levels", "value": 4050, "tps": [4030]})
    w.step()
    p = list(mt5.positions.values())
    assert len(p) == 1 and p[0].sl == 4050 and p[0].tp == 4030 and not w.state["await_sl"]
    # no stop within 30 min -> skipped
    api.push("signal", signal={"id": 9, "side": "sell", "entry_low": 4041, "entry_high": 4043, "sl": None,
                               "tps": [], "order_type": "market"})
    w.step()
    assert "9" in w.state["await_sl"]
    move(rig, 4040, secs=31 * 60)
    assert "9" not in w.state["await_sl"] and any("didn't post a stop loss" in n for n in w.notes)


def test_group_stop_already_passed_closes(rig):  # noqa: F811
    mt5, api, w, clock = rig
    mt5.move(4040)
    api.push("signal", signal={"id": 10, "side": "buy", "entry_low": 0, "entry_high": 0, "sl": None, "tps": [],
                               "order_type": "market", "instant": True})
    w.step()
    assert mt5.positions
    api.push("update", update={"signal_id": 10, "kind": "levels", "value": 4041, "tps": [4060]})
    w.step()
    assert not mt5.positions


def test_same_signal_from_two_groups_trades_once(rig):  # noqa: F811
    mt5, api, w, clock = rig
    api.settings.update(layers=1)
    mt5.move(4036)
    base = {"side": "buy", "entry_low": 4030, "entry_high": 4037, "sl": 4027, "tps": [4047], "order_type": "market"}
    api.push("signal", signal={**base, "id": 21})
    api.push("signal", signal={**base, "id": 22})
    w.step()
    assert len(mt5.positions) == 1 and "22" not in w.state["signals"]
    assert any("same trade as signal #21" in n for n in w.notes)

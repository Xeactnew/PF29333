"""1.3.31: options taken from the FundingPips copier (each one switchable per account)."""

from server import parser, telegram_service as ts
from server.db import Event, SessionLocal, get_setting, main_source, set_setting
from server.plans import clean_settings, effective_settings, locked_keys
from tests.test_worker import SIG, move, rig  # noqa: F401
import trade_logic as tl

BUY3 = {**SIG, "order_type": "limit"}


def _ours(mt5):
    return [p for p in mt5.positions.values() if p.magic != 0]


def test_parser_reads_close_worst_entry():
    kinds = [k for k, _ in parser.parse_update("CLOSE WORST ENTRIES BE / SL TO BE")]
    assert "close_worst" in kinds and "breakeven" in kinds
    assert [k for k, _ in parser.parse_update("close the first entry")] == ["close_worst"]
    assert [k for k, _ in parser.parse_update("close all now")] == ["close"]


def test_close_worst_entry(rig):
    mt5, api, w, clock = rig
    api.settings.update(layers=3, fixed_lot=0.05)
    api.push("signal", signal={**BUY3, "id": 401})
    w.step()
    move(rig, 4033.2)                                     # two of three entries fill (4037, 4033.5)
    assert len(_ours(mt5)) == 2
    api.push("update", update={"signal_id": 401, "kind": "close_worst"})
    w.step()
    left = _ours(mt5)
    assert len(left) == 1 and left[0].price_open == 4033.5           # the 4037 entry was closed
    assert not mt5.orders                                                   # the unfilled entry is cancelled
    assert any("closed the worst entry (4037)" in n for n in w.notes), w.notes
    # switched off: only a note
    api.settings.update(close_worst_entry=False)
    api.push("signal", signal={**BUY3, "id": 402, "entry_low": 4000, "entry_high": 4005, "sl": 3990})
    w.step()
    api.push("update", update={"signal_id": 402, "kind": "close_worst"})
    w.step()
    assert any("\"Close the worst entry\" is off" in n for n in w.notes)


def test_close_worst_in_profit_moves_its_stop_to_entry(rig):
    mt5, api, w, clock = rig
    api.settings.update(layers=2, fixed_lot=0.05, one_trade_per_idea=False)
    api.push("signal", signal={**BUY3, "id": 403})
    w.step()
    move(rig, 4029.7)
    move(rig, 4040)                                       # both filled, price back above both entries
    api.push("update", update={"signal_id": 403, "kind": "close_worst"})
    w.step()
    worst = max(_ours(mt5), key=lambda p: p.price_open)
    assert len(_ours(mt5)) == 2 and worst.sl >= worst.price_open


def test_edited_zone_moves_unfilled_entries(rig):
    mt5, api, w, clock = rig
    api.settings.update(layers=2, fixed_lot=0.02)
    api.push("signal", signal={**BUY3, "id": 404})
    w.step()
    assert sorted(o.price_open for o in mt5.orders.values()) == [4030, 4037]
    api.push("update", update={"signal_id": 404, "kind": "levels", "value": None, "zone": [4020, 4026],
                               "zone_moved": True})
    w.step()
    assert sorted(o.price_open for o in mt5.orders.values()) == [4020, 4026]
    api.settings.update(move_entries_on_edit=False)
    api.push("update", update={"signal_id": 404, "kind": "levels", "zone": [4010, 4016], "zone_moved": True})
    w.step()
    assert sorted(o.price_open for o in mt5.orders.values()) == [4020, 4026]


def test_entry_lifetime_per_group_and_house_rule(rig):
    mt5, api, w, clock = rig
    api.settings.update(entry_expiry_min=120, groups={"-77": {"entry_expiry_min": 2880}})
    api.push("signal", signal={**BUY3, "id": 405, "group": "-77"})
    api.push("signal", signal={**BUY3, "id": 406, "entry_low": 4010, "entry_high": 4015, "sl": 4000,
                               "group": "-78", "entry_expiry_min": 1440})
    api.settings.update(one_trade_per_idea=False)
    w.step()
    s = w.state["signals"]
    assert s["405"]["expires"] - s["405"]["created"] == 2880 * 60
    assert s["406"]["expires"] - s["406"]["created"] == 1440 * 60


def test_overall_loss_limit_and_buffer(rig):
    mt5, api, w, clock = rig
    api.settings.update(fixed_lot=0.1, max_total_loss=100, start_balance=10000, close_on_limit=True)
    api.push("signal", signal={**SIG, "id": 407, "entry_low": 4038, "entry_high": 4041, "sl": 4020})
    w.step()
    assert _ours(mt5)
    move(rig, 4030)                                       # about -100 on 0.1 lots
    move(rig, 4030)
    assert w.blocked and "overall loss limit" in w.blocked and not _ours(mt5)
    # the buffer: a daily limit of 100 acts at 90
    assert tl.daily_block({"daily_loss_limit": 100, "limit_buffer_pct": 90}, 10000, -91)
    assert not tl.daily_block({"daily_loss_limit": 100, "limit_buffer_pct": 100}, 10000, -91)


def test_worst_case_check_skips_a_trade_that_could_break_the_limit(rig):
    mt5, api, w, clock = rig
    api.settings.update(fixed_lot=0.1, daily_loss_limit=50, check_worst_case=True)
    api.push("signal", signal={**SIG, "id": 408})          # 0.1 lot, ~13 dollars to SL x 100 = 130 at risk
    w.step()
    assert not _ours(mt5) and any("if every open trade and this one hit their stops" in n for n in w.notes)


def test_tp1_from_one_minute_candles(rig):
    mt5, api, w, clock = rig
    api.settings.update(fixed_lot=0.02, layers=2, tp1_from_bars=True)
    api.push("signal", signal={**BUY3, "id": 409})
    w.step()
    created = w.state["signals"]["409"]["created_srv"]
    mt5.bars = [{"time": created // 60 * 60 + 60, "high": 4047.5, "low": 4039.0}]
    move(rig, 4040, 30)
    move(rig, 4040, 30)
    assert w.state["signals"]["409"]["tp1_done"]


def test_no_hedging(rig):
    mt5, api, w, clock = rig
    api.settings.update(fixed_lot=0.02, no_hedging="close_old")
    api.push("signal", signal={**SIG, "id": 410, "entry_low": 4038, "entry_high": 4041})
    w.step()
    assert len(_ours(mt5)) == 1
    api.push("signal", signal={"id": 411, "side": "sell", "entry_low": 4039, "entry_high": 4042, "sl": 4055,
                               "tps": [4030], "order_type": "market"})
    w.step()
    sides = {p.type for p in _ours(mt5)}
    assert sides == {1}                                   # the buy was closed, the sell opened
    api.settings.update(no_hedging="skip_new")
    api.push("signal", signal={**SIG, "id": 412, "entry_low": 4038, "entry_high": 4041})
    w.step()
    assert {p.type for p in _ours(mt5)} == {1} and any("hedging is off" in n for n in w.notes)


def test_partials_and_runner(rig):
    mt5, api, w, clock = rig
    api.settings.update(fixed_lot=0.1, tp_target="partials", tp_partials="40,30,30")
    api.push("signal", signal={**SIG, "id": 413, "entry_low": 4038, "entry_high": 4041})
    w.step()
    p = _ours(mt5)[0]
    assert p.tp == 4067 and p.volume == 0.1
    move(rig, 4047.5)
    assert _ours(mt5)[0].volume == 0.06
    move(rig, 4057.5)
    assert _ours(mt5)[0].volume == 0.03
    move(rig, 4068)
    assert not _ours(mt5)                                  # the last share closed at TP3 in MT5

    api.settings.update(runner_pct=20, runner_trail_pips=50)
    api.push("signal", signal={**SIG, "id": 414, "entry_low": 4066, "entry_high": 4069, "sl": 4050,
                               "tps": [4075, 4085, 4095]})
    w.step()
    p = _ours(mt5)[0]
    assert p.tp == 0
    for px in (4075.5, 4085.5, 4095.5):
        move(rig, px)
    p = _ours(mt5)[0]
    assert p.volume == 0.02 and p.sl == 4090.5             # runner: stop at TP2, then trails 50 pips
    move(rig, 4110)
    assert _ours(mt5)[0].sl == 4105.0
    assert tl.partial_plan("40,30,30", 2, 0) == [0.4, 0.6]


def test_hand_trades_get_protected(rig):
    mt5, api, w, clock = rig
    api.settings.update(hand_sl_pips=100, hand_be_pips=50, be_offset_pips=1)
    t = mt5._open(0, 0.1, 4040.2, 0, 0, 0, "")             # opened by hand (magic 0)
    move(rig, 4040)
    assert mt5.positions[t].sl == round(4040.2 - 10, 2)
    move(rig, 4046)
    assert mt5.positions[t].sl == round(4040.2 + 0.1, 2)


def test_settings_and_plans():
    c = clean_settings({"tp_partials": "50, 60", "no_hedging": "close_old", "limit_buffer_pct": 20,
                        "groups": {"-5": {"entry_expiry_min": "2880"}}})
    assert c["tp_partials"] == "50,50" and c["no_hedging"] == "close_old" and c["limit_buffer_pct"] == 50
    assert c["groups"]["-5"]["entry_expiry_min"] == 2880
    std = {"daily_limits": False, "signal_filters": False, "tp_control": False}
    e = effective_settings({"max_total_loss": 100, "no_hedging": "skip_new", "runner_pct": 20,
                            "tp_target": "partials"}, std)
    assert e["max_total_loss"] == 0 and e["no_hedging"] == "off" and e["runner_pct"] == 0
    assert {"max_total_loss", "no_hedging", "tp_partials"} <= locked_keys(std)


def test_server_sends_edited_zone_and_entry_hours(browser):
    chat = -1006660001
    with SessionLocal() as s:
        before = dict(get_setting(s, "group_rules") or {})
        set_setting(s, "group_rules", {**before, str(chat): {"entry_hours": 48}})
        s.commit()
        src = main_source(s).id
    assert ts.handle_message(src, 66001, "XAUUSD BUY 4030-4037\nSL 4020\nTP 4047", None,
                             chat_id=chat, chat_title="H").startswith("signal:")
    r = ts.handle_message(src, 66001, "XAUUSD BUY 4024-4030\nSL 4020\nTP 4047", None, chat_id=chat,
                          chat_title="H", edited=True)
    assert r == "levels_edit"
    with SessionLocal() as s:
        sig_ev = s.query(Event).filter(Event.kind == "signal").order_by(Event.id.desc()).first()
        assert sig_ev.payload["signal"]["entry_expiry_min"] == 2880
        up = s.query(Event).filter(Event.kind == "update").order_by(Event.id.desc()).first()
        assert up.payload["update"]["zone"] == [4024.0, 4030.0] and up.payload["update"]["zone_moved"]
        set_setting(s, "group_rules", before)
        s.commit()

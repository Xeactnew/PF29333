"""Maximum lots open per account: positions + pending orders + waiting layers never go over the cap."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "node"))

import trade_logic as tl  # noqa: E402
from server.plans import clean_settings  # noqa: E402
from tests.test_worker import SIG, move, rig  # noqa: E402,F401  (rig is a fixture)


def _set(api, w, **kw):
    api.settings.update(kw)
    w.settings = api.settings


def _send(rig, sig):
    mt5, api, w, clock = rig
    api.push("signal", signal=sig)
    w.step()


def _lots(mt5):
    return round(sum(p.volume for p in mt5.positions.values()) + sum(o.volume_current for o in mt5.orders.values()), 2)


def test_setting_is_validated():
    assert clean_settings({})["max_open_lots"] == 0
    assert clean_settings({"max_open_lots": "0.257"})["max_open_lots"] == 0.26
    assert clean_settings({"max_open_lots": "-3"})["max_open_lots"] == 0
    assert clean_settings({"max_open_lots": "nonsense"})["max_open_lots"] == 0
    assert clean_settings({"max_open_lots": "5000"})["max_open_lots"] == 1000


def test_fit_helper():
    assert tl.fit_under_cap(0.05, 0.0, 0, 0.01, 0.01) == 0.05          # cap off
    assert tl.fit_under_cap(0.05, 0.05, 0.2, 0.01, 0.01) == 0.05       # room
    assert tl.fit_under_cap(0.05, 0.17, 0.2, 0.01, 0.01) == 0.03       # trimmed
    assert tl.fit_under_cap(0.05, 0.195, 0.2, 0.01, 0.01) == 0.0       # below the broker minimum: skip
    assert tl.fit_under_cap(0.05, 0.3, 0.2, 0.01, 0.01) == 0.0


def test_cap_off_places_everything(rig):
    mt5, api, w, clock = rig
    mt5.move(4036.0)
    _set(api, w, layers=3, fixed_lot=0.05, max_lot=5, max_open_lots=0)
    _send(rig, SIG)
    assert _lots(mt5) == 0.15 and len(mt5.positions) == 1 and len(mt5.orders) == 2


def test_cap_reached_skips_the_signal(rig):
    mt5, api, w, clock = rig
    mt5.move(4036.0)
    _set(api, w, layers=2, fixed_lot=0.05, max_lot=5, max_open_lots=0.1, one_trade_per_idea=False)
    _send(rig, SIG)
    assert _lots(mt5) == 0.1                                               # first signal uses the whole cap
    _send(rig, {**SIG, "id": 2, "entry_low": 4033, "entry_high": 4038})
    assert _lots(mt5) == 0.1                                               # nothing more placed
    assert any("signal #2 skipped: your maximum of 0.1 lots open is reached" in n for n in w.notes)
    assert all(L["state"] == "skipped" for L in w.state["signals"]["2"]["layers"])
    move(rig, mt5.bid)
    assert w.state["signals"]["2"].get("finished")


def test_partial_fit_trims_and_skips(rig):
    mt5, api, w, clock = rig
    mt5.move(4036.0)
    _set(api, w, layers=3, fixed_lot=0.05, max_lot=5, max_open_lots=0.12)
    _send(rig, SIG)
    layers = w.state["signals"]["1"]["layers"]
    assert [L["state"] for L in layers] == ["open", "pending", "pending"]
    assert [L["lot"] for L in layers] == [0.05, 0.05, 0.02] and _lots(mt5) == 0.12   # last layer trimmed to fit
    assert any("maximum 0.12 lots open: layer 3 trimmed from 0.05 to 0.02 lots" in n for n in w.notes)

    _set(api, w, max_open_lots=0.125, one_trade_per_idea=False)           # 0.005 of room: below the broker minimum
    _send(rig, {**SIG, "id": 3, "entry_low": 4033, "entry_high": 4038})
    assert _lots(mt5) == 0.12
    assert any("signal #3 skipped: your maximum of 0.125 lots open is reached" in n for n in w.notes)


def test_waiting_layers_count(rig):
    mt5, api, w, clock = rig
    mt5.move(4036.0)
    _set(api, w, layers=1, fixed_lot=0.1, max_lot=5, max_open_lots=0.15, smart_entry=True,
         smart_distance_pips=5, one_trade_per_idea=False)
    _send(rig, {**SIG, "entry_low": 4020, "entry_high": 4020, "sl": 4005, "tps": [4060]})   # far away: waits
    assert "1" in w.state["signals"], w.notes
    assert w.state["signals"]["1"]["layers"][0]["state"] == "wait", w.notes
    assert w.lots_in_use() == 0.1 and _lots(mt5) == 0
    _send(rig, {**SIG, "id": 2})                                           # 0.1 more would make 0.2
    assert _lots(mt5) == 0.05 and w.lots_in_use() == 0.15
    assert any("layer 1 trimmed from 0.1 to 0.05 lots" in n for n in w.notes)

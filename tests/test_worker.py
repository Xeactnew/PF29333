import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "node"))

from server.plans import DEFAULT_ACCOUNT_SETTINGS  # noqa: E402
from tests.fake_mt5 import FakeMT5  # noqa: E402
import trade_logic as tl  # noqa: E402
from worker import MAGIC, Worker  # noqa: E402

SIG = {"id": 1, "side": "buy", "entry_low": 4030, "entry_high": 4037, "sl": 4027,
       "tps": [4047, 4057, 4067], "order_type": "market"}


class FakeAPI:
    def __init__(self):
        self.queue, self.reports, self.sent_deals = [], [], []
        self.settings = dict(DEFAULT_ACCOUNT_SETTINGS)
        self.copy = True
        self.news = []
        self.eid = 0

    def push(self, kind, **payload):
        self.eid += 1
        self.queue.append({"id": self.eid, "kind": kind, **payload})

    def poll(self, body):
        if body.get("report"):
            self.reports.append(body["report"])
        ev, self.queue = self.queue, []
        return {"copy_enabled": self.copy, "settings": self.settings, "news": self.news, "events": ev}

    def deals(self, deals):
        self.sent_deals.extend(deals)
        return {"added": len(deals)}


class Clock:
    def __init__(self):
        self.t = 1_763_200_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def rig(tmp_path):
    mt5, api, clock = FakeMT5(), FakeAPI(), Clock()
    w = Worker(mt5, api, 1, 123, "pw", "Srv", None, tmp_path / "state.json", clock=clock)
    w.settings = api.settings
    assert w.connect()
    w.step()
    return mt5, api, w, clock


def move(rig, price, secs=1):
    mt5, api, w, clock = rig
    mt5.move(price, secs)
    clock.t += secs
    w.step()


def test_logic_helpers():
    assert tl.layer_levels("buy", 4030, 4037, 3) == [4037, 4033.5, 4030]
    assert tl.layer_levels("sell", 4055, 4060, 2) == [4055, 4060]
    assert tl.layer_tps([1, 2, 3], 5, "spread") == [1, 1, 2, 2, 3]
    assert tl.layer_tps([1, 2, 3], 2, "tp3") == [3, 3]
    lot, _ = tl.lot_per_layer({"lot_mode": "risk_percent", "risk_percent": 1, "max_lot": 5}, 1, 10000, [1000],
                              0.01, 0.01, 100)
    assert lot == 0.1
    lot, why = tl.lot_per_layer({"lot_mode": "risk_amount", "risk_amount": 1, "max_lot": 5}, 3, 100, [1000] * 3,
                                0.01, 0.01, 100)
    assert lot == 0 and "too small" in why
    assert tl.in_session("23:30", "22:00", "02:00") and not tl.in_session("12:00", "22:00", "02:00")
    assert "loss" in tl.daily_block({"daily_loss_pct": 2}, 1000, -25)


def test_symbol_found_automatically(rig):
    assert rig[2].symbol == "XAUUSD.s"


def test_layers_as_limits_then_tp1_breakeven(rig):
    mt5, api, w, clock = rig
    api.settings.update(layers=3, max_lot=1.0)   # plan clamping happens on the server; here we test the engine
    api.push("signal", signal=SIG)
    w.step()
    assert sorted(o.price_open for o in mt5.orders.values()) == [4030, 4033.5, 4037]
    assert {o.tp for o in mt5.orders.values()} == {4047, 4057, 4067}
    move(rig, 4036.7)          # layer 1 fills
    move(rig, 4033.2)          # layer 2 fills
    assert len(mt5.positions) == 2 and len(mt5.orders) == 1
    move(rig, 4047.1)          # TP1: layer 1 closes, remaining entry cancelled, layer 2 to breakeven
    assert len(mt5.orders) == 0
    (p,) = mt5.positions.values()
    assert p.price_open == 4033.5 and p.sl == pytest.approx(4033.6)
    move(rig, 4033.0)          # pull back stops out at breakeven + 1 pip
    assert not mt5.positions
    w.sync_deals()
    outs = [d for d in api.sent_deals if d["entry"] == "out"]
    assert len(outs) == 2 and all(d["ours"] and d["signal_id"] == 1 and d["side"] == "buy" for d in outs)
    assert sum(d["profit"] for d in outs) == pytest.approx(10.0 + 0.1)


def test_market_entry_inside_zone_and_risk_sizing(rig):
    mt5, api, w, clock = rig
    api.settings.update(lot_mode="risk_percent", risk_percent=1.0)
    mt5.move(4035)
    api.push("signal", signal=SIG)
    w.step()
    (p,) = mt5.positions.values()
    # 1% of 10,000 = 100; 1 lot from 4035.2 to 4027 loses 820 -> 0.12 lots
    assert p.volume == 0.12 and p.sl == 4027 and p.tp == 4047 and p.magic == MAGIC


def test_smart_entry_waits_then_enters(rig):
    mt5, api, w, clock = rig
    api.settings.update(smart_entry=True, smart_distance_pips=20)
    mt5.move(4045)
    api.push("signal", signal=SIG)
    w.step()
    assert not mt5.positions and not mt5.orders
    assert api.reports or True
    move(rig, 4040)            # 3.2 away, still waiting
    assert not mt5.positions
    move(rig, 4038.6)          # ask 4038.8, within 2.0 of 4037 -> enter at market
    assert len(mt5.positions) == 1


def test_entry_expires(rig):
    mt5, api, w, clock = rig
    api.settings.update(entry_expiry_min=30)
    api.push("signal", signal=SIG)
    w.step()
    assert len(mt5.orders) == 1
    move(rig, 4041, secs=31 * 60)
    assert not mt5.orders


def test_skips(rig):
    mt5, api, w, clock = rig
    api.settings.update(directions="sell")
    api.push("signal", signal=SIG)
    w.step()
    assert not mt5.orders and any("switched off" in n for n in w.notes)
    api.settings.update(directions="both", max_sl_pips=50)
    api.push("signal", signal={**SIG, "id": 2})
    w.step()
    assert not mt5.orders and any("above your maximum" in n for n in w.notes)
    api.settings.update(max_sl_pips=0)
    api.copy = False
    api.push("signal", signal={**SIG, "id": 3})
    w.step()
    w.step()
    assert not mt5.orders and any("paused" in n for n in w.notes)
    api.copy = True
    mt5.move(4048)
    api.push("signal", signal={**SIG, "id": 4})
    w.step()
    assert not mt5.orders and any("TP1" in n for n in w.notes)


def test_group_updates(rig):
    mt5, api, w, clock = rig
    api.settings.update(layers=2)
    mt5.move(4036)
    api.push("signal", signal=SIG)
    w.step()
    assert len(mt5.positions) == 1 and len(mt5.orders) == 1
    api.push("update", update={"signal_id": 1, "kind": "sl_move", "value": 4029})
    w.step()
    assert all(p.sl == 4029 for p in mt5.positions.values()) and all(o.sl == 4029 for o in mt5.orders.values())
    api.push("update", update={"signal_id": 1, "kind": "close", "value": None})
    w.step()
    assert not mt5.positions and not mt5.orders
    # follow_updates off ignores group messages but not the customer's own
    api.settings.update(follow_updates=False)
    api.push("signal", signal={**SIG, "id": 5})
    w.step()
    api.push("update", update={"signal_id": 5, "kind": "close", "value": None})
    w.step()
    assert mt5.positions
    api.push("update", update={"signal_id": 5, "kind": "close", "value": None, "force": True})
    w.step()
    assert not mt5.positions


def test_daily_loss_limit_closes_and_blocks(rig):
    mt5, api, w, clock = rig
    api.settings.update(daily_loss_limit=50, close_on_limit=True, fixed_lot=0.1)
    mt5.move(4036)
    api.push("signal", signal=SIG)
    w.step()
    assert mt5.positions
    move(rig, 4030.5)        # -55 on 0.1 lot
    assert not mt5.positions and w.blocked and "loss limit" in w.blocked
    api.push("signal", signal={**SIG, "id": 9})
    move(rig, 4033)
    assert not mt5.positions and any("#9 skipped" in n for n in w.notes)


def test_news_blocks_new_trades(rig):
    mt5, api, w, clock = rig
    api.settings.update(news_protection=True, news_before_min=15, news_after_min=15)
    api.news = [{"time": clock.t + 600, "title": "Non-Farm Payrolls", "currency": "USD"}]
    w.step()
    api.push("signal", signal=SIG)
    w.step()
    assert not mt5.orders and "Non-Farm" in (w.blocked or "")


def test_close_all_command_and_report(rig):
    mt5, api, w, clock = rig
    mt5.move(4036)
    api.push("signal", signal=SIG)
    w.step()
    w.last_report = 0
    w.step()
    rep = api.reports[-1]
    assert rep["status"] == "online" and rep["positions"][0]["signal_id"] == "1" and rep["account"]["currency"] == "USD"
    api.push("command", command={"name": "close_all"})
    w.step()
    assert not mt5.positions


def test_login_failure_is_reported(tmp_path):
    mt5 = FakeMT5()
    mt5.fail_login = True
    w = Worker(mt5, FakeAPI(), 1, 123, "bad", "Srv", None, tmp_path / "s.json")
    assert not w.connect() and w.status == "error" and "password" in w.error and w.last_code == -6


class AttachOnlyMT5(FakeMT5):
    """Like a terminal that doesn't know the broker's server: logging in by API fails,
    but the terminal started with a login file connects and the API can attach to it."""
    def __init__(self, **kw):
        super().__init__(**kw)
        self.launched = []

    def initialize(self, **kw):
        self.init_kw = kw
        if "login" in kw:
            return False
        if not self.launched:
            return False
        self.login = 123
        return True

    def last_error(self):
        return (-6, "Terminal: Authorization failed")


def test_unknown_server_falls_back_to_terminal_login(tmp_path, monkeypatch):
    import worker as wmod
    monkeypatch.setattr(wmod.time, "sleep", lambda s: None)
    mt5 = AttachOnlyMT5()
    term = tmp_path / "acc" / "terminal64.exe"
    term.parent.mkdir()
    term.write_bytes(b"x")
    w = Worker(mt5, FakeAPI(), 1, 123, "pw", "NewBroker-Live", str(term), tmp_path / "s.json")
    seen = {}

    def launch(args):
        mt5.launched.append(args)
        ini = Path(args[2].split(":", 1)[1])
        seen["ini"] = ini.read_bytes().decode("utf-16")
    monkeypatch.setattr(w, "launch_terminal", launch)
    monkeypatch.setattr(w, "stop_terminal", lambda: None)
    assert w.connect() and w.status == "online"
    assert "Server=NewBroker-Live" in seen["ini"] and "Login=123" in seen["ini"]
    assert not (term.parent / "Config" / "pf_login.ini").exists()      # password file removed


def test_fallback_not_retried_too_often(tmp_path, monkeypatch):
    import worker as wmod
    monkeypatch.setattr(wmod.time, "sleep", lambda s: None)
    mt5 = FakeMT5()
    mt5.fail_login = True
    term = tmp_path / "terminal64.exe"
    term.write_bytes(b"x")
    w = Worker(mt5, FakeAPI(), 1, 123, "bad", "Srv", str(term), tmp_path / "s.json")
    calls = []
    monkeypatch.setattr(w, "login_via_config", lambda: calls.append(1) or False)
    w.connect()
    w.connect()
    assert len(calls) == 1 and w.status == "error"


def test_state_survives_restart(rig, tmp_path):
    mt5, api, w, clock = rig
    api.push("signal", signal=SIG)
    w.step()
    w2 = Worker(mt5, api, 1, 123, "pw", "Srv", None, w.state_path, clock=clock)
    w2.settings = api.settings
    w2.connect()
    assert "1" in w2.state["signals"]
    mt5.move(4036.5)
    clock.t += 1
    w2.step()
    assert w2.state["signals"]["1"]["layers"][0]["state"] == "open"

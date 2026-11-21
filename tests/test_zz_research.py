"""Research export: the worker only reads M1 history and posts it; the website saves it to data/research."""
from server import research
from tests.test_worker import rig  # noqa: F401


def test_worker_sends_candles_without_trading(rig):
    mt5, api, w, clock = rig
    posted = []
    api.post = lambda path, body, timeout=15: posted.append((path, body)) or {"saved": len(body["rows"])}
    before = (dict(mt5.positions), dict(mt5.orders), list(mt5.sent))
    api.push("command", command={"name": "export_prices", "days": 2})
    w.step()
    rows = sum(len(b["rows"]) for _, b in posted)
    assert 2 * 1440 - 2 <= rows <= 2 * 1440 + 2
    assert all(p.endswith("/research") for p, _ in posted) and posted[0][1]["symbol"] == mt5.sym
    assert (dict(mt5.positions), dict(mt5.orders), list(mt5.sent)) == before      # nothing traded
    assert any("one-minute candles" in n for n in w.notes)


def test_website_saves_candles(tmp_path, monkeypatch):
    monkeypatch.setattr(research, "DIR", tmp_path)
    assert research.save_prices(2, "XAUUSD", [[1763200000, 1, 2, 0.5, 1.5, 10, 20]]) == 1
    text = (tmp_path / "prices-acc2.csv").read_text()
    assert text.splitlines()[0].startswith("time,open,high,low,close") and "XAUUSD" in text

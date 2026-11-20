"""IC Markets demo findings: Algo Trading off, a mistyped broker server, short broker disconnects."""
import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "node"))
import trade_logic as tl  # noqa: E402
from worker import Worker  # noqa: E402
from tests.fake_mt5 import FakeMT5  # noqa: E402
from tests.test_worker import FakeAPI  # noqa: E402


def _ini(folder):
    return (folder / "Config" / "common.ini").read_bytes().decode("utf-16")


def test_algo_trading_ini(tmp_path):
    (tmp_path / "Config").mkdir()
    (tmp_path / "Config" / "common.ini").write_bytes(
        "[Common]\r\nLogin=1\r\n[Experts]\r\nEnabled=0\r\nAccount=1\r\n[Charts]\r\nMaxBars=1000\r\n".encode("utf-16"))
    assert tl.enable_algo_trading(tmp_path)
    ini = _ini(tmp_path)
    for want in ("Enabled=1", "AllowLiveTrading=1", "Account=0", "Profile=0", "MaxBars=1000", "Login=1"):
        assert want in ini, want
    assert ini.count("[Experts]") == 1 and not tl.enable_algo_trading(tmp_path)
    fresh = tmp_path / "fresh"
    tl.enable_algo_trading(fresh)
    assert "[Experts]" in _ini(fresh) and "Enabled=1" in _ini(fresh)


class AlgoOffMT5(FakeMT5):
    def __init__(self):
        super().__init__()
        self.allowed = False
        self.inits = 0

    def initialize(self, **kw):
        self.inits += 1
        return super().initialize(**kw)

    def terminal_info(self):
        return NS(connected=True, trade_allowed=self.allowed)

    def account_info(self):
        return NS(**{**vars(super().account_info()), "server": "ICMarketsSC-Demo"})


def test_worker_turns_algo_trading_on_and_learns_server(tmp_path, monkeypatch):
    term = tmp_path / "acc" / "terminal64.exe"
    term.parent.mkdir()
    term.write_bytes(b"x")
    mt5 = AlgoOffMT5()
    w = Worker(mt5, FakeAPI(), 2, 123, "pw", "ICMarketsEU-Demo", str(term), tmp_path / "s.json")
    stopped = []

    def stop():
        stopped.append(1)
        mt5.allowed = True               # MT5 comes back up with the fixed settings
    monkeypatch.setattr(w, "stop_terminal", stop)
    assert w.connect()
    assert stopped and mt5.inits == 2 and "Enabled=1" in _ini(term.parent)
    assert w.server == "ICMarketsSC-Demo" and w.report()["account"]["server"] == "ICMarketsSC-Demo"
    assert any("Algo Trading was off" in n for n in w.notes)
    # if it's still off, new trades are blocked with a clear reason (and no restart loop)
    mt5.allowed = False
    w.copy_enabled = True
    assert w.connect() and len(stopped) == 1
    assert w.entries_allowed() == "Algo Trading is switched off in MT5"


def test_website_saves_the_real_server(browser):
    from server.db import Account, Plan, SessionLocal, User
    from server import security
    import re
    from tests.test_web import admin_browser
    b = admin_browser(browser)
    r = b.post("/admin/nodes/create", {"name": "VPS Health", "max_slots": "5"})
    token = [t.strip() for t in re.findall(r'<div class="secret"[^>]*>([^<]+)</div>', r.text) if "powershell" not in t][0]
    with SessionLocal() as s:
        u = User(username="icm", email="i@x.com", password_hash="x", plan_id=s.query(Plan).first().id)
        s.add(u)
        s.flush()
        a = Account(user_id=u.id, login="53100001", server="ICMarketsEU-Demo", password_enc=security.encrypt("pw"))
        s.add(a)
        s.commit()
        aid = a.id
    H = {"Authorization": f"Bearer {token}"}
    rep = {"status": "online", "account": {"balance": 100, "equity": 100, "currency": "USD", "company": "Raw Trading Ltd",
                                           "server": "ICMarketsSC-Demo"}}
    with SessionLocal() as s:
        from server.db import Node
        s.get(Account, aid).node_id = s.query(Node).filter_by(name="VPS Health").one().id
        s.commit()
    res = b.c.post(f"/api/worker/{aid}/poll", json={"report": rep}, headers=H)
    assert res.status_code == 200, res.text
    with SessionLocal() as s:
        assert s.get(Account, aid).server == "ICMarketsSC-Demo"

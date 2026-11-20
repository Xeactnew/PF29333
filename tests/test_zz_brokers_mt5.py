"""1.3.2: ready-made MT5, shared broker list, MT5 journal and unknown-server detection."""
import re
import sys
from pathlib import Path
from types import SimpleNamespace as NS

from server import system
from tests.test_web import admin_browser

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "node"))
import node_agent  # noqa: E402
from worker import Worker  # noqa: E402
from tests.fake_mt5 import FakeMT5  # noqa: E402
from tests.test_node import make_node  # noqa: E402
from tests.test_worker import FakeAPI  # noqa: E402


def test_ready_made_mt5_is_built_and_used(tmp_path, monkeypatch):
    n = make_node(tmp_path)
    (n.template / "Config" / "accounts.dat").write_bytes(b"owner's login")
    monkeypatch.setattr(node_agent.time, "sleep", lambda s: None)

    def launch(args):
        folder = Path(args[0]).parent
        (folder / "MQL5" / "Experts").mkdir(parents=True)
        for i in range(3):
            (folder / "MQL5" / "Experts" / f"e{i}.ex5").write_bytes(b"compiled")
        (folder / "logs").mkdir(exist_ok=True)
        (folder / "logs" / "20261118.log").write_bytes(b"x")
        return NS(kill=lambda: None)
    monkeypatch.setattr(n, "launch_process", launch)
    assert not n.base_ready() and n.copy_source() == n.template
    assert n.build_base(wait=60, quiet=0, step=0)
    assert n.base_ready() and n.copy_source() == n.base
    assert not (n.base / "logs").exists() and not (n.base / "Config" / "accounts.dat").exists()
    exe = n.prepare_terminal(21)
    assert (exe.parent / "MQL5" / "Experts" / "e0.ex5").exists()
    assert not (exe.parent / node_agent.BASE_STAMP).exists()
    # a different MT5 installed as the template -> rebuilt
    (n.template / "terminal64.exe").write_bytes(b"newer build")
    assert not n.base_ready()


def test_broker_list_learned_once_used_everywhere(tmp_path):
    n = make_node(tmp_path)
    n.prepare_terminal(1)
    n.prepare_terminal(2)
    learned = b"servers" + b"+PU Prime+Vantage+IC Markets" * 50
    node_agent.servers_file(n.account_folder(1)).write_bytes(learned)
    assert n.harvest_servers()
    assert n.library.read_bytes() == learned
    assert not n.harvest_servers()                       # nothing new
    n.prepare_terminal(2)                                # restarts pick it up
    assert node_agent.servers_file(n.account_folder(2)).read_bytes() == learned
    exe = n.prepare_terminal(3)                          # new accounts too
    assert node_agent.servers_file(exe.parent).read_bytes() == learned


def test_website_keeps_the_biggest_broker_list(browser):
    b = admin_browser(browser)
    r = b.post("/admin/nodes/create", {"name": "VPS Brokers", "max_slots": "5"})
    token = [t.strip() for t in re.findall(r'<div class="secret"[^>]*>([^<]+)</div>', r.text) if "powershell" not in t][0]
    H = {"Authorization": f"Bearer {token}"}
    assert b.c.post("/api/node/heartbeat", json={}, headers=H).json()["servers"]["size"] == 0
    assert b.c.post("/api/node/servers", content=b"big list" * 10, headers=H).json()["kept"] == "yours"
    assert b.c.post("/api/node/servers", content=b"small", headers=H).json()["kept"] == "ours"
    assert b.c.get("/api/node/servers", headers=H).content == b"big list" * 10
    assert b.c.post("/api/node/heartbeat", json={"base_ready": True, "servers_size": 80}, headers=H).json()["servers"]["size"] == 80
    assert b.c.get("/api/node/servers").status_code == 401
    page = b.get("/admin/nodes").text
    assert "Ready-made MT5" in page and "add a broker" in page


def _journal(folder: Path, lines: list[str]):
    (folder / "logs").mkdir(parents=True, exist_ok=True)
    (folder / "logs" / "20261118.log").write_bytes("\r\n".join(lines).encode("utf-16"))


class StuckMT5(FakeMT5):
    """A terminal that doesn't know the broker's server: it starts but never connects."""
    def initialize(self, **kw):
        self.init_kw = kw
        return False

    def last_error(self):
        return (-10005, "IPC timeout")


def test_unknown_server_detected_from_journal(tmp_path, monkeypatch):
    term = tmp_path / "acc" / "terminal64.exe"
    term.parent.mkdir()
    term.write_bytes(b"x")
    _journal(term.parent, [
        "IH\t0\t00:12:58.386\tTerminal\tMetaTrader 5 x64 build 6320 started for MetaQuotes Ltd.",
        "OM\t0\t00:12:58.387\tTerminal\tlaunched with pf_login.ini",
        "KM\t0\t00:13:33.666\tTester\tMQL5 Cloud Server found",
        "XX\t0\t00:13:34.000\tCompiler\tcompiled something"])
    mt5 = StuckMT5()
    w = Worker(mt5, FakeAPI(), 1, 123, "pw", "PUPrime-Demo", str(term), tmp_path / "s.json")
    monkeypatch.setattr(w, "login_via_config", lambda: False)
    assert not w.connect()
    assert mt5.init_kw["timeout"] == 300_000                 # first start of a new copy gets longer
    assert w.unknown_server and "PUPrime-Demo" in w.error
    rep = w.report()
    assert rep["unknown_server"] and any("started for" in j for j in rep["journal"])
    assert not any("compiled" in j for j in rep["journal"])
    w.connect()
    assert mt5.init_kw["timeout"] == 90_000


def test_slow_start_with_network_is_not_called_unknown_server(tmp_path):
    term = tmp_path / "acc" / "terminal64.exe"
    term.parent.mkdir()
    term.write_bytes(b"x")
    _journal(term.parent, [
        "IH\t0\t00:12:58.386\tTerminal\tMetaTrader 5 x64 build 6320 started for MetaQuotes Ltd.",
        "AB\t0\t00:13:01.000\tNetwork\t'123': authorized on PUPrime-Demo"])
    w = Worker(StuckMT5(), FakeAPI(), 1, 123, "pw", "PUPrime-Demo", str(term), tmp_path / "s.json")
    w.config_login_at = 1e18
    assert not w.connect() and not w.unknown_server and "too long" in w.error


def test_worker_takes_bigger_broker_list(tmp_path, monkeypatch):
    term = tmp_path / "acc" / "terminal64.exe"
    (term.parent / "Config").mkdir(parents=True)
    term.write_bytes(b"x")
    (term.parent / "Config" / "servers.dat").write_bytes(b"small")
    lib = tmp_path / "base" / "Config" / "servers.dat"
    lib.parent.mkdir(parents=True)
    lib.write_bytes(b"many more brokers")
    mt5 = FakeMT5()
    w = Worker(mt5, FakeAPI(), 1, 123, "pw", "PUPrime-Demo", str(term), tmp_path / "s.json", library=str(lib))
    stopped = []
    monkeypatch.setattr(w, "stop_terminal", lambda: stopped.append(1))
    assert w.library_has_more()
    assert w.connect() and stopped
    assert (term.parent / "Config" / "servers.dat").read_bytes() == b"many more brokers"
    assert not w.library_has_more()


def test_admin_account_page_shows_journal(browser):
    from server.db import Account, SessionLocal
    b = admin_browser(browser)
    from server.db import Plan, User
    with SessionLocal() as s:
        u = User(username="journaltest", email="j@example.com", password_hash="x",
                 plan_id=s.query(Plan).first().id)
        s.add(u)
        s.flush()
        acc = Account(user_id=u.id, login="5550001", server="PUPrime-Demo", password_enc="x",
                      live_state={"journal": ["00:13:33 Tester: hello journal"], "unknown_server": True})
        s.add(acc)
        s.commit()
        aid = acc.id
    page = b.get(f"/admin/accounts/{aid}").text
    assert "hello journal" in page and "add a broker" in page

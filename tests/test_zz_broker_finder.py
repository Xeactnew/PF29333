"""1.3.35: the node looks up brokers MT5 doesn't know yet, in a spare MT5 with no account."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "node"))
import broker_finder as bf  # noqa: E402
import node_agent  # noqa: E402
from tests.test_node import make_node  # noqa: E402


class FakeDriver:
    """Stands in for MT5's Open an Account window: searching a name MetaQuotes knows adds its servers."""
    def __init__(self, knows=(), works=True):
        self.knows = {k.lower() for k in knows}
        self.works = works
        self.queries = []

    def run(self, exe, query, dump):
        self.queries.append(query)
        assert not (Path(exe).parent / "Config" / "accounts.dat").exists()     # never an account in it
        if not self.works:
            return {"searched": False, "detail": "the Open an Account window didn't open"}
        if query.lower() in self.knows:
            f = Path(exe).parent / "Config" / "servers.dat"
            f.write_bytes(f.read_bytes() + b"+" + query.encode())
        return {"searched": True, "detail": "typed; clicked Find; cancelled"}


def _node(tmp_path, driver, clock=None):
    n = make_node(tmp_path)
    n.finder_on = True
    n.finder.driver = driver
    if clock:
        n.finder.clock = clock
    return n


def _ask(n, aid, server):
    folder = n.account_folder(aid)
    (folder / "Config").mkdir(parents=True, exist_ok=True)
    (folder / "Config" / bf.MARKER).write_text(server)


def _wait(n):
    if n.finder.thread:
        n.finder.thread.join(10)


def test_queries_for():
    assert bf.queries_for("TradeQuo-Demo") == ["TradeQuo-Demo", "TradeQuo"]
    assert bf.queries_for("ICMarketsSC-MT5-2", "IC Markets") == ["ICMarketsSC-MT5-2", "IC Markets", "ICMarketsSC"]
    assert bf.queries_for("Exness-MT5Real7") == ["Exness-MT5Real7", "Exness"]


def test_unknown_broker_is_looked_up_and_shared(tmp_path):
    drv = FakeDriver(knows=["TradeQuo-Demo"])
    n = _node(tmp_path, drv)
    lib_before = node_agent.file_size(n.library)
    a = {"account_id": 4, "login": "106644", "server": "TradeQuo-Demo", "broker": "TradeQuo", "password": "p"}
    _ask(n, 4, "TradeQuo-Demo")
    n.finder_tick([a], [])
    _wait(n)
    assert drv.queries == ["TradeQuo-Demo"]
    assert node_agent.file_size(n.library) > lib_before          # the bigger list is now everyone's
    assert n.finder.state["servers"]["TradeQuo-Demo"]["grew"]
    # the account connected (its worker removed the note): the record is cleared
    (n.account_folder(4) / "Config" / bf.MARKER).unlink()
    n.finder_tick([a], [])
    assert n.finder.state["servers"]["TradeQuo-Demo"]["tries"] == 0


def test_tries_the_next_name_then_gives_up_for_a_day(tmp_path):
    t = [1000.0]
    drv = FakeDriver(knows=[])
    n = _node(tmp_path, drv, clock=lambda: t[0])
    a = {"account_id": 9, "login": "1", "server": "Weird-Live", "broker": "Weird Markets", "password": "p"}
    _ask(n, 9, "Weird-Live")
    for _ in range(3):
        n.finder_tick([a], [])
        _wait(n)
        n.finder_tick([a], [])                                   # too soon: no second search
        _wait(n)
        t[0] += bf.RETRY_SECONDS + 1
    assert drv.queries == ["Weird-Live", "Weird Markets", "Weird"]
    n.finder_tick([a], [])                                       # every name tried: give up for a day
    _wait(n)
    rep = n.finder.report()
    assert rep[0]["server"] == "Weird-Live" and rep[0]["gave_up"] and len(drv.queries) == 3
    t[0] += bf.GIVE_UP_FOR + 1
    n.finder_tick([a], [])
    _wait(n)
    assert drv.queries[-1] == "Weird-Live"                       # a day later it starts again


def test_window_failure_is_reported(tmp_path):
    n = _node(tmp_path, FakeDriver(works=False))
    a = {"account_id": 5, "login": "1", "server": "New-Demo", "broker": "", "password": "p"}
    _ask(n, 5, "New-Demo")
    n.finder_tick([a], [])
    _wait(n)
    r = n.finder.report()[0]
    assert r["searched"] is False and "didn't open" in r["detail"]


def test_heartbeat_carries_the_search_results(tmp_path, monkeypatch):
    import io
    import json
    n = _node(tmp_path, FakeDriver(works=False))
    a = {"account_id": 5, "login": "1", "server": "New-Demo", "broker": "", "password": "p"}
    _ask(n, 5, "New-Demo")
    n.finder_tick([a], [])
    _wait(n)
    sent = {}

    def fake_urlopen(req, timeout=0):
        sent.update(json.loads(req.data.decode()))
        return io.BytesIO(b'{"assignments": []}')
    monkeypatch.setattr(node_agent.urllib.request, "urlopen", fake_urlopen)
    n.heartbeat()
    assert sent["broker_searches"][0]["server"] == "New-Demo" and sent["finder_busy"] is False


def test_prefetches_the_website_brokers_when_idle(tmp_path):
    t = [1000.0]
    drv = FakeDriver(knows=["Vantage"])
    n = _node(tmp_path, drv, clock=lambda: t[0])
    _ready_base(n)
    n.finder_tick([], ["Vantage", "PU Prime"])
    _wait(n)
    n.finder_tick([], ["Vantage", "PU Prime"])                    # waits a few minutes between brokers
    _wait(n)
    assert drv.queries == ["Vantage"]
    t[0] += bf.PREFETCH_GAP + 1
    n.finder_tick([], ["Vantage", "PU Prime"])
    _wait(n)
    assert drv.queries == ["Vantage", "PU Prime"]
    assert n.finder.state["prefetch"]["Vantage"]["grew"] and not n.finder.state["prefetch"]["PU Prime"]["grew"]
    t[0] += bf.PREFETCH_GAP + 1
    n.finder_tick([], ["Vantage", "PU Prime"])                    # all done: nothing more
    _wait(n)
    assert len(drv.queries) == 2


def _ready_base(n):
    (n.base / "Config").mkdir(parents=True, exist_ok=True)
    (n.base / "Config" / "servers.dat").write_bytes(b"servers")
    (n.base / "terminal64.exe").write_bytes(b"x")
    (n.base / node_agent.BASE_STAMP).write_text(n.template_stamp())


def test_waiting_accounts_come_before_prefetch(tmp_path):
    drv = FakeDriver(knows=["New-Demo"])
    n = _node(tmp_path, drv)
    _ready_base(n)
    a = {"account_id": 6, "login": "1", "server": "New-Demo", "broker": "", "password": "p"}
    _ask(n, 6, "New-Demo")
    n.finder_tick([a], ["Vantage"])
    _wait(n)
    assert drv.queries == ["New-Demo"]


def test_spare_mt5_never_gets_a_saved_login(tmp_path):
    n = _node(tmp_path, FakeDriver())
    (n.template / "Config" / "accounts.dat").write_bytes(b"owner's saved login")
    exe = n.finder.prepare()
    assert exe.exists() and not (exe.parent / "Config" / "accounts.dat").exists()
    assert (exe.parent / "Config" / "servers.dat").exists()


def test_state_survives_a_node_restart(tmp_path):
    drv = FakeDriver(knows=["TradeQuo-Demo"])
    n = _node(tmp_path, drv)
    a = {"account_id": 4, "login": "1", "server": "TradeQuo-Demo", "broker": "", "password": "p"}
    _ask(n, 4, "TradeQuo-Demo")
    n.finder_tick([a], [])
    _wait(n)
    n2 = node_agent.Node(n.cfg)
    assert n2.finder.state["servers"]["TradeQuo-Demo"]["tries"] == 1


def test_off_by_default_off_windows(tmp_path):
    n = make_node(tmp_path)
    assert n.finder_on is (node_agent.os.name == "nt")


class FakeWin:
    """MT5's windows as plain data: a main window, then Open an Account with a search box, Find, a list."""
    def __init__(self, opens_by_itself=True, results=3):
        self.main, self.dlg, self.edit, self.find, self.lv, self.nxt, self.cancel = 1, 2, 3, 4, 5, 6, 7
        self.dialog_open = opens_by_itself
        self.results = results
        self.typed = None
        self.posted = []
        self.closed = False

    def top_windows(self, pid):
        return [] if self.closed else ([self.main] + ([self.dlg] if self.dialog_open else []))

    def cls(self, h):
        return {1: "MetaQuotes::MetaTrader::5.00", 2: "#32770", 3: "Edit", 4: "Button", 5: "SysListView32",
                6: "Button", 7: "Button"}[h]

    def text(self, h):
        return {1: "MetaTrader 5", 2: "Open an Account", 4: "&Find your company", 6: "&Next >",
                7: "Cancel"}.get(h, "")

    def children(self, h):
        return [3, 4, 5, 6, 7] if h == self.dlg else []

    def visible(self, h): return True
    def enabled(self, h): return True
    def ctrl_id(self, h): return 1000 + h
    def parent(self, h): return self.dlg
    def area(self, h): return 100 if h == self.main else 10

    def menu_items(self, h):
        return [("Open an Account", 33001), ("Login to Trade Account", 33002)] if h == self.main else []

    def set_text(self, h, v):
        assert h == self.edit
        self.typed = v

    def send(self, h, msg, wp=0, lp=0):
        return self.results if msg == bf.LVM_GETITEMCOUNT else 0

    def post(self, h, msg, wp=0, lp=0):
        self.posted.append((h, msg, wp))
        if h == self.main and msg == bf.WM_COMMAND and wp == 33001:
            self.dialog_open = True
        if h == self.main and msg == bf.WM_CLOSE:
            self.closed = True


class FakeProc:
    pid = 42

    def wait(self, t):
        return 0

    def kill(self):
        raise AssertionError("closed normally, never killed")


def _driver(win):
    return bf.WindowDriver(win=win, launch=lambda exe: FakeProc(), sleep=lambda s: None)


def test_window_driver_searches_and_closes(tmp_path):
    win = FakeWin()
    res = _driver(win).run(tmp_path / "terminal64.exe", "TradeQuo", tmp_path / "dump.txt")
    assert res["searched"] and win.typed == "TradeQuo"
    clicked = [wp & 0xFFFF for h, m, wp in win.posted if h == win.dlg and m == bf.WM_COMMAND]
    assert 1000 + win.find in clicked and 1000 + win.nxt in clicked and 1000 + win.cancel in clicked
    assert win.closed and "3 result" in res["detail"]
    assert "Open an Account" in (tmp_path / "dump.txt").read_text()


def test_window_driver_uses_the_menu_when_the_window_doesnt_open(tmp_path):
    win = FakeWin(opens_by_itself=False)
    t = [0.0]
    drv = bf.WindowDriver(win=win, launch=lambda exe: FakeProc(), sleep=lambda s: t.__setitem__(0, t[0] + s),
                          clock=lambda: t[0])
    res = drv.run(tmp_path / "terminal64.exe", "Vantage", tmp_path / "dump.txt")
    assert res["searched"] and "File > Open an Account" in res["detail"]


def test_window_driver_reports_a_missing_search_box(tmp_path):
    win = FakeWin()
    win.children = lambda h: []
    res = _driver(win).run(tmp_path / "terminal64.exe", "X", tmp_path / "dump.txt")
    assert not res["searched"] and "no search box" in res["detail"] and win.closed
    assert (tmp_path / "dump.txt").read_text().startswith("FAILED")

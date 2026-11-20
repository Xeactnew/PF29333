"""Whole chain over real HTTP: Telegram message -> server -> worker (fake MT5) -> trades -> website."""
import re
import socket
import sys
import threading
import time
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "node"))
from tests.fake_mt5 import FakeMT5  # noqa: E402
from tests.test_web import admin_browser  # noqa: E402
from worker import ServerAPI, Worker  # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_full_chain(app, browser, tmp_path):
    from server.db import SessionLocal, main_source
    from server.telegram_service import handle_message
    from tests.conftest import Browser

    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    while not srv.started:
        time.sleep(0.05)
    try:
        b = admin_browser(browser)
        b.post("/admin/users/create", {"username": "erin", "plan_id": "3", "billing": "lifetime", "password": "erinpass1"})
        r = b.post("/admin/nodes/create", {"name": "E2E VPS", "max_slots": "50"})
        token = [t.strip() for t in re.findall(r'<div class="secret"[^>]*>([^<]+)</div>', r.text) if "powershell" not in t][0]

        eb = Browser(app)
        eb.get("/login")
        eb.post("/login", {"username": "erin", "password": "erinpass1"})
        r = eb.post("/accounts/add", {"login": "9090", "password": "pw", "server": "Fake-Live"})
        aid = int(re.search(r"/accounts/(\d+)/settings", r.text).group(1))
        # Extreme plan: 3 layers with smart entry off, 1% risk
        eb.get(f"/accounts/{aid}")
        with SessionLocal() as s:
            main_id = main_source(s).id
        eb.post(f"/accounts/{aid}/settings", {"layers": "3", "lot_mode": "risk_percent", "risk_percent": "1",
                                              "breakeven_at_tp1": "on", "cancel_pending_after_tp1": "on",
                                              "follow_updates": "on", "require_sl": "on", "skip_if_tp1_passed": "on",
                                              "sources": str(main_id), "max_lot": "5"})

        import json
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/node/heartbeat", data=b"{}", method="POST",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        assigned = json.loads(urllib.request.urlopen(req).read())["assignments"]
        a = next(x for x in assigned if x["account_id"] == aid)

        mt5 = FakeMT5(bid=4036.0)
        w = Worker(mt5, ServerAPI(f"http://127.0.0.1:{port}", token, aid), aid, int(a["login"]), a["password"],
                   a["server"], None, tmp_path / "state.json")
        assert w.connect()
        w.step()                                   # first poll sets the event cursor
        res = handle_message(main_id, 9001, "XAUUSD BUY ZONE : 4037-4030\nSL: 4027\nTP1: 4047\nTP2: 4057\nTP3: 4067", None)
        assert res.startswith("signal:")
        w.step()
        assert len(mt5.positions) == 1 and len(mt5.orders) == 2   # layer 1 at market, 2 limits below
        lot = next(iter(mt5.positions.values())).volume
        assert lot == 0.05   # 100 risk / (920 + 650 + 300 loss per lot across the 3 layers)

        mt5.move(4047.2)
        w.step()                                   # TP1 by price: layer 1 done, limits cancelled
        assert not mt5.orders
        handle_message(main_id, 9002, "TP1 HIT ✅ +100 pips", 9001)
        w.last_report = 0
        w.last_deals = 0
        w.step()

        r = eb.get("/dashboard")
        assert "+$54.00" in r.text            # 0.05 lots from 4036.20 to TP1 4047
        r = eb.get(f"/accounts/{aid}")
        assert "Fake Broker" in r.text and "XAUUSD.s" in r.text
        r = eb.get("/signals")
        assert "TP1 hit" in r.text
    finally:
        from server.db import Node
        with SessionLocal() as s:          # leave capacity as the other tests expect
            for n in s.query(Node).filter(Node.name == "E2E VPS"):
                n.enabled = False
            s.commit()
        srv.should_exit = True
        th.join(5)

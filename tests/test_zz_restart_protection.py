"""After a VPS restart everything comes back by itself, and the admins are told."""
import socket
import time

from server import notify, scheduler, system
from server.db import Account, Node, Plan, SessionLocal, User, utcnow
from tests.test_web import admin_browser


def test_boot_notice(browser, monkeypatch):
    with SessionLocal() as s:
        n = Node(name="Boot VPS", token_hash="bootx", max_slots=5, last_seen=utcnow(),
                 info={"hostname": socket.gethostname()})
        s.add(n)
        s.flush()
        u = User(username="bootcust", email="b@x.com", password_hash="x", plan_id=s.query(Plan).first().id)
        s.add(u)
        s.flush()
        a = Account(user_id=u.id, login="777001", server="X", password_enc="x", node_id=n.id, conn_status="starting")
        s.add(a)
        s.commit()
        aid = a.id
    sent = []
    monkeypatch.setattr(notify, "send_admins", lambda text, *a, **k: sent.append(text))
    now = time.time()
    monkeypatch.setattr(scheduler, "_website_started", now - 60)
    monkeypatch.setitem(scheduler._boot, "at", now - 120)       # Windows started 2 minutes ago
    monkeypatch.setitem(scheduler._boot, "done", False)
    # other test accounts on other VPS don't count; ours is still connecting -> wait
    assert scheduler.boot_tick(now) is None
    with SessionLocal() as s:
        s.get(Account, aid).conn_status = "online"
        for other in s.query(Account).filter(Account.node_id == s.get(Account, aid).node_id, Account.id != aid):
            other.conn_status = "online"
        s.commit()
    text = scheduler.boot_tick(now)
    assert text and "came back by itself" in text and "MT5 manager ✓" in text and sent == [text]
    assert scheduler.boot_tick(now) is None                     # only once


def test_boot_notice_reports_what_is_missing(browser, monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send_admins", lambda text, *a, **k: sent.append(text))
    now = time.time()
    monkeypatch.setattr(scheduler, "_website_started", now - 700)
    monkeypatch.setitem(scheduler._boot, "at", now - 760)
    monkeypatch.setitem(scheduler._boot, "done", False)
    with SessionLocal() as s:
        for a in s.query(Account).filter(Account.login == "777001"):
            a.conn_status = "error"
        s.commit()
    text = scheduler.boot_tick(now)
    assert text and "Not online yet" in text and "777001" in text


def test_no_notice_when_only_the_website_restarted(monkeypatch):
    monkeypatch.setitem(scheduler._boot, "at", time.time() - 86400)
    monkeypatch.setitem(scheduler._boot, "done", False)
    monkeypatch.setattr(scheduler, "_website_started", time.time())
    assert scheduler.boot_tick() is None


def test_system_page_lists_restart_protection(browser):
    b = admin_browser(browser)
    page = b.get("/admin/system").text
    assert "Restart protection" in page and "Watchdog every 5 minutes" in page and "Windows logs in by itself" in page
    labels = [x[0] for x in system.restart_protection(True)]
    assert "Tunnel starts with Windows" in labels and len(labels) == 8


def test_watchdog_script_covers_everything():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    w = (root / "tools" / "watchdog.ps1").read_text()
    assert "PipsFarmer Website" in w and "PipsFarmer MT5 Node" in w and "cloudflared" in w
    r = (root / "tools" / "register_watchdog.ps1").read_text()
    assert "AtStartup" in r and "RepetitionInterval" in r and "sc.exe failure cloudflared" in r
    u = (root / "update_vps.ps1").read_text()
    assert "register_watchdog.ps1" in u and "PF_NODE_HIDDEN" in u

"""Everything that lets PipsFarmer run without anyone on the VPS."""
import io
import re
import sys
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from server import notify, runtime, scheduler, services, system
from server import telegram_service as tg
from server.db import Account, Node, SessionLocal, User, utcnow
from tests.test_web import admin_browser

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "node"))
import node_agent  # noqa: E402


def test_admin_pages_render(browser):
    b = admin_browser(browser)
    for page in ("/admin/telegram", "/admin/system", "/admin/nodes", "/admin"):
        r = b.get(page)
        assert r.status_code == 200, page
    assert "Hands-off checklist" in b.get("/admin").text


def test_telegram_settings_saved_without_env(browser, monkeypatch):
    b = admin_browser(browser)
    r = b.post("/admin/telegram/api", {"api_id": "12345", "api_hash": "0123456789abcdef0123456789abcdef"})
    assert "API keys saved" in r.text
    assert runtime.tg_api() == (12345, "0123456789abcdef0123456789abcdef")
    assert tg.api_ready()

    async def fake_check(token):
        assert token == "123:abc"
        return "pf_alerts_bot"
    monkeypatch.setattr(tg, "check_bot_token", fake_check)
    r = b.post("/admin/telegram/bot", {"token": "123:abc"})
    assert "@pf_alerts_bot connected" in r.text
    assert runtime.bot_token() == "123:abc" and runtime.bot_username() == "pf_alerts_bot"
    with SessionLocal() as s:            # stored encrypted, not in plain text
        from server.db import Setting
        assert "123:abc" not in str(s.get(Setting, "tg_bot_token_enc").value)

    r = b.post("/admin/telegram/link", {})
    link = re.search(r"https://t\.me/pf_alerts_bot\?start=([\w-]+)", r.text)
    assert link
    assert tg.link_admin_chat(link.group(1), 55501) == "boss"
    assert tg.link_admin_chat(link.group(1), 55501) is None          # one use only
    assert 55501 in tg.admin_chat_ids()

    r = b.post("/admin/telegram/vip", {"chat_id": "-1001234"})
    assert runtime.vip_chats() == [-1001234]


def test_system_settings_and_admins(browser):
    b = admin_browser(browser)
    r = b.post("/admin/system/settings", {"public_url": "https://pipsfarmer.com", "daily_summary": "on",
                                          "summary_hour_utc": "6"})
    assert "Saved" in r.text and runtime.public_url() == "https://pipsfarmer.com"
    r = b.post("/admin/system/admins", {"username": "partner", "password": "longenough1"})
    assert "Admin partner added" in r.text
    r = b.post("/admin/system/backup", {"what": "db"})
    assert "Backup saved" in r.text
    assert list(system.BACKUP_DIR.glob("pipsfarmer-manual-*.db"))


def test_update_upload_is_checked(browser):
    b = admin_browser(browser)
    b.get("/admin/system")
    r = b.c.post("/admin/system/update", data={"csrf": b.csrf},
                 files={"zip": ("x.zip", b"not a zip", "application/zip")}, follow_redirects=True)
    assert "Update not started" in r.text
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("pipsfarmer/server/app.py", "x")
        z.writestr("pipsfarmer/update_vps.ps1", "x")
    root = system.stage_update(buf.getvalue())
    assert root.name == "pipsfarmer" and (root / "server" / "app.py").exists()
    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("../evil.txt", "x")
    with pytest.raises(ValueError):
        system.stage_update(bad.getvalue())


def test_node_install_command_and_package(browser):
    b = admin_browser(browser)
    r = b.post("/admin/nodes/create", {"name": "VPS 9", "max_slots": "5"})
    m = re.search(r"/api/install/([\w-]+)\.ps1", r.text)
    assert m
    token = [t.strip() for t in re.findall(r'<div class="secret"[^>]*>([^<]+)</div>', r.text) if "powershell" not in t][0]
    script = b.c.get(f"/api/install/{m.group(1)}.ps1").text
    assert token in script and "VPS 9" in script and "__SERVER_URL__" not in script
    assert b.c.get("/api/install/nope.ps1").status_code == 404

    H = {"Authorization": f"Bearer {token}"}
    pkg = b.c.get("/api/node/package", headers=H)
    assert pkg.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(pkg.content)).namelist()
    assert "worker.py" in names and "node_agent.py" in names
    assert b.c.get("/api/node/package").status_code == 401

    # heartbeat carries the package hash and queued commands, and stores health info
    with SessionLocal() as s:
        node = s.scalar(__import__("sqlalchemy").select(Node).where(Node.name == "VPS 9"))
    services.queue_node_command(node.id, "autologon", user="Administrator", password="pw")
    r = b.c.post("/api/node/heartbeat", json={"version": "1.2", "autologon": False, "reboot_pending": True}, headers=H)
    body = r.json()
    assert body["node_hash"] == system.node_package()[1]
    assert body["commands"] == [{"kind": "autologon", "user": "Administrator", "password": "pw"}]
    with SessionLocal() as s:
        assert s.get(Node, node.id).info["reboot_pending"] is True
    assert b.c.post("/api/node/heartbeat", json={}, headers=H).json()["commands"] == []   # sent once

    # buttons on the Servers page queue commands
    b.get("/admin/nodes")
    b.post(f"/admin/nodes/{node.id}", {"action": "reboot"})
    assert services.take_node_commands(node.id) == [{"kind": "reboot"}]


def test_secure_cookie_behind_https_tunnel(app):
    from fastapi.testclient import TestClient
    c = TestClient(app)
    r = c.get("/login", headers={"x-forwarded-proto": "https"})
    assert "secure" in r.headers.get("set-cookie", "").lower()
    r = TestClient(app).get("/login")
    assert "secure" not in r.headers.get("set-cookie", "").lower()


def _drain():
    return notify.take_all()


def _settings(**kw):
    from server.db import set_setting
    with SessionLocal() as s:
        for k, v in kw.items():
            set_setting(s, k, v)
        s.commit()


def test_watch_alerts_and_weekend_restart(browser):
    _drain()
    _settings(weekend_restarts=True)
    with SessionLocal() as s:
        node = Node(name="Watch VPS", token_hash="x", max_slots=5, last_seen=utcnow(),
                    info={"autologon": False, "reboot_pending": True, "disk_free_gb": 3.0})
        s.add(node)
        s.flush()
        u = s.scalar(__import__("sqlalchemy").select(User))
        acc = Account(user_id=u.id, login="7001", server="X-Live", password_enc="", node_id=node.id,
                      conn_status="error", conn_error="wrong login, password or server name",
                      created_at=utcnow() - timedelta(hours=1), last_report=utcnow())
        s.add(acc)
        s.commit()
        nid = node.id
    scheduler.watch_tick()
    texts = " ".join(i["text"] for i in _drain())
    assert "7001" in texts and "auto-login" in texts and "GB disk" in texts
    scheduler.watch_tick()
    assert not [i for i in _drain() if "7001" in i["text"]]          # no repeat alerts
    # Saturday 10:00 UTC: restart for the pending Windows update only if auto-login is on
    sat = datetime(2026, 11, 21, 10, 0)
    scheduler.watch_tick(sat)
    assert {"kind": "reboot"} not in services.take_node_commands(nid)
    with SessionLocal() as s:
        n = s.get(Node, nid)
        n.info = {**n.info, "autologon": True}
        n.last_seen = utcnow()
        s.commit()
    scheduler.watch_tick(sat)
    assert {"kind": "reboot"} in services.take_node_commands(nid)
    scheduler.watch_tick(sat)
    assert services.take_node_commands(nid) == []                    # once per weekend
    with SessionLocal() as s:
        s.get(Node, nid).enabled = False
        s.commit()


def test_daily_backup_and_summary():
    _drain()
    _settings(backup_to_telegram=True, daily_summary=True, summary_hour_utc=7, daily_done={})
    now = datetime(2026, 11, 20, 8, 0)
    scheduler.daily_tick(now)
    items = _drain()
    assert any(i.get("file") and i["file"][0].endswith(".db.gz") for i in items)
    assert any("daily summary" in i["text"] for i in items)
    scheduler.daily_tick(now)
    assert _drain() == []                                            # once a day


def test_node_hash_matches_server():
    assert node_agent.node_hash(system.ROOT / "node") == system.node_package()[1]


def make_node(tmp_path):
    from tests.test_node import make_node as mk
    return mk(tmp_path)


def test_node_handles_commands_and_cleans_logs(tmp_path, monkeypatch):
    n = make_node(tmp_path)
    called = []
    monkeypatch.setattr(node_agent, "set_autologon", lambda u, d, p: called.append((u, p)) or "auto-login turned on")
    n.handle_commands([{"kind": "autologon", "user": "Administrator", "password": "pw"}, {"kind": "restart_node"}])
    assert called == [("Administrator", "pw")] and n.exit_code == 0
    folder = n.account_folder(3)
    (folder / "logs").mkdir(parents=True)
    old = folder / "logs" / "20250101.log"
    old.write_text("x")
    import os
    os.utime(old, (time.time() - 30 * 86400,) * 2)
    big = folder / "pipsfarmer_worker.log"
    big.write_bytes(b"x" * 5_000_100)
    n.housekeeping()
    assert not old.exists() and not big.exists() and (folder / "pipsfarmer_worker.old.log").exists()


def test_node_self_update(tmp_path, monkeypatch):
    """The node downloads new files when the website's hash differs, then exits so it restarts."""
    n = make_node(tmp_path)
    here = tmp_path / "nodecode"
    here.mkdir()
    for name in node_agent.NODE_FILES:
        (here / name).write_text("old " + name)
    monkeypatch.setattr(node_agent, "HERE", here)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name in node_agent.NODE_FILES:
            z.writestr(name, ("new " + name) if name != "requirements-node.txt" else "old requirements-node.txt")

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(node_agent.urllib.request, "urlopen", lambda req, timeout=0: Resp(buf.getvalue()))
    n.maybe_update("deadbeef")
    assert (here / "worker.py").read_text() == "new worker.py" and n.exit_code == 0
    n.exit_code = None
    n.maybe_update("deadbeef")            # same hash isn't retried within the hour
    assert n.exit_code is None
    n.maybe_update(node_agent.node_hash(here))
    assert n.exit_code is None

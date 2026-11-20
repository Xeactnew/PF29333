"""Update from GitHub: token storage, check, re-rooted install, cert fallback, and the admin bot button."""
import asyncio
import io
import json
import ssl
import urllib.error
import zipfile

import pytest

from server import github_update as gh
from server import system, telegram_service
from server.db import SessionLocal, get_setting
from tests.test_web import admin_browser

TOKEN = "github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyzWXYZ"
SHA = "a" * 40


def _zipball() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        top = f"Xeactnew-PF29333-{SHA[:7]}/"
        z.writestr(top, "")
        z.writestr(top + "server/app.py", "app")
        z.writestr(top + "server/system.py", 'VERSION = "9.9.9"')
        z.writestr(top + "update_vps.ps1", "# update")
        z.writestr(top + "server/__pycache__/app.cpython-312.pyc", "junk")
        z.writestr(top + ".pytest_cache/v/x", "junk")
    return buf.getvalue()


def _fake_get(calls):
    def fake(url, token, accept="application/vnd.github+json", timeout=30):
        calls.append(url)
        assert token == TOKEN
        if url.endswith("/commits/main"):
            return json.dumps({"sha": SHA, "commit": {"message": "Max lots cap\n\nmore",
                                                      "committer": {"date": "2026-11-20T10:00:00Z"}}}).encode()
        if "/contents/server/system.py" in url:
            return b'VERSION = "9.9.9"'
        if "/zipball/" in url:
            return _zipball()
        raise AssertionError(url)
    return fake


def test_token_saved_encrypted_and_never_shown(browser):
    b = admin_browser(browser)
    r = b.post("/admin/system/github/token", {"token": TOKEN})
    assert "GitHub token saved" in r.text
    assert TOKEN not in r.text and "WXYZ" in r.text
    with SessionLocal() as s:
        stored = get_setting(s, "gh_token_enc")
    assert stored and TOKEN not in stored
    assert gh._token() == TOKEN and gh.token_hint() == "WXYZ"
    assert TOKEN not in b.get("/admin/system").text
    r = b.post("/admin/system/github/token", {"token": "nope"})
    assert "doesn&#39;t look like a GitHub token" in r.text or "doesn't look like" in r.text
    r = b.post("/admin/system/github/token", {"action": "remove"})
    assert "GitHub token removed" in r.text and gh.token_hint() == ""


def test_check_and_install(browser, monkeypatch):
    calls, started = [], []
    gh.save_token(TOKEN)
    monkeypatch.setattr(gh, "_get", _fake_get(calls))
    monkeypatch.setattr(system, "run_update", lambda root: started.append(root))
    monkeypatch.setattr(system, "backup_db", lambda *a, **k: None)
    b = admin_browser(browser)
    r = b.post("/admin/system/github/check")
    assert "A new version is ready" in r.text and "aaaaaaa" in r.text and "Max lots cap" in r.text
    assert "v9.9.9" in r.text and "commit unknown" in r.text
    r = b.post("/admin/system/github/install", {"sha": SHA})
    assert "Installing commit aaaaaaa" in r.text and started
    root = started[0]
    assert root.name == "pipsfarmer" and (root / "server" / "app.py").exists()
    assert not (root / "server" / "__pycache__").exists() and not (root / ".pytest_cache").exists()
    assert (root / ".commit").read_text().strip() == SHA
    # update_vps.ps1 copies .commit into data/; once there the check says we're up to date
    gh.DATA_DIR.mkdir(parents=True, exist_ok=True)
    (gh.DATA_DIR / gh.COMMIT_FILE_NAME).write_text(SHA)
    assert gh.check()["new"] is False
    assert TOKEN not in b.get("/admin/system").text


def test_reroot_handles_nested_package():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("repo-abc/README.md", "top readme")
        z.writestr("repo-abc/pipsfarmer/server/app.py", "app")
        z.writestr("repo-abc/pipsfarmer/update_vps.ps1", "x")
    names = zipfile.ZipFile(io.BytesIO(gh.reroot(buf.getvalue()))).namelist()
    assert sorted(names) == ["pipsfarmer/server/app.py", "pipsfarmer/update_vps.ps1"]
    with pytest.raises(ValueError):
        gh.reroot(b"not a zip")


def test_certificate_failure_falls_back_to_powershell(monkeypatch):
    def bad(*a, **k):
        raise urllib.error.URLError(ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED"))
    seen = {}

    def fake_run(cmd, env=None, **k):
        seen["cmd"], seen["env"] = cmd, env
        with open(env["PF_GH_OUT"], "wb") as f:
            f.write(b"ok")

        class R:
            returncode, stdout, stderr = 0, "", ""
        return R()
    monkeypatch.setattr(gh, "_urllib_get", bad)
    monkeypatch.setattr(gh.subprocess, "run", fake_run)
    monkeypatch.setattr(system, "IS_WINDOWS", True)
    assert gh._get("https://api.github.com/x", TOKEN) == b"ok"
    assert all(TOKEN not in part for part in seen["cmd"])          # never on the command line
    assert seen["env"]["PF_GH_TOKEN"] == TOKEN


def test_errors_never_contain_the_token(monkeypatch):
    def boom(*a, **k):
        raise urllib.error.URLError(f"failed with {TOKEN}")
    monkeypatch.setattr(gh, "_urllib_get", boom)
    with pytest.raises(RuntimeError) as e:
        gh._get("https://api.github.com/x", TOKEN)
    assert TOKEN not in str(e.value)


def test_admin_bot_update_button(monkeypatch):
    sent, started = [], []

    async def fake_bot(http, method, token=None, files=None, **params):
        sent.append((method, params))
        return {"message_id": 5}
    gh.save_token(TOKEN)
    monkeypatch.setattr(gh, "_get", _fake_get([]))
    monkeypatch.setattr(gh, "install", lambda sha, who="": started.append(sha) or sha)
    monkeypatch.setattr(telegram_service, "_bot", fake_bot)
    monkeypatch.setattr(telegram_service, "admin_chat_ids", lambda: [111])
    asyncio.run(telegram_service._handle_update(None, {"message": {"chat": {"id": 999}, "text": "/update"}}))
    assert sent == []                                              # not for strangers
    asyncio.run(telegram_service._handle_update(None, {"message": {"chat": {"id": 111}, "text": "/update"}}))
    msg = sent[-1][1]
    assert "Latest on main" in msg["text"] and "Install it?" in msg["text"]
    go = msg["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    assert go.startswith("gh:go:") and not started                 # nothing installed without the tap
    asyncio.run(telegram_service._handle_update(None, {"callback_query": {
        "id": "c1", "from": {"id": 111, "username": "boss"}, "message": {"chat": {"id": 111}, "message_id": 5},
        "data": go}}))
    assert started == [SHA] and "Installing commit aaaaaaa" in sent[-1][1]["text"]

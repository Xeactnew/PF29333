"""Automatic updates: a new commit on main installs itself, but only when it's safe."""
import time
from datetime import timedelta

from server import github_update as gh
from server import notify, scheduler, system
from server.db import SessionLocal, Signal, main_source, set_setting, utcnow


def _setup(monkeypatch, new=True, auto=True):
    with SessionLocal() as s:
        set_setting(s, "gh_auto_update", auto)
        s.commit()
    installed, sent = [], []
    monkeypatch.setattr(gh, "token_hint", lambda: "WXYZ")
    monkeypatch.setattr(gh, "check", lambda: {"sha": "b" * 40, "short": "bbbbbbb", "version": "9.9.9",
                                               "message": "Faster entries", "new": new})
    monkeypatch.setattr(gh, "install", lambda sha, who="": installed.append((sha, who)) or sha)
    monkeypatch.setattr(notify, "send_admins", lambda text, **k: sent.append(text))
    monkeypatch.setattr(system, "IS_WINDOWS", True)
    monkeypatch.setattr(scheduler, "_website_started", time.time() - 3600)
    scheduler._auto["tries"].clear()
    return installed, sent


def _quiet_signals():
    with SessionLocal() as s:
        for x in s.query(Signal).filter(Signal.created_at > utcnow() - timedelta(minutes=10)):
            x.created_at = utcnow() - timedelta(hours=1)
        s.commit()


def test_new_commit_installs_and_tells_admins(browser, monkeypatch):
    installed, sent = _setup(monkeypatch)
    _quiet_signals()
    assert scheduler.github_auto_tick() == "installing"
    assert installed == [("b" * 40, "auto")] and any("automatically" in t for t in sent)


def test_waits_when_unsafe_or_off(browser, monkeypatch):
    installed, _ = _setup(monkeypatch, auto=False)
    assert scheduler.github_auto_tick() == "off"
    installed, _ = _setup(monkeypatch)
    monkeypatch.setattr(scheduler, "_website_started", time.time())
    assert scheduler.github_auto_tick() == "just started"
    monkeypatch.setattr(scheduler, "_website_started", time.time() - 3600)
    with SessionLocal() as s:
        s.add(Signal(source_id=main_source(s).id, side="buy", entry_low=1, entry_high=1, tps=[]))
        s.commit()
    assert scheduler.github_auto_tick() == "signal just in"
    _quiet_signals()
    installed, _ = _setup(monkeypatch, new=False)
    assert scheduler.github_auto_tick() == "up to date" and not installed


def test_gives_up_after_two_failed_installs(browser, monkeypatch):
    installed, _ = _setup(monkeypatch)
    _quiet_signals()
    assert [scheduler.github_auto_tick() for _ in range(3)] == ["installing", "installing", "gave up"]
    assert len(installed) == 2


def test_announces_once_after_update(browser, monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send_admins", lambda text, **k: sent.append(text))
    monkeypatch.setattr(gh, "installed_commit", lambda: "c" * 40)
    with SessionLocal() as s:
        set_setting(s, "gh_announced", "d" * 40)
        s.commit()
    assert scheduler.announce_update() and "ccccccc" in sent[0]
    assert scheduler.announce_update() is None

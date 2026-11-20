import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp()
os.environ.update({
    "PF_ENV_FILE": os.path.join(_tmp, "none.env"),
    "PF_SECRET_KEY": "x" * 40,
    "PF_FERNET_KEY": "ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg=",
    "PF_DB_PATH": os.path.join(_tmp, "test.db"),
    "PF_DISABLE_BACKGROUND": "1",
})

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(autouse=True)
def _tmp_system_dirs(monkeypatch, tmp_path):
    from server import system
    for name in ("BACKUP_DIR", "UPDATE_DIR", "LOG_DIR", "BIN_DIR"):
        monkeypatch.setattr(system, name, tmp_path / name.lower())
    monkeypatch.setattr(system, "SERVERS_FILE", tmp_path / "data" / "mt5-servers.dat")
    from server import reports
    monkeypatch.setattr(reports, "SIGNALS_DIR", tmp_path / "signals")
    from server import site_icon
    monkeypatch.setattr(site_icon, "CUSTOM_DIR", tmp_path / "data")
    from server import github_update
    monkeypatch.setattr(github_update, "DATA_DIR", tmp_path / "data")


@pytest.fixture(scope="session")
def app():
    from server.app import create_app
    return create_app()


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


class Browser:
    def __init__(self, app):
        self.c = TestClient(app)
        self.c.__enter__()
        self.csrf = None

    def get(self, url, **kw):
        r = self.c.get(url, **kw)
        if "text/html" in r.headers.get("content-type", "") and 'name="csrf"' in r.text:
            self.csrf = csrf_of(r.text)
        return r

    def post(self, url, data=None, follow=True):
        if not self.csrf:
            self.get("/login")
        d = dict(data or {})
        d.setdefault("csrf", self.csrf)
        r = self.c.post(url, data=d, follow_redirects=follow)
        if "text/html" in r.headers.get("content-type", "") and 'name="csrf"' in r.text:
            self.csrf = csrf_of(r.text)
        return r


@pytest.fixture
def browser(app):
    return Browser(app)

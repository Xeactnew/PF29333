"""Update PipsFarmer straight from GitHub (Admin → System, or the admin bot).

The owner pastes a fine-grained, read-only token for the repository. It's stored encrypted, never logged
and never shown again (only its last 4 characters). "Install" downloads the main branch as a zip,
re-roots it to pipsfarmer/ and hands it to the same install path an uploaded zip uses.

Some VPS can't verify GitHub's certificate from Python (CERTIFICATE_VERIFY_FAILED). We use certifi's
certificates when it's installed and otherwise fall back to PowerShell's Invoke-WebRequest, which uses
the Windows certificate store. The token goes to PowerShell in an environment variable, never on the
command line.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from . import config, security, system
from .db import SessionLocal, get_setting, set_setting

log = logging.getLogger("pf.github")

REPO = "Xeactnew/PF29333"
BRANCH = "main"
API = "https://api.github.com"
DATA_DIR = config.ROOT / "data"
COMMIT_FILE_NAME = "installed-commit.txt"     # written by update_vps.ps1 from the package's .commit
MARKER = ".commit"                            # sha of the commit a package was built from
SKIP_PARTS = {"__pycache__", ".pytest_cache", ".git", ".github"}

_last: dict = {"check": None, "at": 0.0, "error": ""}
_busy = threading.Lock()


# ------------------------------------------------------------ the token

def save_token(token: str) -> str:
    """Stores the token encrypted. Returns an error message, or '' when saved."""
    token = re.sub(r"\s", "", token or "")
    if len(token) < 20 or not re.fullmatch(r"[A-Za-z0-9_\-]+", token):
        return "That doesn't look like a GitHub token. Paste the whole token (it starts with github_pat_)."
    with SessionLocal() as s:
        set_setting(s, "gh_token_enc", security.encrypt(token))
        set_setting(s, "gh_token_last4", token[-4:])
        s.commit()
    _last.update(check=None, error="")
    return ""


def remove_token() -> None:
    with SessionLocal() as s:
        set_setting(s, "gh_token_enc", "")
        set_setting(s, "gh_token_last4", "")
        s.commit()
    _last.update(check=None, error="")


def _token() -> str:
    with SessionLocal() as s:
        return security.decrypt(get_setting(s, "gh_token_enc") or "")


def token_hint() -> str:
    """'' when no token is saved, otherwise its last 4 characters."""
    with SessionLocal() as s:
        if not get_setting(s, "gh_token_enc"):
            return ""
        return get_setting(s, "gh_token_last4") or "····"


def _scrub(text: str, token: str) -> str:
    text = str(text)
    if token:
        text = text.replace(token, "•••")
    return re.sub(r"(github_pat_|ghp_)[A-Za-z0-9_]+", r"\1•••", text)[:300]


# ------------------------------------------------------------ fetching

def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001  (certifi missing)
        return ssl.create_default_context()


def _is_cert_error(e: BaseException) -> bool:
    reason = getattr(e, "reason", e)
    return isinstance(reason, ssl.SSLError) or "CERTIFICATE_VERIFY_FAILED" in str(e)


def _headers(token: str, accept: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": accept, "User-Agent": "PipsFarmer-updater",
            "X-GitHub-Api-Version": "2022-11-28"}


def _urllib_get(url: str, token: str, accept: str, timeout: int) -> bytes:
    req = urllib.request.Request(url, headers=_headers(token, accept))
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as r:   # noqa: S310
        return r.read()


PS_SCRIPT = (
    "$ErrorActionPreference='Stop';"
    "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;"
    "$h=@{Authorization=('Bearer '+$env:PF_GH_TOKEN);Accept=$env:PF_GH_ACCEPT;"
    "'User-Agent'='PipsFarmer-updater';'X-GitHub-Api-Version'='2022-11-28'};"
    "Invoke-WebRequest -Uri $env:PF_GH_URL -Headers $h -UseBasicParsing -OutFile $env:PF_GH_OUT "
    "-TimeoutSec $env:PF_GH_TIMEOUT | Out-Null"
)


def _powershell_get(url: str, token: str, accept: str, timeout: int) -> bytes:
    if not system.IS_WINDOWS:
        raise RuntimeError("GitHub's certificate couldn't be checked and PowerShell isn't available here.")
    fd, out = tempfile.mkstemp(prefix="pf-gh-", suffix=".bin")
    os.close(fd)
    env = dict(os.environ, PF_GH_TOKEN=token, PF_GH_URL=url, PF_GH_ACCEPT=accept, PF_GH_OUT=out,
               PF_GH_TIMEOUT=str(timeout))
    try:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", PS_SCRIPT],
                           env=env, capture_output=True, text=True, timeout=timeout + 30)
        if r.returncode != 0:
            raise RuntimeError("PowerShell download failed: " + _scrub((r.stderr or r.stdout).strip(), token))
        return Path(out).read_bytes()
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


def _get(url: str, token: str, accept: str = "application/vnd.github+json", timeout: int = 30) -> bytes:
    try:
        return _urllib_get(url, token, accept, timeout)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise RuntimeError("GitHub refused the token. Check it has read-only Contents access to "
                               f"{REPO} and hasn't expired.") from None
        if e.code == 404:
            raise RuntimeError(f"GitHub can't find {REPO} with this token. Give it access to that repository.") from None
        raise RuntimeError(f"GitHub answered {e.code}.") from None
    except (urllib.error.URLError, ssl.SSLError, OSError) as e:
        if not _is_cert_error(e):
            raise RuntimeError("Couldn't reach GitHub: " + _scrub(getattr(e, "reason", e), token)) from None
        log.info("GitHub certificate check failed in Python, trying PowerShell")
    return _powershell_get(url, token, accept, timeout)


# ------------------------------------------------------------ installed vs latest

def installed_commit() -> str:
    try:
        return (DATA_DIR / COMMIT_FILE_NAME).read_text(encoding="utf-8").strip()[:40]
    except OSError:
        return ""


def last_check() -> dict | None:
    return _last["check"]


def last_error() -> str:
    return _last["error"]


def check() -> dict:
    """Latest commit on main: {sha, short, date, message, version, installed, installed_version, new}."""
    token = _token()
    if not token:
        raise RuntimeError("Save a GitHub token first.")
    try:
        data = json.loads(_get(f"{API}/repos/{REPO}/commits/{BRANCH}", token))
        sha = data["sha"]
        c = data.get("commit") or {}
        version = ""
        try:
            src = _get(f"{API}/repos/{REPO}/contents/server/system.py?ref={sha}", token,
                       accept="application/vnd.github.raw")
            m = re.search(rb'VERSION = "([^"]+)"', src)
            version = m.group(1).decode() if m else ""
        except Exception:  # noqa: BLE001  (the version is a nice-to-have)
            pass
    except Exception as e:
        _last.update(error=_scrub(e, token), at=time.time())
        raise RuntimeError(_last["error"]) from None
    inst = installed_commit()
    out = {"sha": sha, "short": sha[:7], "date": (c.get("committer") or c.get("author") or {}).get("date", ""),
           "message": (c.get("message") or "").strip().splitlines()[0][:200] if c.get("message") else "",
           "version": version, "installed": inst, "installed_version": system.VERSION,
           "new": sha != inst}
    _last.update(check=out, error="", at=time.time())
    return out


# ------------------------------------------------------------ install

def reroot(zip_bytes: bytes) -> bytes:
    """GitHub's zipball has one top folder named after the repo and commit. Rebuild it with pipsfarmer/
    as the top folder (wherever server/app.py sits), leaving out caches."""
    try:
        z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        raise ValueError("GitHub didn't send a zip file.") from None
    names = z.namelist()
    apps = sorted((n for n in names if n.endswith("server/app.py")), key=len)
    if not apps:
        raise ValueError("That GitHub download isn't a PipsFarmer package (no server/app.py).")
    prefix = apps[0][: -len("server/app.py")]
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as w:
        for info in z.infolist():
            n = info.filename
            if not n.startswith(prefix) or n.endswith("/"):
                continue
            rel = n[len(prefix):]
            parts = rel.split("/")
            if not rel or SKIP_PARTS & set(parts) or rel.endswith(".pyc"):
                continue
            w.writestr("pipsfarmer/" + rel, z.read(info))
    return out.getvalue()


def stage(sha: str | None = None) -> tuple[Path, str]:
    """Downloads `sha` (default: the latest on main), stages it like an uploaded zip. Returns (folder, sha)."""
    token = _token()
    if not token:
        raise RuntimeError("Save a GitHub token first.")
    sha = sha or check()["sha"]
    if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
        raise ValueError("Bad commit id.")
    try:
        raw = _get(f"{API}/repos/{REPO}/zipball/{sha}", token, timeout=180)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(_scrub(e, token)) from None
    root = system.stage_update(reroot(raw))
    (root / MARKER).write_text(sha + "\n", encoding="utf-8")
    return root, sha


def install(sha: str | None = None, who: str = "") -> str:
    """Download, back up, and start the SYSTEM-task update. Returns the sha being installed."""
    if not _busy.acquire(blocking=False):
        raise RuntimeError("An update from GitHub is already starting.")
    try:
        root, sha = stage(sha)
        system.backup_db("pre-update", keep=10)
        system.run_update(root)
        log.info("GitHub update %s started by %s", sha[:7], who or "?")
        return sha
    finally:
        _busy.release()


def describe(c: dict) -> str:
    """Plain-text summary of a check, for the bot."""
    inst = c["installed"][:7] if c["installed"] else "unknown (installed from a zip)"
    lines = [f"Installed: v{c['installed_version']} · commit {inst}",
             f"Latest on {BRANCH}: {('v' + c['version'] + ' · ') if c['version'] else ''}commit {c['short']}"
             + (f" · {c['date'][:16].replace('T', ' ')} UTC" if c["date"] else "")]
    if c["message"]:
        lines.append(f"“{c['message']}”")
    lines.append("A newer version is ready to install." if c["new"] else "You're up to date.")
    return "\n".join(lines)

"""Running PipsFarmer without anyone on the VPS: backups, updates from the website,
the Cloudflare tunnel, logs, and the node package other VPSes update themselves from."""
from __future__ import annotations

import gzip
import hashlib
import io
import logging
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.request
import zipfile
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import config

VERSION = "1.3.33"
ROOT = config.ROOT
IS_WINDOWS = os.name == "nt"
BACKUP_DIR = ROOT / "backups"
UPDATE_DIR = ROOT / "updates"
LOG_DIR = ROOT / "logs"
BIN_DIR = ROOT / "bin"
SERVERS_FILE = ROOT / "data" / "mt5-servers.dat"   # broker list shared by every VPS
log = logging.getLogger("pf.system")

# files every VPS node runs; nodes compare this hash with their own and update themselves
NODE_FILES = ("__init__.py", "node_agent.py", "worker.py", "trade_logic.py", "requirements-node.txt")


# ------------------------------------------------------------ logs

def setup_file_logging() -> None:
    try:
        LOG_DIR.mkdir(exist_ok=True)
        h = RotatingFileHandler(LOG_DIR / "server.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        logging.getLogger().addHandler(h)
    except OSError:
        pass


def tail(path: Path, lines: int = 120) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 64_000))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return "\n".join(data.splitlines()[-lines:])


# ------------------------------------------------------------ node package

def node_hash(folder: Path) -> str:
    h = hashlib.sha256()
    for name in NODE_FILES:
        p = folder / name
        h.update(name.encode() + b"\0")
        h.update(p.read_bytes() if p.exists() else b"")
        h.update(b"\0")
    return h.hexdigest()[:16]


_pkg: dict = {"at": 0.0}


def node_package() -> tuple[bytes, str]:
    """Zip of the node files this website was installed with, and their hash."""
    folder = ROOT / "node"
    if time.time() - _pkg["at"] > 300:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name in NODE_FILES:
                if (folder / name).exists():
                    z.write(folder / name, name)
        _pkg.update(at=time.time(), data=buf.getvalue(), hash=node_hash(folder))
    return _pkg["data"], _pkg["hash"]


# ------------------------------------------------------------ backups

def backup_db(tag: str = "auto", keep: int = 14) -> Path:
    """Consistent copy of the live database (safe while it's in use)."""
    BACKUP_DIR.mkdir(exist_ok=True)
    dest = BACKUP_DIR / f"pipsfarmer-{tag}-{datetime.now():%Y%m%d-%H%M}.db"
    src = sqlite3.connect(config.DB_PATH)
    try:
        out = sqlite3.connect(dest)
        with out:
            src.backup(out)
        out.close()
    finally:
        src.close()
    olds = sorted(BACKUP_DIR.glob(f"pipsfarmer-{tag}-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in olds[keep:]:
        old.unlink(missing_ok=True)
    return dest


def gz(path: Path) -> bytes:
    return gzip.compress(path.read_bytes(), 6)


def keys_backup_text() -> str:
    return (f"PF_SECRET_KEY={config.SECRET_KEY}\nPF_FERNET_KEY={config.FERNET_KEY}\n"
            "Keep this safe. With a database backup, these two lines restore every customer "
            "(saved MT5 passwords only unlock with PF_FERNET_KEY).\n")


# ------------------------------------------------------------ updates from the website

def find_package_root(folder: Path) -> Path | None:
    for p in [folder, *folder.glob("*"), *folder.glob("*/*")]:
        if p.is_dir() and (p / "server" / "app.py").exists() and (p / "update_vps.ps1").exists():
            return p
    return None


def stage_update(zip_bytes: bytes) -> Path:
    """Unpack an uploaded pipsfarmer zip and check it looks right. Returns the folder to update from."""
    try:
        z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        raise ValueError("That isn't a zip file.") from None
    for n in z.namelist():
        if n.startswith("/") or ".." in Path(n).parts or ":" in n:
            raise ValueError("The zip has unsafe file paths.")
    UPDATE_DIR.mkdir(exist_ok=True)
    dest = UPDATE_DIR / datetime.now().strftime("%Y%m%d-%H%M%S")
    z.extractall(dest)
    root = find_package_root(dest)
    if not root:
        shutil.rmtree(dest, ignore_errors=True)
        raise ValueError("That zip isn't a PipsFarmer update (no server/app.py and update_vps.ps1 in it).")
    olds = sorted([p for p in UPDATE_DIR.iterdir() if p.is_dir()], key=lambda p: p.name, reverse=True)
    for old in olds[3:]:
        shutil.rmtree(old, ignore_errors=True)
    return root


def run_update(root: Path) -> None:
    """Runs update_vps.ps1 from `root` as a separate Windows task, so it survives this website stopping."""
    if not IS_WINDOWS:
        raise RuntimeError("Updates from the website only run on the Windows VPS.")
    script = root / "update_vps.ps1"
    tr = f'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{script}" -Unattended'
    subprocess.run(["schtasks", "/create", "/tn", "PipsFarmer Updater", "/tr", tr, "/sc", "once", "/st", "23:59",
                    "/ru", "SYSTEM", "/rl", "HIGHEST", "/f"], check=True, capture_output=True, timeout=60)
    subprocess.run(["schtasks", "/run", "/tn", "PipsFarmer Updater"], check=True, capture_output=True, timeout=60)


def update_log() -> str:
    return tail(UPDATE_DIR / "last-update.log", 60)


# ------------------------------------------------------------ Cloudflare tunnel

CLOUDFLARED_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
_tunnel = {"state": "", "busy": False}


def tunnel_status() -> str:
    if _tunnel["busy"] or not IS_WINDOWS:
        return _tunnel["state"] or ("only available on the Windows VPS" if not IS_WINDOWS else "")
    try:
        out = subprocess.run(["sc", "query", "cloudflared"], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return _tunnel["state"]
    if "RUNNING" in out:
        return "running"
    if "STOPPED" in out:
        return "installed but stopped"
    return _tunnel["state"] or "not installed"


def install_tunnel(token: str) -> None:
    """Installs cloudflared as a Windows service with the token from the Cloudflare dashboard (background)."""
    if not IS_WINDOWS:
        raise RuntimeError("The tunnel can only be installed on the Windows VPS.")
    if _tunnel["busy"]:
        raise RuntimeError("Already installing, give it a minute.")

    def work():
        _tunnel.update(busy=True, state="downloading cloudflared")
        try:
            BIN_DIR.mkdir(exist_ok=True)
            exe = BIN_DIR / "cloudflared.exe"
            tmp = exe.with_suffix(".tmp")
            urllib.request.urlretrieve(CLOUDFLARED_URL, tmp)
            _tunnel["state"] = "installing the service"
            subprocess.run([str(exe if exe.exists() else tmp), "service", "uninstall"], capture_output=True, timeout=120)
            subprocess.run(["sc", "stop", "cloudflared"], capture_output=True, timeout=60)
            time.sleep(3)
            os.replace(tmp, exe)
            r = subprocess.run([str(exe), "service", "install", token], capture_output=True, text=True, timeout=180)
            if r.returncode != 0:
                raise RuntimeError((r.stderr or r.stdout).strip()[-300:])
            subprocess.run(["sc", "failure", "cloudflared", "reset=", "86400", "actions=",
                            "restart/5000/restart/10000/restart/30000"], capture_output=True, timeout=60)
            _tunnel["state"] = "running"
        except Exception as e:  # noqa: BLE001
            _tunnel["state"] = f"failed: {e}"
            log.exception("tunnel install")
        finally:
            _tunnel["busy"] = False

    threading.Thread(target=work, daemon=True).start()


# ------------------------------------------------------------ keeping this VPS's MT5 manager alive
NODE_TASK = "PipsFarmer MT5 Node"


def _node_processes():
    import psutil
    out = []
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            cmd = " ".join(proc.info.get("cmdline") or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if "node_agent.py" in cmd:
            out.append(proc)
    return out


def restart_local_node(wait: float = 15) -> str:
    """The MT5 manager on this VPS stopped checking in. Stop a stuck copy (its start loop brings it back),
    and if nothing comes back, start it again with its scheduled task. Returns what was done."""
    if not IS_WINDOWS:
        return "not on Windows"
    import time as _t
    stuck = _node_processes()
    for proc in stuck:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
    _t.sleep(wait)
    if _node_processes():
        return "restarted the stuck MT5 manager" if stuck else "the MT5 manager was already starting"
    res = subprocess.run(["schtasks", "/run", "/tn", NODE_TASK], capture_output=True, text=True, timeout=60)
    if res.returncode == 0:
        return "started the MT5 manager again" + (" (it had stopped)" if not stuck else "")
    return "couldn't start the MT5 manager: " + (res.stderr or res.stdout).strip()[:200]



def _task_exists(name: str) -> bool | None:
    if not IS_WINDOWS:
        return None
    try:
        return subprocess.run(["schtasks", "/query", "/tn", name], capture_output=True, timeout=20).returncode == 0
    except Exception:  # noqa: BLE001
        return None


def restart_protection(autologon: bool | None) -> list[tuple[str, bool | None, str]]:
    """What brings this VPS back by itself after a restart or a crash: (check, ok, what it means)."""
    items: list[tuple[str, bool | None, str]] = [
        ("Website starts with Windows", _task_exists("PipsFarmer Website"), "no login needed"),
        ("Windows logs in by itself", autologon, "MT5 needs a logged-in desktop; turn it on in Admin → Servers"),
        ("MT5 manager starts after login", _task_exists(NODE_TASK), "then it starts every customer's MT5"),
    ]
    hidden = None
    try:
        hidden = "PF_NODE_HIDDEN" in (ROOT / "node" / "start_node.bat").read_text(errors="ignore")
    except OSError:
        pass
    items.append(("MT5 manager runs hidden", hidden if IS_WINDOWS else None, "no window to close by accident"))
    items.append(("Watchdog every 5 minutes", _task_exists("PipsFarmer Watchdog"),
                  "restarts the website, MT5 manager or tunnel if one stops"))
    tunnel = None
    if IS_WINDOWS:
        try:
            out = subprocess.run(["sc", "qc", "cloudflared"], capture_output=True, text=True, timeout=15).stdout
            tunnel = "AUTO_START" in out if out else False
        except Exception:  # noqa: BLE001
            tunnel = None
    items.append(("Tunnel starts with Windows", tunnel, "keeps pipsfarmer.com reachable"))
    wu = None
    if IS_WINDOWS:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU") as k:
                wu = winreg.QueryValueEx(k, "NoAutoRebootWithLoggedOnUsers")[0] == 1
        except OSError:
            wu = False
    items.append(("No Windows Update restarts in the week", wu, "PipsFarmer restarts for updates on Saturday"))
    items.append(("Desktop stays live after you disconnect", _task_exists("PipsFarmer Keep Screen On"),
                  "MT5 keeps running when you close Remote Desktop"))
    return items

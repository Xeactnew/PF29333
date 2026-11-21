"""PipsFarmer node agent: runs on each Windows VPS.

Every 10 seconds it checks in with the website, gets the list of customer accounts
assigned to this VPS, and keeps one portable MT5 + one worker process running per account.

Setup: fill in node.json next to this file, then run start_node.bat.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERSION = "1.3.30"
NODE_FILES = ("__init__.py", "node_agent.py", "worker.py", "trade_logic.py", "requirements-node.txt")
RESTART_CODE = 0          # start_node.bat starts us again 10 s after we exit

try:
    import psutil
except ImportError:  # optional, used for stats and killing stray terminals
    psutil = None

log = logging.getLogger("pf.node")

# folders in the MT5 template that don't need copying for each account
SKIP_COPY = {"logs", "Tester", "bases", "Bases",
             # saved logins: never copy one customer's (or your own) login into another customer's MT5
             "accounts.dat", "accounts.ini"}
REMOVED_FOLDER_GRACE = 3600   # delete a removed account's MT5 folder (it may hold a saved login) after 1 hour
BASE_STAMP = ".pipsfarmer_base"
SHORTCUT_NAME = "PipsFarmer - add a broker.lnk"


def servers_file(folder: Path) -> Path:
    return folder / "Config" / "servers.dat"


def file_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def lock_folder(folder: Path) -> bool:
    """Only SYSTEM and Administrators can open the folder (it may hold a saved MT5 login).
    Windows only; skipped when the node isn't elevated, because it would lock the node out."""
    if os.name != "nt" or not is_elevated():
        return False
    r = subprocess.run(["icacls", str(folder), "/inheritance:r", "/grant:r",
                        "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"],
                       capture_output=True, timeout=120)
    return r.returncode == 0


def node_hash(folder: Path) -> str:
    """Must match server/system.py node_hash."""
    h = hashlib.sha256()
    for name in NODE_FILES:
        p = folder / name
        h.update(name.encode() + b"\0")
        h.update(p.read_bytes() if p.exists() else b"")
        h.update(b"\0")
    return h.hexdigest()[:16]


# ------------------------------------------------------------ Windows helpers (no-ops elsewhere)

def _winreg():
    try:
        import winreg
        return winreg
    except ImportError:
        return None


WINLOGON = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"


def autologon_on() -> bool | None:
    wr = _winreg()
    if not wr:
        return None
    try:
        with wr.OpenKey(wr.HKEY_LOCAL_MACHINE, WINLOGON) as k:
            return str(wr.QueryValueEx(k, "AutoAdminLogon")[0]) == "1"
    except OSError:
        return False


def reboot_pending() -> bool | None:
    wr = _winreg()
    if not wr:
        return None
    for path in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired",
                 r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending"):
        try:
            wr.OpenKey(wr.HKEY_LOCAL_MACHINE, path).Close()
            return True
        except OSError:
            pass
    return False


def is_elevated() -> bool | None:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (ImportError, AttributeError, OSError):
        return None


def no_weekday_reboots() -> None:
    """Windows still installs updates, but won't restart while we're logged in.
    The website asks us to restart on Saturday, when gold is closed."""
    wr = _winreg()
    if not wr:
        return
    try:
        k = wr.CreateKeyEx(wr.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU", 0,
                           wr.KEY_SET_VALUE)
        wr.SetValueEx(k, "NoAutoRebootWithLoggedOnUsers", 0, wr.REG_DWORD, 1)
        k.Close()
    except OSError as e:
        log.warning("couldn't set Windows Update restart policy: %s", e)


def set_autologon(user: str, domain: str, password: str) -> str:
    """Turns on automatic Windows login so MT5 comes back by itself after a restart."""
    if not _winreg():
        return "auto-login only works on Windows"
    try:
        import tempfile
        import zipfile
        tmp = Path(tempfile.mkdtemp())
        urllib.request.urlretrieve("https://download.sysinternals.com/files/AutoLogon.zip", tmp / "a.zip")
        zipfile.ZipFile(tmp / "a.zip").extractall(tmp)
        r = subprocess.run([str(tmp / "Autologon64.exe"), user, domain, password, "/accepteula"],
                           capture_output=True, timeout=60)
        shutil.rmtree(tmp, ignore_errors=True)
        if r.returncode == 0 and autologon_on():
            return "auto-login turned on"
    except Exception as e:  # noqa: BLE001
        log.warning("Autologon tool failed (%s), using the registry", e)
    wr = _winreg()
    try:
        with wr.OpenKey(wr.HKEY_LOCAL_MACHINE, WINLOGON, 0, wr.KEY_SET_VALUE) as k:
            for name, val in (("AutoAdminLogon", "1"), ("DefaultUserName", user), ("DefaultDomainName", domain),
                              ("DefaultPassword", password)):
                wr.SetValueEx(k, name, 0, wr.REG_SZ, val)
        return "auto-login turned on"
    except OSError as e:
        return f"couldn't turn on auto-login: {e} (is the node running as administrator?)"


def uptime_hours() -> float | None:
    if psutil:
        return round((time.time() - psutil.boot_time()) / 3600, 1)
    return None


def load_config() -> dict:
    cfg = json.loads((HERE / "node.json").read_text(encoding="utf-8"))
    cfg.setdefault("accounts_dir", str(HERE / "accounts"))
    cfg.setdefault("python", sys.executable)
    cfg.setdefault("poll_seconds", 10)
    return cfg


class Node:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.server = cfg["server_url"].rstrip("/")
        self.token = cfg["node_token"]
        self.template = Path(cfg["mt5_template"])
        self.accounts_dir = Path(cfg["accounts_dir"])
        self.accounts_dir.mkdir(parents=True, exist_ok=True)
        self.locked = lock_folder(self.accounts_dir)
        # a ready-to-use MT5 (examples already compiled, updates done) that new accounts are copied from.
        # Its Config/servers.dat is this VPS's broker list, shared by every account and every VPS.
        self.base = Path(cfg.get("mt5_base") or self.accounts_dir.parent / "mt5-base")
        self.base_thread: threading.Thread | None = None
        self.last_servers_check = 0.0
        self.servers_sync_tried: dict[str, float] = {}
        self.last_wanted: set[int] | None = None
        self.procs: dict[int, dict] = {}      # account_id -> {proc, creds, started, fails}
        self.stopped_at: dict[int, float] = {}
        self.last_result = ""
        self.last_housekeeping = 0.0
        self.update_tried: dict[str, float] = {}
        self.exit_code: int | None = None
        if not (self.template / "terminal64.exe").exists():
            raise SystemExit(f"terminal64.exe not found in {self.template}. Set mt5_template in node.json "
                             f"to your MetaTrader 5 install folder.")

    # ------------------------------------------------------------ server
    def heartbeat(self) -> dict:
        info = {"version": VERSION, "hostname": os.environ.get("COMPUTERNAME", ""), "running": sorted(self.procs),
                "node_hash": node_hash(HERE), "autologon": autologon_on(), "reboot_pending": reboot_pending(),
                "uptime_h": uptime_hours(), "elevated": is_elevated(), "last_result": self.last_result,
                "log_tail": self.log_tail(), "folders_locked": self.locked,
                "template_has_login": any((self.template / "Config" / n).exists() for n in ("accounts.dat", "accounts.ini")),
                "base_ready": self.base_ready(), "servers_size": file_size(self.library),
                "servers_sha": self.library_sha()}
        if psutil:
            info["cpu"] = round(psutil.cpu_percent(), 0)
            vm = psutil.virtual_memory()
            info["ram"] = round(vm.percent, 0)
            info["ram_total_gb"] = round(vm.total / 1e9, 1)
        info["disk_free_gb"] = round(shutil.disk_usage(self.accounts_dir).free / 1e9, 1)
        req = urllib.request.Request(self.server + "/api/node/heartbeat", data=json.dumps(info).encode(),
                                     method="POST", headers={"Content-Type": "application/json",
                                                             "Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())

    @staticmethod
    def log_tail(lines: int = 25) -> str:
        try:
            with open(HERE / "node.log", "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 6000))
                return "\n".join(f.read().decode("utf-8", "replace").splitlines()[-lines:])
        except OSError:
            return ""

    # ------------------------------------------------------------ remote control
    def handle_commands(self, commands: list[dict]) -> None:
        for c in commands:
            kind = c.get("kind")
            log.info("command from website: %s", kind)
            if kind == "autologon":
                self.last_result = set_autologon(c.get("user") or os.environ.get("USERNAME", ""),
                                                 c.get("domain") or os.environ.get("USERDOMAIN", ""),
                                                 c.get("password", ""))
            elif kind == "reboot":
                self.last_result = "restarting Windows in 60 seconds"
                self.stop_all()
                subprocess.run(["shutdown", "/r", "/t", "60", "/c", "PipsFarmer restart", "/d", "p:4:1"],
                               capture_output=True)
            elif kind == "restart_node":
                self.last_result = "node restarted"
                self.exit_code = RESTART_CODE
            elif kind == "restart_all":
                self.stop_all()
                self.last_result = "all MT5 restarted"
            log.info("-> %s", self.last_result)

    def stop_all(self) -> None:
        for aid in list(self.procs):
            self.stop_worker(aid)

    def maybe_update(self, wanted: str | None) -> None:
        """The website tells us the hash of the node files it has. If ours differ, update and restart."""
        if not wanted or wanted == node_hash(HERE) or self.cfg.get("auto_update") is False:
            return
        if time.time() - self.update_tried.get(wanted, 0) < 3600:
            return
        self.update_tried[wanted] = time.time()
        log.info("website has new node files (%s), updating", wanted)
        req = urllib.request.Request(self.server + "/api/node/package",
                                     headers={"Authorization": f"Bearer {self.token}"})
        import io
        import zipfile
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        z = zipfile.ZipFile(io.BytesIO(data))
        names = [n for n in z.namelist() if n in NODE_FILES]
        old_req = (HERE / "requirements-node.txt").read_bytes() if (HERE / "requirements-node.txt").exists() else b""
        for n in names:
            tmp = HERE / (n + ".new")
            tmp.write_bytes(z.read(n))
            os.replace(tmp, HERE / n)
        if (HERE / "requirements-node.txt").read_bytes() != old_req:
            subprocess.run([self.cfg["python"], "-m", "pip", "install", "-r", str(HERE / "requirements-node.txt"),
                            "--quiet", "--disable-pip-version-check"], timeout=600)
        self.last_result = f"updated node files to {node_hash(HERE)}"
        log.info(self.last_result + ", restarting")
        self.stop_all()
        self.exit_code = RESTART_CODE

    # ------------------------------------------------------------ housekeeping
    def kill_strays(self) -> None:
        """Workers left over from before this node started would trade twice; stop them."""
        if not psutil:
            return
        me = os.getpid()
        worker = str(HERE / "worker.py").lower()
        for p in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmd = " ".join(p.info["cmdline"] or []).lower()
                if p.info["pid"] != me and worker in cmd:
                    log.info("stopping leftover worker pid %s", p.info["pid"])
                    p.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    def housekeeping(self) -> None:
        """Hourly: keep logs from filling the disk."""
        if time.time() - self.last_housekeeping < 3600:
            return
        self.last_housekeeping = time.time()
        self.remove_orphan_folders()
        cutoff = time.time() - 14 * 86400
        for folder in self.accounts_dir.iterdir() if self.accounts_dir.exists() else []:
            wl = folder / "pipsfarmer_worker.log"
            try:
                if wl.exists() and wl.stat().st_size > 5_000_000:
                    os.replace(wl, folder / "pipsfarmer_worker.old.log")
            except OSError:
                pass                     # in use; next hour
            for logs in (folder / "logs", folder / "MQL5" / "Logs"):
                for f in logs.glob("*.log") if logs.exists() else []:
                    try:
                        if f.stat().st_mtime < cutoff:
                            f.unlink()
                    except OSError:
                        pass

    # ------------------------------------------------------------ terminals
    def account_folder(self, account_id: int) -> Path:
        return self.accounts_dir / str(account_id)

    def prepare_terminal(self, account_id: int) -> Path:
        dst = self.account_folder(account_id)
        exe = dst / "terminal64.exe"
        if not exe.exists():
            src = self.copy_source()
            log.info("copying MT5 for account %s from %s", account_id, src)
            shutil.copytree(src, dst, dirs_exist_ok=True,
                            ignore=lambda d, names: [n for n in names if n in SKIP_COPY or n == BASE_STAMP])
            lock_folder(dst)
        # give it the biggest broker list we have, but keep one this terminal found itself if it's bigger
        src_servers = max((self.library, servers_file(self.template)), key=file_size)
        dst_servers = servers_file(dst)
        if file_size(src_servers) > file_size(dst_servers):
            dst_servers.parent.mkdir(exist_ok=True)
            shutil.copy2(src_servers, dst_servers)
        self.enable_algo_trading(dst)
        return exe

    @staticmethod
    def enable_algo_trading(folder: Path) -> None:
        import trade_logic
        trade_logic.enable_algo_trading(folder)

    def kill_terminal(self, account_id: int) -> None:
        if not psutil:
            return
        folder = str(self.account_folder(account_id)).lower()
        for p in psutil.process_iter(["exe", "name"]):
            try:
                if p.info["exe"] and p.info["exe"].lower().startswith(folder):
                    p.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    # ------------------------------------------------------------ ready-made MT5 + shared broker list
    @property
    def library(self) -> Path:
        return servers_file(self.base)

    def library_sha(self) -> str:
        try:
            return hashlib.sha256(self.library.read_bytes()).hexdigest()[:16]
        except OSError:
            return ""

    def template_stamp(self) -> str:
        st = (self.template / "terminal64.exe").stat()
        return f"{st.st_size}-{int(st.st_mtime)}"

    def base_ready(self) -> bool:
        try:
            return (self.base / BASE_STAMP).read_text().strip() == self.template_stamp()
        except OSError:
            return False

    def copy_source(self) -> Path:
        return self.base if self.base_ready() else self.template

    def launch_process(self, args: list[str]):
        """Separate method so tests can replace it."""
        return subprocess.Popen(args, cwd=str(Path(args[0]).parent))

    def kill_under(self, folder: Path) -> None:
        if not psutil:
            return
        prefix = str(folder).lower()
        for p in psutil.process_iter(["exe"]):
            try:
                if p.info["exe"] and p.info["exe"].lower().startswith(prefix):
                    p.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    def build_base(self, wait: float = 420, quiet: float = 25, step: float = 5) -> bool:
        """Start MT5 once in a spare copy so it unpacks and compiles its 450 example files and installs
        any update. New accounts are copied from it and connect in seconds instead of minutes."""
        tmp = self.base.with_name(self.base.name + "-building")
        try:
            shutil.rmtree(tmp, ignore_errors=True)
            log.info("preparing a ready-made MT5 in %s (takes a few minutes, once)", self.base)
            shutil.copytree(self.template, tmp, ignore=lambda d, names: [n for n in names if n in SKIP_COPY])
            if file_size(self.library) > file_size(servers_file(tmp)):
                servers_file(tmp).parent.mkdir(exist_ok=True)
                shutil.copy2(self.library, servers_file(tmp))
            proc = self.launch_process([str(tmp / "terminal64.exe"), "/portable"])
            start, count, changed = time.time(), -1, time.time()
            while time.time() - start < wait:
                time.sleep(step)
                mql = tmp / "MQL5"
                n = sum(1 for _ in mql.rglob("*.ex5")) if mql.exists() else 0
                if n != count:
                    count, changed = n, time.time()
                elif n > 0 and time.time() - changed >= quiet:
                    break
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            self.kill_under(tmp)
            time.sleep(step)
            for junk in ("logs", "Tester", "Bases", "bases"):
                shutil.rmtree(tmp / junk, ignore_errors=True)
            for name in ("accounts.dat", "accounts.ini"):
                (tmp / "Config" / name).unlink(missing_ok=True)
            # keep the broker list if it grew while we were building
            if file_size(self.library) > file_size(servers_file(tmp)):
                shutil.copy2(self.library, servers_file(tmp))
            if self.base.exists():
                self.kill_under(self.base)
                shutil.rmtree(self.base)
            os.replace(tmp, self.base)
            (self.base / BASE_STAMP).write_text(self.template_stamp())
            log.info("ready-made MT5 done: %s compiled files", count)
            self.make_shortcut()
            return True
        except Exception:  # noqa: BLE001
            log.exception("couldn't prepare the ready-made MT5; new accounts are copied from the template")
            shutil.rmtree(tmp, ignore_errors=True)
            return False

    def ensure_base(self) -> None:
        if self.base_ready():
            try:
                self.make_shortcut()
            except Exception:  # noqa: BLE001
                pass
            return
        if self.base_thread and self.base_thread.is_alive():
            return
        if self.cfg.get("prepare_base") is False:
            return
        self.base_thread = threading.Thread(target=self.build_base, daemon=True)
        self.base_thread.start()

    def make_shortcut(self) -> None:
        """Desktop shortcut that opens the ready-made MT5, to teach every VPS a new broker in one go."""
        if os.name != "nt":
            return
        lnk = Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop" / SHORTCUT_NAME
        if lnk.exists():
            return
        exe = self.base / "terminal64.exe"
        ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');$s.TargetPath='{exe}';"
              "$s.Arguments='/portable';$s.WorkingDirectory='{wd}';$s.Save()").format(
            lnk=str(lnk).replace("'", "''"), exe=str(exe).replace("'", "''"), wd=str(self.base).replace("'", "''"))
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                       capture_output=True, timeout=60)

    def harvest_servers(self) -> bool:
        """When any MT5 on this VPS has learned more brokers than the shared list, keep its list."""
        best = file_size(self.library)
        candidates = [servers_file(self.template)]
        if self.accounts_dir.exists():
            candidates += [servers_file(f) for f in self.accounts_dir.iterdir() if f.is_dir() and f.name.isdigit()]
        grew = False
        for c in candidates:
            size = file_size(c)
            if size > best:
                try:
                    self.library.parent.mkdir(parents=True, exist_ok=True)
                    tmp = self.library.with_suffix(".new")
                    shutil.copyfile(c, tmp)
                    os.replace(tmp, self.library)
                    log.info("broker list grew to %s KB (from %s)", size // 1024, c.parent.parent.name)
                    best, grew = size, True
                except OSError:
                    pass
        return grew

    def sync_servers(self, web: dict | None) -> None:
        """Share the broker list between VPSes through the website: the bigger list wins."""
        if not web:
            return
        mine, theirs = file_size(self.library), int(web.get("size") or 0)
        key = f"{mine}-{theirs}-{web.get('sha')}"
        if mine == theirs or time.time() - self.servers_sync_tried.get(key, 0) < 3600:
            return
        self.servers_sync_tried[key] = time.time()
        url = self.server + "/api/node/servers"
        headers = {"Authorization": f"Bearer {self.token}"}
        if theirs > mine:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
                data = r.read()
            if len(data) > mine:
                self.library.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.library.with_suffix(".new")
                tmp.write_bytes(data)
                os.replace(tmp, self.library)
                log.info("got a bigger broker list from the website (%s KB)", len(data) // 1024)
        else:
            req = urllib.request.Request(url, data=self.library.read_bytes(), method="POST",
                                         headers={**headers, "Content-Type": "application/octet-stream"})
            with urllib.request.urlopen(req, timeout=60):
                pass
            log.info("sent our broker list to the website (%s KB)", mine // 1024)

    def servers_tick(self, web: dict | None) -> None:
        if time.time() - self.last_servers_check < 120:
            return
        self.last_servers_check = time.time()
        self.harvest_servers()
        try:
            self.sync_servers(web)
        except Exception as e:  # noqa: BLE001
            log.warning("broker list sync: %s", e)

    # ------------------------------------------------------------ workers
    def start_worker(self, a: dict, creds: str) -> None:
        aid = a["account_id"]
        exe = self.prepare_terminal(aid)
        env = dict(os.environ, PF_MT5_LOGIN=str(a["login"]), PF_MT5_PASSWORD=a["password"],
                   PF_MT5_SERVER=a["server"], PF_SERVER_URL=self.server, PF_NODE_TOKEN=self.token,
                   PF_SERVERS_LIBRARY=str(self.library))
        logf = open(self.account_folder(aid) / "pipsfarmer_worker.log", "a", encoding="utf-8")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen([self.cfg["python"], str(HERE / "worker.py"), "--account-id", str(aid),
                                 "--terminal", str(exe)], env=env, stdout=logf, stderr=subprocess.STDOUT,
                                cwd=str(HERE), creationflags=flags)
        prev = self.procs.get(aid, {})
        self.procs[aid] = {"proc": proc, "creds": creds, "started": time.time(), "fails": prev.get("fails", 0),
                           "log": logf}
        self.stopped_at.pop(aid, None)
        log.info("started worker for account %s (pid %s)", aid, proc.pid)

    def stop_worker(self, aid: int) -> None:
        info = self.procs.pop(aid, None)
        if info:
            proc = info["proc"]
            proc.terminate()
            try:
                proc.wait(15)
            except subprocess.TimeoutExpired:
                proc.kill()
            info["log"].close()
        self.kill_terminal(aid)
        self.stopped_at[aid] = time.time()
        log.info("stopped account %s", aid)

    def reconcile(self, assignments: list[dict]) -> None:
        wanted = {a["account_id"]: a for a in assignments}
        self.last_wanted = set(wanted)
        for aid in list(self.procs):
            if aid not in wanted:
                self.stop_worker(aid)
        for aid, a in wanted.items():
            creds = hashlib.sha256(f"{a['login']}|{a['server']}|{a['password']}".encode()).hexdigest()
            info = self.procs.get(aid)
            if info and info["creds"] != creds:
                log.info("login details changed for %s, restarting", aid)
                self.stop_worker(aid)
                info = None
            if info and info["proc"].poll() is not None:
                code = info["proc"].returncode
                if "exited_at" not in info:
                    info["exited_at"] = time.time()
                    ran = info["exited_at"] - info["started"]
                    info["fails"] = 0 if ran > 600 or code == 3 else info["fails"] + 1
                    info["wait"] = 0 if code == 3 else min(300, 10 * 2 ** min(info["fails"], 5))
                if time.time() - info["exited_at"] < info["wait"]:
                    continue
                info["log"].close()
                self.kill_terminal(aid)
                log.info("worker %s exited (%s), restarting", aid, code)
                info = None
            if not info:
                try:
                    self.start_worker(a, creds)
                except Exception:  # noqa: BLE001
                    log.exception("couldn't start account %s", aid)
        # delete folders of removed/closed accounts (they may hold a saved login)
        for aid, t in list(self.stopped_at.items()):
            if time.time() - t > REMOVED_FOLDER_GRACE and aid not in wanted:
                shutil.rmtree(self.account_folder(aid), ignore_errors=True)
                self.stopped_at.pop(aid)
                log.info("deleted MT5 folder of removed account %s", aid)

    def remove_orphan_folders(self) -> None:
        """Folders left from accounts removed while the node was off."""
        if self.last_wanted is None or not self.accounts_dir.exists():
            return
        for folder in self.accounts_dir.iterdir():
            if not folder.is_dir() or not folder.name.isdigit():
                continue
            aid = int(folder.name)
            if aid in self.last_wanted or aid in self.procs or aid in self.stopped_at:
                continue
            try:
                age = time.time() - folder.stat().st_mtime
            except OSError:
                continue
            if age > REMOVED_FOLDER_GRACE:
                self.kill_terminal(aid)
                shutil.rmtree(folder, ignore_errors=True)
                log.info("deleted leftover MT5 folder %s", folder.name)

    def run(self) -> int:
        log.info("PipsFarmer node %s (%s) starting, server %s", VERSION, node_hash(HERE), self.server)
        self.kill_strays()
        no_weekday_reboots()
        self.ensure_base()
        while self.exit_code is None:
            try:
                resp = self.heartbeat()
                self.handle_commands(resp.get("commands") or [])
                if self.exit_code is None:
                    self.maybe_update(resp.get("node_hash"))
                if self.exit_code is None:
                    self.reconcile(resp.get("assignments", []))
                    self.servers_tick(resp.get("servers"))
            except urllib.error.HTTPError as e:
                log.error("server said %s: %s", e.code, "check node_token in node.json" if e.code == 401 else e.reason)
            except Exception as e:  # noqa: BLE001
                log.warning("can't reach server: %s", e)
            try:
                self.housekeeping()
            except Exception:  # noqa: BLE001
                log.exception("housekeeping")
            if self.exit_code is None:
                time.sleep(self.cfg["poll_seconds"])
        log.info("node exiting (%s)", self.exit_code)
        return self.exit_code


ALREADY_RUNNING = 3
_lock_handle = None


def single_instance() -> bool:
    """Only one MT5 manager per VPS: a second copy (for example started twice) quietly stops."""
    global _lock_handle
    path = HERE / "node.lock"
    try:
        _lock_handle = open(path, "a+")
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(_lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def main():
    handlers = [logging.StreamHandler(), RotatingFileHandler(HERE / "node.log", maxBytes=5_000_000, backupCount=3)]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", handlers=handlers)
    if not single_instance():
        log.info("another copy of the node agent is already running; this one stops")
        sys.exit(ALREADY_RUNNING)
    node = Node(load_config())
    try:
        sys.exit(node.run())
    except KeyboardInterrupt:
        node.stop_all()


if __name__ == "__main__":
    main()

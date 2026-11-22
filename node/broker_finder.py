"""Teaches this VPS's MT5 a broker it hasn't seen before, with nobody at the screen.

MT5 only connects to trade servers listed in its Config\\servers.dat. A new broker's servers are
added only when someone searches for it in File > Open an Account ("Find your broker"); there is
no command-line switch or API for it. So this module does that search itself, in a spare MT5 copy
with no account in it (it can't trade): it starts that MT5, opens the Open an Account window, types
the server or broker name, clicks Find, picks the first company and steps on once (which loads its
servers), cancels, and closes MT5 so it saves servers.dat. The node then shares the bigger list
with every account (Node.harvest_servers) and the account that was waiting connects by itself.

The window work uses plain Windows messages (ctypes, no extra package), not the real mouse or
keyboard, so it also works with the screen locked or nobody logged in over remote desktop. If MetaQuotes changes the
window, a run fails safely, writes what it saw to finder_dialog.txt next to that MT5, and the node
reports it so the admins get an alert.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("pf.node")

MARKER = "pf_find_server.txt"            # a worker writes this in its MT5's Config folder: "find this server"
SKIP = {"logs", "Tester", "bases", "Bases", "accounts.dat", "accounts.ini", ".pipsfarmer_base"}
RETRY_SECONDS = 600                      # between tries for the same server
GIVE_UP_FOR = 24 * 3600                  # after every query failed, wait a day before trying that server again
PREFETCH_GAP = 180                       # between looking up the brokers in the website's list
REPORT_DAYS = 2


def servers_file(folder: Path) -> Path:
    return folder / "Config" / "servers.dat"


def size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def queries_for(server: str, broker: str = "") -> list[str]:
    """What to type into MT5's broker search, best first: the exact server name (MT5 accepts it),
    the broker's name, then the part before the dash ("TradeQuo-Demo" -> "TradeQuo")."""
    out: list[str] = []

    def add(q: str) -> None:
        q = re.sub(r"\s+", " ", (q or "").strip())
        if len(q) >= 3 and q.lower() not in (x.lower() for x in out):
            out.append(q)

    add(server)
    add(broker)
    head = re.split(r"[-\s]", (server or "").strip(), maxsplit=1)[0]
    add(head)
    add(re.sub(r"(?i)(mt5|mt|live|demo|real|\d+)$", "", head))
    return out


# ---------------------------------------------------------------- Windows calls (ctypes, no extra package)

WM_SETTEXT, WM_GETTEXT, WM_COMMAND, WM_CLOSE, WM_KEYDOWN, WM_KEYUP = 0x000C, 0x000D, 0x0111, 0x0010, 0x0100, 0x0101
BN_CLICKED, IDCANCEL, VK_RETURN, VK_HOME = 0, 2, 0x0D, 0x24
LVM_GETITEMCOUNT = 0x1004
SMTO_ABORTIFHUNG = 0x0002


class Win:
    """The handful of Windows calls the driver needs. Messages, not the real mouse or keyboard, so it
    works with the screen locked. WM_SETTEXT/WM_GETTEXT are copied across processes by Windows."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.c, self.w = ctypes, wintypes
        self.u = ctypes.WinDLL("user32", use_last_error=True)
        self.u.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                               wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
        self.u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        for name in ("GetMenu", "GetSubMenu"):                    # handles, not ints, on 64-bit
            getattr(self.u, name).restype = wintypes.HMENU
        self.u.GetParent.restype = wintypes.HWND
        self.u.GetMenuItemID.restype = wintypes.UINT

    def _enum(self, parent: int | None) -> list[int]:
        out: list[int] = []
        proto = self.c.WINFUNCTYPE(self.w.BOOL, self.w.HWND, self.w.LPARAM)
        cb = proto(lambda h, _l: out.append(int(h)) or True)
        if parent is None:
            self.u.EnumWindows(cb, 0)
        else:
            self.u.EnumChildWindows(parent, cb, 0)
        return out

    def pid_of(self, h: int) -> int:
        pid = self.w.DWORD()
        self.u.GetWindowThreadProcessId(h, self.c.byref(pid))
        return pid.value

    def top_windows(self, pid: int) -> list[int]:
        return [h for h in self._enum(None) if self.pid_of(h) == pid and self.visible(h)]

    def children(self, h: int) -> list[int]:
        return self._enum(h)

    def cls(self, h: int) -> str:
        buf = self.c.create_unicode_buffer(256)
        self.u.GetClassNameW(h, buf, 256)
        return buf.value

    def text(self, h: int) -> str:
        buf = self.c.create_unicode_buffer(512)
        res = self.c.c_size_t()
        self.u.SendMessageTimeoutW(h, WM_GETTEXT, 512, self.c.addressof(buf), SMTO_ABORTIFHUNG, 2000,
                                   self.c.byref(res))
        return buf.value

    def visible(self, h: int) -> bool:
        return bool(self.u.IsWindowVisible(h))

    def enabled(self, h: int) -> bool:
        return bool(self.u.IsWindowEnabled(h))

    def ctrl_id(self, h: int) -> int:
        return int(self.u.GetDlgCtrlID(h))

    def parent(self, h: int) -> int:
        return int(self.u.GetParent(h) or 0)

    def area(self, h: int) -> int:
        r = self.w.RECT()
        self.u.GetWindowRect(h, self.c.byref(r))
        return max(0, r.right - r.left) * max(0, r.bottom - r.top)

    def set_text(self, h: int, value: str) -> None:
        buf = self.c.create_unicode_buffer(value)
        res = self.c.c_size_t()
        self.u.SendMessageTimeoutW(h, WM_SETTEXT, 0, self.c.addressof(buf), SMTO_ABORTIFHUNG, 3000, self.c.byref(res))

    def send(self, h: int, msg: int, wp: int = 0, lp: int = 0) -> int:
        res = self.c.c_size_t()
        self.u.SendMessageTimeoutW(h, msg, wp, lp, SMTO_ABORTIFHUNG, 3000, self.c.byref(res))
        return int(res.value)

    def post(self, h: int, msg: int, wp: int = 0, lp: int = 0) -> None:
        self.u.PostMessageW(h, msg, wp, lp)

    def menu_items(self, h: int) -> list[tuple[str, int]]:
        """Every command in the window's menu as (text, id). Empty if MT5 draws its own menu bar."""
        out: list[tuple[str, int]] = []

        def walk(menu, depth=0):
            for i in range(max(0, self.u.GetMenuItemCount(menu))):
                sub = self.u.GetSubMenu(menu, i)
                if sub and depth < 4:
                    walk(sub, depth + 1)
                    continue
                buf = self.c.create_unicode_buffer(256)
                self.u.GetMenuStringW(menu, i, buf, 256, 0x400)          # MF_BYPOSITION
                out.append((buf.value.replace("&", ""), int(self.u.GetMenuItemID(menu, i))))
        menu = self.u.GetMenu(h)
        if menu:
            walk(menu)
        return out


# ---------------------------------------------------------------- the window driver

class WindowDriver:
    """Starts an MT5, searches for a broker in File > Open an Account, then closes it cleanly."""

    START_WAIT = 120          # a ready-made MT5 shows its window in seconds; leave room for a slow VPS
    DIALOG_WAIT = 20
    SEARCH_WAIT = 25          # MT5 asks MetaQuotes; results usually arrive in a few seconds
    NEXT_WAIT = 12
    EXIT_WAIT = 45

    def __init__(self, win=None, launch=None, sleep=time.sleep, clock=time.time):
        self.win = win
        self.launch = launch or (lambda exe: subprocess.Popen([str(exe), "/portable"], cwd=str(Path(exe).parent)))
        self.sleep, self.clock = sleep, clock

    def run(self, exe: Path, query: str, dump: Path) -> dict:
        win = self.win = self.win or Win()
        steps: list[str] = []
        proc = self.launch(exe)
        main = None
        try:
            main = self._wait_main(proc.pid)
            steps.append("MT5 started")
            dlg = self._open_account_window(proc.pid, main, steps)
            edit = self._first(dlg, "Edit")
            if not edit:
                raise LookupError("no search box in the Open an Account window")
            win.set_text(edit, query)
            steps.append(f"typed {query!r}")
            btn = self._button(dlg, r"find")
            if btn:
                self._press(btn)
                steps.append("clicked Find")
            else:
                win.post(edit, WM_KEYDOWN, VK_RETURN, 0)
                win.post(edit, WM_KEYUP, VK_RETURN, 0)
                steps.append("pressed Enter (no Find button)")
            self.sleep(self.SEARCH_WAIT)
            listed = self._pick_first_company(dlg)
            if listed:
                steps.append(f"{listed} result(s), picked the first")
                nxt = self._button(dlg, r"next")
                if nxt:
                    self._press(nxt)
                    steps.append("clicked Next")
                    self.sleep(self.NEXT_WAIT)
            else:
                steps.append("no result list found")
            self._dump(proc.pid, dump, ok=True)
            cancel = self._button(dlg, r"cancel")
            if cancel:
                self._press(cancel)
            else:
                win.post(dlg, WM_COMMAND, IDCANCEL, 0)
            steps.append("cancelled")
            return {"searched": True, "detail": "; ".join(steps)}
        except Exception as e:  # noqa: BLE001
            log.warning("broker search for %r failed: %s", query, e)
            self._dump(proc.pid, dump, ok=False)
            return {"searched": False, "detail": ("; ".join(steps) + f"; failed: {type(e).__name__}: {e}")[:400]}
        finally:
            self._close(proc, main, steps)

    # ---- helpers
    def _wait_main(self, pid: int) -> int:
        end = self.clock() + self.START_WAIT
        while self.clock() < end:
            wins = [h for h in self.win.top_windows(pid) if self.win.cls(h) != "#32770"]
            if wins:
                return max(wins, key=self.win.area)
            self.sleep(2)
        raise TimeoutError("MT5's window didn't appear")

    def _dialog(self, pid: int, pattern: str) -> int | None:
        for h in self.win.top_windows(pid):
            if self.win.cls(h) == "#32770" and re.search(pattern, self.win.text(h) or "", re.I):
                return h
        return None

    def _open_account_window(self, pid: int, main: int, steps: list[str]) -> int:
        end = self.clock() + self.DIALOG_WAIT        # an MT5 with no account opens it by itself
        while self.clock() < end:
            dlg = self._dialog(pid, r"open an account")
            if dlg:
                steps.append("Open an Account opened by itself")
                return dlg
            login = self._dialog(pid, r"^login")
            if login:                                 # MT5 asked for a login instead: close that box
                self.win.post(login, WM_COMMAND, IDCANCEL, 0)
            self.sleep(1)
        cmd = next((cid for text, cid in self.win.menu_items(main) if re.search(r"open an account", text, re.I)), None)
        if cmd is None:
            raise LookupError("couldn't find File > Open an Account in MT5's menu")
        self.win.post(main, WM_COMMAND, cmd, 0)
        steps.append("used File > Open an Account")
        end = self.clock() + self.DIALOG_WAIT
        while self.clock() < end:
            dlg = self._dialog(pid, r"open an account")
            if dlg:
                return dlg
            self.sleep(1)
        raise TimeoutError("the Open an Account window didn't open")

    def _controls(self, dlg: int, cls: str) -> list[int]:
        return [h for h in self.win.children(dlg)
                if self.win.cls(h) == cls and self.win.visible(h) and self.win.enabled(h)]

    def _first(self, dlg: int, cls: str) -> int | None:
        found = self._controls(dlg, cls)
        return found[0] if found else None

    def _button(self, dlg: int, pattern: str) -> int | None:
        for h in self._controls(dlg, "Button"):
            if re.search(pattern, (self.win.text(h) or "").replace("&", ""), re.I):
                return h
        return None

    def _press(self, btn: int) -> None:
        """BN_CLICKED to the button's parent: no mouse needed, works with the screen locked."""
        cid = self.win.ctrl_id(btn)
        self.win.post(self.win.parent(btn), WM_COMMAND, (BN_CLICKED << 16) | (cid & 0xFFFF), btn)

    def _pick_first_company(self, dlg: int) -> int:
        """Select the first company in the results (Home key on the list). Returns how many there were."""
        for lv in self._controls(dlg, "SysListView32"):
            n = self.win.send(lv, LVM_GETITEMCOUNT)
            if n:
                self.win.post(lv, WM_KEYDOWN, VK_HOME, 0)
                self.win.post(lv, WM_KEYUP, VK_HOME, 0)
                return n
        return 0

    def _dump(self, pid: int, path: Path, ok: bool) -> None:
        """What MT5's windows looked like, so a changed window can be fixed from this file."""
        try:
            w = self.win
            lines = [f"{'ok' if ok else 'FAILED'} {time.strftime('%Y-%m-%d %H:%M:%S')}"]
            for top in w.top_windows(pid):
                lines.append(f"WINDOW {w.cls(top)!r} {w.text(top)!r} area={w.area(top)}")
                for text, cid in w.menu_items(top)[:80]:
                    lines.append(f"  menu {cid}: {text!r}")
                for h in w.children(top)[:400]:
                    lines.append(f"  {w.cls(h)!r} id={w.ctrl_id(h)} {'shown' if w.visible(h) else 'hidden'} "
                                 f"{'on' if w.enabled(h) else 'off'} {w.text(h)[:80]!r}")
            path.write_text("\n".join(lines), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            log.info("couldn't save the window dump: %s", e)

    def _close(self, proc, main: int | None, steps: list[str]) -> None:
        """Close MT5 normally (it saves servers.dat as it exits); stop it only if it won't go."""
        try:
            for h in self.win.top_windows(proc.pid) if self.win else []:
                if self.win.cls(h) == "#32770":
                    self.win.post(h, WM_COMMAND, IDCANCEL, 0)
            if main:
                self.win.post(main, WM_CLOSE, 0, 0)
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(self.EXIT_WAIT)
        except subprocess.TimeoutExpired:
            proc.kill()
            steps.append("MT5 had to be stopped")


# ---------------------------------------------------------------- what the node uses

class BrokerFinder:
    """A spare, account-less MT5 copy plus the record of what has been looked up."""

    def __init__(self, folder: Path, source: Callable[[], Path], library: Path, driver=None,
                 clock: Callable[[], float] = time.time):
        self.folder = Path(folder)
        self.source = source
        self.library = Path(library)
        self.driver = driver or WindowDriver()
        self.clock = clock
        self.state_path = self.folder.parent / "broker-finder.json"
        self.state = self._load()
        self.thread: threading.Thread | None = None
        self.last_prefetch = 0.0

    # ---- state
    def _load(self) -> dict:
        try:
            st = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            st = {}
        st.setdefault("servers", {})
        st.setdefault("prefetch", {})
        return st

    def _save(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".new")
            tmp.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    @property
    def busy(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    # ---- the spare MT5
    def prepare(self) -> Path:
        exe = self.folder / "terminal64.exe"
        if not exe.exists():
            src = self.source()
            log.info("copying MT5 for the broker finder from %s", src)
            shutil.copytree(src, self.folder, dirs_exist_ok=True, ignore=lambda d, names: [n for n in names if n in SKIP])
        for name in ("accounts.dat", "accounts.ini"):          # never an account in it: it can't trade
            (self.folder / "Config" / name).unlink(missing_ok=True)
        mine = servers_file(self.folder)
        if size(self.library) > size(mine):                    # start from everything this VPS knows
            mine.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.library, mine)
        return exe

    def search(self, query: str) -> dict:
        exe = self.prepare()
        before = size(servers_file(self.folder))
        res = dict(self.driver.run(exe, query, self.folder / "finder_dialog.txt"))
        after = size(servers_file(self.folder))
        res.update(query=query, grew=after > before, before=before, after=after, at=self.clock())
        log.info("broker search %r: %s (broker list %s -> %s bytes)", query,
                 "learned new servers" if res["grew"] else ("nothing new" if res.get("searched") else "failed"),
                 before, after)
        return res

    # ---- deciding what to look up
    def wanted(self, requests: dict[str, str]) -> tuple[str, str] | None:
        """The next (server, query) to look up for accounts that can't find their server."""
        now = self.clock()
        for server, broker in sorted(requests.items()):
            e = self.state["servers"].get(server) or {}
            if e.get("gave_up_at") and now - e["gave_up_at"] < GIVE_UP_FOR:
                continue
            if e.get("last") and now - e["last"] < RETRY_SECONDS:
                continue
            qs = queries_for(server, broker)
            tries = 0 if e.get("gave_up_at") else int(e.get("tries", 0))
            if tries >= len(qs):                 # every query tried and the account still can't connect
                e.update(gave_up_at=now, tries=0)
                self.state["servers"][server] = e
                self._save()
                log.warning("broker finder: couldn't find %s with %s; tries again in a day", server, qs)
                continue
            return server, qs[tries]
        return None

    def next_prefetch(self, names: list[str]) -> str | None:
        if self.clock() - self.last_prefetch < PREFETCH_GAP:
            return None
        for name in names or []:
            e = self.state["prefetch"].get(name)
            if not e or (not e.get("searched") and self.clock() - e.get("at", 0) > GIVE_UP_FOR):
                return name
        return None

    # ---- running (in a thread, one at a time)
    def start(self, job: Callable[[], None]) -> bool:
        if self.busy:
            return False
        self.thread = threading.Thread(target=job, daemon=True)
        self.thread.start()
        return True

    def find_server(self, server: str, query: str, after: Callable[[], None] | None = None) -> dict:
        res = self.search(query)
        e = self.state["servers"].get(server) or {}
        e = {**e, "tries": int(e.get("tries", 0)) + 1, "last": res["at"], "query": query,
             "searched": res.get("searched"), "grew": res["grew"], "detail": res.get("detail", "")}
        if res["grew"]:
            e.pop("gave_up_at", None)
        self.state["servers"][server] = e
        self._save()
        if after:
            after()
        return res

    def prefetch(self, name: str, after: Callable[[], None] | None = None) -> dict:
        self.last_prefetch = self.clock()
        res = self.search(name)
        self.state["prefetch"][name] = {"at": res["at"], "searched": res.get("searched"), "grew": res["grew"]}
        self._save()
        if after:
            after()
        return res

    def solved(self, server: str) -> None:
        """The account connected: forget the failures for that server."""
        if server in self.state["servers"]:
            self.state["servers"][server].update(tries=0, solved_at=self.clock())
            self.state["servers"][server].pop("gave_up_at", None)
            self._save()

    def report(self) -> list[dict]:
        cutoff = self.clock() - REPORT_DAYS * 86400
        out = []
        for server, e in self.state["servers"].items():
            if max(e.get("last") or 0, e.get("gave_up_at") or 0) < cutoff:
                continue
            out.append({"server": server, "tries": e.get("tries", 0), "query": e.get("query", ""),
                        "searched": e.get("searched"), "grew": e.get("grew"),
                        "gave_up": bool(e.get("gave_up_at")), "detail": (e.get("detail") or "")[:300]})
        return out


def marker_server(terminal_folder: Path) -> str | None:
    """The server a worker asked us to look up, if any."""
    try:
        return (Path(terminal_folder) / "Config" / MARKER).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None

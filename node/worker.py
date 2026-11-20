"""One MT5 account: logs MT5 in, takes signals from the PipsFarmer server and manages the trades.

Started by node_agent.py, one process per customer account:
    python worker.py --account-id 12 --terminal C:\\PipsFarmer\\accounts\\12\\terminal64.exe
Credentials and the server address come in environment variables set by the node agent.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import logging
import math
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import trade_logic as tl  # noqa: E402

MAGIC = 770077
VERSION = "1.3.23"
RECONNECT_AFTER = 120           # seconds MT5 may be off the broker before we log in again ourselves
SL_WAIT_MIN = 30                # how long a signal without a stop loss waits for the group to post one
FIRST_START_TIMEOUT = 300_000   # a brand-new MT5 copy compiles its examples and updates itself first
NORMAL_TIMEOUT = 90_000
log = logging.getLogger("pf.worker")


def epoch_now() -> float:
    return time.time()


class ServerAPI:
    def __init__(self, base_url: str, token: str, account_id: int):
        self.base, self.token, self.aid = base_url.rstrip("/"), token, account_id

    def post(self, path: str, body: dict, timeout: float = 15) -> dict:
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())

    def poll(self, body: dict) -> dict:
        return self.post(f"/api/worker/{self.aid}/poll", body)

    def deals(self, deals: list) -> dict:
        return self.post(f"/api/worker/{self.aid}/deals", {"deals": deals})


class Worker:
    def __init__(self, mt5, api, account_id: int, login: int, password: str, server: str,
                 terminal: str | None, state_path: Path, clock=epoch_now, library: str | None = None):
        self.mt5, self.api, self.aid = mt5, api, account_id
        self.login, self.password, self.server, self.terminal = login, password, server, terminal
        self.state_path = state_path
        self.clock = clock
        self.settings: dict = {}
        self.copy_enabled = False
        self.news: list = []
        self.symbol: str | None = None
        self.status, self.error = "starting", ""
        self.notes: list[str] = []
        self.blocked: str | None = None
        self.last_report = self.last_snapshot = self.last_deals = 0.0
        self.pending_ack: int | None = None
        self.restart_requested = False
        self.last_code = 0
        self.config_login_at = 0.0
        self.first_connect = True
        self.unknown_server = False
        self.algo_fixed_at = -1e9
        self.offline_since: float | None = None
        self.library = Path(library) if library else None     # shared broker list (servers.dat) on this VPS
        self._journal: tuple[float, list[str]] = (-1e9, [])
        self.state = {"signals": {}, "day": {}, "pos_signal": {}, "deals_from": None, "news_done": [],
                      "await_sl": {}}
        self.load_state()

    # -------------------------------------------------------------- state
    def load_state(self):
        try:
            self.state.update(json.loads(self.state_path.read_text()))
        except (OSError, ValueError):
            pass
        self.state.setdefault("await_sl", {})

    def save_state(self):
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state))
        os.replace(tmp, self.state_path)

    def note(self, text: str):
        stamp = datetime.now(timezone.utc).strftime("%d %b %H:%M")
        log.info(text)
        self.notes = ([f"{stamp} {text}"] + self.notes)[:15]

    # -------------------------------------------------------------- MT5 connection
    def connect(self) -> bool:
        mt5 = self.mt5
        self.refresh_servers()
        timeout = FIRST_START_TIMEOUT if self.first_connect else NORMAL_TIMEOUT
        self.first_connect = False
        kw = dict(login=int(self.login), password=self.password, server=self.server, timeout=timeout)
        if self.terminal:
            kw.update(path=self.terminal, portable=True)
        ok = mt5.initialize(**kw)
        if not ok and self.terminal and self.clock() - self.config_login_at > 1800:
            code, msg = mt5.last_error()
            if code in (-6, -2, -10003):
                # MT5 may not know this broker's server yet. Starting the terminal with a login
                # file makes it look the server up in MetaQuotes' broker list, like typing it
                # into File > Login. Only tried every 30 min so a wrong password can't lock the account.
                self.config_login_at = self.clock()
                self.note(f"login failed ({code}), trying MT5's own login to look up server {self.server}")
                ok = self.login_via_config()
        if not ok:
            code, msg = mt5.last_error()
            self.last_code = code
            self.status = "error"
            friendly = {-6: "wrong login, password or server name. Check them, and copy the server name exactly "
                            "as MT5 shows it (for example ICMarketsSC-Demo)",
                        -10005: "MT5 took too long to start",
                        -10003: "MT5 failed to start", -10004: "MT5 connection lost"}.get(code)
            self.error = friendly or f"{msg} ({code})"
            self.unknown_server = code == -10005 and self.server_never_tried()
            if self.unknown_server:
                self.error = (f"MT5 couldn't find the server \"{self.server}\". Check it's typed exactly as your "
                              f"broker shows it (for example PUPrime-Live). If it is, we've been told and will add it")
            self.note(f"login failed: {self.error}")
            return False
        self.last_code = 0
        self.unknown_server = False
        info = mt5.account_info()
        if not info or int(info.login) != int(self.login):
            self.status, self.error = "error", "logged into a different account than expected"
            return False
        actual = getattr(info, "server", "") or ""
        if actual and actual != self.server:
            # e.g. typed ICMarketsEU-Demo but the account lives on ICMarketsSC-Demo: use the real one from now on
            self.note(f"broker server is {actual} (was entered as {self.server})")
            self.server = actual
        ti = mt5.terminal_info()
        if ti is not None and not ti.trade_allowed:
            if self.terminal and self.clock() - self.algo_fixed_at > 1800:
                self.algo_fixed_at = self.clock()
                self.note("Algo Trading was off in MT5: switching it on and restarting MT5")
                try:
                    mt5.shutdown()
                except Exception:  # noqa: BLE001
                    pass
                self.stop_terminal()
                tl.enable_algo_trading(Path(self.terminal).parent)
                return self.connect()
            self.note("Algo Trading is still off in MT5, so trades can't be placed")
        self.status, self.error = "online", ""
        self.resolve_symbol()
        return True

    # -------------------------------------------------------------- MT5's own journal
    def journal_entries(self, max_bytes: int = 65536) -> list[tuple[str, str, str]]:
        """(time, source, message) from the newest MT5 journal file (logs/YYYYMMDD.log, UTF-16)."""
        if not self.terminal:
            return []
        folder = Path(self.terminal).parent / "logs"
        try:
            files = sorted((f for f in folder.glob("*.log") if f.stem.isdigit()), key=lambda f: f.stat().st_mtime)
            if not files:
                return []
            with open(files[-1], "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                start = max(0, size - max_bytes)
                start -= start % 2
                f.seek(start)
                raw = f.read()
        except OSError:
            return []
        text = raw.decode("utf-16-le", "replace").replace("\ufeff", "")
        out = []
        for line in text.splitlines()[1 if start else 0:]:
            parts = line.split("\t")
            if len(parts) >= 5:
                out.append((parts[2].strip(), parts[3].strip(), "\t".join(parts[4:]).strip()))
        return out

    def journal_tail(self, lines: int = 25) -> list[str]:
        """Last lines of MT5's journal for the admin page (cached for 30 s)."""
        if self.clock() - self._journal[0] < 30:
            return self._journal[1]
        tail = [f"{t} {src}: {msg}" for t, src, msg in self.journal_entries()
                if src not in ("Compiler", "Compile")][-lines:]
        self._journal = (self.clock(), tail)
        return tail

    def server_never_tried(self) -> bool:
        """MT5 started but never tried the network: it doesn't know this broker's server addresses."""
        entries = self.journal_entries()
        starts = [i for i, (_, src, msg) in enumerate(entries) if src == "Terminal" and " started for " in msg]
        if not starts:
            return False
        return not any(src == "Network" for _, src, _ in entries[starts[-1]:])

    # -------------------------------------------------------------- shared broker list
    def library_has_more(self) -> int:
        """Size of the shared broker list when it's bigger than this terminal's, else 0."""
        if not (self.library and self.terminal):
            return 0
        try:
            lib_size = self.library.stat().st_size
        except OSError:
            return 0
        try:
            my_size = (Path(self.terminal).parent / "Config" / "servers.dat").stat().st_size
        except OSError:
            my_size = 0
        return lib_size if lib_size > my_size else 0

    def refresh_servers(self) -> bool:
        """Copy this VPS's shared broker list (servers.dat) in when it knows more brokers than this
        terminal does. MT5 reads it at start, so the terminal is restarted when it changes."""
        lib_size = self.library_has_more()
        if not lib_size:
            return False
        mine = Path(self.terminal).parent / "Config" / "servers.dat"
        try:
            self.mt5.shutdown()
        except Exception:  # noqa: BLE001
            pass
        self.stop_terminal()
        mine.parent.mkdir(parents=True, exist_ok=True)
        tmp = mine.with_suffix(".new")
        shutil.copyfile(self.library, tmp)
        os.replace(tmp, mine)
        self.note(f"got the updated broker list ({lib_size // 1024} KB)")
        self.first_connect = True
        return True

    def launch_terminal(self, args: list[str]):
        """Separate method so tests can replace it."""
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return subprocess.Popen(args, creationflags=flags)

    def stop_terminal(self):
        try:
            import psutil
        except ImportError:
            return
        exe = str(Path(self.terminal)).lower()
        for p in psutil.process_iter(["exe"]):
            try:
                if (p.info["exe"] or "").lower() == exe:
                    p.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    def login_via_config(self, wait_seconds: int = 150) -> bool:
        mt5 = self.mt5
        folder = Path(self.terminal).parent
        ini = folder / "Config" / "pf_login.ini"
        ini.parent.mkdir(parents=True, exist_ok=True)
        text = (f"[Common]\r\nLogin={int(self.login)}\r\nPassword={self.password}\r\nServer={self.server}\r\n"
                f"NewsEnable=0\r\n[Experts]\r\nEnabled=1\r\nAllowLiveTrading=1\r\n")
        try:
            mt5.shutdown()
            self.stop_terminal()
            ini.write_bytes(text.encode("utf-16"))
            self.launch_terminal([self.terminal, "/portable", f"/config:{ini}"])
            deadline = self.clock() + wait_seconds
            while self.clock() < deadline:
                time.sleep(5)
                if mt5.initialize(path=self.terminal, portable=True, timeout=30_000):
                    info, ti = mt5.account_info(), mt5.terminal_info()
                    if info and int(info.login) == int(self.login) and ti and ti.connected:
                        self.note(f"logged in through MT5 and found server {self.server}")
                        return True
                    mt5.shutdown()
            return False
        finally:
            try:                        # it holds the password: overwrite, then delete (best effort on SSDs)
                if ini.exists():
                    ini.write_bytes(b"\0" * max(ini.stat().st_size, 256))
                ini.unlink()
            except OSError:
                pass

    def resolve_symbol(self) -> str | None:
        mt5 = self.mt5
        wanted = (self.settings.get("symbol") or "").strip()
        names = [s.name for s in (mt5.symbols_get() or [])]
        pick = None
        if wanted and wanted in names:
            pick = wanted
        else:
            up = {n.upper(): n for n in names}
            for test in (lambda n: n == "XAUUSD", lambda n: n.startswith("XAUUSD"), lambda n: "XAUUSD" in n,
                         lambda n: n == "GOLD", lambda n: n.startswith("GOLD"), lambda n: "XAU" in n and "USD" in n):
                hits = [orig for u, orig in up.items() if test(u)]
                if hits:
                    hits.sort(key=lambda n: (len(n), n))
                    for h in hits:
                        info = mt5.symbol_info(h)
                        if info and getattr(info, "trade_mode", 4) != 0:
                            pick = h
                            break
                if pick:
                    break
        if pick:
            mt5.symbol_select(pick, True)
            if pick != self.symbol:
                self.note(f"trading gold as {pick}")
        elif self.symbol is None:
            self.error = "couldn't find a gold symbol on this broker; set it in account settings"
        self.symbol = pick
        return pick

    def connected(self) -> bool:
        ti = self.mt5.terminal_info()
        return bool(ti and ti.connected)

    # -------------------------------------------------------------- MT5 helpers
    def tick(self):
        return self.mt5.symbol_info_tick(self.symbol) if self.symbol else None

    def sinfo(self):
        return self.mt5.symbol_info(self.symbol)

    def our_positions(self, sid: str | None = None):
        out = [p for p in (self.mt5.positions_get() or []) if p.magic == MAGIC]
        if sid is not None:
            ids = {str(t) for t, s in self.state["pos_signal"].items() if s == sid}
            out = [p for p in out if str(p.ticket) in ids or str(p.identifier) in ids]
        return out

    def our_orders(self, sid: str | None = None):
        out = [o for o in (self.mt5.orders_get() or []) if o.magic == MAGIC]
        if sid is not None:
            ids = {str(t) for t, s in self.state["pos_signal"].items() if s == sid}
            out = [o for o in out if str(o.ticket) in ids]
        return out

    def _filling(self, info, pending=False):
        mt5 = self.mt5
        if pending:
            return mt5.ORDER_FILLING_RETURN
        fm = getattr(info, "filling_mode", 0)
        if fm & 1:
            return mt5.ORDER_FILLING_FOK
        if fm & 2:
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    def send(self, req: dict):
        mt5 = self.mt5
        res = mt5.order_send(req)
        if res is not None and res.retcode == 10030 and req.get("action") == mt5.TRADE_ACTION_DEAL:
            for f in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN):
                req["type_filling"] = f
                res = mt5.order_send(req)
                if res is not None and res.retcode != 10030:
                    break
        ok = res is not None and res.retcode in (10008, 10009, 10010)
        if not ok:
            err = res.comment if res is not None else str(mt5.last_error())
            code = res.retcode if res is not None else "?"
            self.note(f"order rejected ({code} {err})")
        return res if ok else None

    def market(self, side: str, lot: float, sl: float, tp: float, comment: str):
        mt5, info, t = self.mt5, self.sinfo(), self.tick()
        req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": self.symbol, "volume": lot,
               "type": mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL,
               "price": t.ask if side == "buy" else t.bid, "sl": float(sl or 0), "tp": float(tp or 0),
               "deviation": 50, "magic": MAGIC, "comment": comment[:31],
               "type_time": mt5.ORDER_TIME_GTC, "type_filling": self._filling(info)}
        return self.send(req)

    def pending(self, side: str, kind: str, price: float, lot: float, sl: float, tp: float, comment: str):
        mt5, info = self.mt5, self.sinfo()
        types = {("buy", "limit"): mt5.ORDER_TYPE_BUY_LIMIT, ("sell", "limit"): mt5.ORDER_TYPE_SELL_LIMIT,
                 ("buy", "stop"): mt5.ORDER_TYPE_BUY_STOP, ("sell", "stop"): mt5.ORDER_TYPE_SELL_STOP}
        req = {"action": mt5.TRADE_ACTION_PENDING, "symbol": self.symbol, "volume": lot, "type": types[(side, kind)],
               "price": round(price, info.digits), "sl": float(sl or 0), "tp": float(tp or 0), "deviation": 50,
               "magic": MAGIC, "comment": comment[:31], "type_time": mt5.ORDER_TIME_GTC,
               "type_filling": self._filling(info, pending=True)}
        return self.send(req)

    def modify_sl(self, pos, sl: float) -> bool:
        mt5 = self.mt5
        return self.send({"action": mt5.TRADE_ACTION_SLTP, "symbol": pos.symbol, "position": pos.ticket,
                          "sl": float(sl), "tp": float(pos.tp)}) is not None

    def modify_sltp(self, pos, sl: float, tp: float) -> bool:
        mt5 = self.mt5
        return self.send({"action": mt5.TRADE_ACTION_SLTP, "symbol": pos.symbol, "position": pos.ticket,
                          "sl": float(sl), "tp": float(tp)}) is not None

    def modify_order_sltp(self, order, sl: float, tp: float) -> bool:
        mt5 = self.mt5
        return self.send({"action": mt5.TRADE_ACTION_MODIFY, "order": order.ticket, "price": order.price_open,
                          "sl": float(sl), "tp": float(tp), "type_time": mt5.ORDER_TIME_GTC}) is not None

    def modify_order_sl(self, order, sl: float) -> bool:
        mt5 = self.mt5
        return self.send({"action": mt5.TRADE_ACTION_MODIFY, "order": order.ticket, "price": order.price_open,
                          "sl": float(sl), "tp": float(order.tp),
                          "type_time": mt5.ORDER_TIME_GTC}) is not None

    def close_position(self, pos, volume: float | None = None) -> bool:
        mt5 = self.mt5
        t = mt5.symbol_info_tick(pos.symbol)
        info = mt5.symbol_info(pos.symbol)
        buy = pos.type == mt5.POSITION_TYPE_BUY
        req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": pos.symbol, "volume": float(volume or pos.volume),
               "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY, "position": pos.ticket,
               "price": t.bid if buy else t.ask, "deviation": 80, "magic": MAGIC, "comment": "PF close",
               "type_time": mt5.ORDER_TIME_GTC, "type_filling": self._filling(info)}
        return self.send(req) is not None

    def cancel_order(self, order) -> bool:
        return self.send({"action": self.mt5.TRADE_ACTION_REMOVE, "order": order.ticket}) is not None

    def side_of(self, pos) -> str:
        return "buy" if pos.type == self.mt5.POSITION_TYPE_BUY else "sell"

    # -------------------------------------------------------------- signals
    def entries_allowed(self) -> str | None:
        if not self.copy_enabled:
            return "copying is paused"
        if self.terminal:
            ti = self.mt5.terminal_info()
            if ti is not None and not ti.trade_allowed:
                return "Algo Trading is switched off in MT5"
        return self.blocked

    def duplicate_of(self, sig: dict) -> str | None:
        """The same signal reaching this account twice (one group ticked twice, or copied between groups)."""
        if not sig.get("entry_low"):
            return None
        key = (sig["side"], round(float(sig["entry_low"]), 2), round(float(sig["entry_high"]), 2),
               round(float(sig.get("sl") or 0), 2))
        for osid, rec in self.state["signals"].items():
            if rec.get("finished") or self.clock() - rec.get("created", 0) > 3600:
                continue
            if (rec["side"], round(rec["entry_low"], 2), round(rec["entry_high"], 2),
                    round(float(rec.get("orig_sl", rec["sl"]) or 0), 2)) == key:
                return osid
        return None

    IDEA_WINDOW = 15 * 60       # a re-post of the same call usually lands within a few minutes
    IDEA_GAP = 6.0              # zones this close ($) count as the same idea

    def same_idea_as(self, side: str, lo: float, hi: float) -> str | None:
        """An open signal on the same side whose zone is within a few dollars, posted in the last 15 minutes."""
        for osid, rec in self.state["signals"].items():
            if rec.get("finished") or rec["side"] != side or self.clock() - rec.get("created", 0) > self.IDEA_WINDOW:
                continue
            if lo - self.IDEA_GAP <= rec["entry_high"] and rec["entry_low"] <= hi + self.IDEA_GAP:
                return osid
        return None

    GROUP_KEYS = ("lot_mode", "fixed_lot", "risk_percent", "risk_amount", "layers", "tp_target")

    def handle_signal(self, sig: dict):
        """A new signal: this group's own settings (if the customer set any) apply on top of the account's."""
        groups = self.settings.get("groups") or {}
        g = groups.get(str(sig.get("group") or "")) or groups.get(f"s{sig.get('source_id')}")
        if not g:
            return self._handle_signal(sig)
        if g.get("paused"):
            if str(sig["id"]) not in self.state["signals"]:
                self.note(f"signal #{sig['id']} skipped: you paused {sig.get('source') or 'this group'}")
            return
        base = self.settings
        self.settings = {**base, **{k: v for k, v in g.items() if k in self.GROUP_KEYS}}
        try:
            return self._handle_signal(sig)
        finally:
            self.settings = base

    def _handle_signal(self, sig: dict):
        sid = str(sig["id"])
        if sid in self.state["signals"]:
            return
        self.state["await_sl"].pop(sid, None)
        dup = self.duplicate_of(sig)
        if dup:
            return self.note(f"signal #{sid} skipped: same trade as signal #{dup} (posted in two of your groups)")
        s = self.settings
        side = sig["side"]
        why = self.entries_allowed()
        if why:
            return self.note(f"signal #{sid} skipped: {why}")
        if not self.symbol and not self.resolve_symbol():
            return self.note(f"signal #{sid} skipped: no gold symbol found")
        if s.get("directions", "both") not in ("both", side):
            return self.note(f"signal #{sid} skipped: {side}s are switched off")
        t = self.tick()
        if not t:
            return self.note(f"signal #{sid} skipped: no price")
        server_hhmm = datetime.fromtimestamp(t.time, timezone.utc).strftime("%H:%M")
        if not tl.in_session(server_hhmm, s.get("session_start", "00:00"), s.get("session_end", "23:59")):
            return self.note(f"signal #{sid} skipped: outside trading hours ({server_hhmm})")
        if s.get("max_open_signals"):
            active = sum(1 for v in self.state["signals"].values() if not v.get("finished"))
            if active >= s["max_open_signals"]:
                return self.note(f"signal #{sid} skipped: {active} signals already open")
        lo, hi, tps = float(sig["entry_low"] or 0), float(sig["entry_high"] or 0), [float(x) for x in sig.get("tps") or []]
        if lo <= 0 or hi <= 0:              # "BUY NOW" with no price: today's price
            lo = hi = t.ask if side == "buy" else t.bid
        sl = sig.get("sl")
        mid = (lo + hi) / 2
        temp_sl = False
        if not sl:
            if sig.get("instant") and s.get("instant_entry", True):
                temp_sl = True              # get in now; the group's real stop replaces this one
            elif s.get("require_sl", True):
                self.state["await_sl"][sid] = {"sig": sig, "until": self.clock() + 60 * SL_WAIT_MIN}
                self.save_state()
                return self.note(f"signal #{sid} {side}: waiting for the group to post a stop loss")
            d = float(s.get("default_sl_pips", 50)) * tl.PIP
            sl = lo - d if side == "buy" else hi + d
        sl = float(sl)
        price = t.ask if side == "buy" else t.bid
        if s.get("max_entry_away_pct", 1.5) and price and abs(mid - price) / price * 100 > float(s.get("max_entry_away_pct", 1.5)):
            return self.note(f"signal #{sid} skipped: entry {mid:.2f} is {abs(mid - price):.0f} away from the price "
                             f"{price:.2f} (a typo in the call?)")
        if not temp_sl and s.get("min_sl_pips", 20) and abs(mid - sl) / tl.PIP < float(s.get("min_sl_pips", 20)):
            return self.note(f"signal #{sid} skipped: SL is only {abs(mid - sl) / tl.PIP:.0f} pips away "
                             f"(your minimum is {float(s.get('min_sl_pips', 20)):.0f})")
        if s.get("require_tp") and not tps and not sig.get("instant"):
            return self.note(f"signal #{sid} skipped: no take profit in the call")
        if s.get("one_trade_per_idea", True):
            same = self.same_idea_as(side, lo, hi)
            if same:
                return self.note(f"signal #{sid} skipped: same idea as signal #{same} already running "
                                 "(another of your groups posted it first)")
        if s.get("max_sl_pips") and abs(mid - sl) / tl.PIP > float(s["max_sl_pips"]):
            return self.note(f"signal #{sid} skipped: SL {abs(mid - sl) / tl.PIP:.0f} pips is above your maximum")
        if (side == "buy" and t.bid <= sl) or (side == "sell" and t.ask >= sl):
            return self.note(f"signal #{sid} skipped: price is already past the stop loss")
        if s.get("skip_if_tp1_passed", True) and tps and tl.tp_reached(side, tps[0], t.bid, t.ask):
            return self.note(f"signal #{sid} skipped: price already reached TP1")

        n = int(s.get("layers", 1))
        levels = tl.layer_levels(side, lo, hi, n)
        ltps = tl.layer_tps(tps, n, s.get("tp_target", "spread"))
        info = self.sinfo()
        account = self.mt5.account_info()
        loss = []
        for lv in levels:
            # a layer that would fill at market right now fills at today's price, not the zone edge
            lv = min(lv, t.ask) if side == "buy" else max(lv, t.bid)
            p = self.mt5.order_calc_profit(self.mt5.ORDER_TYPE_BUY if side == "buy" else self.mt5.ORDER_TYPE_SELL,
                                           self.symbol, 1.0, lv, sl)
            loss.append(abs(p) if p is not None else abs(lv - sl) * info.trade_contract_size)
        lot, why = tl.lot_per_layer(s, n, account.balance, loss, info.volume_step, info.volume_min, info.volume_max)
        if not lot:
            return self.note(f"signal #{sid} skipped: {why}")
        rec = {"side": side, "order_type": sig.get("order_type", "market"), "sl": sl, "tps": tps, "temp_sl": temp_sl,
               "orig_sl": sig.get("sl"),
               "entry_low": lo, "entry_high": hi, "created": self.clock(), "tp1_done": False, "be_done": False,
               "expires": self.clock() + 60 * int(s.get("entry_expiry_min", 120)),
               "layers": [{"i": i, "level": levels[i], "tp": ltps[i], "lot": lot, "state": "new", "ticket": None}
                          for i in range(n)]}
        self.state["signals"][sid] = rec
        self.note(f"signal #{sid} {side} {lo:g}-{hi:g}: {n} layer(s) x {lot} lots"
                  + (f", temporary stop {sl:g} until the group posts its stop loss" if temp_sl else ""))
        self.place_layers(sid, rec)
        self.save_state()

    def place_layers(self, sid: str, rec: dict):
        """Act on layers in state new/wait."""
        t = self.tick()
        if not t:
            return
        s = self.settings
        smart = bool(s.get("smart_entry"))
        dist = float(s.get("smart_distance_pips", 20)) * tl.PIP
        spread_ok = not s.get("max_spread_pips") or (t.ask - t.bid) / tl.PIP <= float(s["max_spread_pips"])
        side = rec["side"]
        cap = float(s.get("max_open_lots") or 0)
        capped: list[str] = []
        for L in rec["layers"]:
            if L["state"] not in ("new", "wait"):
                continue
            act = tl.entry_action(side, rec["order_type"], L["level"], t.bid, t.ask, smart, dist)
            comment = f"PF{sid}-{L['i']}"
            if cap and act in ("market", "limit", "stop") and (act != "market" or spread_ok):
                used = self.lots_in_use(exclude=L)
                info = self.sinfo()
                fit = tl.fit_under_cap(L["lot"], used, cap, info.volume_step, info.volume_min)
                if not fit:
                    L["state"] = "skipped"
                    capped.append(f"layer {L['i'] + 1} skipped ({used:g} lots already open or waiting)")
                    continue
                if fit < L["lot"]:
                    capped.append(f"layer {L['i'] + 1} trimmed from {L['lot']:g} to {fit:g} lots")
                    L["lot"] = fit
            if act == "market":
                if not spread_ok:
                    L["state"] = "wait"
                    continue
                res = self.market(side, L["lot"], rec["sl"], L["tp"], comment)
                if res:
                    L["state"], L["ticket"] = "open", int(res.order)
                    self.state["pos_signal"][str(res.order)] = sid
                    if getattr(res, "deal", 0):
                        self.state["pos_signal"][str(res.deal)] = sid
                else:
                    L["state"] = "failed"
            elif act in ("limit", "stop"):
                res = self.pending(side, act, L["level"], L["lot"], rec["sl"], L["tp"], comment)
                if res:
                    L["state"], L["ticket"] = "pending", int(res.order)
                    self.state["pos_signal"][str(res.order)] = sid
                else:
                    L["state"] = "failed"
            else:
                L["state"] = "wait"
        if capped:
            if all(L["state"] == "skipped" for L in rec["layers"]):
                self.note(f"signal #{sid} skipped: your maximum of {cap:g} lots open is reached")
            else:
                self.note(f"signal #{sid}: maximum {cap:g} lots open: " + "; ".join(capped))

    def lots_in_use(self, exclude: dict | None = None) -> float:
        """Lots this account has in play: open positions + pending orders + layers waiting to enter."""
        used = sum(p.volume for p in self.our_positions())
        used += sum(getattr(o, "volume_current", 0) or 0 for o in self.our_orders())
        for rec in self.state["signals"].values():
            if rec.get("finished"):
                continue
            used += sum(L["lot"] for L in rec["layers"] if L["state"] == "wait" and L is not exclude)
        return round(used, 2)

    # -------------------------------------------------------------- management
    def move_to_breakeven(self, sid: str, rec: dict, lock_pips: float | None = None,
                          group_sl: float | None = None) -> bool:
        """Stop to each trade's own entry (+ lock pips). With group_sl (the group's "move SL to 3725"),
        the more protective of the two is used, so a late entry never ends up with a stop below its entry."""
        lock = float(self.settings.get("be_offset_pips", 0) if lock_pips is None else lock_pips)
        offset = lock * tl.PIP
        t = self.tick()
        ok, plain = True, 0
        for p in self.our_positions(sid):
            side = self.side_of(p)
            valid = (lambda x: not t or (x < t.bid if side == "buy" else x > t.ask))  # noqa: E731
            options = [tl.breakeven_price(side, p.price_open, offset)]
            if group_sl:
                options.append(round(float(group_sl), 2))
            options = [x for x in options if valid(x)]
            if options:
                be = max(options) if side == "buy" else min(options)
            elif valid(round(p.price_open, 2)):
                be, plain = round(p.price_open, 2), plain + 1   # not far enough in profit yet: plain entry
            else:
                continue                                         # price is past entry: leave the stop as it is
            if tl.better_sl(side, be, p.sl):
                ok = self.modify_sl(p, be) and ok
        if ok:
            rec["be_done"] = True
            extra = f" +{lock:g} pips" if lock else ""
            self.note(f"signal #{sid}: stop moved to breakeven{extra}" + (f" ({plain} at plain entry: price not far enough yet)" if plain else ""))
        return ok

    def vanished_order_state(self, sid: str, L: dict) -> str:
        """An unfilled entry left MT5 without us cancelling it: find out why and say so."""
        mt5 = self.mt5
        try:
            hist = mt5.history_orders_get(ticket=int(L["ticket"])) or []
        except Exception:
            hist = []
        if not hist:
            return "done"
        state = getattr(hist[-1], "state", None)
        if state == getattr(mt5, "ORDER_STATE_FILLED", 4):
            return "done"
        why = {getattr(mt5, "ORDER_STATE_CANCELED", 2): "cancelled",
               getattr(mt5, "ORDER_STATE_EXPIRED", 6): "expired",
               getattr(mt5, "ORDER_STATE_REJECTED", 5): "rejected"}.get(state, "removed")
        if not L.get("by_us"):
            self.note(f"signal #{sid}: an unfilled entry at {L.get('level')} was {why} in MT5 "
                      "(not by us: the broker, or someone using the account)")
        return "cancelled"

    def cancel_entries(self, sid: str, rec: dict, why: str):
        n = 0
        mine = {str(L["ticket"]): L for L in rec["layers"] if L.get("ticket")}
        for o in self.our_orders(sid):
            if str(o.ticket) in mine:
                mine[str(o.ticket)]["by_us"] = True
            if self.cancel_order(o):
                n += 1
        for L in rec["layers"]:
            if L["state"] in ("new", "wait", "pending"):
                L["state"] = "cancelled"
        if n:
            self.note(f"signal #{sid}: cancelled {n} unfilled order(s), {why}")

    def close_signal(self, sid: str, rec: dict, why: str, fraction: float = 1.0):
        positions = self.our_positions(sid)
        if fraction >= 1:
            self.cancel_entries(sid, rec, why)
            for p in positions:
                self.close_position(p)
            rec["finished"] = True
            self.note(f"signal #{sid}: closed {len(positions)} trade(s), {why}")
            return
        if len(positions) >= 2:
            k = math.ceil(len(positions) * fraction)
            nearest = sorted(positions, key=lambda p: abs(p.tp - p.price_open) if p.tp else 1e9)[:k]
            for p in nearest:
                self.close_position(p)
            self.note(f"signal #{sid}: closed {k} of {len(positions)} trades, {why}")
        elif positions:
            p = positions[0]
            info = self.sinfo()
            vol = tl.round_lot(p.volume * fraction, info.volume_step, info.volume_min, p.volume)
            if vol and vol < p.volume:
                self.close_position(p, vol)
                self.note(f"signal #{sid}: closed {vol} of {p.volume} lots, {why}")

    def apply_levels(self, sid: str, rec: dict, sl: float | None, tps: list[float]) -> None:
        """The group posted or edited in the stop loss / targets: put them on every order of the signal."""
        side = rec["side"]
        t = self.tick()
        if sl:
            wrong_side = t and ((side == "buy" and t.bid <= sl) or (side == "sell" and t.ask >= sl))
            if wrong_side:
                self.note(f"signal #{sid}: the group's stop {sl:g} is already passed, closing")
                return self.close_signal(sid, rec, "price is past the group's stop loss")
            rec["sl"], rec["temp_sl"] = float(sl), False
        if tps:
            rec["tps"] = [float(x) for x in tps]
            ltps = tl.layer_tps(rec["tps"], len(rec["layers"]), self.settings.get("tp_target", "spread"))
            for L, tp in zip(rec["layers"], ltps):
                L["tp"] = tp
        by_layer = {L["i"]: L for L in rec["layers"]}

        def layer_tp(comment: str, current: float) -> float:
            try:
                return by_layer[int(comment.rsplit("-", 1)[1])]["tp"] or current
            except (IndexError, ValueError, KeyError):
                return current
        for p in self.our_positions(sid):
            new_tp = layer_tp(p.comment or "", p.tp) if tps else p.tp
            if abs(p.sl - rec["sl"]) > 1e-6 or abs(p.tp - new_tp) > 1e-6:
                self.modify_sltp(p, rec["sl"], new_tp)
        for o in self.our_orders(sid):
            new_tp = layer_tp(o.comment or "", o.tp) if tps else o.tp
            self.modify_order_sltp(o, rec["sl"], new_tp)
        self.note(f"signal #{sid}: group's levels set" + (f", stop {rec['sl']:g}" if sl else "")
                  + (f", targets {', '.join(f'{x:g}' for x in rec['tps'])}" if tps else ""))

    def handle_update(self, upd: dict):
        sid = str(upd["signal_id"])
        if upd["kind"] in ("levels", "sl_move") and sid in self.state["await_sl"]:
            sig = dict(self.state["await_sl"].pop(sid)["sig"])
            if upd.get("value"):
                sig["sl"] = upd["value"]
            if upd.get("tps"):
                sig["tps"] = upd["tps"]
            self.save_state()
            if sig.get("sl"):
                return self.handle_signal(sig)
            self.state["await_sl"][sid] = {"sig": sig, "until": self.clock() + 60 * SL_WAIT_MIN}
            return
        rec = self.state["signals"].get(sid)
        if not rec or rec.get("finished"):
            return
        if upd["kind"] == "levels":         # the signal's own stop and targets: always applied
            self.apply_levels(sid, rec, upd.get("value"), upd.get("tps") or [])
            return self.save_state()
        if not (self.settings.get("follow_updates", True) or upd.get("force")):
            what = {"breakeven": "move to breakeven", "sl_move": f"move the stop to {upd.get('value')}",
                    "close": "close", "close_partial": "take partials", "tp_hit": "TP hit", "sl_hit": "SL hit",
                    "cancel": "cancel"}.get(upd["kind"], upd["kind"])
            return self.note(f"signal #{sid}: the group said {what}, not applied because "
                             "\"Follow the group's updates\" is off for this account")
        kind, value = upd["kind"], upd.get("value")
        if kind == "tp_hit":
            if not rec["tp1_done"]:
                self.on_tp1(sid, rec, "the group posted TP hit")
        elif kind == "breakeven":
            self.move_to_breakeven(sid, rec, upd.get("be_lock_pips"))
            if self.settings.get("cancel_pending_on_be", True):
                # the group called the trade risk-free: don't open fresh entries at the old stop
                self.cancel_entries(sid, rec, "the group moved the trade to breakeven")
        elif kind == "sl_move" and value and upd.get("sl_move_as_be"):
            # this group's "move SL to <price>" means risk-free: entry + lock on each trade's own entry
            self.move_to_breakeven(sid, rec, upd.get("be_lock_pips"), float(value))
        elif kind == "sl_move" and value:
            # same as an edited stop: put it on every trade, or close if price is already past it
            self.apply_levels(sid, rec, float(value), [])
        elif kind in ("close", "sl_hit"):
            self.close_signal(sid, rec, "group said close" if kind == "close" else "group's SL was hit")
        elif kind == "close_partial":
            self.close_signal(sid, rec, "group said take partials", float(value or 0.5))
        elif kind == "cancel":
            self.cancel_entries(sid, rec, "group cancelled the signal")
            for L in rec["layers"]:
                if L["state"] in ("new", "wait"):
                    L["state"] = "cancelled"
        self.save_state()

    def on_tp1(self, sid: str, rec: dict, why: str):
        rec["tp1_done"] = True
        if self.settings.get("cancel_pending_after_tp1", True):
            self.cancel_entries(sid, rec, "TP1 reached")
        if self.settings.get("breakeven_at_tp1", True):
            self.move_to_breakeven(sid, rec)

    def manage_signals(self):
        t = self.tick()
        if not t:
            return
        now = self.clock()
        for sid, w in list(self.state["await_sl"].items()):
            if now > w["until"]:
                del self.state["await_sl"][sid]
                self.note(f"signal #{sid} skipped: the group didn't post a stop loss within {SL_WAIT_MIN} min")
                self.save_state()
        open_tickets = {str(p.ticket) for p in self.our_positions()} | {str(p.identifier) for p in self.our_positions()}
        order_tickets = {str(o.ticket) for o in self.our_orders()}
        changed = False
        for sid, rec in list(self.state["signals"].items()):
            if rec.get("finished"):
                if now - rec["created"] > 7 * 86400:
                    del self.state["signals"][sid]
                    changed = True
                continue
            # sync layer states with MT5
            closed_now = False
            for L in rec["layers"]:
                tk = str(L["ticket"]) if L["ticket"] else None
                if L["state"] == "pending" and tk not in order_tickets:
                    if tk in open_tickets:
                        L["state"] = "open"
                    else:
                        L["state"] = self.vanished_order_state(sid, L)
                    changed = True
                elif L["state"] == "open" and tk not in open_tickets:
                    L["state"] = "done"
                    closed_now = True
                    changed = True
            if closed_now and rec.get("be_done") and self.settings.get("cancel_pending_on_be", True) \
                    and any(L["state"] in ("pending", "new", "wait") for L in rec["layers"]):
                # the filled part came back to entry and closed: the trade is over, so are its other entries
                self.cancel_entries(sid, rec, "the first entry closed at breakeven")
            # TP1 by price
            if not rec["tp1_done"] and rec["tps"] and tl.tp_reached(rec["side"], rec["tps"][0], t.bid, t.ask):
                self.on_tp1(sid, rec, "price reached TP1")
                changed = True
            elif rec["tp1_done"] and not rec["be_done"] and self.settings.get("breakeven_at_tp1", True) \
                    and self.our_positions(sid):
                self.move_to_breakeven(sid, rec)
            # waiting layers / expiry
            waiting = [L for L in rec["layers"] if L["state"] in ("new", "wait")]
            if waiting:
                if now > rec["expires"]:
                    for L in waiting:
                        L["state"] = "expired"
                    self.cancel_entries(sid, rec, "entry time ran out")
                    changed = True
                elif not self.entries_allowed():
                    if (rec["side"] == "buy" and t.bid <= rec["sl"]) or (rec["side"] == "sell" and t.ask >= rec["sl"]):
                        for L in waiting:
                            L["state"] = "expired"
                    else:
                        before = [L["state"] for L in rec["layers"]]
                        self.place_layers(sid, rec)
                        changed |= before != [L["state"] for L in rec["layers"]]
            elif now > rec["expires"] and any(L["state"] == "pending" for L in rec["layers"]):
                self.cancel_entries(sid, rec, "entry time ran out")
                changed = True
            if all(L["state"] in ("done", "cancelled", "expired", "failed", "skipped") for L in rec["layers"]):
                rec["finished"] = True
                changed = True
        if changed:
            self.save_state()

    def close_everything(self, why: str):
        for o in self.our_orders():
            self.cancel_order(o)
        for p in self.our_positions():
            self.close_position(p)
        for rec in self.state["signals"].values():
            for L in rec["layers"]:
                if L["state"] in ("new", "wait", "pending"):
                    L["state"] = "cancelled"
        self.note(f"closed all trades: {why}")
        self.save_state()

    # -------------------------------------------------------------- protections
    def check_protections(self):
        s = self.settings
        acc = self.mt5.account_info()
        t = self.tick()
        if not acc or not t:
            return
        day_key = datetime.fromtimestamp(t.time, timezone.utc).strftime("%Y-%m-%d")
        day = self.state["day"]
        if day.get("date") != day_key:
            day.update(date=day_key, start_balance=acc.balance, limit_hit=None, closed=False)
            self.save_state()
        self.day_pnl = acc.equity - day.get("start_balance", acc.balance)
        blocked = None
        hit = day.get("limit_hit") or tl.daily_block(s, day["start_balance"], self.day_pnl)
        if hit:
            blocked = hit + ", no new trades until tomorrow"
            if not day.get("limit_hit"):
                day["limit_hit"] = hit
                self.note(hit)
                if s.get("close_on_limit", True) and not day.get("closed"):
                    day["closed"] = True
                    self.close_everything(hit)
                self.save_state()
        if not blocked and s.get("news_protection"):
            text, ev = tl.news_state(self.news, self.clock(), int(s.get("news_before_min", 15)),
                                     int(s.get("news_after_min", 15)))
            if ev:
                blocked = text
                key = f"{ev['time']}"
                if key not in self.state["news_done"]:
                    self.state["news_done"] = (self.state["news_done"] + [key])[-50:]
                    for sid, rec in self.state["signals"].items():
                        if not rec.get("finished"):
                            self.cancel_entries(sid, rec, "news coming up")
                    if s.get("news_close_trades") and self.clock() < float(ev["time"]):
                        self.close_everything(text)
                    self.save_state()
        self.blocked = blocked

    # -------------------------------------------------------------- reporting
    def report(self, snapshot=False) -> dict:
        acc = self.mt5.account_info() if self.status == "online" else None
        positions, pendings, others = [], [], []
        rev = self.state["pos_signal"]
        if acc:
            for p in self.our_positions():
                positions.append({"ticket": p.ticket, "side": self.side_of(p), "symbol": p.symbol, "volume": p.volume,
                                  "price": p.price_open, "sl": p.sl, "tp": p.tp, "profit": round(p.profit + p.swap, 2),
                                  "signal_id": rev.get(str(p.ticket)) or rev.get(str(p.identifier))})
            for p in (self.mt5.positions_get() or []):
                if p.magic != MAGIC and len(others) < 30:     # trades the customer placed themselves
                    others.append({"ticket": p.ticket, "side": self.side_of(p), "symbol": p.symbol,
                                   "volume": p.volume, "price": p.price_open, "sl": p.sl, "tp": p.tp,
                                   "profit": round(p.profit + p.swap, 2)})
            for o in self.our_orders():
                side = "buy" if o.type in (self.mt5.ORDER_TYPE_BUY_LIMIT, self.mt5.ORDER_TYPE_BUY_STOP) else "sell"
                pendings.append({"ticket": o.ticket, "side": side, "volume": o.volume_current, "price": o.price_open,
                                 "sl": o.sl, "tp": o.tp, "signal_id": rev.get(str(o.ticket))})
        waiting = [{"side": r["side"], "volume": L["lot"], "price": L["level"], "sl": r["sl"], "tp": L["tp"], "signal_id": sid}
                   for sid, r in self.state["signals"].items() if not r.get("finished")
                   for L in r["layers"] if L["state"] in ("new", "wait")]
        t = self.tick() if acc else None
        rep = {"status": self.status, "error": self.error, "positions": positions, "pendings": pendings,
               "other_positions": others,
               "waiting": waiting, "blocked": self.entries_allowed() if self.copy_enabled else None,
               "symbol": self.symbol, "day_pnl": round(getattr(self, "day_pnl", 0.0), 2),
               "server_time": t.time if t else None, "bid": t.bid if t else None, "ask": t.ask if t else None, "notes": self.notes, "snapshot": snapshot,
               "journal": self.journal_tail(), "unknown_server": self.unknown_server}
        if acc:
            rep["account"] = {"balance": acc.balance, "equity": acc.equity, "currency": acc.currency,
                              "company": acc.company, "server": getattr(acc, "server", "") or ""}
        return rep

    def collect_deals(self) -> list[dict]:
        mt5 = self.mt5
        now = self.clock()
        start = self.state.get("deals_from") or (now - 30 * 86400)
        deals = mt5.history_deals_get(datetime.fromtimestamp(start - 3600, timezone.utc),
                                      datetime.fromtimestamp(now + 86400, timezone.utc)) or []
        entries = {0: "in", 1: "out", 2: "inout", 3: "out"}
        rev = self.state["pos_signal"]
        out = []
        for d in deals:
            if d.type not in (0, 1):
                continue
            deal_side = "buy" if d.type == 0 else "sell"
            entry = entries.get(d.entry, "out")
            pos_side = deal_side if entry == "in" else ("sell" if deal_side == "buy" else "buy")
            out.append({"ticket": d.ticket, "position_id": d.position_id, "time": d.time, "symbol": d.symbol,
                        "side": pos_side, "entry": entry, "volume": d.volume, "price": d.price,
                        "profit": round(d.profit + d.commission + d.swap + getattr(d, "fee", 0.0), 2),
                        "ours": d.magic == MAGIC, "comment": d.comment,
                        "signal_id": int(rev[str(d.position_id)]) if str(d.position_id) in rev else None})
        return out

    # -------------------------------------------------------------- main loop
    def apply_poll(self, resp: dict):
        self.copy_enabled = bool(resp.get("copy_enabled"))
        old_symbol = self.settings.get("symbol")
        self.settings = resp.get("settings") or self.settings
        self.news = resp.get("news") or []
        if self.status == "online" and self.settings.get("symbol") != old_symbol:
            self.resolve_symbol()
        last = None
        for ev in resp.get("events", []):
            try:
                if ev["kind"] == "signal":
                    self.handle_signal(ev["signal"])
                elif ev["kind"] == "update":
                    self.handle_update(ev["update"])
                elif ev["kind"] == "command":
                    cmd = ev["command"]["name"]
                    if cmd == "close_all":
                        self.close_everything("requested from the website")
                    elif cmd == "restart":
                        self.restart_requested = True
            except Exception as e:  # noqa: BLE001
                log.exception("event %s", ev.get("id"))
                self.note(f"error handling event: {e}")
            last = ev["id"]
        if last is not None:
            self.pending_ack = last

    def poll_server(self, force_report=False):
        now = self.clock()
        body = {"ack": self.pending_ack}
        if force_report or now - self.last_report >= 5:
            snap = now - self.last_snapshot >= 300 and self.status == "online"
            body["report"] = self.report(snapshot=snap)
            self.last_report = now
            if snap:
                self.last_snapshot = now
        resp = self.api.poll(body)
        self.pending_ack = None
        self.apply_poll(resp)

    def sync_deals(self):
        deals = self.collect_deals()
        if deals:
            for i in range(0, len(deals), 200):
                self.api.deals(deals[i:i + 200])
        self.state["deals_from"] = self.clock() - 2 * 86400
        self.last_deals = self.clock()
        self.save_state()

    def step(self):
        """One pass of the loop (also used by tests)."""
        if self.status == "online":
            self.check_protections()
            self.manage_signals()
        self.poll_server()
        if self.status == "online" and self.clock() - self.last_deals >= 60:
            self.sync_deals()

    def run(self):
        backoff = 15
        while not self.connect():
            # a wrong password: wait 30 min between tries so the broker doesn't lock the account.
            # The node restarts this worker straight away when the customer fixes it on the website.
            pause = 1800 if self.last_code == -6 else backoff
            end = time.time() + pause
            while time.time() < end:
                try:
                    self.poll_server(force_report=True)
                except Exception:  # noqa: BLE001
                    pass
                if self.restart_requested:
                    return 3
                if self.unknown_server and self.library_has_more():
                    break                    # another account on this VPS just learned more brokers
                time.sleep(15)
            backoff = min(backoff * 2, 300)
        self.note("connected")
        errors = 0
        while True:
            try:
                if self.connected():
                    self.offline_since = None
                elif self.offline_since is None:
                    self.offline_since = self.clock()       # MT5 reconnects to the broker by itself; give it time
                elif self.clock() - self.offline_since > RECONNECT_AFTER:
                    self.offline_since = None
                    self.status, self.error = "starting", "MT5 lost connection, reconnecting"
                    self.mt5.shutdown()
                    time.sleep(5)
                    if self.connect():
                        self.note("reconnected")
                self.step()
                errors = 0
            except urllib.error.URLError as e:
                errors += 1
                log.warning("server unreachable: %s", e)
                time.sleep(min(30, 2 * errors))
            except Exception:  # noqa: BLE001
                errors += 1
                log.exception("loop")
                time.sleep(min(30, 2 * errors))
            if self.restart_requested:
                self.mt5.shutdown()
                return 3
            time.sleep(0.5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account-id", type=int, required=True)
    ap.add_argument("--terminal", default="")
    ap.add_argument("--state", default="")
    args = ap.parse_args()
    login = os.environ["PF_MT5_LOGIN"]
    password = os.environ.pop("PF_MT5_PASSWORD")
    server = os.environ["PF_MT5_SERVER"]
    logging.basicConfig(level=logging.INFO, format=f"%(asctime)s [acc {args.account_id}] %(message)s")
    import MetaTrader5 as mt5
    api = ServerAPI(os.environ["PF_SERVER_URL"], os.environ["PF_NODE_TOKEN"], args.account_id)
    state = Path(args.state or (Path(args.terminal).parent / "pipsfarmer_state.json"))
    w = Worker(mt5, api, args.account_id, int(login), password, server, args.terminal or None, state,
               library=os.environ.get("PF_SERVERS_LIBRARY") or None)
    sys.exit(w.run())


if __name__ == "__main__":
    main()

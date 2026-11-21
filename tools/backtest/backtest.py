"""Replay Telegram gold calls against M1 candles and compare ways of copying them.

Inputs (from the VPS, data/research): messages-<chat>.jsonl and prices-acc<id>.csv.
Calls are parsed with PipsFarmer's own parser, so the backtest reads them exactly like the live system.
All results are per 1.00 lot on XAUUSD ($1 move = $100), after spread. Candle ambiguity (SL and TP inside the
same minute) always counts as the SL first: results lean pessimistic, never optimistic.
"""
from __future__ import annotations

import bisect
import re
import csv
import json
import sys
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from server import parser as P  # noqa: E402

LOT = 100.0           # $ per $1 move per 1.00 lot


# ------------------------------------------------------------------ prices
class Prices:
    DST_END = 1793570400      # 2026-11-01 (US clocks back): IC Markets server UTC+3 before, UTC+2 after

    def __init__(self, path: Path, offset_s: int | None = None):
        t, o, h, l, c, sp = [], [], [], [], [], []
        seen = set()
        with open(path) as f:
            for r in csv.DictReader(f):
                raw = int(r["time"])
                off = offset_s if offset_s is not None else (10800 if raw - 10800 < self.DST_END else 7200)
                ts = raw - off                           # broker time -> UTC
                if ts in seen:
                    continue
                seen.add(ts)
                t.append(ts); o.append(float(r["open"])); h.append(float(r["high"])); l.append(float(r["low"]))
                c.append(float(r["close"])); sp.append(int(r["spread"]) * 0.01)
        idx = sorted(range(len(t)), key=t.__getitem__)
        self.t = [t[i] for i in idx]; self.o = [o[i] for i in idx]; self.h = [h[i] for i in idx]
        self.l = [l[i] for i in idx]; self.c = [c[i] for i in idx]; self.sp = [sp[i] for i in idx]

    def at(self, ts: float) -> int:
        """Index of the first candle starting at/after ts."""
        return bisect.bisect_left(self.t, int(ts) // 60 * 60)

    def __len__(self):
        return len(self.t)


# ------------------------------------------------------------------ messages -> calls
@dataclass
class Call:
    group: str
    msg_id: int
    ts: float
    side: str
    lo: float
    hi: float
    sl: float | None
    tps: list
    instant: bool
    kind: str                    # now / zone / stop
    text: str
    updates: list = field(default_factory=list)   # (ts, kind, value)
    zone_after: bool = False


def load_calls(path: Path) -> list[Call]:
    msgs = [json.loads(x) for x in open(path, encoding="utf-8")]
    msgs.sort(key=lambda m: (m["date"], m["id"]))
    calls: list[Call] = []
    by_id: dict[int, Call] = {}
    for m in msgs:
        ts = datetime.fromisoformat(m["date"]).timestamp()
        text = m["text"] or ""
        p = P.parse_signal(text)
        if p:
            raw = text.upper()
            if re.search(r"\b(BUY|SELL)\s*STOP\b", raw):
                kind = "stop"
            elif p.instant or not p.entry_low or re.search(r"\bNOW\b", raw):
                kind = "now"
            elif re.search(r"(BUY|SELL)\s*(📈|📉)|ENTRY\s*-", text.upper()):
                kind = "scalp"
            else:
                kind = "zone"
            # a quick "BUY GOLD!" followed within 5 min by the levels: one trade, entered at the quick call
            prev = calls[-1] if calls else None
            if (prev and prev.kind == "now" and not prev.sl and prev.side == p.side and ts - prev.ts < 300
                    and p.sl):
                prev.sl, prev.tps, prev.lo, prev.hi, prev.zone_after = p.sl, list(p.tps), p.entry_low, p.entry_high, True
                by_id[m["id"]] = prev
                continue
            c = Call(m["title"], m["id"], ts, p.side, p.entry_low, p.entry_high, p.sl, list(p.tps), p.instant, kind, text)
            calls.append(c)
            by_id[m["id"]] = c
            continue
        ups = P.parse_update(text)
        if ups:
            target = by_id.get(m.get("reply_to")) if m.get("reply_to") else None
            if target is None:            # unreplied update: latest call of this group in the last 6 h
                for c in reversed(calls):
                    if ts - c.ts < 6 * 3600:
                        target = c
                    break
            if target:
                for k, v in ups:
                    target.updates.append((ts, k, v))
    return calls


# ------------------------------------------------------------------ one trade
@dataclass
class Rules:
    name: str = "base"
    entry: str = "market"          # market | zone_edge (best price) | zone_mid | first_touch
    max_wait_min: int = 120        # a limit not filled within this is cancelled
    sl_mode: str = "signal"        # signal | fixed
    sl_pips: float = 50            # fixed stop / temp stop when the call has none (10 pips = $1)
    tp: str = "tp1"                # tp1 | tp2 | tp3 | split | runner
    be_after_tp1: bool = True
    be_lock_pips: float = 0
    follow_be: bool = False        # apply the group's breakeven / risk-free messages
    follow_close: bool = False
    max_hold_min: int = 24 * 60
    min_sl_pips: float = 0
    need_sl: bool = False
    kinds: tuple = ("now", "zone", "stop", "scalp")
    commission: float = 7.0        # $ per 1.00 lot round trip (IC Markets Raw)
    now_entry: str = "market"      # for quick calls that got levels: market at the call | wait for the zone


def simulate(c: Call, px: Prices, R: Rules) -> dict | None:
    if c.kind not in R.kinds:
        return None
    i = px.at(c.ts)
    if i >= len(px) - 2:
        return None
    buy = c.side == "buy"
    sgn = 1 if buy else -1
    if c.lo and abs((c.lo + c.hi) / 2 - px.o[i]) / px.o[i] > 0.015:
        return None                                   # entry far from price: a typo or another market (live skips it)
    # ---- entry
    in_zone = bool(c.lo) and min(c.lo, c.hi) - 2 <= px.o[i] <= max(c.lo, c.hi) + 2
    use_market = (not c.lo or (c.kind == "now" and R.now_entry == "market")
                  or (R.entry == "market" and in_zone))
    if R.entry == "market" and not use_market:
        R = Rules(**{**R.__dict__, "entry": "first_touch"})       # price away from the zone: wait for it
    if use_market:
        j = i
        sp = px.sp[j]
        entry = px.o[j] + (sp if buy else 0)          # buy at ask, sell at bid
    else:
        lo, hi = min(c.lo, c.hi), max(c.lo, c.hi)
        lim = {"zone_edge": (lo if buy else hi), "zone_mid": (lo + hi) / 2,
               "first_touch": (hi if buy else lo)}[R.entry]
        j = None
        for k in range(i, min(len(px), i + R.max_wait_min)):
            ask_low = px.l[k] + px.sp[k]
            if (buy and ask_low <= lim) or (not buy and px.h[k] >= lim):
                j = k
                break
            if c.sl and ((buy and px.l[k] <= c.sl) or (not buy and px.h[k] >= c.sl)):
                return {"filled": False}
        if j is None:
            return {"filled": False}
        entry = lim
    # ---- stop and targets
    sl = c.sl if (c.sl and R.sl_mode == "signal") else None
    if R.need_sl and not c.sl:
        return None
    if sl is None:
        sl = entry - sgn * R.sl_pips * 0.1
    risk = abs(entry - sl)
    if risk < R.min_sl_pips * 0.1 or (buy and sl >= entry) or (not buy and sl <= entry):
        return None
    tps = [t for t in c.tps if (t - entry) * sgn > 0]
    if not tps:
        tps = [entry + sgn * risk * x for x in (1, 2, 3)]           # no targets: 1R/2R/3R
    if R.tp == "tp1":
        legs = [(1.0, tps[0])]
    elif R.tp == "tp2":
        legs = [(1.0, tps[min(1, len(tps) - 1)])]
    elif R.tp == "tp3":
        legs = [(1.0, tps[min(2, len(tps) - 1)])]
    elif R.tp == "split":
        n = min(3, len(tps)); legs = [(1.0 / n, tps[k]) for k in range(n)]
    else:   # runner: half at TP1, rest trails to TP3 or beyond
        legs = [(0.5, tps[0]), (0.5, tps[min(2, len(tps) - 1)])]
    open_legs = list(legs)
    pnl, stop = 0.0, sl
    upd = sorted(c.updates)
    ui = 0
    end = min(len(px), j + R.max_hold_min)
    hit_tp1 = False
    for k in range(j, end):
        t = px.t[k]
        # group management messages that arrived by this candle
        while ui < len(upd) and upd[ui][0] <= t:
            _, kind, val = upd[ui]; ui += 1
            if R.follow_be and kind in ("breakeven",):
                nb = entry + sgn * R.be_lock_pips * 0.1
                cur = px.c[k - 1] if k > j else entry
                if (cur - nb) * sgn > 0 and (nb - stop) * sgn > 0:
                    stop = nb
            if R.follow_be and kind == "sl_move" and val:
                if (val - stop) * sgn > 0 and ((px.c[k - 1] if k > j else entry) - val) * sgn > 0:
                    stop = val
            if R.follow_close and kind in ("close",):
                for w, _tp in open_legs:
                    pnl += w * (px.o[k] - entry) * sgn
                return {"filled": True, "pnl": pnl * LOT - R.commission, "risk": risk, "exit": "group close", "t": c.ts}
        lo_, hi_ = px.l[k], px.h[k]
        # stop first (pessimistic)
        if (buy and lo_ <= stop) or (not buy and hi_ + px.sp[k] >= stop):
            for w, _tp in open_legs:
                pnl += w * (stop - entry) * sgn
            return {"filled": True, "pnl": pnl * LOT - R.commission, "risk": risk, "exit": "sl" if stop == sl else "be", "t": c.ts}
        still = []
        for w, tp in open_legs:
            if (buy and hi_ >= tp) or (not buy and lo_ + px.sp[k] <= tp):
                pnl += w * (tp - entry) * sgn
                hit_tp1 = True
            else:
                still.append((w, tp))
        open_legs = still
        if hit_tp1 and R.be_after_tp1:
            nb = entry + sgn * R.be_lock_pips * 0.1
            if (nb - stop) * sgn > 0:
                stop = nb
        if not open_legs:
            return {"filled": True, "pnl": pnl * LOT - R.commission, "risk": risk, "exit": "tp", "t": c.ts}
    k = end - 1
    for w, _tp in open_legs:
        pnl += w * (px.c[k] - entry) * sgn
    return {"filled": True, "pnl": pnl * LOT - R.commission, "risk": risk, "exit": "time", "t": c.ts}


def run(calls: list[Call], px: Prices, R: Rules, dedupe_min: int = 15) -> dict:
    res, last = [], {}
    for c in calls:
        key = (c.group, c.side)
        if key in last and c.ts - last[key][0] < dedupe_min * 60 and abs((c.lo or 0) - (last[key][1] or 0)) < 6:
            continue                                   # a re-post / follow-up of the same idea
        last[key] = (c.ts, c.lo)
        r = simulate(c, px, R)
        if r and r.get("filled"):
            res.append(r)
    pn = [r["pnl"] for r in res]
    eq, peak, dd = 0.0, 0.0, 0.0
    for x in pn:
        eq += x; peak = max(peak, eq); dd = max(dd, peak - eq)
    wins = [x for x in pn if x > 0]; losses = [-x for x in pn if x < 0]
    return {"rules": R.name, "trades": len(pn), "win%": round(100 * len(wins) / len(pn)) if pn else 0,
            "net$": round(sum(pn)), "pf": round(sum(wins) / sum(losses), 2) if losses else float("inf"),
            "maxDD$": round(dd), "avg$": round(sum(pn) / len(pn), 1) if pn else 0}


def trades(calls, px, R, dedupe_min=15):
    out, last = [], {}
    for c in calls:
        key = (c.group, c.side)
        if key in last and c.ts - last[key][0] < dedupe_min * 60 and abs((c.lo or 0) - (last[key][1] or 0)) < 6:
            continue
        last[key] = (c.ts, c.lo)
        r = simulate(c, px, R)
        if r and r.get("filled"):
            out.append((c, r))
    return out

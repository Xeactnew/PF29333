"""Pure trading maths used by the MT5 worker (no MT5 calls, easy to test)."""
from __future__ import annotations

import math

PIP = 0.1  # gold: 1 pip = 0.10


def layer_levels(side: str, lo: float, hi: float, n: int) -> list[float]:
    """Entry price for each layer. Buys fill from the top of the zone down,
    sells from the bottom up, so layer 1 is the first price touched."""
    n = max(1, n)
    first, last = (hi, lo) if side == "buy" else (lo, hi)
    if n == 1 or first == last:
        return [round(first, 2)] * n
    step = (last - first) / (n - 1)
    return [round(first + step * i, 2) for i in range(n)]


def layer_tps(tps: list[float], n: int, target: str) -> list[float]:
    """Take profit for each layer (0 = none)."""
    if not tps:
        return [0.0] * n
    if target == "last":
        return [tps[-1]] * n
    if target in ("tp1", "tp2", "tp3"):
        k = min(int(target[2]) - 1, len(tps) - 1)
        return [tps[k]] * n
    # spread: share layers across the targets, earliest layers take the nearest TP
    if n <= len(tps):
        return [tps[min(i, len(tps) - 1)] for i in range(n)] if n > 1 else [tps[0]]
    return [tps[min(len(tps) - 1, i * len(tps) // n)] for i in range(n)]


def round_lot(lot: float, step: float, vmin: float, vmax: float) -> float:
    if step <= 0:
        step = 0.01
    lot = math.floor(lot / step + 1e-9) * step
    lot = min(lot, vmax)
    decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(lot, decimals) if lot >= vmin - 1e-9 else 0.0


def lot_per_layer(settings: dict, n_layers: int, balance: float, loss_per_lot: list[float],
                  step: float, vmin: float, vmax: float) -> tuple[float, str]:
    """Same lot on every layer. loss_per_lot[i] = money lost by 1.0 lot of layer i at SL.
    Returns (lot, note). lot 0 means skip."""
    cap = min(vmax, float(settings.get("max_lot") or vmax))
    mode = settings.get("lot_mode", "fixed")
    if mode == "fixed":
        lot = round_lot(float(settings.get("fixed_lot", 0.01)), step, vmin, cap)
        return (lot, "") if lot else (0.0, "fixed lot is below the broker's minimum")
    risk = balance * float(settings.get("risk_percent", 1)) / 100 if mode == "risk_percent" \
        else float(settings.get("risk_amount", 0))
    total_loss_per_lot = sum(abs(x) for x in loss_per_lot)
    if risk <= 0 or total_loss_per_lot <= 0:
        return 0.0, "couldn't work out the lot size"
    lot = round_lot(risk / total_loss_per_lot, step, vmin, cap)
    if not lot:
        need = vmin * total_loss_per_lot
        return 0.0, f"risk {risk:.2f} is too small for {n_layers} layer(s) at the minimum lot (needs {need:.2f})"
    return lot, ""


def entry_action(side: str, order_type: str, level: float, bid: float, ask: float, smart: bool,
                 smart_dist: float) -> str:
    """What to do with one layer right now: market / limit / stop / wait."""
    if order_type == "stop":
        if side == "buy":
            return "market" if ask >= level else ("wait" if smart else "stop")
        return "market" if bid <= level else ("wait" if smart else "stop")
    if side == "buy":
        if ask <= level:
            return "market"
        if smart:
            return "market" if ask - level <= smart_dist else "wait"
        return "limit"
    if bid >= level:
        return "market"
    if smart:
        return "market" if level - bid <= smart_dist else "wait"
    return "limit"


def breakeven_price(side: str, open_price: float, offset: float) -> float:
    return round(open_price + offset if side == "buy" else open_price - offset, 2)


def better_sl(side: str, new: float, current: float) -> bool:
    """Only ever tighten a stop."""
    if not current:
        return True
    return new > current + 1e-9 if side == "buy" else new < current - 1e-9


def tp_reached(side: str, tp: float, bid: float, ask: float) -> bool:
    return bool(tp) and (bid >= tp if side == "buy" else ask <= tp)


def in_session(hhmm_now: str, start: str, end: str) -> bool:
    if start == "00:00" and end in ("23:59", "24:00"):
        return True
    if start <= end:
        return start <= hhmm_now <= end
    return hhmm_now >= start or hhmm_now <= end  # overnight window


def daily_block(settings: dict, start_balance: float, day_pnl: float) -> str | None:
    if start_balance <= 0:
        return None
    loss_lim = [v for v in (float(settings.get("daily_loss_limit") or 0),
                            start_balance * float(settings.get("daily_loss_pct") or 0) / 100) if v > 0]
    prof_lim = [v for v in (float(settings.get("daily_profit_limit") or 0),
                            start_balance * float(settings.get("daily_profit_pct") or 0) / 100) if v > 0]
    if loss_lim and day_pnl <= -min(loss_lim):
        return f"daily loss limit reached ({day_pnl:.2f})"
    if prof_lim and day_pnl >= min(prof_lim):
        return f"daily profit target reached (+{day_pnl:.2f})"
    return None


def news_state(windows: list[dict], now: float, before_min: int, after_min: int) -> tuple[str | None, dict | None]:
    """('blocked text', event) if now is inside a news window."""
    for w in windows:
        t = float(w["time"])
        if t - before_min * 60 <= now <= t + after_min * 60:
            return f"news: {w.get('currency', '')} {w.get('title', '')}".strip(), w
    return None, None


ALGO_KEYS = {"Enabled": "1", "AllowLiveTrading": "1", "Account": "0", "Profile": "0"}


def enable_algo_trading(folder) -> bool:
    """Switch MT5's Algo Trading on in a terminal's Config/common.ini (MT5 must not be running).
    Account=0 / Profile=0 stop MT5 switching it off again whenever it logs in. Returns True if the file changed."""
    from pathlib import Path
    ini = Path(folder) / "Config" / "common.ini"
    ini.parent.mkdir(parents=True, exist_ok=True)
    text = ""
    if ini.exists():
        raw = ini.read_bytes()
        text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8", "ignore")
    out, in_experts, done, seen = [], False, set(), False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("["):
            if in_experts:
                out += [f"{k}={v}" for k, v in ALGO_KEYS.items() if k not in done]
            in_experts = s.lower() == "[experts]"
            seen = seen or in_experts
        elif in_experts and "=" in s and s.split("=")[0] in ALGO_KEYS:
            k = s.split("=")[0]
            done.add(k)
            line = f"{k}={ALGO_KEYS[k]}"
        out.append(line)
    if in_experts:
        out += [f"{k}={v}" for k, v in ALGO_KEYS.items() if k not in done]
    if not seen:
        out += ["[Experts]"] + [f"{k}={v}" for k, v in ALGO_KEYS.items()]
    new = ("\r\n".join(out) + "\r\n").encode("utf-16")
    if ini.exists() and ini.read_bytes() == new:
        return False
    ini.write_bytes(new)
    return True

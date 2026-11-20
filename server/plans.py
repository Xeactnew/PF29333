"""Plans, what each plan unlocks, and per-account trade settings.

Everything here is editable later from the admin page (Plans). These are only the
starting values.
"""
from __future__ import annotations

import re

# What a plan can switch on. type: bool / int / lot_modes
FEATURES = [
    {"key": "max_accounts", "label": "MT5 accounts", "type": "int"},
    {"key": "max_channels", "label": "Extra Telegram channels", "type": "int"},
    {"key": "max_layers", "label": "Max layers per signal", "type": "int"},
    {"key": "manual_signals", "label": "Manual signals", "type": "bool"},
    {"key": "breakeven", "label": "Breakeven at TP1", "type": "bool"},
    {"key": "risk_modes", "label": "Risk % / fixed-money lot sizing", "type": "bool"},
    {"key": "daily_limits", "label": "Daily loss / profit limits", "type": "bool"},
    {"key": "news_protection", "label": "News protection", "type": "bool"},
    {"key": "signal_filters", "label": "Signal filters (direction, max SL, hours, spread)", "type": "bool"},
    {"key": "smart_entry", "label": "Smart entry", "type": "bool"},
    {"key": "tp_control", "label": "Choose TP targets per layer", "type": "bool"},
]

UNLIMITED = 999

DEFAULT_PLANS = [
    {
        "key": "standard", "name": "Standard", "monthly_price": 0, "lifetime_price": 0, "sort": 1,
        "features": {
            "max_accounts": 1, "max_channels": 0, "max_layers": 1,
            "manual_signals": True, "breakeven": True, "risk_modes": False,
            "daily_limits": False, "news_protection": False, "signal_filters": False,
            "smart_entry": False, "tp_control": False,
        },
    },
    {
        "key": "pro", "name": "Pro", "monthly_price": 49, "lifetime_price": 299, "sort": 2,
        "features": {
            "max_accounts": 3, "max_channels": 5, "max_layers": 5,
            "manual_signals": True, "breakeven": True, "risk_modes": True,
            "daily_limits": True, "news_protection": True, "signal_filters": True,
            "smart_entry": False, "tp_control": True,
        },
    },
    {
        "key": "extreme", "name": "Extreme", "monthly_price": 99, "lifetime_price": 599, "sort": 3,
        "features": {
            "max_accounts": 10, "max_channels": UNLIMITED, "max_layers": 10,
            "manual_signals": True, "breakeven": True, "risk_modes": True,
            "daily_limits": True, "news_protection": True, "signal_filters": True,
            "smart_entry": True, "tp_control": True,
        },
    },
]


def feature(plan_features: dict, key: str):
    default = next((p["features"][key] for p in DEFAULT_PLANS if p["key"] == "standard"), None)
    return plan_features.get(key, default)


# ------------------------------------------------------------ account settings

DEFAULT_ACCOUNT_SETTINGS = {
    "symbol": "",                 # blank = find the gold symbol automatically
    "lot_mode": "fixed",          # fixed / risk_percent / risk_amount
    "fixed_lot": 0.01,            # per layer
    "risk_percent": 1.0,          # of balance, whole signal (split across layers)
    "risk_amount": 10.0,          # account currency, whole signal
    "max_lot": 1.0,               # safety cap per order
    "max_open_lots": 0.0,         # most lots open + pending + waiting at once on the account; 0 = no cap
    "layers": 1,
    "tp_target": "spread",        # spread / tp1 / tp2 / tp3 / last
    "breakeven_at_tp1": True,
    "be_offset_pips": 1.0,
    "cancel_pending_after_tp1": True,
    "cancel_pending_on_be": True,
    "follow_updates": True,       # act on channel "close / BE / SL hit" messages
    "require_sl": True,           # signals without a stop loss wait (30 min) for the group to post one
    "instant_entry": True,        # "BUY NOW" calls enter straight away with a temporary stop
    "default_sl_pips": 50.0,      # that temporary stop, or the stop used when require_sl is off
    "smart_entry": False,
    "smart_distance_pips": 20.0,  # enter when price is this close to the entry
    "entry_expiry_min": 120,      # give up on unfilled entries after this long
    "daily_loss_limit": 0.0,      # account currency, 0 = off
    "daily_loss_pct": 0.0,        # % of day-start balance, 0 = off
    "daily_profit_limit": 0.0,
    "daily_profit_pct": 0.0,
    "close_on_limit": True,
    "news_protection": False,
    "news_before_min": 15,
    "news_after_min": 15,
    "news_close_trades": False,
    "news_currencies": "USD",
    "directions": "both",         # both / buy / sell
    "max_sl_pips": 0.0,           # 0 = any
    "max_spread_pips": 0.0,       # 0 = any
    "max_open_signals": 0,        # 0 = any
    "session_start": "00:00",     # broker server time
    "session_end": "23:59",
    "skip_if_tp1_passed": True,
    "one_trade_per_idea": True,   # the same call from two groups (a re-post with a slightly different zone) is taken once
    "min_sl_pips": 20.0,          # skip calls whose stop is tighter than this (spread alone can hit it); 0 = any
    "max_entry_away_pct": 1.5,    # skip calls whose entry is this far from the price (a typo like 3616 for 3716)
    "require_tp": False,          # skip calls with no take profit at all
}

LOT_MODES = ("fixed", "risk_percent", "risk_amount")
TP_TARGETS = ("spread", "tp1", "tp2", "tp3", "last")
DIRECTIONS = ("both", "buy", "sell")


def _num(v, default, lo=None, hi=None, cast=float):
    try:
        v = cast(v)
    except (TypeError, ValueError):
        return default
    if lo is not None and v < lo:
        v = lo
    if hi is not None and v > hi:
        v = hi
    return v


def _hhmm(v, default):
    try:
        h, m = str(v).split(":")
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return f"{h:02d}:{m:02d}"
    except ValueError:
        pass
    return default


def clean_settings(raw: dict) -> dict:
    """Validate customer input into a full settings dict."""
    d = dict(DEFAULT_ACCOUNT_SETTINGS)
    raw = raw or {}
    d["symbol"] = str(raw.get("symbol", d["symbol"]))[:32].strip()
    d["lot_mode"] = raw.get("lot_mode") if raw.get("lot_mode") in LOT_MODES else d["lot_mode"]
    d["fixed_lot"] = round(_num(raw.get("fixed_lot"), d["fixed_lot"], 0.01, 100), 2)
    d["risk_percent"] = _num(raw.get("risk_percent"), d["risk_percent"], 0.01, 20)
    d["risk_amount"] = _num(raw.get("risk_amount"), d["risk_amount"], 0.01, 1_000_000)
    d["max_lot"] = round(_num(raw.get("max_lot"), d["max_lot"], 0.01, 100), 2)
    d["max_open_lots"] = round(_num(raw.get("max_open_lots"), d["max_open_lots"], 0, 1000), 2)
    d["layers"] = _num(raw.get("layers"), d["layers"], 1, 20, int)
    d["tp_target"] = raw.get("tp_target") if raw.get("tp_target") in TP_TARGETS else d["tp_target"]
    for b in ("breakeven_at_tp1", "cancel_pending_after_tp1", "cancel_pending_on_be", "follow_updates", "require_sl", "instant_entry",
              "smart_entry", "close_on_limit", "news_protection", "news_close_trades",
              "skip_if_tp1_passed", "one_trade_per_idea", "require_tp"):
        if b in raw:
            d[b] = raw[b] in (True, "1", "on", "true", 1)
    d["be_offset_pips"] = _num(raw.get("be_offset_pips"), d["be_offset_pips"], 0, 100)
    d["default_sl_pips"] = _num(raw.get("default_sl_pips"), d["default_sl_pips"], 5, 2000)
    d["smart_distance_pips"] = _num(raw.get("smart_distance_pips"), d["smart_distance_pips"], 1, 1000)
    d["entry_expiry_min"] = _num(raw.get("entry_expiry_min"), d["entry_expiry_min"], 1, 7 * 24 * 60, int)
    for k in ("daily_loss_limit", "daily_profit_limit"):
        d[k] = _num(raw.get(k), d[k], 0, 1_000_000_000)
    for k in ("daily_loss_pct", "daily_profit_pct"):
        d[k] = _num(raw.get(k), d[k], 0, 100)
    d["news_before_min"] = _num(raw.get("news_before_min"), d["news_before_min"], 0, 240, int)
    d["news_after_min"] = _num(raw.get("news_after_min"), d["news_after_min"], 0, 240, int)
    cur = str(raw.get("news_currencies", d["news_currencies"])).upper()
    d["news_currencies"] = ",".join(c.strip() for c in cur.split(",") if c.strip().isalpha())[:40] or "USD"
    d["directions"] = raw.get("directions") if raw.get("directions") in DIRECTIONS else d["directions"]
    d["max_sl_pips"] = _num(raw.get("max_sl_pips"), d["max_sl_pips"], 0, 10000)
    d["min_sl_pips"] = _num(raw.get("min_sl_pips"), d["min_sl_pips"], 0, 1000)
    d["max_entry_away_pct"] = _num(raw.get("max_entry_away_pct"), d["max_entry_away_pct"], 0, 50)
    d["max_spread_pips"] = _num(raw.get("max_spread_pips"), d["max_spread_pips"], 0, 1000)
    d["max_open_signals"] = _num(raw.get("max_open_signals"), d["max_open_signals"], 0, 100, int)
    d["session_start"] = _hhmm(raw.get("session_start", d["session_start"]), "00:00")
    d["session_end"] = _hhmm(raw.get("session_end", d["session_end"]), "23:59")
    d["groups"] = clean_groups(raw.get("groups"))
    return d


GROUP_KEYS = ("lot_mode", "fixed_lot", "risk_percent", "risk_amount", "layers", "tp_target")


def clean_groups(raw) -> dict:
    """Per signal-group overrides: {source_id: {"paused": bool, lot/risk/layers/tp...}}."""
    out = {}
    if not isinstance(raw, dict):
        return out
    for sid, g in list(raw.items())[:50]:
        if not re.fullmatch(r"-?\d+|s\d+", str(sid)) or not isinstance(g, dict):
            continue
        c = {}
        if g.get("paused"):
            c["paused"] = True
        if g.get("own"):
            c["own"] = True
            c["lot_mode"] = g.get("lot_mode") if g.get("lot_mode") in LOT_MODES else "fixed"
            c["fixed_lot"] = round(_num(g.get("fixed_lot"), 0.01, 0.01, 100), 2)
            c["risk_percent"] = _num(g.get("risk_percent"), 1.0, 0.01, 20)
            c["risk_amount"] = _num(g.get("risk_amount"), 10.0, 0.01, 1_000_000)
            c["layers"] = _num(g.get("layers"), 1, 1, 20, int)
            c["tp_target"] = g.get("tp_target") if g.get("tp_target") in TP_TARGETS else "spread"
        if c:
            out[str(sid)] = c
    return out


def effective_settings(settings: dict, plan_features: dict) -> dict:
    """Settings with anything the plan doesn't include switched back to its default."""
    d = clean_settings(settings)
    base = DEFAULT_ACCOUNT_SETTINGS
    f = lambda k: feature(plan_features, k)  # noqa: E731
    if not f("risk_modes"):
        d["lot_mode"] = "fixed"
    d["layers"] = max(1, min(d["layers"], int(f("max_layers") or 1)))
    if not f("breakeven"):
        d["breakeven_at_tp1"] = False
    if not f("daily_limits"):
        for k in ("daily_loss_limit", "daily_loss_pct", "daily_profit_limit", "daily_profit_pct"):
            d[k] = 0.0
    if not f("news_protection"):
        d["news_protection"] = False
        d["news_close_trades"] = False
    if not f("signal_filters"):
        for k in ("directions", "max_sl_pips", "max_spread_pips", "max_open_signals",
                  "session_start", "session_end"):
            d[k] = base[k]
    if not f("smart_entry"):
        d["smart_entry"] = False
    if not f("tp_control"):
        d["tp_target"] = "spread"
    for g in d["groups"].values():          # a group's own settings stay inside the plan too
        if "layers" in g:
            g["layers"] = max(1, min(g["layers"], int(f("max_layers") or 1)))
        if not f("risk_modes") and "lot_mode" in g:
            g["lot_mode"] = "fixed"
        if not f("tp_control") and "tp_target" in g:
            g["tp_target"] = "spread"
    return d


def locked_keys(plan_features: dict) -> set[str]:
    """Setting names the customer's plan doesn't unlock (greyed out in the form)."""
    f = lambda k: feature(plan_features, k)  # noqa: E731
    locked = set()
    if not f("risk_modes"):
        locked |= {"risk_percent", "risk_amount", "lot_mode"}
    if not f("breakeven"):
        locked |= {"breakeven_at_tp1", "be_offset_pips"}
    if not f("daily_limits"):
        locked |= {"daily_loss_limit", "daily_loss_pct", "daily_profit_limit", "daily_profit_pct", "close_on_limit"}
    if not f("news_protection"):
        locked |= {"news_protection", "news_before_min", "news_after_min", "news_close_trades", "news_currencies"}
    if not f("signal_filters"):
        locked |= {"directions", "max_sl_pips", "max_spread_pips", "max_open_signals", "session_start", "session_end"}
    if not f("smart_entry"):
        locked |= {"smart_entry", "smart_distance_pips"}
    if not f("tp_control"):
        locked |= {"tp_target"}
    if int(f("max_layers") or 1) <= 1:
        locked |= {"layers"}
    return locked

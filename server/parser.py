"""Reads gold signals and trade-management messages from Telegram text.

parse_signal("XAUUSD BUY ZONE : 4037-4030\nSL: 4027\nTP1: 4047 ...")
  -> ParsedSignal(side='buy', entry_low=4030, entry_high=4037, sl=4027, tps=[4047, ...])

parse_update("TP2 HIT, SET BREAKEVEN")
  -> [("tp_hit", 2), ("breakeven", None)]
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

GOLD_RE = re.compile(r"\b(xau\s*/?\s*usd\w*|gold|xau)\b")
OTHER_SYMBOL_RE = re.compile(
    r"\b(eur|gbp|usd|jpy|aud|nzd|cad|chf){2}\b|\b(btc|eth|nas\s*100|us\s*30|us\s*100|spx|ger\s*40|dax|"
    r"oil|wti|xag|silver|nasdaq|dow)\w*\b")
NUM = r"\d+(?:\.\d+)?"

HIT_EMOJI = "✅✔☑🎯💰🤑🔥💸🥳🎉"
MISS_EMOJI = "❌🛑⛔"


@dataclass
class ParsedSignal:
    side: str
    entry_low: float
    entry_high: float
    sl: float | None
    tps: list[float] = field(default_factory=list)
    order_type: str = "market"   # market / limit / stop
    symbol: str = "XAUUSD"
    instant: bool = False        # "BUY NOW": enter straight away; entry 0 = at the current price

    @property
    def entry_mid(self) -> float:
        return (self.entry_low + self.entry_high) / 2

    def as_dict(self) -> dict:
        return {"side": self.side, "entry_low": self.entry_low, "entry_high": self.entry_high,
                "sl": self.sl, "tps": self.tps, "order_type": self.order_type, "symbol": self.symbol,
                "instant": self.instant}


def normalise(text: str) -> str:
    t = text or ""
    for ch in HIT_EMOJI:
        t = t.replace(ch, " hit ")
    for ch in MISS_EMOJI:
        t = t.replace(ch, " miss ")
    t = t.replace("–", "-").replace("—", "-").replace("−", "-")
    t = re.sub(r"[*_`~|]+", " ", t)
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", t)          # 4,037 -> 4037
    t = re.sub(r"[^\x00-\x7f]", " ", t)                  # drop other emoji
    t = t.lower()
    t = re.sub(r"[ \t]+", " ", t)
    return t


def _expand_short(full: float, short: float) -> float:
    """'4037-30' -> 4030: rebuild a shortened second price from the first."""
    if short >= 100 or full < 1000:
        return short
    base = int(full // 100) * 100
    candidates = [base - 100 + short, base + short, base + 100 + short]
    return min(candidates, key=lambda c: abs(c - full))


SL_RE = re.compile(rf"(?:\bs\s*/\s*l\b|\bsl\b|\bstop\s*-?\s*loss\b|\bstoploss\b|\bstop\b)\s*[:=@\-]?\s*(?:at\s*|@\s*)?({NUM})")
TP_RE = re.compile(
    rf"(?:\btp|\bt\.p\.?|\btake\s*-?\s*profit|\btarget)\s*(?:(\d)(?!\d))?\s*[:=@\-.)]?\s*(?:at\s*|@\s*)?({NUM})"
    rf"((?:\s*(?:/|,|-|&|and)\s*{NUM})*)(\s*pips?)?")


NOW_WORDS = r"(?:now|market\s*now|at\s*market|asap|instantly|instant|right\s*now|here)"
NOW_RE = re.compile(
    r"^[\s\W]*(?:(?:i'?m\s*|we'?re\s*)?(?:xau\s*/?\s*usd\w*|gold|xau)\s*[:\-]?\s*)?(?:i'?m\s*|we'?re\s*)?"
    r"(buy|sell|long|short)(?:ing|s)?\s*"
    r"(?:(?:xau\s*/?\s*usd\w*|gold|xau|it)\s*)?" + NOW_WORDS + r"\b(?!\s*(?:if|when|once|after|or)\b)",
    re.MULTILINE)


# the whole message is just the call: "XAUUSD BUY 🔥", "BUY GOLD", "Gold sell"
BARE_RE = re.compile(r"^[\s\W]*(?:(?:xau\s*/?\s*usd\w*|gold|xau)\s*[:\-]?\s*(buy|sell)(?:ing)?|"
                     r"(buy|sell)(?:ing)?\s*(?:xau\s*/?\s*usd\w*|gold|xau))[\s\W]*$")


def is_instant_call(t: str) -> bool:
    """A line that *starts* with the call: 'XAUUSD BUY NOW', 'Gold: sell now', 'BUY GOLD NOW 🔥',
    'Buying gold now', or a message that is only the call ('XAUUSD BUY 🔥').
    Chat such as 'I will buy now if it breaks' or 'should I buy now?' isn't a call."""
    if BARE_RE.match(t.strip()):
        return True
    m = NOW_RE.search(t)
    if not m:
        return False
    line_end = t.find("\n", m.end())
    return "?" not in t[m.start():line_end if line_end >= 0 else len(t)]


def _plausible(n: float, anchor: float | None) -> bool:
    if anchor:
        return abs(n - anchor) <= anchor * 0.1
    return 1000 <= n <= 20000


def parse_signal(text: str, assume_gold: bool = True) -> ParsedSignal | None:
    t = normalise(text)
    if not t.strip():
        return None
    has_gold = bool(GOLD_RE.search(t))
    if not has_gold and (OTHER_SYMBOL_RE.search(t) or not assume_gold):
        return None
    # a results / highlights post ("🏆 HIGHLIGHT … TP 3738 ✅ TP 3743 ✅") is a recap, not a new trade
    if re.search(r"\bhighlights?\b|\brecap\b|\bresults?\s*(?:of\s*the\s*day|today)\b", t) or \
            len(re.findall(r"\btp\s*\d?\s*[@:]?\s*\d{3,5}(?:\.\d+)?\s*hit\b", t)) >= 2:
        return None

    m = re.search(r"\b(buy|sell|long|short)(?:ing|s)?\b", t)
    if not m:
        return None
    side = "buy" if m.group(1) in ("buy", "long") else "sell"

    order_type = "market"
    if re.search(r"\b(buy|sell)\s*limit\b", t):
        order_type = "limit"
    elif re.search(r"\b(buy|sell)\s*stop\b", t):
        order_type = "stop"
    t2 = re.sub(r"\b(buy|sell)\s*(limit|stop)\b", r"\1", t)

    # SL
    sl = None
    for sm in SL_RE.finditer(t2):
        sl = float(sm.group(1))
        break
    t3 = SL_RE.sub(" ", t2)

    # TPs (prices, or pips if followed by "pips")
    tp_prices: list[float] = []
    tp_pips: list[float] = []
    for tm in TP_RE.finditer(t3):
        vals = [float(tm.group(2))] + [float(x) for x in re.findall(NUM, tm.group(3) or "")]
        (tp_pips if tm.group(4) else tp_prices).extend(vals)
    t4 = TP_RE.sub(" ", t3)
    t4 = re.sub(r"\btp\s*\d?\s*[:=\-]?\s*open\b", " ", t4)

    # entry: first plausible numbers left after removing SL/TP parts
    anchor = sl or (tp_prices[0] if tp_prices else None)
    t4 = re.sub(r"\b\d{1,2}:\d{2}\b", " ", t4)          # times
    t4 = re.sub(rf"{NUM}\s*(?:pips?|%|x\b|lots?)", " ", t4)
    nums = [(float(x.group()), x.start(), x.end()) for x in re.finditer(NUM, t4)]
    entry: list[float] = []
    for i, (n, s, e) in enumerate(nums):
        if not entry:
            if _plausible(n, anchor):
                entry.append(n)
                first_end = e
            continue
        between = t4[first_end:s]
        if re.fullmatch(r"\s*(?:-|/|to|~|and|&)\s*", between):
            n2 = _expand_short(entry[0], n)
            if abs(n2 - entry[0]) <= max(entry[0] * 0.01, 5):
                entry.append(n2)
        break
    instant = is_instant_call(t)
    if not entry:
        if not instant or len(re.sub(r"\s+", " ", t).strip()) > 200:
            return None
        if sl is not None or tp_prices:     # "BUY NOW SL 4027 TP 4047": enter at today's price
            if sl and tp_prices and not _plausible(sl, tp_prices[0]):
                return None
        return ParsedSignal(side=side, entry_low=0.0, entry_high=0.0, sl=sl,
                            tps=sorted(set(tp_prices), reverse=side == "sell")[:6] if not tp_pips else [],
                            order_type="market", instant=True)
    lo, hi = min(entry), max(entry)

    pip = 0.1
    if tp_pips and not tp_prices:
        ref = hi if side == "buy" else lo
        tp_prices = [round(ref + p * pip, 2) if side == "buy" else round(ref - p * pip, 2) for p in tp_pips]

    # sanity: a stop more than 3% from the entry is a typo ("SL 4047" on a 3740 sell): drop it and wait for the fix
    if sl is not None and lo and abs(sl - (lo + hi) / 2) / ((lo + hi) / 2) > 0.03:
        sl = None
    # sanity: SL and TPs on the right side of the entry
    if sl is not None:
        if (side == "buy" and sl >= lo) or (side == "sell" and sl <= hi):
            return None
    if side == "buy":
        tps = sorted({tp for tp in tp_prices if tp > lo})
    else:
        tps = sorted({tp for tp in tp_prices if tp < hi}, reverse=True)
    tps = [tp for tp in tps if _plausible(tp, lo)]

    if sl is None and not tps and not instant:
        return None
    return ParsedSignal(side=side, entry_low=lo, entry_high=hi, sl=sl, tps=tps[:6], order_type=order_type,
                        instant=instant and sl is None)


def is_levels_only(text: str) -> bool:
    """A short message that is just a stop (and maybe targets): 'SL 3790', 'SL: 3790 TP 3770'."""
    t = normalise(text)
    rest = TP_RE.sub(" ", SL_RE.sub(" ", t))
    rest = re.sub(r"\b(?:new|updated?|now|guys|pls|please|the|is|our|my)\b|[\W_]+", " ", rest).strip()
    return bool(SL_RE.search(t)) and len(t) <= 80 and not rest


def parse_levels(text: str) -> tuple[float | None, list[float]]:
    """Just the stop loss and targets from a follow-up like 'SL 4027  TP 4047 / 4057'."""
    t = normalise(text)
    sl = None
    for sm in SL_RE.finditer(t):
        sl = float(sm.group(1))
        break
    tps: list[float] = []
    for tm in TP_RE.finditer(SL_RE.sub(" ", t)):
        if tm.group(4):           # pips, not prices
            continue
        tps += [float(tm.group(2))] + [float(x) for x in re.findall(NUM, tm.group(3) or "")]
    if sl is not None and not 100 <= sl <= 100000:
        sl = None
    tps = [x for x in tps if 100 <= x <= 100000]
    return sl, tps


# ------------------------------------------------------------ updates

def parse_update(text: str) -> list[tuple[str, float | None]]:
    t = normalise(text)
    out: list[tuple[str, float | None]] = []

    SLW = r"(?:sl|s/l|stop\s*-?\s*loss|stoploss|stop)"
    sl_patterns = [
        # move SL to 3790 / set stop at 3790 / trail SL to 3790 / adjust your SL @ 3790
        rf"\b(?:move|set|put|adjust|change|trail|update|shift|raise|lower|bring)\w*\s*(?:the\s*|your\s*|our\s*)?{SLW}\s*(?:up\s*|down\s*)?(?:to|@|at|on|:)?\s*({NUM})",
        # SL moved to 3790 / SL now 3790 / stop loss has been moved to 3790 / SL is now at 3790
        rf"\b{SLW}\s*(?:has\s*been\s*|is\s*|was\s*|now\s*)?(?:moved|changed|adjusted|updated|set|trailed|shifted|now|raised|lowered)\s*(?:up\s*|down\s*)?(?:to|at|@|:)?\s*({NUM})",
        # new SL 3790 / updated SL: 3790 / revised stop 3790
        rf"\b(?:new|updated|adjusted|revised|tighter|moved)\s*{SLW}\s*(?:is\s*|now\s*)?(?:to|at|@|:|=|-)?\s*({NUM})",
        # SL to 3790 / Update: SL 3790
        rf"\b{SLW}\s*(?:to|->|=>|→)\s*({NUM})",
        rf"\bupdate\w*\s*[:\-]?\s*{SLW}\s*[:=@\-]?\s*({NUM})",
    ]
    for pat in sl_patterns:
        m = re.search(pat, t)
        if m and float(m.group(1)) > 100:
            out.append(("sl_move", float(m.group(1))))
            break

    if re.search(r"\ball\s*(?:the\s*)?(?:tps?|targets?|take\s*profits?)\b(?:\s*(?:were|are|got|have\s*been|has\s*been))?\s*(?:hit|done|smashed|reached|achieved|secured|closed)"
                 r"|\b(?:full|final)\s*(?:tp|target)\s*(?:hit|done|reached)|\btp\s*all\s*hit", t):
        out.append(("tp_hit", 99))
    else:
        hits = [int(x) for x in re.findall(r"\b(?:tp|target)\s*(\d)\s*(?:is\s*|has\s*been\s*|was\s*|got\s*)?(?:hit|done|smashed|reached|achieved|secured|booked|banked|filled|gone|in\s*profit)", t)]
        hits += [int(x) for x in re.findall(r"\b(?:hit|smashed|reached|secured)\s*(?:the\s*)?(?:tp|target)\s*(\d)", t)]
        if not hits and re.search(r"\b(?:first|1st)\s*(?:tp|target)\s*(?:hit|done|reached)", t):
            hits = [1]
        if hits:
            out.append(("tp_hit", float(max(hits))))

    if re.search(r"\b(?:sl|s/l|stop\s*loss)\s*(?:was\s*|got\s*|has\s*been\s*)?(?:hit|taken|triggered|miss)|\bstopped\s*out\b|\bhit\s*(?:our\s*)?(?:sl|stop)\b", t) and not any(k == "tp_hit" for k, _ in out):
        out.append(("sl_hit", None))

    if re.search(r"\bclose\s*(?:half|50\s*%|partial\w*|some)|\bpartials?\b|\bsecure\s*(?:some|partial|half)", t):
        out.append(("close_partial", 0.5))
    elif re.search(r"\b(?:close|exit)\s*(?:all|now|it|the\s*trade|trades|everything|full\w*|early|position\w*|at\s*market)\b|\bclose\s*(?:and|&)\s*secure|\bsecure\s*(?:all|profits?\s*now)", t):
        out.append(("close", None))

    if re.search(r"\bbreak\s*-?\s*even\b|\bset\s*be\b|\bmove\s*(?:sl\s*|stop\s*)?to\s*(?:be|entry)\b|\bsl\s*(?:to|@|at)\s*(?:be|entry)\b|\brisk\s*-?\s*free\b|\bbe\s*now\b"
                 r"|\bgo\s*(?:to\s*)?be\b|\b(?:put|move|set|place)\s*(?:it\s*|them\s*|trades?\s*|sl\s*|stops?\s*)?(?:to|at|on)\s*be\b"
                 r"|\bsecure\s*(?:at\s*)?be\b|\bbe\s*(?:your|the)\s*(?:trades?|sl|stops?)\b", t) and not re.search(r"\bbe\s*(?:was\s*)?hit\b", t):
        if not any(k == "sl_move" for k, _ in out):
            out.append(("breakeven", None))

    if not out and re.search(r"\b(?:cancel\w*|delete\s*(?:the\s*)?(?:limit|order|pending)|remove\s*(?:the\s*)?(?:limit|order|pending)|ignore\s*(?:this|the)\s*(?:signal|trade)|signal\s*invalid\w*)\b", t):
        out.append(("cancel", None))
    return out


def looks_like_update(text: str) -> bool:
    return bool(parse_update(text))

"""MT5 brokers and their server names, offered as suggestions when customers add an account.

Customers can still type any server: if MT5 doesn't know it, the worker logs in through the
terminal itself, which looks the server up in MetaQuotes' broker directory. Every server that
connects successfully is also learned and suggested to everyone after that (see known_servers).

Checked against broker/help pages in Nov 2026 for Vantage, PU Prime, TradeQuo, IC Markets,
Exness, XM and FundingPips. The rest are the commonly published names; brokers add servers
over time, so the customer's own MT5/email is always the final word.
"""
from __future__ import annotations

import re


def _series(prefix: str, start: int, end: int, sep: str = "") -> list[str]:
    return [f"{prefix}{sep}{i}" for i in range(start, end + 1)]


BROKERS: dict[str, list[str]] = {
    # ---- the ones most PipsFarmer customers use
    "Vantage": ["VantageMarkets-Live", "VantageMarkets-Demo",
                # names before Vantage's 30 May 2026 server rename (still typed by older customers)
                "VantageInternational-Live", "VantageInternational-Live 2", "VantageInternational-Live 3",
                "VantageInternational-Live 4", "VantageInternational-Live 6", "VantageInternational-Demo"],
    "PU Prime": ["PUPrime-Live", "PUPrime-Live 2", "PUPrime-Live 3", "PUPrime-Demo"],
    "TradeQuo": ["TradeQuo-Server", "TradeQuo-Live", "TradeQuo-Demo"],
    "IC Markets": ["ICMarketsSC-MT5", "ICMarketsSC-MT5-2", "ICMarketsSC-MT5-3", "ICMarketsSC-MT5-4",
                   "ICMarketsSC-MT5-5", "ICMarketsSC-MT5-6", "ICMarketsSC-Demo", "ICMarketsEU-MT5",
                   "ICMarketsEU-MT5-2", "ICMarketsEU-MT5-5", "ICMarketsEU-Demo", "ICMarketsAU-Live",
                   "ICMarketsInternational-Demo"],
    # ---- other retail brokers
    "Exness": ["Exness-MT5Real"] + _series("Exness-MT5Real", 2, 40) + ["Exness-MT5Trial"]
              + _series("Exness-MT5Trial", 2, 20) + ["Exness-MT5Demo"],
    "XM": ["XMGlobal-MT5"] + _series("XMGlobal-MT5", 2, 12, " ") + ["XMGlobal-MT5 Demo", "XMTrading-MT5",
                                                                     "XMTrading-MT5 2", "XMTrading-MT5 3"],
    "Pepperstone": ["Pepperstone-MT5-Live01", "Pepperstone-MT5-Live02", "Pepperstone-Demo",
                    "PepperstoneUK-Live", "PepperstoneUK-Demo"],
    "FP Markets": ["FPMarketsSC-Live", "FPMarketsSC-Demo", "FPMarkets-Live", "FPMarkets-Demo",
                   "FPMarketsLLC-Live", "FPMarketsLLC-Demo"],
    "Fusion Markets": ["FusionMarkets-Live", "FusionMarkets-Demo"],
    "Eightcap": ["Eightcap-Live", "Eightcap-Live2", "Eightcap-Demo"],
    "STARTRADER": ["STARTRADERFinancial-Live", "STARTRADERFinancial-Live 2", "STARTRADERFinancial-Demo"],
    "VT Markets": ["VTMarkets-Live", "VTMarkets-Live 2", "VTMarkets-Live 3", "VTMarkets-Demo"],
    "Ultima Markets": ["UltimaMarkets-Live", "UltimaMarkets-Live 1", "UltimaMarkets-Demo"],
    "Tickmill": ["Tickmill-Live", "Tickmill-Live02", "Tickmill-Demo", "TickmillUK-Live", "TickmillUK-Demo"],
    "RoboForex": ["RoboForex-ECN", "RoboForex-Pro", "RoboForex-Demo"],
    "FBS": ["FBS-Real", "FBS-Demo"],
    "HFM": ["HFMarketsGlobal-Live", "HFMarketsGlobal-Live2", "HFMarketsGlobal-Live3", "HFMarketsGlobal-Demo",
            "HFMarketsSV-Live Server", "HFMarketsSV-Demo Server"],
    "FxPro": ["FxPro-MT5", "FxPro-MT5 Live02", "FxPro-MT5 Demo"],
    "Axi": ["Axi-US50-Live", "Axi-US51-Live", "Axi-US03-Demo", "AxiCorp-Live", "AxiCorp-Demo"],
    "ThinkMarkets": ["ThinkMarkets-Live", "ThinkMarkets-Demo"],
    "BlackBull Markets": ["BlackBullMarkets-Live", "BlackBullMarkets-Demo"],
    "Admirals": ["AdmiralsGroup-Live", "AdmiralsGroup-Demo", "AdmiralsSC-Live", "AdmiralsSC-Demo"],
    "FXTM": ["ForexTimeFXTM-Live01", "ForexTimeFXTM-Live02", "ForexTimeFXTM-Demo01"],
    "OctaFX / Octa": ["OctaFX-Real", "OctaFX-Real2", "OctaFX-Demo", "OctaMarkets-Real", "OctaMarkets-Demo"],
    "JustMarkets": ["JustMarkets-Live", "JustMarkets-Live2", "JustMarkets-Demo"],
    "Headway": ["HeadwayInc-Live", "HeadwayInc-Demo"],
    "Valetax": ["Valetax-Live", "Valetax-Demo"],
    "Taurex": ["Taurex-Live", "Taurex-Demo"],
    "Doo Prime": ["DooPrime-Live", "DooPrime-Live 2", "DooPrime-Demo"],
    "M4Markets": ["M4Markets-Live", "M4Markets-Demo"],
    "Errante": ["Errante-Live", "Errante-Demo"],
    "AvaTrade": ["Ava-Real 1-MT5", "Ava-Demo 1-MT5"],
    "Deriv": ["Deriv-Server", "Deriv-Server-02", "Deriv-Demo"],
    "Monaxa": ["Monaxa-Live", "Monaxa-Demo"],
    "Weltrade": ["Weltrade-Real", "Weltrade-Demo"],
    "Fxview": ["Fxview-Live", "Fxview-Demo"],
    "Global Prime": ["GlobalPrime-Live", "GlobalPrime-Demo"],
    "Blueberry Markets": ["BlueberryMarkets-Live", "BlueberryMarkets-Demo"],
    "Hantec Markets": ["HantecMarketsMU-Live", "HantecMarketsMU-Demo"],
    "Plus500": [],
    # ---- prop firms (check their rules: some don't allow copy trading or shared VPS IPs)
    "FundingPips": ["FundingPips-Prime", "FundingPips-SIM", "FundingPips-SIM1", "FundingPips-Trial",
                    "FundingPips2-SIM"],
    "FTMO": ["FTMO-Demo", "FTMO-Demo2", "FTMO-Server", "FTMO-Server2", "FTMO-Server3", "FTMO-Server4",
             "FTMO-Server5"],
    "The5ers": ["FivePercentOnline-Real", "FivePercentOnline-Demo"],
    "FundedNext": ["FundedNext-Server", "FundedNext-Server 2", "FundedNext-Server 3", "FundedNext-Demo"],
    "E8 Markets": ["E8Markets-Live", "E8Markets-Demo"],
    "Alpha Capital": ["AlphaCapitalGroup-Live", "AlphaCapitalGroup-Demo"],
    "Blue Guardian": ["BlueGuardian-Server", "BlueGuardian-Demo"],
    "Funding Traders": ["FundingTraders-Server", "FundingTraders-Demo"],
    "Maven": ["MavenTrading-Server", "MavenTrading-Demo"],
}
BROKERS = {k: v for k, v in BROKERS.items() if v}

# servers a broker has renamed: what customers type -> what MT5 needs now
RENAMED = {
    "vantageinternational-live": "VantageMarkets-Live",
    "vantageinternational-demo": "VantageMarkets-Demo",
}


def _key(server: str) -> str:
    return re.sub(r"[\s_\-.]+", "", server).lower()


_INDEX = {_key(srv): srv for servers in BROKERS.values() for srv in servers}


def canonical(server: str, learned: list[str] | tuple = ()) -> str:
    """Tidy a server name a customer typed: fix case/spacing for known servers and follow renames.
    Unknown names are returned unchanged (MT5 looks them up itself)."""
    s = re.sub(r"\s+", " ", server.strip())
    if s.lower() in RENAMED:
        return RENAMED[s.lower()]
    k = _key(s)
    for srv in learned:
        if _key(srv) == k:
            return srv
    return _INDEX.get(k, s)


def broker_of(server: str) -> str:
    k = _key(server)
    for name, servers in BROKERS.items():
        if any(_key(x) == k for x in servers):
            return name
    return ""


def all_servers(learned: list[str] | tuple = ()) -> list[str]:
    seen, out = set(), []
    for srv in [*learned, *(x for v in BROKERS.values() for x in v)]:
        if _key(srv) not in seen:
            seen.add(_key(srv))
            out.append(srv)
    return out

"""Fill an empty database with demo customers, trades and signals (for trying the site).

    PF_DB_PATH=data/demo.db python tools/demo_seed.py
Logins: admin / adminpass123, customer: demo / demopass123
"""
import random
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import security, services  # noqa: E402
from server.db import (Account, Admin, Deal, Node, Plan, SessionLocal, Snapshot, User, init_engine,  # noqa: E402
                       main_source, utcnow)
from server.parser import parse_signal  # noqa: E402
from server.plans import DEFAULT_ACCOUNT_SETTINGS  # noqa: E402

random.seed(7)
init_engine()
with SessionLocal() as s:
    s.add(Admin(username="admin", password_hash=security.hash_password("adminpass123")))
    plans = {p.key: p for p in s.query(Plan)}
    node = Node(name="VPS 1 London", token_hash=security.hash_token("demo-node"), max_slots=12,
                last_seen=utcnow(), info={"ram": 61, "cpu": 23, "disk_free_gb": 48.2})
    s.add(node)
    s.flush()
    users = []
    for name, plan, billing, days in [("demo", "extreme", "lifetime", 0), ("jay_fx", "pro", "monthly", 12),
                                      ("goldgirl", "pro", "monthly", -1), ("ahmed", "standard", "free", 0),
                                      ("tradesmith", "extreme", "monthly", 3), ("leon", "standard", "free", 0)]:
        u = User(username=name, password_hash=security.hash_password(name + "pass123" if name != "demo" else "demopass123"),
                 plan_id=plans[plan].id, billing=billing, created_by="admin",
                 paid_until=utcnow() + timedelta(days=days) if billing == "monthly" else None,
                 last_seen=utcnow() - timedelta(hours=random.randint(0, 90)), login_count=random.randint(2, 40),
                 created_at=utcnow() - timedelta(days=random.randint(10, 80)))
        s.add(u)
        users.append(u)
    s.flush()
    main = main_source(s)
    for i, u in enumerate(users):
        acc = Account(user_id=u.id, login=str(5_100_000 + i * 37), server=["PUPrime-Live", "ICMarkets-Live07", "FundingPips-Server"][i % 3],
                      label="Main" if i else "Gold main", password_enc=security.encrypt("x"),
                      settings={**DEFAULT_ACCOUNT_SETTINGS, "layers": 3 if u.plan.key != "standard" else 1,
                                "lot_mode": "risk_percent" if u.plan.key != "standard" else "fixed"},
                      node_id=node.id if i < 5 else None, conn_status="online" if i < 4 else ("error" if i == 4 else "waiting"),
                      conn_error="wrong password or server name" if i == 4 else "",
                      waiting_since=utcnow() if i == 5 else None,
                      balance=10_000 + i * 2500, equity=10_000 + i * 2500 + 42.5, currency="USD", broker="PU Prime Ltd",
                      event_cursor=0, last_report=utcnow())
        s.add(acc)
        s.flush()
        services.ensure_default_sources(s, acc)
        bal = acc.balance
        t = 0
        for d in range(30, -1, -1):
            for k in range(random.randint(0, 5)):
                t += 1
                pnl = round(random.choice([1, 1, 1, -1]) * random.uniform(15, 140), 2)
                s.add(Deal(account_id=acc.id, ticket=t, position_id=t, time=utcnow() - timedelta(days=d, hours=k * 2 + 1),
                           symbol="XAUUSD", side=random.choice(["buy", "sell"]), entry="out", volume=0.05,
                           price=4000 + random.uniform(0, 80), profit=pnl, ours=True))
                bal += pnl
            s.add(Snapshot(account_id=acc.id, time=utcnow() - timedelta(days=d), balance=bal, equity=bal + random.uniform(-60, 60)))
        if i == 0:
            acc.live_state = {"positions": [{"ticket": 1, "side": "buy", "symbol": "XAUUSD", "volume": 0.05, "price": 4036.2,
                                             "sl": 4036.3, "tp": 4057, "profit": 61.5, "signal_id": 3}],
                              "pendings": [], "waiting": [], "notes": [], "symbol": "XAUUSD", "day_pnl": 96.4}
    for txt in ["XAUUSD SELL ZONE : 4071-4078\nSL: 4082\nTP1: 4061\nTP2: 4051\nTP3: 4041",
                "Buy GOLD @ 4037-4030\nTP 4047\nTP 4057\nTP 4067\nSL 4027",
                "GOLD BUY NOW 4012-4006 SL 4002 TP 4022 TP 4032"]:
        sig = services.create_signal(s, main, parse_signal(txt), txt)
    services.add_update(s, sig, "tp_hit", 1)
    services.add_update(s, sig, "breakeven")
    services.assign_accounts(s)
    s.commit()
    from server.db import Renewal
    gg = next(u for u in users if u.username == "goldgirl")
    gg.status = "payment_check"
    s.add(Renewal(user_id=gg.id, due=gg.paid_until, asked_at=utcnow() - timedelta(hours=3)))
    s.commit()
print("demo data ready")

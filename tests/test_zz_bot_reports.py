"""Admin bot reports: account sizes, P&L and open positions."""
import asyncio
from datetime import datetime, timedelta

from server import bot_reports, telegram_service
from server.db import Account, Deal, Plan, SessionLocal, User


def _seed():
    now = datetime.utcnow()
    with SessionLocal() as s:
        plan = s.query(Plan).first()
        u = User(username="reportuser", email="r@example.com", password_hash="x", plan_id=plan.id)
        s.add(u)
        s.flush()
        a = Account(user_id=u.id, login="7001001", server="PUPrime-Demo", password_enc="x", conn_status="online",
                    balance=1004.78, equity=1014.90, currency="GBP", last_report=now,
                    live_state={"positions": [{"side": "buy", "volume": 0.05, "symbol": "XAUUSD.s", "price": 3750.5,
                                               "sl": 3740, "tp": 3770, "profit": 12.5}],
                                "other_positions": [{"side": "sell", "volume": 0.02, "symbol": "XAUUSD.s",
                                                     "price": 3760.8, "sl": 0, "tp": 0, "profit": -2.38}]})
        s.add(a)
        s.flush()
        deals = [  # (days ago, entry, ours, profit)
            (0, "in", True, -0.35), (0, "out", True, 20.0), (0, "out", False, -5.0),
            (3, "out", True, 40.0), (20, "out", False, -10.0), (0, "balance", False, 1000.0)]
        for i, (ago, entry, ours, profit) in enumerate(deals):
            s.add(Deal(account_id=a.id, ticket=900000 + i, time=now - timedelta(days=ago, minutes=1),
                       symbol="XAUUSD.s", side="buy", entry=entry, volume=0.05, price=3750, profit=profit, ours=ours))
        s.commit()
        return a.id


def test_broker_day_uses_server_time():
    from datetime import timezone
    now = datetime(2026, 11, 18, 23, 30)
    a = Account(login="1", server="x", password_enc="x", last_report=now,
                live_state={"server_time": now.replace(tzinfo=timezone.utc).timestamp() + 7200})
    assert bot_reports.broker_offset(a) == timedelta(hours=2)
    # 23:30 UTC is already 01:30 on the 19th at a UTC+2 broker, so its "today" starts at 00:00 server time
    assert bot_reports.period_start("today", now + bot_reports.broker_offset(a)) == datetime(2026, 11, 19)


def test_reports(browser):
    _seed()
    with SessionLocal() as s:
        acc = bot_reports.accounts_report(s)
        assert "reportuser · 7001001 @ PUPrime-Demo" in acc and "Balance £1,004.78" in acc
        assert "Today +£14.65 closed" in acc and "+£10.12 open" in acc and "2 open trades" in acc

        today = bot_reports.pnl_report(s, "today")
        assert "Total +£14.65" in today and "Copied +£19.65" in today and "2 closed" in today
        week = bot_reports.pnl_report(s, "week")
        assert "Total +£54.65" in week
        allt = bot_reports.pnl_report(s, "all")
        assert "Total +£44.65" in allt                        # the £1,000 deposit isn't profit

        pos = bot_reports.positions_report(s)
        assert "BUY 0.05 XAUUSD.s @ 3750.5 SL 3740 TP 3770 → +£12.50" in pos
        assert "SELL 0.02 XAUUSD.s @ 3760.8 → -£2.38 (own trade)" in pos

        one = bot_reports.account_report(s, "7001001")
        assert "Last 7 days: +£54.65" in one and "Last 30 days: +£44.65" in one and "All time: +£44.65" in one and "Open trades (2" in one
        assert bot_reports.account_report(s, "reportuser") == one
        assert "No account matches" in bot_reports.account_report(s, "nobody")

    assert telegram_service.report_for("/help").startswith("📊")
    assert "Last 7 days" in telegram_service.report_for("/pnl week")
    assert telegram_service.report_for("/pnl@PipsFarmerBot month").startswith("💰 Closed P&L: Last 30 days")
    assert telegram_service.report_for("/somethingelse") is None


def test_split_long_reports():
    text = "\n".join(f"line {i} " + "x" * 90 for i in range(200))
    parts = bot_reports.split_message(text)
    assert len(parts) > 1 and all(len(p) <= 4000 for p in parts)
    assert "\n".join(parts) == text


def test_bot_only_answers_admins(monkeypatch):
    sent = []

    async def fake_bot(http, method, token=None, files=None, **params):
        sent.append((method, params))
        return {}
    monkeypatch.setattr(telegram_service, "_bot", fake_bot)
    monkeypatch.setattr(telegram_service, "admin_chat_ids", lambda: [111])
    asyncio.run(telegram_service._handle_update(None, {"message": {"chat": {"id": 999}, "text": "/accounts"}}))
    assert sent == []
    asyncio.run(telegram_service._handle_update(None, {"message": {"chat": {"id": 111}, "text": "/accounts"}}))
    assert sent and sent[-1][0] == "sendMessage" and "inline_keyboard" in sent[-1][1]["reply_markup"]
    sent.clear()
    asyncio.run(telegram_service._handle_update(None, {"callback_query": {
        "id": "c1", "from": {"id": 111}, "message": {"chat": {"id": 111}}, "data": "rp:pnl:week"}}))
    assert any("Last 7 days" in p.get("text", "") for m, p in sent if m == "sendMessage")

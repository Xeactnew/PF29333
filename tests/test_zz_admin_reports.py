"""Admin views: every account's size / P&L / groups, and the signals log organised by channel."""
import io
import zipfile
from datetime import datetime, timedelta

from server import reports, scheduler, telegram_service as ts
from server.db import Account, AccountSource, Deal, Plan, SessionLocal, Signal, Source, User, main_source
from tests.test_web import admin_browser

CH_A, CH_B = -1003001, -1003002


def _seed():
    now = datetime.utcnow()
    with SessionLocal() as s:
        plan = s.query(Plan).filter_by(key="pro").first() or s.query(Plan).first()
        u = User(username="logbook", email="l@example.com", password_hash="x", plan_id=plan.id)
        s.add(u)
        s.flush()
        a = Account(user_id=u.id, login="5559876", server="VantageMarkets-Live", password_enc="x",
                    conn_status="online", balance=2500.0, equity=2510.0, currency="USD", last_report=now,
                    live_state={"positions": [{"side": "sell", "volume": 0.1, "symbol": "XAUUSD", "price": 3790,
                                               "sl": 3795, "tp": 3780, "profit": 10.0}]})
        s.add(a)
        s.flush()
        s.add(AccountSource(account_id=a.id, source_id=main_source(s).id))
        own = Source(kind="channel", owner_user_id=u.id, tg_chat_id=-1009999, title="Logbook's channel")
        s.add(own)
        s.flush()
        s.add(AccountSource(account_id=a.id, source_id=own.id))
        s.commit()
        main_id, acc_id, own_id = main_source(s).id, a.id, own.id
    # two VIP groups and the customer's channel each post a signal
    sa = int(ts.handle_message(main_id, 501, "XAUUSD SELL 3786-3790\nSL 3795\nTP1 3780\nTP2 3770", None,
                               chat_id=CH_A, chat_title="Gold Group A").split(":")[1])
    ts.handle_message(main_id, 502, "Move SL to 3792", 501, chat_id=CH_A, chat_title="Gold Group A")
    sb = int(ts.handle_message(main_id, 501, "XAUUSD BUY 3750-3746\nSL 3740\nTP1 3760", None,
                               chat_id=CH_B, chat_title="Gold Group B").split(":")[1])
    sc = int(ts.handle_message(own_id, 77, "GOLD BUY 3751\nSL 3741\nTP 3771", None, chat_id=-1009999).split(":")[1])
    with SessionLocal() as s:
        for i, (sig, entry, profit) in enumerate([(sa, "in", -0.5), (sa, "out", 42.0), (sc, "in", 0.0)]):
            s.add(Deal(account_id=acc_id, ticket=800000 + i, position_id=1, time=now - timedelta(minutes=5 - i),
                       symbol="XAUUSD", side="sell", entry=entry, volume=0.1, price=3790, profit=profit,
                       ours=entry == "in", signal_id=sig))
        s.commit()
    return acc_id, sa, sb, sc


def test_reports_pages_files_and_bot(browser, tmp_path):
    acc_id, sa, sb, sc = _seed()
    with SessionLocal() as s:
        rows = {r["login"]: r for r in reports.account_rows(s)}
        r = rows["5559876"]
        assert r["balance"] == 2500 and r["open"] == 1 and r["floating"] == 10
        assert r["pnl"]["today"]["total"] == 41.5 and r["pnl"]["today"]["copied"] == 41.5
        assert "PipsFarmer VIP (VIP)" in r["groups"] and "Logbook's channel" in r["groups"]

        log = {x["id"]: x for x in reports.signal_rows(s)}
        assert log[sa]["channel"] == "Gold Group A" and log[sb]["channel"] == "Gold Group B"
        assert log[sc]["channel"] == "Logbook's channel" and log[sc]["type"] == "Customer channel"
        assert log[sa]["orig_sl"] == 3795 and log[sa]["sl"] == 3792 and log[sa]["sl_changes"][0][1] == 3792
        assert log[sa]["accounts"] == 1 and log[sa]["pnl"] == {"USD": 41.5} and log[sb]["accounts"] == 0
        chans = {c["channel"]: c for c in reports.channel_summary(list(log.values()))}
        assert chans["Gold Group A"]["taken"] == 1 and chans["Gold Group B"]["taken"] == 0

        data = reports.signals_xlsx(s)
    z = zipfile.ZipFile(io.BytesIO(data))
    wb = z.read("xl/workbook.xml").decode()
    assert all(n in wb for n in ("Summary", "All signals", "Gold Group A", "Gold Group B", "Logbook"))
    assert "3792" in z.read("xl/worksheets/sheet3.xml").decode() or "3792" in z.read("xl/worksheets/sheet2.xml").decode()

    # files on the VPS, rewritten only when something changed
    assert scheduler.signals_log_tick()
    folder = reports.SIGNALS_DIR
    assert (folder / "PipsFarmer signals.xlsx").exists() and (folder / "Gold Group A.csv").exists()
    assert "Move SL" not in (folder / "Gold Group B.csv").read_text(encoding="utf-8-sig")
    assert not scheduler.signals_log_tick()

    b = admin_browser(browser)
    page = b.get("/admin/accounts").text
    assert "5559876" in page and ("Logbook&#39;s channel" in page or "Logbook's channel" in page)
    assert "$2,500.00" in page
    xl = b.get("/admin/accounts.xlsx")
    assert xl.status_code == 200 and xl.content[:2] == b"PK"
    sig_page = b.get("/admin/signals").text
    assert "Gold Group A" in sig_page and "Taken by 1 account" in sig_page
    assert "SL: 3,795" in sig_page or "SL: 3795" in sig_page
    only_b = b.get("/admin/signals?channel=Gold+Group+B").text
    assert "Not taken by any account" in only_b and "3786" not in only_b
    assert b.get("/admin/signals.xlsx").content[:2] == b"PK"
    assert "Groups: PipsFarmer VIP (VIP), Logbook's channel" in ts.report_for("/account logbook")

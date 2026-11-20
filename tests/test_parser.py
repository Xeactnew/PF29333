from server.parser import parse_signal, parse_update


def sig(text):
    s = parse_signal(text)
    return s and (s.side, s.entry_low, s.entry_high, s.sl, s.tps, s.order_type)


def test_zone_format():
    assert sig("XAUUSD BUY ZONE : 4037-4030\n\nSL: 4027\n\nTP1: 4047 \n\nTP2: 4057 \n\nTP3: 4067") == \
        ("buy", 4030, 4037, 4027, [4047, 4057, 4067], "market")


def test_pipsfarmer_format():
    assert sig("Buy GOLD @ 4037-4030\n\nTP 4047\nTP 4057\nTP 4067\nSL 4027\n\nTHIS REPRESENTS MY PERSONAL EXECUTION") == \
        ("buy", 4030, 4037, 4027, [4047, 4057, 4067], "market")


def test_short_zone_and_emoji():
    assert sig("🔥 GOLD SELL NOW 4055-60 🔥\n🎯 TP1 4050\n🎯 TP2 4045\nTP3 open\n❌ SL 4065") == \
        ("sell", 4055, 4060, 4065, [4050, 4045], "market")


def test_limit_and_slash_tps():
    assert sig("XAU/USD Sell Limit 4102.5\nStop Loss: 4110\nTake Profit: 4095/4090/4080") == \
        ("sell", 4102.5, 4102.5, 4110, [4095, 4090, 4080], "limit")


def test_buy_stop_not_confused_with_sl():
    assert sig("GOLD BUY STOP 4050 SL 4040 TP 4060") == ("buy", 4050, 4050, 4040, [4060], "stop")


def test_tp_pips():
    s = parse_signal("Gold buy now 4000 sl 3990 tp 30 pips")
    assert s.tps == [4003.0]


def test_rejects_non_gold_and_bad_sl():
    assert parse_signal("EURUSD BUY 1.0850 SL 1.0800 TP 1.0900") is None
    assert parse_signal("GOLD BUY 4037 SL 4050 TP 4060") is None   # SL above a buy
    assert parse_signal("good morning team, gold looking bullish today") is None
    assert parse_signal("TP1 HIT +100 pips") is None


def test_comma_numbers_and_tp_index_spacing():
    assert sig("#XAUUSD long 4,037 - 4,031\nsl 4,025\ntp 1: 4,045\ntp 2: 4,060") == \
        ("buy", 4031, 4037, 4025, [4045, 4060], "market")


def test_updates():
    assert parse_update("TP2 HIT, SET BREAKEVEN") == [("tp_hit", 2), ("breakeven", None)]
    assert parse_update("TP1 ✅ +100 pips") == [("tp_hit", 1)]
    assert parse_update("all tp hit 🎯") == [("tp_hit", 99)]
    assert parse_update("Move SL to 4040") == [("sl_move", 4040)]
    assert parse_update("SL hit, on to the next") == [("sl_hit", None)]
    assert parse_update("close now and secure profits") == [("close", None)]
    assert parse_update("close half and move sl to entry") == [("close_partial", 0.5), ("breakeven", None)]
    assert parse_update("cancel the limit order") == [("cancel", None)]
    assert parse_update("nice morning everyone") == []
    assert parse_update("TP3 HIT 300 PIPS HIT\nDO NOT UNDER ANY CIRCUMSTANCES ENTER THE MARKET AGAIN") == [("tp_hit", 3)]


def test_quick_calls_in_more_words():
    from server.parser import parse_signal
    for t in ("Buying gold now", "BUYING NOW", "Gold buys now", "BUY GOLD", "XAUUSD BUY", "Buy here", "Selling here",
              "I'm buying gold now"):
        p = parse_signal(t)
        assert p and p.instant and p.entry_low == 0, t
    for t in ("buys are looking good", "I will buy now if it breaks", "should I buy now?", "Not buying here",
              "dont buy now", "TP1 hit, buys running", "Gold looking bullish", "Buying is risky here?"):
        assert parse_signal(t) is None, t

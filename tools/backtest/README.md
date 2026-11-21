# Signal replay (backtest)

Replays every call from a Telegram group against M1 gold candles and compares ways of copying it.

1. Admin → System → **Export research data** (or it runs once after an update). That writes
   `data/research/messages-<chat>.jsonl` (the groups' last 90 days) and `data/research/prices-acc<id>.csv`
   (M1 candles read from MT5's history, broker time: UTC+3 before 1 Nov 2026, UTC+2 after).
2. Copy those files somewhere and run, for example:

```python
import backtest as B
px = B.Prices("prices-acc2.csv")
calls = B.load_calls("messages--1004407977092.jsonl")
print(B.run(calls, px, B.Rules(entry="market", tp="tp1", be_after_tp1=True)))
```

Results are per 1.00 lot after spread and $7 commission. If SL and TP fall in the same minute, the SL counts first.
Calls use the final text of each message (edits included), so quick calls look like they had their levels at once.

## Findings, 24 Aug – 20 Nov 2026 (split at 20 Oct to check they hold)
- FX_BY_H: TP1 only, BE after TP1, enter in the zone → PF 2.1 / 1.8 in the two halves. Profitable.
- KD scalps ("XAUUSD SELL 📉 ENTRY - …"): PF 4.9 / 3.3. Profitable, few calls.
- KD zones ("💵XAUUSD BUY ZONE"): TP1 only + his breakevens → PF 0.7 / 1.2; sells PF 1.8 both halves.
- KD quick "BUY GOLD!": at market PF ~0.95; waiting for the zone he posts next PF 1.5 / 2.0.
- Pips Farmer VIP (the owner's relay): PF 0.67 (−$31k per lot). H and Savreena: late copies of FX_BY_H.
- Combined FX_BY_H + KD scalps + KD zones: positive every month (+$70k per 1 lot, PF 1.56, max DD $11k).

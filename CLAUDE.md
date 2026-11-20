# PipsFarmer: notes for Claude

A public, paid gold (XAUUSD) copy-trading service. Signals posted in Telegram VIP groups are parsed and copied onto
customers' MT5 accounts, which run as portable MT5 terminals on our own Windows VPS (Contabo, hostname vmi3758251,
node "VPS 1"). Live site: pipsfarmer.com (Cloudflare tunnel → 127.0.0.1:8000, Cloudflare Access on /admin).

## House rules (from the owner, always apply)
- **MT5 is view-only for Claude.** Read logs/history; never place, modify or close trades, never control a terminal.
- Never type, store or ask for passwords, API keys, bot/tunnel tokens or MT5 passwords. The owner enters them on the website.
- Don't change the owner's other trading-bot projects; only use them as templates.
- **Theme colours and icons move together.** Colours live in `server/brand.py` (THEME) *and* the `:root` block of
  `server/assets_src/style.css`. After changing either, run `python tools/build_assets.py` (rebuilds the minified
  CSS/JS and the favicon/app icons). `tests/test_zz_groups_ux.py::test_icons_follow_the_theme` enforces this.
- Fx by KD's "risk free" / "move SL to <price>" = each trade's own entry + 15 pips (setting `group_rules`, default
  in `server/db.py` DEFAULT_SETTINGS, editable in Admin → Telegram).
- Keep the public face generic (no framework names, versions, comments or stack traces in public pages; code served to
  browsers is minified). Don't copy TradeByFocus's look or wording.

## Layout
- `server/`: FastAPI + SQLite website, admin, node API, Telegram (Telethon reader + Bot API admin bot), scheduler.
  - `parser.py`: signal / update parsing (zones, BUY NOW instant calls, SL/TP edits, BE / risk-free, typo guards).
  - `telegram_service.py`: reads VIP chats, per-chat identity (Signal.tg_chat_id + channel), edits and replies.
  - `services.py`: signals, updates → Events for accounts; groups (`signal_groups`, `group_stats`, `group_rule`).
  - `customer.py` / `admin.py`: pages. `templates/`, `assets_src/` (readable CSS/JS) → `static/` (minified, served).
  - `plans.py`: account settings defaults, validation, plan limits (`effective_settings`).
- `node/`: runs on each VPS. `node_agent.py` manages one `worker.py` per account (portable MT5 copied from the
  ready-made `C:\PipsFarmer\mt5-base`), shares the broker list, self-updates. `worker.py` places and manages trades
  (layers, instant entries, breakeven, partials, levels, daily limits, news, one-trade-per-idea, min SL, typo guard,
  per-group settings). `trade_logic.py` holds shared pure helpers.
- `tools/`: `build_assets.py` (+ `build_icons.py`), `watchdog.ps1`, `register_watchdog.ps1`.
- `update_vps.ps1` / `setup_vps.ps1`: install/update on Windows (robocopy; keeps data, accounts, logs, signals, mt5-base).

## Working on it
- Tests: `python -m pytest -q` (all must pass; currently 112). Worker tests use `tests/fake_mt5.py`.
- Before packaging: `python tools/build_assets.py [path-to-esbuild]` (needs esbuild; icons need Chrome + playwright).
- Version lives in three places: `server/system.py`, `node/worker.py`, `node/node_agent.py` (`VERSION = "x.y.z"`).
- Release: zip the folder as `pipsfarmer/…` → the owner sends it to the Telegram admin bot and taps Install
  (or Admin → System → upload). The node self-updates from the website.

## On the VPS (C:\PipsFarmer)
- `data\pipsfarmer.db` (+ -wal), `logs\server.log` (VIP messages logged with result; ignored ones with their text),
  `node\node.log`. Account folders `accounts\<id>` are locked to SYSTEM/Administrators.
- Scheduled tasks: "PipsFarmer Website" (SYSTEM, boot), "PipsFarmer MT5 Node" (logon, hidden), "PipsFarmer Watchdog".
- Test accounts: 1 = PU Prime demo (follows Fx by KD only), 2 = IC Markets demo (ICMarketsSC-Demo).

## Known open items
- The owner's own relay posts "Pips Farmer VIP" (re-posts KD a minute later, sometimes with a no-SL zone 100 too low).
- Admin: untick the "Pips Farmer" highlights group and "H and Savreena 💍" as VIP groups.
- Email (Gmail app password), key backup, legal pages, Channels page tidy-up, trade alerts to customers.

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
- House rules per group (`group_rules`, Admin → Telegram) also set `tp_target` (KD and FX_BY_H: TP1) and
  `wait_for_zone` (KD: a quick "BUY GOLD!" waits for the zone he posts next). Both come from the replay in
  `tools/backtest/` (README has the findings). A group with tp_target / wait_for_zone shows customers
  "✦ Optimised copying" (on by default; per account per group `groups[key].optimised = False` turns it off).
  Optimised TP wins over a customer's own group TP; their own size/layers still apply.
- "Optimise this group" (plan feature `group_optimise`, Extreme only; others see it locked) stores a request
  in setting `optimise_requests` and pings the admin bot. Answer in Admin → Telegram → Optimisation
  requests (not possible + note), or optimise it by replaying it and adding house rules for that chat.
  New plan features reach existing plans through `db.seed` (missing keys only).
- Add-account / login forms: Broker → Server dropdowns (`brokers.choices` + `_macros.server_picker`),
  "isn't listed" opens a typed box; without JavaScript only the typed box shows.
- Keep the public face generic (no framework names, versions, comments or stack traces in public pages; code served to
  browsers is minified). Don't copy TradeByFocus's look or wording.

## Layout
- `server/`: FastAPI + SQLite website, admin, node API, Telegram (Telethon reader + Bot API admin bot), scheduler.
  - `parser.py`: signal / update parsing (zones, BUY NOW instant calls, SL/TP edits, BE / risk-free, typo guards).
  - `telegram_service.py`: reads VIP chats, per-chat identity (Signal.tg_chat_id + channel), edits and replies.
  - `services.py`: signals, updates → Events for accounts; groups (`signal_groups`, `group_stats`, `group_rule`).
  - `customer.py` / `admin.py`: pages. `templates/`, `assets_src/` (readable CSS/JS) → `static/` (minified, served).
  - `plans.py`: account settings defaults, validation, plan limits (`effective_settings`). `max_open_lots` (0 = off)
    caps lots open + pending + waiting per account; the worker trims a layer to fit or skips it, with a note.
  - `customer_alerts.py`: customers' Telegram trade alerts (opened once per signal, partials, closed + today's
    total, stop to entry, signal not taken + why, MT5 / password / Algo Trading problems with a grace period,
    optional daily summary at their local time). Built in `api.py` from new deals and the worker report *after*
    the worker's data is committed. Unique `key` per alert (never sent twice), 40/hour cap, the bot sends
    3 per chat per pass with retries; linked from Plan & login via `/start c<code>`, `/stop` unlinks.
  - `github_update.py`: Admin → System "Update from GitHub" + `/update` in the admin bot. The owner pastes a
    read-only token (stored Fernet-encrypted, shown only as last 4). Downloads main, re-roots to `pipsfarmer/`,
    then the same `stage_update` → backup → `run_update` path as an uploaded zip. certifi, else PowerShell
    fallback with the token in an env var. Installed sha: `data\installed-commit.txt` (written by update_vps.ps1).
  - Channels page never waits on Telegram: `telegram_service.dialogs_for_page` (cache, background refresh,
    2.5 s wait, clear loading/error states).
- `research.py`: read-only research export for backtesting: the VIP groups' last 90 days of messages
  (`data/research/messages-<chat>.jsonl`) and M1 gold candles a worker reads with `copy_rates_range`
  (`data/research/prices-acc<id>.csv`). Runs once after install (setting `research_v1`) and from Admin → System.
- `node/`: runs on each VPS. `node_agent.py` manages one `worker.py` per account (portable MT5 copied from the
  ready-made `C:\PipsFarmer\mt5-base`), shares the broker list, self-updates. `worker.py` places and manages trades
  (layers, instant entries, breakeven, partials, levels, daily limits, news, one-trade-per-idea, min SL, typo guard,
  per-group settings). `trade_logic.py` holds shared pure helpers.
- `tools/`: `build_assets.py` (+ `build_icons.py`), `watchdog.ps1`, `register_watchdog.ps1`.
- `update_vps.ps1` / `setup_vps.ps1`: install/update on Windows (robocopy; keeps data, accounts, logs, signals, mt5-base).

## Working on it
- Tests: `python -m pytest -q` (all must pass; currently 153). Worker tests use `tests/fake_mt5.py`.
- Before packaging: `python tools/build_assets.py [path-to-esbuild]` (needs esbuild; icons need Chrome + playwright).
- Version lives in three places: `server/system.py`, `node/worker.py`, `node/node_agent.py` (`VERSION = "x.y.z"`).
- Release: push to `main`, then the owner uses **Update from GitHub** (Admin → System → Check for updates →
  Install, or `/update` in the admin bot). A zip of the folder as `pipsfarmer/…` still works too: send it to the
  Telegram admin bot and tap Install (or Admin → System → upload). The node self-updates from the website.

## Releasing
- **Ask the owner in chat before pushing to `main`, and only push after they say yes.** That yes is the approval:
  the VPS checks `main` every 5 minutes and installs new commits by itself (needs the owner's read-only token in
  Admin → System). It waits 10 min after a website start and 3 min after a new signal, tries a commit at most
  twice, and the admin bot says when it's installing and when it's done. Setting `gh_update_mode`: auto (default) / off.
- Only push to main when the whole test suite passes.

## On the VPS (C:\PipsFarmer)
- `data\pipsfarmer.db` (+ -wal), `logs\server.log` (VIP messages logged with result; ignored ones with their text),
  `node\node.log`. Account folders `accounts\<id>` are locked to SYSTEM/Administrators.
- Scheduled tasks: "PipsFarmer Website" (SYSTEM, boot), "PipsFarmer MT5 Node" (logon, hidden), "PipsFarmer Watchdog".
- Test accounts: 1 = PU Prime demo (follows Fx by KD only), 2 = IC Markets demo (ICMarketsSC-Demo).

## Known open items
- The owner's own relay posts "Pips Farmer VIP" (re-posts KD a minute later, sometimes with a no-SL zone 100 too low).
- Admin: untick the "Pips Farmer" highlights group and "H and Savreena 💍" as VIP groups.
- Email (Gmail app password), key backup, legal pages.
- The GitHub download, its PowerShell certificate fallback and customer alerts are only tested with fakes so far.

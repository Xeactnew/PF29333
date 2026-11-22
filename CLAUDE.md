# PipsFarmer: notes for Claude

A public, paid gold (XAUUSD) copy-trading service. Signals posted in Telegram VIP groups are parsed and copied onto
customers' MT5 accounts, which run as portable MT5 terminals on our own Windows VPS (Contabo, hostname vmi3758251,
node "VPS 1"). Live site: pipsfarmer.com (Cloudflare tunnel → 127.0.0.1:8000, Cloudflare Access on /admin).

## House rules (from the owner, always apply)
- **MT5 and trades:** never place, modify or close trades without the owner's OK. Using a terminal's windows for
  non-trading work (broker search, logs, history, settings) is fine (owner, 22 Nov 2026).
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
- Admin → Accounts: filter MT5 accounts / all customers / customers without MT5, by plan; plan + billing
  change inline (posts to /admin/users/<id>/plan with `next`). "Remove account" on an MT5 account page
  (`services.remove_account`); "Delete customer for good" on the customer page (type the username;
  `services.delete_user` deletes accounts + deals/snapshots/events, keeps tickets and activity unlinked).
- Options taken from the owner's FundingPips copier (1.3.31), all per account in the settings form:
  close_worst_entry (parser kind `close_worst`), move_entries_on_edit (edited zone → `zone_moved` update),
  per-group `entry_expiry_min` (+ house rule `entry_hours`), max_total_loss/_pct + start_balance,
  limit_buffer_pct (act at 90% etc.), check_worst_case (`Worker.open_risk`), tp1_from_bars
  (`copy_rates_from_pos`), no_hedging (off/close_old/skip_new), tp_target "partials" + tp_partials +
  runner_pct/runner_trail_pips (`manage_partials`), hand_sl/be/trail_pips (`manage_hand_trades`, gold only).
  Plan gating: limits need `daily_limits`, hedging needs `signal_filters`, partials/runner need `tp_control`.
- **Broker finder (1.3.35, replaces 1.3.34's wrong idea):** MT5 only connects to servers in `Config\servers.dat`,
  and a new broker's servers are only added by File > Open an Account's company search (no command line, MQL5
  or Python way; a `/config:` login file does NOT do it, proven on the VPS). Symptom: -10005 timeout +
  `server_never_tried()`. The worker then writes `Config\pf_find_server.txt` (server name) and shows
  "We're looking up the server"; the node (`node/broker_finder.py`, `finder_tick`) runs a spare MT5 with
  **no account** in `C:\PipsFarmer\broker-finder`, types the server / broker / name-before-dash into the search
  (plain window messages via ctypes, works with the screen locked; never logs in, never trades), and keeps it
  if servers.dat grew. `harvest_servers` → shared library → `sync_servers` to other VPSes; the worker's loop
  picks the bigger list up and connects. Retry 10 min per name, gives up for a day after all names (admin
  alert with manual steps, customer alert after 25 min). When idle it pre-fetches every broker on the
  website's list (`broker_names`), 3 min apart. Each run saves `broker-finder\finder_dialog.txt` (window dump)
  for fixing the automation if MT5's window changes. State: `broker-finder.json`. Setting `broker_finder` in
  node config (on by default on Windows).
- **Private VIP groups (1.3.36, owner's choice: hidden = not copied):** a customer only sees AND copies a VIP group
  (by chat id) that is open to everyone (setting `vip_open`, Admin → Telegram "Everyone gets it"), turned on for
  them (`User.vip_grants`, admin customer page "VIP groups they get"), or that they're in on their own Telegram
  (`User.tg_member_chats`, saved whenever their chat list loads + every 6 h). `services.vip_keys` / `vip_ok` /
  `visible_signal`; `events_for_account` drops hidden VIP events (and moves the cursor past leading ones);
  `signal_groups(..., user)`, the Signals page, overview and catch-up filter too. Admin manual VIP signals (no
  chat) go to everyone. Migration `vip_access_v1` gave existing customers with accounts every VIP group.
- **Telegram topics (1.3.36):** forum groups (e.g. Fx by KD VIP: Signals, Scalp trades, Discussion…).
  `telegram_service.topic_of(msg, forum)` → (topic, reply); `topic_route` reads only picked topics. Picked topics:
  VIP `tg_vip_topics` {chat: {topic: title}} (Admin → Telegram), customer `Source.topics`. A picked topic is its own
  group `"<chat>:<topic>"` (`Signal.grp`, name "Group › Topic"); unpicked forums are read whole as one group.
  Every signal stores `Signal.tg_topic_id`; updates only reach signals in the same topic (or pre-1.3.36 signals).
  House rules / optimisation for a chat cover its topics. Chat lists show every channel (no 300 limit) with topics.
- **Telegram login code (1.3.36):** `normalise_phone` + country picker (a UK "07…" used to go to Telegram as
  another country's number); the code page says where Telegram sent it (`_code_where`: usually the "Telegram" chat
  in the app, not SMS), "Send it again" (`resend_user_code`, next way e.g. SMS), "Wrong number?" (`cancel_login`),
  Telegram errors in plain words, and every attempt is logged in server.log (phone masked). Forms: `_macros`
  `tg_phone_form` / `tg_code_form`. A 2-step password or a mistyped code no longer restarts the login.
- **Servers full (1.3.36):** `scheduler.capacity_text` warns at ≥80% used or ≤ the free-slot setting, "full" when
  none free / accounts waiting, with how to add a VPS. A VPS at ≥90% RAM (`services.RAM_FULL`) takes no new accounts.
- Add-account / login forms: Broker → Server dropdowns (`brokers.choices` + `_macros.server_picker`),
  "isn't listed" opens a typed box; without JavaScript only the typed box shows.
- **Plain words for customers** (owner: "easy enough for a 10 year old"): say "signal" not "call", "Ready" not
  "Armed", no "//" labels. Account settings show the main choices per section with the rest under
  "More options", a "What do these words mean?" glossary and "Use recommended settings"
  (`/accounts/<id>/settings/recommended`). Closed trades show the group they came from.
- **Credential archive (`server/vault.py`, table `archived_credentials`, 1.3.32):** a former customer's
  MT5 logins (encrypted) + their channel list are snapshotted on leave (`close_user` → reason "left")
  and on delete (`delete_user` → "deleted", Telegram dropped). Matched back by username **or** email.
  Restored on signup, `reopen_user` and `set_plan` upgrades (capped to the plan's `max_accounts`).
  The Telegram session is archived only while kept: `tg_unlink` and delete call `vault.forget_telegram`
  / archive with keep_telegram=False, because it reaches the whole personal Telegram. Admin delete has a
  "purge" box (`vault.purge`). Terms §2 covers retention. The main reader session is only cleared on a
  genuine Telegram logout (not on disconnect), so it stays logged in across restarts.
- **Speed & motion (1.3.33):** GZipMiddleware (minimum_size=700) compresses HTML/CSS/JS; dropped the unused
  Space Grotesk web font. Motion layer at the end of `style.css`: `@view-transition { navigation: auto }`
  cross-fades page nav, bounded card lists (`.gdeck`, `.mini-trade`, `.plans`, `.how-steps`) stagger-enter,
  panels lift on hover, `details` open smoothly, rows press. No blanket `.main > *` entrance (would hide
  figures); all motion is off under `prefers-reduced-motion`. Static assets keep `?v=` immutable caching.
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
- Tests: `python -m pytest -q` (all must pass; currently 212). Worker tests use `tests/fake_mt5.py`.
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

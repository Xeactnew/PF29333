# PipsFarmer

A website where customers connect their MT5 accounts and PipsFarmer copies gold signals onto them, with an admin side for you and your partner.

```
 PipsFarmer VIP group ─┐                       ┌─ VPS 1 (Windows) ─ MT5 per account ─ worker
 customers' channels ──┼─> website + database ─┼─ VPS 2 (Windows) ─ MT5 per account ─ worker
 manual signals ───────┘   (run_server.py)     └─ ...
             admin bot on Telegram: "Did they pay?"  Paid / Not paid, "VPS nearly full"
```

* **Website** (`server/`): customer portal, admin pages, Telegram reader, admin bot, renewals, news calendar.
* **Node agent** (`node/`): runs on every Windows VPS. It keeps one portable copy of MT5 and one worker running for each customer account the website assigns to that VPS.
* **Worker** (`node/worker.py`): logs into the customer's MT5 and does the trading: layers, risk sizing, smart entry, breakeven at TP1, daily limits, news protection, following the group's updates.

## What customers get

| | Standard (free) | Pro | Extreme |
|---|---|---|---|
| MT5 accounts | 1 | 3 | 10 |
| PipsFarmer VIP signals | ✓ | ✓ | ✓ |
| Extra Telegram channels | – | 5 | unlimited |
| Breakeven at TP1, manual signals | ✓ | ✓ | ✓ |
| Risk % / fixed-money sizing | – | ✓ | ✓ |
| Layers per signal | 1 | 5 | 10 |
| Daily loss / profit limits, news protection, signal filters, TP choice | – | ✓ | ✓ |
| Smart entry | – | – | ✓ |

Prices start at £49/month or £299 lifetime (Pro) and £99/£599 (Extreme). Change prices and what each plan includes in **Admin → Plans**. Changes apply straight away.

---

## Quick setup on a fresh Windows VPS (recommended)

1. Connect to the VPS with Remote Desktop (on a Mac, use **Windows App** from the App Store).
2. On the VPS, open claude.ai in the browser, download `pipsfarmer.zip` and extract it.
3. Double-click **SETUP.bat** and click Yes. It:
   * installs Python, MetaTrader 5 and the Claude desktop app
   * installs PipsFarmer to `C:\PipsFarmer`, creates your secret keys (with a backup copy) and asks for your two admin usernames
   * registers this VPS as your first MT5 server
   * sets the website to start when Windows boots, and the MT5 node to start when you log in (with optional automatic login)
   * opens the admin page at `http://localhost:8000/admin`
4. Open the admin page and follow the **Hands-off checklist** on the Overview. Everything on it (Telegram reader, admin bot, alerts, domain, auto-login) is done on the website, from any device.

**When you're done on the VPS, close the Windows App window. Don't sign out of Windows**, or MT5 stops.

The manual steps below do the same thing by hand.

## Running hands-off

After setup, nobody needs to connect to a VPS:

* **Telegram** (Admin → Telegram): API keys, reader login (phone → code), VIP group, admin bot token, and a one-tap link so each admin gets alerts. No `.env` edits or restarts.
* **Customers** add their MT5 login on the website. The VPS copies MT5 for them, turns Algo Trading on, and logs in. If MT5 doesn't know a broker's server yet, the worker starts the terminal with a login file so MT5 looks the server up itself. Wrong passwords are retried only every 30 minutes so brokers don't lock the account; fixing it on the website retries straight away.
* **Customers' own channels**: they link their Telegram on the Channels page (phone → code).
* **Restarts**: crashed MT5s restart by themselves. With auto-login (Admin → Servers), a rebooted VPS logs itself in and the node starts every MT5 again. Windows updates install but restart the VPS only on Saturday, when gold is closed (Admin → System).
* **Updates**: send the new zip to your admin bot on Telegram and tap Install, or upload it in Admin → System. No permission prompts: it runs as a Windows task. The database is backed up, the site restarts in about a minute, and every other VPS updates itself.
* **Brokers**: the add-account form has a broker picker with 300+ MT5 server names (Vantage, PU Prime, TradeQuo, IC Markets, Exness, XM, prop firms and more). Typed names are tidied (case, spaces, Vantage's 2026 rename), servers that connect are learned for everyone, and unknown servers are looked up by MT5 itself.
* **Paid plans**: customers pick a plan when they sign up or on their Plan page. They're told to pay by crypto or bank transfer through @xaupipsfarmer on Telegram, and you tap **Paid** (Admin → Overview or the bot) to switch them on.
* **Time limits**: give any login a time limit (7/14/30/60/90 days, 6 months, a year, a date, or none). It closes by itself when it runs out.
* **Emails** (Admin → Settings → Email, e.g. Gmail with an app password): payment reminders before a monthly renewal or time limit, plan confirmations, support.
* **Support**: customers ask on their Support page (or /help if they can't log in). Questions are emailed to the support address and answered in Admin → Support; replies are emailed back.
* **Customer MT5 folders**: each account gets its own portable MT5 copy in C:\PipsFarmer\accounts\<id>. The folders are locked to SYSTEM and Administrators, saved logins (accounts.dat) are never copied from the base MT5, MT5 passwords reach the worker through its environment (never the command line), the one-off login file is overwritten and deleted, and a removed account's folder is deleted after an hour. The base MT5 (C:\Program Files\MetaTrader 5) must never be logged in to an account; the bot warns if it is.
* **Desktop stays live**: when you close Windows App, the VPS hands the desktop back to its own screen (tscon), so MT5 keeps a real desktop and remote screenshots keep working.
* **Alerts** on Telegram: VPS offline/full/low disk/high RAM, auto-login off, an MT5 account that can't log in or stops reporting, the Telegram reader disconnected, plus a daily summary.
* **Backups**: every night at 02:00 UTC (kept 14 days) and sent to your Telegram. Send yourself the key backup once from Admin → System.
* **New VPS**: Admin → Servers → Add a VPS gives you one command to paste on the new VPS once (PowerShell as Administrator).
* **Domain**: paste the Cloudflare tunnel install command in Admin → System. Login cookies switch to secure automatically over https.

The manual steps below are only needed if you set things up by hand.

## 1. Set up the website (once)

Use your main Windows VPS. It can be the same machine as your first node.

1. Install **Python 3.11 or newer** from python.org (tick "Add to PATH").
2. Copy this folder to, for example, `C:\PipsFarmer`, then open a terminal there:
   ```
   pip install -r requirements-server.txt
   copy .env.example .env
   python tools\make_keys.py
   ```
   Paste the two keys into `.env`. **Back up `PF_FERNET_KEY`.** If you lose it, every saved MT5 password stops working.
3. **Telegram reader.** Get an API ID and hash at https://my.telegram.org, then run `python tools\telegram_login.py` with the Telegram account that's in your PipsFarmer VIP group. Paste the lines it prints into `.env`, and set `TG_VIP_CHATS` to the group's id from the list.
4. **Admin bot.** Create a bot with @BotFather, put the token in `TG_BOT_TOKEN`, then send `/start` to the bot from your phone and from your partner's phone. It replies with each chat id. Put both in `TG_ADMIN_CHAT_IDS=111,222`.
5. **Admin logins** for you and your partner:
   ```
   python manage.py create-admin yourname
   python manage.py create-admin partnername
   ```
6. Start it with `start_server.bat`. To start it with Windows, add it in Task Scheduler with the trigger "At startup".
7. **Put it on your domain.** Install `cloudflared` and run a tunnel to `http://localhost:8000`, for example `app.yourdomain.com`. Set `PF_BASE_URL` to that address and keep `PF_HTTPS_ONLY=1`. Customers use `https://app.yourdomain.com` and you use `https://app.yourdomain.com/admin`.

## 2. Add a VPS (repeat for each one)

Each MT5 uses roughly 150–250 MB of RAM. A 4 GB VPS runs about 10–12 accounts, and an 8 GB VPS about 25.

1. Install **MetaTrader 5**. This install is the template that gets copied for each customer.
   * Open it, click **Algo Trading** so it turns green, then close it.
   * The node prepares a ready-made copy of it once (`C:\PipsFarmer\mt5-base`): MT5's 450 example files compiled and updates installed, so new accounts connect in seconds.
   * **Brokers:** MT5 has to learn each broker's servers once. Double-click **PipsFarmer - add a broker** on the desktop, then *File → Open an Account*, search the broker, Cancel, close MT5. The broker list is shared with every account and every VPS through the website (the biggest list wins). If a customer's broker isn't known yet, their account says *"MT5 couldn't find the server"*, the bot tells you, and the account connects by itself once the broker is added. Searching inside the normal MT5 install doesn't work: it keeps its list in AppData, not in the folder that gets copied.
2. Install Python, then run `pip install -r node\requirements-node.txt`.
3. In **Admin → Servers → Add a VPS**, enter how many accounts it can run. Copy the key it shows (it's shown once).
4. Copy the `node` folder to the VPS, rename `node.json.example` to `node.json` and fill it in:
   * `server_url`: your site address
   * `node_token`: the key from step 3
   * `mt5_template`: the MT5 install folder
5. Run `start_node.bat`, and add it to Task Scheduler "At startup" as well.

Within about 10 seconds the VPS shows as **Online** in Admin → Servers, and waiting customers start connecting.

### When you need more VPS
* When free slots drop to your alert level (2 by default, change it in Admin → Settings), the bot messages you both: **"VPS nearly full"**.
* When there are no slots left, customers can still add accounts. They see *"All our servers are full right now. You're number N in the queue"*, and the bot tells you how many are waiting. As soon as you add a VPS or raise a slot limit, queued accounts connect by themselves in the order they joined.
* If a VPS stops checking in, the bot tells you which one and how many accounts are on it.

## 3. Day to day

**Someone pays you:** go to *Admin → Customers → Create a login*, pick the plan, and choose Monthly (with the paid-until date) or Lifetime. Send them the username and password it shows. To upgrade an existing customer, open them and change the plan.

**Monthly renewals:** when a monthly customer's paid month ends, the bot asks you both *"Did they pay?"* with **Paid / Not paid** buttons. The same question waits in *Admin → Renewals*. Copying keeps running until one of you answers. If nobody answers, it reminds you every 24 hours.
* **Paid**: their month is extended from the due date.
* **Not paid**: their account is closed, they can't log in, and their MT5 is disconnected (open trades are left alone). You can reopen them at any time.

**Lifetime:** never expires and never asks.

**Free sign-ups:** anyone can create a Standard account on the website. Turn this off in *Admin → Settings* if you only want paid logins.

**Seeing activity:** *Admin → Customers* shows each person's last visit, logins and trades in the last 30 days, and how many of their MT5 accounts are online. Open a customer to see their accounts, results, activity log, renewals and notes. Open an MT5 account to see open trades, settings in force and the worker's recent notes, or to pause it, close all trades, restart MT5 or move it to another VPS.

**Posting signals yourself:** *Admin → Signals* sends a signal to every account following PipsFarmer VIP, and lets you send TP hit, move SL, breakeven, close half, close or cancel for it. Signals posted in the VIP Telegram group are picked up automatically, and so are replies like "TP1 hit", "set breakeven", "move SL to 4040", "close now" or "SL hit".

## Updating PipsFarmer

**From anywhere:** Admin → System → Upload and update.

**On the VPS (if the site is down):**
1. Download the new `pipsfarmer.zip` and extract it, for example in Downloads.
2. Double-click **UPDATE.bat** in the new folder and click Yes.

It takes about a minute. It stops PipsFarmer, backs up the database to `C:\PipsFarmer\backups` (keeping the last 10), copies the new files, installs any new packages and starts everything again. Your settings, customers, VPS key and MT5 folders are kept. Open trades stay open at the broker with their SL and TP, and the workers pick them back up when they restart. Database changes in an update are applied automatically.

## How a signal is traded

1. A gold signal arrives (`XAUUSD BUY ZONE : 4037-4030 / SL 4027 / TP1 4047 ...`, `Buy GOLD @ ...`, `SELL LIMIT ...`, emoji or not).
2. Every account following that source checks its filters: copying on, direction, trading hours, max SL, max open signals, daily limit not hit, no news window, price not already past TP1 or SL.
3. The zone is split into layers (layer 1 at the edge price reaches first). The lot is either fixed, or sized so that every layer hitting SL loses exactly the chosen % or money. Each layer gets a TP: spread across TP1/2/3, or all at one TP.
4. Each layer is placed at market if price is already there. Otherwise it becomes a limit order, or with **smart entry** it waits and enters at market once price comes within the set distance. Unfilled entries are dropped after the expiry time.
5. When price hits TP1 (or the group says so), unfilled entries are cancelled and the rest move to breakeven plus the lock-in pips.
6. Daily limits use the balance at the start of the broker's day. Once a limit is hit, PipsFarmer closes its trades (if that's ticked) and takes nothing new until the next day.

The worker only touches trades it opened itself (magic number 770077). A customer's own manual trades are never changed.

## Before real money

* Run it on **demo accounts** for a few days first. The trading engine is tested against a simulated MT5 (`tests/`), but the first run on real MT5 is where broker-specific surprises show up (symbol names, filling modes, minimum stop distances).
* Signal prices come from the group's broker. A customer's broker can be a few cents different, so entries and TPs can fill slightly differently.
* Only gold signals are copied. Messages about other symbols are ignored.
* Back up `data\pipsfarmer.db` and your `.env` regularly.
* Some prop firms don't allow copy trading or trading from a shared VPS IP. Customers should check their firm's rules.

## Files

```
run_server.py / start_server.bat   website + Telegram + scheduler
manage.py                          admin logins
server/                            website code, templates, signal parser
node/node_agent.py                 VPS agent (start_node.bat)
node/worker.py, trade_logic.py     MT5 trading engine
tools/                             key generator, Telegram login, demo data
tests/                             44 automated tests (python -m pytest tests)
```

Try it locally with demo data:
```
set PF_DB_PATH=data\demo.db
python tools\demo_seed.py
python run_server.py
```
Then log in as `demo` / `demopass123`, and at `/admin` as `admin` / `adminpass123`.

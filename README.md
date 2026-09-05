# xdmovies-bot — CloakBrowser + VPS token solve-server (no VPN)

A Telegram bot that scrapes movie download links from xdmovies.wtf,
bypassing Cloudflare Turnstile via a 2-layer setup:

1. **CloakBrowser** — patched Chromium (C++ source-level fingerprint
   patches, 30.8k⭐ on GitHub). Drop-in Playwright replacement that
   scores as a real browser on FingerprintJS, BrowserScan, and
   Cloudflare Turnstile itself. The bot runs CloakBrowser headed
   (`xvfb-run` provides a virtual display on the VPS).
2. **VPS token solve-server** — when CF still challenges, the bot
   opens a fresh browser, the user clicks the Turnstile checkbox from
   their phone, the cookie is auto-captured, and the original movie
   request retries.
3. **25-minute post-level keep-alive** — once solved, the bot silently
   visits a recent post every 25m with the stored cookie, auto-nudges
   light Turnstile frames, and auto-renews before expiry.

> **Caveat:** the bot connects from the VPS IP directly
> (`132.145.15.58`). Cloudflare's datacenter-ASN fingerprinting will
> reduce `/solve` success rate vs. a residential IP. If you need higher
> success, route CloakBrowser through a residential SOCKS5 proxy by
> passing `proxy={"server": "socks5://host:port", "username": …, …}` to
> `launch_chromium()` at the call sites (or via `cloak_stealth.launch_chromium`
> default). This release does not include that wiring by default.

## Architecture

```
User sends https://top.xdmovies.wtf/movies/<slug> to Telegram
  │
  ├─ Stage A: Movie page (CloakBrowser + cookie injection)
  │     pipeline.py fetches via CloakBrowser with stored cf_clearance
  │     if CF still challenges → raises CFChallengeError
  │
  ├─ Stage B: VPS token solve (port 8080)
  │     solve_server.py opens a CloakBrowser on the VPS
  │     Telegram message: "open http://132.145.15.58:8080/solve/<token>, tap"
  │     user taps Turnstile checkbox → cookie auto-captured → msg deleted
  │     original request retries
  │
  ├─ Stage C: Follow download link (latestnewsonline.sbs redirect)
  │     link_follower.py + link_exec.py drive the gate
  │
  └─ Stage D: 25-minute keep-alive
        keep_alive.py visits a recent post every 25m to refresh the cookie
        silently (no user click needed unless CF challenges the refresh)
```

## Quick start

### Run locally (smoke test)

```bash
git clone <this repo>
cd xdmovies_bot_repo
python -m venv venv
source venv/bin/activate
pip install -r xdmovies/requirements.txt
playwright install chromium   # for CloakBrowser's playwright dependency
# Edit xdmovies/.env: TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_ID
python -m xdmovies --check    # smoke test
python -m xdmovies
```

### Run on VPS (recommended)

See [`DEPLOY.md`](./DEPLOY.md) for the full guide.

```bash
ssh dhruv@132.145.15.58
cd ~/xdmovies_bot_repo
sudo bash deploy.sh          # installs system pkgs, venv, CloakBrowser, systemd
nano xdmovies/.env            # set TELEGRAM_BOT_TOKEN + TELEGRAM_OWNER_ID
sudo bash deploy.sh          # re-run so systemd unit picks up .env
sudo systemctl enable --now xdmovies-bot
journalctl -u xdmovies-bot -f
```

That's it — no VPN, no `/etc/...` ceremony, no credential files. Just
one `.env` with two values.

## File layout

```
xdmovies_bot_repo/
├── xdmovies/                      # Python package — `python -m xdmovies`
│   ├── __main__.py                # entry point + VPN preflight + --check
│   ├── __init__.py
│   ├── bot.py                     # Telegram bot + queue + /status
│   ├── bot_config.py              # env parsing (TELEGRAM_BOT_TOKEN, …)
│   ├── cli.py                     # standalone CLI scraper
│   ├── cloak_stealth.py           # CloakBrowser wrapper (crash-loudly, SOCKS5 by default)
│   ├── browser_stealth.py         # gate-page stealth injection (used by link_exec.py)
│   ├── logger_utils.py            # colored logger
│   ├── cookie_store.py            # cf_clearance store (xdmovies_state/cf_cookies.json)
│   ├── extractor.py               # Card(title, size, language, file_name, url) parser
│   ├── pipeline.py                # Stage A: movie page fetch via CloakBrowser
│   ├── link_follower.py           # follow redirect to latestnewsonline.sbs
│   ├── link_exec.py               # drive the Generate→Continue→Go gate
│   ├── solve_server.py            # Stage B: VPS token server on port 8080
│   ├── keep_alive.py              # Stage D: 25m silent cookie refresh
│   ├── eternal_bypass.py          # operator helper: persistent-profile manual solve
│   └── scrape_all.py              # /scrapeall owner-only catalog scraper
│   └── requirements.txt
├── deploy.sh                      # VPS setup (apt + venv + cloak + systemd)
├── start.sh                       # local wrapper (xvfb-run + .env sanity check)
├── start_bot.cmd                  # Windows launcher (not for VPS)
├── xdmovies_kill_all.sh           # nuke stale processes + free port 8080
├── run.py                         # thin forwarder `python -m xdmovies`
├── AGENTS.md                      # agent instructions (architecture + conventions)
├── README.md                      # this file
├── DEPLOY.md                      # VPS deploy walkthrough
├── DRIFT_REPORT.md                # doc-vs-code drift report
├── CONTEXT_stealth.md             # [ARCHIVED] 2026-08 speed-tuning notes (v2 era)
└── future_plan.md                 # [ARCHIVED] deferred Fortress/SquidProxy plan
```

## Telegram commands

These are the only commands registered in `bot.py` (see
`app.add_handler(CommandHandler(...))`). Anything else referenced in
older docs (`/setcookie`, `/cookies`, `/clearcookie`, `/cookiehelp`,
`/testcookie`, `/setua`) is not registered — the bot now handles
cookies transparently via the VPS token solve-server.

| Command | What |
|---|---|
| `/start` | welcome + how the flow works |
| `/status` | alive + queue + cookies + keep-alive age + **cloak** + **vpn** (always "none") + **desktop** |
| `/queue` | running + queued jobs |
| `/cancel` | cancel your running/queued job(s) |
| `/scrapeall` | owner only — catalog scrape (resumable, choice of all/movies/series) |

`/status` shows the live state:

```
🟢 alive (VPS token solve-server + CloakBrowser, no VPN layer)
queue: 0
current: idle
last solve: 5m ago
errors: 0 (...)
keep-alive: 12m ago (next refresh in 13m) target: top.xdmovies.wtf/movies/...
cloak: ✅ v0.5.8 (linux-x64, C++ patches)
vpn: ❌ none (direct from VPS IP — expect CF to flag this)
desktop: ❌ xdmovies-vnc not active
cookies:
  xdmovies.wtf — 1 cookie, age 12m, TTL 28d
  latestnewsonline.sbs — 1 cookie, age 12m, TTL 28d
```

## Env (xdmovies/.env)

```
TELEGRAM_BOT_TOKEN=...        # required, from @BotFather
TELEGRAM_OWNER_ID=...         # required, your numeric user ID from @userinfobot
TELEGRAM_ADMIN_IDS=...        # optional, comma-separated additional operator IDs
SOLVE_SERVER_HOST=0.0.0.0     # aiohttp bind (don't change unless behind a reverse proxy)
SOLVE_SERVER_PORT=8080        # aiohttp bind (must match firewall + Telegram solve link)
SOLVE_SERVER_PUBLIC_BASE=...  # public URL the bot uses in Telegram messages for the solve link
```

Optional logging overrides (read by `bot_config.py`):
```
XDMOVIES_BOT_LOG=xdmovies_bot.log
XDMOVIES_BOT_ERROR_LOG=xdmovies_bot_errors.log
XDMOVIES_MAX_SESSIONS=8
```

## Cost

$0. CloakBrowser free tier (latest binary with the same stealth patches
as paid, one concurrent session). No VPN, no paid proxies, no Docker,
no API keys.

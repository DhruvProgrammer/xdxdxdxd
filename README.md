# xdmovies-bot — full-auto movie-link resolver (CloakBrowser, no VPN)

A Telegram bot that turns `top.xdmovies.wtf` movie/series pages into final
HubCloud / GDrive download links, bypassing every Cloudflare Turnstile and
ad-gate in the path **without asking the user to do anything** in the common
case. A manual solve-link fallback exists for the rare hard block.

> **Caveat:** the bot connects from the VPS IP directly
> (`132.145.15.58`). Cloudflare's datacenter-ASN fingerprinting is stricter
> than on residential IPs. Everything below is tuned for exactly that
> environment. Routing CloakBrowser through a residential SOCKS5 proxy
> remains possible via `proxy={...}` at the `launch_chromium()` call sites.

## How a link gets resolved (the full pipeline)

```
User sends https://top.xdmovies.wtf/movies/<slug> (or /series/<slug>)
  │
  ├─ 0. EXTRACT (extractor.py, pure function, no browser)
  │     Parses download cards straight from the movie-page HTML:
  │     pack-cards (title + size-badge + hub link) and episode-cards
  │     (title + size + hub link), plus legacy link.xdmovies.wtf anchors.
  │     Ad/nav links never match the URL shapes. 0 cards → clear FAIL.
  │
  ├─ 1. SHORT-CIRCUIT (bot.py per-card loop)
  │     Card URL already on a destination host (hubcloud.one/drive/...
  │     printed directly on newer pages)? Delivered as-is. No browser,
  │     no gate, seconds per page.
  │
  ├─ 2. MOVIE PAGE (pipeline.py, CloakBrowser + stored cf_clearance)
  │     Cookie injected, page loaded. On challenge: ~8s auto-verify wait,
  │     one Turnstile checkbox nudge, ~8s re-check; fresh cookies are saved
  │     IP+UA-bound. Only a surviving challenge raises CFChallengeError.
  │
  ├─ 3. FOLLOW (link_follower.py)
  │     Follows link.xdmovies.wtf/download/<token> to its redirect target
  │     (usually latestnewsonline.sbs/r/<token>), injecting cookies for
  │     BOTH domains. Passive auto-wait for invisible challenges; fresh
  │     cookies saved only when the value actually changed.
  │
  ├─ 4. GATE (link_exec.py — latestnewsonline.sbs, full auto-drive)
  │     Settle → ad-impression wait → Generate → 6s counter → Continue →
  │     step-2 6s timer → Turnstile checkbox → Go to Link → hub page →
  │     inner download-link follow to the token-bearing final URL.
  │     Details in "Gate bypass approaches" below.
  │
  ├─ 5. MANUAL FALLBACK (solve_server.py VPS token link, port 8080)
  │     Only when automation fails. User opens /solve/<token> on their
  │     phone, sees the live VPS browser, taps; cookie/destination is
  │     auto-captured and the message self-deletes to hide the VPS IP.
  │
  └─ 6. KEEP-ALIVE (keep_alive.py, every 25 min)
        Silently revisits a recent post with stored cookies so clearance
        never goes stale. Escalates to an owner token link after repeat
        need_human failures.
```

## Cloudflare bypass approaches (all of them)

1. **CloakBrowser + cookie injection** (`cloak_stealth.py`, `cookie_store.py`).
   Patched Chromium (C++ fingerprint patches) launched headed under
   `xvfb-run`. Stored `cf_clearance` injected per domain from
   `xdmovies_state/cf_cookies.json`. Crash-loudly policy: missing
   CloakBrowser raises at import instead of silently falling back.
2. **Movie-page auto-nudge** (`pipeline.py`). Challenge → 8s wait →
   one iframe checkbox click → 8s re-check → raise only if still blocked.
   Fresh cookies saved with the solving browser's real UA (fixes
   IP+UA-binding mismatches on replay).
3. **Gate auto-drive** (`link_exec.py`). Effect-verified clicks with
   re-click until the page provably advances (counter starts, URL flips
   to `step=2`, Turnstile unhides) — because the first click is routinely
   eaten by a pop-under ad tab, which is closed and the click repeated.
   Covering in-page modals get their grey Close trusted-clicked (blue
   Continue buttons are ads and are never touched). Clicks are
   synthetic-first (the gate JS checks nothing about `isTrusted`, and
   synthetic clicks open no pop-unders) with trusted-mouse fallback.
   Nothing is ever hidden or neutered via DOM — the gate verifies its
   ads, and blocking them stops generation.
4. **Inner-Turnstile handling** (`link_exec.py`, env-gated
   `XDMOVIES_INNER_NUDGE=1`). Nudge-click + fresh-cookie poll; on timeout
   raises so the bot falls back to a solve link. `=0` restores the old
   passive wait.
5. **VPS token solve-server** (`solve_server.py`, `:8080`). Per-block
   isolated browser + 2s JPEG screenshot stream + tap forwarding with
   DPR-correct coordinate mapping and element-aware routing (taps on ad
   Close go to the page, taps on the checkbox go to the Turnstile
   iframe). Captures fresh `cf_clearance` (never the pre-injected value)
   and, for gate URLs, drives the whole Generate→Go flow and captures
   the destination tab itself (`/api/session/complete` response hook
   plus same-tab `/go/` polling). Sessions live 300s; solved sessions
   linger 25s so phone tabs self-close instead of showing expired links.
6. **Keep-alive** (`keep_alive.py`). Silent 25m post refresh with light
   Turnstile nudging; `need_human` escalates to an owner token link with
   backoff. Targets remembered *post* URLs (CF sits on posts, not home).
7. **Destination matcher + ad blocklist** (`link_exec.py`).
   Host-based (never full-URL substring) matching, so `pureddl` in an ad
   query can no longer trip the `ddl` hint. Known ad/CPA hosts are
   excluded outright; a last-line guard refuses ad URLs as results.
8. **Static hub read** (`extractor.extract_hub_details` +
   `link_exec.fetch/resolve_hub_details`). HubCloud drive pages state
   their title, size, and final gamerxyt token URL in plain HTML, so one
   cheap HTTPS GET replaces a full browser navigation wherever a hub
   URL is in hand; the browser flow remains the fallback.

## Telegram commands

| Command | Who | What |
|---|---|---|
| `/start` | allowed | welcome + flow help |
| `/status` | allowed | alive, queue, cookies, keep-alive age, cloak, vpn (always "none"), desktop |
| `/queue` | allowed | running + queued jobs as clickable names with titles, waits, ETAs |
| `/cancel` | allowed | stop your running/queued job(s) |
| `/restart` | **owner only** | cancel everything, tear down browsers/servers, re-exec the process (same PID, lock survives) |
| `/scrapeall` | owner + admins | catalog scrape: All / Movies / Series (+ ❌ Cancel), resumable, checkpoints per kind |
| `/scrapeallchannel` | **owner only** | same as scrapeall but results post to a saved channel; forward a channel message once to set it |

Conventions across all handlers: non-allowed users get silence (except
`/start`, which reveals their ID for allowlisting). Names render as
`t.me/<username>` links, `tg://user?id=` mentions, or masked `...1234`
fallback. Transient notes (waiting pills, progress, enqueue confirmations)
are deleted when their job ends — the chat keeps results + errors only.
Result links post bare (no code boxes) but entity-escaped so `&` in
tokens can't break parsing. HTML parse failures fall back to plain text.

## Queue, dispatcher, and scrape model

- One dispatcher, one running job; per-user busy guard; atomic queue
  snapshot (no drain/reput race); `/cancel` marks `cancel_event`,
  observed at per-card boundaries.
- Catalog scrapes (`scrape_all.py`) checkpoint every item and page to
  `xdmovies_state/scrapeall_{kind}.json` (tmp+fsync+rename). Resume
  continues from `next_page`, skipping completed URLs and retrying failed
  ones; the choice prompt shows saved progress (`page N, M done`) so a
  restart-from-scratch is never a surprise.
- Channel scrapes share the same checkpoints and per-item resolve flow;
  each movie's batch posts to the channel as it completes, with one
  completion line in the user chat. Children inherit routing + silence
  flags; solve taps still happen in user chat.
- Single-instance lock (`.xdmovies_bot.lock`); second instance would
  conflict-poll the token.

## Quick start

### Smoke test

```bash
git clone <this repo>
cd xdmovies_bot_repo
python -m venv venv && source venv/bin/activate
pip install -r xdmovies/requirements.txt
python -m xdmovies --check    # imports 5 modules + loads .env
python -m compileall xdmovies # syntax sweep (no test framework exists)
```

### VPS (recommended) — full guide in [`DEPLOY.md`](./DEPLOY.md)

```bash
ssh user@132.149.35.78
cd ~/xdmovies_bot_repo && git pull
sudo bash deploy.sh          # apt + venv + CloakBrowser + systemd unit
nano xdmovies/.env            # TELEGRAM_BOT_TOKEN + TELEGRAM_OWNER_ID
sudo bash deploy.sh          # re-run so the unit picks up .env
sudo systemctl enable --now xdmovies-bot   # survives close/reboot/crash
journalctl -u xdmovies-bot -f
```

Day-to-day: `sudo systemctl restart xdmovies-bot` after config/code changes;
`bash xdmovies_kill_all.sh` for a hard reset (kills xvfb/chromium, frees
8080); `RESTART=1 bash xdmovies_kill_all.sh` to also restart the service.
`XDMOVIES_ENABLE_VNC=1` in the deploy environment adds a real desktop +
noVNC on `:6080` for first manual solves.

## File layout

```
xdmovies_bot_repo/
├── xdmovies/                      # Python package — `python -m xdmovies`
│   ├── __main__.py                # entry (chdir + --check smoke test)
│   ├── bot.py                     # Telegram bot: queue, all commands, resolve orchestration
│   ├── bot_config.py              # env parsing (token, owner, admins)
│   ├── cli.py                     # standalone CLI resolver (no Telegram)
│   ├── cloak_stealth.py           # CloakBrowser wrapper (crash-loudly, proxy-capable)
│   ├── browser_stealth.py         # gate-page stealth init script
│   ├── logger_utils.py            # colored logger
│   ├── cookie_store.py            # cf_clearance store + manual /setcookie parsing
│   ├── extractor.py               # Card(title, size, language, file_name, url) parser
│   ├── pipeline.py                # movie-page fetch + auto-nudge
│   ├── link_follower.py           # redirect follower, dual-domain cookies
│   ├── link_exec.py               # gate auto-drive + destination matcher
│   ├── solve_server.py            # token solve-server :8080 (manual fallback)
│   ├── keep_alive.py              # 25m silent refresh + escalation
│   ├── eternal_bypass.py          # operator-only single-shot manual helper
│   ├── scrape_all.py              # catalog parsing, checkpoints, channel config
│   └── requirements.txt
├── deploy.sh                      # VPS setup (apt + venv + cloak + systemd + awk/getopt repair)
├── start.sh                       # wrapper (xvfb-run + .env sanity check)
├── start_bot.cmd                  # Windows launcher
├── xdmovies_kill_all.sh           # nuclear reset (RESTART=1 to also restart)
├── run.py                         # thin forwarder to `python -m xdmovies`
├── plan.md                        # auto-first/token-fallback plan (pending decision)
├── AGENTS.md                      # agent source-of-truth (architecture + conventions)
├── DEPLOY.md                      # VPS walkthrough + CloakBrowser verification
├── DRIFT_REPORT.md                # doc-vs-code drift notes
├── CONTEXT_stealth.md             # [ARCHIVED] v2-era tuning notes
└── future_plan.md                 # [ARCHIVED] deferred Fortress/SquidProxy plan
```

## Env (`xdmovies/.env`, created on VPS — template at `xdmovies/.env.template`)

```
TELEGRAM_BOT_TOKEN=...        # required, from @BotFather
TELEGRAM_OWNER_ID=...         # required, numeric ID from @userinfobot
TELEGRAM_ADMIN_IDS=...        # optional, comma-separated extra operator IDs
SOLVE_SERVER_HOST=0.0.0.0     # aiohttp bind
SOLVE_SERVER_PORT=8080        # must match firewall + solve links
SOLVE_SERVER_PUBLIC_BASE=...  # public base used in solve-link messages
```

Optional: `XDMOVIES_BOT_LOG`, `XDMOVIES_BOT_ERROR_LOG`,
`XDMOVIES_MAX_SESSIONS`, `XDMOVIES_INNER_NUDGE=0` (disable gate nudge),
`XDMOVIES_ENABLE_VNC=1` (deploy-time desktop + noVNC).

## Bandwidth & cost

$0 (CloakBrowser free tier, no VPN/proxies/Docker/keys; Oracle free tier
covers traffic ~1000× over). The bot never downloads movie files — only
links; GB-sized downloads happen on your own device. Per gate solve ≈
5–15MB VPS-side (page + ad assets + screenshot stream); keep-alive ≈
~115MB/day worst case. See `xdmovies_state/` for runtime data (gitignored).

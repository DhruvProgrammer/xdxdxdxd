# xdmovies-bot — Telegram movie-link resolver (CloakBrowser, no VPN)

A Telegram bot that turns `top.xdmovies.wtf` movie/series pages into final
download links (HubCloud / GDrive / etc.). You send a link, it bypasses every
Cloudflare Turnstile and ad-gate in the path, and replies with clean,
ready-to-download links. It can also **watch the site for new uploads** and
post them to your channel automatically, **search** for titles, and **scrape
the whole catalog**.

> **Environment caveat:** the bot connects from its VPS IP directly
> (`132.145.15.58`, no VPN/proxy). Cloudflare fingerprints datacenter ASNs
> more strictly than residential IPs, so hard challenges fall back to a
> one-tap manual solve link. Everything is tuned for exactly that setup.

---

## Table of contents

1. [Getting access](#1-getting-access)
2. [Core usage: paste a link](#2-core-usage-paste-a-link)
3. [What happens after you send a link (the pipeline)](#3-what-happens-after-you-send-a-link-the-pipeline)
4. [When Cloudflare blocks (the solve flow)](#4-when-cloudflare-blocks-the-solve-flow)
5. [Commands](#5-commands)
   - [/start](#start)
   - [/search](#search)
   - [/status](#status)
   - [/usage](#usage)
   - [/queue](#queue)
   - [/cancel](#cancel)
   - [/qmode](#qmode)
   - [/dmode](#dmode)
   - [/settings](#settings)
   - [/scrapeall](#scrapeall)
   - [/scrapeallchannel](#scrapeallchannel)
   - [/autodetect](#autodetect)
   - [/stop](#stop)
   - [/restart](#restart)
6. [Features](#6-features)
7. [Use cases with examples](#7-use-cases-with-examples)
8. [Setup & deploy](#8-setup--deploy)
9. [Configuration reference](#9-configuration-reference)
10. [Troubleshooting](#10-troubleshooting)
11. [File layout & runtime state](#11-file-layout--runtime-state)
12. [Bandwidth & cost](#12-bandwidth--cost)

---

## 1. Getting access

The bot is **private**. Only the owner (`TELEGRAM_OWNER_ID`) and extra admins
(`TELEGRAM_ADMIN_IDS`) can use it.

- A stranger who sends anything gets **silence** — except `/start`, which
  replies with *their own Telegram user ID* so the owner can allowlist them:

```
⛔ This bot is private. Your Telegram user ID is 123456789. Add it to
TELEGRAM_OWNER_ID or TELEGRAM_ADMIN_IDS and restart the bot.
```

- Get your ID from [@userinfobot](https://t.me/userinfobot), add it to
  `xdmovies/.env`, restart the bot. Done.

---

## 2. Core usage: paste a link

The main interaction is not a command — just **send a movie/series URL**:

```
https://top.xdmovies.wtf/movies/kalki-2898-ad-2160p-1080p-hindi-tamil-download-123456
```

or a series URL:

```
https://top.xdmovies.wtf/series/mirzapur-1080p-720p-hindi-download-789012
```

You get back a result card like:

```
Kalki 2898 AD:
1) Kalki.2898.AD.2160p.Hindi-Tamil.WEB-DL [8.2 GB] [Hindi, Tamil]
→ https://hubcloud.one/drive/abc123...
2) Kalki.2898.AD.1080p.Hindi-Tamil.WEB-DL [3.1 GB] [Hindi, Tamil]
→ https://hubcloud.one/drive/def456...
[OK] 2/2 resolved
```

- You can paste **multiple links in one message** (up to 10,000 per message);
  each becomes a queued job.
- Every link **queues** — nothing is ever rejected as "busy". One job runs at
  a time (FIFO); position and ETA are shown at enqueue.
- Transient notes ("Starting now", "Queued #3", progress pills) are **deleted**
  when the job ends. Your chat keeps results + errors only.
- Result links post bare (clickable, no code boxes). If a card title contains
  `<`/`&` that breaks HTML parsing, the bot retries the same message as plain
  text so you still get it.

---

## 3. What happens after you send a link (the pipeline)

```
You send https://top.xdmovies.wtf/movies/<slug> (or /series/<slug>)
  │
  ├─ 0. EXTRACT (extractor.py, pure function, no browser)
  │     Parses download cards straight from the movie-page HTML:
  │     pack-cards (title + size + hub link) and episode-cards.
  │     0 cards → clear FAIL (with the page title, so you know why).
  │
  ├─ 1. SHORT-CIRCUIT (per-card loop)
  │     Card URL already on a destination host (hubcloud.one/drive/... on
  │     newer pages)? Delivered as-is. No browser, no gate, seconds.
  │
  ├─ 2. MOVIE PAGE (pipeline.py, CloakBrowser + stored cf_clearance)
  │     Cookie injected, page loaded. On challenge: 8s auto-verify wait,
  │     then up to 3 Turnstile checkbox nudges with a 10s verify gap
  │     after each (only real, dispatched clicks count; detached frames
  │     are skipped). Fresh cookies are saved IP+UA-bound. Only a
  │     surviving challenge raises CFChallengeError.
  │
  ├─ 3. FOLLOW (link_follower.py)
  │     Follows link.xdmovies.wtf/download/<token> to its redirect target
  │     (usually latestnewsonline.sbs/r/<token>), injecting cookies for
  │     BOTH domains. Passive auto-wait for invisible challenges.
  │
  ├─ 4. GATE (link_exec.py — latestnewsonline.sbs, full auto-drive)
  │     Settle → ad-impression wait → Generate → counter → Continue →
  │     step-2 timer → Turnstile checkbox → Go to Link → hub page →
  │     inner link follow to the final URL. Clicks are effect-verified
  │     (re-clicked until the page provably advances) because the first
  │     click is routinely eaten by a pop-under ad tab.
  │
  ├─ 5. MANUAL FALLBACK (solve_server.py, port 8080) — only if needed
  │     You get a /solve/<token> link (in YOUR DM, never a channel),
  │     open it on your phone, see the live VPS browser, tap the
  │     checkbox; the cookie/destination is auto-captured and the
  │     message self-deletes to hide the VPS IP.
  │
  └─ 6. KEEP-ALIVE (keep_alive.py, every 25 min)
        Silently revisits a recent post with stored cookies so clearance
        never goes stale. Escalates to an owner solve link after repeat
        failures. CF sits on posts, not the homepage — so it hits deep
        links, never home.
```

---

## 4. When Cloudflare blocks (the solve flow)

Most of the time you never see this — the auto-nudge handles it. When a hard
challenge survives automation:

1. The bot sends **you (DM only)** a message like:

```
🔒 Cloudflare blocked -- xdmovies.wtf
Link: https://top.xdmovies.wtf/movies/...
👉 Tap to solve (auto-captures, no paste):
http://132.145.15.58:8080/solve/ce334dd24d22
Open it, tap checkbox on screenshot if you see it. I'll auto-capture
and delete this message to hide VPS IP.
```

2. Open the link → you see a live screenshot of the VPS browser (refreshes
   every 5s while the tab is visible) → **tap the checkbox** (taps are forwarded
   with correct coordinates; taps on ad popups go to the page, taps on the
   checkbox go to the Turnstile iframe). Hidden tabs stop the screenshot stream,
   and video/audio/font assets are blocked to reduce VPS bandwidth.
3. The bot auto-captures the fresh `cf_clearance`, **deletes the solve
   message**, and resumes your job where it stopped.

Timeouts: movie-page solves wait up to **120s**; gate solves up to **240s**
(latestnews) or **90s** (other hosts). The solve browser itself remains
available for **one hour of viewer activity**, then closes automatically.
On bot wait timeout you get `⏰ Solve timed out…` — just resend the link for a
fresh solve link.

> Solve links always go to the requester's DM — even for channel jobs. The
> public channel only ever sees clean result batches. The same applies to
> wait pills, timeouts and error tracebacks.

---

## 5. Commands

Who-can-use legend: **all** = owner + admins, **owner** = owner only.

### /start

| | |
|---|---|
| Who | all (also the one command that answers strangers — with their ID) |
| Usage | `/start` |
| Does | Welcome card + command help |

### /search

| | |
|---|---|
| Who | all |
| Usage | `/search lucky baskhar` |
| Does | Searches the site, shows 5 results per page. Tap a result → pick what to scrape → optionally route to your channel → job enqueued. |

Step-by-step example:

```
/search mirzapur
→ 🔎 results page 1/3:
   🎬 Mirzapur (2018) ...
   [✅ Accept all] [❌ Close] [➡️]
```

- Tap one result → it enqueues that title immediately (same as pasting its URL).
- Tap **✅ Accept all** → choose content first, then destination:
  - `✅ All (EP+Pack)` — episodes + season packs
  - `🎞️ Episodes only` — single-episode cards
  - `🗜️ Packs/Zips only` — season/full `.zip` packs
  - then `✅ Yes, to channel` (posts to your preset channel) or
    `❌ No, here in DM`.
- Search results are cached ~20 min; the channel-confirm step times out after
  ~15 min (then it posts to DM by default — nothing is lost).

**Batch search via `.txt`:** send a `.txt` file, one title per line
(up to 10,000 lines, env `XDMOVIES_TXT_MAX_LINES`), and every title is
searched at once:

```
mirzapur
kalki 2898 ad
panchayat season 4
```

### /status

| | |
|---|---|
| Who | all |
| Usage | `/status` |
| Does | One-screen health report |

Example output:

```
🟢 alive (VPS token solve-server + CloakBrowser, no VPN layer)
queue: 3
current: running (user ...4974)
last solve: 4m ago
errors: 0 (see xdmovies_bot_errors.log)
keep-alive: 2m ago (next refresh in 23m) ...
cloak: ✅ v0.5.8 (linux-x64, C++ patches)
vpn: ❌ none (direct from VPS IP -- expect CF to flag this)
desktop: ❌ xdmovies-vnc not active ...
modes (you): qmode=OFF dmode=OFF
channel: preset to 4khdhub links -- results mirror there; pickers still ask
autodetect: ON feed=all seen=128 last_check=4m ago
cookies:
xdmovies.wtf: cf_clearance ✅ (12m old) ...
```

### /usage

| | |
|---|---|
| Who | all |
| Usage | `/usage` |
| Does | Shows current-month VPS bandwidth from vnstat and whole-VPS traffic since the last bot restart. |

```
📊 Bandwidth Usage

🌐 Overall — current month (eth0)
⬇️ Received: 4.82 GiB
⬆️ Sent: 612.40 MiB
🔁 Total: 5.41 GiB

🔄 Since last bot restart
⬇️ Received: 1.24 GiB
⬆️ Sent: 88.20 MiB
🔁 Total: 1.32 GiB
⏱️ Uptime: 6h 14m
```

The public interface is detected from the default route. Set
`XDMOVIES_USAGE_INTERFACE=eth0` in `.env` to override it. `vnstat` is
installed by `deploy.sh`; if it is unavailable, the since-restart counters
still work.

### /queue

| | |
|---|---|
| Who | all |
| Usage | `/queue` |
| Does | Shows the running job (with card progress, e.g. `📦 cards 137/240 delivered`), urgent lane, and the waiting list with per-job wait times. Empty → `✅ All clear`. |

Example:

```
📋 Queue Status
▶️ Now running
🎬 khuddar 1080p 720p hindi
👤 ...4974 • ⏱ 3m 18s in
📦 cards 1/2 delivered

⏳ Up next (3)
1. 🎬 kalki 2898 ad 2160p 1080p hindi tamil
     👤 ...4974 • waiting 5m 45s
2. 🎬 hushar pittalu 1080p 720p telugu
     ...
```

### /cancel

| | |
|---|---|
| Who | all (acts on **your own** jobs only) |
| Usage | `/cancel` · `/cancel 2` · `/cancel 2 4 8` (commas work too: `/cancel 2,4,8`) |
| Does | Stops jobs. Bare = running + **all** your queued jobs. With numbers = only those queued positions. |

Examples:

```
/cancel            → 🛑 cancelled your running job and removed 2 queued job(s).
/cancel 2          → 🛑 cancelled #2 🎬 kalki 2898 ad...
                     📋 your remaining queue (2):
                     1. 🎬 hushar pittalu...
                     2. 🎬 phata poster...
/cancel 2 4 8      → 🛑 cancelled #2 ..., #4 ..., #8 ...   (or /cancel 2,4,8)
```

- Positions are 1-based over **your own queued jobs** (your urgent jobs
  first, then your normal ones — dispatcher pick order).
- Bad numbers are reported (`⚠️ ignored: 9, abc (you have 3 queued)`),
  never fatal.
- The running job is never indexed — use bare `/cancel` to stop it (it stops
  at the next per-card boundary, i.e. after the current link finishes).

### /qmode

| | |
|---|---|
| Who | all (per-user, survives restart) |
| Usage | `/qmode` → tap `✅ Yes` / `❌ No` |
| Does | When **ON** and you send a `/series/` link with 2+ quality groups, the bot asks which qualities to resolve before starting (toggle grid + Select All + Done, 2-min timeout). **OFF** (default) = resolve everything, exactly as before. |

Use case: a series page has 1080p + 720p + 480p sections but you only want
1080p — turn qmode ON, tap just that group, Done.

### /dmode

| | |
|---|---|
| Who | all (per-user, survives restart) |
| Usage | `/dmode` → tap `✅ Yes` / `❌ No` |
| Does | When **ON**, asks **section** picker first: Season packs vs Episodes (per season). Runs **before** qmode; chain is dmode → qmode. |

Use case: a series has "Season 1 Pack (12 GB)" plus 20 single episodes — pick
only `Season 1 Pack` and skip 20 slow episode resolves.

### /settings

| | |
|---|---|
| Who | all (per-user, instant-apply, survives restart) |
| Usage | `/settings` → tap toggles (`Size ⭐ Rating 🎭 Genre 📅 First Air 🔊 Audios 💽 Sources 🆔 TMDB`) → `❌ Close` |
| Does | Chooses which detail lines appear under every result card. Default = Size + Audios (today's output). |

Example card with everything on:

```
1) Mirzapur.S01.1080p.Hindi.WEB-DL [4.2 GB] [Hindi] ⭐ 8.5 🎭 Crime, Thriller
   📅 2018 🔊 Hindi, English 💽 WEB-DL 🆔 12345
→ https://hubcloud.one/drive/...
```

### /scrapeall

| | |
|---|---|
| Who | all (owner + admins) |
| Usage | `/scrapeall` → `📚 All` / `🎬 Movies` / `🎞️ Series` → Resume or Start |
| Does | Catalog scrape: walks every catalog page and resolves every movie/series, posting results to your chat. **Resumable** — checkpoints every item and page to disk, so a restart/resume continues from `next_page`, retrying failed ones and skipping completed ones. |

Example:

```
/scrapeall → [🎬 Movies] → "Movies has unfinished scrape history.
Progress: page 14, 210 done, 3 failed. Resume or Start from beginning?"
→ [▶ Resume] → ⏳ Movies page 14: item 3/24 ... total completed: 212
→ [OK] Movies scraping complete. pages: 14 completed: 236 failed: 3
```

### /scrapeallchannel

| | |
|---|---|
| Who | **owner only** |
| Usage | `/scrapeallchannel` → follow the prompt → **forward any message from your channel** to the bot (within 10 min) → pick All/Movies/Series |
| Does | Same as `/scrapeall`, but each movie's batch posts **to your channel** as it completes, with one completion line in your chat. The bot verifies it is admin-capable in the channel. |

Setup example:

```
/scrapeallchannel
→ "Forward any message from the channel you want results in (10 min window)."
→ (you forward a channel post)
→ "Channel set: 4khdhub links. What do you want to scrape?" → [🎬 Movies]
→ ✅ Movies scrape done -- posted to the channel.
```

Every paste-link result is also mirrored to this preset channel automatically
(clean links only, no timing/summary footer — that stays in your DM).

### /autodetect

| | |
|---|---|
| Who | **owner only** |
| Usage | `/autodetect` → pick feed → Turn ON/OFF |
| Does | Watches xdmovies for **new uploads every 30 minutes** and auto-scrapes them to your channel. Default OFF. |

Full flow example:

```
/autodetect
→ "Pick the feed to watch:"  [🎬 Movies] [🎞️ Series] [🌐 All (homepage)]
→ tap 🎞️ Series
→ "Auto detect: OFF / Feed: Series / Interval: 30m / Channel: 4khdhub links"
  [🟢 Turn ON]
→ tap Turn ON
→ "Series detected -- what should auto-detect resolve?"
  [📋 All (EP+Pack)] [🎞️ Episodes only] [🗜️ Packs/Zips only]
→ tap 🎞️ Episodes only
→ "Auto detect: ON / Feed: Series / Interval: 30m / Content: Episodes only"
  [🔴 Turn OFF] [🎞️ Content filter] [⬅️ Change feed] [❌ Close]
```

Behavior notes:

- **Single feed at a time** — picking another feed replaces the previous one.
- The content question (All / Episodes / Packs) appears for the **Series**
  and **All (homepage)** feeds, reusing the exact search accept-all filter.
  It narrows **series posts only** — movie posts always resolve fully (movie
  cards pass through every filter). The Movies feed asks nothing and
  resolves everything.
- Requires the `/scrapeallchannel` preset — without it, ticks are skipped and
  the toggle screen says `Channel: none (run /scrapeallchannel first)`.
- **First tick after enabling only takes a snapshot** (baseline) — nothing
  already on the site gets re-scraped. Only genuinely new posts enqueue.
- Max **5 new posts per tick** (oldest first); a burst spreads over ticks.
- New posts become silent channel jobs (no pickers, no progress chatter);
  solve taps, if ever needed, still happen in **your DM**, never the channel.
- Survives restarts (state in `xdmovies_state/autodetect.json`).
- 30 min (not 5) is deliberate: each tick costs one browser page load; 30 min
  keeps VPS bandwidth tiny while catching same-day uploads.
- `/status` shows `autodetect: ON feed=series content=episodes seen=42 …`.

### /stop

| | |
|---|---|
| Who | **owner only** |
| Usage | `/stop` |
| Does | Cancels all bot work, closes browsers/solve sessions, and stops keep-alive, autodetect, the dispatcher and solve server. The lightweight Telegram listener remains so only the owner's `/start` can resume it. |

Shutdown mode is saved in `xdmovies_state/shutdown.json`, so a bot process
restart stays stopped until the owner sends `/start`. The VPS, SSH and systemd
service are not shut down. While stopped, all other commands, links, documents
and buttons are silently ignored, and Telegram shows only `/start`.

### /restart

| | |
|---|---|
| Who | **owner only** |
| Usage | `/restart` |
| Does | Cancels everything, tears down browsers/servers, and re-execs the process (same PID, lock survives). Use after config/code changes instead of SSH. You'll get a success note when it's back. |

---

## 6. Features

- **Owner-only shutdown mode.** `/stop` turns off every browser, job, solve server,
  keep-alive and autodetect task while leaving a minimal Telegram listener;
  only the owner's `/start` resumes it, even after a process restart.
- **Queue, never busy.** One dispatcher, one running job; everything else
  FIFO-queues with position + ETA at enqueue (`deque` + lock + condition, no
  drain/re-put races). An **urgent lane** lets a pasted link jump ahead of a
  running channel scrape at the next movie boundary.
- **Indexed cancel.** `/cancel 2` / `/cancel 2 4 8` (commas also work) removes exactly those
  queued positions (yours only); the reply re-numbers the remainder.
- **Silent access control.** Non-allowed users get a black hole (only `/start`
  reveals their ID for allowlisting).
- **Layered CF bypass.** CloakBrowser (C++ fingerprint patches, crash-loudly —
  no silent fallback to vanilla Playwright) + stored `cf_clearance` +
  auto-nudge (max 3 dispatched clicks, 10s verify gaps, detached-frame guard)
  + full gate auto-drive + VPS token solve-server + 25-min keep-alive.
- **DM-only operations.** Solve links (which expose the VPS IP), wait pills,
  timeouts, FAILs and tracebacks always go to the requester's DM — the public
  channel only ever sees clean result batches. Solve messages self-delete.
- **Per-user preferences that survive restarts.** `/qmode`, `/dmode`,
  `/settings` in `xdmovies_state/*.json` (atomic tmp+fsync+rename writes).
- **Resumable catalog scrapes.** Per-item + per-page checkpoints; resume never
  re-does completed URLs.
- **Auto-detect.** 30-min new-post watch → full resolve → channel, with
  baseline-on-enable and flood cap.
- **Single-instance lock.** `.xdmovies_bot.lock` — a second
  `python -m xdmovies` on the same box exits instead of splitting Telegram
  updates with zero errors.
- **HTML with plain-text fallback.** Every send retries unformatted on
  `BadRequest`, so entity-breaking titles never eat your results.
- **Debug dumps.** When a movie page yields no cards, the HTML is saved to
  `xdmovies/xdmovies_dumps/latest_movie_page.html` for inspection.
- **No test framework tax.** Verification is `python -m xdmovies --check`
  (imports 6 modules + loads `.env`) plus `python -m compileall xdmovies`.

---

## 7. Use cases with examples

### A. "I want tonight's movie, fast"

```
(you) https://top.xdmovies.wtf/movies/dune-part-two-2160p-1080p-hindi-english-download-999999
(bot) 🔄 Bypassing Cloudflare on the movie page...  →  (seconds later)
      Dune Part Two:
      1) Dune.Part.Two.2160p... [12.4 GB] ...
      → https://hubcloud.one/drive/...
      [OK] 3/3 resolved
```

Short-circuit path: cards already on hub hosts resolve with no browser at all.

### B. "Only 1080p episodes of this 40-episode series, post to my channel"

```
/dmode → Yes … /qmode → Yes … /search panchayat season 4
→ tap the series → content: Episodes only → ✅ Yes, to channel
→ pick 1080p group → Done
→ batches land in your channel; your DM gets the summary.
```

### C. "Seed my channel with the whole Movies catalog, unattended"

```
/scrapeallchannel → forward a channel post → 🎬 Movies
→ go to sleep. Each movie posts as it completes; checkpoint survives
  VPS reboots — /scrapeall → Resume continues at the exact page.
```

### D. "Never miss a new upload"

```
/autodetect → 🌐 All (homepage) → Turn ON
→ every 30 min the bot diffs the homepage; a new post enqueues itself,
  resolves fully, and lands in your channel. /status shows
  autodetect: ON ... seen=128.
→ /autodetect → Turn OFF when the season ends.
```

### E. "My queue is full of autodetect items but I need one movie NOW"

```
(you) https://top.xdmovies.wtf/movies/urgent-film-...
(bot) ⚡ Urgent link — running now (channel scrape paused).
      → resolves immediately → channel scrape resumes after.
/cancel 2 4   → drop the two autodetect items you don't care about.
```

### F. "Cloudflare is being stubborn"

```
(bot) 🔒 Cloudflare blocked -- latestnewsonline.sbs
      👉 Tap to solve: http://132.145.15.58:8080/solve/09bc15be2026
(you) open on phone → tap checkbox on the live screenshot
(bot) (message deleted) → ⏳ resuming → final hub link delivered
```

---

## 8. Setup & deploy

### Smoke test (any machine)

```bash
git clone <this repo>
cd xdmovies_bot_repo
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r xdmovies/requirements.txt
python -m xdmovies --check      # imports 6 modules + loads .env
python -m compileall xdmovies   # syntax sweep (no test framework exists)
python -m xdmovies --test <movie-url>  # one-shot resolve, no Telegram
```

### VPS (recommended) — full guide in [`DEPLOY.md`](./DEPLOY.md)

```bash
ssh user@132.145.15.58
cd ~/xdmovies_bot_repo && git pull
sudo bash deploy.sh          # apt + venv + CloakBrowser + systemd unit
nano xdmovies/.env            # TELEGRAM_BOT_TOKEN + TELEGRAM_OWNER_ID
sudo bash deploy.sh          # re-run so the unit picks up .env
sudo systemctl enable --now xdmovies-bot   # survives close/reboot/crash
journalctl -u xdmovies-bot -f
```

Day-to-day:

```bash
sudo systemctl restart xdmovies-bot               # after config/code changes
bash xdmovies_kill_all.sh                         # hard reset (kills xvfb/chromium, frees 8080)
RESTART=1 bash xdmovies_kill_all.sh               # hard reset + restart service
XDMOVIES_ENABLE_VNC=1 sudo -E bash deploy.sh      # real desktop + noVNC on :6080
```

Notes:

- Chromium always launches **headed** (`headless=False` is hardcoded); the
  VPS provides a virtual display via `xvfb-run -a`.
- `deploy.sh` repairs broken `/usr/bin/awk` and `/usr/bin/getopt` symlinks
  first (minimal Ubuntu images ship dangling alternatives symlinks).
- `CLOAKBROWSER_CACHE_DIR=/opt/cloakbrowser` keeps the ~200MB Chromium binary
  across restarts (first install downloads it once).
- The bot **crashes loudly** if `cloakbrowser` is missing (no silent
  Playwright fallback) — you'll see the restart loop in `journalctl`; fix
  with `pip install 'cloakbrowser>=0.5.0'`.

---

## 9. Configuration reference

One env file: `xdmovies/.env` (created on the VPS at deploy; template at
`xdmovies/.env.template`). Secrets are `chmod 600`; the bot token is redacted
from every log line.

| Variable | Required | What |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | From [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_OWNER_ID` | ✅ | Numeric ID from [@userinfobot](https://t.me/userinfobot) |
| `TELEGRAM_ADMIN_IDS` | – | Comma/space-separated extra operator IDs (legacy `TELEGRAM_ADMIN_ID` also works) |
| `SOLVE_SERVER_HOST` | – | aiohttp bind, default `0.0.0.0` |
| `SOLVE_SERVER_PORT` | – | default `8080` (must match firewall + solve links) |
| `SOLVE_SERVER_PUBLIC_BASE` | – | public base in solve-link messages, e.g. `http://132.145.15.58:8080` |
| `XDMOVIES_BOT_LOG` | – | normal log path, default `xdmovies_bot.log` |
| `XDMOVIES_BOT_ERROR_LOG` | – | errors-only log, default `xdmovies_bot_errors.log` |
| `XDMOVIES_MAX_SESSIONS` | – | browser session cap, default `8` |
| `XDMOVIES_INNER_NUDGE` | – | `=0` disables the gate-page Turnstile nudge (default on) |
| `XDMOVIES_TXT_MAX_LINES` | – | `.txt` batch cap, default `10000` |
| `XDMOVIES_ENABLE_VNC` | – | deploy-time only: adds XFCE + x11vnc + noVNC (`:6080`) |
| `XDMOVIES_USAGE_INTERFACE` | – | interface used by `/usage`; blank auto-detects the default route |
| `XDMOVIES_SOLVE_IDLE_TIMEOUT` | – | solve browser idle lifetime in seconds, default `3600` |
| `XDMOVIES_SOLVE_VIEWER_WINDOW` | – | seconds of viewer activity required before screenshots resume, default `90` |
| `XDMOVIES_SOLVE_BLOCK_GATE_IMAGES` | – | `=1` also blocks images on latestnews gate pages; default `0` to preserve ad verification |

Timings baked into code: keep-alive refresh **25m** (check every 30s),
autodetect poll **30m** (max 5 posts/tick), solve waits **120s** movie /
**240s** gate / **90s** follow, nudge **max 3 clicks + 10s gaps**, solve
sessions close after **1h** without viewer activity, search cache **20m**, channel-setup window **10m**,
picker timeouts **2m**.

---

## 10. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| systemd restart loop on start | `cloakbrowser` not installed | `pip install 'cloakbrowser>=0.5.0'` in the venv |
| `/status` shows `cloak: ❌ not importable` | venv mismatch | reinstall, then `sudo systemctl restart xdmovies-bot` |
| Solve link doesn't open | firewall on 8080 | `sudo ufw allow 8080/tcp` |
| `⏰ Solve timed out` often | datacenter ASN flagged by CF (expected, no VPN layer) | solve promptly (<120s movie / <240s gate); auto-nudge (3 clicks + 10s gaps) handles light challenges alone |
| `start.sh` exits on `__REPLACE_ME__` | `.env` not filled | `nano xdmovies/.env`, replace placeholders |
| Port 8080 already in use | stale process | `bash xdmovies_kill_all.sh` then retry |
| `address already in use` / two bots answering | second instance | only one `python -m xdmovies` per box (lockfile); `bash xdmovies_kill_all.sh` |
| Autodetect ticks but posts nothing | no channel preset | run `/scrapeallchannel` once, then `/autodetect` → Turn ON again |
| Autodetect floods after enabling | — | by design it baselines on first tick; only *new* posts enqueue (max 5/tick) |
| Channel shows "Solve timed out" / solve links | old behavior (fixed) | pull latest — operational chatter now routes to the requester's DM only |
| SSL error downloading CloakBrowser | stale CA bundle | `sudo apt update && sudo apt install -y --reinstall ca-certificates` |

---

## 11. File layout & runtime state

```
xdmovies_bot_repo/
├── xdmovies/                      # Python package — `python -m xdmovies`
│   ├── __main__.py                # entry (chdir + --check smoke test + --test one-shot)
│   ├── bot.py                     # Telegram bot: queue, all commands, resolve orchestration
│   ├── bot_config.py              # env parsing (token, owner, admins)
│   ├── autodetect.py              # 30m new-post poll loop → channel jobs
│   ├── autodetect_store.py        # autodetect ON/OFF + feed + content + seen URLs
│   ├── cli.py                     # standalone CLI resolver (no Telegram)
│   ├── cloak_stealth.py           # CloakBrowser wrapper (crash-loudly, proxy-capable)
│   ├── browser_stealth.py         # gate-page stealth init script
│   ├── logger_utils.py            # colored logger
│   ├── cookie_store.py            # cf_clearance store + CFChallengeError
│   ├── extractor.py               # Card(title, size, language, file_name, url, kind) parser
│   ├── pipeline.py                # movie-page fetch + auto-nudge
│   ├── link_follower.py           # redirect follower, dual-domain cookies
│   ├── link_exec.py               # gate auto-drive + destination matcher
│   ├── solve_server.py            # token solve-server :8080 (manual fallback)
│   ├── keep_alive.py              # 25m silent refresh + escalation
│   ├── eternal_bypass.py          # operator-only single-shot manual helper
│   ├── scrape_all.py              # catalog parsing, checkpoints, channel config
│   ├── search.py                  # site search API client
│   ├── usage.py                   # /usage: vnstat month + restart bandwidth baseline
│   ├── shutdown_store.py          # persistent owner /stop + /start state
│   ├── channel_post.py            # channel-title → search matching
│   ├── qmode_store.py / dmode_store.py / settings_store.py  # per-user prefs
│   └── requirements.txt
├── xdmovies_state/                # runtime data (gitignored): cookies, prefs,
│                                  # checkpoints, autodetect/keep-alive/usage/shutdown state
├── deploy.sh                      # VPS setup (apt + venv + cloak + systemd + awk/getopt repair)
├── start.sh                       # wrapper (xvfb-run + .env sanity check)
├── start_bot.cmd                  # Windows launcher
├── xdmovies_kill_all.sh           # nuclear reset (RESTART=1 to also restart)
├── run.py                         # thin forwarder to `python -m xdmovies`
├── AGENTS.md                      # agent source-of-truth (architecture + conventions)
├── DEPLOY.md                      # VPS walkthrough + CloakBrowser verification
└── DRIFT_REPORT.md                # doc-vs-code drift notes
```

---

## 12. Bandwidth & cost

$0 (CloakBrowser free tier, no VPN/proxies/Docker/keys; Oracle free tier
covers traffic ~1000× over). The bot never downloads movie files — only
links; GB-sized downloads happen on your own device. Per gate solve ≈
5–15MB VPS-side (page + ad assets + screenshot stream); keep-alive ≈
~115MB/day worst case; autodetect adds one catalog page load per 30 min
(negligible next to keep-alive).

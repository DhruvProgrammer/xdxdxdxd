> **Authored and owned by Bruno.** Copyright (c) 2026 Bruno — all rights
> reserved. Proprietary and unlicensed: see [`COPYRIGHT`](COPYRIGHT). This
> notice must not be removed, obscured or altered.
> Repository fingerprint: `BRUNO-E1CCB1FB`

# AGENTS.md — xdmovies_bot_repo

Telegram bot that scrapes movie download links from xdmovies.wtf, bypassing
Cloudflare Turnstile via: (1) CloakBrowser (patched Chromium) cookie
injection, (2) VPS token solve-server on `:8080` for human fallback,
(3) 25-minute post-level keep-alive. No VPN layer — the bot runs
direct from the VPS IP.

**Source of truth = the executable code.** `README.md`, `DEPLOY.md`,
`DRIFT_REPORT.md`, and the parent `CONTEXT.md` describe the current
architecture. The two `[ARCHIVED]` files (`CONTEXT_stealth.md`,
`future_plan.md`) describe the v2/v3 eras and should be ignored.

---

## 1. Run / verify

There is **no test framework, no linter, no typecheck**. Verification is a
two-step combo: offline import smoke test, then live Telegram.

```bash
# 1. Offline smoke test (imports 5 modules + loads .env)
python -m xdmovies --check
# or via the forwarder:
python run.py --check

# 2. Start the bot (long-running; needs TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_ID)
python -m xdmovies
# VPS uses systemd: sudo systemctl start xdmovies-bot
```

`--check` imports `bot_config, extractor, cookie_store, pipeline, bot` and
prints cookie store status. It does **not** verify CloakBrowser can launch
— the real test is the next solve in Telegram.

Quick syntax sweep: `python -m compileall xdmovies` (no tests).

## 2. Deploy (VPS 132.145.15.58)

`deploy.sh` is the canonical entry point and is the only path that
installs the systemd unit at `/etc/systemd/system/xdmovies-bot.service`.
It runs `xvfb-run -a` to give the bot a virtual X server because
**Chromium is always launched headed (`headless=False` is hardcoded in
`bot.py:263` and every launch site)**.

`deploy.sh` repairs broken `/usr/bin/awk` and `/usr/bin/getopt` symlinks
before starting the unit — some minimal Ubuntu images ship dangling
alternatives symlinks and `xvfb-run` exits 127 with "awk: not found".

Env-gated optional features:
- `XDMOVIES_ENABLE_VNC=1` → installs XFCE + x11vnc + noVNC (real desktop
  for first manual solve; noVNC at `:6080`).

`xdmovies_kill_all.sh` is the nuclear reset: stops the unit, kills all
`xvfb-run`/`chromium`/`playwright` processes, frees port 8080, removes
the lockfile. Set `RESTART=1` to also restart the service.

## 3. The 3-stage CF flow

CF is on **posts** (`/movies/<slug>`), not on `huh.xdmovies.wtf/` home.
Keep-alive must hit a deep link, not home. The flow:

1. `pipeline.py` — fetch movie page with stored `cf_clearance` injected
   via CloakBrowser. On challenge → `CFChallengeError`.
2. `bot.py:_send_solve_link` — opens a VPS token at
   `http://132.145.15.58:8080/solve/<token>` via `solve_server.py`. The
   user taps the Turnstile checkbox; the server auto-captures
   `cf_clearance` and the original Telegram message is auto-deleted (to
   hide the VPS IP).
3. `link_follower.py` + `link_exec.py` — follow the redirect through
   `latestnewsonline.sbs` and drive the Generate→Continue→Turnstile→Go
   gate to the final HubCloud / GDrive URL.
4. `keep_alive.py` — every 25m silently visits a remembered post URL
   with stored cookies; nudges the Turnstile frame at (18,32) on light
   challenges, escalates to a fresh VPS token + owner notification on
   `need_human`.

## 4. Non-obvious conventions

- **CloakBrowser crash-loudly policy** (`cloak_stealth.py:46`): the bot
  imports `cloakbrowser` at module load and `raise`s on failure. There
  is **no silent fallback to vanilla Playwright**. Missing →
  `systemd` restart-loops visibly in `journalctl`. The fix is
  `pip install 'cloakbrowser>=0.5.0'` (triggers a one-time ~200MB
  Chromium download).

- **All 5 launch sites use `cloak_stealth.launch_chromium`** (not
  `launch_persistent_chromium`, which is for solve_server / keep_alive
  / eternal_bypass). Sites: `pipeline.py:82`, `link_follower.py:56`,
  `link_exec.py:386` (×2), `solve_server.py:214`, `keep_alive.py:284`.
  Do NOT call `async_playwright()` anywhere — the wrapper exposes a
  Playwright-shaped adapter (`new_page` / `new_context` / `close`).

- **`browser_stealth.py` provides gate-page stealth injection.**
  It exposes `is_chromium_family()` + `stealth_plugin()` for the
  `link_exec._gate_via_manual()` flow. The old `chromium_launch_kwargs`
  is still there for back-compat. Browser launch + proxy are handled
  by `cloak_stealth.py`; this file is the gate-page anti-ad / stealth
  init script, not a browser launcher.

- **Headless is hardcoded false** at every launch site (19 occurrences
  across the package). The `headless` arg is passed for API parity but
  always set to `False`. The VPS uses `xvfb-run` to provide `$DISPLAY`.

- **CLOAKBROWSER_CACHE_DIR=/opt/cloakbrowser** is set in the systemd
  unit so the 200MB Chromium binary survives restarts. First install
  downloads the binary to that path; subsequent restarts reuse it.

- **No VPN layer.** The bot connects from the VPS IP (`132.145.15.58`)
  directly. Cloudflare's datacenter-ASN fingerprinting will reduce
  `/solve` success rate vs. a residential IP. If you need higher
  success, route CloakBrowser through a residential SOCKS5 proxy by
  passing `proxy={"server": "socks5://host:port", ...}` to
  `launch_chromium()` at the call sites.

- **Telegram single-instance lock.** `bot.py` opens
  `.xdmovies_bot.lock` (in the repo root, one level above `xdmovies/`)
  with `fcntl.flock` on POSIX. A second `python -m xdmovies` on POSIX
  exits(2) instead of starting (two pollers would split updates between
  instances with zero errors). On Windows, the lock falls back to a
  mtime-based stale check + psutil.

- **Access control is silent.** `bot.py:_on_text` and `_cmd_*` return
  without replying if the user ID is not in `TELEGRAM_OWNER_ID` or
  `TELEGRAM_ADMIN_IDS`. Only `/start` (and the unauthorized reply)
  tells a stranger their ID; everything else is a black hole for
  non-allowed users.

- **Per-card CFChallengeError in `link_exec.py` is retried** by creating
  a fresh VPS token. The same solve-link message + auto-delete + retry
  happens for every blocked stage, not just the first.

- **HTML parse mode with fallback.** `bot.py:_send_safe` retries the
  same chunk in plain text on `BadRequest` (entity parse tripped) so
  the user still gets the message. Unescaped `<` from extracted page
  content is the usual culprit.

- **Cookie dump on extraction failure.** When the movie page returns no
  cards, `bot.py:_run_for_user` writes the page to
  `xdmovies/xdmovies_dumps/latest_movie_page.html` for debugging.
  Empty-results errors distinguish "stub/error page" (title checks for
  `404`, `not found`, `error`, `oops`, `template`) from "real page
  with empty download section".

- **`eternal_bypass.py` is operator-only.** Single-shot manual-solve
  helper. Not part of the bot flow; intended for `python -m xdmovies.eternal_bypass`
  on the VPS. Runs with the VPS IP directly (no VPN layer).

## 5. Files worth knowing

- `xdmovies/cloak_stealth.py` — the only browser launcher. Read this
  before touching any launch site.
- `xdmovies/cookie_store.py` — `cf_clearance` JSON store at
  `xdmovies_state/cf_cookies.json`. `CFChallengeError`, `cf_prompt_text`,
  `status_text` for `/cookies` and `/status`.
- `xdmovies/solve_server.py` — `aiohttp` server on `0.0.0.0:8080`,
  public base `http://132.145.15.58:8080`. Serves `/solve/<token>`
  HTML (with a `<img /shot>` JPEG pulled from the CloakBrowser session
  every 2s) and forwards click events to `page.mouse.click`. Auto-captures
  the cookie on `ctx.cookies()` change and signals the waiting bot.
- `xdmovies/keep_alive.py` — 25m silent refresh loop. Targets
  `xdmovies_state/refresh_targets.json` (posts the bot solved recently),
  NOT the home page. `REFRESH_AFTER=25m`, `CHECK_INTERVAL=30s`.
- `xdmovies/link_exec.py` — drives the latestnewsonline.sbs gate.
  Speed-tuned defaults: `step1_wait=8.0`, `step2_wait=8.0`,
  `final_wait=20.0`, `ad_impression_wait=1.5`. Imports
  `is_chromium_family` + `stealth_plugin` from `browser_stealth.py`
  for the gate-page anti-ad init script (call site: `_gate_via_manual`).
- `xdmovies/browser_stealth.py` — gate-page stealth + init script.
  `playwright_stealth.Stealth()` plugin + `is_chromium_family()` helper.
  Used by `link_exec.py` only. Browser launch itself is in
  `cloak_stealth.py`; this is the per-page init script.
- `xdmovies/qmode_store.py` + `xdmovies/dmode_store.py` +
  `xdmovies/settings_store.py` — per-user prefs in
  `xdmovies_state/{qmode,dmode,settings}.json`. `/qmode` (quality
  picker, `qp:` callbacks, `_QPICK`) and `/dmode` (section picker,
  `dp:` callbacks, `_DPICK`) are independent ON/OFF toggles; when both
  are ON the chain is dmode→qmode with fallback to the dmode set on
  empty intersection. A preset scrape channel never suppresses either
  picker (only silent/channel-routed jobs skip). `/settings` toggles
  result-card detail lines (Size/Rating/Genre/First Air/Audios/Sources/
  TMDB) via `settings:` callbacks; page meta comes from
  `extractor.extract_page_meta` + `extract_tmdb_code` through
  `pipeline.ExtractionResult` into `Job.page_meta`/`detail_fields`.

## 6. Environment / secrets

There is **one** env file: `xdmovies_bot_repo/xdmovies/.env`, created on
the VPS at deploy time. A safe-to-commit template at
`xdmovies/.env.template` shows the exact format.

| File (on VPS) | Created by | Edited on | Template (in repo) |
|---|---|---|---|
| `xdmovies_bot_repo/xdmovies/.env` | `deploy.sh` (line ~190) | `nano xdmovies/.env` (one-time, then on token rotation) | `xdmovies/.env.template` |

`bot_config._RedactTelegramToken` strips the bot token from any log line
that matches `/bot<token>/`. Logs go to `xdmovies_bot.log` and errors
to `xdmovies_bot_errors.log` (paths overridable via
`XDMOVIES_BOT_LOG` / `XDMOVIES_BOT_ERROR_LOG`).

## 7. Common operations

```bash
# Restart cleanly after config change
sudo systemctl restart xdmovies-bot && journalctl -u xdmovies-bot -f

# Hard reset (kills stale xvfb, frees 8080, removes lockfile)
bash xdmovies_kill_all.sh            # leaves service stopped
RESTART=1 bash xdmovies_kill_all.sh   # also restarts

# Verify CloakBrowser
sudo -u dhruv venv/bin/python -c "import cloakbrowser; print(cloakbrowser.__version__)"
ls -la /opt/cloakbrowser/   # binary cache

# Inspect a stored cookie
grep -A2 'xdmovies.wtf' xdmovies/xdmovies_state/cf_cookies.json
```

## 8. Do not touch without re-validating

- The 3-stage flow files (`pipeline.py`, `link_follower.py`,
  `link_exec.py`, `solve_server.py`, `keep_alive.py`) — each has
  hand-tuned waits and click coordinates that the operator benchmarked
  per `CONTEXT_stealth.md` (archived but still accurate for the values).
- `cloak_stealth.py` API surface — the 5 launch sites depend on
  `new_page()` / `new_context()` / `close()` matching Playwright
  Browser shape.
- The systemd unit's `CLOAKBROWSER_CACHE_DIR=/opt/cloakbrowser` env
  var — removing it re-downloads 200MB on every restart.

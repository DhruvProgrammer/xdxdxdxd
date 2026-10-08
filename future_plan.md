> **Authored and owned by Bruno.** Copyright (c) 2026 Bruno — all rights
> reserved. Proprietary and unlicensed: see [`COPYRIGHT`](COPYRIGHT). This
> notice must not be removed, obscured or altered.
> Repository fingerprint: `BRUNO-E1CCB1FB`

# [ARCHIVED] Future Plan — Linux VPS Cloudflare Bypass (Fortress + Raw CDP)

> **Status:** deferred. Not implementing now. This document is the reference when the VPS deploy is ready to start.
> **Date drafted:** 2026-08-13
> **Scope (locked in by user):** "Just the VPS Cloudflare fix" — minimal blast radius. No README, no rollback scripts, no healthcheck, no cookie-domain fix, no other bundled changes.

---

## 1. Context

The cf-bypass sidecar (`third_party/cf-bypass`) currently uses `rebrowser-puppeteer-core` driven against a local Chromium launched by `chrome-launcher` (`br.ts:200`). On Linux it requires `xvfb-run` (the existing Dockerfile) because `br.ts:163` throws without `DISPLAY`.

The 2026 anti-detect benchmark (651 verdicts) places `rebrowser-puppeteer` in the **bottom tier** — the same tier as vanilla Playwright. Cloudflare fingerprints the Playwright/CDP protocol handshake (`Runtime.enable` / `Target.setAutoAttach`) and hard-blocks the browser regardless of headless/headed.

**Linux + `xvfb-run` fixes the *display* requirement but does NOT fix the *driver fingerprint* issue.** Whether it works for xdmovies depends on the challenge mode xdmovies serves to the VPS IP/UA. The SquidProxy datacenter IP is a separate, possibly worse risk — but it already works locally today, so the IP itself is not the blocker.

**Solution:** swap the browser host from local `rebrowser-puppeteer` to **Fortress** (`tilion/fortress:latest`) — a C++-patched Chromium exposed over **raw CDP** on `:9222`, with no Playwright shim and no `Runtime.enable` leak. This fixes both the protocol layer (raw CDP) and the fingerprint layer (C++ patches) in one swap. Per the benchmark, Fortress is the top performer and clears live Cloudflare Turnstile from a datacenter IP.

---

## 2. Locked-in decisions

| Decision | Value |
|---|---|
| **Path** | **Path 2 — Fortress over raw CDP** (drop-in for the cf-bypass browser) |
| **Driver** | `rebrowser-puppeteer-core` retained only for the `puppeteer.connect({ browserURL })` call. No Playwright shim in the protocol stack once connected to Fortress. |
| **Proxy** | **Keep SquidProxy** — `p102.squidproxies.com:9093:1352:23CfS1Bz7oF0`. Passed to Fortress via `--proxy-server=...`; auth still via `page.authenticate()` in `iuam.ts:30-34`. |
| **`xvfb-run`** | **Removed from the cf-bypass Dockerfile** — cf-bypass no longer opens a window. Fortress container runs its own headed Chromium. |
| **`DISPLAY` throw** | **Removed** in `br.ts:163` — not required when connecting to an external CDP endpoint. |
| **Scope** | VPS Cloudflare fix only. No README, no rollback scripts, no healthcheck, no cookie-domain fix, no other bundled changes. |

**Security note:** the SquidProxy credential `23CfS1Bz7oF0` will end up in plaintext in `deploy/cantarella/docker-compose.yml`. **Rotate this credential before the VPS goes live** since the repo may be tracked by git or shared.

---

## 3. Files to change (4 files)

### 3.1 `third_party/cf-bypass/src/lib/browser/br.ts`

Replace the `connect()` function (lines 150-256) so it connects to a running Fortress over CDP instead of launching a local Chromium with `chrome-launcher`.

**Remove:**
- The `DISPLAY` throw at line 163 (`if (process.platform === "linux" && !process.env.DISPLAY) throw ...`).
- The headed-args filter at line 167 (`const headedArgs = args.filter(...)`).
- The `chrome-launcher` import at line 159 and the local `launch({...})` call at line 200.
- The `chrome.kill()` / `kill(chrome.pid)` cleanup paths (lines 93-94, 250-253). Fortress is managed externally, not killed by this process.

**Keep:**
- The `puppeteer = require("rebrowser-puppeteer-core")` import (line 161) — `puppeteer.connect({ browserURL })` is the only call site we need.
- The `proxy.host/proxy.port` assembly (lines 172-174, 192-194) — these no longer feed `--proxy-server` to `chrome-launcher`; instead, the proxy is set on the Fortress container at launch. **But** `proxy.username/password` still flow to `page.authenticate()` in `iuam.ts:30-34`, which is unchanged.
- The `pageController` setup (lines 224-247) — operates on the page returned by `browser.newPage()`, which works the same whether the browser is local or remote.
- The `customConfig`, `ignoreAllFlags`, `plugins`, `connectOption` options for forward compatibility (most unused once `chrome-launcher` is gone).

**Replace lines 200-219 with:**
```ts
const cdpUrl = process.env.FORTRESS_CDP_URL || "http://127.0.0.1:9222";
const browser = await puppeteer.connect({
    browserURL: cdpUrl,
    ...connectOption,
});
```

The returned `browser` is a standard Puppeteer `Browser` object. `browser.pages()`, `browser.newPage()`, `page.goto`, `page.setRequestInterception`, `page.on('response')`, `page.cookies()`, `page.wait_for_selector` — all the APIs used by `iuam.ts` and the rest of the sidecar — work identically over raw CDP.

**Risk:** low. `rebrowser-puppeteer-core.connect({ browserURL })` is documented to support any CDP endpoint, including non-Playwright ones. Fortress speaks stock Chromium DevTools Protocol.

### 3.2 `third_party/cf-bypass/src/index.ts`

Read `FORTRESS_CDP_URL` and pass it to `connect()`.

**Changes:**
- Around lines 9-16: keep the `browserProxy` env-var block (still used for `page.authenticate`). No structural change.
- Around line 4: the `import { connect }` is already there. No new import.
- In `initBrowser()` (lines 65-88), pass the new option into the `connect({...})` call:
  ```ts
  browserInit = connect({
      turnstile: true,
      proxy: browserProxy,
      connectOption: { defaultViewport: null },
      fortressCdpUrl: process.env.FORTRESS_CDP_URL,
  })
  ```
  (`fortressCdpUrl` is a new option in the `Options` interface in `br.ts:135-143`.)
- `/health` (lines 61-63): unchanged — still reports whether the browser is connected (now to Fortress, not local Chrome).

**Net change:** ~3 lines in `index.ts` + 1 new field in the `Options` interface in `br.ts`.

### 3.3 `third_party/cf-bypass/Dockerfile`

Drop `xvfb-run` from the cf-bypass entrypoint.

**Change:**
- Remove `xvfb-run` from the `CMD` / `ENTRYPOINT`. The cf-bypass process no longer opens a window (it only talks to Fortress over HTTP/WS), so the virtual display is no longer needed.
- New `CMD` example: `["node", "dist/index.js"]` (or whatever the existing `node` invocation is, minus the `xvfb-run` prefix).
- Keep the existing system packages (`xauth`, `curl`, `fonts-liberation`, etc.) for now — harmless and they may help with fonts/ICU for the Node process.

**Net change:** 1 line in the Dockerfile.

### 3.4 `deploy/cantarella/docker-compose.yml`

Add the `fortress` service and wire `cf-bypass` to it.

**Add a new service:**
```yaml
fortress:
  image: tilion/fortress:latest
  container_name: fortress
  restart: unless-stopped
  ports:
    - "9222:9222"
  command:
    - --remote-debugging-port=9222
    - --remote-debugging-address=0.0.0.0
    - --no-sandbox
    - --proxy-server=http://p102.squidproxies.com:9093
    - --user-data-dir=/data/profile
  volumes:
    - fortress-data:/data
  shm_size: 1g
```

**In the existing `cf-bypass` service:**
- Add `depends_on: [fortress]`.
- Add the new env var: `FORTRESS_CDP_URL: http://fortress:9222`.
- Keep the existing `CANTARELLA_PROXY_HOST/PORT/USERNAME/PASSWORD` env vars (they still drive `page.authenticate()` in `iuam.ts`).

**Add a top-level volume:**
```yaml
volumes:
  fortress-data:
```

**Net change:** ~20 lines in `docker-compose.yml`.

---

## 4. Files NOT changed (deliberately, per locked-in scope)

- `third_party/cf-bypass/src/lib/ends/iuam.ts` — unchanged. The `page.authenticate()` call at lines 30-34 keeps working against Fortress (stock Chromium network layer).
- `third_party/cf-bypass/src/lib/ends/turnstile.ts` — unchanged.
- `xdmovies/live_solver.py` — unchanged. The body-wait fix and the cookie-domain fix from this session are already in the repo and orthogonal to the VPS deploy.
- `xdmovies/link_exec.py` — unchanged.
- `xdmovies/bot.py` — unchanged.
- `xdmovies/scrape_all.py`, `xdmovies/pipeline.py`, `xdmovies/extractor.py` — unchanged.
- No README, no rollback scripts, no healthcheck, no `proxy_tester` integration.

---

## 5. Deployment order (when ready)

1. **Rotate the SquidProxy credential** before building the image — it will end up in plaintext in `docker-compose.yml`.
2. **Rebuild cf-bypass image** (modified `br.ts` + `index.ts` + Dockerfile):
   ```bash
   cd xdmovies_bot_repo/deploy/cantarella
   docker compose build cf-bypass
   ```
3. **Pull Fortress:**
   ```bash
   docker compose pull fortress
   ```
4. **Start Fortress:**
   ```bash
   docker compose up -d fortress
   ```
5. **Validate Fortress is reachable** from inside the cf-bypass container:
   ```bash
   docker compose exec cf-bypass curl -s http://fortress:9222/json/version
   ```
   Must return Chromium version JSON. If it doesn't, check the Docker network and the fortress container logs (`docker compose logs fortress`).
6. **Start cf-bypass:**
   ```bash
   docker compose up -d cf-bypass
   ```
7. **End-to-end validation** — send a Telegram message to the bot with an xdmovies URL (e.g. `https://top.xdmovies.wtf/movies/coco-2160p-1080p-hindi-english-download-354912`). The bot logs should show:
   - `chromium connected to http://fortress:9222`
   - `cf_clearance` obtained
   - Total solve time **8–15s** (the benchmark expectation for raw CDP + C++ patches on light Turnstile).
8. **Validate the proxy is being used** — from inside the cf-bypass container:
   ```bash
   docker compose exec cf-bypass curl --proxy http://p102.squidproxies.com:9093 https://api.ipify.org
   ```
   Must return SquidProxy's egress IP, not the VPS's. If the VPS bypasses the proxy, that's a misconfiguration to fix in `docker-compose.yml`.

---

## 6. Rollback (if it fails on the VPS)

1. **Stop cf-bypass:**
   ```bash
   docker compose down cf-bypass
   ```
2. **Revert `br.ts`** to use `chrome-launcher` + local Chromium (re-add the `DISPLAY` throw, the `launch({...})` call, the `chrome.kill()` cleanup).
3. **Revert `index.ts`** to drop the `fortressCdpUrl` pass-through.
4. **Re-add `xvfb-run`** to the cf-bypass `Dockerfile` entrypoint.
5. **Rebuild + restart:**
   ```bash
   docker compose build cf-bypass && docker compose up -d cf-bypass
   ```
6. The `fortress` container can stay running (harmless) or be removed: `docker compose down fortress`.

The rollback restores the exact pre-change behavior. No data is lost; no other services are affected.

---

## 7. Why Path 2 over the alternatives (recorded for the future)

| Path | Driver | Cost | Posture |
|---|---|---|---|
| **1 — Linux + xvfb + keep rebrowser** | rebrowser (bottom-tier benchmark) | zero code | weakest; fails if xdmovies escalates to managed CF |
| **2 — Fortress CDP (this plan)** | raw CDP, C++ patches (top-tier benchmark) | ~30 lines in `br.ts` + 1 docker-compose service | best long-term; recommended |
| 3 — CurlWright nodriver (Python rewrite) | raw CDP, no fingerprint fix | biggest rework; changes the `cf_clearance`-only contract | fixes protocol only; loses sidecar architecture |

**Why Fortress over CurlWright:** Fortress fixes both the protocol layer (raw CDP, no Playwright shim) and the fingerprint layer (C++ patches). CurlWright/nodriver only fixes the protocol layer — it can't touch canvas/WebGL (per the ByteTunnels analysis: "nodriver cannot modify canvas/WebGL because it does not alter Chrome's rendering engine"). For xdmovies *light* Turnstile this is fine, but for any future escalation Fortress is strictly more robust.

**Why Fortress over CloakBrowser:** CloakBrowser's free binary is v146 (older Chrome). Fortress is actively updated (v151 as of this writing), BSD-3 open source, and has documented `tools/gauntlet.py` reproducibility. The 2026 benchmark puts Fortress ahead of CloakBrowser.

**Why keep `rebrowser-puppeteer-core` as the client:** the cf-bypass sidecar is already written against the Puppeteer API (`page.goto`, `page.setRequestInterception`, `page.on('response')`, `page.cookies()`). Switching to vanilla `puppeteer-core` would be a one-line import change but is not required — `rebrowser-puppeteer-core.connect({ browserURL })` is fully compatible with non-Playwright CDP endpoints.

---

## 8. Open risks (orthogonal to this plan, recorded for awareness)

- **Datacenter IP reputation.** The benchmark unanimously says datacenter IPs get blocked regardless of browser stealth. This plan assumes the VPS sidecar actually routes through SquidProxy (validated in step 8 of the deployment). If xdmovies later starts blocking your SquidProxy IP, the fix is to add a residential proxy pool — the `xdmovies/proxy_tester.py` + `proxy_results.json` infrastructure already exists for this.
- **Fortress container upgrades.** The `latest` tag tracks the upstream release. For reproducibility, pin to a digest (`tilion/fortress@sha256:...`) once a known-good image is validated.
- **`page.authenticate()` over Fortress's `--proxy-server`.** This combination is standard Chromium behavior and *almost certainly* works, but it has not been empirically validated in this codebase. The staging spike (deploy to a throwaway VPS first, time a solve, inspect logs) is the right way to confirm.
- **CF escalation from light → managed.** If xdmovies serves *managed* Turnstile to the VPS IP/UA, the solve time will increase from 8–15s to 30–60s. The existing `solve_live_v2` light-mode loop should still clear it (the passive-wait + nudge logic is mode-agnostic), but expect longer per-solve latency.

---

## 9. Companion changes that were considered and explicitly EXCLUDED

- **Cookie-domain fix for the gate flow** (`latestnewsonline.sbs` issues `cf_clearance` on `.cloudflare.com`, not the gate's domain). The current `link_exec.py:execute_button_flow` already has a cookie-duplication fix (added this session) that handles this. Excluded from this VPS plan to keep scope tight.
- **Body-wait fix in `solve_live_v2`** (the House of the Dragon "head-only" bug fixed this session). Already in the repo. Excluded from this VPS plan.
- **Per-item skip in `/scrapeall`** (one bad slug shouldn't fail a 174-page scrape). Excluded.
- **Hardening `extract_cards`** to also match `data-href` and relative-href forms. Excluded.
- **README updates** describing the VPS deploy. Excluded — user said "Just the VPS Cloudflare fix."
- **Healthcheck** on the fortress container. Excluded.
- **Persistent browser profile volume** for Fortress. Included as a `volumes:` entry, but the profile is optional (cookie reuse across container restarts is a nice-to-have, not a correctness requirement).

---

# ACTIVE PLAN — Public Colab Solve API (added 2026-09-07; supersedes archived content above)

> **Status:** planned, not yet built. Everything above this line is `[ARCHIVED]`
> v2/v3-era material (Fortress sidecar, SquidProxy, `deploy/cantarella`) and
> does not describe the current stack (CloakBrowser + systemd + no VPN).
> **Date drafted:** 2026-09-07
> **Scope (locked in by user):** public bypass API for Google Colab users.
> Anyone pastes ~5 lines into Colab; the VPS does the 60-90s browser work;
> Colab polls; the direct link is returned as JSON.

## 1. Goal

A shared HTTP endpoint on the VPS (`132.145.15.58`) that accepts a
gate/movie URL and returns the final direct link, so individuals can solve
from Google Colab (or curl) without running any browser code themselves.
The Telegram bot keeps working directly and untouched; the API imports the
same flow code but runs as a separate service so traffic spikes never stall
the Telegram queue.

## 2. Architecture

```
colab / curl ──HTTPS──▶ Caddy (auto-TLS) ──▶ bypass-api:8090 ──▶ job queue ──▶ browser pool
                                                                          (CloakBrowser, persistent,
                                                                           cap 2 concurrent)
                                                     │── API keys + quotas (anonymous + keyed tiers)
                                                     │── SSRF host allowlist
                                                     └── reuses link_exec / extractor /
                                                         cookie_store / hub cache
```

- **Async jobs, not sync requests.** `POST /api/v1/bypass {"url": ...}` →
  `{"job_id": ..., "position": N}` in ~1s;
  `GET /api/v1/result/<job_id>` polls `pending | working | done | error`.
  Colab cells and plain HTTP clients cannot hold 90s connections reliably.
- **Persistent browser pool.** One CloakBrowser process, one context per
  job, cap 2 concurrent (RAM is the ceiling). Saves the ~3-5s launch cost
  per request vs the bot's launch-per-link pattern.
- **Reuse, don't rewrite.** Workers call the existing tested flow
  (`execute_button_flow`, hub parse/cache). Every bot fix benefits the API.

## 3. API contract (v1)

- `POST /api/v1/bypass` — `{"url": "..."}` (+ `Authorization: Bearer <key>`
  for the keyed tier) → `{"job_id": "...", "position": 0}`
- `GET /api/v1/result/<job_id>` →
  `pending | working | done | error` plus
  `final_url / title / size / elapsed_s` or typed `error`
  (`unsupported_host`, `cf_challenged_retry_later`, `timeout`,
  `no_destination`, `rate_limited_reset_after`, `queued_behind_N`)
- `GET /healthz`, `GET /queue` (key-gated; shows load)
- JSON-only, CORS open (`*`), snippet prints queue position + ETA so
  polling never looks hung.

## 4. Access tiers

- **Anonymous:** strict per-IP limits (start ~3 solves/day, 1 concurrent) —
  enough to try, useless to abuse at scale. Keys can't stay secret in
  public notebooks, so anonymous abuse control is rate-based, not auth-based.
- **Keyed (free, DM'd):** higher quota (start ~50/day). Per-user keys so one
  abuser loses one key, not the service.

## 5. Security (non-negotiable for public use)

1. **SSRF allowlist.** Only gate/movie/hub hosts accepted
   (`latestnewsonline.sbs`, `top.xdmovies.wtf` / `xdmovies.wtf`, `hubcloud`,
   `hubdrive`, `gamerxyt`, `gdflix`, …). Everything else (including cloud
   metadata `169.254.169.254` and internal ranges) → `unsupported_host`.
2. **Limits:** per-IP + per-key rate windows, max 2 concurrent solves
   VPS-wide, queue depth cap with `busy_retry_later`, 3-minute per-job kill.
3. **IP-reputation guard:** every public solve burns the VPS IP's standing
   with Cloudflare/hub hosts. Monitor solve success rate; auto-throttle the
   anonymous tier on dips. One env kill-switch (`BYPASS_PUBLIC=0`) drops to
   keyed-only instantly.
4. **Logging:** hashed IPs (not raw), response tokens redacted like the bot
   token is today.

## 6. Deployment

- Same VPS, new unit `bypass-api.service`, `aiohttp` (matches
  `solve_server.py`, no new deps), port `8090` on localhost only; **Caddy**
  in front for automatic HTTPS. Requires a domain/subdomain — raw-IP HTTPS
  won't validate, and public Colab users need a trustworthy URL.
- Secrets in the `.env` pattern: `BYPASS_API_KEYS`, `BYPASS_MAX_CONCURRENT`,
  quotas. Coexists with `xdmovies-bot` (different port, shared imports).

## 7. Throughput reality (read before promising anything)

2 concurrent x ~75s per solve = **under ~100 solves/hour total**. Fine for
individuals, not for a crowd — quotas must reflect that. A wave of
anonymous users also increases Cloudflare friction for the owner's own bot;
that is what the auto-throttle + kill-switch are for.

## 8. Build phases

1. **Core service** — aiohttp app: auth, job queue, worker, result store.
   Mock-test locally with fixtures **including an evil-URL refusal suite**
   (metadata IP, internal hosts, non-gate domains — all must refuse).
2. **Colab snippet + docs** — copy-paste `requests` submit/poll snippet;
   deploy unit + Caddy; live curl test + real Colab notebook test.
3. **Watch a week** — success-rate / queue-depth metrics; tune quotas or
   throttle. Decision rule: API healthy + escalation-solve ~0 → token
   system removable; escalations solve → keep.

## 9. Open items (need owner input)

- Domain/subdomain for HTTPS (required for public trust).
- Anonymous quota comfort: open-but-tiny (~3/day) to start, or keyed-only
  at launch with anonymous added later?
- Bot calls API internally (single path) vs stays direct (recommended v1:
  stays direct — fewer moving parts in the working system).

## 10. Parked findings (measured 2026-09-07, do not re-learn)

- `_gate_via_api` recipe (in-page session/rebind/complete + widget reset
  for a late-minted Turnstile token): **HUB-OK 47.3s local** vs click flow
  73.1s-to-failure on the same gate code. Reverted (`eb8820b`) after VPS
  showed +~60s additive cost on miss (datacenter Turnstile demands
  interaction; polls burn 20s+25s before fallback). Retry with tight poll
  budgets or VPS-side disable; prototype at
  `Temp/opencode/proto_api_fastpath.py` (outside repo).
- Gate `challenge_ts` rule: instant auto-pass tokens get
  `400 "Please wait for the timer to complete"`; token must be minted late.
- Gate codes are reusable for session creation; JWT is IP-locked, ~1h TTL.
- Token-system waits (bot.py: 120s movie / 240s gate / 240s fallback):
  decision deferred pending VPS escalation-fire metrics (measure-first).

## 11. Parked — wpformat-bot (formatter bot, deferred, added 2026-09-08)

> **Status:** planned, not built. Owner deferred on 2026-09-08 ("for now
> i am not doing it"). No code, no token, no deploy until re-activated.

### Goal

A second Telegram bot that removes all retyping between the xdmovies bot
and the WordPress movie site. Owner forwards a Bot-1 resolve message;
Bot-2 replies with paste-ready text for the movie post's Download Links
meta box. **Bot-1 is frozen** — it is never modified for this project
(rendered-text parsing with pinned fixtures instead).

### Output spec (locked)

Movie-post Download Links grammar, one per line (per the meta-box
placeholder `4K ULTRA|https://example.com/movie-4k.mp4|2.1 GB`):

```text
🎬 The Assessment (2025)
TMDB: 1317088

720p BluRay|https://gamerxyt…|916.78 MB
1080p BluRay|https://gamerxyt…|2.91 GB
```

- Quality labels condensed from filenames to the box's style
  (`1080p BluRay`, `2160p WEB-DL`, `4K ULTRA` …). Codec/audio detail
  (`x265`, `DDP 5.1`) stays out. Exact condensation mapping awaiting
  owner's taste check.
- URL is always the final resolved URL (gamerxyt/hub), never a gate or
  movie-page link. Missing size → line still emitted with blank size +
  `⚠️` note, never silently dropped.
- Header block (clean `Name (Year)` + TMDB ID when known) feeds the
  existing TMDB importer step.
- **Franited Links is explicitly out** — dropped from the chain by owner
  on 2026-09-08. No shorten step; paste goes straight to the post.

### Architecture (when built)

- Separate repo `wpformat-bot/` (`wpbot/bot.py`, `wpbot/formatter.py`,
  `requirements.txt` with `python-telegram-bot>=21,<22`), separate token,
  separate systemd unit (`wpformat-bot.service`), same VPS. Polling only
  (no ports). Zero shared code/state with the xdmovies bot.
- Owner-only allow-list, strangers silently ignored (same pattern).
- Parser: pure functions + `SAMPLES.md` fixtures from real forwarded
  messages; unparseable lines quoted back under `⚠️ review manually`.
- No site access, no credentials, no writes — worst-case failure is a
  bad reply, never a corrupted post.

### Still needed to build

1. 2–3 real forwarded Bot-1 resolve messages (parser fixtures).
2. Quality-label taste answer (short vs fuller labels).
3. Second bot token from BotFather + VPS deploy approval.
4. (Later, optional) TMDB API clean-name resolution; direct WP posting
   only after further study — out of v1 scope.

## 12. Planned — xdmovies bot ↔ 4kbolly.py connection (added 2026-09-29)

> **Status:** planned, not built. Awaiting direction choice (A, B, or both)
> plus the API prerequisites below.

### Hard constraint

Telegram bots never receive messages from other bots — not in DMs, not in
groups, not even as admins — and cannot start chats with bots. Direct
bot-to-bot messaging is impossible, full stop. Any wiring must go *around*
Telegram (HTTP API, shared channel + listener), never *through* it.

### Direction A — 4kbolly → xdmovies → 4kbolly (recommended)

4kbolly finds a title with no links (or an xdmovies URL) → submits it to the
xdmovies job API → polls → converts `cards[]` to pipe rows → the existing
`post_to_wordpress` / `append-links` flow runs unchanged.

- Transport: `POST /api/v1/jobs` + `GET /api/v1/jobs/<id>` (+ `DELETE` for
  cancel) on the already-built `api_server.py` (`feature/wp-api-bridge`,
  tested: 33 unit + 18 live-HTTP). No Telegram messages, no chat-text
  parsing, no userbot anywhere in the loop.
- 4kbolly side: ~30 lines — one `requests` submit function, one poll loop
  (5s while queued/running, 15s while `needs_human`), one converter from API
  `cards[]` (`raw_title`/`size`/`language`/`url`/`status`) to the existing
  `LABEL|url|size` pipe rows. `needs_human` surfaces the solve URL for an
  owner tap (job auto-resumes); `failed` drops into the existing retry path.
- Queue policy: API jobs already run in the lowest-priority lane
  (urgent → normal → api) with card-boundary yield, so a bulk import never
  stalls interactive Telegram use. `/cancel` deliberately cannot touch API
  jobs.

### Direction B — xdmovies → channel → 4kbolly (half-built already)

xdmovies bot posts result batches to a channel (`post_to_channel`);
4kbolly's `parse_direct_forward` already parses exactly that card grammar
(numbered filename cards + Audio/Size rows + final hub URLs) into the WP
pipeline. Still needed: a channel listener on 4kbolly's side (pyrogram
channel-history read on an interval, or a userbot forward) feeding new
channel posts into the existing direct-forward worker, plus dedupe against
`seen_urls` so re-reads don't repost.

### Prerequisites (owner side, either direction)

1. Merge `feature/wp-api-bridge` (or deploy that branch on the VPS) — `main`
   has no API code.
2. Set `XDMOVIES_API_KEY` in `xdmovies/.env`. The API stays off and port 8090
   stays closed without it. Restart the bot; `/status` must show
   `wp-bridge: ON :8090`.
3. Direction A needs nothing else on the VPS. Direction B needs the mirror
   channel configured.

### Still needed to build

1. Direction choice (A, B, or both).
2. Direction A: 4kbolly API client (~30 lines) + `cards[]`-to-pipe converter
   + error mapping.
3. Direction B: 4kbolly channel listener + `seen_urls` dedupe for re-reads.

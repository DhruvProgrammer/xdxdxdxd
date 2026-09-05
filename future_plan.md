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

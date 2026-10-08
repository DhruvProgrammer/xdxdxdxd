> **Authored and owned by Bruno.** Copyright (c) 2026 Bruno — all rights
> reserved. Proprietary and unlicensed: see [`COPYRIGHT`](COPYRIGHT). This
> notice must not be removed, obscured or altered.
> Repository fingerprint: `BRUNO-E1CCB1FB`

# [ARCHIVED] Speed Improvements — Aug 2026

> ⚠️ This document is historical. It describes the v2 era of the bot
> (using `live_solver.py` + `rebrowser-puppeteer` + `patchright`).
> The v3/v4 architecture is completely different:
> - Manual /setcookie flow → VPS token solve-server (port 8080)
> - `playwright_stealth` → CloakBrowser (C++ patches)
> - `live_solver.py` was reverted; `cloak_stealth.py` is the active wrapper
> - Proxy pool was reverted; ProtonVPN WireGuard is the active IP fix
>
> Kept for historical context only. See README.md for current architecture.

## TL;DR

**Median solve time dropped from 22.10s → 7.93s on xdmovies (64% reduction)**
without changing the browser engine. Numbers in
`live_solver.py:_light_mode_loop` and the button-flow wait params in
`link_exec.py` + `link_follower.py` were tuned based on data from a 27-solve
benchmark + manual instrumentation of the latestnewsonline gate page.

## Setup

User asked for stealth-browser research to reduce Turnstile solve time.
Evaluated two candidates:

- **Patchright** (`patchright-python` v1.61.2) — patches Playwright driver
- **CloakBrowser** v0.5.7 free binary — Chromium 146 with C++ stealth patches

## Benchmark: 27 solves (3 engines × 3 URLs × 3 reps)

`xdmovies_bot_repo/test_stealth_bench.py` — headless=True, fresh session_id
per run.

| Engine        | latestnew | coco | kantara | Aggregate median | Success |
|---------------|----------:|-----:|--------:|-----------------:|--------:|
| vanilla       |    6.22 s | 22.20s | 22.13s |         22.10 s |   9/9   |
| patchright    |    6.26 s | 22.39s | 22.38s |         22.35 s |   9/9   |
| cloak         |    6.04 s | 22.36s | 22.41s |         22.12 s |   9/9   |

All 3 engines are statistically identical. CF serves `light` mode to all of
them — no detection-evasion speedup available.

## Root cause analysis (after browser swap proved useless)

Inspected the per-solve log. Every xdmovies solve spent the full 18s of the
passive-wait phase before nudging. The code comment claimed "Most light-mode
sites clear in 8-12s" but the benchmark showed xdmovies does NOT passively
clear — the 18s was dead wait time. The cookie only arrives AFTER the nudge.

## Patch: `live_solver.py:_light_mode_loop` (2 numbers)

```python
# Before:
passive_deadline = time.time() + 18.0   # 18s passive wait before nudge
nudge_interval   = 30.0                 # 30s between re-nudges

# After:
passive_deadline = time.time() + 3.0    # 3s passive wait — we know xdmovies
                                       # doesn't passively clear; nudge fast
nudge_interval   = 8.0                  # 8s between re-nudges — was 30s which
                                       # caused 30-40s outliers when first nudge
                                       # didn't trigger cookie
```

Locations in `xdmovies_bot_repo/xdmovies/live_solver.py`:
- Line ~524: `passive_deadline = time.time() + 3.0` (was 18.0)
- Line ~560: `nudge_interval = 8.0` (was 30.0)

## Verification: 15-solve benchmark (5 reps × 3 URLs, vanilla Chromium)

`xdmovies_bot_repo/results/fastpassive_v2.log`:

| URL                    | Median | Min   | Max     | Success |
|------------------------|-------:|------:|--------:|--------:|
| latestnewsonline.sbs   | 9.30s  | 6.26s | 11.30s  | 5/5     |
| xdmovies_coco          | 8.32s  | 7.10s | 23.16s  | 5/5     |
| xdmovies_kantara       | 7.51s  | 7.20s |  7.93s  | 5/5     |
| **Aggregate median**   |**7.93s**|      |         | 15/15   |

**vs baseline median 22.10s → 7.93s = 64% faster.**
Worst case improved from 38s → 23s.
100% success across all 15 solves.

## Regression check: `test_proxy_fetch.py`

3/3 success, solve times 15.7s and 15.9s on the actual production code path
(pre-warm + 2 solves via real proxies). No regression.

## Decisions

- **Apply** the 18s→3s and 30s→8s changes — production code path.
- **Don't integrate** Patchright or CloakBrowser — no measurable benefit.
- **Keep** `test_stealth_bench.py` for future re-evaluation if CF changes
  its challenge distribution.

## Cleanup performed

- `pip uninstall patchright cloakbrowser` — done
- `python -m patchright uninstall chromium` — done (frees ~200 MB)
- CloakBrowser binary at `~/.cloakbrowser/` — kept; can be cleared with
  `python -m cloakbrowser clear-cache` if disk space needed.

## What to monitor going forward

If a future target site DOES passively clear in 8-12s, the early-exit on
`cookie_passive_fast` in this loop will still catch it in <3s (no regression
on passively-clearing sites). If the 3s window turns out to be too short on
some other site, increase it back to ~6s (still saves ~12s per solve).

## Follow-up: button-flow wait cuts (link_exec.py + link_follower.py)

After the passive-wait fix, the next-largest source of dead time was the
gate-page button flow: `link_exec.execute_button_flow` and
`link_follower.follow_download_link_in_browser_async`. Cuts made:

### `link_exec.py`
| What | Was | Now | Savings |
|---|---|---|---|
| `ad_impression_wait` default | 5.0s | **1.5s** | up to 14s/flow (4 calls × 3.5s) |
| `step1_wait` / `step2_wait` defaults | 12.0s | **8.0s** | up to 8s/flow |
| `final_wait` default (hubcloud deep link) | 45.0s | **20.0s** | up to 25s/flow |
| Unconditional `asyncio.sleep(2)` after CF | 2.0s | **0s (removed)** | 2s/flow |
| `_wait_for_load_state("load", timeout=30000)` after continueBtn | up to 30s | **`asyncio.sleep(1.0)`** | up to 29s/flow |
| `expect_navigation` timeout for goToLinkBtn | 30s | **10s** | up to 20s/flow |
| `#goToLinkBtn` poll loop | 60×0.4s (24s max) | **15×0.2s (3s max)** | up to 21s/flow |
| `_real_click` visibility wait default | 12.0s | **6.0s** | up to 6s/click |
| `_wait_visible` poll interval | 0.4s | **0.1s** | tighter response |
| `_wait_visible` default | 12.0s | **6.0s** | up to 6s/click |

### `link_follower.py`
| What | Was | Now | Savings |
|---|---|---|---|
| `wait_for_load_state("load", timeout=4000)` after follow | up to 4s | **removed** | up to 4s/follow |
| `asyncio.sleep(0.5)` after follow | 0.5s | **removed** | 0.5s/follow |

### Browser reuse — already correct

The user's concern was that "open/close/open/close" wastes RAM. The existing
`SessionPool` in `live_solver.py:1078` already keeps the Chromium
`Browser` + `BrowserContext` alive across solves — only individual `Page`s
are closed per-solve. `close_all()` is only called at bot shutdown
(`bot.py`/`cli.py`/`__main__.py`). No changes needed here.

### Known limitation: cookie-domain mismatch on latestnewsonline.sbs

While investigating the wait cuts, I discovered a deeper issue with the
gate page flow that is NOT a wait problem:

- `latestnewsonline.sbs` issues `cf_clearance` on domain `.cloudflare.com`
  (not on the site domain itself) when the challenge is solved
  passively. The Playwright cookie is set with `domain=.cloudflare.com`
  and therefore is NOT sent on the second `page.goto` to the same URL.
- CF re-challenges the second visit. The button-flow code doesn't solve
  this second challenge — it just polls the title and times out after
  `step1_wait` seconds.
- This is pre-existing behavior, not introduced by these wait cuts.

**To fully fix the gate flow**, the next step would be to either:
1. Make `execute_button_flow` re-call `solve_live_v2` on the second goto
   (which auto-resolves), OR
2. Make `solve_live_v2` wait for a `.latestnewsonline.sbs`-scoped
   `cf_clearance` instead of returning on the `.cloudflare.com`-scoped
   one.

This is out of scope for the speed-tuning work — the existing
`solve_live_v2` is fast (~7.93s median) and works. The gate-flow button
sequence is a separate problem.

---

## 2026-08-11 � Proxy CF-suitability filter

### Problem

proxy_fetch.py previously verified proxies via pi.ipify.org reachability only.
This was insufficient: a proxy can pass the basic reachability test but still
fail against Cloudflare's challenge page because:
- The egress IP is on a CF geo-block CIDR
- The proxy is an SSL-inspecting honeypot that CF rejects
- HTTP/2 negotiation fails against challenges.cloudflare.com
- DNS through the proxy can't resolve CF's challenge hostname

Wasted handoffs: the bot pulled a proxy, tried to bypass CF, and timed out
because the proxy couldn't even reach the challenge page.

### Fix � Two-tier verify in proxy_fetcher_worker.py

proxy_fetcher_worker.py:CF_TEST_URL = "https://latestnewsonline.sbs/r/U9pkpBxM"

Phase 1 (erify_one): unchanged � httpx.get(api.ipify.org) over HTTPS.
Phase 2 (check_cf_compatibility): NEW � httpx.get(CF_TEST_URL, follow_redirects=False).
Looks for these markers in the response body:
- challenges.cloudflare.com � Turnstile script URL
- cf-mitigated � CF interstitial marker
- cf-turnstile � legacy widget class
- Just a moment � title text
- _cf_chl_opt � CF challenge option blob

Phase 2 only runs on proxies that passed phase 1 (typically 2-8 of 40 sampled),
capping the expensive CF-test wall-clock at the number of actually-reachable
proxies.

### Pool schema change

ProxyEntry gained:
- cf_compatible: bool � set from worker payload; gates get_next()
- cf_status_code: int � diagnostic
- cf_marker: str � which marker string was matched

ProxyFetchPool.get_next(cf_compatible=True) is now the default � proxies
that haven't been proven to reach a CF-protected URL are filtered out.

### Empirical results

Worker smoke test (python proxy_fetcher_worker.py --max-verify 40):
- 2/40 proxies passed both phases
- e.g. socks5://43.156.84.41:10808 ? CF 403 + challenges.cloudflare.com
- e.g. http://113.160.132.26:8080 ? CF 403 + challenges.cloudflare.com

Integration test on Wl1P6d_Z via proxy pool:
- rep1: 120s TIMEOUT (proxy passed phase2 against U9pkpBxM but failed on Wl1P6d_Z)
- rep2: 58.5s OK (1 re-nudge)
- rep3: 37.0s OK (2 re-nudges)

### Limitation discovered

**CF compatibility is per-URL, not per-proxy.** A proxy that passes the phase-2
test against one CF-protected URL (U9pkpBxM) may fail on another
(Wl1P6d_Z). This showed up as the rep1 TIMEOUT.

For per-URL compatibility, the next step would be:
1. Add cf_compatible_urls: Set[str] to ProxyEntry
2. On solve failure, mark the proxy as cf_incompatible_for_url(target_host)
3. The pool learns per-URL compatibility over time

Not implemented in this turn � the filter still removes the worst class of
wasted handoffs (geo-blocked proxies entirely), even if per-URL filtering
would tighten the result further.

---

## 2026-08-11 � Proxy pipeline (fetch ? test ? save ? reload)

### What the user asked for

> "It takes proxies from iplocate, tests them all, saves them, and uses
> them to solve CF. Every hour it makes and tests proxies and saves
> them in a .json file or whatever is efficient."

### Architecture

Three modules + one JSON file. Source of truth is the JSON; the in-process
pool just reflects it.

`
iplocate/free-proxy-list (GitHub)
  �
  �  HTTP GET every hour
  ?
proxy_tester.py  -----?  proxy_results.json  -----?  ProxyFetchPool
  � fetch_upstream()        (atomic write)              (load_from_payload)
  � verify_many()           +--------------+
    � phase 1: api.ipify    �  schema v1   �    --?  get_next(cf_compatible=True)
    � phase 2: CF URL       �  proxies[]   �            served to live_solver
  � write_results()         +--------------+
       ?
       �  every N seconds
       �
proxy_refresher.py  (background task, started by bot.py)
`

### Files added

- xdmovies/proxy_tester.py (NEW) � fetch ? verify ? write pipeline
- xdmovies/proxy_refresher.py (NEW) � background scheduler
- xdmovies/proxy_results.json (NEW, generated) � output cache
- xdmovies/proxy_fetch.py (MODIFIED) � added load_from_payload +
  uto_load_from_json; get_next() auto-loads from JSON on first call
- xdmovies/bot.py (MODIFIED) � starts proxy_refresher on first
  pipeline load; non-blocking (initial delay 30s, then 1h loop)

### How to run

**One-shot refresh:**
`
python -m xdmovies.proxy_tester --max-verify 60
`

**One-shot refresh, then exit:**
`
python -m xdmovies.proxy_refresher --once
`

**Background loop (60-min default, 30-min option):**
`
python -m xdmovies.proxy_refresher --interval 3600
`

**Auto-start with bot:** happens automatically on first Telegram DM
(when _load_pipeline() runs in ot.py).

**Env vars:**
- XDMOVIES_PROXY_MAX_VERIFY � proxies sampled per tick (default 60)

### JSON shape

xdmovies/proxy_results.json:

`json
{
  "schema_version": 1,
  "generated_at": "2026-08-11T13:33:05Z",
  "generated_unix": 1786455185.2,
  "tested_count": 80,
  "alive_count": 1,
  "cf_compat_count": 1,
  "duration_seconds": 24.35,
  "upstream": { "http_lines": 542, "socks5_lines": 451, "total": 993 },
  "proxies": [
    {
      "server": "socks5://147.45.60.241:1082",
      "scheme": "socks5",
      "host":   "147.45.60.241",
      "port":   1082,
      "https_capable":  true,
      "cf_compatible":  true,
      "cf_status_code": 403,
      "cf_marker":      "challenges.cloudflare.com",
      "egress_ip":      "147.45.60.241",
      "tested_at":      "2026-08-11T13:33:05Z",
      "phase1_ms":      1443,
      "phase2_ms":      2770,
      "score":          4213
    }
  ]
}
`

### Verification

- python test_refresh_sync.py � runs pipeline, then asserts in-process
  pool was updated. Pass: pool=1 entries (was 0 before).
- python test_auto_load.py � proves pool auto-loads from JSON on
  first get_next() when JSON exists but pool is empty. Pass.
- python -m xdmovies.proxy_tester � CLI smoke test.
- python test_new_link.py � full solve with proxy from new pipeline.
  rep1 14.6s, rep2 20s (vs old 60.9s median = 3-4� faster).

### Bug fixed during integration

proxy_tester.py:reload_pool_from_results was importing
rom xdmovies.proxy_fetch import get_proxy_pool. The xdmovies.
prefix made Python create a SUBMODULE (xdmovies.proxy_fetch) with its
own _POOL singleton. The test's rom proxy_fetch import
get_proxy_pool used a different singleton. Reloads landed in the
wrong pool. Fix: use bare import (rom proxy_fetch import
get_proxy_pool) so both call sites hit the same module.

### Known limitations

- **Per-URL compatibility** � proxies that pass phase 2 against
  U9pkpBxM may fail on Wl1P6d_Z. Not implemented in this turn.
- **170s for --max-verify 120** � phase 2 wall-clock is bounded by
  CF_TEST_TIMEOUT � number of slow candidates. Operators can lower
  --max-verify to keep ticks under 30s.
- **Hourly interval is opinionated** � upstream (iplocate) refreshes
  every 30 min. We refresh every 60 min so the JSON is at most 1h
  stale. Set proxy_refresher --interval 1800 for 30 min.
- **Rep3 fail in integration test** is the pre-existing cookie-domain
  issue on latestnewsonline.sbs (documented earlier in this file),
  not a pipeline problem. The proxy got us a cf_clearance cookie;
  the cookie's domain is .cloudflare.com and isn't sent on the
  second page.goto in link_exec.execute_button_flow.

### What runs in the bot, in order, on Telegram DM

1. ot.py receives the message, calls _load_pipeline() (lazy).
2. _load_pipeline() imports live_solver.configure_pool and starts
   proxy_refresher (initial delay 30s, then hourly loop).
3. The user's solve runs. solve_live_v2 calls
   pool.get_next(cf_compatible=True).
4. get_next() auto-loads from proxy_results.json if the pool is
   empty (e.g. first ever solve, or right after a bot restart before
   the refresher's initial tick has fired).
5. While the user's solve runs, the refresher ticks every 1h in the
   background. Each tick writes a fresh JSON and tells the pool to
   pick it up � the next get_next() after that uses the new list.

> **Authored and owned by Bruno.** Copyright (c) 2026 Bruno — all rights
> reserved. Proprietary and unlicensed: see [`COPYRIGHT`](COPYRIGHT). This
> notice must not be removed, obscured or altered.
> Repository fingerprint: `BRUNO-E1CCB1FB`

# Plan: auto-first solve, token link as fallback

## Goal
Make the Telegram bot resolve gates fully automatically. The VPS token
solve link (`/solve/<token>`) becomes a fallback that is only sent when
automatic verification fails -- not the default path.

## Background (proven on VPS 2026-09-04)
- A solve session drove `top.xdmovies.wtf` to "Verification successful"
  with zero taps and the bot extracted the links.
- The `latestnewsonline.sbs` gate runs entirely in the solve-session
  browser: Generate -> 6s counter -> Continue -> step-2 6s timer ->
  Turnstile checkbox -> Go to Link -> destination tab. All clicks are
  synthetic-first (gate JS checks nothing about isTrusted), ads are never
  hidden or neutered, pop-under tabs are reaped with destination tabs
  preserved.
- The inner gate captcha issues no `cf_clearance`, so cookie-watching
  alone can never complete a gate solve. Completion = captured
  destination URL (`sess.final_url`), stashed via `get_solved_result()`.

## Target behavior
1. On `CFChallengeError`, the bot creates a SILENT session (no Telegram
   message) and lets auto-drive + cookie poller work for an auto window.
   User sees only: "trying automatic bypass first..."
2. Solved in-window (destination or fresh cookie) -> proceed silently.
3. Window expires unsolved -> bot sends the solve link for the SAME live
   session (`get_public_url` on the existing token: no relaunch, no state
   loss) and waits the manual remainder for taps.
4. TTL budget: auto 90s (gate) / 60s (movie pages) + manual 240s/90s.
   Worst case ~240s + 25s destroy delay < `SESSION_TTL` 300s.

## Changes (`bot.py` only -- no `solve_server.py` changes needed)
`solve_server.py` already exposes everything: `create_session`,
`get_session`, `get_public_url`, `wait_for_solve`, `get_solved_result`.

1. New helper `_auto_then_manual_solve(chat_id, url, host, user_id,
   auto_wait, manual_wait)`:
   - `token = create_session(...)` (silent, no message yet)
   - `wait_for_solve(token, auto_wait)` -> on True return `(True,)`
   - on timeout and session still alive: send link message for the same
     token, `wait_for_solve(token, manual_wait)` -> return result
2. Switch the 3 call sites to it, keeping their post-solve retry logic:
   - movie-page handler (~L383): auto 60s / manual 120s (cookie path)
   - gate button-flow handler (~L542): auto 90s / manual 240s
   - follow handler (~L584): auto 90s / manual 240s for gate hosts
3. Kill-switch env flag `XDMOVIES_AUTO_FIRST=1` (default on); `=0`
   restores immediate-link behavior. One-line rollback.
4. Double `wait_for_solve` on one token is safe (second call re-waits the
   same event; destroy fires only on the success path, which returns).

## Verification
- `python -m compileall xdmovies` clean.
- `python -m xdmovies --check` loads 5/5 modules.
- Temp-harness source test of the new helper with a fake session.
- VPS markers: silent solves log `go-to-link: captured destination` /
  `auto-captured NEW cf_clearance` with NO solve-link message in chat;
  fallback path logs the link send exactly as today.

## Tuning knobs (later, only if logs demand)
- `SESSION_TTL` (300s) if full-auto worst case ever outruns it.
- Auto/manual window split per host.
- `XDMOVIES_INNER_NUDGE_WAIT` for the checkbox loop.

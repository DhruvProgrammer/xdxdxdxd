# -*- coding: utf-8 -*-
#
#  ============================================================
#  xdmovies-bot  --  authored and owned by Bruno
#  Copyright (c) 2026 Bruno.  All rights reserved.
#
#  This file is proprietary work. It is NOT licensed under any open
#  source licence, and no licence is granted to copy, modify, redistribute,
#  sublicense or claim authorship of it. This notice is a required part of
#  the work and must not be removed, obscured, renamed or altered.
#
#  Author:  Bruno
#  File:    xdmovies/keep_alive.py
#  Fingerprint: BRUNO-E1CCB1FB-FC6A5E
#  ============================================================

"""Hybrid keep-alive — post-level CF refresh (25m) + VPS token fallback.

CF is on posts (huh.xdmovies.wtf/movies/<slug>), NOT on homepage.
So we must hit a deep link to trigger renewal. Hitting / alone does nothing.

Flow:
  last_solve_ts = cookie_store _updated_at (when cf_clearance saved)
  every CHECK_INTERVAL (30s) check time_since = now - last_solve_ts
  if time_since >= 25m (1500s) -> do silent refresh on a POST url:
     - inject stored cf_clearance via Playwright (headed+ xvfb, stealth)
     - goto post URL, if not is_cf_challenge_page -> success (warm), reset timer via touch
     - if challenge -> light auto-nudge frame.click(body, 18,32) -> poll for cf_clearance -> save -> success
     - if still challenge (interactive/captcha) -> create VPS token link via solve_server and notify owner
  hybrid: movie requests via pipeline already reset timer; background just keeps warm during gaps.

No invisible_playwright, no persistent profile; uses cookie_store + browser_stealth.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import random
from typing import Optional, Tuple

from xdmovies.logger_utils import logger

# CF is on posts, so we need a real movie page to refresh
DEFAULT_MOVIE_TARGETS = [
    "https://huh.xdmovies.wtf/movies/jailer-2160p-1080p-hindi-tamil-download-937020",
    "https://huh.xdmovies.wtf/movies/players-1080p-720p-hindi-download-83382",
    "https://huh.xdmovies.wtf/movies/aasha-2160p-1080p-malayalam-hindi-download-1513475",
]

CHECK_INTERVAL = 30  # seconds to check timer
REFRESH_AFTER = 25 * 60  # 25 minutes -> refresh at 25th min of 30m window
RENEWAL_MARGIN_S = 5 * 60
STATE_PATH_CANDIDATES = [
    os.path.join(os.path.dirname(__file__), "..", "xdmovies_state", "keep_alive_state.json"),
    os.path.join(os.path.dirname(__file__), "xdmovies_state", "keep_alive_state.json"),
    os.path.join(os.getcwd(), "xdmovies_state", "keep_alive_state.json"),
]

def _state_path() -> str:
    for p in STATE_PATH_CANDIDATES:
        d = os.path.dirname(os.path.abspath(p))
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            continue
        return os.path.abspath(p)
    return os.path.abspath(STATE_PATH_CANDIDATES[0])

STATE_PATH = _state_path()

# also keep last successful movie url used for refresh (smart)
REFRESH_TARGETS_PATH_CANDIDATES = [
    os.path.join(os.path.dirname(__file__), "..", "xdmovies_state", "refresh_targets.json"),
    os.path.join(os.getcwd(), "xdmovies_state", "refresh_targets.json"),
]

def _targets_path() -> str:
    for p in REFRESH_TARGETS_PATH_CANDIDATES:
        d = os.path.dirname(os.path.abspath(p))
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            continue
        return os.path.abspath(p)
    return os.path.abspath(REFRESH_TARGETS_PATH_CANDIDATES[0])

TARGETS_PATH = _targets_path()

def _load_state() -> dict:
    if not os.path.isfile(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def _save_state(d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
        os.replace(tmp, STATE_PATH)
    except Exception:
        pass

def _load_targets() -> list:
    if not os.path.isfile(TARGETS_PATH):
        return []
    try:
        with open(TARGETS_PATH, "r", encoding="utf-8") as f:
            arr = json.load(f)
            return arr if isinstance(arr, list) else []
    except Exception:
        return []

def _save_targets(arr: list) -> None:
    try:
        os.makedirs(os.path.dirname(TARGETS_PATH), exist_ok=True)
        tmp = TARGETS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(arr[-20:], f, indent=2)  # keep last 20
        os.replace(tmp, TARGETS_PATH)
    except Exception:
        pass

def remember_movie_url(url: str) -> None:
    """Call from bot after successful movie fetch to keep refresh target smart."""
    if not url or "xdmovies.wtf/movies/" not in url and "xdmovies.wtf/series/" not in url:
        return
    arr = _load_targets()
    if url in arr:
        arr.remove(url)
    arr.append(url)
    _save_targets(arr)

def _pick_refresh_target() -> str:
    arr = _load_targets()
    if arr:
        # pick most recent that looks like movie/series post
        for u in reversed(arr):
            if "/movies/" in u or "/series/" in u:
                return u
    return random.choice(DEFAULT_MOVIE_TARGETS)

def _get_last_solve_ts(domain_key: str = "xdmovies.wtf") -> Optional[float]:
    try:
        from xdmovies.cookie_store import get_entry
        e = get_entry(domain_key)
        if e and e.get("_updated_at"):
            return float(e["_updated_at"])
    except Exception:
        pass
    # fallback to state file
    st = _load_state()
    v = st.get(domain_key, {}).get("last_refresh_ok")
    if v:
        try:
            return float(v)
        except Exception:
            pass
    return None

def _touch_keep_alive(domain_key: str = "xdmovies.wtf") -> None:
    st = _load_state()
    st.setdefault(domain_key, {})["last_refresh_ok"] = time.time()
    st[domain_key]["last_refresh_ok_human"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save_state(st)

def _classify_turnstile_mode(frame_url: str) -> str:
    if not frame_url:
        return "unknown"
    from urllib.parse import urlparse
    p = urlparse(frame_url).path
    parts = [s for s in p.split("/") if s]
    for k in ("non-interactive", "light", "managed", "interactive", "captcha"):
        if k in parts:
            return k
    return "unknown"

async def _classify_and_nudge(page, context) -> bool:
    """Respect mode: non-interactive = passive wait (no click), light/managed = one nudge."""
    frame = None
    try:
        for fr in page.frames:
            u = (fr.url or "").lower()
            if "challenges.cloudflare.com" in u and "turnstile" in u:
                frame = fr
                break
            if "challenge-platform" in u:
                frame = fr
                break
    except Exception:
        frame = None
    if frame is None:
        return False
    try:
        url = (frame.url or "").lower()
        mode = _classify_turnstile_mode(url)
        logger.info(f"[keep_alive] challenge mode={mode} url={url[:90]}")
        if mode == "non-interactive":
            logger.info("[keep_alive] non-interactive (latestnews invisible, no box) — passive wait, no nudge")
            # just wait 8s for fingerprint auto-verify, don't click
            await asyncio.sleep(8)
            # check cookie after wait
            for _ in range(5):
                try:
                    cookies = await context.cookies()
                    for c in cookies:
                        if c.get("name") == "cf_clearance" and len(c.get("value","")) > 20:
                            from xdmovies.cookie_store import set_cookies_for_domain
                            set_cookies_for_domain("xdmovies.wtf", f"cf_clearance={c['value']}")
                            return True
                except Exception:
                    pass
                await asyncio.sleep(1.5)
            # also check page no longer challenge
            try:
                from xdmovies.pipeline import _is_cf_challenge_page
                if not await _is_cf_challenge_page(page):
                    cookies = await context.cookies()
                    for c in cookies:
                        if c.get("name") == "cf_clearance":
                            from xdmovies.cookie_store import set_cookies_for_domain
                            set_cookies_for_domain("xdmovies.wtf", f"cf_clearance={c['value']}")
                            return True
            except Exception:
                pass
            return False
        # light/managed/interactive: one nudge
    except Exception:
        pass
    try:
        await frame.click("body", position={"x": 18, "y": 32}, delay=80, timeout=5000)
        logger.info("[keep_alive] nudged challenge frame at 18,32")
    except Exception as e:
        logger.debug(f"[keep_alive] frame.click failed: {e}")
        return False
    # poll for cookie up to 20s
    for _ in range(10):
        await asyncio.sleep(2)
        try:
            cookies = await context.cookies()
            for c in cookies:
                if c.get("name") == "cf_clearance" and len(c.get("value","")) > 20:
                    # save
                    try:
                        from xdmovies.cookie_store import set_cookies_for_domain, _normalize_domain
                        # domain is xdmovies.wtf
                        set_cookies_for_domain("xdmovies.wtf", f"cf_clearance={c['value']}")
                        logger.info("[keep_alive] auto-captured cf_clearance after nudge")
                        return True
                    except Exception as e:
                        logger.warning(f"[keep_alive] save failed: {e}")
                        return False
        except Exception:
            pass
        # also check if page no longer challenge
        try:
            from xdmovies.pipeline import _is_cf_challenge_page
            if not await _is_cf_challenge_page(page):
                # check cookie again via context
                cookies = await context.cookies()
                for c in cookies:
                    if c.get("name") == "cf_clearance":
                        try:
                            from xdmovies.cookie_store import set_cookies_for_domain
                            set_cookies_for_domain("xdmovies.wtf", f"cf_clearance={c['value']}")
                            return True
                        except Exception:
                            pass
                # page cleared but no cookie yet - wait a bit more
        except Exception:
            pass
    return False

async def _silent_refresh_once(target_url: str) -> Tuple[bool, str]:
    """Try silent refresh on a post URL. Returns (ok, reason)."""
    try:
        from xdmovies.cookie_store import get_cookies_for_url, get_user_agent_for_url
        from xdmovies.pipeline import _is_cf_challenge_page
    except Exception as e:
        return False, f"import_fail:{e}"
    cookies = get_cookies_for_url(target_url)
    if not cookies:
        return False, "no_cookie_stored"
    ua = get_user_agent_for_url(target_url)
    # need at least cf_clearance
    has_cf = any(c.get("name","").lower()=="cf_clearance" for c in cookies)
    if not has_cf:
        return False, "no_cf_clearance"

    try:
        from xdmovies.cloak_stealth import launch_chromium
        browser = await launch_chromium(
            browser_type="chromium", headless=False, useragent=ua,
        )
        ctx = None
        page = None
        try:
            ctx_kwargs = {}
            if ua:
                ctx_kwargs["user_agent"] = ua
            try:
                ctx = await browser.new_context(**ctx_kwargs)
            except Exception:
                ctx = browser  # older cloakbrowser returns context directly
            try:
                await ctx.add_cookies(cookies)
            except Exception:
                try:
                    await ctx.add_cookies([{**c, "domain": c["domain"].lstrip(".")} for c in cookies])
                except Exception:
                    pass
            try:
                page = await ctx.new_page()
            except Exception:
                page = await browser.new_page()
            # block heavy media
            try:
                await page.route(re.compile(r"\.(png|jpe?g|css|woff2?|gif|svg|ico)$"), lambda r: r.abort())
            except Exception:
                pass
            # goto post (CF is on posts, not home)
            await page.goto(target_url, wait_until="domcontentloaded", timeout=35000)
            try:
                await page.wait_for_load_state("load", timeout=4000)
            except Exception:
                pass
            await asyncio.sleep(1.0)
            is_chal = await _is_cf_challenge_page(page)
            if not is_chal:
                # warm success - no challenge, cookie still valid, no need to save
                logger.info(f"[keep_alive] silent hit ok (no challenge) {target_url[:70]}")
                return True, "still_valid"
            # challenge -> try light nudge auto-solve
            logger.info(f"[keep_alive] challenge detected on refresh target, trying nudge")
            ok = await _classify_and_nudge(page, ctx)
            if ok:
                return True, "auto_nudged"
            else:
                return False, "need_human"
        except Exception as e:
            return False, f"goto_fail:{e}"
        finally:
            try:
                if page is not None:
                    await page.close()
            except Exception:
                pass
            try:
                if ctx is not None and ctx is not browser:
                    await ctx.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass
    except Exception as e:
        return False, f"pw_fail:{e}"

_keep_task: Optional[asyncio.Task] = None
_stop_event: Optional[asyncio.Event] = None

async def _loop(bot_app) -> None:
    """Background loop — runs forever until cancelled."""
    logger.info(f"[keep_alive] started (post-level, refresh every {REFRESH_AFTER//60}m, check every {CHECK_INTERVAL}s)")
    # wait a bit after startup so bot is ready
    await asyncio.sleep(20)
    consecutive_need_human = 0
    last_notify_ts = 0.0
    while True:
        try:
            if _stop_event and _stop_event.is_set():
                break
            # check xdmovies.wtf
            last_ts = _get_last_solve_ts("xdmovies.wtf")
            if last_ts is None:
                # no cookie yet, nothing to keep alive
                await asyncio.sleep(CHECK_INTERVAL)
                continue
            age = time.time() - last_ts
            # also consider last successful silent hit
            st = _load_state()
            last_ok = st.get("xdmovies.wtf", {}).get("last_refresh_ok")
            if last_ok:
                # age is min of cookie age and last_ok age
                age = min(age, time.time() - float(last_ok))
            if age < REFRESH_AFTER:
                await asyncio.sleep(CHECK_INTERVAL)
                continue
            # time to refresh
            target = _pick_refresh_target()
            logger.info(f"[keep_alive] 25m gap reached (age {int(age)}s), refreshing post {target[:70]}")
            ok, reason = await _silent_refresh_once(target)
            if ok:
                logger.info(f"[keep_alive] refresh success: {reason}")
                _touch_keep_alive("xdmovies.wtf")
                consecutive_need_human = 0
                # also touch cookie_store updated_at? no, just keep_alive state
                await asyncio.sleep(CHECK_INTERVAL)
                continue
            else:
                logger.warning(f"[keep_alive] refresh failed: {reason}")
                if reason == "need_human":
                    consecutive_need_human += 1
                    # only notify owner if we have failed twice and 30m since last notify
                    now = time.time()
                    if consecutive_need_human >= 2 and (now - last_notify_ts) > 1800:
                        try:
                            from xdmovies.solve_server import create_session, get_public_url
                            from xdmovies.bot_config import load_config
                            cfg = load_config()
                            owner = cfg.owner_id
                            # create token for the target
                            token = await create_session(target, user_id=owner, host="xdmovies.wtf", chat_id=owner)
                            url = get_public_url(token)
                            # send via bot
                            if bot_app and hasattr(bot_app, "bot") and bot_app.bot:
                                await bot_app.bot.send_message(
                                    chat_id=owner,
                                    text=(f"🔄 <b>Keep-alive needs human</b> — <code>xdmovies.wtf</code> challenge escalated.\n"
                                          f"Post: <code>{target}</code>\n"
                                          f"👉 Solve (auto-captures): {url}\n"
                                          f"After solve, loop resumes unlimited."),
                                    parse_mode="HTML"
                                )
                                last_notify_ts = now
                                logger.info(f"[keep_alive] notified owner with VPS link {url[:60]}")
                        except Exception as e:
                            logger.warning(f"[keep_alive] notify failed: {e}")
                    # backoff 10m before retry
                    await asyncio.sleep(600)
                    continue
                elif reason in ("no_cookie_stored", "no_cf_clearance"):
                    # nothing to do, wait for human solve via normal flow
                    await asyncio.sleep(300)
                    continue
                else:
                    await asyncio.sleep(120)
                    continue
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.exception(f"[keep_alive] loop error: {e}")
            await asyncio.sleep(60)

async def start_keep_alive(bot_app) -> None:
    global _keep_task, _stop_event
    if _keep_task and not _keep_task.done():
        return
    _stop_event = asyncio.Event()
    _keep_task = asyncio.create_task(_loop(bot_app))
    logger.info("[keep_alive] task scheduled")

async def stop_keep_alive() -> None:
    global _keep_task, _stop_event
    if _stop_event:
        _stop_event.set()
    if _keep_task:
        _keep_task.cancel()
        try:
            await _keep_task
        except asyncio.CancelledError:
            pass
        _keep_task = None
    logger.info("[keep_alive] stopped")

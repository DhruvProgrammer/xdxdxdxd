"""Hybrid keep-alive — post-level CF refresh (25m) + VPS token fallback.

CF is on posts (top.xdmovies.wtf/movies/<slug>), NOT on homepage.
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
import html
import json
import os
import re
import time
import random
from typing import Optional, Tuple
from urllib.parse import urlsplit

from xdmovies.bot_config import atomic_write_json, ensure_private_directory, ensure_private_file, open_private_text
from xdmovies.browser_runtime import close_browser_objects, is_cf_challenge_page
from xdmovies.logger_utils import logger

# CF is on posts, so we need a real movie page to refresh
DEFAULT_MOVIE_TARGETS = [
    "https://top.xdmovies.wtf/movies/coco-2160p-1080p-hindi-english-download-354912",
    "https://top.xdmovies.wtf/movies/kantara-2160p-1080p-hindi-kannada-download-858485",
    "https://top.xdmovies.wtf/movies/road-movie-1080p-720p-hindi-download-37438",
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
            ensure_private_directory(d)
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
            ensure_private_directory(d)
        except Exception:
            continue
        return os.path.abspath(p)
    return os.path.abspath(REFRESH_TARGETS_PATH_CANDIDATES[0])

TARGETS_PATH = _targets_path()

def _load_state() -> dict:
    if not os.path.isfile(STATE_PATH):
        return {}
    try:
        ensure_private_file(STATE_PATH)
        with open_private_text(STATE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def _save_state(d: dict) -> None:
    try:
        ensure_private_directory(os.path.dirname(STATE_PATH))
        atomic_write_json(STATE_PATH, d)
    except Exception:
        pass

def _load_targets() -> list:
    if not os.path.isfile(TARGETS_PATH):
        return []
    try:
        ensure_private_file(TARGETS_PATH)
        with open_private_text(TARGETS_PATH, "r", encoding="utf-8") as f:
            arr = json.load(f)
            return arr if isinstance(arr, list) else []
    except Exception:
        return []

def _save_targets(arr: list) -> None:
    try:
        ensure_private_directory(os.path.dirname(TARGETS_PATH))
        atomic_write_json(TARGETS_PATH, arr[-20:])
    except Exception:
        pass

def remember_movie_url(url: str) -> None:
    """Call from bot after successful movie fetch to keep refresh target smart."""
    try:
        parsed = urlsplit((url or "").strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        valid_path = parsed.path.startswith(("/movies/", "/series/"))
    except ValueError:
        return
    if parsed.scheme not in ("http", "https") or host != "xdmovies.wtf" and not host.endswith(".xdmovies.wtf") or not valid_path:
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
        if e:
            for field_name in ("_last_used_at", "_updated_at"):
                if e.get(field_name):
                    return float(e[field_name])
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
    try:
        from xdmovies.cookie_store import mark_successful_use
        mark_successful_use(domain_key)
    except Exception:
        pass
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

def _keepalive_domain_ok(cookie_domain: str, target_url: str) -> bool:
    try:
        target = (urlsplit(target_url or "").hostname or "").lower().rstrip(".")
        candidate = (cookie_domain or "").lower().lstrip(".").rstrip(".")
    except ValueError:
        return False
    if not target or not candidate:
        return False
    return candidate == target or candidate.endswith("." + target) or target.endswith("." + candidate)


async def _fresh_keepalive_cookie(context, target_url: str, pre_injected: set[str]) -> str:
    try:
        for cookie in await context.cookies():
            if str(cookie.get("name") or "").lower() != "cf_clearance":
                continue
            value = str(cookie.get("value") or "")
            if not value or len(value) <= 20 or value in pre_injected:
                continue
            if _keepalive_domain_ok(str(cookie.get("domain") or ""), target_url):
                return value
    except Exception:
        pass
    return ""


async def _save_keepalive_cookie(page, context, target_url: str, pre_injected: set[str]) -> bool:
    value = await _fresh_keepalive_cookie(context, target_url, pre_injected)
    if not value:
        return False
    try:
        user_agent = str(await page.evaluate("navigator.userAgent") or "")
        from xdmovies.cookie_store import set_cookies_for_domain
        await asyncio.to_thread(
            set_cookies_for_domain,
            "xdmovies.wtf",
            f"cf_clearance={value}",
            user_agent=user_agent,
        )

        logger.info("[keep_alive] captured a fresh cf_clearance")
        return True
    except Exception as exc:
        logger.warning(f"[keep_alive] cookie save failed: {type(exc).__name__}")
        return False


async def _classify_and_nudge(page, context, target_url: str, pre_injected: set[str]) -> bool:
    frame = None
    try:
        for candidate in page.frames:
            candidate_url = (candidate.url or "").lower()
            if "challenges.cloudflare.com" in candidate_url and "turnstile" in candidate_url:
                frame = candidate
                break
            if "challenge-platform" in candidate_url:
                frame = candidate
                break
    except Exception:
        frame = None
    mode = _classify_turnstile_mode(frame.url if frame is not None else "")
    logger.info(f"[keep_alive] challenge mode={mode}")
    if mode == "non-interactive":
        for _ in range(5):
            if await _save_keepalive_cookie(page, context, target_url, pre_injected):
                return True
            try:
                if not await is_cf_challenge_page(page):
                    return True
            except Exception:
                pass
            await asyncio.sleep(1.5)
        return False
    if frame is None:
        return False
    try:
        await frame.click("body", position={"x": 18, "y": 32}, delay=80, timeout=5000)
        logger.info("[keep_alive] nudged challenge frame at 18,32")
    except Exception as exc:
        logger.debug(f"[keep_alive] frame click failed: {type(exc).__name__}")
        return False
    for _ in range(10):
        await asyncio.sleep(2)
        if await _save_keepalive_cookie(page, context, target_url, pre_injected):
            return True
        try:
            if not await is_cf_challenge_page(page):
                return True
        except Exception:
            pass
    return False

async def _silent_refresh_once(target_url: str) -> Tuple[bool, str]:
    """Try silent refresh on a post URL. Returns (ok, reason)."""
    try:
        from xdmovies.cookie_store import get_cookies_for_url, get_user_agent_for_url
    except Exception as exc:
        return False, f"import_fail:{type(exc).__name__}"
    cookies = get_cookies_for_url(target_url)
    if not cookies:
        return False, "no_cookie_stored"
    pre_injected = {
        str(cookie.get("value") or "")
        for cookie in cookies
        if str(cookie.get("name") or "").lower() == "cf_clearance" and cookie.get("value")
    }
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
            ctx = await browser.new_context(**ctx_kwargs)
            try:
                await ctx.add_cookies(cookies)
            except Exception:
                await ctx.add_cookies([{**c, "domain": c["domain"].lstrip(".")} for c in cookies])
            page = await ctx.new_page()
            try:
                await page.route(re.compile(r"\.(png|jpe?g|css|woff2?|gif|svg|ico)$"), lambda r: r.abort())
            except Exception:
                pass
            response = await page.goto(target_url, wait_until="domcontentloaded", timeout=35000)
            try:
                await page.wait_for_load_state("load", timeout=4000)
            except Exception:
                pass
            await asyncio.sleep(1.0)
            is_chal = await is_cf_challenge_page(page, response=response)
            if not is_chal:
                logger.info("[keep_alive] silent hit succeeded with the stored cookie")
                return True, "still_valid"
            logger.info("[keep_alive] challenge detected on refresh target, trying nudge")
            ok = await _classify_and_nudge(page, ctx, target_url, pre_injected)
            return (True, "auto_nudged") if ok else (False, "need_human")
        except Exception as exc:
            return False, f"goto_fail:{type(exc).__name__}"
        finally:
            await close_browser_objects([page, ctx, browser], timeout=10.0)
    except Exception as exc:
        return False, f"pw_fail:{type(exc).__name__}"

async def _request_human_solve(bot_app, target: str, owner: int) -> bool:
    from xdmovies.solve_server import (
        SESSION_TTL,
        SolveBusyError,
        create_session,
        destroy_session,
        get_public_url,
        wait_for_solve,
    )
    token = None
    solve_task = None
    try:
        token = await create_session(target, user_id=owner, host="xdmovies.wtf", chat_id=owner)
        solve_task = asyncio.create_task(wait_for_solve(token, timeout=float(SESSION_TTL - 5)))
        if not bot_app or not getattr(bot_app, "bot", None):
            raise RuntimeError("telegram bot unavailable")
        public_url = get_public_url(token)
        await bot_app.bot.send_message(
            chat_id=owner,
            text=(
                "🔄 <b>Keep-alive needs human</b> — <code>xdmovies.wtf</code> challenge escalated.\n"
                f"Post: <code>{html.escape(target, quote=True)}</code>\n"
                f"👉 Solve (auto-captures): {html.escape(public_url, quote=True)}\n"
                "After solve, keep-alive resumes."
            ),
            parse_mode="HTML",
        )
        solved = bool(await solve_task)
        if solved:
            logger.info("[keep_alive] owner solve captured successfully")
        return solved
    except SolveBusyError as exc:
        logger.warning(f"[keep_alive] solve request busy: {exc}")
        return False
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(f"[keep_alive] notify failed: {type(exc).__name__}")
        return False
    finally:
        if solve_task is not None and not solve_task.done():
            solve_task.cancel()
        if solve_task is not None:
            await asyncio.gather(solve_task, return_exceptions=True)
        if token is not None and _unobserved_session(token):
            await destroy_session(token)


def _unobserved_session(token: str) -> bool:
    try:
        from xdmovies.solve_server import get_session
        sess = get_session(token)
        return bool(sess and not sess.solved)
    except Exception:
        return False


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
            if age < REFRESH_AFTER:
                await asyncio.sleep(CHECK_INTERVAL)
                continue
            # time to refresh
            target = _pick_refresh_target()
            logger.info(f"[keep_alive] 25m gap reached (age {int(age)}s), refreshing a stored post")
            ok, reason = await _silent_refresh_once(target)
            if ok:
                logger.info(f"[keep_alive] refresh success: {reason}")
                await asyncio.to_thread(_touch_keep_alive, "xdmovies.wtf")
                consecutive_need_human = 0
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
                            from xdmovies.bot_config import load_config
                            owner = load_config().owner_id
                            solved = await _request_human_solve(bot_app, target, owner)
                            if solved:
                                consecutive_need_human = 0
                                last_notify_ts = 0.0
                                await asyncio.sleep(CHECK_INTERVAL)
                                continue
                            last_notify_ts = now
                        except Exception as exc:
                            logger.warning(f"[keep_alive] solve request failed: {type(exc).__name__}")
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
        except Exception as exc:
            logger.exception(f"[keep_alive] loop error: {type(exc).__name__}")
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

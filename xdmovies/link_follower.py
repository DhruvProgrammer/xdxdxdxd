"""Stage 4: Follow a download URL (link.xdmovies.wtf/download/<token>) — MANUAL COOKIE MODE.

Redirect target (latestnewsonline.sbs) needs cf_clearance. We inject the
stored cookie via Playwright; no auto solver.

Returns (final_url, final_status, page_text).
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Tuple

from xdmovies.cookie_store import CFChallengeError, get_cookies_for_url, get_user_agent_for_url, _normalize_domain
from xdmovies.browser_runtime import (
    GATE_HOST,
    close_browser_objects,
    is_cf_challenge_page,
    validate_gate_destination_url,
)


# Audit finding L6: removed the sync `follow_download_link` (httpx
# variant). It was a leftover from the pre-CloakBrowser era and had
# no live callers. The async `follow_download_link_in_browser_async`
# below is the only path used by the bot.


async def follow_download_link_in_browser_async(
    download_url: str,
    session_id: str = "xdmovies_follow_session",
    headless: bool = False,
    timeout_ms: int = 30000,
) -> Tuple[str, int, str]:
    """Browser redirect follower with manual cf_clearance injection.

    Raises CFChallengeError if the destination is still behind Cloudflare and
    no (or stale) cookie is stored — caller should prompt user to paste.
    """
    _ = session_id, headless
    from xdmovies.cloak_stealth import launch_chromium

    # CF binds cf_clearance to IP+UA+domain. The redirect from
    # link.xdmovies.wtf lands on latestnewsonline.sbs — a different
    # eTLD+1 with its own cookie. We must inject cookies for BOTH
    # domains; otherwise the gate is guaranteed to challenge.
    # Audit finding C3.
    cookies = list(get_cookies_for_url(download_url))
    cookies.extend(get_cookies_for_url(f"https://{GATE_HOST}/"))
    ua = get_user_agent_for_url(f"https://{GATE_HOST}/")

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
        page = await ctx.new_page()
        if cookies:
            try:
                await ctx.add_cookies(cookies)
            except Exception:
                await ctx.add_cookies([{**c, "domain": c["domain"].lstrip(".")} for c in cookies])
        await page.set_extra_http_headers({
            "Referer": "https://top.xdmovies.wtf/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        try:
            await page.route(re.compile(r"\.(png|jpe?g|css|woff2?|gif|svg|ico)$"), lambda r: r.abort())
        except Exception:
            pass
        response = await page.goto(download_url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            await page.wait_for_load_state("load", timeout=4000)
        except Exception:
            pass
        await asyncio.sleep(0.6)
        final_url = page.url
        if await is_cf_challenge_page(page, response=response):
            mode = "unknown"
            try:
                for fr in page.frames:
                    frame_url = (fr.url or "").lower()
                    if "challenges.cloudflare.com" in frame_url or "challenge-platform" in frame_url:
                        from urllib.parse import urlparse as _up
                        parts = [s for s in _up(fr.url).path.split("/") if s]
                        for key in ("non-interactive", "light", "managed", "interactive", "captcha"):
                            if key in parts:
                                mode = key
                                break
                        break
            except Exception:
                pass
            if mode == "non-interactive":
                await asyncio.sleep(8)
                final_url = page.url
            if await is_cf_challenge_page(page):
                dest_host = _normalize_domain(final_url)
                from xdmovies.cookie_store import get_entry
                if not get_entry(dest_host):
                    raise CFChallengeError(final_url, reason="no_cookie_for_dest", host=dest_host)
                raise CFChallengeError(final_url, reason="stale_cookie_for_dest", host=dest_host)
            try:
                actual_user_agent = str(await page.evaluate("navigator.userAgent") or ua or "")
                stored_cookies = await page.context.cookies()
                dest_host = _normalize_domain(final_url)
                from xdmovies.cookie_store import get_entry, set_cookies_for_domain
                old_entry = get_entry(dest_host) or {}
                old_cf = old_entry.get("cf_clearance") or ""
                for cookie in stored_cookies:
                    if cookie.get("name") == "cf_clearance" and len(cookie.get("value", "")) > 20:
                        if cookie.get("value") != old_cf:
                            await asyncio.to_thread(
                                set_cookies_for_domain,
                                dest_host,
                                f"cf_clearance={cookie['value']}",
                                user_agent=actual_user_agent,
                            )
                        break
            except CFChallengeError:
                raise
            except Exception:
                pass
        final_url = validate_gate_destination_url(page.url)
        status = response.status if response is not None else 0
        text = await page.content()
        return final_url, status, text
    finally:
        await close_browser_objects([page, ctx, browser], timeout=10.0)

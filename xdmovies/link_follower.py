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
    cookies.extend(get_cookies_for_url("https://latestnewsonline.sbs/"))
    ua = get_user_agent_for_url(download_url)

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
            page = await ctx.new_page()
        except Exception:
            page = await browser.new_page()
        if cookies and ctx is not None:
            try:
                await ctx.add_cookies(cookies)
            except Exception:
                try:
                    await ctx.add_cookies([{**c, "domain": c["domain"].lstrip(".")} for c in cookies])
                except Exception:
                    pass
        await page.set_extra_http_headers({
            "Referer": "https://top.xdmovies.wtf/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        try:
            await page.route(re.compile(r"\.(png|jpe?g|css|woff2?|gif|svg|ico)$"), lambda r: r.abort())
        except Exception:
            pass
        try:
            resp = await page.goto(download_url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception:
            resp = None
        try:
            await page.wait_for_load_state("load", timeout=4000)
        except Exception:
            pass
        await asyncio.sleep(0.6)
        final_url = page.url
        status = resp.status if resp else 0
        try:
            title = (await page.title()) or ""
            html = await page.content()
            # Tighter detection (same as live_solver.py proven on xdmovies):
            # only flag challenge if title is the real "Just a moment" interstitial,
            # or the body has the explicit CF marker. Don't flag on bare
            # "challenge-platform/turnstile" substring — those words appear in
            # post-clearance pages too (CSP meta + script tags).
            is_chal = ("Just a moment" in title or "Checking your browser" in title or "cf-mitigated" in html)
            if is_chal:
                # for latestnews invisible (non-interactive, no box) — try passive auto-wait before asking human
                mode = "unknown"
                try:
                    for fr in page.frames:
                        u = (fr.url or "").lower()
                        if "challenges.cloudflare.com" in u and "turnstile" in u or "challenge-platform" in u:
                            from urllib.parse import urlparse as _up
                            parts = [s for s in _up(fr.url).path.split("/") if s]
                            for k in ("non-interactive", "light", "managed", "interactive", "captcha"):
                                if k in parts:
                                    mode = k
                                    break
                            break
                except Exception:
                    pass
                if mode == "non-interactive":
                    # invisible fingerprint — no click needed, just wait 8s for auto-verify
                    await asyncio.sleep(8)
                    try:
                        title2 = (await page.title()) or ""
                        html2 = await page.content()
                        still = ("Just a moment" in title2 or "Checking your browser" in title2 or "cf-mitigated" in html2)
                        if not still:
                            # auto-solved — only save cookie if it's actually fresh
                            # (don't overwrite good cookie with the same injected one)
                            try:
                                cookies = await page.context.cookies()
                                dest_host = _normalize_domain(final_url)
                                from xdmovies.cookie_store import get_entry
                                old_entry = get_entry(dest_host) or {}
                                old_cf = (old_entry.get("cf_clearance") or "")
                                for c in cookies:
                                    if c.get("name") == "cf_clearance" and len(c.get("value","")) > 20:
                                        new_val = c.get("value")
                                        if new_val != old_cf:
                                            # only save when value actually changed — fresh from CF
                                            from xdmovies.cookie_store import set_cookies_for_domain
                                            set_cookies_for_domain(dest_host, f"cf_clearance={new_val}")
                                        break
                            except Exception:
                                pass
                        else:
                            dest_host = _normalize_domain(final_url)
                            from xdmovies.cookie_store import get_entry
                            if not get_entry(dest_host):
                                raise CFChallengeError(final_url, reason="no_cookie_for_dest", host=dest_host)
                            else:
                                raise CFChallengeError(final_url, reason="stale_cookie_for_dest", host=dest_host)
                    except CFChallengeError:
                        raise
                    except Exception:
                        pass
                else:
                    dest_host = _normalize_domain(final_url)
                    from xdmovies.cookie_store import get_entry
                    if not get_entry(dest_host):
                        raise CFChallengeError(final_url, reason="no_cookie_for_dest", host=dest_host)
                    else:
                        raise CFChallengeError(final_url, reason="stale_cookie_for_dest", host=dest_host)
        except CFChallengeError:
            raise
        except Exception:
            pass
        text = await page.evaluate("document.documentElement.outerHTML") or ""
        return final_url, status, text
    finally:
        try:
            if page is not None:
                await page.close()
        except Exception:
            pass
        try:
            if ctx is not None:
                await ctx.close()
        except Exception:
            pass
        try:
            await browser.close()
        except Exception:
            pass

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
#  File:    xdmovies/link_follower.py
#  Fingerprint: BRUNO-E1CCB1FB-328470
#  ============================================================

"""Stage 4: Follow a download URL (link.xdmovies.wtf/download/<token>) — MANUAL COOKIE MODE.

Redirect target (latestnewsonline.sbs) needs cf_clearance. We inject the
stored cookie via Playwright; no auto solver.

Returns (final_url, final_status, page_text).
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Optional, Tuple

from xdmovies.cookie_store import CFChallengeError, get_cookies_for_url, get_user_agent_for_url, _normalize_domain
from xdmovies.link_exec import _is_navigable_url


# Audit finding L6: removed the sync `follow_download_link` (httpx
# variant). It was a leftover from the pre-CloakBrowser era and had
# no live callers. The async `follow_download_link_in_browser_async`
# below is the only path used by the bot.


class FollowFailedError(Exception):
    """The follow hop did not land on a real page.

    A failed navigation leaves ``page.url`` at a browser-internal
    pseudo-URL (``chrome-error://chromewebdata/`` or ``about:blank``).
    The old code swallowed the goto exception and returned that junk URL
    as a success, so the button flow then raised
    ``Page.goto: net::ERR_ABORTED at chrome-error://chromewebdata/``
    two stages later. This error surfaces the failure at the point it
    actually happened, with a message shaped for the Telegram card.
    """


# Managed/light CF challenges are proven by a non-interactive
# proof-of-work plus TLS/header checks and very often auto-verify within
# a few seconds of the interstitial loading -- no click anywhere. The
# old code only granted this auto-verify wait to `non-interactive`
# mode and raised CFChallengeError INSTANTLY for light/managed, which
# made the first gate card of every job demand a 240s human solve for a
# challenge Cloudflare itself was about to clear. Both knobs are
# env-overridable so the VPS can be tuned without a code edit.
import time as _time
try:
    _MANAGED_GRACE_SECONDS = max(0.0, float(os.getenv("XDMOVIES_CF_MANAGED_GRACE", "12")))
except (TypeError, ValueError):
    _MANAGED_GRACE_SECONDS = 12.0
_MANAGED_GRACE_INTERVAL = 3.0


def _cf_challenge_raise(final_url: str) -> None:
    """Raise the typed CFChallengeError for a still-blocked destination,
    distinguishing no-cookie from stale-cookie (bot.py shows both)."""
    dest_host = _normalize_domain(final_url)
    from xdmovies.cookie_store import get_entry
    if not get_entry(dest_host):
        raise CFChallengeError(final_url, reason="no_cookie_for_dest", host=dest_host)
    raise CFChallengeError(final_url, reason="stale_cookie_for_dest", host=dest_host)


async def _save_fresh_clearance(page, final_url: str) -> None:
    """Save a newly-issued cf_clearance for the destination host, but only
    when the browser value actually differs from the stored one (never
    overwrite a good cookie with the same value we injected).

    Domain-hygiene audit fix: the context deliberately holds cookies for
    BOTH domains (the short-link host and the gate host are injected
    together). Only cookies whose domain actually belongs to the
    destination host may be persisted under it -- the first
    cf_clearance in the jar otherwise poisons the gate key with the
    short-link host's stale cookie and the fresh gate clearance is
    never scanned (cookie save loop had a bare `break` on first hit).
    """
    try:
        cookies = await page.context.cookies()
        dest_host = _normalize_domain(final_url)
        if not dest_host:
            return
        from xdmovies.cookie_store import get_entry, set_cookies_for_domain
        old_cf = ((get_entry(dest_host) or {}).get("cf_clearance") or "")

        def _belongs(cookie_domain: str) -> bool:
            d = (cookie_domain or "").lstrip(".").lower()
            return bool(d) and (
                d == dest_host or d.endswith("." + dest_host)
            )

        for c in cookies:
            if c.get("name") != "cf_clearance" or len(c.get("value", "")) <= 20:
                continue
            cd = (c.get("domain") or "").lstrip(".").lower()
            if cd and not _belongs(cd):
                # A cookie minted for the other injected domain: never
                # store it under the destination key.
                continue
            new_val = c.get("value")
            if new_val != old_cf:
                set_cookies_for_domain(dest_host, f"cf_clearance={new_val}")
            break
    except Exception:
        pass


async def _challenge_still_present(page) -> bool:
    """True while the page still renders a CF interstitial challenge.
    Mirrors the title/cf-mitigated sniff used everywhere in this module."""
    try:
        title = (await page.title()) or ""
        html = await page.content()
        return bool(
            ("Just a moment" in title)
            or ("Checking your browser" in title)
            or ("cf-mitigated" in html)
        )
    except Exception:
        return True


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
            "Referer": "https://huh.xdmovies.wtf/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        try:
            await page.route(re.compile(r"\.(png|jpe?g|css|woff2?|gif|svg|ico)$"), lambda r: r.abort())
        except Exception:
            pass
        goto_exc: Optional[Exception] = None
        try:
            resp = await page.goto(download_url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as exc:
            resp = None
            goto_exc = exc
        try:
            await page.wait_for_load_state("load", timeout=4000)
        except Exception:
            pass
        await asyncio.sleep(0.6)
        final_url = page.url
        # A failed navigation leaves page.url at chrome-error://
        # chromewebdata/ (or about:blank). Raise here -- at the point of
        # failure -- instead of letting the junk URL flow into
        # execute_button_flow, where it caused net::ERR_ABORTED.
        # If goto raised but the page recovered to a real URL, we still
        # proceed (strictly more permissive than the old behavior).
        if not _is_navigable_url(final_url):
            if goto_exc is not None:
                print(f"[link_follower] goto failed: {goto_exc!r}")
            raise FollowFailedError(
                f"did not reach a real page for {download_url[:80]} "
                "-- link may be expired"
            )
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
                    if not await _challenge_still_present(page):
                        await _save_fresh_clearance(page, final_url)
                    else:
                        _cf_challenge_raise(final_url)
                elif mode in ("light", "managed"):
                    # Managed/light challenges auto-verify passive checks
                    # within seconds -- grant them the same grace wait the
                    # non-interactive path gets instead of demanding a
                    # human solve instantly (the first-card stall bug).
                    _cleared = False
                    _gdeadline = _time.monotonic() + _MANAGED_GRACE_SECONDS
                    while _time.monotonic() < _gdeadline:
                        await asyncio.sleep(_MANAGED_GRACE_INTERVAL)
                        if not await _challenge_still_present(page):
                            _cleared = True
                            break
                    if _cleared:
                        await _save_fresh_clearance(page, final_url)
                    else:
                        _cf_challenge_raise(final_url)
                else:
                    _cf_challenge_raise(final_url)
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

"""Eternal bypass operator helper.

Single-shot manual-solve script for operators who want to capture a
fresh cf_clearance cookie on the VPS without going through the
Telegram bot's solve-server flow.

Workflow:
  1. Open a browser via CloakBrowser with the same patched Chromium
     the bot uses. If CloakBrowser is not installed, raise loudly
     (operator policy: crash, no silent fallback). The browser runs
     with the VPS IP directly — Cloudflare's datacenter-ASN
     fingerprinting will reduce solve success rate, so this is
     intended for one-off / test captures only.
  2. If no cookie exists, wait for the operator to click the Turnstile
     checkbox in the VNC-attached browser.
  4. Save the cookie to xdmovies_state/cf_cookies.json via the
     shared cookie_store.
  5. Enter an eternal loop: visit the target shortly before the
     cookie expires, so CF auto-renews it without re-challenging.

This is a thin wrapper around the bot's existing cookie_store + a
CloakBrowser launch. It does NOT replace the Telegram bot — both
can coexist; the Telegram solve_server remains the primary path for
multi-user solving.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from typing import Optional

from xdmovies.bot_config import ensure_private_directory
from xdmovies.browser_runtime import close_browser_objects
from xdmovies.logger_utils import logger


TARGET_URL = "https://top.xdmovies.wtf"
COOKIE_NAME = "cf_clearance"
RENEWAL_MARGIN = 300   # 5 min before expiry: renew
CHECK_INTERVAL = 30    # how often to check the cookie
DEFAULT_PROFILE_DIR = "/var/lib/xdmovies/eternal_profile"


async def _capture_or_renew_once(target_url: str, profile_dir: str) -> None:
    from xdmovies.cloak_stealth import launch_persistent_chromium
    from xdmovies.cookie_store import get_cookies_for_url, _normalize_domain, get_entry, get_user_agent_for_url, set_cookies_for_domain
    ua = get_user_agent_for_url(target_url)
    ctx = await launch_persistent_chromium(
        user_data_dir=profile_dir,
        headless=False,
        useragent=ua,
    )
    page = None
    try:
        stored = get_cookies_for_url(target_url)
        if stored:
            try:
                await ctx.add_cookies(stored)
            except Exception:
                await ctx.add_cookies([
                    {**cookie, "domain": str(cookie.get("domain") or "").lstrip(".")}
                    for cookie in stored
                ])
        page = await ctx.new_page()
        await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(2.0)
        cookies = await ctx.cookies()
        for c in cookies:
            if c.get("name") == COOKIE_NAME and len(c.get("value", "")) > 20:
                dest_host = _normalize_domain(page.url)
                new_val = c.get("value")
                old_entry = get_entry(dest_host) or {}
                old_cf = old_entry.get("cf_clearance") or ""
                if new_val != old_cf:
                    await asyncio.to_thread(
                        set_cookies_for_domain,
                        dest_host,
                        f"{COOKIE_NAME}={new_val}",
                    )
                    logger.info(f"eternal: saved fresh {COOKIE_NAME} for {dest_host}")
                return
        print()
        print("=" * 70)
        print(f"  No {COOKIE_NAME} cookie found.")
        print("  Open the VNC desktop at http://127.0.0.1:6080/vnc.html through an SSH tunnel")
        print("  and click the Turnstile checkbox in the browser window.")
        print("=" * 70)
        print()
        while True:
            await asyncio.sleep(2.0)
            cookies = await ctx.cookies()
            for c in cookies:
                if c.get("name") == COOKIE_NAME and len(c.get("value", "")) > 20:
                    dest_host = _normalize_domain(page.url)
                    await asyncio.to_thread(
                        set_cookies_for_domain,
                        dest_host,
                        f"{COOKIE_NAME}={c['value']}",
                    )
                    logger.info(f"eternal: saved fresh {COOKIE_NAME} for {dest_host}")
                    return
    finally:
        await close_browser_objects([page, ctx], timeout=10.0)


async def main(target: str, profile_dir: Optional[str]) -> None:
    profile = profile_dir or DEFAULT_PROFILE_DIR
    ensure_private_directory(profile)
    logger.info(f"eternal_bypass: target={target} profile={profile}")
    while True:
        try:
            await _capture_or_renew_once(target, profile)
        except Exception as e:
            logger.warning(f"eternal: capture/renew failed: {e!r} — retrying in 30s")
            await asyncio.sleep(30.0)
            continue
        # Wait until close to expiry.
        from xdmovies.cookie_store import get_entry
        e = get_entry("xdmovies.wtf") or {}
        exp = e.get("_expires_at") or 0
        if exp and exp > time.time():
            sleep_s = max(60, exp - time.time() - RENEWAL_MARGIN)
            logger.info(f"eternal: cookie valid for {int(exp - time.time())}s; sleeping {int(sleep_s)}s until next renew")
            await asyncio.sleep(sleep_s)
        else:
            await asyncio.sleep(CHECK_INTERVAL)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Eternal cf_clearance renewer")
    p.add_argument("--target", default=TARGET_URL, help="URL to keep alive")
    p.add_argument("--profile", default=DEFAULT_PROFILE_DIR, help="persistent profile dir")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    try:
        asyncio.run(main(args.target, args.profile))
    except KeyboardInterrupt:
        sys.exit(0)

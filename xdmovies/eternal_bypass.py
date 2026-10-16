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
#  File:    xdmovies/eternal_bypass.py
#  Fingerprint: BRUNO-E1CCB1FB-B1EDF9
#  ============================================================

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
import os
import sys
import time
from typing import Optional

from xdmovies.logger_utils import logger


TARGET_URL = "https://huh.xdmovies.wtf"
COOKIE_NAME = "cf_clearance"
RENEWAL_MARGIN = 300   # 5 min before expiry: renew
CHECK_INTERVAL = 30    # how often to check the cookie
DEFAULT_PROFILE_DIR = "/var/lib/xdmovies/eternal_profile"


async def _capture_or_renew_once(target_url: str) -> None:
    from xdmovies.cloak_stealth import launch_chromium
    from xdmovies.cookie_store import get_cookies_for_url, _normalize_domain, get_entry, set_cookies_for_domain
    browser = await launch_chromium(
        browser_type="chromium", headless=False, useragent=None,
    )
    page = await browser.new_page()
    try:
        # Try the target. If the cookie is already in the store, it gets
        # injected by the launch path; otherwise we'll see a challenge.
        await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
        # Wait a bit for any post-load CF checks to settle.
        await asyncio.sleep(2.0)
        cookies = await page.context.cookies()
        for c in cookies:
            if c.get("name") == COOKIE_NAME and len(c.get("value", "")) > 20:
                dest_host = _normalize_domain(page.url)
                new_val = c.get("value")
                old_entry = get_entry(dest_host) or {}
                old_cf = old_entry.get("cf_clearance") or ""
                if new_val != old_cf:
                    set_cookies_for_domain(dest_host, f"{COOKIE_NAME}={new_val}")
                    logger.info(f"eternal: saved fresh {COOKIE_NAME} for {dest_host}")
                return
        # No cookie yet — wait for operator to click.
        print()
        print("=" * 70)
        print(f"  No {COOKIE_NAME} cookie found.")
        print(f"  Open the VNC desktop (http://{os.environ.get('VPS_IP', '127.0.0.1')}:6080/vnc.html)")
        print("  and click the Turnstile checkbox in the browser window.")
        print("=" * 70)
        print()
        while True:
            await asyncio.sleep(2.0)
            cookies = await page.context.cookies()
            for c in cookies:
                if c.get("name") == COOKIE_NAME and len(c.get("value", "")) > 20:
                    dest_host = _normalize_domain(page.url)
                    set_cookies_for_domain(dest_host, f"{COOKIE_NAME}={c['value']}")
                    logger.info(f"eternal: saved fresh {COOKIE_NAME} for {dest_host}")
                    return
    finally:
        try:
            await page.close()
        except Exception:
            pass
        try:
            await browser.close()
        except Exception:
            pass


async def main(target: str, profile_dir: Optional[str]) -> None:
    logger.info(f"eternal_bypass: target={target}")
    if profile_dir:
        os.makedirs(profile_dir, exist_ok=True)
    while True:
        try:
            await _capture_or_renew_once(target)
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
    p.add_argument("--profile", default=None, help="persistent profile dir (optional)")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    try:
        asyncio.run(main(args.target, args.profile))
    except KeyboardInterrupt:
        sys.exit(0)

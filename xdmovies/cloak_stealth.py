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
#  File:    xdmovies/cloak_stealth.py
#  Fingerprint: BRUNO-E1CCB1FB-0BA2C6
#  ============================================================

"""CloakBrowser wrapper — async-first drop-in for our 5 Playwright launch sites.

CloakBrowser is a Chromium build with C++ source-level fingerprint patches
(https://github.com/CloakHQ/CloakBrowser, 30.8k stars). API surface:

  - launch()                 sync, returns Playwright Browser
  - launch_async()           async, returns Playwright Browser
  - launch_context_async()   async, returns Playwright BrowserContext
  - launch_persistent_context_async()  async, returns persistent BrowserContext

All 5 of our launch sites (pipeline, link_follower, link_exec × 2,
solve_server, keep_alive) are async functions that historically did:

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**kwargs)
        ctx = await browser.new_context(**ctx_kwargs)
        page = await ctx.new_page()
        ...
        await page.close(); await ctx.close(); await browser.close()

This module exposes `launch_chromium(...)` which returns a `CloakBrowser`
adapter with the same `new_context()`, `new_page()`, `close()` shape, so
the call sites stay nearly identical (just swap the launcher, drop
`async with async_playwright() as pw:`). 

Crash-loudly policy: if `import cloakbrowser` fails, the bot's systemd
unit restart-loops visibly in `journalctl`. There is no silent fallback
to vanilla Playwright.

Important: CloakBrowser's binary is downloaded once on first run (~200MB)
to `~/.cloakbrowser/`. Deploy.sh sets `CLOAKBROWSER_CACHE_DIR=/opt/cloakbrowser`
to keep the binary in a stable location across systemd unit restarts.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from xdmovies.logger_utils import logger


# ---------------------------------------------------------------------------
# Import cloakbrowser — crash loudly if missing
# ---------------------------------------------------------------------------

try:
    import cloakbrowser  # type: ignore
    _CLOAK_OK = True
    _CLOAK_VERSION = getattr(cloakbrowser, "__version__", "unknown")
    logger.info(f"cloak_stealth: cloakbrowser v{_CLOAK_VERSION} imported OK")
except Exception as exc:  # pragma: no cover - import-time guard
    _CLOAK_OK = False
    _CLOAK_VERSION = None
    _IMPORT_ERROR: Optional[BaseException] = exc
    logger.error(
        f"cloak_stealth: FAILED to import cloakbrowser ({exc!r}). "
        "Install with `pip install cloakbrowser>=0.5.0` and let it "
        "download the ~200MB Chromium binary. Aborting (crash-loudly policy)."
    )
    raise


# ---------------------------------------------------------------------------
# Argument helpers
# ---------------------------------------------------------------------------

# Standard Chromium args we always pass. These are belt-and-braces on top
# of CloakBrowser's own stealth args (C++ patches). Mostly defensive — they
# make Playwright/Chromium behave like a regular Chrome on a desktop, which
# is what CF and most anti-bot systems expect.
def _std_args(useragent: Optional[str]) -> list[str]:
    args: list[str] = [
        "--disable-blink-features=AutomationControlled",
        "--disable-features=IsolateOrigins,site-per-process",
        "--disable-infobars",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        # Force public DNS (avoids Windows DNS filtering for CF hosts).
        "--dns-server=1.1.1.1,8.8.8.8",
    ]
    if useragent:
        args.append(f"--user-agent={useragent}")
    return args


# ---------------------------------------------------------------------------
# Proxy translation
# ---------------------------------------------------------------------------

def _translate_proxy(proxy: Optional[Dict[str, Any]]) -> Optional[str]:
    """Translate the internal `{"server": "...", "username": ..., "password": ...}`
    shape into a URL string CloakBrowser accepts. CloakBrowser takes a plain
    proxy URL (`http://...` or `socks5://...`), and Chromium's --proxy-server
    flag accepts inline `user:pass@host:port` for basic auth. Both SOCKS5
    (with optional DNS isolation) and HTTP/HTTPS proxies are supported.
    """
    if not proxy or not isinstance(proxy, dict):
        return None
    server = proxy.get("server")
    if not server:
        return None
    user = proxy.get("username")
    pwd = proxy.get("password")
    if user and pwd and "@" not in server:
        scheme, rest = server.split("://", 1)
        # url-encode credentials (CloakBrowser uses urllib.parse.urlparse
        # internally, but we encode to be safe with special chars).
        from urllib.parse import quote
        return f"{scheme}://{quote(user, safe='')}:{quote(pwd, safe='')}@{rest}"
    return server


# ---------------------------------------------------------------------------
# Adapter so call sites can keep the `browser.new_context()` shape
# ---------------------------------------------------------------------------

class _CloakBrowserAdapter:
    """Wraps a CloakBrowser Browser and re-exposes new_context()/close()
    using CloakBrowser's launch_context_async for context creation.

    CloakBrowser's Browser already has a default context, so we use the
    simpler flow:
      - browser.new_page()  → returns a Page from the default context
      - browser.new_context(...) → calls launch_context_async() internally
                                  (creates a new browser+context, then closes
                                   the original browser to avoid leaks)
      - browser.close()    → closes the underlying browser

    Most of our call sites use new_page() directly on the default context,
    which is exactly what CloakBrowser is designed for. The few that need
    a custom context (UA, viewport) use new_context().
    """

    def __init__(self, browser: Any, proxy_url: Optional[str], headless: bool, useragent: Optional[str], humanize: bool, geoip: bool):
        self._browser = browser
        self._proxy_url = proxy_url
        self._headless = headless
        self._useragent = useragent
        self._humanize = humanize
        self._geoip = geoip
        self._extra_contexts: list = []  # additional contexts we created

    async def new_context(self, **kwargs) -> Any:
        """Create a new context. CloakBrowser's launch_context_async creates
        a brand-new browser+context. We close our original browser first
        to avoid two Chromium processes running simultaneously.
        """
        # Pull kwargs the wrapper already knows about; pass the rest through.
        from cloakbrowser import launch_context_async
        # Strip non-Playwright kwargs that CloakBrowser doesn't accept.
        # IMPORTANT: pop `viewport` exactly once and pass it explicitly.
        # The previous code did `kwargs.pop('viewport', None)` then
        # `kwargs.setdefault('viewport', viewport)`, which produced
        # `TypeError: got multiple values for keyword argument 'viewport'`
        # at launch_context_async time (audit finding C1).
        user_agent = kwargs.pop("user_agent", None) or self._useragent
        viewport = kwargs.pop("viewport", None)
        # Build args list — merge our std args with any caller's.
        std = _std_args(user_agent)
        extra_args = kwargs.pop("args", None) or []
        # Use a fresh browser for the context (CloakBrowser's design).
        # Close the current browser first to avoid resource bloat.
        try:
            await self._browser.close()
        except Exception:
            pass
        new_browser = await launch_context_async(
            headless=self._headless,
            proxy=self._proxy_url,
            args=std + extra_args,
            user_agent=user_agent,
            viewport=viewport,
            humanize=self._humanize,
            geoip=self._geoip,
            **kwargs,
        )
        # Replace the inner browser so subsequent new_page() / close() work.
        self._browser = new_browser
        return new_browser

    async def new_page(self, **kwargs) -> Any:
        """Open a new page in the default context. Most call sites use this.
        CloakBrowser's Browser has a default context with new_page() built in.
        """
        # Pass through any kwargs (e.g. viewport) to the underlying new_page.
        return await self._browser.new_page(**kwargs)

    async def close(self) -> None:
        try:
            await self._browser.close()
        except Exception as e:
            logger.debug(f"cloak_stealth: browser.close() raised: {e!r}")

    # Some callers may iterate contexts (rare). Forward that too.
    @property
    def contexts(self):
        return getattr(self._browser, "contexts", [])

    @property
    def pages(self):
        return getattr(self._browser, "pages", [])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def cloak_available() -> bool:
    return _CLOAK_OK


def cloak_version() -> Optional[str]:
    return _CLOAK_VERSION


async def launch_chromium(
    *,
    browser_type: str = "chromium",
    headless: bool = False,
    useragent: Optional[str] = None,
    proxy: Optional[Dict[str, Any]] = None,
    humanize: bool = True,
    geoip: bool = False,
) -> Any:
    """Async-first replacement for `pw.chromium.launch(**kwargs)`.

    Returns an async browser with `new_context()`, `new_page()`, and
    `close()` methods matching the Playwright Browser shape.

    Args:
        browser_type: kept for API parity. Only "chromium" is supported.
        headless: caller hint. Default False (CF needs headed).
        useragent: optional UA string. Passed to CloakBrowser.
        proxy: optional Playwright-shaped dict
            (`{"server": "socks5://host:port", "username": ..., "password": ...}`)
            When set, CloakBrowser routes all Chromium traffic through
            it via Chromium's --proxy-server flag. Default is direct
            (no proxy); the bot connects from the VPS IP directly.
            Pass `proxy={"server": None}` to force a no-proxy launch
            even when a proxy is otherwise configured.
        humanize: enable CloakBrowser's natural mouse/typing simulation.
            Default True. Disable only for headless server-side tasks
            where latency matters more than fingerprint.
        geoip: auto-detect timezone + locale from the proxy's exit IP
            (requires `pip install 'cloakbrowser[geoip]'`). Free tier will
            be limited to random tz/locale. Default False.

    Returns:
        A _CloakBrowserAdapter (shape-compatible with Playwright Browser).

    Raises:
        RuntimeError: if cloakbrowser is unavailable.
    """
    if not _CLOAK_OK:
        raise RuntimeError(
            "cloakbrowser is not importable. Run `pip install cloakbrowser>=0.5.0` "
            "and let it download the patched Chromium binary. Refusing to "
            "fall back to vanilla Playwright (operator policy: crash loudly)."
        )

    if browser_type != "chromium":
        logger.warning(
            f"cloak_stealth: browser_type={browser_type!r} requested; "
            "CloakBrowser only ships Chromium. Using chromium."
        )

    # Direct IP launch (no VPN). The bot connects from the VPS IP
    # directly; Cloudflare's datacenter-ASN fingerprinting will reduce
    # success rate on /solve flows. Trade simplicity for reliability.

    proxy_url = _translate_proxy(proxy)
    args = _std_args(useragent)

    logger.info(
        f"cloak_stealth: launch (headless={headless}, ua={'set' if useragent else 'default'}, "
        f"proxy={proxy_url or 'direct'}, humanize={humanize}, geoip={geoip})"
    )

    # CloakBrowser's launch_async is the async version that returns a Browser.
    # We use this even for the default-context flow (no new_context) so the
    # returned object has new_page() built in.
    #
    # Note: do NOT pass `user_agent` here. cloakbrowser 0.5.8's
    # `launch_async` accepts the same kwargs as Playwright's
    # `BrowserType.launch` (headless, proxy, args, channel, etc.) but
    # not `user_agent` — that's a BrowserContext kwarg. The adapter's
    # `new_context(**kwargs)` already routes `user_agent=...` to
    # `cloakbrowser.launch_context_async`, which is the correct path.
    # Setting a default UA via `--user-agent=...` Chromium arg is the
    # fallback if callers don't pass one at the context level.
    launch_kwargs = dict(
        headless=headless,
        proxy=proxy_url,
        args=args,
        humanize=humanize,
        geoip=geoip,
    )
    if useragent:
        # Inject the UA as a Chromium arg. CloakBrowser passes `args`
        # through to Chromium's command line, where --user-agent=...
        # sets the default UA for all contexts that don't override it.
        launch_kwargs["args"] = list(args) + [f"--user-agent={useragent}"]
    browser = await cloakbrowser.launch_async(**launch_kwargs)

    # One-shot exit-IP check: prove in the logs that Chromium is actually
    # going through the proxy. Best-effort, never blocks launch.
    if proxy_url:
        try:
            import aiohttp  # already in requirements for solve_server
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get("https://api.ipify.org", proxy=proxy_url) as r:
                    exit_ip = (await r.text()).strip()
                    logger.info(f"cloak_stealth: Chromium exit IP = {exit_ip} (via {proxy_url})")
        except Exception as e:
            logger.debug(f"cloak_stealth: exit-IP probe failed ({e!r})")
    return _CloakBrowserAdapter(
        browser=browser,
        proxy_url=proxy_url,
        headless=headless,
        useragent=useragent,
        humanize=humanize,
        geoip=geoip,
    )


async def launch_persistent_chromium(
    *,
    user_data_dir: str,
    headless: bool = False,
    useragent: Optional[str] = None,
    proxy: Optional[Dict[str, Any]] = None,
    humanize: bool = True,
    geoip: bool = False,
) -> Any:
    """Persistent-context variant. Returns a BrowserContext (not a Browser).

    Cookies/localStorage/cache persist across runs in `user_data_dir`. Use
    this for solve_server / keep_alive / eternal_bypass where we want
    warm sessions to bypass incognito detection.

    Returns:
        CloakBrowser BrowserContext (async). Has new_page() and close().
    """
    if not _CLOAK_OK:
        raise RuntimeError("cloakbrowser not importable")

    proxy_url = _translate_proxy(proxy)
    args = _std_args(useragent)
    user_data_dir = os.path.expanduser(user_data_dir)
    os.makedirs(user_data_dir, exist_ok=True)

    logger.info(
        f"cloak_stealth: launch_persistent (user_data_dir={user_data_dir}, "
        f"headless={headless}, ua={'set' if useragent else 'default'}, "
        f"proxy={proxy_url or 'direct'}, humanize={humanize}, geoip={geoip})"
    )

    from cloakbrowser import launch_persistent_context_async
    ctx = await launch_persistent_context_async(
        user_data_dir=user_data_dir,
        headless=headless,
        proxy=proxy_url,
        user_agent=useragent,
        args=args,
        humanize=humanize,
        geoip=geoip,
    )
    return ctx


__all__ = [
    "launch_chromium",
    "launch_persistent_chromium",
    "cloak_available",
    "cloak_version",
]

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

import asyncio
import os
from typing import Any, Dict, Optional

from xdmovies.bot_config import ensure_private_directory
from xdmovies.browser_runtime import close_browser_objects
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
        "--disable-infobars",
        "--disable-dev-shm-usage",
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


def _configured_max_sessions() -> int:
    try:
        return max(1, int(os.getenv("XDMOVIES_MAX_SESSIONS", "8").strip() or "8"))
    except ValueError:
        return 8


_MAX_SESSIONS = _configured_max_sessions()
_SESSION_SEMAPHORE = asyncio.Semaphore(_MAX_SESSIONS)
_ACTIVE_SESSIONS = 0


def configure_session_limit(max_sessions: int) -> None:
    global _MAX_SESSIONS, _SESSION_SEMAPHORE, _ACTIVE_SESSIONS
    value = max(1, int(max_sessions))
    if value == _MAX_SESSIONS:
        return
    if _ACTIVE_SESSIONS:
        raise RuntimeError("cannot change XDMOVIES_MAX_SESSIONS while browser sessions are active")
    _MAX_SESSIONS = value
    _SESSION_SEMAPHORE = asyncio.Semaphore(value)


async def _acquire_session_slot() -> None:
    global _ACTIVE_SESSIONS
    await _SESSION_SEMAPHORE.acquire()
    _ACTIVE_SESSIONS += 1


def _release_session_slot() -> None:
    global _ACTIVE_SESSIONS
    if _ACTIVE_SESSIONS <= 0:
        return
    _ACTIVE_SESSIONS -= 1
    _SESSION_SEMAPHORE.release()


class _CloakBrowserAdapter:
    def __init__(self, launch_kwargs: Dict[str, Any], proxy_url: Optional[str], headless: bool, useragent: Optional[str], humanize: bool, geoip: bool):
        self._browser = None
        self._launch_kwargs = launch_kwargs
        self._proxy_url = proxy_url
        self._headless = headless
        self._useragent = useragent
        self._humanize = humanize
        self._geoip = geoip
        self._released = False
        self._proxy_checked = False

    async def _probe_proxy(self) -> None:
        if self._proxy_checked:
            return
        self._proxy_checked = True
        if not self._proxy_url:
            return
        try:
            import aiohttp
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get("https://api.ipify.org", proxy=self._proxy_url) as response:
                    exit_ip = (await response.text()).strip()
                    logger.info(f"cloak_stealth: Chromium exit IP = {exit_ip} (via {self._proxy_url})")
        except Exception as exc:
            logger.debug(f"cloak_stealth: exit-IP probe failed ({exc!r})")

    async def _ensure_browser(self) -> Any:
        if self._browser is not None:
            return self._browser
        try:
            self._browser = await cloakbrowser.launch_async(**self._launch_kwargs)
            await self._probe_proxy()
            return self._browser
        except BaseException:
            self._browser = None
            if not self._released:
                self._released = True
                _release_session_slot()
            raise

    async def new_context(self, **kwargs) -> Any:
        from cloakbrowser import launch_context_async
        user_agent = kwargs.pop("user_agent", None) or self._useragent
        viewport = kwargs.pop("viewport", None)
        extra_args = kwargs.pop("args", None) or []
        try:
            if self._browser is not None:
                await close_browser_objects([self._browser], timeout=10.0)
                self._browser = None
            self._browser = await launch_context_async(
                headless=self._headless,
                proxy=self._proxy_url,
                args=_std_args(user_agent) + extra_args,
                user_agent=user_agent,
                viewport=viewport,
                humanize=self._humanize,
                geoip=self._geoip,
                **kwargs,
            )
            await self._probe_proxy()
            return self._browser
        except BaseException:
            self._browser = None
            if not self._released:
                self._released = True
                _release_session_slot()
            raise

    async def new_page(self, **kwargs) -> Any:
        browser = await self._ensure_browser()
        try:
            return await browser.new_page(**kwargs)
        except BaseException:
            self._browser = None
            if not self._released:
                self._released = True
                _release_session_slot()
            raise

    async def close(self) -> None:
        try:
            if self._browser is not None:
                await close_browser_objects([self._browser], timeout=10.0)
        finally:
            self._browser = None
            if not self._released:
                self._released = True
                _release_session_slot()

    @property
    def contexts(self):
        return getattr(self._browser, "contexts", [])

    @property
    def pages(self):
        return getattr(self._browser, "pages", [])


class _SessionContextProxy:
    def __init__(self, context: Any):
        self._context = context
        self._released = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._context, name)

    async def close(self) -> None:
        try:
            await close_browser_objects([self._context], timeout=10.0)
        finally:
            self._context = None
            if not self._released:
                self._released = True
                _release_session_slot()


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

    proxy_url = _translate_proxy(proxy)
    args = _std_args(useragent)
    if useragent:
        args = list(args) + [f"--user-agent={useragent}"]
    launch_kwargs = dict(
        headless=headless,
        proxy=proxy_url,
        args=args,
        humanize=humanize,
        geoip=geoip,
    )

    await _acquire_session_slot()
    logger.info(
        f"cloak_stealth: launch (headless={headless}, ua={'set' if useragent else 'default'}, "
        f"proxy={proxy_url or 'direct'}, humanize={humanize}, geoip={geoip}, "
        f"active={_ACTIVE_SESSIONS}/{_MAX_SESSIONS})"
    )
    return _CloakBrowserAdapter(
        launch_kwargs=launch_kwargs,
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
    ensure_private_directory(user_data_dir)

    logger.info(
        f"cloak_stealth: launch_persistent (user_data_dir={user_data_dir}, "
        f"headless={headless}, ua={'set' if useragent else 'default'}, "
        f"proxy={proxy_url or 'direct'}, humanize={humanize}, geoip={geoip})"
    )

    from cloakbrowser import launch_persistent_context_async
    await _acquire_session_slot()
    try:
        ctx = await launch_persistent_context_async(
            user_data_dir=user_data_dir,
            headless=headless,
            proxy=proxy_url,
            user_agent=useragent,
            args=args,
            humanize=humanize,
            geoip=geoip,
        )
        return _SessionContextProxy(ctx)
    except BaseException:
        _release_session_slot()
        raise


__all__ = [
    "launch_chromium",
    "launch_persistent_chromium",
    "cloak_available",
    "cloak_version",
    "configure_session_limit",
]

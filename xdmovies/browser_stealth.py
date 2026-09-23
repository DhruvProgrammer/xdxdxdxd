"""playwright + stealth-plugin wrappers for Chromium-family browsers.

The `playwright_stealth` package exposes two things we use:

  * `Stealth()` - a playwright-extra-compatible plugin applied at context creation
  * `stealth_async(page)` / `stealth_sync(page)` - ad-hoc application on a page

We use the per-context `Stealth` plugin so it only attaches when we open a
new BrowserContext, keeping it out of the way for camoufox (FireFox-derived)
which is handled in a separate code path.
"""
from __future__ import annotations

from typing import Optional, Sequence

from xdmovies.logger_utils import logger


try:
    from playwright_stealth import Stealth  # type: ignore
    _STEALTH_OK = True
    _STEALTH_ERROR: Optional[BaseException] = None
except Exception as exc:  # pragma: no cover - import-time guard
    Stealth = None  # type: ignore[assignment]
    _STEALTH_OK = False
    _STEALTH_ERROR = exc


def stealth_available() -> bool:
    return _STEALTH_OK


def stealth_import_error() -> Optional[BaseException]:
    return _STEALTH_ERROR


def stealth_plugin():  # pragma: no cover - simple factory
    """Return a stealth plugin instance, or None if unavailable."""
    if not _STEALTH_OK:
        return None
    return Stealth()


def _chromium_args(useragent: Optional[str]) -> Sequence[str]:
    args: list[str] = [
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        # Force Chromium to use public DNS resolvers — Windows DNS may filter
        # some CF-protected hosts (xdmovies.wtf etc.) which manifests as
        # `net::ERR_NAME_NOT_RESOLVED` in Playwright.
        "--dns-server=1.1.1.1,8.8.8.8",
    ]
    if useragent:
        args.append(f"--user-agent={useragent}")
    return args


def warn_if_stealth_missing() -> None:
    if not _STEALTH_OK:
        logger.warning(
            "playwright_stealth not importable (%s); continuing without stealth.",
            _STEALTH_ERROR,
        )


# Gate-page fingerprint consistency script. Applied via
# ctx.add_init_script() in every gate context. Covers ONLY things CloakBrowser's C++ patches and
# playwright-stealth do not already handle, to avoid double-spoof
# conflicts:
#
# 1. platform spoof: headed Chromium on the Linux VPS reports
#    "Linux x86_64" while our UA says "Windows NT 10.0" — a classic
#    automation tell CF checks. Spoof to Win32 to match the UA.
# 2. languages match: link_exec sends Accept-Language: en-US,en;q=0.9
#    headers, so navigator.languages must agree (default Playwright
#    context reports only ["en-US"] or the host locale).
# 3. webdriver=false + window.chrome presence: belt-and-braces
#    duplicates of what the C++ patches already do. Harmless if the
#    patches already hide them; decisive if a patch regresses.
# 4. permissions.query for notifications returns "denied" like a real
#    desktop Chrome with default settings (headless automation
#    typically reports "prompt").
#
# Deliberately NOT spoofed here (covered elsewhere or too risky):
# canvas/WebGL/audio (CloakBrowser C++ patches), plugins length
# (playwright-stealth), hardwareConcurrency/deviceMemory (verifiable
# against real hardware counters — spoofing invites mismatch).
GATE_FINGERPRINT_SCRIPT = r"""
(() => {
    try {
        Object.defineProperty(navigator, 'webdriver', { get: () => false, configurable: true });
    } catch (e) {}
    try {
        Object.defineProperty(navigator, 'platform', { get: () => 'Win32', configurable: true });
    } catch (e) {}
    try {
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'], configurable: true });
        Object.defineProperty(navigator, 'language', { get: () => 'en-US', configurable: true });
    } catch (e) {}
    try {
        if (!window.chrome) {
            Object.defineProperty(window, 'chrome', {
                value: { runtime: {}, loadTimes: function(){}, csi: function(){} },
                configurable: true, writable: true,
            });
        }
    } catch (e) {}
    try {
        const origQuery = window.navigator.permissions && window.navigator.permissions.query;
        if (origQuery) {
            window.navigator.permissions.query = function (params) {
                if (params && params.name === 'notifications') {
                    return Promise.resolve({ state: 'denied' });
                }
                return origQuery.apply(this, arguments);
            };
        }
    } catch (e) {}
})();
"""


def gate_fingerprint_script() -> str:
    """Return the gate fingerprint init script (always available —
    pure JS string, no dependency on playwright_stealth)."""
    return GATE_FINGERPRINT_SCRIPT


async def apply_gate_context_init_scripts(context) -> None:
    await context.add_init_script(gate_fingerprint_script())
    plugin = stealth_plugin()
    if plugin is None:
        return
    init = getattr(plugin, "init_script", None) or getattr(plugin, "init_scripts", None)
    if isinstance(init, list):
        for script in init:
            if script:
                await context.add_init_script(script)
    elif init:
        await context.add_init_script(init)


def chromium_launch_kwargs(
    *,
    browser_type: str,
    headless: bool,
    useragent: Optional[str],
) -> dict:
    """Build kwargs for `playwright.chromium.launch` (sync or async)."""
    # FORCED HEADFULL: ignore caller headless flag — Cloudflare needs headed.
    kwargs: dict = {
        "headless": False,
        "args": list(_chromium_args(useragent)),
    }
    if browser_type == "chrome":
        kwargs["channel"] = "chrome"
    elif browser_type == "msedge":
        kwargs["channel"] = "msedge"
    return kwargs


def is_chromium_family(browser_type: str) -> bool:
    return browser_type in {"chromium", "chrome", "msedge"}


__all__ = [
    "stealth_available",
    "stealth_import_error",
    "stealth_plugin",
    "apply_gate_context_init_scripts",
    "gate_fingerprint_script",
    "chromium_launch_kwargs",
    "is_chromium_family",
    "warn_if_stealth_missing",
]

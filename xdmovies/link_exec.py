"""Stage 5: Drive the gate page (latestnewsonline.sbs) through Generate/Continue/Go -- MANUAL COOKIE MODE.

No auto CF solver, no Gemma, no Cantarebella sidecar.
CF clearance is supplied by the user via /setcookie and injected from
xdmovies_state/cf_cookies.json.

If the gate is still behind Cloudflare, we raise CFChallengeError so the
bot can prompt the user to solve and send a fresh cookie.
"""
from __future__ import annotations

import asyncio
import re
import sys
import time
from typing import Dict, Optional, Tuple

from xdmovies.cookie_store import CFChallengeError, get_cookies_for_url, get_user_agent_for_url, _normalize_domain, set_cookies_for_domain
from xdmovies.browser_stealth import is_chromium_family, stealth_plugin

import os

# Env-gated auto-nudge for the gate's inner Turnstile (the f/av0 widget
# that appears after Continue). On a datacenter VPS IP the widget never
# auto-verifies -- a single nudge-click at the typical checkbox offset
# captures a fresh cf_clearance for the same browser context. On a
# residential IP the widget auto-solves and the nudge is a harmless
# no-op. Set XDMOVIES_INNER_NUDGE=0 to fall back to the old passive
# wait (matches AGENTS.md section4 env-gated-feature pattern).
INNER_TURNSTILE_NUDGE = os.environ.get("XDMOVIES_INNER_NUDGE", "1").lower() not in ("0", "false", "no")
INNER_TURNSTILE_NUDGE_WAIT = float(os.environ.get("XDMOVIES_INNER_NUDGE_WAIT", "30") or "30")

GATE_HOST = "latestnewsonline.sbs"

DEST_HOST_HINTS = (
    "hubcloud", "hubdrive", "gdflix", "gofile", "vcloud",
    "drive.google", "fdrive", "ddl", "faststream", "gamerxyt",
)

# Hub details cache: url -> (final_url, title, size, timestamp).
# The browser flows already load hub pages behind CF; parsing the loaded
# DOM and sharing it here beats a separate HTTPS fetch that a datacenter
# IP cannot complete. 1h TTL; best-effort, never raises.
_HUB_DETAILS_CACHE: Dict[str, Tuple[str, str, str, float]] = {}
_HUB_CACHE_TTL = 3600.0


def note_hub_details(url: str, final_url: str = "", title: str = "", size: str = "") -> None:
    try:
        if not url:
            return
        if not final_url and not title:
            return
        _HUB_DETAILS_CACHE[url] = (final_url or "", title or "", size or "", time.time())
    except Exception:
        pass


def get_hub_details(url: str):
    """(final_url, title, size) or None. Fresh entries only."""
    try:
        e = _HUB_DETAILS_CACHE.get(url or "")
        if not e:
            return None
        if time.time() - e[3] > _HUB_CACHE_TTL:
            _HUB_DETAILS_CACHE.pop(url, None)
            return None
        return e[0], e[1], e[2]
    except Exception:
        return None

# Hosts that are NEVER destinations (ad networks / CPA landers observed
# in the wild). Checked first: a substring hit on a dest hint inside an
# ad URL must never promote an ad tab/link (e.g. "pureddl" tripping the
# "ddl" hint in a glovefork.info CPA URL sent users an ad instead of the
# movie link).
AD_HOST_HINTS = (
    "glovefork.info",
    "getsmartyapp.com",
    "aichouphaugn.com",
    "crn77.com",
    "dd133.com",
    "aqle3.com",
    "d2n5v45616cm6u.cloudfront.net",
    "sootoarathus.net",
)

def _url_host(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return (urlparse(url or "").hostname or "").lower()
    except Exception:
        return ""

def _is_ad_url(url: str) -> bool:
    h = _url_host(url)
    if not h:
        return False
    return any(h == a or h.endswith("." + a) for a in AD_HOST_HINTS)

def _is_destination_url(url: str) -> bool:
    """Host-based destination check -- never full-URL substring.

    Matching the whole URL lets query junk promote ads (e.g. "pureddl"
    in a CPA lander tripping the "ddl" hint). Host-only matching plus
    the ad-host blocklist above keeps ad tabs/links out of results.
    """
    h = _url_host(url)
    if not h:
        return False
    if _is_ad_url(url):
        return False
    return any(x in h for x in DEST_HOST_HINTS)


async def fetch_hub_details(url: str, timeout: float = 12.0):
    """Cheap static read of a hub drive page (no browser).

    Plain HTTPS GET with stored cookies + UA, parsed by
    extractor.extract_hub_details. Returns the details, or None when the
    fetch fails, is challenged, or parses empty -- callers fall back to
    the browser flow. Never raises. Only attempted for hubcloud/hubdrive
    hosts (the parser is shaped for their drive pages).
    """
    try:
        h = _url_host(url or "")
        if not h or ("hubcloud" not in h and "hubdrive" not in h):
            return None
        try:
            import httpx
        except Exception:
            return None
        cookies = {}
        try:
            for c in get_cookies_for_url(url) or []:
                if c.get("name") and c.get("value"):
                    cookies[c["name"]] = c["value"]
        except Exception:
            cookies = {}
        try:
            ua = get_user_agent_for_url(url) or None
        except Exception:
            ua = None
        headers = {
            "Referer": "https://latestnewsonline.sbs/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        if ua:
            headers["User-Agent"] = ua
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, headers=headers, cookies=cookies
        ) as cli:
            r = await cli.get(url)
        if r.status_code != 200:
            return None
        html = r.text or ""
        if len(html) < 2000:
            return None
        low = html.lower()
        if "just a moment" in low or "checking your browser" in low or "cf-mitigated" in low:
            return None
        from xdmovies.extractor import extract_hub_details
        d = extract_hub_details(html)
        if not d.final_url and not d.title:
            return None
        return d
    except Exception:
        return None


async def resolve_hub_details(url: str):
    """(final_url, title, size) for a hub drive URL via static read.

    Validates the parsed final URL (must itself be a destination, never
    an ad or a self-loop). Falls back to (url, "", "") on anything
    unexpected -- callers keep today's behavior. Never raises.
    """
    try:
        d = await fetch_hub_details(url)
    except Exception:
        d = None
    if not d:
        return url, "", ""
    final = (d.final_url or "").strip()
    if final and final != url and _is_destination_url(final) and not _is_ad_url(final):
        return final, d.title, d.size
    return url, d.title, d.size

ANTI_AD_INIT_SCRIPT = r"""
(() => {
    if (window.__adNeutralized) return;
    window.__adNeutralized = true;
    const noop = function () { return null; };
    try {
        Object.defineProperty(window, 'open', { configurable: false, writable: false, value: noop });
    } catch (e) { window.open = noop; }
    try {
        const realAssign = window.location.assign.bind(window.location);
        const realReplace = window.location.replace.bind(window.location);
        const isSafe = (url) => {
            try { const u = new URL(url, window.location.href); return u.origin === window.location.origin; } catch (e) { return false; }
        };
        window.location.assign = function (url) { if (isSafe(url)) return realAssign(url); return null; };
        window.location.replace = function (url) { if (isSafe(url)) return realReplace(url); return null; };
    } catch (e) {}
    window.addEventListener('beforeunload', function (e) {
        try { Object.defineProperty(e, 'returnValue', { get: function () { return undefined; } }); } catch (_) {}
        e.preventDefault();
    }, true);
})();
"""

async def _wait_for_ad_impressions(page, max_wait: float = 8.0) -> None:
    deadline = time.time() + max_wait + 3.0
    while time.time() < deadline:
        try:
            state = await page.evaluate("""() => {
                    const c = document.getElementById('counter');
                    if (c) { const n = parseInt((c.innerText || c.textContent || '').trim(), 10); if (n === 0) return 'counter_zero'; }
                    const b = document.getElementById('continueBtn') || document.getElementById('goToLinkBtn');
                    if (b && b.classList && !b.classList.contains('hidden') && !b.disabled) return 'next_button_visible';
                    return 'waiting';
                }""")
            if state in ('counter_zero', 'next_button_visible'):
                await asyncio.sleep(0.2)
                return
        except Exception:
            break
        await asyncio.sleep(0.25)
    try:
        await page.wait_for_load_state("networkidle", timeout=int(max_wait * 1000))
    except Exception:
        pass

async def _is_on_gate_page(page) -> bool:
    try:
        result = await page.evaluate("""() => {
                const has = (id) => !!document.getElementById(id);
                const t = (document.title || '').toLowerCase();
                return { hasGenerate: has('generateBtn'), hasContinue: has('continueBtn'), hasGo: has('goToLinkBtn'), hasTurnstile: has('turnstileWidget'), titleOk: t.includes('get your link') || t.includes('xdmovies') };
            }""")
    except Exception:
        return False
    if not isinstance(result, dict):
        return False
    return (result.get("hasGenerate", False) or result.get("hasContinue", False)) and result.get("titleOk", False)

async def _close_ad_popups(ctx, gate_page) -> Optional[str]:
    """Close ad popup/popunder tabs and refocus the gate page.

    Clicking Generate/Continue/Go spawns ad popups (new tabs) that steal
    window focus. The gate pauses its countdown timer while its tab is in
    the background ("Timer paused - please stay on this tab"), so a stolen
    focus freezes the counter at 6 and Continue never appears. This helper
    closes every tab that is neither the gate page nor an already-loaded
    destination (hubcloud/drive/...), then brings the gate page back to
    the front. Returns a destination URL if a popup tab already holds one
    (Go sometimes opens the destination in a new tab), else None.
    """
    found_dest: Optional[str] = None
    try:
        pages = list(getattr(ctx, "pages", []) or [])
    except Exception:
        return None
    for p in pages:
        try:
            if p == gate_page:
                continue
            try:
                u = (p.url or "").lower()
            except Exception:
                u = ""
            if not u or u in ("about:blank", "chrome-error://chromewebdata/"):
                try: await p.close()
                except Exception: pass
                continue
            if _is_destination_url(p.url):
                from urllib.parse import urlparse
                try:
                    pp = urlparse(p.url)
                    if (pp.path and pp.path.strip("/") != "") or pp.query:
                        if found_dest is None:
                            found_dest = p.url
                        continue  # keep destination tabs open
                except Exception:
                    pass
            # Ad popup/popunder -- close it.
            try: await p.close()
            except Exception: pass
        except Exception:
            pass
    try:
        await gate_page.bring_to_front()
    except Exception:
        pass
    if found_dest:
        print(f"[link_exec] destination found in popup tab: {found_dest[:100]}")
    return found_dest


async def _wait_visible(page, selector: str, max_wait: float = 12.0) -> bool:
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            visible = await page.evaluate("""(sel) => {
                    const b = document.querySelector(sel);
                    if (!b) return false;
                    if (b.classList && b.classList.contains('hidden')) return false;
                    if (b.offsetParent === null) return false;
                    return !b.disabled;
                }""", selector)
            if visible:
                return True
        except Exception:
            pass
        await asyncio.sleep(0.4)
    return False

async def _nuke_overlays_stacking_on(page, selector: str) -> None:
    try:
        killed = await page.evaluate(r"""(sel) => {
                const b = document.querySelector(sel);
                if (!b) return {error: 'not_found'};
                const r = b.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) return {error: 'no_box'};
                const cx = r.x + r.width / 2; const cy = r.y + r.height / 2;
                const els = (document.elementsFromPoint ? document.elementsFromPoint(cx, cy) : []);
                let idx = -1;
                for (let i = 0; i < els.length; i++) { if (els[i] === b || b.contains(els[i])) { idx = i; break; } }
                if (idx <= 0) return {killed: 0, skipped: els.length};
                let count = 0;
                for (let i = 0; i < idx; i++) {
                    const e = els[i];
                    const tag = (e.tagName || '').toLowerCase();
                    if (tag === 'html' || tag === 'body' || tag === 'head') continue;
                    if (e.contains && e.contains(b)) continue;
                    try { e.style.setProperty('pointer-events', 'none', 'important'); e.style.setProperty('display', 'none', 'important'); count++; } catch (_) {}
                }
                return {killed: count, total_above: idx};
            }""", selector)
    except Exception:
        return
    if not killed or killed.get("error") or killed.get("killed") == 0:
        return
    await asyncio.sleep(0.15)

async def _real_click(page, selector: str, max_wait: float = 12.0) -> bool:
    """Click a selector with overlay-nuking + synthetic-click fallback.

    The latestnewsonline.sbs gate stacks ad iframes over the Generate /
    Continue / Go buttons. A vanilla `page.mouse.click()` at the button's
    bounding-box center can land on the iframe ad and miss the button
    entirely -- the click is registered but the gate never sees it, so
    the counter doesn't decrement and Continue never appears.

    Two safeguards:
      1. Nuke every element stacked above the target button at its
         center point (set pointer-events:none + display:none) BEFORE
         computing the click coordinates.
      2. After the mouse click, dispatch a synthetic mousedown/mouseup/
         click event sequence on the button as a fallback for the rare
         case the mouse click still missed.
    """
    try:
        loc = page.locator(selector)
        await loc.wait_for(state="visible", timeout=int(max_wait * 1000))
        try:
            await page.bring_to_front()
        except Exception:
            pass
        await _nuke_overlays_stacking_on(page, selector)
        box = await loc.bounding_box()
        if not box:
            return await _synthetic_click(page, selector)
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        try:
            await page.mouse.move(cx, cy, steps=4)
            await page.mouse.click(cx, cy, delay=120)
        except Exception:
            pass
        # Synthetic fallback: dispatches a click event the page's
        # socket.io handler WILL see even if the mouse click was eaten
        # by a residual overlay.
        await _synthetic_click(page, selector)
        return True
    except Exception:
        return False

async def _synthetic_click(page, selector: str) -> bool:
    try:
        ok = await page.evaluate("""(sel) => {
                const b = document.querySelector(sel);
                if (!b) return false;
                b.dispatchEvent(new MouseEvent('mousedown', {bubbles: false, cancelable: true, view: window, button: 0}));
                b.dispatchEvent(new MouseEvent('mouseup',   {bubbles: false, cancelable: true, view: window, button: 0}));
                b.dispatchEvent(new MouseEvent('click',     {bubbles: true,  cancelable: true, view: window, button: 0}));
                return true;
            }""", selector)
        return bool(ok)
    except Exception:
        return False

async def _find_legitimate_turnstile_frame(page):
    box = None
    try:
        box = await page.evaluate("""() => {
                const t = document.getElementById('turnstileContainer');
                if (!t) return null;
                const r = t.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) return null;
                return {x: r.x, y: r.y, w: r.width, h: r.height};
            }""")
    except Exception:
        box = None
    for fr in page.frames:
        try:
            u = fr.url or ""
        except Exception:
            u = ""
        if "challenges.cloudflare.com" not in u and "turnstile" not in u:
            continue
        if not box:
            return fr
        try:
            fb = await fr.locator("body").bounding_box()
            if not fb:
                continue
            cx = fb["x"] + fb["width"] / 2
            cy = fb["y"] + fb["height"] / 2
            if (box["x"] <= cx <= box["x"] + box["w"] and box["y"] <= cy <= box["y"] + box["h"]):
                return fr
        except Exception:
            continue
    # fallback: any turnstile frame
    for fr in page.frames:
        try:
            u = fr.url or ""
            if "challenges.cloudflare.com" in u or "turnstile" in u:
                return fr
        except Exception:
            continue
    return None

def _classify_turnstile_mode(frame_url: str) -> str:
    """Same classifier as old solver: light/managed = passive, non-interactive = auto, interactive = needs click."""
    if not frame_url: return "unknown"
    from urllib.parse import urlparse
    p = urlparse(frame_url).path
    parts = [s for s in p.split("/") if s]
    for k in ("non-interactive", "light", "managed", "interactive", "captcha"):
        if k in parts: return k
    return "unknown"

async def _click_turnstile_if_present(page, max_wait: float = 3.0) -> bool:
    """Click the Turnstile checkbox inside the gate's iframe if visible.

    The latestnewsonline.sbs gate renders an interactive Turnstile widget
    (`<div class="cf-turnstile" data-callback="onTurnstileSuccess">`)
    inside `challenges.cloudflare.com/turnstile/v0/...`. Without a
    cf_clearance, the widget's auto-verify will reject the bot's
    fingerprint; an actual click inside the iframe can still produce a
    real token (CloakBrowser's humanized mouse mimics a desktop click).

    Returns True if a Turnstile iframe was found AND a click was issued
    in it; False otherwise. Best-effort: any failure is silently
    swallowed because callers fall back to a passive wait for
    `#goToLinkBtn`.
    """
    # Find the Turnstile iframe.
    target = None
    try:
        for fr in page.frames:
            try:
                u = (fr.url or "").lower()
            except Exception:
                continue
            if "challenges.cloudflare.com" in u and "turnstile" in u:
                target = fr
                break
    except Exception:
        return False
    if target is None:
        return False
    try:
        # The checkbox lives in the iframe's body. Click center of body
        # -- CloakBrowser's humanize=True displaces the cursor naturally.
        loc = target.locator("body")
        await loc.wait_for(state="visible", timeout=int(max_wait * 1000))
        box = await loc.bounding_box()
        if not box:
            return False
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        # Cloudflare's checkbox is small (~24x24) and usually top-left.
        # Offset up-and-left to land on it.
        try:
            await page.mouse.move(cx + 12, cy + 12, steps=4)
            await page.mouse.click(cx + 12, cy + 12, delay=120)
        except Exception:
            await target.locator("body").click(force=True)
        return True
    except Exception:
        return False

async def _wait_go_button_enabled(page, max_wait: float = 20.0) -> bool:
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            enabled = await page.evaluate("() => { const b=document.getElementById('goToLinkBtn'); return !!(b && !b.classList.contains('hidden') && !b.disabled); }")
            if enabled:
                return True
        except Exception:
            pass
        await asyncio.sleep(0.4)
    return False

async def _solve_inner_turnstile_simple(page, max_wait: float = 30.0) -> bool:
    """Legacy passive-only wait for the gate's Go button.

    Deprecated: kept as a fallback when XDMOVIES_INNER_NUDGE=0. New code
    path is `_solve_inner_turnstile_with_nudge` which adds a single
    nudge-click on the Turnstile iframe and polls for a fresh
    cf_clearance. On a datacenter VPS IP this passive-only path is a
    guaranteed hang -- the f/av0 widget never auto-verifies.
    """
    try:
        fr = await _find_legitimate_turnstile_frame(page)
        url = (fr.url or "")[:80] if fr is not None else "(no frame)"
    except Exception:
        url = "(frame lookup failed)"
    print(f"[turnstile] passive wait for #goToLinkBtn (frame: {url})")
    await asyncio.sleep(2)
    return await _wait_go_button_enabled(page, max_wait)


async def _solve_inner_turnstile_with_nudge(page, ctx, short_link_url: str, max_wait: float = 30.0):
    """Inner-Turnstile auto-nudge for the gate page.

    On the user's local residential IP the gate's f/av0 widget auto-
    verifies and the Go button enables by itself -- this helper is a
    harmless no-op (the nudge click happens, then the cookie poll
    immediately finds a fresh cf_clearance already produced by CF's
    own JS). On the VPS datacenter IP the widget stays inert; a single
    nudge-click on the iframe body at offset (30, 32) -- same strategy
    as solve_server._auto_nudge -- is enough to coax CF into issuing a
    fresh cf_clearance for the same browser context.

    Returns a (mode, value) tuple:
      ("auto", cf_clearance_value) -- a fresh cf_clearance was captured
                                     and saved to xdmovies_state.
      ("timeout", None)            -- nudge + max_wait exhausted; caller
                                     should raise CFChallengeError so
                                     the bot falls back to the
                                     user-clicked solve link.
    """
    pre_injected: set[str] = set()
    try:
        if ctx is not None:
            for c in await ctx.cookies():
                if c.get("name") == "cf_clearance":
                    v = c.get("value") or ""
                    if v:
                        pre_injected.add(v)
    except Exception:
        pass

    # Nudge the Turnstile iframe body at the typical checkbox offset.
    fr = await _find_legitimate_turnstile_frame(page)
    if fr is not None:
        try:
            await fr.click("body", position={"x": 30, "y": 32}, delay=80, timeout=5000)
            print("[link_exec] inner-turnstile nudge-click issued")
        except Exception as exc:
            print(f"[link_exec] inner-turnstile nudge-click failed (continuing): {exc!r}")
    else:
        print("[link_exec] inner-turnstile nudge skipped (no frame found)")

    # Poll for a FRESH cf_clearance (not in pre_injected set).
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            if ctx is not None:
                for c in await ctx.cookies():
                    if c.get("name") != "cf_clearance":
                        continue
                    v = c.get("value") or ""
                    if not v or len(v) <= 20:
                        continue
                    if v in pre_injected:
                        continue
                    print(f"[link_exec] inner-turnstile captured fresh cf_clearance ({len(v)} chars)")
                    try:
                        ua = get_user_agent_for_url(short_link_url)
                        set_cookies_for_domain(GATE_HOST, f"cf_clearance={v}", user_agent=ua)
                        print(f"[link_exec] saved cf_clearance for host={GATE_HOST}")
                    except Exception as exc:
                        print(f"[link_exec] cookie_store save failed (non-fatal): {exc!r}")
                    return ("auto", v)
        except Exception:
            pass
        await asyncio.sleep(1.0)
    return ("timeout", None)




async def _extract_destination(page, ctx=None) -> Optional[str]:
    # When ctx is given, also scan every other open tab: the Go button
    # sometimes opens the destination (hubcloud/drive/...) in a NEW tab
    # while the gate tab stays behind. Without this scan the new-tab
    # destination is invisible to the caller.
    if ctx is not None:
        try:
            for p in list(getattr(ctx, "pages", []) or []):
                try:
                    u = p.url or ""
                except Exception:
                    continue
                if p is page or not u:
                    continue
                if _is_destination_url(u):
                    from urllib.parse import urlparse as _up
                    try:
                        pp = _up(u)
                        if (pp.path and pp.path.strip("/") != "") or pp.query:
                            return u
                    except Exception:
                        return u
        except Exception:
            pass
    try:
        current = page.url or ""
    except Exception:
        current = ""
    if current:
        if _is_destination_url(current):
            from urllib.parse import urlparse
            try:
                p = urlparse(current)
                if p.path and p.path.strip("/") != "":
                    return current
            except Exception:
                if "/" in current.split("//", 1)[-1].split("?", 1)[0].lstrip("/"):
                    return current
    try:
        hrefs = await page.evaluate("""() => {
                return [...document.querySelectorAll('a[href]')].map(a => ({href: a.getAttribute('href') || '', text: (a.innerText||'').slice(0, 80)}))
                    .filter(x => x.href && !x.href.startsWith('#') && !x.href.startsWith('javascript'));
            }""")
    except Exception:
        hrefs = []
    for item in hrefs:
        href = (item.get("href") or "")
        if _is_destination_url(href):
            return href
    for item in hrefs:
        href = (item.get("href") or "")
        text = (item.get("text") or "").lower().strip()
        if text in ("go to download", "continue to download", "go to link", "open link", "click here"):
            if href and not href.startswith("#"):
                return href
    return None

async def _wait_for_hubcloud_deep_link(page, max_wait: float = 30.0, ctx=None) -> Optional[str]:
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            current = page.url or ""
        except Exception:
            current = ""
        if current:
            if _is_destination_url(current):
                from urllib.parse import urlparse
                try:
                    p = urlparse(current)
                    if p.path and p.path.strip("/") != "":
                        return current
                except Exception:
                    pass
        dest = await _extract_destination(page, ctx)
        if dest:
            from urllib.parse import urlparse
            try:
                p = urlparse(dest)
                if (p.path and p.path.strip("/") != "") or p.query:
                    return dest
            except Exception:
                return dest
        await asyncio.sleep(0.5)
    return await _extract_destination(page, ctx)

# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

async def _gate_via_manual(short_link_url: str, session_id: str, step1_wait: float, step2_wait: float, final_wait: float, ad_impression_wait: float) -> Optional[str]:
    from xdmovies.cloak_stealth import launch_chromium
    # Audit finding M7: the previous fallback loop re-called
    # get_cookies_for_url(short_link_url) after it had already returned
    # empty. Dead code. The audit's C3 fix in link_follower.py also
    # pre-injects cookies for latestnewsonline.sbs, so by the time
    # this runs, cookies are already populated if the user has a
    # fresh-enough cookie.
    cookies = get_cookies_for_url(short_link_url)
    ua = get_user_agent_for_url(short_link_url)
    browser = await launch_chromium(
        browser_type="chromium", headless=False, useragent=ua,
    )
    ctx_kwargs = {}
    if ua: ctx_kwargs["user_agent"] = ua
    ctx = await browser.new_context(**ctx_kwargs)
    try:
        if is_chromium_family("chromium"):
            plugin = stealth_plugin()
            if plugin is not None:
                try:
                    init = getattr(plugin, "init_script", None) or getattr(plugin, "init_scripts", None)
                    if isinstance(init, list):
                        for s in init: await ctx.add_init_script(s)
                    elif init: await ctx.add_init_script(init)
                except: pass
    except: pass
    await ctx.add_init_script(ANTI_AD_INIT_SCRIPT)
    if cookies:
        try: await ctx.add_cookies(cookies)
        except Exception:
            try: await ctx.add_cookies([{**c, "domain": c["domain"].lstrip(".")} for c in cookies])
            except: pass
    page = await ctx.new_page()
    try:
        await page.goto(short_link_url, wait_until="load", timeout=120000)
        print("[link_exec] navigated (cookie mode)")
        # CF wait: be generous (was 5s raise; now up to 60s) so a slow
        # edge check or auto-verify doesn't immediately fail the call.
        for i in range(120):
            try:
                title = await page.title()
                html = await page.content()
                is_chal = "Just a moment" in title or "Checking your browser" in title or "cf-mitigated" in html
                if is_chal:
                    if i % 10 == 0: print(f"[link_exec] still on CF challenge (t={i*0.5}s) title={title!r}")
                    if i > 120: raise CFChallengeError(short_link_url, reason="still_challenge_after_cookie", host=_normalize_domain(short_link_url))
                else:
                    if title and "Just a moment" not in title and "Checking your browser" not in title:
                        print(f"[link_exec] title ready after {i*0.5}s -> {title!r}")
                        break
            except CFChallengeError: raise
            except: pass
            await asyncio.sleep(0.5)
        await asyncio.sleep(2)
        try:
            title = await page.title()
            html = await page.content()
            if "Just a moment" in title or "Checking your browser" in title or "cf-mitigated" in html:
                raise CFChallengeError(short_link_url, reason="cf_still_present", host=_normalize_domain(short_link_url))
        except CFChallengeError: raise
        except: pass

        # Wait for the gate UI to settle. After CF clears, the title
        # briefly says 'Loading <url>' while the socket.io connection
        # establishes and the Generate button becomes clickable.
        deadline_settle = time.time() + 15.0
        while time.time() < deadline_settle:
            try:
                t = (await page.title()) or ""
            except Exception:
                t = ""
            if "Loading" not in t and "Just a moment" not in t:
                try:
                    html_len = len(await page.content())
                    if html_len > 5000:
                        ok = await page.evaluate(
                            "() => { const b=document.getElementById('generateBtn'); return !!(b && !b.classList.contains('hidden') && !b.disabled); }"
                        )
                        if ok:
                            break
                except Exception:
                    pass
            await asyncio.sleep(0.5)
        try:
            await page.bring_to_front()
        except Exception:
            pass
        await _close_ad_popups(ctx, page)

        # Ad impression wait (skip if 0). After this, the gate is ready.
        if ad_impression_wait > 0:
            print(f"[link_exec] waiting {ad_impression_wait}s for ad impressions...")
            await _wait_for_ad_impressions(page, max_wait=ad_impression_wait)

        try:
            body = (await page.evaluate("() => document.body.innerText") or "").lower()
        except Exception:
            body = ""
        on_gate = await _is_on_gate_page(page)
        if not on_gate:
            print(f"[link_exec] not on gate (DOM check); body[:200]={body[:200]!r}")
            return await _extract_destination(page, ctx)

        # STEP 1: click Generate. Counter starts at 6; we need >6s + ad
        # load. Loop with periodic popup-close + paused-notice refocus
        # so the counter doesn't stall while a popup steals focus.
        max_generate_attempts = 3
        got_continue = False
        for generate_attempt in range(1, max_generate_attempts + 1):
            if not await _wait_visible(page, "#generateBtn", max_wait=12.0):
                print(f"[link_exec] #generateBtn never visible (attempt {generate_attempt})")
                if generate_attempt < max_generate_attempts: continue
                return await _extract_destination(page, ctx)
            print(f"[link_exec] #generateBtn visible -> clicking (attempt {generate_attempt})")
            if not await _real_click(page, "#generateBtn"):
                print(f"[link_exec] click generate failed (attempt {generate_attempt})")
                if generate_attempt < max_generate_attempts: continue
                return None
            await _close_ad_popups(ctx, page)
            _pop_dest = await _close_ad_popups(ctx, page)
            if _pop_dest:
                print(f"[link_exec] destination opened in popup tab after generate: {_pop_dest[:100]}")
                return _pop_dest
            if ad_impression_wait > 0:
                await _wait_for_ad_impressions(page, max_wait=ad_impression_wait)

            # Keep-focused counter wait: poll for #continueBtn unhiding
            # while closing popups and refocusing if paused-notice
            # appears. Bumped from 12s to 25s to absorb ad load + counter.
            print(f"[link_exec] waiting for #continueBtn (counter=6s + ads) (attempt {generate_attempt})...")
            deadline_counter = time.time() + max(25.0, step1_wait)
            last_paused = False
            while time.time() < deadline_counter:
                await _close_ad_popups(ctx, page)
                try:
                    paused = await page.evaluate(
                        "() => { const n=document.getElementById('pausedNotice'); return !!(n && !n.classList.contains('hidden')); }"
                    )
                except Exception:
                    paused = False
                if paused and not last_paused:
                    try: await page.bring_to_front()
                    except Exception: pass
                last_paused = paused
                try:
                    ok = await page.evaluate(
                        "() => { const b=document.getElementById('continueBtn'); return !!(b && !b.classList.contains('hidden')); }"
                    )
                    if ok:
                        got_continue = True
                        break
                except Exception:
                    pass
                await asyncio.sleep(1.0)
            if got_continue:
                break
            print(f"[link_exec] #continueBtn did not appear (attempt {generate_attempt})")
            if generate_attempt < max_generate_attempts:
                print(f"[link_exec] retrying generate (counter never reached 0)")

        if not got_continue:
            return await _extract_destination(page, ctx)

        # STEP 2: click Continue. Same keep-focused pattern.
        if not await _real_click(page, "#continueBtn"):
            return None
        await _close_ad_popups(ctx, page)
        _pop_dest = await _close_ad_popups(ctx, page)
        if _pop_dest:
            print(f"[link_exec] destination opened in popup tab after continue: {_pop_dest[:100]}")
            return _pop_dest
        try:
            await page.wait_for_load_state("load", timeout=30000)
        except Exception:
            pass

        # Wait for the gate's Turnstile container to unhide. The Turnstile
        # widget is the second-challenge gate; without a valid fingerprint
        # it won't auto-verify and the operator's manual solve (or a fresh
        # cf_clearance cookie) is what enables #goToLinkBtn.
        print(f"[link_exec] waiting for #turnstileContainer (up to {step2_wait:.0f}s)...")
        deadline_ts = time.time() + max(60.0, step2_wait)
        while time.time() < deadline_ts:
            await _close_ad_popups(ctx, page)
            try:
                await page.bring_to_front()
            except Exception:
                pass
            try:
                ok = await page.evaluate(
                    "() => { const t=document.getElementById('turnstileContainer'); return !!(t && !t.classList.contains('hidden')); }"
                )
                if ok:
                    break
            except Exception:
                pass
            await asyncio.sleep(1.0)

        # Inner Turnstile (the f/av0 widget that unhides after Continue).
        # On a residential IP it auto-verifies and Go enables by itself.
        # On a datacenter VPS IP it stays inert -- nudge it once and poll
        # for a fresh cf_clearance. If neither auto-nudge nor auto-verify
        # captures within ~max_wait, raise so the bot's existing CF
        # handler at bot.py:569-602 issues a fresh solve link for the
        # gate host (latestnewsonline.sbs).
        if INNER_TURNSTILE_NUDGE:
            mode, _val = await _solve_inner_turnstile_with_nudge(
                page, ctx, short_link_url, max_wait=INNER_TURNSTILE_NUDGE_WAIT,
            )
            captcha_ok = (mode == "auto")
        else:
            captcha_ok = await _solve_inner_turnstile_simple(page, max_wait=30.0)
        if not captcha_ok:
            print(f"[link_exec] inner-turnstile not solved; raising CFChallengeError for manual solve link")
            raise CFChallengeError(
                short_link_url,
                reason="inner_turnstile_unsolved",
                host=_normalize_domain(short_link_url),
            )
        if ad_impression_wait > 0 and captcha_ok:
            await _wait_for_ad_impressions(page, max_wait=ad_impression_wait)

        if captcha_ok:
            try:
                # Wait for #goToLinkBtn to enable.
                deadline_go = time.time() + 30.0
                while time.time() < deadline_go:
                    try:
                        if await page.evaluate("() => { const b=document.getElementById('goToLinkBtn'); return !!(b && !b.classList.contains('hidden') && !b.disabled); }"):
                            break
                    except Exception:
                        pass
                    await asyncio.sleep(0.4)
                await _nuke_overlays_stacking_on(page, "#goToLinkBtn")
                async def _nav_now():
                    async with page.expect_navigation(wait_until="load", timeout=30000): pass
                nav_task = asyncio.create_task(_nav_now())
                clicked = await _real_click(page, "#goToLinkBtn", max_wait=10.0)
                if not clicked:
                    try:
                        await page.evaluate("() => { const b=document.getElementById('goToLinkBtn'); if (b) b.click(); }")
                    except Exception:
                        pass
                try:
                    await nav_task
                except Exception:
                    pass
            except Exception as exc:
                print(f"[link_exec] goToLink click/nav failed: {exc}")

        _pop_dest = await _close_ad_popups(ctx, page)
        if _pop_dest:
            print(f"[link_exec] final destination in popup tab: {_pop_dest[:100]}")
            return _pop_dest

        # First hop: extract destination via the existing helper (handles
        # new-tab destinations). Then, if we landed on a hub page
        # (hubcloud / hubdrive / fdrive / gamerxyt), follow the inner
        # redirect link to the final token-bearing URL.
        dest = await _wait_for_hubcloud_deep_link(page, max_wait=final_wait, ctx=ctx)
        if not dest:
            dest = await _extract_destination(page, ctx)
        if dest:
            print(f"[link_exec] hub page: {dest}")
            hub_hints = ("hubcloud", "hubdrive", "gdflix", "fdrive", "gamerxyt")
            if _is_ad_url(dest) or not any(h in _url_host(dest) for h in hub_hints):
                return dest
            else:
                # Fast path first: the hub drive page states its title, size
                # and final gamerxyt token URL statically -- one cheap HTTPS
                # GET instead of a full browser navigation.
                try:
                    _rf, _rt, _rs = await resolve_hub_details(dest)
                    if _rf and _rf != dest:
                        print(f"[link_exec] hub static fast path: {dest[:80]} -> {_rf[:80]}")
                        return _rf
                except Exception:
                    pass
                try:
                    await page.goto(dest, wait_until="domcontentloaded", timeout=30000)
                    # The loaded hub page states its own title/size/final
                    # URL statically -- parse and share via cache (zero
                    # extra network; works behind CF where plain HTTPS
                    # fetches are challenged).
                    try:
                        from xdmovies.extractor import extract_hub_details as _ehd
                        _html = await page.content()
                        _hd = _ehd(_html or "")
                        if _hd.title or _hd.final_url:
                            note_hub_details(dest, _hd.final_url, _hd.title, _hd.size)
                            if _hd.final_url and _hd.final_url != dest \
                                    and _is_destination_url(_hd.final_url) \
                                    and not _is_ad_url(_hd.final_url):
                                print(f"[link_exec] hub page parse: {dest[:80]} -> {_hd.final_url[:80]}")
                                return _hd.final_url
                    except Exception:
                        pass
                    for sel in ("a#download", "a.btn-download", "a.btn-primary",
                                "button.btn-primary", "a[class*='download']"):
                        try:
                            loc = page.locator(sel)
                            if await loc.count() > 0:
                                href = await loc.first.get_attribute("href")
                                if href and ("http" in href or href.startswith("/")):
                                    if href.startswith("/"):
                                        from urllib.parse import urljoin
                                        href = urljoin(dest, href)
                                    print(f"[link_exec] hub redirect href: {href}")
                                    dest = href
                                    break
                        except Exception:
                            pass
                except Exception as exc:
                    print(f"[link_exec] hub follow failed: {exc}")
            return dest
        return await _extract_destination(page, ctx)
    finally:
        try: await page.close()
        except: pass
        try: await ctx.close()
        except: pass
        try: await browser.close()
        except: pass

async def execute_button_flow(
    short_link_url: str,
    session_id: str = "xdmovies_button_session",
    headless: bool = False,
    step1_wait: float = 8.0,
    step2_wait: float = 8.0,
    final_wait: float = 20.0,
    ad_impression_wait: float = 1.5,
) -> Optional[str]:
    """Manual cookie only -- bullet proof, no auto solver. Speed-tuned."""
    return await _gate_via_manual(short_link_url, session_id, step1_wait, step2_wait, final_wait, ad_impression_wait)


def execute_button_flow_sync(short_link_url: str, headless: bool = False, ad_impression_wait: float = 5.0) -> Optional[str]:
    return asyncio.run(execute_button_flow(short_link_url, headless=False, ad_impression_wait=ad_impression_wait))

async def _main():
    url = sys.argv[1] if len(sys.argv) > 1 else input("short-link URL: ").strip()
    dest = await execute_button_flow(url)
    print("FINAL DESTINATION:", dest)

if __name__ == "__main__":
    asyncio.run(_main())

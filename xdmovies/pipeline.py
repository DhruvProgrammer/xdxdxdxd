"""Stage 1+2+3: Movie page (URL1) → extract all download links (URLs2) — MANUAL CF ONLY (bullet proof).

  https://top.xdmovies.wtf/movies/<slug>
      ↓ fetch with stored cf_clearance (manual) via Playwright
  HTML with 6 <a href="https://link.xdmovies.wtf/download/<base64>">...

If still Cloudflare, raise CFChallengeError → bot asks you to paste cf_clearance (reply here).
Cookie saved to xdmovies_state/cf_cookies.json and reused until 1h expiry.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import re
from dataclasses import dataclass
from typing import List, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from xdmovies.extractor import Card, extract_cards_from_html
from xdmovies.cookie_store import CFChallengeError, get_cookies_for_url, get_user_agent_for_url, _normalize_domain

# ---------------------------------------------------------------------------
# Challenge detection (lightweight, no solver needed)
# ---------------------------------------------------------------------------

async def _is_cf_challenge_page(page) -> bool:
    # Strong detection: title + body markers + challenge form + CF headers
    try:
        title = (await page.title()) or ""
        tl = title.lower()
        if "just a moment" in tl or "checking your browser" in tl or "attention required" in tl or "ddos protection" in tl or "security check" in tl:
            return True
    except Exception:
        pass
    try:
        html = await page.content()
        hl = html.lower()
        # CF interstitial markers
        if "cf-mitigated" in hl: return True
        if "cf-please-wait" in hl or "cf-spinner-please-wait" in hl: return True
        if "challenge-platform" in hl and "turnstile" in hl: return True
        if "cf-challenge" in hl or "cf-browser-verification" in hl: return True
        if "checking your browser before accessing" in hl: return True
        # also check for Turnstile iframe still present + tiny body
        if len(html) < 8000 and ("challenges.cloudflare.com" in html or "cf-challenge" in hl):
            try:
                cnt = await page.locator("#challenge-form, #cf-challenge-running, .cf-turnstile").count()
                if cnt > 0: return True
            except: pass
            if len(html) < 5000:
                return True
    except Exception:
        pass
    try:
        cnt = await page.locator("#challenge-form, #cf-challenge-running").count()
        if cnt > 0:
            return True
    except Exception:
        pass
    return False


async def _try_auto_nudge(page, ctx, url: str) -> bool:
    """One auto-bypass attempt on a challenged movie page.

    Mirrors the proven solve_server._auto_nudge: give CF a chance to
    auto-verify, else click the Turnstile checkbox once, then re-check.
    On a fresh solve, saves the NEW cf_clearance (IP+UA bound to this
    browser) so next runs and keep-alive reuse it. Returns True when the
    challenge is gone. Never raises; worst case costs ~20s on a page
    that stays blocked (caller then raises CFChallengeError as before).
    """
    try:
        from xdmovies.cookie_store import set_cookies_for_domain
    except Exception:
        set_cookies_for_domain = None  # type: ignore
    injected: set = set()
    try:
        if ctx is not None:
            for c in await ctx.cookies():
                if c.get("name") == "cf_clearance" and c.get("value"):
                    injected.add(c.get("value"))
    except Exception:
        pass

    async def fresh_cookie() -> str:
        try:
            if ctx is None:
                return ""
            for c in await ctx.cookies():
                if c.get("name") != "cf_clearance":
                    continue
                v = c.get("value") or ""
                if v and len(v) > 20 and v not in injected:
                    return v
        except Exception:
            pass
        return ""

    async def clear() -> bool:
        try:
            return not await _is_cf_challenge_page(page)
        except Exception:
            return False

    try:
        for _ in range(8):
            if await clear():
                return True
            await asyncio.sleep(1.0)
        if await fresh_cookie():
            return await clear()
        try:
            for fr in page.frames:
                try:
                    u = (fr.url or "").lower()
                except Exception:
                    continue
                if "challenges.cloudflare.com" in u and "turnstile" in u:
                    try:
                        await fr.click("body", position={"x": 30, "y": 32}, delay=80, timeout=5000)
                    except Exception:
                        pass
                    break
        except Exception:
            pass
        for _ in range(8):
            if await clear():
                break
            await asyncio.sleep(1.0)
        if not await clear():
            return False
        val = await fresh_cookie()
        if val and set_cookies_for_domain is not None:
            try:
                ua = await page.evaluate("navigator.userAgent")
            except Exception:
                ua = None
            try:
                set_cookies_for_domain(_normalize_domain(url), f"cf_clearance={val}", user_agent=ua)
            except Exception:
                pass
        return True
    except Exception:
        return False


async def _fetch_html_with_cookie_browser(url: str, timeout_ms: int = 35000) -> str:
    """Fetch url via CloakBrowser (patched Chromium) with stored
    cf_clearance injected. Raises CFChallengeError if still challenged.

    CloakBrowser is a drop-in Playwright replacement but with C++ source-
    level fingerprint patches (vs. JS-level playwright_stealth). It also
    has its own pre-configured context, so the API is slightly different
    from vanilla `pw.chromium.launch()`.
    """
    from xdmovies.cloak_stealth import launch_chromium

    cookies = get_cookies_for_url(url)
    ua = get_user_agent_for_url(url)

    browser = await launch_chromium(
        browser_type="chromium", headless=False, useragent=ua,
    )
    # CloakBrowser returns a context-like object. Create a new context
    # with our UA + cookies, then a page in it. Fall back to a direct
    # new_page() if the wrapper doesn't expose new_context (older versions).
    page = None
    ctx = None
    try:
        ctx_kwargs = {}
        if ua:
            ctx_kwargs["user_agent"] = ua
        try:
            ctx = await browser.new_context(**ctx_kwargs)
            page = await ctx.new_page()
        except Exception:
            # Older cloakbrowser: launch() returns a context, not a browser.
            page = await browser.new_page()
        if cookies and ctx is not None:
            try:
                await ctx.add_cookies(cookies)
            except Exception:
                try:
                    fallback = [{**c, "domain": c["domain"].lstrip(".")} for c in cookies]
                    await ctx.add_cookies(fallback)
                except Exception:
                    pass
        # Block heavy media for speed.
        try:
            await page.route(re.compile(r"\.(png|jpe?g|css|woff2?|gif|svg|ico)$"), lambda r: r.abort())
        except Exception:
            pass
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            await page.wait_for_load_state("load", timeout=4000)
        except Exception:
            pass
        await asyncio.sleep(0.8)
        is_chal = await _is_cf_challenge_page(page)
        html = await page.content()
        if is_chal:
            # Auto-nudge first: CF often auto-verifies within seconds and
            # an interactive widget can still be coaxed with one checkbox
            # click (same pattern as the gate flow). Only a page that
            # survives the nudge raises CFChallengeError as before.
            try:
                if await _try_auto_nudge(page, ctx, url):
                    html = await page.content()
                    is_chal = await _is_cf_challenge_page(page)
            except Exception:
                pass
        if is_chal:
            # Audit finding H1: previous code only raised
            # CFChallengeError if the challenge was *also* in the title
            # or the body was very small. With a stored cookie, a
            # larger/renamed CF interstitial would silently return the
            # challenge HTML as if it were the movie page, and the
            # extractor would find 0 cards and report "xdmovies returned
            # an empty page… slug may be invalid" — the wrong diagnosis.
            # Raise on any detected challenge, regardless of cookie state.
            raise CFChallengeError(
                url,
                reason=("no_cookie_stored" if not cookies else "still_challenge"),
                host=_normalize_domain(url),
            )
        return html
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


async def _solve_movie_page(movie_url: str, session_id: str, headless: bool) -> str:
    _ = session_id, headless
    return await _fetch_html_with_cookie_browser(movie_url)


async def fetch_rendered_html(url: str, session_id: str = "xdmovies_catalog_session", headless: bool = False) -> str:
    _ = session_id, headless
    return await _fetch_html_with_cookie_browser(url)


async def extract_download_links(movie_url: str, session_id: str = "xdmovies_video_session", headless: bool = False) -> List[str]:
    _ = session_id, headless
    html = await _fetch_html_with_cookie_browser(movie_url)
    cards = extract_cards_from_html(html)
    if not cards:
        try:
            os.makedirs("xdmovies_dumps", exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            dump_path = os.path.join("xdmovies_dumps", f"latest_movie_page_{stamp}.html")
            with open(dump_path, "w", encoding="utf-8", errors="replace") as f:
                f.write(html or "")
            with open("xdmovies_dumps/latest_movie_page.html", "w", encoding="utf-8", errors="replace") as f:
                f.write(html or "")
        except Exception:
            pass
    return [c.url for c in cards]


async def extract_download_links_with_titles(movie_url: str, session_id: str = "xdmovies_video_session", headless: bool = False) -> List[Tuple[str, str]]:
    _ = session_id, headless
    html = await _fetch_html_with_cookie_browser(movie_url)
    return [(c.url, c.title) for c in extract_cards_from_html(html)]


async def extract_cards_async(movie_url: str, session_id: str = "xdmovies_video_session", headless: bool = False) -> List[Card]:
    _ = session_id, headless
    html = await _fetch_html_with_cookie_browser(movie_url)
    cards = extract_cards_from_html(html)
    if not cards:
        try:
            os.makedirs("xdmovies_dumps", exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            dump_path = os.path.join("xdmovies_dumps", f"latest_movie_page_{stamp}.html")
            with open(dump_path, "w", encoding="utf-8", errors="replace") as f:
                f.write(html or "")
            with open("xdmovies_dumps/latest_movie_page.html", "w", encoding="utf-8", errors="replace") as f:
                f.write(html or "")
        except Exception:
            pass
    return cards


def extract_download_links_with_titles_from_html(html: str) -> List[Tuple[str, str]]:
    from xdmovies.extractor import quick_extract
    return quick_extract(html)


def extract_cards_from_html_async(html: str) -> List[Card]:
    from xdmovies.extractor import extract_cards_from_html as _ec
    return _ec(html)


@dataclass
class ExtractionResult:
    cards: List[Card]
    html_len: int
    bypass_status: str
    challenge_mode: str
    elapsed_time_seconds: float
    page_title: str = ""


async def extract_with_diagnostics(movie_url: str, session_id: str = "xdmovies_video_session", headless: bool = False) -> ExtractionResult:
    _ = session_id, headless
    start = time.time()
    html = await _fetch_html_with_cookie_browser(movie_url)
    bypass_status = "cookie"
    challenge_mode = "manual"
    cards = extract_cards_from_html(html)
    elapsed = round(time.time() - start, 3)
    if not cards:
        try:
            os.makedirs("xdmovies_dumps", exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            dump_path = os.path.join("xdmovies_dumps", f"latest_movie_page_{stamp}.html")
            with open(dump_path, "w", encoding="utf-8", errors="replace") as f:
                f.write(html or "")
            with open("xdmovies_dumps/latest_movie_page.html", "w", encoding="utf-8", errors="replace") as f:
                f.write(html or "")
        except Exception:
            pass
    page_title = _extract_page_title(html or "", url=movie_url)
    return ExtractionResult(cards=cards, html_len=len(html or ""), bypass_status=bypass_status, challenge_mode=challenge_mode, elapsed_time_seconds=elapsed, page_title=page_title)


# ---------------------------------------------------------------------------
# Title extraction — prefer og:title (clean), fall back to <title> minus
# the " — Series"/" — Movie" suffix, fall back to <h2> in the info block,
# fall back to URL slug.
# ---------------------------------------------------------------------------

_TITLE_SUFFIXES = (
    " — Series", " - Series", " – Series",
    " — Movie", " - Movie", " – Movie",
    " | XDMovies", " - XDMovies", " — XDMovies", " • XDMovies",
    " — Watch Online", " - Watch Online",
)

# Generic titles from listing pages (home / /?type=movies / /?type=series)
# — same template on all three, no per-item info in <title> or og:title.
_GENERIC_TITLE_EXACT = {
    "XDMovies - Watch Movies & TV Series Online",
    "XDMovies - Download Movies & TV Series Online | HD Streaming",
    "XDMovies - Watch & Download Movies",
    "XDMovies",
}

# Tokens that don't count as "real movie-name signal" when checking if a
# title containing "XDMovies" is actually a real title vs. a site-branded
# generic listing page.
_TITLE_STOPWORDS = {
    "a", "an", "the", "of", "and", "or", "in", "on", "to", "for",
    "watch", "download", "online", "movies", "series", "tv",
    "movie", "shows", "streaming", "hd", "free", "best", "top",
    "this", "week", "latest", "new", "all", "&",
}


def _clean_title(raw: str) -> str:
    if not raw:
        return ""
    s = re.sub(r"\s+", " ", raw).strip()
    for suf in _TITLE_SUFFIXES:
        if s.endswith(suf):
            s = s[: -len(suf)].rstrip()
            break
    return s[:120]


def _is_generic_site_title(t: str) -> bool:
    """True if `t` is the site's own generic branding, not a movie title."""
    if not t:
        return False
    if t in _GENERIC_TITLE_EXACT:
        return True
    low = t.lower()
    if "xdmovies" in low:
        # Site-branded title. If no proper-noun movie signal remains,
        # it's just the generic site header, not a real title.
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9']+", t)
        signal = [
            tok for tok in tokens
            if tok[0].isupper()
            and tok.lower() not in _TITLE_STOPWORDS
            and tok.lower() != "xdmovies"
        ]
        if not signal:
            return True
    return False


_SLUG_LANGS = {
    "hindi", "english", "tamil", "telugu", "malayalam",
    "bengali", "marathi", "kannada", "japanese", "korean",
    "punjabi", "gujarati", "spanish", "french", "german",
    "italian", "portuguese", "chinese", "arabic", "thai",
}


def _title_from_slug(url: str) -> str:
    """Pull the slug from a /movies/<slug> or /series/<slug> URL and
    title-case the remaining name tokens. Last-resort fallback when
    og:title / <title> / <h2> all yield the generic site branding
    (home / listing pages)."""
    if not url:
        return ""
    try:
        path = url.split("?", 1)[0].split("#", 1)[0]
        m = re.search(r"/(?:movies|series)/([^/]+)/?", path, re.IGNORECASE)
        if not m:
            return ""
        slug = m.group(1)
        # e.g. house-of-the-dragon-2160p-1080p-hindi-english-download-94997
        # -> house-of-the-dragon
        tokens = slug.split("-")
        # strip pure-digit id at end
        while tokens and tokens[-1].isdigit():
            tokens.pop()
        if tokens and tokens[-1].lower() == "download":
            tokens.pop()
        # strip quality tokens (anything containing a digit, e.g. 2160p, 1080p)
        tokens = [t for t in tokens if not any(ch.isdigit() for ch in t)]
        # strip common language codes
        tokens = [t for t in tokens if t.lower() not in _SLUG_LANGS]
        if not tokens:
            return ""
        return " ".join(t.capitalize() for t in tokens)[:120]
    except Exception:
        return ""


def _extract_page_title(html: str, url: str = "") -> str:
    """Pick the cleanest human-readable movie/series name from the page.

    Falls back through (in order):
      1. <meta property="og:title">       (cleanest)
      2. <title>  minus "— Series"/"— Movie" suffix
      3. <h2> inside .info (movie/series name without suffix)
      4. URL slug title-cased              (last-resort)

    Filters out the generic site title shared by home / listing pages.
    """
    if not html:
        return ""
    # 1. og:title (cleanest, exactly what social cards use)
    try:
        m = re.search(
            r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']',
            html, re.IGNORECASE,
        )
        if m:
            t = _clean_title(m.group(1))
            if t and not _is_generic_site_title(t):
                return t
    except Exception:
        pass
    # 2. <title> minus " — Series" / " — Movie" suffix
    try:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        if m:
            t = _clean_title(m.group(1))
            if t and not _is_generic_site_title(t):
                return t
    except Exception:
        pass
    # 3. <h2> inside .info (movie/series name without " — Series")
    try:
        m = re.search(
            r'<div\s+class=["\']info["\'][^>]*>\s*<h2[^>]*>(.*?)</h2>',
            html, re.IGNORECASE | re.DOTALL,
        )
        if m:
            t = _clean_title(re.sub(r"<[^>]+>", "", m.group(1)))
            if t and not _is_generic_site_title(t):
                return t
    except Exception:
        pass
    # 4. URL slug title-cased — last resort when the page is a generic
    #    listing (home / ?type=movies / ?type=series) with no per-item
    #    title in og:title / <title> / <h2>.
    return _title_from_slug(url)


async def _main():
    url = sys.argv[1] if len(sys.argv) > 1 else input("xdmovies URL: ").strip()
    links = await extract_download_links(url)
    print(f"\nFound {len(links)} download links:")
    for i, l in enumerate(links, 1):
        print(f"  {i}. {l}")


if __name__ == "__main__":
    asyncio.run(_main())

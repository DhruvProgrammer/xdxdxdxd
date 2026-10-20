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
#  File:    xdmovies/hub_mirrors.py
#  Fingerprint: BRUNO-E1CCB1FB-HM01
#  ============================================================

"""Stage 6: pull the real DDL mirror links out of a hub ("hubcloud") page.

The gate (latestnewsonline.sbs) lands on a hub drive page such as
``https://hubcloud.ist/drive/imv2ibxqt9bjx6o``. That page does not carry
the file links itself -- it carries an opaque, short-lived *generator* URL
that in turn renders the mirrors. The chain is two plain HTTPS GETs, no
browser and no Cloudflare challenge:

    STEP 1  GET https://hubcloud.ist/drive/<code>
            -> 200, ~18 KB. Holds ``var url = 'https://gamerxyt.com/
            hubcloud.php?host=hubcloud&id=<code>&token=<OPAQUE>'`` and the
            same URL on the ``<a id="download">`` anchor. The token is
            opaque (base64 of base64 of ciphertext) and NOT derivable --
            it must be scraped, and it rotates every few minutes.

    STEP 2  GET <that generator URL>
            -> 200, ~15 KB, body contains "Download Link Generated". The
            mirrors are flat ``<a>`` elements carrying a ``btn*`` class.

The mirror set is randomised per request (10Gbps always, Telegram always,
Buzz ~75%, PixelServer ~25%), so the generator page is fetched ONCE and
that single set is used -- never re-fetched per mirror.

The pixeldrain DECOY
--------------------
The pixeldrain anchor's ``href`` is always the constant
``https://pixeldrain.dev/u/negn6f``, which is dead (404). The real
per-file URL only exists in the inline ``var pxl = "...";`` script and is
applied by JS at click time. If the anchor host is pixeldrain.*, the href
is REPLACED with the ``var pxl`` value that the anchor's OWN id is bound
to; if that binding is missing, the mirror is dropped rather than emitted
dead. The document-wide ``var pxl`` fallback is used ONLY for an anchor
that carries no id at all -- never to bind another file's URL to it.

Public surface:
    HubMirror        dataclass: label, url, host
    HubPageInfo      dataclass: code, title, size, file_type, share_date
    MirrorResult     dataclass: info, mirrors, error
    fetch_hub_page(url, timeout, total_timeout) -> MirrorResult (never raises)
    fetch_hub_mirrors(url, timeout)   -> list[HubMirror] (never raises)
    extract_generator_url(html)       -> str | None
    extract_mirrors(html)             -> list[HubMirror]
    extract_hub_info(html)            -> HubPageInfo
    label_for(host, anchor_text)      -> str
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from html import unescape as _unescape
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlsplit

from xdmovies import url_guard

# --- policy -----------------------------------------------------------------

#: Hosts a mirror link may point at. EXACT host matching (h == base or
#: h.endswith("." + base)) -- never a substring match, so an ad host that
#: merely mentions a mirror name can never pass.
MIRROR_HOSTS: tuple[str, ...] = (
    "hubcloud.ist",
    "pixel.hubcloud.ist",
    "gpdl.hubcloud.ist",
    "pixeldrain.dev",
    "pixeldrain.com",
    "fuckingfast.net",
    # ZipDisk also serves some files from a public Google Cloud Storage
    # bucket.
    "storage.googleapis.com",
)

#: Mirror families whose subdomain is randomised per deployment, so no
#: single hostname can be allowlisted: ZipDisk on Cloudflare Workers
#: (ddl2.<random>.workers.dev) and FSL on Cloudflare R2
#: (<hash>.r2.cloudflarestorage.com). The patterns live in url_guard --
#: the policy module -- so the fetch guard and the echo filter apply
#: exactly the same rule.
MIRROR_HOST_PATTERNS = url_guard.MIRROR_HOST_PATTERNS

#: Per-host path policy for mirrors. hubcloud.ist is a valid mirror host
#: ONLY for the Telegram mirror -- the "/drive/admin" login link shares
#: that host and must never be emitted.
#:
#: DERIVED from the guard's single policy table so the two cannot drift
#: apart -- there used to be a near-duplicate of _HOST_PATH_PREFIXES
#: here, and the two disagreed on the randomised families.
MIRROR_PATH_PREFIXES: Dict[str, tuple[str, ...]] = dict(
    url_guard.MIRROR_PATH_PREFIXES
)

#: Known ad / tracker hosts seen on the generator page. They are not in
#: MIRROR_HOSTS anyway; this list is a second, explicit barrier.
AD_HOSTS: tuple[str, ...] = (
    "paisavyapari.com",
    "visheshtoday.com",
    "bonuscaf.com",
    "winexch.com",
    "acscdn.com",
    "a-ads.com",
    "sads.adsboosters.xyz",
    "d33f51dyacx7bd.cloudfront.net",
    "lh3.googleusercontent.com",
    "snvhost.com",
    "one.one.one.one",
    "google.com",
    "tinyurl.com",
    "t.me",
    "gamerxyt.com",
)

#: Stable, unambiguous short labels keyed by host. Host-derived beats the
#: page label because the page label varies ("[PixelServer : 2]").
HOST_LABELS: Dict[str, str] = {
    "pixel.hubcloud.ist": "10Gbps",
    "gpdl.hubcloud.ist": "10Gbps",
    "pixeldrain.dev": "pixeldrain",
    "pixeldrain.com": "pixeldrain",
    "fuckingfast.net": "buzz",
    "hubcloud.ist": "telegram",
    # ZipDisk: a Cloudflare Workers deployment OR a public GCS bucket.
    "storage.googleapis.com": "zipdisk",
    "zipdisk": "zipdisk",
    "fsl": "fsl",
}

#: The constant dead pixeldrain href the generator page always ships.
PIXELDRAIN_DECOY = "https://pixeldrain.dev/u/negn6f"

#: Success marker on the generator page.
GENERATOR_OK_MARKER = "Download Link Generated"

#: Body of a 404 on the hub host.
NOT_FOUND_BODY = "404 ! File Not Found"

#: Cap on manual redirect hops (each hop is re-validated). Single source
#: of truth: url_guard.MAX_REDIRECTS -- the two must never drift apart.
MAX_HOPS: int = url_guard.MAX_REDIRECTS

#: Overall wall-clock budget of one fetch_hub_page() call. A slow chain
#: (timeout + timeout + timeout) can never exceed it.
DEFAULT_TOTAL_TIMEOUT = 40.0

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# --- regexes ----------------------------------------------------------------

#: `var url = '...'` / `var url = "..."` -- ONE alternation so the EARLIEST
#: match in document order wins, whatever the quote style is.
_GEN_URL_RE = re.compile(
    r"var\s+url\s*=\s*['\"](https?://[^'\"]+)['\"]",
    re.IGNORECASE,
)

#: Attribute bodies that tolerate a `>` inside a quoted value.
_ATTR_BODY = r"(?:[^>\"']|\"[^\"]*\"|'[^']*'){0,500}?"

#: `<a ... id="download" ... href="...">` -- the fallback source of the
#: generator URL. The lookbehind keeps `data-id=` / `data-href=` from
#: matching.
_DOWNLOAD_ANCHOR_RE = re.compile(
    r"<a\b(?=" + _ATTR_BODY + r"(?<![\w-])id=[\"']download[\"'])"
    + _ATTR_BODY
    + r"(?<![\w-])href=[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)

#: Anchor open tag / anchor end markers.
_A_OPEN_RE = re.compile(r"<a\b", re.IGNORECASE)
_A_STOP_RE = re.compile(r"</a\b|<a\b", re.IGNORECASE)

#: Attribute lookups. The `(?<![\w-])` lookbehind is mandatory: `\bhref=`
#: matches inside `data-href=` (the `-` is a non-word char, so it IS a
#: word boundary) and the wrong attribute would win.
_HREF_RE = re.compile(r"(?<![\w-])href=[\"']([^\"']+)[\"']", re.IGNORECASE)
_CLASS_RE = re.compile(r"(?<![\w-])class=[\"']([^\"']+)[\"']", re.IGNORECASE)
_ID_RE = re.compile(r"(?<![\w-])id=[\"']([^\"']+)[\"']", re.IGNORECASE)
#: Strip a tag from the anchor's inner text. The length is BOUNDED on
#: purpose: the unbounded "<[^>]+>" rescans forward from every "<", so a
#: body of N bare "<" costs O(N^2) (80 000 of them -> 10 s). A bound of
#: 500 is far above any real tag and makes the scan linear. A tag longer
#: than that is left in the text, which is harmless -- the renderer
#: HTML-escapes everything anyway.
_TAG_RE = re.compile(r"<[^>]{0,500}>")
#: `var pxl = "https://pixeldrain.dev/u/xxxx";` and friends.
_PXL_VAR_RE = re.compile(
    r"\bvar\s+([A-Za-z_$][\w$]*)\s*=\s*[\"'](https?://[^\"']+)[\"']", re.IGNORECASE
)
#: `document.getElementById("pxl-1").href = pxl;`
_PXL_BIND_RE = re.compile(
    r"getElementById\(\s*[\"']([^\"']+)[\"']\s*\)\s*\.\s*href\s*=\s*([A-Za-z_$][\w$]*)",
    re.IGNORECASE,
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_CARD_HEADER_RE = re.compile(
    r"(?<![\w-])class=[\"']card-header[^\"']*[\"'][^>]*>(.*?)</div>", re.IGNORECASE | re.DOTALL
)
_SIZE_RE = re.compile(r"File\s*Size\s*<i[^>]*>([^<]{1,40})</i>", re.IGNORECASE)
_FILE_TYPE_RE = re.compile(r"File\s*Type\s*<i[^>]*>([^<]{1,60})</i>", re.IGNORECASE)
_SHARE_DATE_RE = re.compile(r"Share\s*Date\s*<i[^>]*>([^<]{1,60})</i>", re.IGNORECASE)
_CODE_RE = re.compile(r"[?&]id=([A-Za-z0-9_\-]+)")
#: HTML comments are stripped by _strip_comments() below, not by a regex.
#: The obvious "<!--.*?-->" is quadratic: at every "<!--" it rescans
#: forward for the closing "-->", so a body of N unterminated comments
#: costs O(N^2) (8 000 of them -> 3 s). The parse runs in a worker
#: thread, but a thread cannot be cancelled by asyncio.wait_for, so the
#: fetch budget would NOT have protected the bot.
_CHALLENGE_MARKS = ("just a moment", "checking your browser", "cf-mitigated")

#: Hard cap on the attribute text of one anchor. A malformed page must
#: not be able to make the scan expensive.
#:
#: The scan itself is LINEAR (see _iter_anchors), so this cap no longer
#: bounds the scan cost -- it only stops a pathological attribute blob
#: from being handed to the parsers. It must therefore be large enough
#: for a REAL href: the FSL mirror is a Cloudflare R2 presigned URL
#: carrying an AWS SigV4 query string plus a response-content-disposition
#: filename, which runs to ~640 characters. At the old 500 cap that
#: anchor was silently skipped and the mirror was lost.
_MAX_ATTR_LEN = 4096


# --- dataclasses ------------------------------------------------------------


@dataclass
class HubMirror:
    """One real DDL mirror link."""

    label: str = ""
    url: str = ""
    host: str = ""


@dataclass
class HubPageInfo:
    """Metadata scraped from the hub drive page (step 1)."""

    code: str = ""
    title: str = ""
    size: str = ""
    file_type: str = ""
    share_date: str = ""


@dataclass
class MirrorResult:
    """Outcome of the 2-step chain. `error` is "" on success."""

    info: HubPageInfo = field(default_factory=HubPageInfo)
    mirrors: List[HubMirror] = field(default_factory=list)
    error: str = ""


# --- helpers ----------------------------------------------------------------


def _text(raw: str) -> str:
    """Strip tags, collapse whitespace."""
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", raw or "")).strip()


def _strip_comments(html: str) -> str:
    """Remove HTML comments -- LINEAR for any input.

    str.find scans in C and the cursor only ever moves forward, so the
    whole body is walked once. A comment of ANY length is handled
    correctly (an unterminated one drops the rest of the body).
    """
    if not html or "<!--" not in html:
        return html or ""
    out: List[str] = []
    i = 0
    while True:
        j = html.find("<!--", i)
        if j < 0:
            out.append(html[i:])
            break
        out.append(html[i:j])
        k = html.find("-->", j + 4)
        if k < 0:
            # unterminated comment -- drop the remainder
            break
        i = k + 3
    return " ".join(out)


def _host(url: str) -> str:
    try:
        # rstrip the trailing dot so it matches the guard, which
        # normalises the same way. Without this a FQDN with a trailing
        # dot passes the guard and is then dropped by the extractor.
        return (urlsplit(url or "").hostname or "").lower().rstrip(".")
    except Exception:
        return ""


def _scheme(url: str) -> str:
    try:
        return (urlsplit(url or "").scheme or "").lower()
    except Exception:
        return ""


def _resource(url: str) -> str:
    """path + query -- what the path policy applies to."""
    try:
        parts = urlsplit(url or "")
    except Exception:
        return ""
    path = parts.path or "/"
    return path + ("?" + parts.query if parts.query else "")


def _is_ad_host(host: str) -> bool:
    if not host:
        return False
    return any(host == a or host.endswith("." + a) for a in AD_HOSTS)


def _is_pixeldrain(host: str) -> bool:
    return host in ("pixeldrain.dev", "pixeldrain.com") or host.endswith(
        ".pixeldrain.dev"
    ) or host.endswith(".pixeldrain.com")


def _mirror_base(host: str) -> Optional[str]:
    """The MIRROR_HOSTS entry covering `host` (exact match or real subdomain).

    Mirror families whose subdomain is randomised per deployment (ZipDisk on
    Cloudflare Workers, FSL on Cloudflare R2) are matched by PATTERN and
    return their pattern id ("zipdisk" / "fsl") instead of a hostname.

    A family pattern wins over a concrete hostname: the family's path
    policy is the one that describes the mirror shape. Otherwise adding a
    ZipDisk host to MIRROR_HOSTS would shadow the pattern and silently
    drop every mirror on it.
    """
    if not host:
        return None
    for rx, pid in MIRROR_HOST_PATTERNS:
        try:
            if rx.match(host):
                return pid
        except Exception:
            continue
    for base in MIRROR_HOSTS:
        if host == base:
            return base
    for base in MIRROR_HOSTS:
        if host.endswith("." + base):
            return base
    return None


def _mirror_path_ok(host: str, url: str) -> bool:
    """Mirror-specific path policy (hubcloud.ist only serves /tg/go)."""
    base = _mirror_base(host)
    if base is None:
        return False
    prefixes = MIRROR_PATH_PREFIXES.get(base)
    if not prefixes:
        return False
    res = _resource(url)
    for p in prefixes:
        if res.startswith(p) and len(res) > len(p):
            return True
    return False


def _iter_anchors(body: str):
    """Yield ``(attrs, text)`` for every anchor of `body`.

    A hand written, LINEAR scan. The old single regex
    ``<a\\b([^>]*)>(.*?)(?=</a>|<a\\b)`` was quadratic on a malformed body
    (18 KB -> 0.29 s, 288 KB -> 93 s) and it was called synchronously
    from the async fetch, so it froze the bot's event loop.

    The open tag is read with a quote-aware scanner, so a `>` inside a
    quoted attribute value does not truncate the tag, and the attribute
    text is bounded to ``_MAX_ATTR_LEN`` characters.
    """
    n = len(body or "")
    pos = 0
    while True:
        m = _A_OPEN_RE.search(body, pos)
        if not m:
            return
        i = m.end()
        attrs_end = -1
        while i < n:
            ch = body[i]
            if ch == ">":
                attrs_end = i
                break
            if ch == '"' or ch == "'":
                j = body.find(ch, i + 1)
                if j < 0:
                    break
                i = j + 1
                continue
            i += 1
        if attrs_end < 0:
            # unterminated open tag -> nothing sane left to parse
            return
        attrs = body[m.end():attrs_end]
        content_start = attrs_end + 1
        stop = _A_STOP_RE.search(body, content_start)
        if stop is None:
            if len(attrs) <= _MAX_ATTR_LEN:
                yield attrs, body[content_start:]
            return
        if len(attrs) <= _MAX_ATTR_LEN:
            yield attrs, body[content_start:stop.start()]
        if stop.group(0).lower().startswith("</a"):
            pos = stop.end()
        else:
            # unbalanced anchor: the next <a starts the next block
            pos = stop.start()


def _pxl_urls(html: str) -> Dict[str, str]:
    """Map of JS variable name -> the pixeldrain URL it holds.

    The generator page ships `var pxl = "https://pixeldrain.dev/u/xxxx";`
    and then applies it with `document.getElementById("pxl-1").href = pxl;`.
    """
    out: Dict[str, str] = {}
    for m in _PXL_VAR_RE.finditer(html or ""):
        name = m.group(1)
        url = m.group(2).strip()
        if "pxl" in name.lower() and url:
            out[name] = url
    return out


def _pxl_bindings(html: str) -> Dict[str, str]:
    """Map of element id -> JS variable name that holds its real href."""
    out: Dict[str, str] = {}
    for m in _PXL_BIND_RE.finditer(html or ""):
        out[m.group(1)] = m.group(2)
    return out


def label_for(host: str, anchor_text: str = "") -> str:
    """Short, stable label for a mirror.

    Host-derived wins (stable and unambiguous); the page label is only used
    as a fallback for hosts we do not know.
    """
    h = (host or "").lower()
    base = _mirror_base(h)
    for key in (h, base or ""):
        if key and key in HOST_LABELS:
            return HOST_LABELS[key][:40]
    t = _text(anchor_text)
    low = t.lower()
    if "10gbps" in low:
        return "10Gbps"
    if "pixeldrain" in low or "pixelserver" in low:
        return "pixeldrain"
    if "buzz" in low:
        return "buzz"
    if "telegram" in low:
        return "telegram"
    if "zipdisk" in low:
        return "zipdisk"
    if "fsl server" in low:
        return "fsl"
    if low.startswith("download"):
        t = t[len("download"):].strip(" :[]")
    return (t or base or "mirror")[:40]


# --- pure parsers -----------------------------------------------------------


def extract_generator_url(html: str) -> Optional[str]:
    """The opaque gamerxyt generator URL of a hub drive page.

    HTML comments are stripped first: a commented-out
    ``<script>var url='https://evil.example/gen';</script>`` must never
    win over the real one.
    """
    if not html:
        return None
    body = _strip_comments(html)
    m = _GEN_URL_RE.search(body)
    if m:
        return _unescape(m.group(1).strip())
    m = _DOWNLOAD_ANCHOR_RE.search(body)
    if m:
        return _unescape(m.group(1).strip())
    return None


def extract_hub_info(html: str) -> HubPageInfo:
    """Metadata of a hub drive page: code, title, size, type, share date."""
    info = HubPageInfo()
    if not html:
        return info
    m = _TITLE_RE.search(html)
    if m:
        info.title = _text(m.group(1))
    if not info.title:
        m = _CARD_HEADER_RE.search(html)
        if m:
            info.title = _text(m.group(1))
    m = _SIZE_RE.search(html)
    if m:
        info.size = _text(m.group(1))
    m = _FILE_TYPE_RE.search(html)
    if m:
        info.file_type = _text(m.group(1))
    m = _SHARE_DATE_RE.search(html)
    if m:
        info.share_date = _text(m.group(1))
    gen = extract_generator_url(html)
    if gen:
        m = _CODE_RE.search(gen)
        if m:
            info.code = m.group(1)
    return info


def extract_mirrors(html: str) -> List[HubMirror]:
    """Parse the generator page into the mirror set it renders.

    Only anchors that carry a btn* class, whose href is an https URL,
    whose host passes the mirror allowlist and whose visible text starts
    with "Download" are considered.

    The pixeldrain decoy href is replaced with the real `var pxl` URL
    bound to that anchor's OWN id. An anchor that has an id which is not
    bound is DROPPED -- the document-wide fallback would silently emit
    another file's URL.
    """
    out: List[HubMirror] = []
    if not html:
        return out
    # Commented-out ad blocks contain unbalanced anchors -- drop them so
    # the anchor scan cannot be thrown off.
    body = _strip_comments(html)
    pxl_vars = _pxl_urls(body)
    pxl_bind = _pxl_bindings(body)
    default_pxl = ""
    for name, url in pxl_vars.items():
        if name.lower() == "pxl":
            default_pxl = url
            break
    if not default_pxl and pxl_vars:
        default_pxl = next(iter(pxl_vars.values()))

    seen = set()
    for attrs, raw_text in _iter_anchors(body):
        text = _text(raw_text or "")
        hm = _HREF_RE.search(attrs)
        if not hm:
            continue
        cm = _CLASS_RE.search(attrs)
        if not cm:
            continue
        if not any(c.strip().lower().startswith("btn") for c in cm.group(1).split()):
            continue
        if not text.lower().startswith("download"):
            continue
        href = _unescape((hm.group(1) or "").strip())
        if not href:
            continue
        # Only real https links are mirrors -- javascript:, data:, ftp:
        # and file: must never leave this function.
        if _scheme(href) != "https":
            continue
        host = _host(href)
        if not host or _is_ad_host(host):
            continue
        if _mirror_base(host) is None:
            continue
        if not _mirror_path_ok(host, href):
            continue
        if _is_pixeldrain(host):
            # The href is a constant dead decoy -- the real link only
            # exists in the inline `var pxl` script.
            real = ""
            im = _ID_RE.search(attrs)
            if im:
                # The anchor HAS an id: only the value bound to THAT id
                # may be used. An unbound id -> drop the mirror.
                var = pxl_bind.get(im.group(1))
                if var and pxl_vars.get(var):
                    real = pxl_vars[var]
            else:
                # No id at all -> the document-wide fallback is the only
                # thing that can be right here.
                real = default_pxl
            if not real:
                continue
            href = real
            host = _host(href)
            if not host or _mirror_base(host) is None:
                continue
            if not _mirror_path_ok(host, href):
                continue
        if href == PIXELDRAIN_DECOY:
            continue
        if href in seen:
            continue
        seen.add(href)
        out.append(
            HubMirror(label=label_for(host, _unescape(text)), url=href, host=host)
        )
    return out


# --- network ----------------------------------------------------------------


def _is_challenge(body: str) -> bool:
    low = (body or "").lower()
    return any(m in low for m in _CHALLENGE_MARKS)


async def _guard(
    url: str,
    allowed_hosts=None,
    allowed_path_prefixes=None,
):
    """url_guard.check_url off the event loop.

    Returns (normalized_url, "") when the URL passes, (None, reason) when
    the guard rejects it.
    """
    try:
        safe = await asyncio.to_thread(
            url_guard.check_url,
            url,
            allowed_hosts=allowed_hosts,
            allowed_path_prefixes=allowed_path_prefixes,
        )
        return safe, ""
    except Exception as exc:
        # The guard's reason can name a host the user never pasted (a
        # redirect target) -- no URL may leak into a user-visible text.
        return None, url_guard.redact_foreign_urls(
            url_guard.redact_request_url(str(exc))
        )


async def _get(
    client,
    url: str,
    allowed_hosts=None,
    allowed_path_prefixes=None,
):
    """GET a URL, following redirects manually.

    Every hop is re-validated with url_guard.check_url before it is
    requested. Returns (final_url, body, error).
    """
    current = url
    for _ in range(MAX_HOPS + 1):
        safe, reason = await _guard(current, allowed_hosts, allowed_path_prefixes)
        if safe is None:
            return current, "", f"blocked url: {reason}"
        current = safe
        try:
            resp = await client.get(current)
        except Exception as exc:
            # httpx timeouts have an EMPTY str() -- name the exception
            # type so the operator gets a usable diagnosis. The request
            # URL is stripped: on httpx 0.25-0.27 the str() of a
            # RequestError falls back to the repr, which embeds
            # "request=URL('https://...')" -- that must never reach a
            # user-visible error string.
            return current, "", (
                f"request failed: {type(exc).__name__}: "
                f"{url_guard.redact_request_url(exc)}"
            )
        if resp.is_redirect and resp.headers.get("location"):
            nxt = resp.headers["location"].strip()
            # urljoin resolves absolute, root-relative ("/x") AND
            # relative ("gen") locations against the current URL.
            current = urljoin(current, nxt)
            continue
        body = resp.text or ""
        if resp.status_code == 404 and body.strip() == NOT_FOUND_BODY:
            return current, "", "file not found (404 ! File Not Found)"
        if resp.status_code != 200:
            return current, "", f"http {resp.status_code}"
        if _is_challenge(body):
            return current, "", "cloudflare challenge on hub page"
        return current, body, ""
    return current, "", "too many redirects"


async def _fetch_hub_page(url: str, timeout: float) -> MirrorResult:
    """The actual 2-step chain (see fetch_hub_page)."""
    result = MirrorResult()
    try:
        import httpx
    except Exception as exc:
        result.error = f"httpx unavailable: {url_guard.redact_request_url(exc)}"
        return result

    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            headers=DEFAULT_HEADERS,
        ) as client:
            # STEP 1 -- the hub drive page.
            _, html, err = await _get(
                client, str(url).strip(), allowed_hosts=url_guard.HUB_PAGE_HOSTS
            )
            if err:
                result.error = err
                return result
            # The HTML parsing is CPU bound and can be slow on a huge or
            # malformed body -- keep it OFF the event loop.
            result.info = await asyncio.to_thread(extract_hub_info, html)

            gen = await asyncio.to_thread(extract_generator_url, html)
            if not gen:
                result.error = "no generator url on hub page"
                return result

            # STEP 2 -- the generator page. Fetched ONCE: the mirror set
            # is randomised per request, so it must be used as-is.
            _, gen_html, err = await _get(client, gen, allowed_hosts=url_guard.HUB_PAGE_HOSTS)
            if err:
                result.error = err
                return result
            if GENERATOR_OK_MARKER not in gen_html:
                result.error = "no mirrors generated"
                return result

            parsed = await asyncio.to_thread(extract_mirrors, gen_html)
            kept: List[HubMirror] = []
            seen = set()
            for m in parsed:
                if m.url in seen:
                    continue
                base = _mirror_base(m.host)
                prefixes = MIRROR_PATH_PREFIXES.get(base or "")
                if not prefixes:
                    continue
                try:
                    # Second, independent barrier: the mirror allowlist
                    # with the host's own path policy. check_mirror_url
                    # keeps the exact-host list, the randomised-family
                    # patterns and the path policy together -- passing
                    # them by hand is easy to get wrong, and forgetting
                    # the patterns silently drops every ZipDisk/FSL
                    # mirror.
                    await asyncio.to_thread(
                        url_guard.check_mirror_url, m.url
                    )
                except Exception:
                    continue
                seen.add(m.url)
                kept.append(m)
            result.mirrors = kept
            if not kept:
                result.error = "no mirrors generated"
            return result
    except Exception as exc:
        result.error = (
            f"unexpected error: {type(exc).__name__}: "
            f"{url_guard.redact_request_url(exc)}"
        )
        result.mirrors = []
        return result


async def fetch_hub_page(
    url: str,
    timeout: float = 12.0,
    total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
) -> MirrorResult:
    """Full 2-step chain for ONE hub page url.

    Never raises: on any failure the returned MirrorResult carries the
    reason in `.error` and an empty mirror list.

    `timeout` is the per-request budget, `total_timeout` the overall
    wall-clock budget of the whole chain (a slow chain can never exceed
    it). Pass `total_timeout=None` (or 0) to disable the overall
    deadline.
    """
    result = MirrorResult()
    if not url or not str(url).strip():
        result.error = "empty url"
        return result
    coro = _fetch_hub_page(str(url).strip(), timeout)
    if total_timeout and total_timeout > 0:
        try:
            return await asyncio.wait_for(coro, total_timeout)
        except asyncio.TimeoutError:
            result.error = f"total timeout after {total_timeout:g}s"
            result.mirrors = []
            return result
        except Exception as exc:
            result.error = (
                f"unexpected error: {type(exc).__name__}: "
                f"{url_guard.redact_request_url(exc)}"
            )
            result.mirrors = []
            return result
    return await coro


async def fetch_hub_mirrors(
    url: str,
    timeout: float = 12.0,
    total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
) -> List[HubMirror]:
    """The mirror set of a hub page. Empty list on any failure."""
    try:
        res = await fetch_hub_page(url, timeout=timeout, total_timeout=total_timeout)
    except Exception:
        return []
    return list(res.mirrors or [])

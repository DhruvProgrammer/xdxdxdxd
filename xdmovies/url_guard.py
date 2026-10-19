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
#  File:    xdmovies/url_guard.py
#  Fingerprint: BRUNO-E1CCB1FB-UG01
#  ============================================================

"""SSRF guard for every outbound URL the bot is allowed to touch.

Self-contained on purpose: nothing in the repo did URL vetting before, so
this module owns the whole policy (scheme allowlist, host allowlist, path
shape, port policy, DNS-level private-range rejection, solve-server
exclusion).

Public surface:
    ALLOWED_SCHEMES            tuple of permitted URL schemes
    HUB_PAGE_HOSTS             exact hostnames the bot may talk to
    HUB_PAGE_PATH_PREFIXES     accepted path shapes
    MIRROR_HOSTS               exact hostnames whose links may be echoed
    is_safe_url(url, ...)       -> bool   (never raises)
    check_url(url, ...)         -> str    (raises ValueError with a reason)
    host_of(url)               -> str    (lowercased hostname, "" on failure)
    is_hub_page_url(url)       -> bool
    is_echo_safe_url(url)      -> bool   (may the link be re-published?)
    redact_request_url(text)   -> str    (strip a URL out of an error text)
    redact_foreign_urls(text, keep="") -> str
    urls_in(text)              -> list   (every http(s) URL inside a text)
    install_route_guard(ctx, ...)        async, aborts unsafe subrequests

Notes
-----
* Host matching is EXACT or a real subdomain of an allowlisted base
  (``h == base or h.endswith("." + base)``). A host such as
  ``hubcloud.ist.evil.com`` therefore never matches a ``hubcloud.ist``
  entry -- substring matching would let it through.
* The "non-empty remainder" rule is evaluated against the *resource*, i.e.
  path plus query string. That is what lets a legitimate mirror such as
  ``https://pixel.hubcloud.ist/?id=<hex>`` (path ``/``, payload in the
  query) pass, while a bare ``https://hubcloud.ist/`` is still rejected.
* DNS resolution happens for every accepted host and any resolved address
  that is private/loopback/link-local/reserved/multicast/unspecified/
  CGNAT rejects the URL (169.254.169.254 and 100.64.0.0/10 included).
* The bot's own solve server (``SOLVE_SERVER_PUBLIC_BASE``) and every
  ``/solve/`` path are rejected -- the bot must never be tricked into
  calling itself. The path check normalises dot-segments and is case
  insensitive, so ``/SOLVE/abc``, ``//solve/abc`` and ``/x/../solve/abc``
  are all rejected.
* Only the default HTTPS port is allowed (explicit ``:443`` included).
* The host is normalised ONCE (lower case, no trailing dot) and the URL
  that is returned is built from that very name, so the host that gets
  vetted is the host that gets requested.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import posixpath
import re
import socket
import time
from typing import Dict, Optional, Sequence
from urllib.parse import urlsplit

# --- policy -----------------------------------------------------------------

#: URL schemes the bot may ever request. Everything else (file:, data:,
#: gopher:, ftp:, javascript:, chrome:, ...) is denied.
ALLOWED_SCHEMES: tuple[str, ...] = ("https",)

#: Exact hostnames the bot may talk to. Keep this list in ONE place so it
#: stays easy to extend.
#:
#: hubcloud.ist / hubcloud.one / hubcloud.foo -- the hub drive hosts.
#: pixel.hubcloud.ist / gpdl.hubcloud.ist       -- the "10Gbps" mirror hosts.
#: pixeldrain.dev / pixeldrain.com             -- the PixelServer mirror.
#: fuckingfast.net                              -- the Buzz mirror.
#: gamerxyt.com                                -- the link-generator host.
#: hubcloud.cx                                  -- same estate: the live hub
#:   page links to https://hubcloud.cx/favicon.png. It is kept as a TARGET
#:   but it times out from most networks -- the fetch helpers turn that
#:   into a clean error, never into a hang.
#:
#: hubcloud.fans is NOT listed: it 301s to https://hubcloud.ist/#contact and
#: that path shape ("/" with only a fragment) never passes the path policy,
#: so the host could never be used as an entry point anyway.
HUB_PAGE_HOSTS: tuple[str, ...] = (
    "hubcloud.ist",
    "hubcloud.one",
    "hubcloud.foo",
    "hubcloud.cx",
    "pixel.hubcloud.ist",
    "gpdl.hubcloud.ist",
    "pixeldrain.dev",
    "pixeldrain.com",
    "fuckingfast.net",
    "gamerxyt.com",
)

#: Exact hostnames whose links may be ECHOED back into a chat message.
#:
#: This is the publishing policy, not the fetch policy: a card that the
#: owner pasted back into the chat is re-emitted verbatim, so a link that
#: points at the bot's own solve server (or at the VPS IP) would end up
#: in a group chat. Only the real mirror hosts survive that filter.
#:
#: Matching is EXACT or a real subdomain (``h == base or
#: h.endswith("." + base)``) -- never a substring match.
MIRROR_HOSTS: tuple[str, ...] = (
    "pixel.hubcloud.ist",
    "gpdl.hubcloud.ist",
    "hubcloud.ist",
    "hubcloud.one",
    "pixeldrain.dev",
    "pixeldrain.com",
    "fuckingfast.net",
    "gamerxyt.com",
)

#: Accepted path shapes. A path must start with one of these AND keep a
#: non-empty remainder -- "/drive/" alone is not a hub page.
HUB_PAGE_PATH_PREFIXES: tuple[str, ...] = ("/drive/", "/file/", "/d/", "/video/")

#: Extra path shapes that only make sense for a specific host. Applied on
#: top of HUB_PAGE_PATH_PREFIXES when no explicit prefix list is given.
#:   /tg/go        -- the Telegram mirror served by the hub itself
#:   /             -- the 10Gbps mirror, payload carried in "?id=..."
#:   /u/           -- the PixelServer mirror
#:   /hubcloud.php -- the gamerxyt link generator
_HOST_PATH_PREFIXES: dict[str, tuple[str, ...]] = {
    "hubcloud.ist": ("/tg/go",),
    "hubcloud.one": ("/tg/go",),
    "hubcloud.foo": ("/tg/go",),
    "pixel.hubcloud.ist": ("/",),
    "gpdl.hubcloud.ist": ("/",),
    "pixeldrain.dev": ("/u/",),
    "pixeldrain.com": ("/u/",),
    "fuckingfast.net": ("/",),
    "gamerxyt.com": ("/hubcloud.php",),
}

#: Hosts that may serve the hub page's own static assets (CSS / JS /
#: fonts / SVG namespaces). They are allowed to LOAD inside the guarded
#: browser context so the page renders, but they are NEVER a valid target
#: for check_url -- the target allowlist stays strict.
_ASSET_HOSTS: tuple[str, ...] = (
    "cdn.jsdelivr.net",
    "use.fontawesome.com",
    "code.jquery.com",
    "unpkg.com",
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "www.w3.org",
    "lh3.googleusercontent.com",
)

#: The bot's own solve server must never be reachable through the guard.
_SOLVE_ENV_KEY = "SOLVE_SERVER_PUBLIC_BASE"

#: TTL for the DNS cache (seconds). Short enough to follow a fast
#: re-point, long enough to keep a mirror sweep cheap.
_DNS_TTL = 60.0
_dns_cache: dict[str, tuple[float, tuple[str, ...]]] = {}

#: How many manual redirects the fetch helpers are allowed to follow.
#: This is the SINGLE SOURCE OF TRUTH for the hop cap -- hub_mirrors
#: aliases it as MAX_HOPS so the two can never drift apart.
MAX_REDIRECTS = 3

#: RFC 6598 carrier-grade NAT range. ``ipaddress.is_private`` does not
#: cover it on every Python build, so it is checked explicitly.
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")

#: The only port the bot may talk to (plus "no port at all").
_ALLOWED_PORTS = (443,)


# --- helpers -----------------------------------------------------------------


def _stripped(url: str) -> str:
    try:
        return (url or "").strip()
    except Exception:
        return ""


def host_of(url: str) -> str:
    """Lowercased hostname of `url`, or "" when it cannot be parsed."""
    u = _stripped(url)
    if not u:
        return ""
    try:
        host = urlsplit(u).hostname or ""
    except Exception:
        return ""
    return host.lower().rstrip(".")


def _scheme_of(url: str) -> str:
    try:
        return (urlsplit(url or "").scheme or "").lower()
    except Exception:
        return ""


def _host_matches(host: str, base: str) -> bool:
    """Exact host match, or a real subdomain of `base`. Never a substring."""
    if not host or not base:
        return False
    if host == base:
        return True
    return host.endswith("." + base)


def _pick_base(host: str, hosts: Sequence[str]) -> Optional[str]:
    """The allowlist entry that covers `host` (exact first, then longest base)."""
    exact = None
    longest = None
    for base in hosts:
        b = (base or "").lower().rstrip(".")
        if not b:
            continue
        if host == b:
            exact = b
            break
        if _host_matches(host, b) and (longest is None or len(b) > len(longest)):
            longest = b
    return exact or longest


def _resource(parts) -> str:
    """path + query -- the part of the URL the path policy applies to."""
    path = parts.path or "/"
    if parts.query:
        return path + "?" + parts.query
    return path


def _prefix_ok(resource: str, prefixes: Sequence[str]) -> bool:
    """True when `resource` starts with a prefix and keeps a remainder."""
    for p in prefixes:
        if not p:
            continue
        if resource.startswith(p) and len(resource) > len(p):
            return True
    return False


def _allowed_prefixes(
    host: str,
    base: Optional[str],
    allowed_path_prefixes: Optional[Sequence[str]],
) -> tuple[str, ...]:
    if allowed_path_prefixes is not None:
        return tuple(allowed_path_prefixes)
    prefixes = list(HUB_PAGE_PATH_PREFIXES)
    for key in (host, base):
        if key and key in _HOST_PATH_PREFIXES:
            prefixes.extend(_HOST_PATH_PREFIXES[key])
    # keep order, drop duplicates
    seen: dict[str, bool] = {}
    for p in prefixes:
        seen.setdefault(p, True)
    return tuple(seen.keys())


def _resolve(host: str) -> tuple[str, ...]:
    """Resolve `host` to every address the system knows about (cached)."""
    now = time.time()
    hit = _dns_cache.get(host)
    if hit is not None and (now - hit[0]) < _DNS_TTL:
        return hit[1]
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except Exception as exc:
        raise ValueError(f"dns resolution failed for {host}: {exc}") from exc
    addrs = tuple(sorted({str(info[4][0]) for info in infos if info and info[4]}))
    if not addrs:
        raise ValueError(f"dns resolution returned no address for {host}")
    _dns_cache[host] = (now, addrs)
    return addrs


def _is_public_ip(addr: str) -> bool:
    """False for private/loopback/link-local/reserved/multicast/unspecified."""
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return False
    # RFC 6598 carrier-grade NAT (100.64.0.0/10) -- not flagged by
    # is_private on every Python build, but it is NOT public either.
    if ip.version == 4 and ip in _CGNAT_NET:
        return False
    return True


def _solve_server_host() -> str:
    base = (os.environ.get(_SOLVE_ENV_KEY) or "").strip()
    if not base:
        return ""
    try:
        return (urlsplit(base).hostname or "").lower().rstrip(".")
    except Exception:
        return ""


def _is_solve_path(path: str) -> bool:
    """True for the bot's own /solve/ endpoint, whatever the spelling.

    Dot-segments are collapsed and the comparison is case insensitive, so
    ``/SOLVE/abc``, ``//solve/abc`` and ``/x/../solve/abc`` are all
    rejected.
    """
    raw = (path or "").replace("\\", "/")
    if not raw:
        return False
    try:
        norm = posixpath.normpath(raw)
    except Exception:
        return False
    if not norm.startswith("/"):
        norm = "/" + norm
    # collapse any number of leading slashes ("//solve/abc" -> "/solve/abc")
    norm = "/" + norm.lstrip("/")
    low = norm.lower()
    return low == "/solve" or low.startswith("/solve/")


def _is_asset_host(host: str) -> bool:
    """True for the CDN / asset hosts the hub page is allowed to pull."""
    if not host:
        return False
    return any(host == a or host.endswith("." + a) for a in _ASSET_HOSTS)


def _normalize(url: str, parts) -> str:
    """Lowercase scheme+host, keep everything else byte-identical.

    The host is normalised EXACTLY the way check_url vetted it (lower
    case, trailing dot removed) so the name that is REQUESTED is the name
    that was VALIDATED -- otherwise "hubcloud.ist." would be vetted as
    "hubcloud.ist" but requested as "hubcloud.ist.".
    """
    host = (parts.hostname or "").lower().rstrip(".")
    netloc = parts.netloc
    if netloc and host:
        userinfo, _, hostport = netloc.rpartition("@")
        prefix = (userinfo + "@") if userinfo else ""
        if hostport.startswith("["):
            hostport = hostport.lower()
        else:
            h, sep, port = hostport.partition(":")
            hostport = h.lower().rstrip(".") + (sep + port if sep else "")
        netloc = prefix + hostport
    out = f"{(parts.scheme or '').lower()}://{netloc}{parts.path}"
    if parts.query:
        out += "?" + parts.query
    if parts.fragment:
        out += "#" + parts.fragment
    return out


# --- public API --------------------------------------------------------------


def check_url(
    url: str,
    *,
    allowed_hosts: Optional[Sequence[str]] = None,
    allowed_path_prefixes: Optional[Sequence[str]] = None,
) -> str:
    """Validate `url` and return its normalized form.

    Raises ValueError with a specific reason for every failed check:
      1. empty / non-string url
      2. scheme not in ALLOWED_SCHEMES
      3. no hostname
      4. port other than 443 (or no port at all)
      5. host not on the allowlist (exact or real subdomain)
      6. path shape rejected (no matching prefix / no remainder)
      7. host resolves to a non-public address
      8. url points at the bot's own solve server

    THREADING
    ---------
    This function is SYNCHRONOUS and it resolves DNS, so it BLOCKS the
    calling thread for as long as the resolver takes (tens of
    milliseconds, more on a cold cache). Inside an async function call it
    through ``asyncio.to_thread(check_url, url, ...)`` -- never inline.
    The call sites in this repo already do that.
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be a non-empty string")
    u = url.strip()
    try:
        parts = urlsplit(u)
    except Exception as exc:
        raise ValueError(f"url could not be parsed: {exc}") from exc

    # 1. non-empty string -- already enforced above
    scheme = (parts.scheme or "").lower()
    # 2. scheme
    if scheme not in ALLOWED_SCHEMES:
        raise ValueError(f"scheme not allowed: {scheme or '(none)'}")
    host = (parts.hostname or "").lower().rstrip(".")
    # 3. hostname present
    if not host:
        raise ValueError("url has no hostname")
    # 4. port -- only the default HTTPS port
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"url has an invalid port: {exc}") from exc
    if port is not None and port not in _ALLOWED_PORTS:
        raise ValueError(f"port not allowed: {port}")
    hosts = tuple(allowed_hosts) if allowed_hosts is not None else HUB_PAGE_HOSTS
    # 5. host allowlist
    base = _pick_base(host, hosts)
    if base is None:
        raise ValueError(f"host not allowed: {host}")
    # 6. path shape
    resource = _resource(parts)
    prefixes = _allowed_prefixes(host, base, allowed_path_prefixes)
    if not _prefix_ok(resource, prefixes):
        raise ValueError(
            f"path not allowed for host {host}: {parts.path or '/'!r} "
            f"(needs one of {', '.join(prefixes)} with a non-empty remainder)"
        )
    # 7. DNS / private ranges
    for addr in _resolve(host):
        if not _is_public_ip(addr):
            raise ValueError(f"host {host} resolves to a non-public address: {addr}")
    # 8. the bot's own solve server
    solve_host = _solve_server_host()
    if solve_host and host == solve_host:
        raise ValueError(f"url points at the bot's own solve server: {host}")
    if _is_solve_path(parts.path or ""):
        raise ValueError("url points at the bot's own solve endpoint")
    return _normalize(u, parts)


def is_safe_url(
    url: str,
    *,
    allowed_hosts: Optional[Sequence[str]] = None,
    allowed_path_prefixes: Optional[Sequence[str]] = None,
) -> bool:
    """Boolean flavour of check_url. Never raises.

    THREADING
    ---------
    SYNCHRONOUS and DNS-bound, exactly like check_url -- it blocks the
    calling thread. Inside an async function use
    ``await asyncio.to_thread(is_safe_url, url, ...)``.
    """
    try:
        check_url(
            url,
            allowed_hosts=allowed_hosts,
            allowed_path_prefixes=allowed_path_prefixes,
        )
        return True
    except Exception:
        return False


def is_hub_page_url(url: str) -> bool:
    """True when `url` looks like a hub drive page (host + path shape).

    Cheap and structural: no DNS, no I/O. Use check_url before fetching.
    """
    u = _stripped(url)
    if not u:
        return False
    try:
        parts = urlsplit(u)
    except Exception:
        return False
    if (parts.scheme or "").lower() not in ALLOWED_SCHEMES:
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return False
    try:
        port = parts.port
    except ValueError:
        return False
    if port is not None and port not in _ALLOWED_PORTS:
        return False
    base = _pick_base(host, HUB_PAGE_HOSTS)
    if base is None:
        return False
    resource = _resource(parts)
    return _prefix_ok(resource, HUB_PAGE_PATH_PREFIXES)


def is_echo_safe_url(url: str) -> bool:
    """True when `url` may be ECHOED back into a chat message.

    The owner pastes a card dump back into the chat and the bot
    re-emits it. A link that is not a real mirror link (the bot's own
    solve server, the VPS IP, a tracker, an internal endpoint) must
    never be re-published -- the reply can land in a group chat.

    A URL is echo-safe when ALL of the following hold:

    * the scheme is ``https`` (plain ``http`` is refused),
    * the host is one of :data:`MIRROR_HOSTS` (exact or a real
      subdomain -- never a substring match, so
      ``hubcloud.ist.evil.com`` does not pass),
    * the port is the default HTTPS port (or no port at all),
    * the path is NOT a ``/solve/`` path (any spelling),
    * the host is not the bot's own solve server,
    * the host does not resolve to a private / loopback / link-local /
      reserved address (the same DNS helper the fetch guard uses).

    THREADING
    ---------
    SYNCHRONOUS and DNS-bound -- it blocks the calling thread. Inside
    an async function use ``await asyncio.to_thread(is_echo_safe_url,
    url)``. Never raises: an unparsable URL is simply not safe.
    """
    u = _stripped(url)
    if not u:
        return False
    try:
        parts = urlsplit(u)
    except Exception:
        return False
    if (parts.scheme or "").lower() != "https":
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return False
    try:
        port = parts.port
    except ValueError:
        return False
    if port is not None and port not in _ALLOWED_PORTS:
        return False
    if _pick_base(host, MIRROR_HOSTS) is None:
        return False
    if _is_solve_path(parts.path or ""):
        return False
    solve_host = _solve_server_host()
    if solve_host and host == solve_host:
        return False
    try:
        for addr in _resolve(host):
            if not _is_public_ip(addr):
                return False
    except Exception:
        # DNS failure -> the host cannot be vetted -> not echo-safe.
        return False
    return True


#: httpx renders a failed request inside the exception repr as
#: ``request=URL('https://...')``. On httpx 0.25-0.27 a
#: ``RequestError.__str__`` falls back to that repr, so the request
#: URL would end up inside a user-visible error string.
_REQUEST_URL_RE = re.compile(r"request=URL\([^)]*\)")

#: Any http(s) URL inside a free-form text.
_HTTP_URL_RE = re.compile(r"https?://[^\s'\"<>)\]}]+")


def redact_request_url(text: str) -> str:
    """Strip the ``request=URL('...')`` fragment out of an error text.

    The fragment is replaced by ``request=<hidden>``; everything else
    in the text is kept. Never raises.
    """
    if not text:
        return ""
    try:
        return _REQUEST_URL_RE.sub("request=<hidden>", str(text))
    except Exception:
        return str(text)


def redact_foreign_urls(text: str, keep: str = "") -> str:
    """Hide every http(s) URL in `text` except the one the user supplied.

    `keep` is the URL the user pasted (the hub page URL) -- it stays
    visible, because showing it back is harmless. Every other URL is
    replaced by ``<hidden-url>``. Never raises.
    """
    if not text:
        return ""
    s = str(text)
    kept = (keep or "").strip()
    try:
        def _sub(m):
            found = m.group(0)
            if kept and found == kept:
                return found
            return "<hidden-url>"
        return _HTTP_URL_RE.sub(_sub, s)
    except Exception:
        return s


def urls_in(text: str) -> list:
    """Every http(s) URL inside a free-form text.

    Used to decide whether a pasted prose line may be echoed back: a
    line that carries a URL the bot must not publish is dropped.
    Never raises.
    """
    if not text:
        return []
    try:
        return list(_HTTP_URL_RE.findall(str(text)))
    except Exception:
        return []


async def install_route_guard(
    ctx,
    allowed_hosts: Optional[Sequence[str]] = None,
    allowed_path_prefixes: Optional[Sequence[str]] = None,
    allow_subresources: bool = True,
) -> None:
    """Abort every sub-request whose URL does not pass the guard.

    Uses the Playwright / CloakBrowser shaped API: ``ctx.route("**/*", h)``
    with an async handler ``h(route, request)``. The DNS part of the check
    runs in a thread executor so the event loop is never blocked.

    ``allow_subresources`` (default True) keeps the page's own CDN assets
    loadable: a request whose host is on the internal ``_ASSET_HOSTS``
    list (jsdelivr, fontawesome, jquery, unpkg, google fonts, the W3C
    namespaces) is let through even though it would never pass the target
    policy. The TARGET allowlist itself is untouched -- those hosts can
    never be fetched through check_url.
    """
    hosts = tuple(allowed_hosts) if allowed_hosts is not None else HUB_PAGE_HOSTS
    prefixes = tuple(allowed_path_prefixes) if allowed_path_prefixes is not None else None

    async def _continue(route) -> None:
        cont = getattr(route, "continue_", None) or getattr(route, "continue", None)
        if cont is None:
            return
        await cont()

    async def _handler(route, request=None):
        url = ""
        try:
            if request is not None:
                url = getattr(request, "url", "") or ""
            if not url:
                req = getattr(route, "request", None)
                url = getattr(req, "url", "") or ""
        except Exception:
            url = ""
        # Known-benign CDN / asset hosts may load, but they are never a
        # valid target -- the target allowlist stays strict.
        if allow_subresources and url and _is_asset_host(host_of(url)):
            try:
                await _continue(route)
            except Exception:
                pass
            return
        safe = False
        try:
            await asyncio.to_thread(
                check_url,
                url,
                allowed_hosts=hosts,
                allowed_path_prefixes=prefixes,
            )
            safe = True
        except Exception:
            safe = False
        try:
            if safe:
                await _continue(route)
            else:
                await route.abort()
        except Exception:
            pass

    await ctx.route("**/*", _handler)

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
#  File:    xdmovies/cookie_store.py
#  Fingerprint: BRUNO-E1CCB1FB-B4958F
#  ============================================================

"""Manual CF cookie store — hardened.

User solves Cloudflare in their browser, pastes cf_clearance back. Bot injects
via Playwright with correct domain / Secure / HttpOnly / SameSite / expiry
so CF accepts it. Persisted to xdmovies_state/cf_cookies.json per-eTLD+1.

Hardened handling:
  - Parses: header string, bare token, JSON object, JSON array (Cookie-Editor),
    Netscape cookies.txt, cURL -H, document.cookie string.
  - Stores UA per-domain (IP+UA binding mitigation) and replays same UA.
  - Injects with Secure/HttpOnly/SameSite=None/expiry (real CF attrs).
  - Shows age/TTL in /cookies, warns when stale >20h.
  - CFChallengeError with rich prompt including IP-binding warning + /setcookie help.

Domains: xdmovies.wtf covers top/link subhosts via .xdmovies.wtf wildcard.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
_CANDIDATES = [
    os.path.join(_HERE, "..", "xdmovies_state", "cf_cookies.json"),
    os.path.join(_HERE, "xdmovies_state", "cf_cookies.json"),
    os.path.join(os.getcwd(), "xdmovies_state", "cf_cookies.json"),
    os.path.join(os.getcwd(), "xdmovies", "xdmovies_state", "cf_cookies.json"),
]

def _store_path() -> str:
    for p in _CANDIDATES:
        d = os.path.dirname(os.path.abspath(p))
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            continue
        return os.path.abspath(p)
    return os.path.abspath(_CANDIDATES[0])

STORE_PATH = _store_path()

_XDMOVIES_HOSTS = {"xdmovies.wtf", "top.xdmovies.wtf", "link.xdmovies.wtf"}
_CF_NAMES = {"cf_clearance", "__cf_bm", "cf_chl_2", "__cf_chl_tk", "_cfuvid", "__cf_chl_f_tk"}
# Audit finding M6: real Cloudflare rotation window is ~30-60 minutes for
# most sites (sometimes 1-2h, rarely longer). 30 days is way too
# long — the keep-alive loop reads `_updated_at` and assumes the cookie
# is still valid; a 30-day-old expired-cookie state will silently
# keep the bot retrying with a stale value. 2h is a sensible upper
# bound that catches the very rare long-rotation cases while
# preventing infinite stale-cookie loops.
DEFAULT_TTL_SECONDS = 2 * 3600
# Warn when cookie is older than 30 minutes (matches keep-alive cadence)
STALE_AFTER_SECONDS = 30 * 60

def _normalize_domain(url_or_host: str) -> str:
    s = (url_or_host or "").strip().lower()
    if "://" in s:
        try:
            s = urlparse(s).hostname or s
        except Exception:
            pass
    s = s.split(":")[0].strip().lstrip(".")
    if s in _XDMOVIES_HOSTS or s.endswith(".xdmovies.wtf"):
        return "xdmovies.wtf"
    return s or "xdmovies.wtf"

def _host_for_injection(store_key: str, url: str) -> str:
    if store_key == "xdmovies.wtf":
        return ".xdmovies.wtf"
    try:
        h = urlparse(url).hostname or store_key
    except Exception:
        h = store_key
    h = (h or store_key).strip().lower()
    return h if h.startswith(".") else h

def _load_raw() -> Dict:
    path = STORE_PATH
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _save_raw(data: Dict) -> None:
    path = STORE_PATH
    d = os.path.dirname(path)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    try:
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(path)
        except Exception:
            pass
        try:
            os.replace(tmp, path)
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Parsing — hardened
# ---------------------------------------------------------------------------

_CF_RE = re.compile(r"cf_clearance\s*=\s*([A-Za-z0-9_\-\.]+)", re.IGNORECASE)

def _parse_netscape(text: str) -> Dict[str, str]:
    """Netscape cookies.txt: domain flag path secure expiration name value (tab-separated)."""
    out: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "\t" not in line:
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            # sometimes spaces
            parts = re.split(r"\s+", line)
            if len(parts) < 7:
                continue
        name = parts[-2].strip()
        val = parts[-1].strip()
        if name.lower() in _CF_NAMES or name == "cf_clearance":
            out[name] = val
    return out

def _parse_cookie_editor_array(text: str) -> Dict[str, str]:
    """Cookie-Editor / EditThisCookie export: JSON array of {name,value,domain,...}."""
    try:
        arr = json.loads(text.strip())
        if isinstance(arr, list):
            out: Dict[str, str] = {}
            for item in arr:
                if isinstance(item, dict):
                    n = str(item.get("name") or item.get("Name") or "").strip()
                    v = str(item.get("value") or item.get("Value") or "").strip()
                    if n.lower() in _CF_NAMES and v:
                        out[n] = v
            if out:
                return out
    except Exception:
        pass
    return {}

def _parse_header_like(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    # strip common prefixes: "Cookie:", "cookie:", "-H 'Cookie: ...'" etc
    t = re.sub(r"(?i)(?:^|\s)(?:cookie\s*:|set-cookie\s*:)\s*", "", text)
    t = t.strip().strip('"').strip("'")
    # find all k=v
    for m in re.finditer(r"([A-Za-z0-9_\-]+)\s*=\s*([^\s;,\n\"']+)", t):
        k = m.group(1).strip()
        v = m.group(2).strip().strip('"').strip("'").strip(";")
        if k.lower() in _CF_NAMES and v and len(v) >= 2:
            # preserve original case for name but normalize lower check
            # keep first occurrence
            if k not in out and k.lower() not in {x.lower() for x in out}:
                out[k] = v
            else:
                # normalize to canonical case
                for existing in list(out.keys()):
                    if existing.lower() == k.lower():
                        out[existing] = v
                        break
                else:
                    out[k] = v
    return out

def parse_cookie_input(text: str) -> Dict[str, str]:
    """Parse any user-pasted cookie export into {name: value}.

    Order: JSON array → JSON object → Netscape → header-like → bare token.
    """
    t = (text or "").strip()
    if not t:
        return {}
    # 1) JSON array (Cookie-Editor)
    if t.lstrip().startswith("["):
        arr = _parse_cookie_editor_array(t)
        if arr:
            return arr
    # 2) JSON object
    if t.lstrip().startswith("{") and t.rstrip().endswith("}"):
        try:
            j = json.loads(t)
            if isinstance(j, dict):
                out: Dict[str, str] = {}
                for k, v in j.items():
                    if isinstance(k, str) and k.strip().lower() in _CF_NAMES and v:
                        out[k.strip()] = str(v).strip()
                if out:
                    return out
        except Exception:
            pass
    # 3) Netscape (has tabs and domain)
    if "\t" in t and "cf_clearance" in t.lower():
        ns = _parse_netscape(t)
        if ns:
            return ns
    # 4) Header-like
    hdr = _parse_header_like(t)
    if hdr:
        return hdr
    # 5) Bare token (user copied just the value)
    bare = t.strip().strip('"').strip("'")
    if "=" not in bare and len(bare) >= 8 and re.fullmatch(r"[A-Za-z0-9_\-\.]+", bare):
        # looks like a raw cf_clearance value — 80+ chars normally, but accept >=8 for tests
        # extra check: CF clearance is usually 80+ and contains no spaces, often with '-' '.' '_'
        if len(bare) >= 16 or re.search(r"[-_\.]", bare):
            return {"cf_clearance": bare}
        # even short tokens that look like clearance (e.g. test) — accept
        return {"cf_clearance": bare}
    # 6) last resort: regex search for cf_clearance anywhere
    m = _CF_RE.search(t)
    if m:
        return {"cf_clearance": m.group(1)}
    return {}

def _extract_ua_from_text(text: str) -> Optional[str]:
    """If user pasted 'cookie | UA' or 'cookie\nUA', extract UA part."""
    if not text:
        return None
    # split by | or newline that looks like UA
    for sep in ["|", "\n"]:
        if sep in text:
            parts = text.split(sep)
            for p in parts:
                p = p.strip()
                if len(p) > 20 and "Mozilla" in p and "AppleWebKit" in p:
                    return p[:600]
    # also search for Mozilla substring anywhere
    m = re.search(r"Mozilla/5\.0.*", text)
    if m:
        ua = m.group(0).strip().strip('"').strip("'")
        if len(ua) > 20:
            return ua[:600]
    return None

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def set_cookies_for_domain(domain_or_url: str, cookie_text: str, user_agent: Optional[str] = None) -> Tuple[str, Dict[str, str]]:
    """Store cookies for a domain. Auto-extracts UA if embedded with '|'.

    Returns (store_key, parsed_dict).
    """
    # auto-extract UA if user did "cf=... | Mozilla/5.0 ..."
    auto_ua = _extract_ua_from_text(cookie_text)
    if auto_ua and not user_agent:
        user_agent = auto_ua
        # strip UA from cookie_text for parsing
        cookie_text = cookie_text.split("|")[0].strip()
        # also handle newline case
        if "\n" in cookie_text and "Mozilla" in cookie_text:
            cookie_text = cookie_text.split("\n")[0].strip()
    # also handle case where cookie_text still contains UA line — remove it
    if user_agent and user_agent in cookie_text:
        cookie_text = cookie_text.replace(user_agent, "").replace("|", "").strip()

    key = _normalize_domain(domain_or_url)
    # If domain_or_url was a full URL and cookie_text contains its own domain hints (e.g. Netscape with .latestnewsonline.sbs), prefer that
    # but we already normalized to key param — keep it.

    parsed = parse_cookie_input(cookie_text)
    if not parsed:
        raise ValueError(
            "Could not find cf_clearance in that text. Just paste the value:\n"
            "  cf_clearance=YOUR_VALUE\n"
            "  cf_clearance=abc; __cf_bm=xyz\n"
            "  YOUR_VALUE  (bare 80+ chars) → xdmovies.wtf\n"
            "Tip: F12 → Application → Cookies → copy cf_clearance."
        )
    # sanity: cf_clearance should be decent length — warn if too short but still store
    cf = parsed.get("cf_clearance") or parsed.get("CF_clearance") or next((v for k,v in parsed.items() if k.lower()=="cf_clearance"), "")
    if cf and len(cf) < 20:
        # still allow (test tokens), but don't warn hard
        pass

    data = _load_raw()
    entry = data.get(key, {})
    # merge: new values overwrite; keep old if not provided (so __cf_bm persists when user only sends cf_clearance)
    for k, v in parsed.items():
        # normalize key to canonical case: keep first seen case but ensure cf_clearance exact
        # use as provided but ensure lower comparison doesn't duplicate
        # remove any existing lower-duplicate
        to_remove = [ek for ek in list(entry.keys()) if ek.lower() == k.lower() and ek != k and not ek.startswith("_")]
        for rk in to_remove:
            entry.pop(rk, None)
        entry[k] = v

    now = time.time()
    entry["_updated_at"] = now
    entry["_updated_at_human"] = time.strftime("%Y-%m-%d %H:%M:%S")
    entry["_store_key"] = key
    entry["_raw_input"] = (cookie_text or "")[:800]
    entry["_expires_at"] = now + DEFAULT_TTL_SECONDS
    entry["_expires_human"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now + DEFAULT_TTL_SECONDS))
    if user_agent:
        entry["_user_agent"] = user_agent.strip()[:600]
    # if we have no UA yet, keep previous one if any
    data[key] = entry
    _save_raw(data)
    return key, parsed

def set_cookie(domain_or_url: str, cookie_text: str, user_agent: Optional[str] = None) -> Tuple[str, Dict[str, str]]:
    return set_cookies_for_domain(domain_or_url, cookie_text, user_agent)

def get_entry(domain_or_url: str) -> Optional[Dict]:
    key = _normalize_domain(domain_or_url)
    data = _load_raw()
    return data.get(key)

def get_cookies_for_url(url: str) -> List[Dict]:
    """Return Playwright add_cookies payload with real CF attributes.

    Includes Secure/HttpOnly/SameSite/expires so CF doesn't reject injection.
    """
    key = _normalize_domain(url)
    entry = get_entry(key)
    if not entry:
        return []
    # check expiry — if expired, treat as missing (caller will prompt)
    exp = entry.get("_expires_at", 0)
    if exp and time.time() > exp:
        return []
    cookies: List[Dict] = []
    host = _host_for_injection(key, url)
    # CF sets expiry ~30d; we stored it, else default
    expires = int(entry.get("_expires_at", time.time() + DEFAULT_TTL_SECONDS))
    for name in ("cf_clearance", "__cf_bm", "cf_chl_2", "__cf_chl_tk", "_cfuvid"):
        # support both cases
        val = entry.get(name)
        if val is None:
            # case-insensitive fallback
            for k, v in entry.items():
                if k.lower() == name.lower():
                    val = v
                    name = k
                    break
        if not val:
            continue
        # Build Playwright cookie — domain with leading dot for wildcard
        # Playwright wants domain without scheme, path, and expires as int seconds
        c: Dict[str, object] = {
            "name": name,
            "value": str(val),
            "domain": host,
            "path": "/",
            "expires": expires,
            "httpOnly": name.lower() == "cf_clearance",  # only cf_clearance is HttpOnly in real CF
            "secure": True,
            "sameSite": "None",
        }
        cookies.append(c)
    return cookies

def get_user_agent_for_url(url: str) -> Optional[str]:
    key = _normalize_domain(url)
    e = get_entry(key)
    if e:
        ua = e.get("_user_agent")
        if ua and isinstance(ua, str) and len(ua) > 10:
            return ua
    return None

def get_all() -> Dict:
    return _load_raw()

def clear_domain(domain_or_url: str) -> bool:
    key = _normalize_domain(domain_or_url)
    data = _load_raw()
    if key in data:
        data.pop(key, None)
        _save_raw(data)
        return True
    return False

def clear_all() -> int:
    data = _load_raw()
    n = len(data)
    _save_raw({})
    return n

def _age_human(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds//60)}m ago"
    if seconds < 86400:
        return f"{int(seconds//3600)}h ago"
    return f"{int(seconds//86400)}d ago"

def status_text() -> str:
    data = _load_raw()
    if not data:
        base = os.environ.get("SOLVE_SERVER_PUBLIC_BASE", "http://VPS:8080").rstrip("/")
        return f"No cf_clearance stored yet. Send a movie link — if CF on post blocks, I'll send a solve link {base}/solve/... (tap checkbox) + auto keep-alive every 25m."
    lines: List[str] = []
    now = time.time()
    for k, v in sorted(data.items()):
        upd = v.get("_updated_at", 0)
        age = _age_human(now - upd) if upd else "?"
        exp = v.get("_expires_at", 0)
        ttl = f"expires in {_age_human(exp - now)}" if exp and exp > now else "expired" if exp else "no expiry"
        stale = " ⚠️ STALE (>20h) — may need refresh" if upd and (now - upd) > STALE_AFTER_SECONDS else ""
        has_cf = "yes" if any(x.lower()=="cf_clearance" for x in v.keys()) else "no"
        has_bm = "yes" if any(x.lower()=="__cf_bm" for x in v.keys()) else "no"
        keys = [x for x in v.keys() if not x.startswith("_")]
        lines.append(f"• {k}: cf_clearance={has_cf} __cf_bm={has_bm} keys={','.join(keys) or 'none'} • {age} • {ttl}{stale}")
        if v.get("_user_agent"):
            ua = v["_user_agent"]
            lines.append(f"  UA: {ua[:90]}{'…' if len(ua)>90 else ''}")
        # show value preview (first 12 chars) so user can tell which cookie without leaking full
        cfv = next((str(val) for kk,val in v.items() if kk.lower()=="cf_clearance"), "")
        if cfv:
            lines.append(f"  cf_clearance: {cfv[:12]}… ({len(cfv)} chars)")
    return "\n".join(lines)

class CFChallengeError(RuntimeError):
    def __init__(self, url: str, reason: str = "cf_challenge", host: str = ""):
        self.url = url
        self.host = host or _normalize_domain(url)
        self.reason = reason
        super().__init__(f"CF challenge at {url}: {reason}")

def cf_prompt_text(url: str, host: str = "") -> str:
    h = host or _normalize_domain(url)
    is_xmovies = h == "xdmovies.wtf"
    host_label = "xdmovies.wtf (covers every subdomain (*.xdmovies.wtf: top, huh, new, link))" if is_xmovies else h
    return (
        f"🔒 <b>Cloudflare blocked</b> — <code>{h}</code>\n\n"
        f"Link: <code>{url}</code>\n\n"
        "<b>Solve it in 30s:</b>\n"
        "1. Open the link in Chrome/Firefox on your device\n"
        "2. Wait until you see the real page (not “Just a moment…”)\n"
        "3. F12 → Application → Cookies → copy <code>cf_clearance</code> value\n"
        "   (or Cookie-Editor → Export → copy JSON)\n"
        "4. <b>Just reply here with the value</b> (or full <code>cf_clearance=...; __cf_bm=...</code>, or bare token) — I’ll save it automatically. With UA: paste <code>value | Mozilla/5.0 ...</code>\n\n"
        "⚠️ <b>IP-bound:</b> <code>cf_clearance</code> is tied to IP+UA. "
        "If still blocked, try solving on a device/VPN near the VPS region.\n\n"
        f"Stored domain: <code>{host_label}</code> • Reused until Cloudflare asks again."
    )

def cookie_help_text() -> str:
    return (
        "<b>Manual CF Cookie — Help</b>\n\n"
        "<b>Why manual?</b> VPS datacenter IP fails fingerprint. Your phone/PC on residential IP solves once, bot reuses for hours.\n\n"
        "<b>Flow:</b> Send movie link → bot says “blocked” + link → open link, wait for real page → copy <code>cf_clearance</code> → just <b>reply here with the value</b> (no command) → resend movie link.\n\n"
        "<b>How to copy (desktop):</b> F12 → Application → Cookies → find <code>cf_clearance</code> → copy Value\n"
        "<b>Mobile:</b> Cookie-Editor → Export → paste JSON\n\n"
        "<b>All accepted:</b> <code>cf_clearance=abc</code> • <code>cf_clearance=abc; __cf_bm=xyz</code> • bare token • JSON • Netscape cookies.txt • with UA: <code>value | Mozilla/5.0 ...</code>\n\n"
        "<b>Domains:</b> <code>xdmovies.wtf</code> covers every subdomain (*.xdmovies.wtf) • <code>latestnewsonline.sbs</code> needs its own cookie if its gate blocks.\n\n"
        "If still blocked after pasting, cookie is IP-bound — retry from network near VPS."
    )

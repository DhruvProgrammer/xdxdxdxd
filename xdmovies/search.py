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
#  File:    xdmovies/search.py
#  Fingerprint: BRUNO-E1CCB1FB-B19E70
#  ============================================================

"""Title search against the xdmovies catalog search API.

Endpoint (discovered live 2026-09-07 from js/search.js):
  GET /php/get_token.php                     -> {"token": "..."} (open, no auth)
  GET /php/search_api.php?query=<q>&fuzzy=true  (headers X-Auth-Token + X-Requested-With)
    -> [{"title", "release_year", "poster", "type" ("movie"|"tv"),
         "path" ("/movies/<slug>" | "/series/<slug>"), ...}]

Plain HTTPS works -- no browser, no CF clearance needed (verified live).
Fresh token per call; one retry with a new token on auth-shaped failures.
Never returns browser-dependent data; raises SearchUnavailable on
transport/auth errors so callers can distinguish "service down" from
"no matches" (empty list).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List

SEARCH_BASE = os.environ.get("XDMOVIES_SEARCH_BASE", "https://top.xdmovies.wtf").rstrip("/")
SEARCH_MAX_RESULTS = int(os.environ.get("XDMOVIES_SEARCH_MAX", "8") or "8")


class SearchUnavailable(RuntimeError):
    pass


@dataclass
class SearchResult:
    title: str
    url: str
    year: str = ""
    kind: str = ""  # "movie" | "series"


def _abs_url(path: str) -> str:
    p = (path or "").strip()
    if not p:
        return ""
    if p.startswith("http://") or p.startswith("https://"):
        return p
    if not p.startswith("/"):
        p = "/" + p
    return SEARCH_BASE + p


def parse_search_items(items, limit: int = SEARCH_MAX_RESULTS) -> List[SearchResult]:
    """Pure parser: API JSON list -> SearchResult list. No I/O (testable)."""
    out: List[SearchResult] = []
    try:
        seq = list(items or [])
    except Exception:
        return out
    for it in seq:
        if len(out) >= limit:
            break
        try:
            if not isinstance(it, dict):
                continue
            title = str(it.get("title") or "").strip()
            url = _abs_url(str(it.get("path") or ""))
            if not title or not url:
                continue
            year = str(it.get("release_year") or "").strip()
            raw_kind = str(it.get("type") or "").strip().lower()
            kind = "series" if raw_kind in ("tv", "show", "series") else "movie"
            out.append(SearchResult(title=title, url=url, year=year, kind=kind))
        except Exception:
            continue
    return out


async def _fetch_token(client) -> str:
    resp = await client.get(f"{SEARCH_BASE}/php/get_token.php")
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception as exc:
        raise SearchUnavailable(f"token endpoint returned non-JSON: {exc!r}")
    token = str((data or {}).get("token") or "").strip()
    if not token:
        raise SearchUnavailable("token endpoint returned no token")
    return token


async def _query(client, token: str, query: str):
    resp = await client.get(
        f"{SEARCH_BASE}/php/search_api.php",
        params={"query": query, "fuzzy": "true"},
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/json",
            "X-Auth-Token": token,
        },
    )
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception as exc:
        raise SearchUnavailable(f"search endpoint returned non-JSON: {exc!r}")


async def search_titles(query: str, limit: int = SEARCH_MAX_RESULTS) -> List[SearchResult]:
    """Search the catalog; returns matches (possibly empty). Raises
    SearchUnavailable when the service itself is unreachable/broken."""
    import httpx

    q = (query or "").strip()
    if not q:
        return []
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0"}) as client:
            try:
                token = await _fetch_token(client)
                items = await _query(client, token, q)
            except SearchUnavailable:
                raise
            except Exception as exc:
                raise SearchUnavailable(f"search request failed: {exc!r}")
            # Auth-shaped failure (expired/rotated token): one retry fresh.
            if isinstance(items, dict) and items.get("error"):
                token = await _fetch_token(client)
                items = await _query(client, token, q)
            if isinstance(items, dict) and items.get("error"):
                raise SearchUnavailable(f"search API error: {items.get('error')}")
            return parse_search_items(items, limit=limit)
    except SearchUnavailable:
        raise
    except Exception as exc:
        raise SearchUnavailable(f"search failed: {exc!r}")

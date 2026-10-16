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
#  File:    xdmovies/channel_post.py
#  Fingerprint: BRUNO-E1CCB1FB-FD36B8
#  ============================================================

"""Channel-post title parser — XDMovies channel captions.

Example captions:
    🎞️ Chennai Express (2013)
    🔊 Audios: Hindi
    💿 Source: Netflix, BluRay
    🎬 Qualities: 1080p, 720p
    ✅ Now Available !!!
    🎬 Visit: huh.xdmovies.wtf
    🔖 Bookmark: XDMovies.com

Only the first line carries the title (+ optional year in parens).
Year is stripped (operator choice: ignore year, search title only).
Pure functions, no I/O — safe to unit-test offline.
"""
from __future__ import annotations

import re
from typing import Optional


_YEAR_PAREN_RE = re.compile(r"^(?P<title>.+?)\s*\(\s*(?P<year>19\d{2}|20\d{2})\s*\)\s*$")

# Leading emoji / symbols / variation selectors / ZWSP before the title.
_LEADING_JUNK_RE = re.compile(
    r"^[\s\u200b\u200c\u200d\ufe0f"
    r"\U0001F300-\U0001FAFF"  # misc pictographs, emoticons, etc.
    r"\u2600-\u27BF"  # misc symbols / dingbats
    r"🎞🎬🔊💿📺✅🔖🎥🍿⭐✨❖▪▫\-–—:;|\"'#*]+" ,
)

# Lines that are never a title (footer / meta rows).
_NON_TITLE_RES = (
    re.compile(r"^now\s+available", re.IGNORECASE),
    re.compile(r"^visit\s*:", re.IGNORECASE),
    re.compile(r"^bookmark\s*:", re.IGNORECASE),
    re.compile(r"^audios?\s*:", re.IGNORECASE),
    re.compile(r"^source\s*:", re.IGNORECASE),
    re.compile(r"^qualities?\s*:", re.IGNORECASE),
    re.compile(r"^no\.\s*of\s+seasons?\s*:", re.IGNORECASE),
    re.compile(r"^seasons?\s*:", re.IGNORECASE),
)


def _clean_first_line(line: str) -> str:
    s = (line or "").strip()
    # Strip leading emoji/symbols repeatedly (e.g. "🎞️ " leaves VS16 behind).
    prev = None
    while prev != s:
        prev = s
        s = _LEADING_JUNK_RE.sub("", s).strip()
    # Strip matching surrounding quotes/brackets some forwards add.
    s = s.strip("\"'“”‘’[]").strip()
    return s


def parse_channel_title(text: str) -> Optional[str]:
    """Extract the movie/series title from a channel-post caption.

    Returns the stripped title (year removed) or None when the text
    does not look like a channel post. Never raises.
    """
    try:
        if not text:
            return None
        # First non-empty line is the title row.
        first = ""
        for raw in (text or "").splitlines():
            t = (raw or "").strip()
            if t:
                first = t
                break
        if not first:
            return None
        low = first.lower()
        # Reject pasted URLs / commands outright — those take the URL path.
        if "http://" in low or "https://" in low or "xdmovies.wtf" in low:
            return None
        title = _clean_first_line(first)
        if not title or len(title) < 2 or len(title) > 150:
            return None
        # Year in parens: "Chennai Express (2013)" -> "Chennai Express".
        m = _YEAR_PAREN_RE.match(title)
        if m:
            title = (m.group("title") or "").strip().strip("\"'“”‘’").strip()
        # Bare trailing year without parens is also common in forwards.
        title = re.sub(r"\s+(19\d{2}|20\d{2})\s*$", "", title).strip()
        if not title or len(title) < 2:
            return None
        for rx in _NON_TITLE_RES:
            if rx.search(title):
                return None
        # Must contain at least one letter or digit (reject pure emoji).
        # Numeric-only titles like 1917 / 300 / 2012 are valid.
        if not re.search(r"[A-Za-z0-9\u00C0-\u024F]", title):
            return None
        return title[:120]
    except Exception:
        return None


def _normalize(s: str) -> str:
    try:
        s = (s or "").lower()
        s = re.sub(r"[^a-z0-9]+", " ", s)
        return re.sub(r"\s+", " ", s).strip()
    except Exception:
        return ""


def _strip_trailing_year(norm: str) -> str:
    """Remove a trailing release year from an already-normalized title.

    The search API sometimes folds the year into the title ("Sweetpea
    (2024)" -> "sweetpea 2024") while channel posts carry it separately
    (stripped by parse_channel_title). Comparing both forms keeps the
    exact-match path working either way.
    """
    try:
        return re.sub(r"\s+(19\d{2}|20\d{2})\s*$", "", norm or "").strip()
    except Exception:
        return norm


def find_exact_match(title: str, results) -> Optional[object]:
    """Exact normalized-title match, or None. Never raises."""
    try:
        seq = list(results or [])
        if not seq or not title:
            return None
        want = _normalize(title)
        if not want:
            return None
        for r in seq:
            try:
                cand = _normalize(getattr(r, "title", ""))
                if cand == want or _strip_trailing_year(cand) == want:
                    return r
            except Exception:
                continue
        return None
    except Exception:
        return None


def pick_best_match(title: str, results) -> Optional[object]:
    """Best-match pick from search results (operator: auto, year ignored).

    Exact normalized-title match wins, else the top result (search API
    already ranks by relevance). Returns None on empty input. Never raises.

    NOTE: channel forwards no longer use the top-result fallback — see
    Bot._resolve_channel_title, which shows a picker when there is no
    exact match (blindly taking seq[0] scraped the wrong movie, e.g.
    "Dark" -> "Darkness Falls"). Kept for back-compat only.
    """
    try:
        exact = find_exact_match(title, results)
        if exact is not None:
            return exact
        seq = list(results or [])
        return seq[0] if seq else None
    except Exception:
        return None

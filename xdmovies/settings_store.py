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
#  File:    xdmovies/settings_store.py
#  Fingerprint: BRUNO-E1CCB1FB-A2A54D
#  ============================================================

"""Per-user result-detail preferences for /settings.

`/settings` shows toggle buttons for every detail line a result card can
carry. Each user has their own prefs, persisted in
`xdmovies_state/settings.json` (atomic tmp+fsync+rename, same pattern as
qmode/dmode stores). Missing/corrupt file (or user) falls back to
DEFAULTS, so behaviour is unchanged until someone toggles.

Free, not hardcoded: to add a new detail, append one
`(key, label, emoji)` row to ALL_FIELDS, scrape it into
`Job.page_meta` under the same key, and the render loop
(`_card_detail_lines` in bot.py) picks it up with zero UI changes.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, List, Tuple

STATE_DIR = Path(__file__).resolve().parent.parent / "xdmovies_state"
_SETTINGS_PATH = STATE_DIR / "settings.json"
_SCHEMA = 1

# (key, label, emoji). Order = render order in result cards.
ALL_FIELDS: Tuple[Tuple[str, str, str], ...] = (
    ("size", "Size", "\U0001F4CF"),        # 📏 per-card size badge
    ("rating", "Rating", "⭐"),             # page info block
    ("genres", "Genre", "\U0001F3AD"),     # 🎭 page info block
    ("air_date", "First Air", "\U0001F4C5"),  # 📅 page info block
    ("audios", "Audios", "\U0001F50A"),    # 🔊 page info block
    ("sources", "Sources", "\U0001F4BD"),  # 💽 page info block
    ("tmdb", "TMDB", "\U0001F194"),        # 🆔 numeric id off the page URL
)
_KEYS = tuple(k for k, _l, _e in ALL_FIELDS)
_LABEL = {k: label for k, label, _e in ALL_FIELDS}

# Defaults preserve today's output: title + Audio + Size + link.
DEFAULTS: Dict[str, bool] = {
    "size": True,
    "rating": False,
    "genres": False,
    "air_date": False,
    "audios": True,
    "sources": False,
    "tmdb": False,
}


def _load() -> Dict[int, Dict[str, bool]]:
    """user_id -> {key: bool}. Never raises; anything unreadable is {}."""
    try:
        if not _SETTINGS_PATH.exists():
            return {}
        data = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema_version") != _SCHEMA:
            return {}
        users = data.get("users")
        if not isinstance(users, dict):
            return {}
        out: Dict[int, Dict[str, bool]] = {}
        for k, v in users.items():
            try:
                uid = int(k)
            except (TypeError, ValueError):
                continue
            if isinstance(v, dict):
                out[uid] = {key: bool(v.get(key, DEFAULTS[key])) for key in _KEYS}
        return out
    except Exception:
        return {}


def _save(users: Dict[int, Dict[str, bool]]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": _SCHEMA,
        "users": {str(uid): prefs for uid, prefs in sorted(users.items())},
        "updated_at": time.time(),
    }
    tmp = _SETTINGS_PATH.with_suffix(_SETTINGS_PATH.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, _SETTINGS_PATH)


def get_settings(user_id: int) -> Dict[str, bool]:
    """Full pref map for a user (defaults filled in). Never raises."""
    try:
        prefs = _load().get(int(user_id))
        if not prefs:
            return dict(DEFAULTS)
        return {key: bool(prefs.get(key, DEFAULTS[key])) for key in _KEYS}
    except Exception:
        return dict(DEFAULTS)


def enabled_fields(user_id: int) -> List[str]:
    """Ordered keys switched ON for this user. Never raises."""
    try:
        prefs = get_settings(user_id)
        return [key for key in _KEYS if prefs.get(key)]
    except Exception:
        return [key for key in _KEYS if DEFAULTS[key]]


def set_field(user_id: int, key: str, on: bool) -> Dict[str, bool]:
    """Persist one field. Returns the full map. Never raises."""
    try:
        if key not in _KEYS:
            return get_settings(user_id)
        uid = int(user_id)
        users = _load()
        prefs = users.get(uid, dict(DEFAULTS))
        prefs = {k: bool(prefs.get(k, DEFAULTS[k])) for k in _KEYS}
        prefs[key] = bool(on)
        users[uid] = prefs
        _save(users)
        return prefs
    except Exception:
        return dict(DEFAULTS)


def toggle_field(user_id: int, key: str) -> Dict[str, bool]:
    """Flip one field. Returns the full map. Never raises."""
    try:
        if key not in _KEYS:
            return get_settings(user_id)
        cur = get_settings(user_id)
        return set_field(user_id, key, not cur.get(key, False))
    except Exception:
        return dict(DEFAULTS)


def field_label(key: str) -> str:
    return _LABEL.get(key, key)

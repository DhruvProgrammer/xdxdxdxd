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

from xdmovies.bot_config import atomic_write_json, ensure_private_directory, ensure_private_file, update_json

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
        ensure_private_directory(STATE_DIR)
        if not _SETTINGS_PATH.exists():
            return {}
        ensure_private_file(_SETTINGS_PATH)
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
    ensure_private_directory(STATE_DIR)
    payload = {
        "schema_version": _SCHEMA,
        "users": {str(uid): prefs for uid, prefs in sorted(users.items())},
        "updated_at": time.time(),
    }
    atomic_write_json(_SETTINGS_PATH, payload, sort_keys=True)


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
    """Persist one field. Returns the full map."""
    if key not in _KEYS:
        return get_settings(user_id)
    uid = int(user_id)
    result: Dict[str, bool] = {}

    def apply(data):
        if not isinstance(data, dict) or data.get("schema_version") != _SCHEMA:
            data = {}
        users = data.get("users")
        if not isinstance(users, dict):
            users = {}
        raw = users.get(str(uid), {})
        if not isinstance(raw, dict):
            raw = {}
        prefs = {k: bool(raw.get(k, DEFAULTS[k])) for k in _KEYS}
        prefs[key] = bool(on)
        users[str(uid)] = prefs
        data["schema_version"] = _SCHEMA
        data["users"] = users
        data["updated_at"] = time.time()
        result.update(prefs)
        return data

    update_json(_SETTINGS_PATH, apply, {}, sort_keys=True)
    return result


def toggle_field(user_id: int, key: str) -> Dict[str, bool]:
    """Flip one field. Returns the full map."""
    if key not in _KEYS:
        return get_settings(user_id)
    cur = get_settings(user_id)
    return set_field(user_id, key, not cur.get(key, False))


def field_label(key: str) -> str:
    return _LABEL.get(key, key)

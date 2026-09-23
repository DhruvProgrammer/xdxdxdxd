"""Quality-mode setting: per-user ON/OFF for the series quality picker.

`/qmode` asks "Do you want to enable quality mode?" (Yes/No). Default is
OFF: pasted links resolve exactly as before with no picker. When ON and
the pasted URL is a /series/ page with 2+ quality groups, the bot shows
the toggle picker before resolving.

Persisted in xdmovies_state/qmode.json with atomic tmp+fsync+rename
writes (same pattern as scrape_all channel checkpoints), so the choice
survives restarts. Missing/corrupt file means OFF for everyone.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict

from xdmovies.bot_config import atomic_write_json, ensure_private_directory, ensure_private_file, update_json

STATE_DIR = Path(__file__).resolve().parent.parent / "xdmovies_state"
_QMODE_PATH = STATE_DIR / "qmode.json"
_SCHEMA = 1


def _load() -> Dict[int, bool]:
    """user_id -> True (ON). Never raises; anything unreadable is OFF."""
    try:
        ensure_private_directory(STATE_DIR)
        if not _QMODE_PATH.exists():
            return {}
        ensure_private_file(_QMODE_PATH)
        data = json.loads(_QMODE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema_version") != _SCHEMA:
            return {}
        users = data.get("users")
        if not isinstance(users, dict):
            return {}
        out: Dict[int, bool] = {}
        for k, v in users.items():
            try:
                uid = int(k)
            except (TypeError, ValueError):
                continue
            if v is True:
                out[uid] = True
        return out
    except Exception:
        return {}


def _save(users: Dict[int, bool]) -> None:
    ensure_private_directory(STATE_DIR)
    payload = {
        "schema_version": _SCHEMA,
        "users": {str(uid): True for uid in sorted(users)},
        "updated_at": time.time(),
    }
    atomic_write_json(_QMODE_PATH, payload, sort_keys=True)


def is_qmode_on(user_id: int) -> bool:
    """True when this user enabled quality mode. Default OFF."""
    try:
        return bool(_load().get(int(user_id), False))
    except Exception:
        return False


def set_qmode(user_id: int, on: bool) -> bool:
    """Persist the choice. Returns the stored value."""
    uid = int(user_id)

    def apply(data):
        if not isinstance(data, dict) or data.get("schema_version") != _SCHEMA:
            data = {}
        users = data.get("users")
        if not isinstance(users, dict):
            users = {}
        if on:
            users[str(uid)] = True
        else:
            users.pop(str(uid), None)
        data["schema_version"] = _SCHEMA
        data["users"] = {str(key): True for key, value in users.items() if value is True}
        data["updated_at"] = time.time()
        return data

    update_json(_QMODE_PATH, apply, {}, sort_keys=True)
    return bool(on)

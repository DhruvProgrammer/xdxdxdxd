"""Download-mode setting: per-user ON/OFF for the series section picker.

`/dmode` asks "Do you want to enable download mode?" (Yes/No). Default is
OFF: pasted links resolve exactly as before with no picker. When ON and
the pasted URL is a /series/ page with 2+ section groups (Season N
Packs/Zips vs Season N Episodes), the bot shows the toggle picker before
resolving (and before the /qmode quality picker when both are ON).

Persisted in xdmovies_state/dmode.json with atomic tmp+fsync+rename
writes (same pattern as qmode_store / scrape_all channel checkpoints),
so the choice survives restarts. Missing/corrupt file means OFF for
everyone. Fully independent from qmode_store (separate file, no shared
state) so the two modes can never corrupt each other.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict

STATE_DIR = Path(__file__).resolve().parent.parent / "xdmovies_state"
_DMODE_PATH = STATE_DIR / "dmode.json"
_SCHEMA = 1


def _load() -> Dict[int, bool]:
    """user_id -> True (ON). Never raises; anything unreadable is OFF."""
    try:
        if not _DMODE_PATH.exists():
            return {}
        data = json.loads(_DMODE_PATH.read_text(encoding="utf-8"))
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
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": _SCHEMA,
        "users": {str(uid): True for uid in sorted(users)},
        "updated_at": time.time(),
    }
    tmp = _DMODE_PATH.with_suffix(_DMODE_PATH.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, _DMODE_PATH)


def is_dmode_on(user_id: int) -> bool:
    """True when this user enabled download mode. Default OFF."""
    try:
        return bool(_load().get(int(user_id), False))
    except Exception:
        return False


def set_dmode(user_id: int, on: bool) -> bool:
    """Persist the choice. Returns the stored value. Never raises."""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    try:
        users = _load()
        if on:
            users[uid] = True
        else:
            users.pop(uid, None)
        _save(users)
        return bool(on)
    except Exception:
        return False

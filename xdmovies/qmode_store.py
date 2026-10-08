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
#  File:    xdmovies/qmode_store.py
#  Fingerprint: BRUNO-E1CCB1FB-463D77
#  ============================================================

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

STATE_DIR = Path(__file__).resolve().parent.parent / "xdmovies_state"
_QMODE_PATH = STATE_DIR / "qmode.json"
_SCHEMA = 1


def _load() -> Dict[int, bool]:
    """user_id -> True (ON). Never raises; anything unreadable is OFF."""
    try:
        if not _QMODE_PATH.exists():
            return {}
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
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": _SCHEMA,
        "users": {str(uid): True for uid in sorted(users)},
        "updated_at": time.time(),
    }
    tmp = _QMODE_PATH.with_suffix(_QMODE_PATH.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, _QMODE_PATH)


def is_qmode_on(user_id: int) -> bool:
    """True when this user enabled quality mode. Default OFF."""
    try:
        return bool(_load().get(int(user_id), False))
    except Exception:
        return False


def set_qmode(user_id: int, on: bool) -> bool:
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

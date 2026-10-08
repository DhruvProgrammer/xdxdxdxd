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
#  File:    xdmovies/shutdown_store.py
#  Fingerprint: BRUNO-E1CCB1FB-167ED5
#  ============================================================

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Optional


_STATE_DIR = Path(__file__).resolve().parent.parent / "xdmovies_state"
_STATE_PATH = _STATE_DIR / "shutdown.json"


def _write(payload: dict) -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(_STATE_DIR, 0o700)
    fd, tmp = tempfile.mkstemp(
        prefix=f".{_STATE_PATH.name}.",
        suffix=".tmp",
        dir=str(_STATE_DIR),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "posix":
            os.chmod(tmp, 0o600)
        os.replace(tmp, _STATE_PATH)
    finally:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass


def is_stopped() -> bool:
    try:
        if not _STATE_PATH.exists():
            return False
        if os.name == "posix":
            os.chmod(_STATE_PATH, 0o600)
        data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        return isinstance(data, dict) and bool(data.get("stopped"))
    except Exception:
        return False


def stopped_at() -> Optional[float]:
    try:
        if not _STATE_PATH.exists():
            return None
        data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        value = float((data or {}).get("stopped_at", 0) or 0)
        return value or None
    except Exception:
        return None


def set_stopped(owner_id: int) -> float:
    timestamp = time.time()
    _write({"stopped": True, "stopped_at": timestamp, "owner_id": int(owner_id)})
    return timestamp


def clear_stopped() -> None:
    try:
        _STATE_PATH.unlink()
    except FileNotFoundError:
        pass

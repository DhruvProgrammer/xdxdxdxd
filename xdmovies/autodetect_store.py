"""Auto-detect state for /autodetect: ON/OFF + watched feed + seen URLs.

Single global toggle (owner-only), persisted in
`xdmovies_state/autodetect.json` with atomic tmp+fsync+rename writes
(same pattern as qmode/dmode/settings stores), so the choice survives
restarts. Missing/corrupt file means OFF + feed=all for everyone.

`feed` is one of {"all", "movies", "series"} -- the catalog page the
30-minute loop polls. `seen_urls` holds canonical detail URLs already
enqueued/posted so the loop never re-scrapes the same post.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, List

STATE_DIR = Path(__file__).resolve().parent.parent / "xdmovies_state"
_STATE_PATH = STATE_DIR / "autodetect.json"
_SCHEMA = 1
_VALID_FEEDS = ("all", "movies", "series")
_VALID_CONTENT = ("all", "episodes", "packs")
_SEEN_CAP = 500


def _load() -> Dict:
    """Whole state dict. Never raises; anything unreadable -> defaults."""
    defaults: Dict = {
        "enabled": False,
        "feed": "all",
        "card_filter": "all",
        "seen_urls": [],
        "last_check_ts": 0,
        "updated_at": 0,
    }
    try:
        if not _STATE_PATH.exists():
            return defaults
        data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema_version") != _SCHEMA:
            return defaults
        feed = data.get("feed")
        if feed not in _VALID_FEEDS:
            feed = "all"
        cf = data.get("card_filter")
        if cf not in _VALID_CONTENT:
            cf = "all"
        seen = data.get("seen_urls")
        if not isinstance(seen, list):
            seen = []
        return {
            "enabled": bool(data.get("enabled", False)),
            "feed": feed,
            "card_filter": cf,
            "seen_urls": [u for u in seen if isinstance(u, str)],
            "last_check_ts": _safe_float(data.get("last_check_ts")),
            "updated_at": _safe_float(data.get("updated_at")),
        }
    except Exception:
        return defaults


def _safe_float(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _save(state: Dict) -> None:
    """Atomic persist. Never raises (caller already has the intent)."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": _SCHEMA,
            "enabled": bool(state.get("enabled", False)),
            "feed": state.get("feed") if state.get("feed") in _VALID_FEEDS else "all",
            "card_filter": (
                state.get("card_filter")
                if state.get("card_filter") in _VALID_CONTENT else "all"
            ),
            "seen_urls": list(state.get("seen_urls", []))[-_SEEN_CAP:],
            "last_check_ts": _safe_float(state.get("last_check_ts")),
            "updated_at": time.time(),
        }
        tmp = _STATE_PATH.with_suffix(_STATE_PATH.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, _STATE_PATH)
    except Exception:
        pass


# ---- public API ----------------------------------------------------------

def is_on() -> bool:
    """True when auto-detect is enabled. Default OFF."""
    try:
        return bool(_load().get("enabled", False))
    except Exception:
        return False


def set_on(on: bool) -> bool:
    """Persist the ON/OFF choice. Returns stored value. Never raises."""
    try:
        st = _load()
        st["enabled"] = bool(on)
        _save(st)
        return bool(on)
    except Exception:
        return False


def get_feed() -> str:
    """Active feed: "all" | "movies" | "series". Default "all"."""
    try:
        feed = _load().get("feed", "all")
        return feed if feed in _VALID_FEEDS else "all"
    except Exception:
        return "all"


def set_feed(feed: str) -> str:
    """Persist the chosen feed (replaces previous). Returns stored value."""
    try:
        st = _load()
        st["feed"] = feed if feed in _VALID_FEEDS else "all"
        _save(st)
        return st["feed"]
    except Exception:
        return "all"


def get_card_filter() -> str:
    """Series content filter: "all" | "episodes" | "packs". Default "all"."""
    try:
        cf = _load().get("card_filter", "all")
        return cf if cf in _VALID_CONTENT else "all"
    except Exception:
        return "all"


def set_card_filter(card_filter: str) -> str:
    """Persist the series content filter. Returns stored value."""
    try:
        st = _load()
        st["card_filter"] = card_filter if card_filter in _VALID_CONTENT else "all"
        _save(st)
        return st["card_filter"]
    except Exception:
        return "all"


def load_seen() -> List[str]:
    """Canonical detail URLs already handled. Never raises."""
    try:
        seen = _load().get("seen_urls", [])
        return [u for u in seen if isinstance(u, str)]
    except Exception:
        return []


def add_seen(urls: List[str]) -> None:
    """Append URLs to the seen set (dedup, cap, atomic). Never raises."""
    try:
        st = _load()
        cur = st.get("seen_urls", [])
        for u in urls:
            if isinstance(u, str) and u and u not in cur:
                cur.append(u)
        st["seen_urls"] = cur[-_SEEN_CAP:]
        _save(st)
    except Exception:
        pass


def touch_check(new_count: int = 0) -> None:
    """Record a poll tick + how many new URLs it produced. Never raises."""
    try:
        st = _load()
        st["last_check_ts"] = time.time()
        _save(st)
    except Exception:
        pass


def status_text() -> str:
    """Human summary for /status. Never raises."""
    try:
        st = _load()
        state = "ON" if st.get("enabled") else "OFF"
        feed = st.get("feed", "all")
        cf = st.get("card_filter", "all")
        last = _safe_float(st.get("last_check_ts"))
        seen_n = len(st.get("seen_urls", []))
        if last:
            age_m = int((time.time() - last) // 60)
            when = f"{age_m}m ago"
        else:
            when = "never"
        extra = f" content={cf}" if feed == "series" else ""
        return f"{state} feed={feed}{extra} seen={seen_n} last_check={when}"
    except Exception:
        return "unknown"

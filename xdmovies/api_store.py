"""Durable job registry for the WordPress bridge API.

The bridge hands out a `job_id` immediately and polls for the result later,
so job state cannot live in process memory: a bot restart mid-scrape would
lose the result the plugin is waiting for. SQLite (stdlib, WAL mode) gives
concurrent HTTP-handler reads alongside dispatcher writes without lock
contention.

Status machine:
    queued -> running -> done
                     -> failed
                     -> needs_human -> queued (after a successful solve)
    queued|running -> cancelled

`needs_human` is a parked state: the dispatcher slot is already released, a
background waiter owns the solve session, and the job re-enters `queued` when
the cookie lands.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

STATE_DIR = Path(__file__).resolve().parent.parent / "xdmovies_state"
DB_PATH = STATE_DIR / "api_jobs.db"

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_NEEDS_HUMAN = "needs_human"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

TERMINAL_STATUSES = (STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED)
ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING, STATUS_NEEDS_HUMAN)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_jobs (
    job_id      TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    status      TEXT NOT NULL,
    card_filter TEXT NOT NULL DEFAULT 'all',
    created_at  REAL NOT NULL,
    started_at  REAL,
    finished_at REAL,
    updated_at  REAL NOT NULL,
    error       TEXT,
    solve_url   TEXT,
    solve_token TEXT,
    cancel_flag INTEGER NOT NULL DEFAULT 0,
    result_json TEXT,
    request_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_jobs_url    ON api_jobs(url, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_api_jobs_status ON api_jobs(status, created_at DESC);
"""


def _connect() -> sqlite3.Connection:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db() -> None:
    conn = _connect()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def new_job_id() -> str:
    return uuid.uuid4().hex[:12]


def create_job(
    url: str,
    card_filter: str = "all",
    request: Optional[Dict[str, Any]] = None,
    job_id: Optional[str] = None,
) -> Dict[str, Any]:
    jid = job_id or new_job_id()
    now = time.time()
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO api_jobs (job_id, url, status, card_filter, created_at, updated_at, request_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (jid, url, STATUS_QUEUED, card_filter, now, now,
             json.dumps(request or {}, ensure_ascii=True)),
        )
        conn.commit()
    finally:
        conn.close()
    return get_job(jid) or {}


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    out = dict(row)
    raw = out.pop("result_json", None)
    out["result"] = None
    if raw:
        try:
            out["result"] = json.loads(raw)
        except Exception:
            out["result"] = None
    out["cancel_requested"] = bool(out.pop("cancel_flag", 0))
    return out


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    if not job_id:
        return None
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM api_jobs WHERE job_id = ?", (job_id,)).fetchone()
    finally:
        conn.close()
    return _row_to_dict(row)


def update_job(job_id: str, **fields: Any) -> bool:
    if not job_id or not fields:
        return False
    allowed = {
        "status", "error", "solve_url", "solve_token", "result",
        "started_at", "finished_at", "card_filter", "cancel_flag", "url",
    }
    sets: List[str] = []
    args: List[Any] = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "result":
            sets.append("result_json = ?")
            args.append(json.dumps(value, ensure_ascii=True, default=str) if value is not None else None)
        else:
            sets.append(f"{key} = ?")
            args.append(value)
    if not sets:
        return False
    sets.append("updated_at = ?")
    args.append(time.time())
    args.append(job_id)
    conn = _connect()
    try:
        cur = conn.execute(f"UPDATE api_jobs SET {', '.join(sets)} WHERE job_id = ?", args)
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def set_status(job_id: str, status: str, error: Optional[str] = None) -> bool:
    fields: Dict[str, Any] = {"status": status}
    if error is not None:
        fields["error"] = error
    if status in TERMINAL_STATUSES:
        fields["finished_at"] = time.time()
    if status in (STATUS_QUEUED, STATUS_RUNNING):
        fields["finished_at"] = None
        fields["error"] = None
    return update_job(job_id, **fields)


def save_result(job_id: str, result: Dict[str, Any], status: str = STATUS_DONE,
                error: Optional[str] = None) -> bool:
    fields: Dict[str, Any] = {
        "result": result,
        "status": status,
        "finished_at": time.time(),
    }
    if error is not None:
        fields["error"] = error
    return update_job(job_id, **fields)


def request_cancel(job_id: str) -> bool:
    return update_job(job_id, cancel_flag=1)


def cancel_requested(job_id: str) -> bool:
    row = get_job(job_id)
    return bool(row and row.get("cancel_requested"))


def find_recent_completed(url: str, within_seconds: float) -> Optional[Dict[str, Any]]:
    """Most recent terminal job for the same URL inside the dedup window."""
    try:
        window = max(0.0, float(within_seconds))
    except (TypeError, ValueError):
        window = 0.0
    if window <= 0:
        return None
    cutoff = time.time() - window
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM api_jobs WHERE url = ? AND finished_at IS NOT NULL"
            " AND finished_at >= ? ORDER BY finished_at DESC LIMIT 1",
            (url, cutoff),
        ).fetchone()
    finally:
        conn.close()
    return _row_to_dict(row)


def list_jobs(status: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    try:
        cap = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        cap = 50
    conn = _connect()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM api_jobs WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, cap),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM api_jobs ORDER BY created_at DESC LIMIT ?", (cap,)
            ).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in rows if r is not None]


def queue_depths() -> Dict[str, int]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM api_jobs GROUP BY status", ()
        ).fetchall()
    finally:
        conn.close()
    counts = {str(r["status"]): int(r["n"]) for r in rows}
    return {
        "queued": counts.get(STATUS_QUEUED, 0),
        "running": counts.get(STATUS_RUNNING, 0),
        "needs_human": counts.get(STATUS_NEEDS_HUMAN, 0),
        "done": counts.get(STATUS_DONE, 0),
        "failed": counts.get(STATUS_FAILED, 0),
        "cancelled": counts.get(STATUS_CANCELLED, 0),
    }


def requeue_orphans() -> int:
    """After a restart, jobs left `running` can never resume -- their worker
    died with the process. Move them to `failed` so the plugin sees a
    terminal state instead of polling `running` forever."""
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE api_jobs SET status = ?, error = ?, finished_at = ?, updated_at = ?"
            " WHERE status = ?",
            (STATUS_FAILED, "bot restarted while job was running", time.time(),
             time.time(), STATUS_RUNNING),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def purge(older_than_seconds: float = 7 * 86400) -> int:
    try:
        keep = max(3600.0, float(older_than_seconds))
    except (TypeError, ValueError):
        keep = 7 * 86400.0
    cutoff = time.time() - keep
    conn = _connect()
    try:
        cur = conn.execute(
            "DELETE FROM api_jobs WHERE finished_at IS NOT NULL AND finished_at < ?",
            (cutoff,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()

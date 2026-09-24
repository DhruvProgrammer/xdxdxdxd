"""HTTP bridge API for the WordPress plugin.

Design notes
------------
* **Separate aiohttp app on its own port** (default 8090) rather than extra
  routes on solve_server. The 3-stage Cloudflare flow is hand-tuned and
  AGENTS.md marks those files "do not touch without re-validating"; keeping
  this surface separate means a bug here cannot take the solve flow down.

* **The caller never blocks.** `POST /api/v1/jobs` returns 202 with a
  `job_id` immediately. A series can take 20+ minutes to resolve and
  WordPress/PHP time out well before that, so the plugin polls
  `GET /api/v1/jobs/{id}` instead.

* **URL allowlist.** Only `xdmovies.wtf` / `top.xdmovies.wtf` detail pages
  are accepted. Without this the endpoint is an open proxy that will drive
  our expensive headed browser at arbitrary attacker-chosen sites.

* **Hard cap on cards** so one request cannot occupy the queue for hours.

Auth is a static `X-Api-Key` header compared with `hmac.compare_digest`.
"""
from __future__ import annotations

import hmac
import json
import os
import time
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

try:
    from aiohttp import web
except ImportError:  # pragma: no cover
    web = None  # type: ignore

from xdmovies import api_store
from xdmovies.logger_utils import logger

API_PREFIX = "/api/v1"

_ALLOWED_HOSTS = {"xdmovies.wtf", "top.xdmovies.wtf"}
_ALLOWED_PATH_HEADS = ("/movies/", "/series/")

_APP: Optional[Any] = None
_RUNNER: Optional[Any] = None
_SITE: Optional[Any] = None
_BOT: Optional[Any] = None

API_HOST = os.environ.get("XDMOVIES_API_HOST", "0.0.0.0").strip() or "0.0.0.0"
try:
    API_PORT = int(os.environ.get("XDMOVIES_API_PORT", "8090").strip() or "8090")
except ValueError:
    API_PORT = 8090
API_KEY = (os.environ.get("XDMOVIES_API_KEY", "") or "").strip()
try:
    API_MAX_CARDS = int(os.environ.get("XDMOVIES_API_MAX_CARDS", "200").strip() or "200")
except ValueError:
    API_MAX_CARDS = 200
try:
    API_DEDUP_SECONDS = float(
        os.environ.get("XDMOVIES_API_DEDUP_SECONDS", "86400").strip() or "86400"
    )
except ValueError:
    API_DEDUP_SECONDS = 86400.0
API_RATE_LIMIT = int(os.environ.get("XDMOVIES_API_RATE_LIMIT", "60").strip() or "60")
API_RATE_WINDOW = 60.0

_hits: Dict[str, list] = {}


def is_enabled() -> bool:
    return bool(API_KEY)


def reload_config() -> None:
    """Re-read settings from the environment.

    Module-level constants are captured at import time, but
    `bot_config.load_config()` populates `os.environ` from .env and runs
    AFTER some imports have already happened. Re-reading here makes the
    effective config correct regardless of import order.
    """
    global API_HOST, API_PORT, API_KEY, API_MAX_CARDS, API_DEDUP_SECONDS
    global API_RATE_LIMIT
    API_HOST = os.environ.get("XDMOVIES_API_HOST", "0.0.0.0").strip() or "0.0.0.0"
    try:
        API_PORT = int(os.environ.get("XDMOVIES_API_PORT", "8090").strip() or "8090")
    except ValueError:
        API_PORT = 8090
    API_KEY = (os.environ.get("XDMOVIES_API_KEY", "") or "").strip()
    try:
        API_MAX_CARDS = int(os.environ.get("XDMOVIES_API_MAX_CARDS", "200").strip() or "200")
    except ValueError:
        API_MAX_CARDS = 200
    try:
        API_DEDUP_SECONDS = float(
            os.environ.get("XDMOVIES_API_DEDUP_SECONDS", "86400").strip() or "86400"
        )
    except ValueError:
        API_DEDUP_SECONDS = 86400.0
    try:
        API_RATE_LIMIT = int(os.environ.get("XDMOVIES_API_RATE_LIMIT", "60").strip() or "60")
    except ValueError:
        API_RATE_LIMIT = 60


def canonicalize(raw: str) -> str:
    """Return a canonical xdmovies detail URL, or '' when not allowed.

    Host must be xdmovies.wtf / top.xdmovies.wtf and the path must be a
    single-segment /movies/<slug> or /series/<slug> detail page. Query and
    fragment are dropped so dedup keys are stable.
    """
    try:
        value = (raw or "").strip()
        if not value or len(value) > 600:
            return ""
        if value.startswith("//"):
            value = "https:" + value
        if "://" not in value:
            value = "https://" + value
        parsed = urlsplit(value)
        if parsed.scheme.lower() != "https":
            return ""
        try:
            port = parsed.port
        except ValueError:
            return ""
        if port is not None and port != 443:
            return ""  # non-standard port: not the public site
        host = (parsed.hostname or "").lower().rstrip(".")
        if host not in _ALLOWED_HOSTS:
            return ""
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) != 2:
            return ""
        if parts[0].lower() not in ("movies", "series"):
            return ""
        if not parts[1]:
            return ""
        return f"https://top.xdmovies.wtf/{parts[0].lower()}/{parts[1]}"
    except (TypeError, ValueError):
        return ""


def _rate_ok(key: str) -> bool:
    now = time.time()
    window = _hits.setdefault(key, [])
    cutoff = now - API_RATE_WINDOW
    while window and window[0] < cutoff:
        window.pop(0)
    if len(window) >= max(1, API_RATE_LIMIT):
        return False
    window.append(now)
    return True


def _auth_ok(request) -> bool:
    if not API_KEY:
        return False
    supplied = request.headers.get("X-Api-Key", "") or ""
    return hmac.compare_digest(supplied, API_KEY)


def _public_view(row: Dict[str, Any]) -> Dict[str, Any]:
    now = time.time()
    started = row.get("started_at")
    finished = row.get("finished_at")
    if started and finished:
        elapsed = round(float(finished) - float(started), 1)
    elif started:
        elapsed = round(now - float(started), 1)
    else:
        elapsed = 0.0
    return {
        "job_id": row.get("job_id"),
        "status": row.get("status"),
        "source_url": row.get("url"),
        "created_at": row.get("created_at"),
        "started_at": started,
        "finished_at": finished,
        "elapsed_s": elapsed,
        "solve_url": row.get("solve_url"),
        "error": row.get("error"),
        "cancel_requested": bool(row.get("cancel_requested")),
        "result": row.get("result"),
    }


def _json(payload: Dict[str, Any], status: int = 200):
    return web.json_response(payload, status=status, dumps=lambda o: json.dumps(o, default=str))


async def _auth_middleware(request, handler):
    if request.path == f"{API_PREFIX}/health":
        return await handler(request)
    if not is_enabled():
        return _json({"error": "api_disabled"}, status=503)
    if not _auth_ok(request):
        return _json({"error": "unauthorized"}, status=401)
    if not _rate_ok("global"):
        return _json({"error": "rate_limited"}, status=429)
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception as exc:
        logger.exception("api_server: unhandled error on %s", request.path)
        return _json({"error": f"{exc.__class__.__name__}: {exc}"}, status=500)


if web is not None:
    _auth_middleware = web.middleware(_auth_middleware)


async def handle_health(request):
    return _json({"ok": True, "service": "xdmovies-api", "version": 1})


async def handle_create_job(request):
    if _BOT is None:
        return _json({"error": "bot_unavailable"}, status=503)
    try:
        body = await request.json()
    except Exception:
        return _json({"error": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "invalid_body"}, status=400)

    raw_url = body.get("url") or body.get("link") or ""
    url = canonicalize(str(raw_url))
    if not url:
        return _json(
            {"error": "invalid_url",
             "detail": "expected an https://top.xdmovies.wtf/movies/<slug> or /series/<slug> page"},
            status=400,
        )

    card_filter = str(body.get("card_filter") or "all").strip().lower()
    if card_filter not in ("all", "episodes", "packs"):
        card_filter = "all"

    force = bool(body.get("force"))

    if not force and API_DEDUP_SECONDS > 0:
        prior = api_store.find_recent_completed(url, API_DEDUP_SECONDS)
        if prior and prior.get("result"):
            view = _public_view(prior)
            view["deduplicated"] = True
            return _json(view, status=200)

    row = api_store.create_job(url, card_filter=card_filter,
                               request={"raw_url": str(raw_url)[:600], "force": force})
    job_id = row["job_id"]

    try:
        from xdmovies.bot import Job
        job = Job(
            user_id=_BOT.cfg.owner_id,
            chat_id=0,
            url=url,
            job_type="api",
            content_type="series" if "/series/" in url else "movies",
            user_name="api",
            username="",
            silent=True,
            card_filter=card_filter,
            api_job_id=job_id,
        )
        await _BOT._enqueue_api(job)
    except Exception as exc:
        api_store.set_status(job_id, api_store.STATUS_FAILED,
                             error=f"enqueue failed: {exc.__class__.__name__}: {exc}")
        logger.exception("api_server: enqueue failed for %s", job_id)
        return _json({"error": "enqueue_failed", "job_id": job_id}, status=500)

    logger.info("api_server: job %s queued for %s (filter=%s)", job_id, url, card_filter)
    return _json({
        "job_id": job_id,
        "status": api_store.STATUS_QUEUED,
        "source_url": url,
        "poll_url": f"{API_PREFIX}/jobs/{job_id}",
        "poll_after_s": 5,
    }, status=202)


async def handle_get_job(request):
    job_id = request.match_info.get("job_id", "")
    row = api_store.get_job(job_id)
    if not row:
        return _json({"error": "not_found"}, status=404)
    return _json(_public_view(row))


async def handle_list_jobs(request):
    status = request.query.get("status") or None
    try:
        limit = int(request.query.get("limit", "50"))
    except ValueError:
        limit = 50
    rows = api_store.list_jobs(status=status, limit=limit)
    return _json({"jobs": [_public_view(r) for r in rows], "count": len(rows),
                  "depths": api_store.queue_depths()})


async def handle_delete_job(request):
    job_id = request.match_info.get("job_id", "")
    row = api_store.get_job(job_id)
    if not row:
        return _json({"error": "not_found"}, status=404)
    if row.get("status") in api_store.TERMINAL_STATUSES:
        return _json({"error": "already_terminal", "status": row.get("status")}, status=409)
    api_store.request_cancel(job_id)
    if _BOT is not None:
        try:
            for j in list(getattr(_BOT, "_api", []) or []):
                if getattr(j, "api_job_id", "") == job_id:
                    j.cancel_event.set()
            cur = getattr(_BOT, "current", None)
            if cur is not None and getattr(cur, "api_job_id", "") == job_id:
                cur.cancel_event.set()
        except Exception:
            pass
    return _json({"job_id": job_id, "status": "cancelling"})


async def create_app():
    if web is None:
        raise RuntimeError("aiohttp not installed - pip install aiohttp")
    app = web.Application(middlewares=[_auth_middleware])
    app.router.add_get(f"{API_PREFIX}/health", handle_health)
    app.router.add_post(f"{API_PREFIX}/jobs", handle_create_job)
    app.router.add_get(f"{API_PREFIX}/jobs", handle_list_jobs)
    app.router.add_get(f"{API_PREFIX}/jobs/{{job_id}}", handle_get_job)
    app.router.add_delete(f"{API_PREFIX}/jobs/{{job_id}}", handle_delete_job)
    return app


async def start_api_server(bot) -> bool:
    global _APP, _RUNNER, _SITE, _BOT
    reload_config()
    if web is None:
        logger.warning("api_server: aiohttp not installed")
        return False
    if _APP is not None:
        return True
    if not is_enabled():
        logger.warning("api_server: XDMOVIES_API_KEY not set - API disabled")
        return False
    reload_config()
    api_store.init_db()
    try:
        requeued = api_store.requeue_orphans()
        if requeued:
            logger.warning("api_server: marked %d interrupted job(s) failed", requeued)
    except Exception:
        logger.exception("api_server: orphan sweep failed")
    _BOT = bot
    try:
        _APP = await create_app()
        _RUNNER = web.AppRunner(_APP)
        await _RUNNER.setup()
        _SITE = web.TCPSite(_RUNNER, API_HOST, API_PORT)
        await _SITE.start()
    except Exception:
        logger.exception("api_server: failed to bind %s:%s", API_HOST, API_PORT)
        _APP = _RUNNER = _SITE = None
        return False
    logger.info(
        "api_server listening on %s:%s (max_cards=%s dedup=%.0fs rate=%s/min)",
        API_HOST, API_PORT, API_MAX_CARDS, API_DEDUP_SECONDS, API_RATE_LIMIT,
    )
    return True


async def stop_api_server() -> None:
    global _APP, _RUNNER, _SITE, _BOT
    if _SITE is not None:
        try:
            await _SITE.stop()
        except Exception:
            pass
        _SITE = None
    if _RUNNER is not None:
        try:
            await _RUNNER.cleanup()
        except Exception:
            pass
        _RUNNER = None
    _APP = None
    _BOT = None
    logger.info("api_server stopped")

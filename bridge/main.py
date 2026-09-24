"""
app.py - Render relay between the WordPress plugin and the xdmovies bot.

    WordPress  ->  this service  ->  bot API (:8090)  ->  back to WordPress

The bot API key (BOT_KEY) lives only in this service's environment. The
WordPress plugin never sees it -- it authenticates here with PLUGIN_KEY
instead. That way a compromised WP install cannot talk to the bot directly,
and the bot's real key is not sitting in a database dump.

The relay is intentionally thin: it forwards, it does not interpret. Keeping
the status machine and the payload shape in one place (the bot) means the
plugin has a single contract to code against.

Run on Render:

    uvicorn main:app --host 0.0.0.0 --port $PORT

Environment:
    BOT_BASE     base URL of the bot API, e.g. http://132.145.15.58:8090
    BOT_KEY      XDMOVIES_API_KEY from the bot's .env
    PLUGIN_KEY   shared secret the WordPress plugin sends as X-Bridge-Key
    PORT         provided by Render
"""

from __future__ import annotations

import hmac
import logging
import os

import httpx
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse

BOT_BASE = os.environ.get("BOT_BASE", "http://127.0.0.1:8090").rstrip("/")
BOT_KEY = os.environ.get("BOT_KEY", "")
PLUGIN_KEY = os.environ.get("PLUGIN_KEY", "")
TIMEOUT = float(os.environ.get("RELAY_TIMEOUT", "25"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("bridge")

app = FastAPI(title="xdmovies bridge", version="1.0.0")


def authorized(supplied: str | None) -> bool:
    """Constant-time check of the plugin's shared secret."""
    if not PLUGIN_KEY:
        return False
    return hmac.compare_digest(supplied or "", PLUGIN_KEY)


def configured() -> bool:
    return bool(BOT_KEY and PLUGIN_KEY)


async def relay(method: str, path: str, body: dict | None = None):
    """Forward one request to the bot, preserving its status and body.

    The plugin treats a non-2xx as an error and reads the JSON body, so the
    bot's status code and payload are passed through verbatim rather than
    being wrapped.
    """
    headers = {"Accept": "application/json"}
    if BOT_KEY:
        headers["X-Api-Key"] = BOT_KEY
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.request(
                method, f"{BOT_BASE}{path}", headers=headers, json=body
            )
    except httpx.TimeoutException:
        log.warning("bot timeout on %s %s", method, path)
        return JSONResponse(
            status_code=504, content={"error": "bot_timeout"}
        )
    except httpx.HTTPError as exc:
        log.warning("bot unreachable on %s %s: %r", method, path, exc)
        return JSONResponse(
            status_code=502, content={"error": "bot_unreachable"}
        )

    try:
        data = response.json()
    except ValueError:
        data = {"error": "invalid_json_from_bot"}
    if not isinstance(data, dict):
        data = {"result": data}
    return JSONResponse(status_code=response.status_code, content=data)


@app.get("/health")
async def health():
    """Render health check.

    Deliberately does not call the bot: a slow or sleeping bot must not cause
    Render to restart this service, and the bot is a separate failure domain.
    """
    return {
        "ok": True,
        "service": "xdmovies-bridge",
        "bot_base": BOT_BASE,
        "bot_key_set": bool(BOT_KEY),
        "plugin_key_set": bool(PLUGIN_KEY),
    }


@app.post("/xdm-scrape")
async def xdm_scrape(payload: dict, x_bridge_key: str | None = Header(default=None)):
    """Ask the bot to start resolving an xdmovies page."""
    if not configured():
        return JSONResponse(status_code=503, content={"error": "bridge_not_configured"})
    if not authorized(x_bridge_key):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    return await relay("POST", "/api/v1/jobs", body=payload)


@app.get("/xdm-result/{job_id}")
async def xdm_result(job_id: str, x_bridge_key: str | None = Header(default=None)):
    """Poll a job. The bot returns 200 with the status envelope."""
    if not configured():
        return JSONResponse(status_code=503, content={"error": "bridge_not_configured"})
    if not authorized(x_bridge_key):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    return await relay("GET", f"/api/v1/jobs/{job_id}")


@app.delete("/xdm-cancel/{job_id}")
async def xdm_cancel(job_id: str, x_bridge_key: str | None = Header(default=None)):
    """Cancel a job. 409 means it already reached a terminal state."""
    if not configured():
        return JSONResponse(status_code=503, content={"error": "bridge_not_configured"})
    if not authorized(x_bridge_key):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    return await relay("DELETE", f"/api/v1/jobs/{job_id}")

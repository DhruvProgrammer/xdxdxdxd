"""Stub bot API + stub tmovies importer for the end-to-end test.

Implements only the wire contracts:
  * the bot's  /api/v1/jobs            (X-Api-Key)
  * tmovies'  /?rest_route=/tgmovies/v1/auto-post and /append-links

Nothing here is production code -- it exists so the relay and the WordPress
plugin can be exercised over real HTTP without a browser or a live site.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BOT_KEY = os.environ.get("STUB_BOT_KEY", "bot-secret-key")
RESULT = json.loads(os.environ.get("STUB_RESULT", "{}"))
AUTO_POST_BODY: dict = {}
APPEND_BODY: dict = {}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence
        pass

    # ---- helpers ----
    def _send(self, code: int, payload: dict):
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode() or "{}")
        except ValueError:
            return {}

    # ---- routes ----
    def do_POST(self):
        path = self.path.split("?")[0]

        if path == "/api/v1/jobs":
            if self.headers.get("X-Api-Key") != BOT_KEY:
                return self._send(401, {"error": "unauthorized"})
            body = self._body()
            url = (body.get("url") or "").strip()
            if "xdmovies.wtf/" not in url or "/movies/" not in url:
                return self._send(400, {"error": "invalid_url"})
            job_id = "e2e-job-0001"
            # The bot answers immediately; the result appears on poll.
            return self._send(202, {
                "job_id": job_id, "status": "queued",
                "poll_url": f"/api/v1/jobs/{job_id}", "poll_after_s": 5,
            })

        if "rest_route=/tgmovies/v1/auto-post" in self.path:
            payload = self._body()
            if payload.get("secret_key") != "wp-secret-abc":
                return self._send(403, {"message": "bad secret"})
            AUTO_POST_BODY.update(payload)
            return self._send(200, {
                "success": True, "post_url": "https://tgmovies.test/movies/baby-die-do/",
                "title": "Baby Do Die Do",
            })

        if "rest_route=/tgmovies/v1/append-links" in self.path:
            payload = self._body()
            APPEND_BODY.update(payload)
            return self._send(200, {
                "success": True, "post_url": "https://tgmovies.test/movies/existing/",
                "title": "Existing Movie",
            })

        return self._send(404, {"error": "not_found"})

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/v1/jobs/e2e-job-0001":
            if self.headers.get("X-Api-Key") != BOT_KEY:
                return self._send(401, {"error": "unauthorized"})
            return self._send(200, {
                "job_id": "e2e-job-0001", "status": "done",
                "source_url": RESULT.get("_source", ""),
                "created_at": time.time() - 80, "started_at": time.time() - 78,
                "finished_at": time.time(), "elapsed_s": 78.4,
                "solve_url": None, "error": None, "cancel_requested": False,
                "result": RESULT,
            })
        if path == "/api/v1/jobs/e2e-needs-human":
            if self.headers.get("X-Api-Key") != BOT_KEY:
                return self._send(401, {"error": "unauthorized"})
            return self._send(200, {
                "job_id": "e2e-needs-human", "status": "needs_human",
                "source_url": "", "created_at": time.time(), "started_at": time.time(),
                "finished_at": None, "elapsed_s": 0.0,
                "solve_url": "http://vps:8080/solve/abc123",
                "error": None, "cancel_requested": False, "result": None,
            })
        return self._send(404, {"error": "not_found"})

    def do_DELETE(self):
        if self.path.split("?")[0] == "/api/v1/jobs/e2e-job-0001":
            if self.headers.get("X-Api-Key") != BOT_KEY:
                return self._send(401, {"error": "unauthorized"})
            return self._send(200, {"job_id": "e2e-job-0001", "status": "cancelling"})
        return self._send(404, {"error": "not_found"})


def serve(port: int):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


if __name__ == "__main__":
    port = int(sys.argv[1])
    srv = serve(port)
    print(f"stub listening on {port}", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.shutdown()

"""Run the end-to-end contract test.

Starts three local services:
    :8091  stub bot API + stub tmovies importer   (tests/e2e/stub_servers.py)
    :8092  the REAL bridge relay                 (bridge/main.py via uvicorn)
then runs the REAL plugin classes against them with PHP.

    python tests/e2e/run_e2e.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STUB_PORT = 8091
BRIDGE_PORT = 8092
BOT_KEY = "bot-secret-key"
BRIDGE_KEY = "plugin-shared-key"
PHP = os.environ.get("PHP_BIN") or str(Path(os.environ.get("TEMP", "/tmp")) / "phptools" / "php83" / "php.exe")

RESULT = {
    "title": "Baby Do Die Do",
    "tmdb_id": "1512084",
    "language": "Hindi, Tamil",
    "meta": {"rating": "7.4", "genres": "Comedy, Crime", "first_air": "2026", "sources": "WEB-DL"},
    "cards": [
        {"index": 1, "raw_title": "Baby.Do.Die.Do.2026.2160p.NF.WEB-DL.DDP5.1.Atmos.H.265-XDMovies.com.mkv",
         "size": "13.02 GB", "language": "", "url": "https://hubcloud.ist/drive/aaa",
         "status": "resolved", "error": None},
        {"index": 2, "raw_title": "Baby.Do.Die.Do.2026.1080p.NF.WEB-DL.DDP5.1.H.264-XDMovies.com.mkv",
         "size": "4.90 GB", "language": "Hindi, Tamil", "url": "https://hubdrive.click/bbb",
         "status": "resolved", "error": None},
    ],
    "stats": {"total": 2, "resolved": 2, "failed": 0, "elapsed_s": 78.4, "truncated": False},
    "_source": "https://top.xdmovies.wtf/movies/baby-1512084",
}


def wait_for(url: str, tries: int = 60) -> bool:
    for _ in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def main() -> int:
    stub_env = dict(os.environ, STUB_BOT_KEY=BOT_KEY, STUB_RESULT=json.dumps(RESULT))
    stub = subprocess.Popen([sys.executable, str(ROOT / "tests" / "e2e" / "stub_servers.py"), str(STUB_PORT)], env=stub_env)

    bridge_env = dict(
        os.environ,
        BOT_BASE=f"http://127.0.0.1:{STUB_PORT}",
        BOT_KEY=BOT_KEY,
        PLUGIN_KEY=BRIDGE_KEY,
    )
    bridge = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(BRIDGE_PORT), "--log-level", "warning"],
        cwd=str(ROOT / "bridge"), env=bridge_env,
    )

    rc = 1
    try:
        if not wait_for(f"http://127.0.0.1:{BRIDGE_PORT}/health"):
            print("BRIDGE DID NOT START", file=sys.stderr)
            return 1
        if not wait_for(f"http://127.0.0.1:{STUB_PORT}/api/v1/jobs/e2e-job-0001") and False:
            pass

        php_env = dict(
            os.environ,
            BRIDGE_URL=f"http://127.0.0.1:{BRIDGE_PORT}",
            BRIDGE_KEY=BRIDGE_KEY,
            TGMOVIES_URL=f"http://127.0.0.1:{STUB_PORT}",
            STUB_BOT_KEY=BOT_KEY,
        )
        result = subprocess.run(
            [PHP, str(ROOT / "tests" / "e2e" / "integration.php")],
            env=php_env, capture_output=True, text=True,
        )
        print(result.stdout)
        if result.stderr.strip():
            print(result.stderr, file=sys.stderr)
        rc = result.returncode
    finally:
        for proc in (bridge, stub):
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
    return rc


if __name__ == "__main__":
    sys.exit(main())

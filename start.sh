#!/usr/bin/env bash
# Boot xdmovies bot — VPS token solve-server (VPS_IP:8080), no Docker.
# Direct IP from VPS (no VPN layer — Cloudflare will fingerprint the
# Oracle Cloud ASN; this is intentional per the v6 architecture).

set -e
# VPS public IP: explicit env wins, else auto-detect, else fail loudly.
# (No hardcoded default: a stale IP silently breaks solve links.)
# Override per machine without editing: VPS_IP=1.2.3.4 bash start.sh
VPS_IP="${VPS_IP:-}"
if [ -z "$VPS_IP" ]; then
    VPS_IP="$(curl -fsS --max-time 8 https://api.ipify.org 2>/dev/null || wget -qO- --timeout=8 https://api.ipify.org 2>/dev/null || true)"
fi
if ! printf '%s' "$VPS_IP" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
    echo "[start.sh] FATAL: cannot determine VPS_IP (set VPS_IP=1.2.3.4 env and re-run)." >&2
    exit 1
fi
SOLVE_PORT="${SOLVE_SERVER_PORT:-8080}"
export SOLVE_SERVER_HOST="${SOLVE_SERVER_HOST:-0.0.0.0}"
export SOLVE_SERVER_PORT="$SOLVE_PORT"
export SOLVE_SERVER_PUBLIC_BASE="${SOLVE_SERVER_PUBLIC_BASE:-http://$VPS_IP:$SOLVE_PORT}"

DISPLAY="${DISPLAY-}"
HOME="${HOME:-$USERPROFILE}"
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then PYTHON_BIN="python3"; else PYTHON_BIN="python"; fi
if [[ -x venv/bin/python ]]; then PYTHON_BIN="venv/bin/python"; elif [[ -x .venv/bin/python ]]; then PYTHON_BIN=".venv/bin/python"; fi

USE_XVFB="${XDMOVIES_USE_XVFB:-1}"
NEEDS_XVFB=0

# xvfb-run shells out to awk/getopt without absolute paths. On some VPS
# images (e.g. minimal Ubuntu where the alternatives are broken) those
# names are missing or non-executable, so /usr/bin/xvfb-run exits 127.
# We resolve xvfb-run + its helpers from a known-good set of absolute
# paths and put that dir first on PATH so the wrapper finds them.
XVFB_BIN=""
for cand in /usr/bin/xvfb-run /usr/local/bin/xvfb-run /opt/xvfb-run; do
    if [[ -x "$cand" ]]; then XVFB_BIN="$cand"; break; fi
done

AWK_BIN="$(command -v awk 2>/dev/null || true)"
[[ -x "$AWK_BIN" ]] || AWK_BIN="/usr/bin/awk"
GETOPT_BIN="$(command -v getopt 2>/dev/null || true)"
[[ -x "$GETOPT_BIN" ]] || GETOPT_BIN="/usr/bin/getopt"

# Sanity: if any of these are missing/non-executable, fall back to no-xvfb.
if [[ -n "$XVFB_BIN" && -x "$AWK_BIN" && -x "$GETOPT_BIN" ]]; then
    if [[ "$USE_XVFB" == "1" && -z "$DISPLAY" ]]; then
        NEEDS_XVFB=1
        export PATH="$(dirname "$AWK_BIN"):$(dirname "$GETOPT_BIN"):$PATH"
        export XVFB_RUN="$XVFB_BIN"
    fi
else
    echo "[start.sh] xvfb-run not usable (xvfb=$XVFB_BIN awk=$AWK_BIN getopt=$GETOPT_BIN) — running headless"
    NEEDS_XVFB=0
fi
[[ "$NEEDS_XVFB" == "1" ]] && echo "[start.sh] wrapping in xvfb-run (solve link http://$VPS_IP:$SOLVE_PORT/solve/<token> will be deleted after capture)"

# ---------------------------------------------------------------------------
# Early .env sanity check — refuse to start with the placeholder
# `__REPLACE_ME__` values that deploy.sh ships.
# ---------------------------------------------------------------------------
ENV_FILE="$(dirname "$0")/xdmovies/.env"
if [[ -f "$ENV_FILE" ]] && grep -q '^TELEGRAM_BOT_TOKEN=__REPLACE_ME__\|^TELEGRAM_OWNER_ID=__REPLACE_ME__' "$ENV_FILE"; then
    echo "[start.sh] FATAL: $ENV_FILE still has __REPLACE_ME__ placeholders." >&2
    echo "[start.sh]   Edit it and set TELEGRAM_BOT_TOKEN + TELEGRAM_OWNER_ID, then re-run." >&2
    echo "[start.sh]   Quick check:" >&2
    echo "[start.sh]     grep -E 'TELEGRAM_(BOT_TOKEN|OWNER_ID)' $ENV_FILE" >&2
    exit 1
fi

if ! compgen -G "$HOME/.cache/ms-playwright/chromium*" >/dev/null 2>&1; then
    echo "[start.sh] installing Playwright Chromium (one-time)..."
    "$PYTHON_BIN" -m playwright install chromium 2>&1 | tail -5 || true
fi
curl -fs "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN:-}/getUpdates?limit=1&offset=-1" >/dev/null 2>&1 || true
if [[ "$NEEDS_XVFB" == "1" ]]; then exec "$XVFB_BIN" -a "$PYTHON_BIN" -u -m xdmovies "$@"; else exec "$PYTHON_BIN" -u -m xdmovies "$@"; fi

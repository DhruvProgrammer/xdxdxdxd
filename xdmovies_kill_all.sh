#!/usr/bin/env bash
# xdmovies-bot hard reset — kill every related process, free 8080,
# clear lockfile, restart the systemd service.
#
# Usage:
#     bash ~/xdmovies_kill_all.sh

set -u

RED=$'\033[1;31m'
GRN=$'\033[1;32m'
YEL=$'\033[1;33m'
CYN=$'\033[1;36m'
OFF=$'\033[0m'

log()  { printf "${CYN}[kill_all]${OFF} %s\n" "$*"; }
warn() { printf "${YEL}[warn]${OFF} %s\n" "$*"; }
ok()   { printf "${GRN}[ok]${OFF} %s\n" "$*"; }
die()  { printf "${RED}[fail]${OFF} %s\n" "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 0. Pre-flight — needs sudo
# ---------------------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
    if ! command -v sudo >/dev/null 2>&1; then
        die "not root and sudo is not available; run as root"
    fi
    SUDO="sudo"
else
    SUDO=""
fi

# ---------------------------------------------------------------------------
# 1. Stop the systemd unit first (graceful) so the supervisor can't
#    respawn while we kill children.
# ---------------------------------------------------------------------------
log "stopping xdmovies-bot systemd unit (if present)..."
$SUDO systemctl stop xdmovies-bot 2>/dev/null || true
$SUDO systemctl reset-failed xdmovies-bot 2>/dev/null || true
ok "unit stopped"

# ---------------------------------------------------------------------------
# 2. Kill every process that could be holding the bot, port 8080, the
#    Playwright driver, or the chromium children.
# ---------------------------------------------------------------------------
KILL_PATTERNS=(
    "xvfb-run"
    "Xvfb"
    "python.*-m xdmovies"
    "python.*xdmovies_bot"
    "python.*playwright"
    "chrome.*--type"
    "chromium.*--type"
    "headless_shell"
)

log "killing related processes..."
for pat in "${KILL_PATTERNS[@]}"; do
    pids=$(pgrep -af "$pat" 2>/dev/null | awk '{print $1}' | xargs)
    if [[ -n "$pids" ]]; then
        warn "  matching '$pat' -> pids: $pids"
        $SUDO kill -9 $pids 2>/dev/null || true
    fi
done

# Belt-and-suspenders: pkill for the obvious names
$SUDO pkill -9 -f "xvfb-run" 2>/dev/null || true
$SUDO pkill -9 -f "xdmovies" 2>/dev/null || true
$SUDO pkill -9 -f "playwright" 2>/dev/null || true
$SUDO pkill -9 -f "chrome" 2>/dev/null || true
$SUDO pkill -9 -f "chromium" 2>/dev/null || true
ok "killed related processes"

# (No VPN layer to tear down — the bot runs direct from the VPS IP.)

# ---------------------------------------------------------------------------
# 3. Free port 8080 (or whatever SOLVE_PORT the user has configured).
# ---------------------------------------------------------------------------
SOLVE_PORT="${SOLVE_PORT:-8080}"
log "freeing port $SOLVE_PORT..."
if command -v fuser >/dev/null 2>&1; then
    $SUDO fuser -k "${SOLVE_PORT}/tcp" 2>/dev/null || true
fi
# Give the kernel a moment to release TIME_WAIT sockets
sleep 2

if command -v ss >/dev/null 2>&1; then
    if $SUDO ss -tlnp 2>/dev/null | grep -q ":${SOLVE_PORT} "; then
        warn "port $SOLVE_PORT is STILL held after fuser; dumping holders:"
        $SUDO ss -tlnp | grep ":${SOLVE_PORT} " || true
        warn "you may need to kill the PID by hand: sudo kill -9 <PID>"
    else
        ok "port $SOLVE_PORT is free"
    fi
fi

# ---------------------------------------------------------------------------
# 4. Remove the single-instance lockfile so the next start doesn't
#    think another instance is alive.
# ---------------------------------------------------------------------------
LOCK_CANDIDATES=(
    "$HOME/xdmovies_bot_repo/.xdmovies_bot.lock"
    "$HOME/xdmovies_bot_deploy/xdmovies_bot_repo/.xdmovies_bot.lock"
    "$HOME/xdmovies_bot_deploy_v2/xdmovies_bot_repo/.xdmovies_bot.lock"
    "/home/*/xdmovies_bot_repo/.xdmovies_bot.lock"
    "/root/xdmovies_bot_repo/.xdmovies_bot.lock"
    "/opt/xdmovies_bot_repo/.xdmovies_bot.lock"
)
for lf in "${LOCK_CANDIDATES[@]}"; do
    for f in $lf; do
        if [[ -f "$f" ]]; then
            log "removing lockfile: $f"
            $SUDO rm -f "$f"
        fi
    done
done
ok "lockfiles cleared"

# ---------------------------------------------------------------------------
# 5. Verify nothing is left
# ---------------------------------------------------------------------------
log "verifying clean state..."
LEFT=$(pgrep -af "xdmovies|chromium|xvfb" 2>/dev/null | grep -v grep || true)
if [[ -n "$LEFT" ]]; then
    warn "still running after cleanup:"
    echo "$LEFT"
else
    ok "no xdmovies / chromium / xvfb processes left"
fi

if command -v ss >/dev/null 2>&1; then
    if $SUDO ss -tlnp 2>/dev/null | grep -q ":${SOLVE_PORT} "; then
        warn "port $SOLVE_PORT still held; do not start the bot yet"
        warn "check: sudo lsof -i :$SOLVE_PORT   or   sudo ss -tlnp | grep $SOLVE_PORT"
    fi
fi

# ---------------------------------------------------------------------------
# 6. (Optional) Restart the systemd unit
#    Default: do NOT restart. Set RESTART=1 to restart.
# ---------------------------------------------------------------------------
if [[ "${RESTART:-0}" == "1" ]]; then
    log "starting xdmovies-bot systemd unit..."
    $SUDO systemctl start xdmovies-bot
    sleep 5
    log "service status:"
    $SUDO systemctl status xdmovies-bot --no-pager -n 5
    log "last 15 log lines:"
    $SUDO journalctl -u xdmovies-bot -n 15 --no-pager
else
    log "RESTART not set — service left stopped. Start it manually when ready:"
    log "  sudo systemctl start xdmovies-bot"
fi

echo
ok "done (nothing restarted)"

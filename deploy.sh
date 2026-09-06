#!/usr/bin/env bash
# xdmovies bot — VPS deploy — VPS token only, no Docker/Cantarebella

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_DIR="${REPO_DIR:-${SCRIPT_DIR}}"
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-${USER}}}"
SWAP_SIZE_MB="${SWAP_SIZE_MB:-2048}"
# VPS public IP: explicit env wins, else auto-detect, else fail loudly.
# (No hardcoded default anywhere: a stale IP silently breaks solve links.)
# Override per machine without editing: VPS_IP=1.2.3.4 bash deploy.sh
VPS_IP="${VPS_IP:-}"
SOLVE_PORT="8080"

log() { printf "\033[1;34m[deploy]\033[0m %s\n" "$*"; }
err() { printf "\033[1;31m[err]\033[0m %s\n" "$*" >&2; }

if [ -z "${VPS_IP:-}" ]; then
    VPS_IP="$(curl -fsS --max-time 8 https://api.ipify.org 2>/dev/null || wget -qO- --timeout=8 https://api.ipify.org 2>/dev/null || true)"
fi
if [ -z "${VPS_IP:-}" ] || [[ ! "$VPS_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    err "VPS_IP is empty/unresolvable and public-IP auto-detect failed."
    err "Re-run as: VPS_IP=1.2.3.4 bash deploy.sh"
    exit 1
fi

log "VPS: $VPS_IP  solve link: http://$VPS_IP:$SOLVE_PORT/solve/<token>"

log "Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip \
    git curl wget unzip fontconfig ca-certificates \
    xvfb gawk mawk util-linux xauth \
    x11-xkb-utils xkb-data \
    libgbm1 libnss3 libxshmfence1 libasound2 \
    libatk-bridge2.0-0 libatk1.0-0 libatspi2.0-0 libcairo2 \
    libcups2 libdbus-1-3 libdrm2 libglib2.0-0 \
    libnspr4 libpango-1.0-0 libxcomposite1 \
    libxdamage1 libxfixes3 libxcb1 libxkbcommon0 libxrandr2

# Optional but recommended: real desktop + VNC for the operator to
# manually solve the first Turnstile. Off by default; set
# XDMOVIES_ENABLE_VNC=1 in the deploy environment to enable.
if [[ "${XDMOVIES_ENABLE_VNC:-0}" == "1" ]]; then
    log "XDMOVIES_ENABLE_VNC=1 → installing XFCE + x11vnc + noVNC (real desktop)"
    sudo apt-get install -y --no-install-recommends \
        xfce4 xfce4-goodies x11vnc novnc websockify dbus-x11
fi

# xvfb-run shells out to awk + getopt without absolute paths; ensure the
# binaries the system alternatives point to actually exist and run. Some
# minimal VPS images ship a dangling /usr/bin/awk -> /etc/alternatives/awk
# symlink with no target, which makes xvfb-run exit 127 with
# "awk: not found" / "getopt: not found". We repair that here.
log "Verifying + repairing xvfb-run dependencies (awk, getopt)..."
sudo apt-get install --reinstall -y gawk mawk util-linux xvfb

repair_symlink() {
    local name="$1" real="$2"
    local cur
    cur="$(readlink -f "/usr/bin/$name" 2>/dev/null || true)"
    if [[ ! -x "$cur" ]]; then
        log "  /usr/bin/$name -> $cur is broken, relinking to $real"
        sudo ln -sf "$real" "/etc/alternatives/$name"
        sudo ln -sf "$real" "/usr/bin/$name"
    fi
    local final
    final="$(readlink -f "/usr/bin/$name" 2>/dev/null || true)"
    if [[ ! -x "$final" ]]; then
        err "still broken after repair: /usr/bin/$name -> $final"
        return 1
    fi
    if ! "$final" --version >/dev/null 2>&1 && ! "$final" -V >/dev/null 2>&1; then
        "$final" >/dev/null 2>&1 || true
    fi
    log "  $name -> $final OK"
}

# pick the awk we want (gawk > mawk > original-awk)
if   [[ -x /usr/bin/gawk ]]; then repair_symlink awk /usr/bin/gawk
elif [[ -x /usr/bin/mawk ]]; then repair_symlink awk /usr/bin/mawk
else err "no awk implementation found after reinstall"; exit 1
fi

if [[ ! -x /usr/bin/getopt ]]; then
    # util-linux ships /usr/bin/getopt; fall back to busybox if that's what we have
    for cand in /bin/getopt /usr/bin/getopt; do
        if [[ -x "$cand" ]]; then
            log "  getopt -> $cand OK"
            break
        fi
    done
fi

for bin in awk getopt; do
    p="$(command -v "$bin" || true)"
    if [[ -z "$p" || ! -x "$p" ]]; then
        err "missing or non-executable: $bin"
        exit 1
    fi
    log "  $bin -> $p OK"
done

# Sanity: actually exec xvfb-run with a no-op so we catch any other
# missing helper (xauth, Xvfb, etc.) before the systemd unit goes live.
log "Smoke-testing xvfb-run with /bin/true..."
if ! sudo xvfb-run -a /bin/true; then
    err "xvfb-run smoke test failed; check 'xvfb-run -a /bin/true' output above"
    exit 1
fi
log "  xvfb-run OK"

if [ ! -f /swapfile ] && [ "${SKIP_SWAP:-0}" != "1" ]; then
    log "Creating ${SWAP_SIZE_MB}MB swap..."
    sudo fallocate -l "${SWAP_SIZE_MB}M" /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
    sudo swapon /swapfile
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
fi

if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then err "no such user $SERVICE_USER"; exit 1; fi
if [ ! -f "${REPO_DIR}/xdmovies/__main__.py" ]; then err "repo not at $REPO_DIR"; exit 1; fi
log "Repo: $REPO_DIR  user: $SERVICE_USER"
SERVICE_GROUP="$(id -gn "${SERVICE_USER}")"
chmod +x "${REPO_DIR}/start.sh"

cd "${REPO_DIR}"
if [ ! -d venv ]; then
    log "Creating venv..."
    sudo -u "${SERVICE_USER}" python3 -m venv venv
fi
log "Installing Python deps..."
sudo -u "${SERVICE_USER}" ./venv/bin/pip install --upgrade pip setuptools wheel
sudo -u "${SERVICE_USER}" ./venv/bin/pip install -r requirements.txt
sudo -u "${SERVICE_USER}" ./venv/bin/pip install -r xdmovies/requirements.txt 2>/dev/null || true

log "Installing Playwright Chromium..."
sudo -u "${SERVICE_USER}" ./venv/bin/playwright install chromium
sudo ./venv/bin/playwright install-deps chromium 2>/dev/null || log "WARN: playwright install-deps failed"

# .env with VPS IP
if [ ! -f "${REPO_DIR}/xdmovies/.env" ]; then
    log "Creating xdmovies/.env with VPS IP $VPS_IP..."
    cat > "${REPO_DIR}/xdmovies/.env" <<EOF
TELEGRAM_BOT_TOKEN=__REPLACE_ME__
TELEGRAM_OWNER_ID=__REPLACE_ME__
TELEGRAM_ADMIN_IDS=
SOLVE_SERVER_HOST=0.0.0.0
SOLVE_SERVER_PORT=$SOLVE_PORT
SOLVE_SERVER_PUBLIC_BASE=http://$VPS_IP:$SOLVE_PORT
EOF
    chmod 600 "${REPO_DIR}/xdmovies/.env"
    log "*** EDIT ${REPO_DIR}/xdmovies/.env — set TELEGRAM_BOT_TOKEN / OWNER_ID (SOLVE link already set to http://$VPS_IP:$SOLVE_PORT) ***"
else
    # ensure solve link is correct even if .env existed
    if ! grep -q "SOLVE_SERVER_PUBLIC_BASE" "${REPO_DIR}/xdmovies/.env"; then
        echo "SOLVE_SERVER_HOST=0.0.0.0" | sudo -u "${SERVICE_USER}" tee -a "${REPO_DIR}/xdmovies/.env" >/dev/null
        echo "SOLVE_SERVER_PORT=$SOLVE_PORT" | sudo -u "${SERVICE_USER}" tee -a "${REPO_DIR}/xdmovies/.env" >/dev/null
        echo "SOLVE_SERVER_PUBLIC_BASE=http://$VPS_IP:$SOLVE_PORT" | sudo -u "${SERVICE_USER}" tee -a "${REPO_DIR}/xdmovies/.env" >/dev/null
    else
        sudo sed -i "s|SOLVE_SERVER_PUBLIC_BASE=.*|SOLVE_SERVER_PUBLIC_BASE=http://$VPS_IP:$SOLVE_PORT|" "${REPO_DIR}/xdmovies/.env"
    fi
    chmod 600 "${REPO_DIR}/xdmovies/.env" 2>/dev/null || true
fi
if [ ! -f "${REPO_DIR}/.env" ]; then
    sudo -u "${SERVICE_USER}" cp "${REPO_DIR}/xdmovies/.env" "${REPO_DIR}/.env" 2>/dev/null || true
fi

# firewall for solve link (VPS IP:8080 must be reachable)
if command -v ufw >/dev/null 2>&1; then
    log "Opening firewall $SOLVE_PORT/tcp..."
    sudo ufw allow "$SOLVE_PORT/tcp" 2>/dev/null || true
fi
if command -v firewall-cmd >/dev/null 2>&1; then
    sudo firewall-cmd --add-port="$SOLVE_PORT/tcp" --permanent 2>/dev/null || true
    sudo firewall-cmd --reload 2>/dev/null || true
fi

SERVICE_FILE="/etc/systemd/system/xdmovies-bot.service"
log "Installing systemd unit $SERVICE_FILE..."

# Pre-flight: free port $SOLVE_PORT (kill any stale xdmovies/xvfb from a
# previous deploy) and remove the lockfile. Without this, a fresh
# `systemctl start` will fail with "address already in use" or
# "terminated by other getUpdates" (Telegram 409 Conflict) because the
# old process is still bound to 8080 + still polling.
log "Killing any stale xdmovies / xvfb-run / chromium / port-$SOLVE_PORT holders..."
for pat in "xvfb-run.*xdmovies" "python.*-m xdmovies" "chrome.*--type" "chromium.*--type"; do
    pkill -9 -f "$pat" 2>/dev/null || true
done
if command -v fuser >/dev/null 2>&1; then
    fuser -k "${SOLVE_PORT}/tcp" 2>/dev/null || true
fi
sleep 1
if command -v ss >/dev/null 2>&1; then
    if ss -tlnp 2>/dev/null | grep -q ":${SOLVE_PORT} "; then
        err "port $SOLVE_PORT is STILL held after cleanup; check 'ss -tlnp | grep $SOLVE_PORT'"
        err "try: sudo lsof -i :$SOLVE_PORT   and kill the PID by hand"
    else
        log "port $SOLVE_PORT is free"
    fi
fi
rm -f "${REPO_DIR}/.xdmovies_bot.lock" 2>/dev/null || true

sudo tee "${SERVICE_FILE}" >/dev/null <<EOF
[Unit]
Description=xdmovies Telegram bot — VPS token solve-server + CloakBrowser ($VPS_IP)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
WorkingDirectory=${REPO_DIR}
# Keep the system PATH so xvfb-run can find awk/getopt; prepend venv so
# the python deps (playwright, telegram-bot, cloakbrowser) resolve first.
Environment="PATH=${REPO_DIR}/venv/bin:/usr/local/bin:/usr/bin:/bin"
Environment="PYTHONUNBUFFERED=1"
Environment="SOLVE_SERVER_HOST=0.0.0.0"
Environment="SOLVE_SERVER_PORT=$SOLVE_PORT"
Environment="SOLVE_SERVER_PUBLIC_BASE=http://$VPS_IP:$SOLVE_PORT"
# CloakBrowser binary cache (200MB Chromium). Set to /opt so the
# systemd unit survives container rebuilds without re-downloading.
Environment="CLOAKBROWSER_CACHE_DIR=/opt/cloakbrowser"
Environment="CLOAKBROWSER_AUTO_UPDATE=false"
ExecStart=/usr/bin/xvfb-run -a ${REPO_DIR}/venv/bin/python -u -m xdmovies
# Defensive: if the bot dies and the port is held by a zombie child,
# kill the whole cgroup on stop so the next start is clean.
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=15
Restart=always
RestartSec=10
TimeoutStartSec=180
MemoryMax=4G
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload

# ---------------------------------------------------------------------------
# Sudoers: let the bot user manage xdmovies-bot without retyping a
# password. Scope is the bare minimum: ONLY this one unit, NO shell,
# NO NOPASSWD for other commands. (No VPN units anymore.)
# ---------------------------------------------------------------------------
SUDOERS_FILE="/etc/sudoers.d/xdmovies-bot"
log "Installing $SUDOERS_FILE (passwordless systemd for the bot user)..."
sudo tee "$SUDOERS_FILE" >/dev/null <<EOF
# Allow ${SERVICE_USER} to manage xdmovies-bot without a password.
# Anything else still requires the usual password.
${SERVICE_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl restart xdmovies-bot
${SERVICE_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl start xdmovies-bot
${SERVICE_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl stop xdmovies-bot
${SERVICE_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl status xdmovies-bot
EOF
sudo chmod 0440 "$SUDOERS_FILE"
sudo visudo -c -f "$SUDOERS_FILE" || {
    err "sudoers syntax check failed — removing $SUDOERS_FILE"
    sudo rm -f "$SUDOERS_FILE"
    exit 1
}
log "  ${SERVICE_USER} can now restart xdmovies-bot without a password"

log "Installed. Enable+start:"
log "  sudo systemctl enable --now xdmovies-bot"
log "Solve link base: http://$VPS_IP:$SOLVE_PORT/solve/<token> (ensure $VPS_IP:$SOLVE_PORT is reachable, message auto-deleted after capture)"

# ---------------------------------------------------------------------------
# Real desktop + VNC (optional, env-gated)
# ---------------------------------------------------------------------------
# Set XDMOVIES_ENABLE_VNC=1 in the deploy env to install XFCE + x11vnc
# + noVNC. This gives the operator a persistent desktop to manually
# solve the first Turnstile in a real browser window. After the first
# solve, /status shows desktop ✅ and the bot keeps the cookie alive
# via CloakBrowser auto-renewal — no further manual interaction needed.
if [[ "${XDMOVIES_ENABLE_VNC:-0}" == "1" ]]; then
    log "Setting up XFCE + x11vnc + noVNC (XDMOVIES_ENABLE_VNC=1)"
    # Pick the existing non-root user (defaults to dhruv).
    VNC_USER="${SERVICE_USER:-dhruv}"
    sudo install -d -m 0755 -o "${VNC_USER}" -g "${VNC_USER}" /home/${VNC_USER}/.vnc 2>/dev/null || true
    if [[ ! -f /home/${VNC_USER}/.vnc/passwd ]]; then
        VNC_PASS="$(head -c 16 /dev/urandom | xxd -p)"
        sudo -u "${VNC_USER}" bash -c "x11vnc -storepasswd '$VNC_PASS' /home/${VNC_USER}/.vnc/passwd"
        log "  noVNC password: $VNC_PASS (saved to /home/${VNC_USER}/.vnc/passwd)"
    fi
    # systemd units for Xvfb, XFCE, x11vnc, noVNC.
    sudo install -d /etc/systemd/system
    cat <<'XVFB' | sudo tee /etc/systemd/system/xdmovies-xvfb.service >/dev/null
[Unit]
Description=xdmovies virtual display :0
[Service]
ExecStart=/usr/bin/Xvfb :0 -screen 0 1920x1080x24
Restart=always
[Install]
WantedBy=multi-user.target
XVFB
    cat <<XFCE | sudo tee /etc/systemd/system/xdmovies-xfce.service >/dev/null
[Unit]
Description=xdmovies XFCE session on :0
After=xdmovies-xvfb.service
[Service]
User=${VNC_USER}
Environment=DISPLAY=:0
ExecStart=/usr/bin/startxfce4
Restart=always
[Install]
WantedBy=multi-user.target
XFCE
    cat <<'XVNC' | sudo tee /etc/systemd/system/xdmovies-vnc.service >/dev/null
[Unit]
Description=xdmovies x11vnc
After=xdmovies-xfce.service
[Service]
User=${VNC_USER}
Environment=DISPLAY=:0
ExecStart=/usr/bin/x11vnc -display :0 -forever -shared -rfbauth /home/${VNC_USER}/.vnc/passwd
Restart=always
[Install]
WantedBy=multi-user.target
XVNC
    cat <<'NOVNC' | sudo tee /etc/systemd/system/xdmovies-novnc.service >/dev/null
[Unit]
Description=xdmovies noVNC (browser-based)
After=xdmovies-vnc.service
[Service]
ExecStart=/usr/share/novnc/utils/launch.sh --listen 6080 --vnc localhost:5900
Restart=always
[Install]
WantedBy=multi-user.target
NOVNC
    sudo systemctl daemon-reload
    sudo systemctl enable --now xdmovies-xvfb xdmovies-xfce xdmovies-vnc xdmovies-novnc
    log "  XFCE desktop up; noVNC at http://$VPS_IP:6080/vnc.html"
    log "  (set up an SSH tunnel for security: ssh -L 6080:127.0.0.1:6080 dhruv@$VPS_IP)"
fi

log "Running --check..."
sudo -u "${SERVICE_USER}" ./venv/bin/python -m xdmovies --check || log "WARN: --check failed"

log ""
log "DONE — VPS token solve-server + CloakBrowser stealth (no VPN layer)."
log "  Edit xdmovies/.env then: sudo systemctl enable --now xdmovies-bot"
log "  journalctl -u xdmovies-bot -f"

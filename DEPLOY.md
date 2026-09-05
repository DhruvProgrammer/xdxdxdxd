# Deploy — CloakBrowser + VPS token solve-server (no VPN)

No solver, no sidecar, no VPN. VPS needs Python + CloakBrowser + xvfb.

## 1. Quick deploy

```bash
unzip xdmovies_bot_deploy_v6.zip
cd xdmovies_bot_repo
chmod +x deploy.sh start.sh
./deploy.sh
nano xdmovies/.env       # TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_ID
./deploy.sh              # re-run so the systemd unit picks up the new .env
sudo systemctl enable --now xdmovies-bot
journalctl -u xdmovies-bot -f
```

`./deploy.sh` installs:

- Python 3 + venv at `venv/`
- System packages: `python3-venv xvfb x11-xkb-utils xkb-data libgbm1 libnss3 libxshmfence1 ...` for Chromium
- `cloakbrowser` (pinned `>=0.5.0` in `xdmovies/requirements.txt`) — the bot imports it at startup; if missing, systemd restarts in a tight loop until you `pip install` it
- `pip install cloakbrowser` triggers a ~200MB Chromium download on first run; deploy.sh sets `CLOAKBROWSER_CACHE_DIR=/opt/cloakbrowser` so the binary survives systemd restarts
- A systemd unit at `/etc/systemd/system/xdmovies-bot.service` that runs the bot under `xvfb-run -a`

In Telegram, send a movie URL. When the bot says "Cloudflare blocked",
open the solve link it sent and click the Turnstile checkbox. The bot
auto-captures `cf_clearance` and retries. `/status` shows cloak / desktop
state.

## 2. CloakBrowser verification

The bot uses CloakBrowser (a patched Chromium with C++ source-level
fingerprint patches) for ALL 5 launch sites: `pipeline.py`,
`link_follower.py`, `link_exec.py` (×2), `solve_server.py`,
`keep_alive.py`. The wrapper is in `xdmovies/cloak_stealth.py`.

Verify after deploy:

```bash
# 1. Is cloakbrowser importable from the venv?
sudo -u dhruv venv/bin/python -c "import cloakbrowser; print(cloakbrowser.__version__)"
# Expected: 0.5.8

# 2. Did the binary download to /opt/cloakbrowser?
ls -la /opt/cloakbrowser/
# Expected: chrome (binary), chrome_crashpad_handler, etc.

# 3. Does /status show it?
# In Telegram: /status
# Expected line: "cloak: ✅ v0.5.8 (linux-x64, C++ patches)"

# 4. (Optional) Run a fingerprint test page
sudo -u dhruv venv/bin/python -c "
import asyncio
from cloakbrowser import launch_async
async def main():
    b = await launch_async(headless=False, humanize=True)
    p = await b.new_page()
    await p.goto('https://bot.sannysoft.com/')
    input('Press Enter to close...')
    await b.close()
asyncio.run(main())
"
# Should show: navigator.webdriver = false, plugins.length = 5, etc.
```

## 3. Architecture: no VPN layer

The bot connects from the VPS IP (`132.145.15.58`) directly. Cloudflare
flags the Oracle Cloud ASN, so `/solve` flow success rate is reduced
vs. a residential IP. If you need higher success, route CloakBrowser
through a residential SOCKS5 proxy by passing
`proxy={"server": "socks5://host:port", "username": …, "password": …}`
to `launch_chromium()` at the call sites. This release does not include
that wiring by default.

**Adding a SOCKS5 proxy later (operator option):** edit
`xdmovies/cloak_stealth.py` to set the default `proxy` to your
endpoint, or pass `proxy=...` per call site. The `cloakbrowser` library
passes the proxy URL to Chromium's `--proxy-server` flag directly
(verified: `cloakbrowser/browser.py:1722-1733`).

## 4. Optional: real desktop (XFCE + VNC) for the first solve

```bash
# Re-run deploy.sh with the env var:
XDMOVIES_ENABLE_VNC=1 sudo -E ./deploy.sh

# Open noVNC in your browser:
#   http://132.145.15.58:6080/vnc.html
# (set up an SSH tunnel for security: ssh -L 6080:127.0.0.1:6080 dhruv@132.145.15.58)
```

The noVNC password is printed to the deploy log on first install.
After the first solve, the bot keeps the cookie alive via CloakBrowser
auto-renewal — no further manual interaction needed.

## 5. Optional: `eternal_bypass.py` — operator-only single-shot helper

For a more controlled manual-solve experience than the Telegram-mediated
solve-server, the operator can run `xdmovies/eternal_bypass.py` directly
on the VPS. It uses CloakBrowser's persistent profile so cookies
survive restarts, and auto-renews the cookie before expiry.

```bash
# On the VPS, as the bot user:
sudo -u dhruv venv/bin/python -m xdmovies.eternal_bypass

# It will:
# 1. Launch CloakBrowser (persistent profile at /var/lib/xdmovies/eternal_profile)
# 2. Navigate to the target site
# 3. If a valid cf_clearance exists, enter the renewal loop immediately
# 4. If not, wait for you to click the Turnstile checkbox
# 5. After capture, save the cookie and enter the eternal renewal loop
#    (visit the site 5 min before expiry, wait 30s, repeat forever)
```

This is for the operator's personal use. The Telegram solve-server
remains the primary path for all users.

## 6. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| systemd restart loop on start | `cloakbrowser` not installed | `sudo -u dhruv venv/bin/pip install 'cloakbrowser>=0.5.0'` |
| `/status` shows `cloak: ❌ not importable` | Python venv mismatch | `sudo systemctl restart xdmovies-bot` after `pip install` |
| `vpn: ❌ none (direct from VPS IP — expect CF to flag this)` | The bot has no VPN layer (intentional). Cloudflare's datacenter-ASN fingerprinting is reducing `/solve` success rate. | Route CloakBrowser through a residential SOCKS5 proxy by passing `proxy={"server": "socks5://host:port", ...}` to `launch_chromium()` at the 5 call sites. |
| Solve link sent to user doesn't work | Firewall on 8080 is blocking Telegram users from reaching the VPS | `sudo ufw allow 8080/tcp` (or `firewall-cmd --add-port=8080/tcp --permanent && firewall-cmd --reload`) |
| `start.sh` exits with "still has `__REPLACE_ME__` placeholders" | You didn't fill in `xdmovies/.env` after first deploy | `nano xdmovies/.env`, replace `__REPLACE_ME__` for `TELEGRAM_BOT_TOKEN` and `TELEGRAM_OWNER_ID` |
| `OpenSSL.SSL.Error: certificate verify failed` (CloakBrowser binary download) | Stale CA bundle on the VPS | `sudo apt update && sudo apt install -y --reinstall ca-certificates` |
| `xdmovies-bot` service won't start: "address already in use" for port 8080 | Previous `xdmovies-bot` is still bound to 8080 | `bash xdmovies_kill_all.sh` then retry |

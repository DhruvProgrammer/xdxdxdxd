"""Solve link server — VPS token only (no manual paste).

Bot creates token for blocked URL: token = await create_session(url, user_id, host)
public_url = get_public_url(token)  # http://VPS:8080/solve/<token>

User opens public_url, sees live JPEG screenshot of VPS headed Chromium, taps checkbox.
Server polls context.cookies() for cf_clearance, auto-saves to cookie_store,
signals bot via event. Bot then deletes the Telegram message containing VPS IP:port.
No manual paste — only auto-capture.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional

try:
    from aiohttp import web
except ImportError:
    web = None  # type: ignore

from xdmovies.cookie_store import _normalize_domain, set_cookies_for_domain
from xdmovies.logger_utils import logger

SOLVE_HOST = os.getenv("SOLVE_SERVER_HOST", "0.0.0.0")
SOLVE_PORT = int(os.getenv("SOLVE_SERVER_PORT", "8080"))
SOLVE_PUBLIC_BASE = os.getenv("SOLVE_SERVER_PUBLIC_BASE", "")
if not SOLVE_PUBLIC_BASE:
    public_host = os.getenv("SOLVE_SERVER_PUBLIC_HOST", "")
    if public_host:
        SOLVE_PUBLIC_BASE = f"http://{public_host}:{SOLVE_PORT}"
    else:
        SOLVE_PUBLIC_BASE = f"http://localhost:{SOLVE_PORT}"

@dataclass
class SolveSession:
    token: str
    url: str
    host: str
    user_id: int
    chat_id: int
    created_at: float = field(default_factory=time.time)
    solved: bool = False
    cf_value: Optional[str] = None
    event: asyncio.Event = field(default_factory=asyncio.Event)
    pw: Optional[object] = None
    browser: Optional[object] = None
    context: Optional[object] = None
    page: Optional[object] = None
    poll_task: Optional[asyncio.Task] = None
    goto_error: Optional[str] = None
    # Set to True when the auto-nudge tried (and failed) to bypass CF
    # without user input. bot.py reads this to decide whether to send
    # the manual token link as a fallback.
    auto_nudge_failed: bool = False
    # Gate result: for latestnewsonline.sbs URLs the inner gate captcha
    # does NOT issue cf_clearance, so cookie capture alone can never
    # complete the solve. When the auto-drive (or the user) clicks Go to
    # Link, the destination tab URL is stored here and the session is
    # marked solved. bot.py reads it via get_solved_result() and uses it
    # directly instead of retrying with a stale cookie.
    final_url: Optional[str] = None
    # Background tasks owned by this session (cancelled in destroy).
    launch_task: Optional[asyncio.Task] = None
    auto_nudge_task: Optional[asyncio.Task] = None
    expire_task: Optional[asyncio.Task] = None
    last_viewer_activity: float = 0.0
    last_shot_at: float = 0.0


SESSION_IDLE_TIMEOUT = max(300, int(os.getenv("XDMOVIES_SOLVE_IDLE_TIMEOUT", "3600") or "3600"))
VIEWER_ACTIVITY_TIMEOUT = max(60, int(os.getenv("XDMOVIES_SOLVE_VIEWER_WINDOW", "90") or "90"))
SCREENSHOT_MIN_INTERVAL = max(1.0, float(os.getenv("XDMOVIES_SOLVE_SHOT_INTERVAL", "3") or "3"))
SCREENSHOT_QUALITY = max(20, min(50, int(os.getenv("XDMOVIES_SOLVE_SHOT_QUALITY", "30") or "30")))
BLOCK_GATE_IMAGES = os.getenv("XDMOVIES_SOLVE_BLOCK_GATE_IMAGES", "0").lower() in ("1", "true", "yes", "on")

_SESSIONS: Dict[str, SolveSession] = {}
_APP: Optional[object] = None
_RUNNER: Optional[object] = None
_SITE: Optional[object] = None
_LOCK = asyncio.Lock()
SESSION_TTL = SESSION_IDLE_TIMEOUT


def _touch_viewer(sess: SolveSession) -> None:
    sess.last_viewer_activity = time.time()


def _viewer_is_active(sess: SolveSession, now: Optional[float] = None) -> bool:
    timestamp = time.time() if now is None else float(now)
    last = float(sess.last_viewer_activity or 0.0)
    return last > 0 and timestamp - last <= VIEWER_ACTIVITY_TIMEOUT


def _session_idle_seconds(sess: SolveSession, now: Optional[float] = None) -> float:
    timestamp = time.time() if now is None else float(now)
    reference = max(float(sess.created_at or timestamp), float(sess.last_viewer_activity or 0.0))
    return max(0.0, timestamp - reference)

# Solved-result stash, keyed by token. wait_for_solve destroys the
# session ~25s after capture (Fix A), so by the time the bot's
# _wait_for_solve returns True the session object is gone. The final
# destination URL (gate flow) and cookie value are stashed here at
# solve time so bot.py can still read them. Entries older than 10
# minutes are purged on read.
_SOLVED_RESULTS: Dict[str, dict] = {}
_SOLVED_RESULT_TTL = 600


def _stash_solved_result(token: str, final_url: Optional[str] = None,
                         cf_value: Optional[str] = None) -> None:
    try:
        _SOLVED_RESULTS[token] = {
            "final_url": final_url or "",
            "cf_value": cf_value or "",
            "at": time.time(),
        }
    except Exception:
        pass


def get_solved_result(token: str) -> Optional[dict]:
    try:
        now = time.time()
        for tok in [t for t, r in _SOLVED_RESULTS.items()
                    if now - float(r.get("at", 0)) > _SOLVED_RESULT_TTL]:
            _SOLVED_RESULTS.pop(tok, None)
        return _SOLVED_RESULTS.get(token)
    except Exception:
        return None

# Tiny 1x1 dark-gray JPEG (~600 bytes). Used as a placeholder when
# the CloakBrowser session isn't ready yet (still booting the CF
# interstitial, or the launch itself failed). Returning this instead
# of a 500 error means the user-facing page doesn't get stuck on
# "loading VPS browser…" forever — the status poller will tell the
# user what's actually going on via /status.
_PLACEHOLDER_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080607"
    "0707070909"
    "0a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837"
    "292c30313434341f2730393d38323c2e333432ffdb0043010909090c0b0c180d0d"
    "1832211c18325242423232323232323232323232323232323232323232323232323"
    "232323232323232323232323232323232323232323232323232323232323232323"
    "232323232323232323232323232323232ffc00011080080808080808080ff0000"
    "ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ffd9"
)

HTML_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Solve Cloudflare — xdmovies</title>
<style>
 body{font-family:system-ui,Arial;margin:0;padding:16px;background:#0f1419;color:#e6e6e6}
 h2{margin:0 0 8px} .host{color:#8ab4f8} img{max-width:100%;border:1px solid #333;border-radius:8px;cursor:pointer}
 .bar{margin:8px 0;padding:8px;background:#1a222b;border-radius:6px;font-size:14px}
 #status{margin-top:8px;font-size:14px;padding:8px;background:#1a222b;border-radius:6px}
 #status.ok{color:#7ee787} #status.err{color:#ff7b72} #status.info{color:#a0a0a0}
</style></head><body>
<h2>Tap to solve — <span class="host">{host}</span></h2>
<div class="bar">Target: <a href="{url}" target="_blank" style="color:#8ab4f8">{url}</a><br>
Tap the checkbox on the image if you see it. VPS will mirror your tap and auto-capture.</div>
<img id="shot" src="/solve/{token}/shot" alt="loading VPS browser…">
<div id="status" class="info">Connecting to VPS browser…</div>
<script>
const token="{token}";
function setStatus(text, kind){
  const el=document.getElementById('status');
  el.textContent=text;
  el.className=kind||'info';
}
function refreshShot(){
  const img=document.getElementById('shot');
  img.src='/solve/'+token+'/shot?t='+Date.now();
}
let shotTimer=null;
function stopStream(){ if(shotTimer!==null){ clearInterval(shotTimer); shotTimer=null; } }
function startStream(){ if(shotTimer===null){ refreshShot(); shotTimer=setInterval(refreshShot, 5000); } }
async function markActivity(){
  if(document.hidden) return;
  try{ await fetch('/solve/'+token+'/activity',{method:'POST',cache:'no-store'}); }catch(e){}
}
markActivity();
startStream();
const activityTimer=setInterval(markActivity, 30000);
document.addEventListener('visibilitychange', ()=>{
  markActivity();
  if(document.hidden){ stopStream(); } else { startStream(); }
});
window.addEventListener('pagehide', stopStream);
const statusTimer=setInterval(async ()=>{
  try{
    const r=await fetch('/solve/'+token+'/status');
    if(!r.ok){
      // Session is gone (destroyed after a successful solve, or expired).
      // Stop polling instead of hammering 404s in a loop.
      setStatus('✅ Solved or session expired — you can close this tab.', 'ok');
      clearInterval(shotTimer); clearInterval(statusTimer); clearInterval(activityTimer);
      return;
    }
    const j=await r.json();
    if(j.solved){
      setStatus('✅ Solved! You can close this tab.', 'ok');
      clearInterval(shotTimer); clearInterval(statusTimer); clearInterval(activityTimer);
      return;
    }
    let msg='⏳ Waiting — '+(j.title||'loading');
    if(j.goto_error){ msg += ' • VPS browser error: '+j.goto_error; }
    if(j.iframe_mode){
      msg += ' • CF mode: '+j.iframe_mode;
      if(j.iframe_mode==='non-interactive'||j.iframe_mode==='managed'){
        msg += ' (auto-capturing — tap the checkbox below if it appears)';
      }
    }
    if(j.auto_nudge_failed && !j.has_cookie){
      msg += ' • bot could not bypass — please tap the checkbox';
    } else if(j.has_cookie){
      msg += ' • cookie present';
    } else {
      msg += ' • waiting for cookie';
    }
    setStatus(msg, 'info');
  }catch(e){}
}, 3000);
document.getElementById('shot').addEventListener('click', async (e)=>{
  const r=e.target.getBoundingClientRect();
  const natW=e.target.naturalWidth, natH=e.target.naturalHeight;
  const x=Math.round(((e.clientX - r.left)/r.width)*natW);
  const y=Math.round(((e.clientY - r.top)/r.height)*natH);
  setStatus('↗ clicking VPS @'+x+','+y+'…', 'info');
  markActivity();
  try{
    const resp=await fetch('/solve/'+token+'/click', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({x,y})});
    const j=await resp.json().catch(()=>({ok:false,error:'bad response'}));
    if(j.ok){
      setStatus('✅ click sent ('+j.mode+') — waiting for cookie…', 'ok');
    }else{
      setStatus('❌ click failed: '+(j.error||'unknown'), 'err');
    }
  }catch(e){
    setStatus('❌ network error: '+e, 'err');
  }
});
</script></body></html>
"""

async def _poll_for_cookie(sess: SolveSession):
    try:
        # Domain match: accept cookies whose domain is the host itself,
        # a parent of the host, or any subdomain under xdmovies.wtf.
        # CF often sets domain=".top.xdmovies.wtf" not ".xdmovies.wtf",
        # which the old check missed.
        def _domain_ok(cookie_domain: str, target_host: str) -> bool:
            d = (cookie_domain or "").lstrip(".").lower()
            h = (target_host or "").lower()
            if not d or not h:
                return False
            if d == h:
                return True
            if h.endswith("." + d):
                return True
            if d.endswith("." + h):
                return True
            # Treat any *.xdmovies.wtf as matching xdmovies.wtf and vice versa
            xd = "xdmovies.wtf"
            if d == xd or h == xd or d.endswith("." + xd) or h.endswith("." + xd):
                return True
            return False

        # Snapshot the cf_clearance value(s) we PRE-INJECTED into the
        # browser context at launch time. Without this, the very first
        # poll tick would mark the session "solved" by reading back the
        # stale stored cookie — the user never gets a chance to click
        # and the bot then retries the same blocked page.
        pre_injected: set[str] = set()
        try:
            if sess.context is not None:
                for c in await sess.context.cookies():
                    if c.get("name") == "cf_clearance":
                        v = c.get("value") or ""
                        if v:
                            pre_injected.add(v)
        except Exception:
            pass
        logger.debug(f"[solve_server] pre-injected cf_clearance values: {len(pre_injected)}")

        while not sess.solved and _SESSIONS.get(sess.token) is sess:
            try:
                if sess.context is None:
                    await asyncio.sleep(1.2)
                    continue
                cookies = await sess.context.cookies()
                for c in cookies:
                    if c.get("name") != "cf_clearance":
                        continue
                    val = c.get("value")
                    if not val or len(val) <= 20:
                        continue
                    # Audit finding H3: the previous code had
                    # a 33s window where a pre-injected (stale)
                    # cookie was treated as a fresh solve if the
                    # user took >33s to open the link — they'd
                    # get a false "solved" with the same stale
                    # cookie and the bot would retry against it
                    # indefinitely. Pre-injected values are NEVER
                    # accepted as solved.
                    if val in pre_injected:
                        # Same value as what we injected. The
                        # user hasn't solved anything new.
                        continue
                    # NOTE (latestnewsonline.sbs gate fix): CF
                    # sometimes issues cf_clearance on domain
                    # ".cloudflare.com" instead of the site
                    # domain (documented in CONTEXT_stealth.md).
                    # The old _domain_ok() gate rejected those,
                    # so a successful manual tap on the gate
                    # never counted as solved and the bot
                    # reported "still blocked" after 90s. Each
                    # solve session owns a dedicated browser
                    # context that only ever visits sess.url,
                    # so any fresh cf_clearance here belongs to
                    # this flow regardless of cookie domain.
                    # We still log the domain for diagnostics.
                    if not _domain_ok(c.get("domain", ""), sess.host):
                        logger.info(
                            f"[solve_server] accepting cf_clearance with "
                            f"cookie-domain={c.get('domain')} for host={sess.host} "
                            f"(cross-domain issue, value is fresh)"
                        )
                    try:
                        # Audit finding H7: cf_clearance is bound
                        # to IP+UA. The solve browser uses CloakBrowser's
                        # default UA, but cookie_store was being
                        # called without user_agent, so a stale
                        # `_user_agent` from an earlier manual paste
                        # persisted and the next pipeline/link_exec
                        # replayed it via get_user_agent_for_url,
                        # causing UA mismatch and CF rejection.
                        # Capture the actual UA from the solve
                        # browser and store it with the cookie.
                        ua = None
                        try:
                            if sess.page is not None:
                                ua = await sess.page.evaluate("navigator.userAgent")
                        except Exception as e:
                            logger.debug(f"[solve_server] UA capture failed: {e!r}")
                        set_cookies_for_domain(
                            sess.host,
                            f"cf_clearance={val}",
                            user_agent=ua,
                        )
                        sess.cf_value = val
                        sess.solved = True
                        sess.event.set()
                        _stash_solved_result(sess.token, cf_value=val)
                        logger.info(
                            f"[solve_server] auto-captured NEW cf_clearance for {sess.host} "
                            f"(cookie-domain={c.get('domain')}) token={sess.token[:8]} "
                            f"pre_injected={val in pre_injected}"
                        )
                    except Exception as e:
                        logger.warning(f"[solve_server] auto-save failed: {e}")
                    break
            except asyncio.CancelledError:
                break
            except Exception:
                pass
            await asyncio.sleep(1.2)
    except asyncio.CancelledError:
        pass

async def _auto_nudge(sess: SolveSession) -> bool:
    """Try to bypass CF without user input.

    Strategy: wait a few seconds for the page to settle (CF's "managed"
    / "light" mode auto-verifies in <10s once the stealth patches are
    in place). If a *fresh* cf_clearance appears, we're done. If not,
    find the Turnstile iframe and try a single nudge-click at the
    typical checkbox coordinates (30, 32). If that still doesn't yield
    a *fresh* cf_clearance, give up and set `sess.auto_nudge_failed=True`
    so the bot can fall back to the manual token link.

    Returns True if a NEW cf_clearance was captured automatically.
    Returns False if the user needs to solve manually.

    Audit finding H4: the previous version returned True on ANY
    cf_clearance longer than 20 chars, including the pre-injected
    stale value. So when a user came in with a pre-injected cookie
    already past CF (the common steady-state), the nudge always
    "succeeded" and `auto_nudge_failed` stayed False — even if the
    cookie was about to expire. Now we capture the pre-injected set
    once and only count cookies whose value is NOT in that set.
    """
    # Capture the cookies already present BEFORE we start nudging.
    pre_injected: set[str] = set()
    try:
        if sess.context is not None:
            for c in await sess.context.cookies():
                if c.get("name") == "cf_clearance":
                    pre_injected.add(c.get("value") or "")
    except Exception:
        pass

    async def has_fresh_cookie() -> bool:
        """True if a cf_clearance exists that wasn't in pre_injected."""
        try:
            if sess.context is None:
                return False
            for c in await sess.context.cookies():
                if c.get("name") != "cf_clearance":
                    continue
                val = c.get("value") or ""
                if not val or len(val) <= 20:
                    continue
                if val not in pre_injected:
                    return True
        except Exception:
            return False
        return False

    # Step 1: give the page a chance to auto-verify.
    for _ in range(8):  # up to ~8 seconds
        if await has_fresh_cookie():
            return True
        await asyncio.sleep(1.0)

    # Step 2: try a single nudge-click on the Turnstile iframe.
    try:
        if sess.page is None:
            return False
        for fr in sess.page.frames:
            try:
                u = (fr.url or "").lower()
            except Exception:
                continue
            if "challenges.cloudflare.com" in u and "turnstile" in u:
                try:
                    await fr.click("body", position={"x": 30, "y": 32}, delay=80, timeout=5000)
                    logger.info("[solve_server] auto-nudged Turnstile frame at 30,32")
                except Exception as e:
                    logger.debug(f"[solve_server] auto-nudge click failed: {e!r}")
                break
    except Exception as e:
        logger.debug(f"[solve_server] auto-nudge frame search failed: {e!r}")

    # Step 3: wait up to ~8 more seconds for a FRESH cookie to appear.
    for _ in range(8):
        if await has_fresh_cookie():
            return True
        await asyncio.sleep(1.0)

    return False


async def _launch_for_session(sess: SolveSession):
    """Single-shot auto-solve. Try `humanize=True` (stealth patches +
    fingerprint noise) so Cloudflare's own JS handles the challenge
    in light/non-interactive mode. If the auto-nudge is enough, the
    bot captures `cf_clearance` without any user interaction.

    If the auto-nudge doesn't work (browser crashed, CF escalated to
    a managed challenge, or the cookie didn't appear within ~20s),
    we set `sess.auto_nudge_failed=True` and the page stays live so
    the user can solve it manually via the token link the bot sends.
    """
    from xdmovies.cloak_stealth import launch_chromium
    try:
        browser = await launch_chromium(
            browser_type="chromium",
            headless=False,
            useragent=None,
            humanize=True,
        )
    except Exception as e:
        sess.goto_error = f"browser launch failed: {e!r}"
        logger.exception("[solve_server] browser launch failed")
        return
    sess.browser = browser
    try:
        ctx = await browser.new_context(viewport={"width": 1280, "height": 800})
    except Exception:
        ctx = browser
    # Gate policy: NEVER block or hide ads on latestnewsonline.sbs.
    # The gate verifies its ad flow (impressions, popups, modal state);
    # nuking overlays, hiding modals, or neutering window.open lets us
    # SEE the buttons but the gate then refuses to generate the link and
    # never reaches the final destination URL. So on gate URLs we apply
    # only the stealth fingerprint plugin (not ad-blocking) and use real
    # trusted clicks. Tabs that are pure about:blank are still reaped by
    # _close_ad_popups; destination tabs are preserved.
    is_gate = "latestnewsonline.sbs" in (sess.url or "").lower()
    if is_gate:
        try:
            from xdmovies.browser_stealth import is_chromium_family, stealth_plugin
            if is_chromium_family("chromium"):
                plugin = stealth_plugin()
                if plugin is not None:
                    init = getattr(plugin, "init_script", None) or getattr(plugin, "init_scripts", None)
                    if isinstance(init, list):
                        for s in init:
                            try: await ctx.add_init_script(s)
                            except: pass
                    elif init:
                        try: await ctx.add_init_script(init)
                        except: pass
        except Exception as e:
            logger.debug(f"[solve_server] could not apply stealth plugin: {e!r}")
    try:
        from xdmovies.cookie_store import get_cookies_for_url
        cookies = get_cookies_for_url(sess.url)
        if cookies:
            try: await ctx.add_cookies(cookies)
            except Exception:
                try: await ctx.add_cookies([{**c, "domain": c["domain"].lstrip(".")} for c in cookies])
                except: pass
    except: pass
    sess.context = ctx
    try:
        page = await ctx.new_page()
    except Exception as e:
        # Most common case: humanize patch + Xvfb + libnss3 mismatch
        # crashed the renderer before Playwright could attach.
        sess.goto_error = f"new_page failed: {e!r}"
        logger.exception("[solve_server] new_page failed")
        try: await browser.close()
        except Exception: pass
        sess.browser = None
        sess.context = None
        return
    sess.page = page
    media_pattern = r"\.(?:mp4|webm|ogg|mp3|m4a|wav|woff2?|ttf|eot)(?:[?#]|$)"
    if not is_gate or BLOCK_GATE_IMAGES:
        media_pattern = r"\.(?:png|jpe?g|gif|webp|svg|ico|css|mp4|webm|ogg|mp3|m4a|wav|woff2?|ttf|eot)(?:[?#]|$)"
    async def _abort_media(route):
        try:
            request_url = str(getattr(getattr(route, "request", None), "url", "") or "").lower()
            if "challenges.cloudflare.com" in request_url:
                await route.continue_()
                return
            await route.abort()
        except Exception:
            pass
    try:
        await ctx.route(re.compile(media_pattern, re.IGNORECASE), _abort_media)
    except Exception as exc:
        logger.warning(f"[solve_server] media blocking failed: {exc!r}")
    try:
        # Cap goto at 30s. CF interstitials with light Turnstile auto-verify
        # in <10s, so 30s is generous.
        await page.goto(sess.url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        sess.goto_error = f"goto failed for {e!r}"
        logger.warning(f"[solve_server] goto failed for {sess.url}: {e}")
    # Gate auto-drive: if this is a latestnewsonline.sbs URL, drive
    # Generate -> Continue -> Turnstile container automatically. Clicks
    # are synthetic-first (proven live 2026-09-04: main.js handlers check
    # nothing about isTrusted, dispatch sails through covering iframes,
    # and no pop-under opens), real trusted mouse as fallback. The gate
    # requires ads loaded AND unhidden, so nothing is ever hidden via DOM
    # and window.open is left intact. User only taps Close on covering
    # modals and the inner Turnstile checkbox via the screenshot.
    if is_gate:
        try:
            from xdmovies.link_exec import (
                _wait_visible,
                _close_ad_popups,
                _wait_for_ad_impressions,
            )
            # Wait for the gate UI to settle (Loading title clears, generateBtn unhides).
            deadline_settle = time.time() + 15.0
            while time.time() < deadline_settle:
                try:
                    t = (await page.title()) or ""
                except Exception:
                    t = ""
                if "Loading" not in t and "Just a moment" not in t:
                    try:
                        if await page.evaluate(
                            "() => { const b=document.getElementById('generateBtn'); return !!(b && !b.classList.contains('hidden') && !b.disabled); }"
                        ):
                            break
                    except Exception:
                        pass
                await asyncio.sleep(0.5)
            # Watch ads before clicking Generate. The gate has timers
            # that must complete before the Turnstile can be solved.
            try:
                await _wait_for_ad_impressions(page, max_wait=1.5)
            except Exception:
                pass
            # STEP 1: Generate. The first click is routinely eaten by a
            # pop-under ad tab (gate tab keeps focus, counter never
            # starts), so click, reap the new tabs, and re-click until
            # the counter visibly starts. A covering in-page modal gets
            # its Close trusted-clicked first -- never hidden via DOM.
            gen_ok, gen_dest = await _gate_click_step(
                page, ctx, "#generateBtn",
                # Source-truth (main.js): a landed Generate click hides
                # #generateBtn, shows #counterWrapper with title
                # "Please Wait...", then "Step 1 Complete!" + #continueBtn.
                "() => { const c=document.getElementById('continueBtn');"
                " if (c && !c.classList.contains('hidden')) return true;"
                " try { const dt=(document.title||'').toLowerCase();"
                " if (dt.indexOf('please wait') !== -1 || dt.indexOf('step 1 complete') !== -1) return true; } catch (_) {}"
                " const cw=document.getElementById('counterWrapper');"
                " if (cw && !cw.classList.contains('hidden')) return true;"
                " const t=(document.body.innerText||'').toLowerCase();"
                " if (t.indexOf('step 1 complete') !== -1) return true;"
                " const g=document.getElementById('generateBtn');"
                " if (g && g.classList.contains('hidden')) return true;"
                " return false; }",
                "Generate",
            )
            if gen_dest:
                _finish_gate_solve(sess, gen_dest, "after-generate")
            # Keep-focused counter wait: poll for #continueBtn unhiding
            # while closing popups and refocusing if paused-notice
            # appears.
            if not sess.solved and gen_ok:
                deadline_counter = time.time() + 30.0
                last_paused = False
                while time.time() < deadline_counter:
                    try:
                        await _close_ad_popups(ctx, page)
                    except Exception:
                        pass
                    try:
                        paused = await page.evaluate(
                            "() => { const n=document.getElementById('pausedNotice'); return !!(n && !n.classList.contains('hidden')); }"
                        )
                    except Exception:
                        paused = False
                    if paused and not last_paused:
                        try:
                            await page.bring_to_front()
                        except Exception:
                            pass
                    last_paused = paused
                    try:
                        if await page.evaluate(
                            "() => { const b=document.getElementById('continueBtn'); return !!(b && !b.classList.contains('hidden')); }"
                        ):
                            break
                    except Exception:
                        pass
                    await asyncio.sleep(1.0)
            # STEP 2: Continue. Same eat-and-reclick pattern: the click
            # that opens the second pop-under does not advance the gate,
            # so close the new tabs and click again until step 2 starts
            # (Turnstile unhides or the URL flips to step=2).
            cont_ok, cont_dest = (False, None)
            if not sess.solved:
                cont_ok, cont_dest = await _gate_click_step(
                    page, ctx, "#continueBtn",
                    # Source-truth (main.js): Continue does a FULL reload
                    # to /r/<code>?step=2&sid=... ("Almost There..."), so
                    # the step=2 URL is the strongest proof the click
                    # landed; Turnstile unhide follows after its timer.
                    "() => { try { if ((window.location.href||'').indexOf('step=2') !== -1) return true; } catch (_) {}"
                    " try { if ((document.title||'').toLowerCase().indexOf('almost there') !== -1) return true; } catch (_) {}"
                    " const t=document.getElementById('turnstileContainer');"
                    " if (t && !t.classList.contains('hidden')) return true;"
                    " const b=(document.body.innerText||'').toLowerCase();"
                    " if (b.indexOf('almost there') !== -1 || b.indexOf('step 2 of 2') !== -1) return true;"
                    " return false; }",
                    "Continue",
                )
                if cont_dest:
                    _finish_gate_solve(sess, cont_dest, "after-continue")
            if not sess.solved and cont_ok:
                try:
                    await page.wait_for_load_state("load", timeout=30000)
                except Exception:
                    pass
                # Wait for Turnstile container.
                deadline_ts = time.time() + 30.0
                while time.time() < deadline_ts:
                    try:
                        await page.bring_to_front()
                    except Exception:
                        pass
                    try:
                        if await page.evaluate(
                            "() => { const t=document.getElementById('turnstileContainer'); return !!(t && !t.classList.contains('hidden')); }"
                        ):
                            logger.info("[solve_server] gate auto-drive: Turnstile container ready")
                            await _drive_turnstile_stage(page, sess, max_wait=90.0)
                            break
                    except Exception:
                        pass
                    await asyncio.sleep(1.0)
            elif not sess.solved:
                logger.warning("[solve_server] gate auto-drive: Continue never advanced to step 2")
            # STEP 3: Go to Link. After Turnstile Success the button
            # enables; it also spawns a pop-under, so click it, reap new
            # tabs, and capture the destination tab URL as the solve
            # result (the inner captcha issues no cf_clearance, so only
            # the destination completes this session).
            if not sess.solved:
                try:
                    deadline_go = time.time() + 90.0
                    go_enabled = False
                    while time.time() < deadline_go:
                        if sess.solved:
                            break
                        try:
                            if await page.evaluate(
                                "() => { const b=document.getElementById('goToLinkBtn');"
                                " return !!(b && !b.classList.contains('hidden') && !b.disabled); }"
                            ):
                                go_enabled = True
                                break
                        except Exception:
                            pass
                        await asyncio.sleep(1.0)
                    if go_enabled and not sess.solved:
                        try:
                            covered, note = await _gate_button_covered(page, "#goToLinkBtn")
                            if covered:
                                logger.info(f"[solve_server] gate: Go covered by [{note}] -- closing modal first")
                                await _click_covering_modal_close(page)
                                await asyncio.sleep(0.8)
                        except Exception:
                            pass
                        logger.info("[solve_server] gate auto-drive: clicking Go to Link")
                        # Primary capture: the gate's own complete API. Per
                        # main.js the Go handler POSTs /api/session/complete
                        # {fingerprint, turnstileToken} and the response
                        # carries {token}; the destination is then
                        # /go/<sid>?t=<token>. Listening for that response
                        # is deterministic proof the click registered
                        # server-side, with the exact token even if the
                        # same-tab navigation is slow or interrupted.
                        # Synthetic first (proven live), trusted fallback.
                        go_clicked = False
                        complete_token = None
                        try:
                            pre_url = page.url or ""
                        except Exception:
                            pre_url = ""
                        try:
                            from xdmovies.link_exec import _synthetic_click as _syn_go
                            try:
                                async with page.expect_response(
                                    "**/api/session/complete", timeout=15000
                                ) as _resp_info:
                                    go_clicked = bool(await _syn_go(page, "#goToLinkBtn"))
                                    if not go_clicked:
                                        try:
                                            go_clicked = bool(await _trusted_click(page, "#goToLinkBtn"))
                                        except Exception:
                                            pass
                                try:
                                    _resp = await _resp_info.value
                                    _body = await _resp.json()
                                    if isinstance(_body, dict) and _body.get("token"):
                                        complete_token = str(_body["token"])
                                        logger.info("[solve_server] gate: complete API issued token")
                                    elif isinstance(_body, dict) and _body.get("error"):
                                        logger.warning(f"[solve_server] gate: complete API error: {_body.get('error')}")
                                except Exception as exc_resp:
                                    logger.debug(f"[solve_server] gate: complete response unreadable: {exc_resp!r}")
                            except Exception as exc_wait:
                                # No complete request observed (click missed
                                # or handler never fired). Fall through to a
                                # plain trusted click below.
                                logger.debug(f"[solve_server] gate: no complete response yet: {exc_wait!r}")
                        except Exception:
                            pass
                        if not go_clicked:
                            try:
                                go_clicked = bool(await _trusted_click(page, "#goToLinkBtn"))
                            except Exception:
                                pass
                        if go_clicked:
                            # Reconstruct the destination the page itself
                            # navigates to (main.js: location.href =
                            # `/go/${sessionId}?t=${token}`). Used when the
                            # navigation is not observed in the poll window.
                            constructed = None
                            if complete_token:
                                try:
                                    from urllib.parse import urlparse, parse_qs
                                    _pu = urlparse(pre_url or page.url or "")
                                    _sid = (parse_qs(_pu.query).get("sid") or [""])[0]
                                    if _pu.netloc and _sid:
                                        constructed = (
                                            f"{_pu.scheme or 'https'}://{_pu.netloc}"
                                            f"/go/{_sid}?t={complete_token}"
                                        )
                                        logger.info("[solve_server] gate: built destination from complete token")
                                except Exception as exc_mk:
                                    logger.debug(f"[solve_server] gate: destination build failed: {exc_mk!r}")
                            # Source-truth (main.js): Go navigates the SAME
                            # tab to /go/<sid>?t=... (deliberate
                            # anti-popup-blocker design) while also spawning
                            # a pop-under ad tab. So poll the gate tab URL
                            # for the /go/ transit or a dest host AND scan
                            # tabs; first dest-host hit wins, /go/ transit
                            # is the fallback.
                            dest = None
                            try:
                                from xdmovies.link_exec import (
                                    _extract_destination, _is_destination_url, _is_ad_url,
                                )
                                transit = None
                                deadline_nav = time.time() + 25.0
                                while time.time() < deadline_nav and not dest and not sess.solved:
                                    try:
                                        cur = page.url or ""
                                    except Exception:
                                        cur = ""
                                    if cur and not _is_ad_url(cur):
                                        if _is_destination_url(cur):
                                            dest = cur
                                            break
                                        if "/go/" in cur and not transit:
                                            transit = cur
                                    try:
                                        tab_dest = await _close_ad_popups(ctx, page)
                                        if tab_dest:
                                            dest = tab_dest
                                            break
                                    except Exception:
                                        pass
                                    await asyncio.sleep(1.0)
                                if not dest:
                                    dest = transit
                                if not dest:
                                    dest = constructed
                                if not dest:
                                    try:
                                        dest = await _extract_destination(page, ctx)
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                            if dest:
                                # If the solve browser itself sits on the hub
                                # page, parse its title/size/final URL into
                                # the shared cache (zero extra network; works
                                # behind CF where plain HTTPS fetches fail).
                                try:
                                    from xdmovies.link_exec import note_hub_details as _note
                                    from xdmovies.extractor import extract_hub_details as _ehd
                                    try:
                                        _cur = page.url or ""
                                    except Exception:
                                        _cur = ""
                                    if _cur and ("hubcloud" in _cur or "hubdrive" in _cur):
                                        try:
                                            _hd = _ehd(await page.content() or "")
                                        except Exception:
                                            _hd = None
                                        if _hd is not None and (_hd.title or _hd.final_url):
                                            _note(_cur, _hd.final_url, _hd.title, _hd.size)
                                except Exception:
                                    pass
                                _finish_gate_solve(sess, dest, "go-to-link")
                            else:
                                logger.warning("[solve_server] gate auto-drive: Go clicked but no destination found")
                except Exception as exc_go:
                    logger.warning(f"[solve_server] gate Go-click failed: {exc_go!r}")
        except Exception as exc:
            logger.warning(f"[solve_server] gate auto-drive failed (user can still click manually): {exc!r}")
    # Always start the poller — it picks up cf_clearance as soon as
    # the page lands.
    sess.poll_task = asyncio.create_task(_poll_for_cookie(sess))
    # Try the auto-nudge in the background; result is reflected in
    # sess.auto_nudge_failed when the user opens the token link.
    sess.auto_nudge_task = asyncio.create_task(_run_auto_nudge(sess))


async def _run_auto_nudge(sess: SolveSession) -> None:
    try:
        # Short delay so the poller has time to see the pre-injected
        # cookie state before we start trying to bypass.
        await asyncio.sleep(2.0)
        ok = await _auto_nudge(sess)
        if ok:
            logger.info(f"[solve_server] auto-nudge succeeded for {sess.url}")
        else:
            sess.auto_nudge_failed = True
            logger.info(f"[solve_server] auto-nudge did not capture cookie within ~20s; "
                       f"user fallback required")
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.warning(f"[solve_server] auto_nudge error: {e!r}")
        sess.auto_nudge_failed = True

async def handle_solve_page(request):
    token = request.match_info.get("token", "")
    sess = _SESSIONS.get(token)
    if not sess:
        # Friendly expiry page (was bare "Invalid or expired solve
        # link" text). Sessions close after one hour without viewer activity.
        return web.Response(
            text=(
                "<!doctype html><html><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>Solve link expired</title>"
                "<style>body{font-family:system-ui;margin:0;padding:24px;background:#0f1419;color:#e6e6e6}"
                "h2{margin:0 0 8px}.box{background:#1a222b;border-radius:8px;padding:16px;margin-top:12px}</style>"
                "</head><body><h2>⏰ This solve link has expired</h2>"
                "<div class='box'>Solve links close after one hour without viewer activity. "
                "Go back to Telegram and send the movie URL again — "
                "the bot will issue a fresh link.</div></body></html>"
            ),
            content_type="text/html",
            status=410,
        )
    # Use .replace() instead of .format() because HTML_TEMPLATE contains literal
    # CSS/JS braces (e.g. body{font-family:...}) which would crash str.format()
    # with KeyError. Only substitute our 3 known placeholders.
    _touch_viewer(sess)
    html = (
        HTML_TEMPLATE
        .replace("{token}", token)
        .replace("{host}", sess.host)
        .replace("{url}", sess.url)
    )
    return web.Response(text=html, content_type="text/html")

async def handle_activity(request):
    token = request.match_info.get("token", "")
    sess = _SESSIONS.get(token)
    if not sess:
        return web.json_response({"ok": False}, status=404)
    _touch_viewer(sess)
    return web.json_response({"ok": True, "idle_seconds": int(_session_idle_seconds(sess))})

async def handle_shot(request):
    token = request.match_info.get("token", "")
    sess = _SESSIONS.get(token)
    if not sess:
        return web.Response(status=404)
    if sess.page is None or sess.solved:
        # Browser not yet ready (CloakBrowser may be still booting the
        # CF interstitial, or the launch itself failed) — or already
        # solved and torn down by wait_for_solve (C5 fix destroys the
        # session right after capture, so an in-flight screenshot would
        # otherwise race the close and log TargetClosedError). Return a
        # 1x1 gray JPEG with a short Cache-Control so the page can show
        # it and the status poller keeps the user informed.
        return web.Response(
            body=_PLACEHOLDER_JPEG,
            content_type="image/jpeg",
            headers={"Cache-Control": "no-store, max-age=1"},
        )
    now = time.time()
    if not _viewer_is_active(sess, now):
        return web.Response(
            body=_PLACEHOLDER_JPEG,
            content_type="image/jpeg",
            headers={"Cache-Control": "no-store, max-age=1"},
        )
    if now - float(sess.last_shot_at or 0.0) < SCREENSHOT_MIN_INTERVAL:
        return web.Response(
            body=_PLACEHOLDER_JPEG,
            content_type="image/jpeg",
            headers={"Cache-Control": "no-store, max-age=1"},
        )
    sess.last_shot_at = now
    try:
        # Hard cap on screenshot so a hung page never blocks the
        # browser-side (the user's phone) for more than a few seconds.
        png = await asyncio.wait_for(
            sess.page.screenshot(type="jpeg", quality=SCREENSHOT_QUALITY, full_page=False),
            timeout=4.0,
        )
        return web.Response(body=png, content_type="image/jpeg", headers={"Cache-Control": "no-store"})
    except asyncio.TimeoutError:
        logger.warning(f"[solve_server] screenshot timed out for {sess.url}")
        return web.Response(
            body=_PLACEHOLDER_JPEG,
            content_type="image/jpeg",
            headers={"Cache-Control": "no-store, max-age=1"},
        )
    except Exception as e:
        # TargetClosedError here is expected when the browser was just
        # closed after a successful solve (destroy races an in-flight
        # shot). Keep it at debug so the log doesn't cry wolf.
        if "TargetClosedError" in type(e).__name__ or "TargetClosedError" in str(e):
            logger.debug(f"[solve_server] screenshot raced browser close (benign): {e!r}")
        else:
            logger.warning(f"[solve_server] screenshot failed: {e!r}")
        return web.Response(
            body=_PLACEHOLDER_JPEG,
            content_type="image/jpeg",
            headers={"Cache-Control": "no-store, max-age=1"},
        )

async def _find_turnstile_frame(page):
    """Locate the Cloudflare Turnstile/challenges iframe inside the page.

    Cloudflare renders the actual checkbox inside a cross-origin iframe
    hosted at challenges.cloudflare.com. Clicking on the top-level page
    with page.mouse.click never reaches that checkbox — we have to
    target the frame itself.
    """
    try:
        for fr in page.frames:
            try:
                u = (fr.url or "").lower()
            except Exception:
                u = ""
            if "challenges.cloudflare.com" in u or "turnstile" in u:
                return fr
    except Exception:
        pass
    return None


async def _auto_solve_iframe(frame) -> bool:
    """If the Turnstile iframe exposes a checkbox/input, click it directly
    via locator (no coordinate math). Works for visible-light challenges
    that have a real clickable widget inside the iframe.
    """
    candidates = [
        "input[type='checkbox']",
        "label.ctp-checkbox-label",
        ".cb-lb",
        "input",
        "[role='checkbox']",
        "button",
    ]
    for sel in candidates:
        try:
            loc = frame.locator(sel).first
            if await loc.count() == 0:
                continue
            box = await loc.bounding_box()
            if not box or box["width"] == 0 or box["height"] == 0:
                continue
            try:
                await loc.click(force=True, timeout=3000)
                return True
            except Exception:
                # Cross-origin: try mouse at box center
                cx = box["x"] + box["width"] / 2
                cy = box["y"] + box["height"] / 2
                # frame-relative mouse via element handle:
                try:
                    el = await loc.element_handle()
                    if el:
                        await el.click(force=True, timeout=3000)
                        return True
                except Exception:
                    return False
        except Exception:
            continue
    return False


async def _dismiss_gate_overlays(page) -> int:
    """INTENTIONALLY UNUSED -- kept for reference only.

    Hiding the gate's "Download is ready / Tap to proceed" modals lets
    us SEE the Turnstile checkbox, but the gate verifies its ad flow and
    then refuses to generate the link, so we never reach the final
    destination URL. Policy: never block or hide ads on gate URLs; tap
    Close on the screenshot so the site's own handlers run instead.
    """
    try:
        return int(await page.evaluate(r"""() => {
            let dismissed = 0;
            const hide = (el) => {
                try {
                    el.style.setProperty('display', 'none', 'important');
                    el.style.setProperty('pointer-events', 'none', 'important');
                    el.style.setProperty('visibility', 'hidden', 'important');
                } catch (_) {}
            };
            const isProtected = (el) => {
                try {
                    if (!el || !el.querySelector) return false;
                    if (el.querySelector('#turnstileContainer, #goToLinkBtn, #generateBtn, #continueBtn')) return true;
                    const id = (el.id || '');
                    if (id === 'turnstileContainer' || id === 'goToLinkBtn' || id === 'generateBtn' || id === 'continueBtn') return true;
                } catch (_) {}
                return false;
            };
            // 1) Click Close/Dismiss buttons inside Download-ready modals.
            try {
                const btns = Array.from(document.querySelectorAll('button'));
                for (const b of btns) {
                    let bt = '';
                    try { bt = ((b.innerText || b.textContent) || '').trim().toLowerCase(); } catch (_) {}
                    if (bt !== 'close' && bt !== 'dismiss' && bt !== 'no thanks' && bt !== 'x') continue;
                    try {
                        const r = b.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) continue;
                    } catch (_) { continue; }
                    // Only click Close buttons that live inside an ad modal.
                    let scope = b;
                    let inAdModal = false;
                    try {
                        for (let i = 0; i < 5 && scope; i++) {
                            const t = ((scope.innerText || '') + ' ' + (scope.textContent || '')).slice(0, 300);
                            if (t.includes('Download is ready') || t.includes('Tap to proceed')) { inAdModal = true; break; }
                            scope = scope.parentElement;
                        }
                    } catch (_) {}
                    if (!inAdModal) continue;
                    try { b.click(); dismissed++; } catch (_) {}
                }
            } catch (_) {}
            // 2) Hide leftover modal containers holding that text.
            try {
                const all = Array.from(document.querySelectorAll('div, section, aside'));
                for (const el of all) {
                    if (isProtected(el)) continue;
                    let t = '';
                    try { t = ((el.innerText || '') + ' ' + (el.textContent || '')).slice(0, 300); } catch (_) {}
                    if (!t || (!t.includes('Download is ready') && !t.includes('Tap to proceed'))) continue;
                    if (el.querySelector && el.querySelector('#turnstileContainer')) continue;
                    // Only hide leaf-ish modal boxes, not <body>/huge wrappers.
                    let innerBtns = 0;
                    try { innerBtns = el.querySelectorAll('button').length; } catch (_) {}
                    if (innerBtns === 0) continue;
                    hide(el);
                    dismissed++;
                    if (dismissed > 6) break;
                }
            } catch (_) {}
            return dismissed;
        }""") or 0)
    except Exception:
        return 0


async def _trusted_click(page, selector: str, max_wait: float = 12.0) -> bool:
    """Real trusted mouse click: no DOM hiding, no synthetic events.

    Used for ad-network controls (modal Close buttons) and as the
    fallback for gate buttons when a synthetic dispatch does not take.
    Waits for visible + enabled, scrolls into view, then issues a
    humanized mouse click. Returns False instead of faking a click.
    """
    try:
        loc = page.locator(selector)
        await loc.wait_for(state="visible", timeout=int(max_wait * 1000))
        try:
            await page.bring_to_front()
        except Exception:
            pass
        try:
            await loc.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass
        box = await loc.bounding_box()
        if not box:
            return False
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        await page.mouse.move(cx, cy, steps=6)
        await page.mouse.click(cx, cy, delay=120)
        return True
    except Exception:
        return False


async def _gate_button_covered(page, selector: str) -> tuple:
    """Read-only check: is an in-page ad modal covering the gate button?

    Uses elementsFromPoint at the button center. Returns (covered, note).
    A fullscreen ad iframe counts as covering even where it looks
    transparent: it still eats real mouse clicks (only its own Close
    dismisses it, after which the ad script removes the overlay -- the
    manual flow proves the path clears). Nothing is written here.
    """
    try:
        info = await page.evaluate(r"""(sel) => {
            const b = document.querySelector(sel);
            if (!b) return {found: false};
            const r = b.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) return {found: true, covered: true, note: 'no_box'};
            const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
            const els = (document.elementsFromPoint ? document.elementsFromPoint(cx, cy) : []);
            for (const e of els) {
                if (e === b || b.contains(e)) return {found: true, covered: false, note: ''};
                const tag = (e.tagName || '').toLowerCase();
                if (tag === 'html' || tag === 'body' || tag === 'head') continue;
                let t = '';
                try {
                    if (tag === 'iframe') { t = 'src=' + ((e.src || '') + '').slice(-40); }
                    else { t = ((e.innerText || e.textContent) || '').slice(0, 60); }
                } catch (_) {}
                return {found: true, covered: true, note: (tag + ' ' + t).slice(0, 80)};
            }
            return {found: true, covered: false, note: ''};
        }""", selector) or {}
    except Exception:
        return (False, "")
    if not info.get("found"):
        return (False, "not_found")
    return (bool(info.get("covered")), str(info.get("note") or ""))


async def _click_covering_modal_close(page) -> bool:
    """Trusted-click the grey Close control of a covering ad modal.

    Two shapes exist in the wild:
    1. Main-document <button>Close</button> inside a modal overlay.
    2. The real fronter modal (proved 2026-09-04 via inspector): a
       FULL-PAGE overlay iframe whose Close is a <span> reading exactly
       "Close" (sibling: blue "Continue" span, title "Download:" /
       "Finish download?"). A main-doc button search can never see it.

    Targets ONLY exact-text Close/Dismiss/No-thanks controls. The blue
    Continue-style controls are ads themselves and are never touched.
    Real trusted clicks so the site's own dismiss handler runs; nothing
    hidden via DOM. The 8s actionability timeout rides out the modal's
    ~6s showButtonTimerText entrance animation.
    """
    # Path 1: main-document buttons (button-based modals).
    try:
        pt = await page.evaluate(r"""() => {
            const norm = (s) => ((s || '').trim().toLowerCase());
            const btns = Array.from(document.querySelectorAll('button'));
            for (const b of btns) {
                const t = norm(b.innerText || b.textContent);
                if (t !== 'close' && t !== 'dismiss' && t !== 'no thanks' && t !== 'x') continue;
                let r;
                try { r = b.getBoundingClientRect(); } catch (_) { continue; }
                if (!r || r.width < 20 || r.height < 10) continue;
                try {
                    const st = window.getComputedStyle(b);
                    if (st.display === 'none' || st.visibility === 'hidden') continue;
                } catch (_) {}
                let scope = b.parentElement, inModal = false, depth = 0;
                while (scope && depth < 6) {
                    try {
                        const s = window.getComputedStyle(scope);
                        const txt = ((scope.innerText || '') + ' ' + (scope.textContent || '')).slice(0, 400);
                        if ((s.position === 'fixed' || s.position === 'absolute') && parseInt(s.zIndex || '0') >= 100) { inModal = true; break; }
                        if (/tap to proceed|download is ready|tired of ads|downloads:|finish download/i.test(txt)) { inModal = true; break; }
                    } catch (_) {}
                    scope = scope.parentElement;
                    depth++;
                }
                if (!inModal) continue;
                return {x: r.x + r.width / 2, y: r.y + r.height / 2};
            }
            return null;
        }""")
    except Exception:
        pt = None
    if pt:
        try:
            await page.mouse.move(pt["x"], pt["y"], steps=4)
            await page.mouse.click(pt["x"], pt["y"], delay=120)
            logger.info("[solve_server] gate: clicked covering modal Close (trusted, main-doc)")
            return True
        except Exception:
            pass
    # Path 2: exact-text Close controls inside ad iframes (span-based
    # fronter modals). Playwright resolves + clicks in-frame, trusted,
    # cross-origin safe. Blue Continue is never selected.
    try:
        main_fr = None
        try:
            main_fr = page.main_frame
        except Exception:
            main_fr = None
        for fr in page.frames:
            try:
                if main_fr is not None and fr is main_fr:
                    continue
                try:
                    fu = (fr.url or "")
                except Exception:
                    fu = ""
                for txt in ("Close", "Dismiss", "No thanks"):
                    try:
                        loc = fr.get_by_text(txt, exact=True).first
                        try:
                            if await loc.count() == 0:
                                continue
                        except Exception:
                            continue
                        # Fast pre-check: a dismissed modal leaves hidden
                        # matches behind; skip them instantly instead of
                        # burning the 8s actionability timeout.
                        try:
                            if not await loc.is_visible():
                                continue
                        except Exception:
                            pass
                        await loc.click(timeout=8000, delay=80)
                        logger.info(f"[solve_server] gate: clicked iframe modal Close ({txt}, frame={(fu or '')[:60]})")
                        return True
                    except Exception:
                        continue
            except Exception:
                continue
    except Exception:
        pass
    return False


async def _gate_click_step(page, ctx, selector: str, effect_js: str, label: str) -> tuple:
    """Click a gate button so it lands despite ad overlays.

    Proven live (2026-09-04, headless, residential): a synthetic
    mousedown/mouseup/click dispatch on the button starts the gate
    (main.js handlers check nothing about isTrusted), sails through any
    covering iframe, and opens no pop-under. A real trusted mouse click
    is the fallback for handlers that ever demand one. Up to 3 attempts:
    if a modal covers the button, trusted-click its Close first; dispatch
    synthetic, else trusted-click; wait 2.5s; reap new tabs (destination
    tabs are preserved and returned); check effect_js. Nothing is ever
    hidden via DOM. Returns (ok, dest).
    """
    from xdmovies.link_exec import _wait_visible, _close_ad_popups, _synthetic_click
    for attempt in (1, 2, 3):
        try:
            if not await _wait_visible(page, selector, max_wait=12.0):
                logger.warning(f"[solve_server] gate: {label} never visible (attempt {attempt})")
                continue
            try:
                covered, note = await _gate_button_covered(page, selector)
                if covered:
                    logger.info(f"[solve_server] gate: {label} covered by [{note}] -- closing modal first")
                    await _click_covering_modal_close(page)
                    await asyncio.sleep(0.8)
            except Exception:
                pass
            logger.info(f"[solve_server] gate auto-drive: clicking {label} (attempt {attempt})")
            landed = False
            try:
                landed = bool(await _synthetic_click(page, selector))
            except Exception:
                pass
            if not landed:
                try:
                    landed = bool(await _trusted_click(page, selector))
                except Exception:
                    pass
            if not landed:
                continue
            await asyncio.sleep(2.5)
            try:
                dest = await _close_ad_popups(ctx, page)
                if dest:
                    logger.info(f"[solve_server] gate: destination tab opened after {label}: {dest[:100]}")
                    return (True, dest)
            except Exception:
                pass
            try:
                if await page.evaluate(effect_js):
                    return (True, None)
            except Exception:
                pass
            logger.info(f"[solve_server] gate: {label} click had no visible effect (attempt {attempt})")
        except Exception as exc:
            logger.debug(f"[solve_server] gate click step error ({label}): {exc!r}")
    return (False, None)


def _finish_gate_solve(sess, dest: str, how: str) -> None:
    """Complete a gate session with a captured destination-tab URL.

    The gate's inner captcha issues no cf_clearance, so only the Go to
    Link destination (or a destination tab caught mid-flow) completes
    the session. Stashes the result for bot.py via get_solved_result().
    """
    try:
        if not dest or sess.solved:
            return
        try:
            from xdmovies.link_exec import _is_ad_url
            if _is_ad_url(dest):
                logger.warning(f"[solve_server] gate {how}: refusing ad URL as destination: {dest[:100]}")
                return
        except Exception:
            pass
        sess.final_url = dest
        sess.solved = True
        try:
            sess.event.set()
        except Exception:
            pass
        _stash_solved_result(sess.token, final_url=dest)
        logger.info(f"[solve_server] gate {how}: captured destination {dest[:100]}")
    except Exception:
        pass


async def _drive_turnstile_stage(page, sess, max_wait: float = 90.0) -> bool:
    """FINAL STAGE, fully automatic: Close -> checkbox -> Go-enabled.

    The fronter ad modal ("Download is ready") usually covers the
    checkbox, and the old one-shot auto-nudge fired ~18s after page load
    -- long before this widget exists. So every ~3s: trusted-click the
    modal Close if it covers the widget (never hide via DOM), then click
    the checkbox inside the Turnstile frame, until Go enables (Success)
    or max_wait passes. A user tap on the screenshot works the same way
    through handle_click.     Returns True when Go is enabled.
    """
    async def _go_enabled() -> bool:
        try:
            return bool(await page.evaluate(
                "() => { const b=document.getElementById('goToLinkBtn');"
                " return !!(b && !b.classList.contains('hidden') && !b.disabled); }"
            ))
        except Exception:
            return False

    deadline_cb = time.time() + max_wait
    tick = 0
    had_frame = False
    while time.time() < deadline_cb:
        try:
            if getattr(sess, "solved", False):
                return True
        except Exception:
            pass
        tick += 1
        if await _go_enabled():
            logger.info("[solve_server] gate auto-drive: Turnstile Success (Go enabled)")
            return True
        # 1. covering modal -> trusted Close (never hide via DOM).
        closed = None
        try:
            cov, note = await _gate_button_covered(page, "#turnstileContainer")
            if cov:
                logger.info(f"[solve_server] gate: checkbox covered by [{note}] -- closing modal (tick {tick})")
                closed = await _click_covering_modal_close(page)
                logger.info(f"[solve_server] gate: modal Close clicked={closed} (tick {tick})")
                await asyncio.sleep(0.8)
            else:
                logger.debug(f"[solve_server] gate: checkbox uncovered (tick {tick})")
        except Exception as exc:
            logger.debug(f"[solve_server] gate: cover-check failed: {exc!r}")
        # 2. click the checkbox inside the Turnstile frame. Prefer the
        # frame verified inside #turnstileContainer; fall back to any
        # challenges frame. Box-aware point first (checkbox lives at the
        # left-center of the widget body): fixed offsets can land outside
        # short widget bodies, where Playwright still reports success but
        # the click hits <html> and never reaches the checkbox.
        found_frame = False
        clicked = False
        try:
            fr = None
            try:
                from xdmovies.link_exec import _find_legitimate_turnstile_frame as _legit
                fr = await _legit(page)
            except Exception:
                fr = None
            if fr is None:
                try:
                    fr = await _find_turnstile_frame(page)
                except Exception:
                    fr = None
            found_frame = fr is not None
            if found_frame:
                had_frame = True
                # Most direct first: click the actual checkbox/input inside
                # the frame (its center is the real widget control).
                try:
                    if await _auto_solve_iframe(fr):
                        clicked = True
                except Exception:
                    pass
            if found_frame and not clicked:
                pts = []
                try:
                    bb = await fr.locator("body").bounding_box()
                    if bb and bb["width"] > 0 and bb["height"] > 0:
                        pts.append((
                            min(24, max(8, int(bb["width"] // 8))),
                            min(32, max(8, int(bb["height"] // 2))),
                        ))
                except Exception:
                    pass
                pts += [(30, 32), (20, 20)]
                for ox, oy in pts:
                    try:
                        await fr.click("body", position={"x": ox, "y": oy}, delay=80, timeout=5000)
                        clicked = True
                        break
                    except Exception:
                        continue
        except Exception as exc:
            logger.debug(f"[solve_server] gate: frame click failed: {exc!r}")
        if tick == 1 or tick % 5 == 0:
            logger.info(f"[solve_server] gate: checkbox tick {tick}: frame={found_frame} click={clicked} modal_closed={closed}")
        # 3. reap pop-under tabs (Close taps and ad scripts spawn them;
        # they steal focus and pause the gate timer). Destination tabs
        # are preserved and finish the solve immediately.
        try:
            from xdmovies.link_exec import _close_ad_popups as _reap
            try:
                _ctx = page.context
            except Exception:
                _ctx = None
            if _ctx is not None:
                _td = await _reap(_ctx, page)
                if _td:
                    _finish_gate_solve(sess, _td, "checkbox-stage-popup")
                    return True
        except Exception:
            pass
        await asyncio.sleep(3.0)
    logger.warning(
        "[solve_server] gate auto-drive: checkbox loop timed out, Go never enabled"
        f" (ever_saw_frame={had_frame})"
    )
    return await _go_enabled()


async def handle_click(request):
    token = request.match_info.get("token", "")
    sess = _SESSIONS.get(token)
    if not sess or sess.page is None:
        return web.json_response({"ok": False, "error": "no session"}, status=404)
    _touch_viewer(sess)
    try:
        data = await request.json()
        x = int(data.get("x", 0))
        y = int(data.get("y", 0))
        page = sess.page

        # Convert screenshot device px -> page CSS px once. The phone
        # sends naturalWidth/naturalHeight pixels; Playwright mouse and
        # elementFromPoint both work in CSS px (divided by DPR).
        try:
            meta = await page.evaluate(
                "() => ({w: window.innerWidth, h: window.innerHeight, dpr: window.devicePixelRatio || 1})"
            )
        except Exception:
            meta = {"dpr": 1}
        dpr = meta.get("dpr", 1) or 1
        px = x / dpr
        py = y / dpr

        # Respect the user's tap intent. Previously ANY tap was diverted
        # to the Turnstile iframe whenever one existed, so tapping the
        # "Close" button on the "Download is ready" ad modal never
        # dismissed it -- the overlay stayed over the checkbox forever.
        # Hit-test first: only take the Turnstile path when the tap
        # actually landed inside #turnstileContainer.
        in_turnstile = False
        tapped = ""
        try:
            hit = await page.evaluate(
                """(pt) => {
                    try {
                        const el = document.elementFromPoint(pt[0], pt[1]);
                        if (!el) return {inTurnstile: false};
                        const t = document.getElementById('turnstileContainer');
                        if (t && (t === el || t.contains(el))) return {inTurnstile: true};
                        const txt = ((el.innerText || el.textContent) || '').slice(0, 60);
                        return {inTurnstile: false, tag: (el.tagName || ''),
                                id: (el.id || ''), cls: ((el.className || '') + '').slice(0, 60),
                                txt: txt};
                    } catch (e) { return {inTurnstile: false}; }
                }""",
                [px, py],
            ) or {}
            in_turnstile = bool(hit.get("inTurnstile"))
            tapped = f"{hit.get('tag', '')}#{hit.get('id', '')} {hit.get('txt', '')}".strip()[:80]
        except Exception:
            in_turnstile = False

        # 1) Locate Turnstile iframe (only used when tap hit it).
        frame = None
        if in_turnstile:
            try:
                frame = await _find_turnstile_frame(page)
            except Exception:
                frame = None

        if frame is not None:
            # 1a) Best path: ask the iframe directly which selector is the
            #     checkbox/widget and click it. Avoids coord math entirely.
            #     No overlay nuking here: hiding stacking elements breaks
            #     the gate's ad verification and it then refuses to
            #     generate the link.
            try:
                if await _auto_solve_iframe(frame):
                    return web.json_response({"ok": True, "mode": "locator"})
            except Exception as e:
                logger.debug(f"[solve_server] locator click failed: {e}")

            # 1b) Coordinate math fallback: CSS px -> frame-relative px,
            #     then frame.click(body, position=...).
            try:
                fb = None
                try:
                    fb = await frame.locator("body").bounding_box()
                except Exception:
                    fb = None
                if fb:
                    fx = max(0, min(int(px - fb["x"]), max(0, int(fb["width"]) - 1)))
                    fy = max(0, min(int(py - fb["y"]), max(0, int(fb["height"]) - 1)))
                else:
                    fx, fy = int(px), int(py)
                clicked = False
                try:
                    await frame.click(
                        "body",
                        position={"x": fx, "y": fy},
                        delay=80,
                        timeout=5000,
                    )
                    clicked = True
                except Exception:
                    # cross-origin frame.click can fail; the iframe's own
                    # mouse events don't propagate to the parent, so the
                    # only reliable cross-origin click is via the input
                    # element selector (which 1a already tried).
                    logger.debug("[solve_server] frame.click on body failed (cross-origin?)")
                if clicked:
                    return web.json_response(
                        {"ok": True, "mode": "frame-coord", "fx": fx, "fy": fy, "frame": (frame.url or "")[:80]}
                    )
            except Exception as e:
                logger.warning(f"[solve_server] frame coord click failed: {e}")

        # 2) Page-level click at CSS coords (ad Close buttons, Go button,
        #    or Turnstile when no iframe was found). Real trusted click
        #    only -- never hide or nuke anything: the gate verifies its
        #    ads, and hiding them stops generation. The site's own
        #    handlers (e.g. modal Close) run normally on a trusted click.
        await page.mouse.click(px, py, delay=80)
        return web.json_response({"ok": True, "mode": "page-coord",
                                  "tapped": tapped})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)

async def handle_status(request):
    token = request.match_info.get("token", "")
    sess = _SESSIONS.get(token)
    if not sess:
        return web.json_response({"error": "invalid token"}, status=404)
    if sess.solved:
        # Fast path: don't touch page/context (they may already be
        # closed by wait_for_solve's destroy). Just confirm solved so
        # the phone tab can close itself.
        return web.json_response({
            "solved": True,
            "has_cookie": True,
            "title": "",
            "age": time.time() - sess.created_at,
            "host": sess.host,
            "url": sess.url,
            "final_url": sess.final_url or "",
            "iframe_url": "",
            "iframe_mode": "",
            "goto_error": sess.goto_error or "",
            "auto_nudge_failed": sess.auto_nudge_failed,
            "viewer_active": _viewer_is_active(sess),
            "idle_seconds": int(_session_idle_seconds(sess)),
        })
    title = ""
    try:
        if sess.page: title = await sess.page.title()
    except: pass
    has_cookie = False
    try:
        if sess.context:
            cookies = await sess.context.cookies()
            has_cookie = any(c.get("name") == "cf_clearance" for c in cookies)
    except: pass
    # Detect whether a Turnstile/challenges iframe is present and grab its
    # mode (non-interactive, light, managed, interactive) from URL path.
    iframe_url = ""
    iframe_mode = ""
    try:
        if sess.page:
            for fr in sess.page.frames:
                try:
                    u = (fr.url or "").lower()
                except Exception:
                    u = ""
                if "challenges.cloudflare.com" in u or "turnstile" in u:
                    iframe_url = fr.url or ""
                    # mode is in path: /.../light/... or /.../non-interactive/...
                    from urllib.parse import urlparse
                    parts = [s for s in urlparse(iframe_url).path.split("/") if s]
                    for k in ("non-interactive", "light", "managed", "interactive", "captcha"):
                        if k in parts:
                            iframe_mode = k
                            break
                    break
    except Exception:
        pass
    return web.json_response({
        "solved": sess.solved,
        "has_cookie": has_cookie,
        "title": title,
        "age": time.time() - sess.created_at,
        "host": sess.host,
        "url": sess.url,
        "final_url": sess.final_url or "",
        "iframe_url": iframe_url,
        "iframe_mode": iframe_mode,
        "goto_error": sess.goto_error or "",
        "auto_nudge_failed": sess.auto_nudge_failed,
        "viewer_active": _viewer_is_active(sess),
        "idle_seconds": int(_session_idle_seconds(sess)),
    })

async def handle_health(request):
    out = {"ok": True, "sessions": len(_SESSIONS), "details": []}
    for tok, sess in list(_SESSIONS.items()):
        try:
            age = time.time() - sess.created_at
            out["details"].append({
                "token": tok[:8],
                "solved": sess.solved,
                "age": round(age, 1),
                "url": (sess.url or "")[:80],
                "goto_error": sess.goto_error or "",
            })
        except Exception:
            pass
    return web.json_response(out)


async def create_app():
    if web is None:
        raise RuntimeError("aiohttp not installed — pip install aiohttp")
    app = web.Application()
    app.router.add_get("/solve/{token}", handle_solve_page)
    app.router.add_post("/solve/{token}/activity", handle_activity)
    app.router.add_get("/solve/{token}/shot", handle_shot)
    app.router.add_post("/solve/{token}/click", handle_click)
    app.router.add_get("/solve/{token}/status", handle_status)
    app.router.add_get("/health", handle_health)
    return app

async def start_server():
    global _APP, _RUNNER, _SITE
    if web is None:
        logger.warning("solve_server: aiohttp not installed")
        return
    if _APP is not None:
        return
    _APP = await create_app()
    _RUNNER = web.AppRunner(_APP)
    await _RUNNER.setup()
    _SITE = web.TCPSite(_RUNNER, SOLVE_HOST, SOLVE_PORT)
    await _SITE.start()
    logger.info(f"solve_server listening on {SOLVE_HOST}:{SOLVE_PORT} public_base={SOLVE_PUBLIC_BASE}")

async def stop_server():
    global _APP, _RUNNER, _SITE
    for token in list(_SESSIONS.keys()):
        await destroy_session(token)
    if _SITE is not None:
        try: await _SITE.stop()
        except: pass
        _SITE = None
    if _RUNNER is not None:
        try: await _RUNNER.cleanup()
        except: pass
        _RUNNER = None
    _APP = None

async def create_session(url: str, user_id: int, host: str, chat_id: int = 0) -> str:
    token = uuid.uuid4().hex[:12]
    normalized_chat = chat_id or user_id
    stale = [
        existing.token for existing in list(_SESSIONS.values())
        if not existing.solved
        and existing.user_id == user_id
        and existing.chat_id == normalized_chat
    ]
    for existing_token in stale:
        await destroy_session(existing_token)
    sess = SolveSession(token=token, url=url, host=_normalize_domain(host), user_id=user_id, chat_id=normalized_chat)
    async with _LOCK:
        _SESSIONS[token] = sess
    # Keep a handle on the launch task and capture its exception into
    # goto_error. Previously this was a bare create_task with no
    # reference, so a launch crash surfaced only as "Task exception was
    # never retrieved" and the user's page sat on "loading" forever.
    async def _launch_wrapped():
        try:
            await _launch_for_session(sess)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            sess.goto_error = f"browser launch failed: {exc!r}"
            logger.exception("[solve_server] _launch_for_session crashed")
    sess.launch_task = asyncio.create_task(_launch_wrapped())
    # Viewer-aware idle cleanup keeps a token usable for one hour while the
    # solve page is being used. Hidden tabs stop screenshot requests, and a
    # fresh bot link always creates and launches a new browser session.
    async def _expire():
        while _SESSIONS.get(token) is sess and not sess.solved:
            if _session_idle_seconds(sess) >= SESSION_IDLE_TIMEOUT:
                break
            await asyncio.sleep(min(30.0, max(1.0, float(SESSION_IDLE_TIMEOUT))))
        if _SESSIONS.get(token) is sess:
            await destroy_session(token)
    sess.expire_task = asyncio.create_task(_expire())
    return token

async def destroy_session(token: str):
    sess = _SESSIONS.pop(token, None)
    if not sess:
        return
    if sess.poll_task:
        try: sess.poll_task.cancel()
        except: pass
    if sess.auto_nudge_task:
        try: sess.auto_nudge_task.cancel()
        except: pass
    if sess.launch_task:
        try: sess.launch_task.cancel()
        except: pass
    if sess.expire_task and sess.expire_task is not asyncio.current_task():
        try: sess.expire_task.cancel()
        except: pass
    try:
        if sess.page:
            try: await sess.page.close()
            except: pass
        if sess.context:
            try: await sess.context.close()
            except: pass
        if sess.browser:
            try: await sess.browser.close()
            except: pass
        if sess.pw:
            try: await sess.pw.stop()
            except: pass
    except: pass

def get_session(token: str) -> Optional[SolveSession]:
    return _SESSIONS.get(token)

def get_public_url(token: str) -> str:
    base = SOLVE_PUBLIC_BASE.rstrip("/")
    return f"{base}/solve/{token}"

async def wait_for_solve(token: str, timeout: float = 120.0) -> bool:
    sess = _SESSIONS.get(token)
    if not sess:
        return False
    solved = False
    try:
        await asyncio.wait_for(sess.event.wait(), timeout=timeout)
        solved = sess.solved
    except asyncio.TimeoutError:
        solved = False
    finally:
        # Audit finding C5: stop leaking the headed Chromium once the
        # bot has the cookie (or has given up waiting). The user's
        # phone page may still be open, but the JS status poller
        # now sees `solved=true` and closes the tab.
        if solved:
            # Fix A: hold the session ~25s before destroy so the user's
            # in-app browser (Telegram mobile is a one-shot GET — it
            # does NOT keep the JS poller running) has time to fetch
            # /status once and self-close. Without this, the destroy
            # races the user's tap: VPS auto-captures the cookie in
            # ~8s locally, the bot retries the page and succeeds,
            # destroy_session fires, and the user clicks their
            # already-dead link to find "Invalid or expired".
            await asyncio.sleep(25)
            await destroy_session(token)
    return solved

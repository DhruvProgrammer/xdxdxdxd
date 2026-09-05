"""Telegram bot for xdmovies.wtf CF-bypass link extraction.

Pipeline per DM:
    DM with `top.xdmovies.wtf` URL
        → extract_download_links / extract_cards_async  (CF bypass + parse)
        → for each card: follow_download_link → execute_button_flow
        → per-link Telegram card + summary

Access:
    Only TELEGRAM_OWNER_ID and TELEGRAM_ADMIN_IDS may use the bot.
    Everyone else is silently ignored.
Queue:
    One FIFO asyncio.Queue. Owner-priority only when both jobs land in the
    same loop iteration AND owner's arrived strictly first -- implemented
    via the queue's natural arrival order. We do NOT preempt an in-flight
    job.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from xdmovies.extractor import Card
from xdmovies import bot_config

# Single-instance lock: prevent a second `python -m xdmovies` from
# stealing the polling slot (Telegram raises "Conflict: terminated by
# other getUpdates" when two pollers race the same token).
_LOCK_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".xdmovies_bot.lock",
)
_LOCK = None  # set in main()

def _acquire_single_instance_lock() -> "fcntl.flock|None":
    """Best-effort advisory lock. On Windows (no fcntl) we fall back to a
    stale-lock check on the lockfile's mtime.
    """
    try:
        import fcntl  # type: ignore[import-not-found]
        fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            return None
        os.write(fd, str(os.getpid()).encode("ascii"))
        return fd
    except ImportError:
        # Windows path: check if the lockfile is stale.
        try:
            if os.path.isfile(_LOCK_PATH):
                age = time.time() - os.path.getmtime(_LOCK_PATH)
                if age < 60:
                    try:
                        with open(_LOCK_PATH, "r") as f:
                            pid = int((f.read() or "0").strip() or 0)
                        if pid and pid != os.getpid():
                            try:
                                import psutil  # type: ignore[import-not-found]
                                if psutil.pid_exists(pid):
                                    return None
                            except ImportError:
                                return None  # can't tell; assume live
                    except Exception:
                        return None
            with open(_LOCK_PATH, "w") as f:
                f.write(str(os.getpid()))
            return None
        except OSError:
            return None

# Heavy pipeline imports are deferred to _run_for_user() so this module
# can be imported without Playwright being ready.
_extract_with_diagnostics = None
_extract_cards_async = None
_fetch_rendered_html = None
_follow_download_link = None
_execute_button_flow = None


def _load_pipeline():
    """Lazy import the cookie-mode pipeline (no solver, no pool)."""
    global _extract_with_diagnostics, _extract_cards_async, _fetch_rendered_html, _follow_download_link, _execute_button_flow
    if _extract_with_diagnostics is not None:
        return
    parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if parent not in sys.path:
        sys.path.insert(0, parent)
    from xdmovies.pipeline import (
        extract_cards_async,
        extract_with_diagnostics,
        fetch_rendered_html,
    )
    from xdmovies.link_follower import follow_download_link_in_browser_async
    from xdmovies.link_exec import execute_button_flow
    _extract_with_diagnostics = extract_with_diagnostics
    _extract_cards_async = extract_cards_async
    _fetch_rendered_html = fetch_rendered_html
    _follow_download_link = follow_download_link_in_browser_async
    _execute_button_flow = execute_button_flow

_BOT_MAX_SESSIONS = 8

_LOG = logging.getLogger("xdmovies.bot")

# Try to import python-telegram-bot. If it isn't installed, the module
# still imports so the rest of the package can be used headless; the bot
# just refuses to start.
try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.constants import ParseMode
    import telegram.error as _TelegramError  # noqa: E402
    from telegram.ext import (
        Application,
        ApplicationBuilder,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
    _PTB_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PTB_AVAILABLE = False
    Update = None  # type: ignore
    ContextTypes = None  # type: ignore
    Application = None  # type: ignore
    ApplicationBuilder = None  # type: ignore
    CommandHandler = None  # type: ignore
    CallbackQueryHandler = None  # type: ignore
    InlineKeyboardButton = None  # type: ignore
    InlineKeyboardMarkup = None  # type: ignore
    MessageHandler = None  # type: ignore
    filters = None  # type: ignore
    ParseMode = None  # type: ignore


URL_RE = re.compile(r"https?://(?:top\.)?xdmovies\.wtf/(?:movies|series)/[A-Za-z0-9_\-]+", re.IGNORECASE)

# Per Telegram hard limit. We split at 4000 to leave room for the
# "(...continued)" prefix.
MSG_LIMIT = 4000


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------

def _html_escape(s: str) -> str:
    """Escape user-extracted text for safe insertion into HTML-mode messages.

    Telegram's `parse_mode=HTML` parser is strict: any unmatched `<`/`>` in
    the message body raises BadRequest("Can't parse entities"). Strings we
    extract from the source page (titles, file names, urls) MUST be
    escaped before interpolation.

    Audit finding C4: the previous implementation called
    `s.replace('&', '&').replace('<', '<').replace('>', '>')` -- each
    `replace` substituted the character with itself, producing an output
    identical to the input. Telegram rejected any title/filename/URL
    containing `<` or `&`. Fixed to use `html.escape`.
    """
    import html as _html
    if s is None:
        return ""
    return _html.escape(s, quote=False)


def _split_text(text: str, limit: int = MSG_LIMIT) -> List[str]:
    """Split on line boundaries, never exceeding `limit` per chunk.
    Continuation chunks get a "(... continued N/M)" header.

    The header adds ~20 chars, so we shrink the per-chunk content budget to
    leave room for it. Otherwise long inputs would produce chunks that exceed
    `limit` once the header is prepended.
    """
    if len(text) <= limit:
        return [text]
    # Reserve room for the "(... continued N/M)\n" header (worst case ~22 chars).
    HEADER_RESERVE = 24
    content_limit = max(100, limit - HEADER_RESERVE)
    chunks: List[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= content_limit:
            chunks.append(remaining)
            break
        cut_at = remaining.rfind("\n", 0, content_limit)
        if cut_at <= 0:
            cut_at = content_limit
        chunks.append(remaining[:cut_at].rstrip())
        remaining = remaining[cut_at:].lstrip("\n")
    total = len(chunks)
    if total <= 1:
        return chunks
    return [chunks[0]] + [f"(... continued {i}/{total - 1})\n{c}" for i, c in enumerate(chunks[1:], 1)]


async def _send_safe(bot, chat_id: int, text: str, parse_mode: Optional[str] = None) -> Optional[int]:
    """Send a long message as one or more chunks ≤ MSG_LIMIT.

    On `BadRequest` (entity parsing tripped -- typically an unescaped `<`),
    fall back to plain-text mode so the user still receives the message
    instead of a silently-dropped Telegram error.

    Returns the last sent message_id (or None if nothing went out) so
    callers can delete transient notes once a job ends.
    """
    last_id: Optional[int] = None
    for chunk in _split_text(text):
        if not chunk:
            continue
        try:
            msg = await bot.send_message(chat_id=chat_id, text=chunk, parse_mode=parse_mode)
            try:
                last_id = msg.message_id
            except Exception:
                pass
            continue
        except _TelegramError.BadRequest as exc:
            _LOG.warning(
                "send_message BadRequest on chunk of %d chars (%s) -- retrying plain",
                len(chunk), exc,
            )
            try:
                msg = await bot.send_message(chat_id=chat_id, text=chunk)
                try:
                    last_id = msg.message_id
                except Exception:
                    pass
            except Exception:
                _LOG.exception("plain-text retry also failed for chunk %d chars", len(chunk))
        except Exception:
            _LOG.exception("send_message failed on chunk of %d chars", len(chunk))
    return last_id


async def _send_transient_note(bot, chat_id: int, text: str) -> Optional[int]:
    """Send a short-lived status pill (e.g. the solve-wait note).

    Returns the message_id so the caller can delete it once the wait
    resolves -- otherwise one pill piles up per card and the chat looks
    broken. Fixed operator text only (no user content), so the
    BadRequest fallback of _send_safe is unnecessary here.
    """
    try:
        if bot is None:
            return None
        msg = await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
        return msg.message_id
    except Exception:
        _LOG.debug("transient note send failed (ignored)", exc_info=True)
        return None


async def _drop_msg(bot, chat_id: int, message_id: Optional[int]) -> None:
    """Best-effort delete of a transient note. Never raises."""
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


def _fmt_dur(seconds: float) -> str:
    """Compact duration: 36s, 2m 28s, 1h 05m. Used for queue ages/ETAs."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m}m" if sec == 0 else f"{m}m {sec:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def _pretty_title(url: str) -> Tuple[str, str]:
    """(icon, short title) derived from an xdmovies URL slug.

    Turns `.../movies/race-3-1080p-720p-hindi-download-496320` into
    `("🎬", "race 3 1080p 720p hindi")`. Title is HTML-escaped for direct
    use in parse_mode=HTML messages. Falls back to a truncated URL.
    """
    try:
        from urllib.parse import urlparse
        path = urlparse(url or "").path or ""
    except Exception:
        path = ""
    segs = [s for s in path.split("/") if s]
    icon = "📺" if (len(segs) >= 2 and segs[0].lower() in ("series", "tv", "shows")) else "🎬"
    slug = segs[-1] if segs else ""
    slug = re.sub(r"-\d+$", "", slug)       # trailing site id
    slug = re.sub(r"-download$", "", slug)  # trailing -download
    title = re.sub(r"\s+", " ", slug.replace("-", " ").replace("_", " ").strip())
    if len(title) > 45:
        title = title[:44] + "…"
    if not title:
        title = (url or "")[:45]
    return icon, _html_escape(title)


def _masked_uid(user_id: int) -> str:
    """Privacy: `...4974` instead of a full Telegram user id."""
    s = str(user_id or "")
    return f"...{s[-4:]}" if len(s) > 4 else (s or "?")


def _display_name(user) -> Tuple[str, str]:
    """(display name, username) from a Telegram user object.

    Never raises: PTB guarantees first_name, but last_name/username may
    be missing (both optional in Telegram).
    """
    try:
        first = (getattr(user, "first_name", "") or "").strip()
        last = (getattr(user, "last_name", "") or "").strip()
        uname = (getattr(user, "username", "") or "").strip()
        return (" ".join(p for p in (first, last) if p), uname)
    except Exception:
        return ("", "")


def _user_mention(user_id: int, user_name: str = "", username: str = "") -> str:
    """Clickable user reference for HTML messages.

    - @username known -> links to `https://t.me/<username>` (always
      opens the profile when tapped).
    - otherwise with a display name -> `tg://user?id=` mention link.
    - otherwise -> masked id fallback (`...4974`).
    Display text is HTML-escaped (names can contain <>&).
    """
    name = (user_name or "").strip()
    uname = (username or "").strip()
    if uname:
        return f'<a href="https://t.me/{uname}">{_html_escape(name or "@" + uname)}</a>'
    if name:
        try:
            return f'<a href="tg://user?id={int(user_id)}">{_html_escape(name)}</a>'
        except Exception:
            pass
    return f"<code>{_masked_uid(user_id)}</code>"


# --------------------------------------------------------------------------
# Job state
# --------------------------------------------------------------------------

@dataclass
class Job:
    user_id: int
    chat_id: int
    url: str
    enqueued_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    status: str = "queued"  # queued | running | done | error | cancelled
    error: Optional[str] = None
    progress_msg_id: Optional[int] = None
    # "Starting now" / "Queued position" note sent at enqueue. Deleted
    # together with the progress message when the job terminates so the
    # chat keeps only results + summary.
    enqueue_msg_id: Optional[int] = None
    resolved: int = 0
    failed: int = 0
    cancel_event: "asyncio.Event" = field(default_factory=asyncio.Event)
    job_type: str = "resolve"  # resolve | scrape_all
    content_type: str = ""     # all | movies | series
    # Who queued it -- captured at enqueue so /queue can show a clickable
    # name instead of a raw id. May be empty for legacy/programmatic jobs.
    user_name: str = ""
    username: str = ""
    # Batched results message -- we edit a single Telegram message instead of
    # sending one per card. List because long outputs may need >1 message.
    batch_msg_ids: List[int] = field(default_factory=list)


class Bot:
    def __init__(self, cfg: "bot_config.BotConfig"):
        self.cfg = cfg
        # We always launch Chromium HEADED (`headless=False`). On a
        # desktop this is straightforward; on a headless server (VPS /
        # systemd) `start.sh` wraps the bot in `xvfb-run` to give it a
        # virtual X server with $DISPLAY set. Same behavior on both,
        # no env-var dance.
        self.headless = False
        # Audit finding M4: replace `asyncio.Queue` with a deque + Lock
        # so /queue and /cancel can read the full queue contents
        # atomically without racing the dispatcher's `popleft()`. The
        # previous `drain and re-put` pattern allowed the dispatcher
        # to wake up mid-refill, reorder, and start the wrong job.
        import collections
        self._queue_lock = asyncio.Lock()
        # Condition must wrap the same lock that protects the deque so
        # _pick_next can wait_for(queue non-empty) without racing
        # _enqueue. The previous version did `popleft()` directly under
        # the lock, which raised IndexError every time the queue was
        # empty -- visible in the logs as a tight
        # `dispatcher loop error; sleeping 1s` loop.
        self._queue_cond = asyncio.Condition(self._queue_lock)
        self.queue: collections.deque[Job] = collections.deque()
        self.current: Optional[Job] = None
        self.last_solve_ago: Optional[float] = None
        self.error_count: int = 0
        self._dispatcher_task: Optional[asyncio.Task] = None
        self._app: Optional["Application"] = None
        self._bot = None  # telegram.Bot, set in _post_init
        # user_id -> flag. When /cancel is invoked, the bot marks the
        # user's currently-running (and any queued) jobs so the
        # dispatcher aborts cleanly between sub-steps.
        # Audit finding M1: removed the unused `_cancel_flags` dict.
        # The real cancel signal is `Job.cancel_event` (asyncio.Event),
        # set in `_cmd_cancel` and observed in the per-card loop and the
        # dispatcher. Keeping a parallel boolean dict that nothing read
        # was misleading.
        self._last_cf_url: dict[int, str] = {}
        _LOG.info("browser: headed (Chromium launches with xvfb if DISPLAY is unset)")

    # ---- queue helpers ---------------------------------------------------
    def _queue_depth(self) -> int:
        return len(self.queue)

    async def _enqueue(self, job: Job) -> int:
        async with self._queue_cond:
            self.queue.append(job)
            # Wake the dispatcher (if blocked in _pick_next waiting for
            # the queue to become non-empty).
            self._queue_cond.notify_all()
            return len(self.queue)

    async def _pick_next(self) -> Job:
        # Block until the queue has at least one job, then atomically
        # pop the leftmost. Replaces the previous `popleft()` which raised
        # IndexError on every empty-queue iteration, flooding the log
        # with `dispatcher loop error; sleeping 1s`.
        async with self._queue_cond:
            while not self.queue:
                await self._queue_cond.wait()
            return self.queue.popleft()

    async def _snapshot_queue(self) -> List[Job]:
        """Atomic snapshot under the queue lock."""
        async with self._queue_lock:
            return list(self.queue)

    # ---- progress messages ----------------------------------------------
    async def _set_progress(self, job: Job, text: str) -> None:
        if self._bot is None:
            _LOG.debug("progress skipped: bot not yet initialized")
            return
        try:
            if job.progress_msg_id is None:
                msg = await self._bot.send_message(
                    chat_id=job.chat_id, text=text, parse_mode=ParseMode.HTML
                )
                job.progress_msg_id = msg.message_id
            else:
                await self._bot.edit_message_text(
                    chat_id=job.chat_id,
                    message_id=job.progress_msg_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                )
        except Exception:
            _LOG.debug("progress update failed (ignored)", exc_info=True)

    async def _drop_job_clutter(self, job: Job) -> None:
        """Delete a finished job's transient messages.

        Removes the per-card progress note ("Resolving link i/n") and the
        enqueue note ("Starting now" / "Queued position") so the chat keeps
        only results + summary. Best-effort: never raises. Safe to call
        twice (second delete fails silently).
        """
        try:
            if self._bot is None:
                return
            for mid in (getattr(job, "progress_msg_id", None),
                        getattr(job, "enqueue_msg_id", None)):
                if not mid:
                    continue
                try:
                    await self._bot.delete_message(chat_id=job.chat_id, message_id=mid)
                except Exception:
                    pass
            job.progress_msg_id = None
            job.enqueue_msg_id = None
        except Exception:
            pass

    # ---- the big resolver coroutine -------------------------------------
    async def _run_for_user(self, job: Job) -> None:
        if job.job_type == "scrape_all":
            await self._run_scrape_all(job)
            return
        cfg = self.cfg
        started = time.time()
        job.started_at = started
        job.status = "running"
        await self._set_progress(job, "🔄 Bypassing Cloudflare on the movie page...")
        session_id = f"tg_bot_{uuid.uuid4().hex[:8]}"

        try:
            _load_pipeline()
        except Exception as exc:
            job.error = f"pipeline-import: {exc!r}"
            await self._fail(job, session_id)
            return

        # import CF helpers here to avoid circular import at module load
        from xdmovies.cookie_store import CFChallengeError, cf_prompt_text

        cards: List[Card] = []
        result = None
        try:
            result = await _extract_with_diagnostics(  # type: ignore[misc]
                job.url, session_id=session_id, headless=self.headless
            )
            cards = result.cards
        except CFChallengeError as cf_exc:
            host = getattr(cf_exc, "host", "") or "xdmovies.wtf"
            blocked_url = cf_exc.url or job.url
            try:
                self._last_cf_url[job.user_id] = blocked_url
            except: pass
            # hybrid: try VPS token solve (posts need it, not home)
            token, msg_id = await self._send_solve_link(job.chat_id, blocked_url, host, job.user_id)
            if token:
                # Audit finding H6: align with SESSION_TTL (300s) and
                # shorten the wait so the user isn't left waiting after
                # the solve_server already destroyed the session.
                _waitpill = await _send_transient_note(self._bot, job.chat_id, "⏳ Waiting up to 120s -- open solve link and tap checkbox if you see it. Keep-alive will resume after.")
                try:
                    _solved = await self._wait_for_solve(token, timeout=120.0)
                finally:
                    await _drop_msg(self._bot, job.chat_id, _waitpill)
                if _solved:
                    if msg_id:
                        try: await self._bot.delete_message(chat_id=job.chat_id, message_id=msg_id)
                        except: pass
                    # Audit finding H10: move the post-solve retry out of
                    # the except CFChallengeError block so non-CF
                    # exceptions are also caught by the parent's generic
                    # except Exception. The previous structure raised the
                    # unhandled exception as an "unhandled error" from the
                    # dispatcher with no _fail() bookkeeping.
                    try:
                        result = await _extract_with_diagnostics(job.url, session_id=session_id, headless=self.headless)  # type: ignore[misc]
                        cards = result.cards
                    except CFChallengeError as cf2:
                        # still blocked after solve
                        host2 = getattr(cf2, "host", "") or host
                        await _send_safe(self._bot, job.chat_id, f"[FAIL] Still blocked on <code>{host2}</code> after solve -- try again or /status", parse_mode=ParseMode.HTML)
                        job.status = "error"
                        job.error = f"cf_challenge:{host2}"
                        job.finished_at = time.time()
                        return
                else:
                    await _send_safe(self._bot, job.chat_id, f"⏰ Solve timed out. Resend the link to get a fresh solve link (keep-alive paused).", parse_mode=ParseMode.HTML)
                    job.status = "error"
                    job.error = f"cf_challenge:{host}"
                    job.finished_at = time.time()
                    return
            else:
                await _send_safe(self._bot, job.chat_id, f"🔒 <b>Cloudflare blocked</b> -- <code>{host}</code>\nLink: <code>{blocked_url}</code>\nCould not create solve link -- check VPS 132.145.15.58:8080", parse_mode=ParseMode.HTML)
                job.status = "error"
                job.error = f"cf_challenge:{host}"
                job.finished_at = time.time()
                return
        except Exception as exc:
            job.error = f"extract: {exc!r}"
            await self._fail(job, session_id)
            return

        if not cards:
            # Distinguish "xdmovies served a stub/error page" from
            # "real page but no download links" using the <title>.
            # Byte count alone is unreliable: a real series page with
            # no download section is only ~2KB (title = "Name - Series"),
            # while a 404 page is ~1-3KB (title = "404 Not Found").
            _title = (result.page_title or "").strip()
            _title_lc = _title.lower()
            _looks_like_error = (
                not _title_lc
                or "404" in _title_lc
                or "not found" in _title_lc
                or "page not found" in _title_lc
                or "error" in _title_lc
                or "template" in _title_lc
                or "oops" in _title_lc
                or "something went wrong" in _title_lc
                or "doesn't exist" in _title_lc
            )
            if _looks_like_error:
                _title_part = (
                    f", title: <code>{_html_escape(_title[:80])}</code>"
                    if _title else ""
                )
                await _send_safe(
                    self._bot, job.chat_id,
                    f"[FAIL] xdmovies returned an empty page for this URL "
                    f"(<code>{result.html_len} bytes</code>{_title_part}). "
                    f"The slug may be invalid or the site may be serving "
                    f"a template error. Try a different URL.\n"
                    f"📄 Saved: <code>xdmovies/xdmovies_dumps/latest_movie_page.html</code>",
                    parse_mode=ParseMode.HTML,
                )
            else:
                # Real page (e.g. "House of the Dragon - Series") but
                # the listing has no download links. Don't blame the slug.
                await _send_safe(
                    self._bot, job.chat_id,
                    f"[FAIL] no download links found on that page "
                    f"(title: <code>{_html_escape(_title[:80])}</code>, "
                    f"<code>{result.html_len} bytes</code>). The page "
                    f"exists but the download section is empty.",
                    parse_mode=ParseMode.HTML,
                )
            job.status = "done"
            job.finished_at = time.time()
            self.last_solve_ago = 0.0
            # Audit finding M5: touch keep-alive on every successful
            # job so the 25m refresh loop restarts from now. Otherwise
            # the loop fires 25m after the *last solve*, not the *last
            # successful use*, and a user running the bot all day would
            # see the keep-alive fire only once an hour after the first
            # solve instead of 25m after the most recent one.
            try:
                from xdmovies.keep_alive import _touch_keep_alive
                _touch_keep_alive("xdmovies.wtf")
            except Exception as _e:
                _LOG.debug(f"keep_alive touch failed: {_e!r}")
            return

        n = len(cards)
        await self._set_progress(
            job,
            f"🔄 Found <b>{n}</b> download link{'s' if n != 1 else ''}. Resolving each...",
        )

        # If the user issued /cancel between the CF bypass and now, exit
        # before doing any link resolution work.
        if job.cancel_event.is_set():
            await _send_safe(
                self._bot, job.chat_id,
                "🛑 cancelled before link resolution started.",
                parse_mode=ParseMode.HTML,
            )
            return

        results: List[Tuple[Card, Optional[str], Optional[str]]] = []
        # ONE shared session for every card in this job. The first card's
        # CF clear attaches to the session; cards 2..N hit the cache and
        # skip the ~30s solve entirely.
        shared_session = f"tg_job_{job.user_id}_{uuid.uuid4().hex[:8]}"
        for i, card in enumerate(cards, 1):
            if job.cancel_event.is_set():
                await _send_safe(
                    self._bot, job.chat_id,
                    f"🛑 cancelled before link <b>{i}/{n}</b>.",
                    parse_mode=ParseMode.HTML,
                )
                return
            await self._set_progress(
                job,
                f"⏳ Resolving link <b>{i}/{n}</b>:\n<code>{_html_escape(card.url)}</code>",
            )
            dest: Optional[str] = None
            err: Optional[str] = None
            try:
                from xdmovies.link_exec import _is_destination_url as _is_dest_url
                if _is_dest_url(card.url):
                    # Direct hub link straight on the movie page (e.g.
                    # hubcloud.one/drive/...): no link.xdmovies.wtf hop,
                    # no latestnews gate. Deliver as-is instead of
                    # burning two browsers on follow + button flow.
                    dest = card.url
                    _LOG.info("direct dest card, skipping follow+gate: %s", card.url[:100])
                    results.append((card, dest, None))
                    job.resolved += 1
                    try:
                        await self._send_or_edit_batch(job, result.page_title, results, in_progress_idx=i)
                    except Exception:
                        _LOG.exception("batch edit failed at card %d/%d", i, n)
                    continue
            except Exception:
                pass
            try:
                url2, status2, page_text = await _follow_download_link(  # type: ignore[misc]
                    card.url, session_id=shared_session, headless=self.headless
                )
                if not url2:
                    err = "follow returned empty URL"
                else:
                    for _btn_attempt in range(2):
                        try:
                            dest = await _execute_button_flow(  # type: ignore[misc]
                                url2, session_id=shared_session, headless=False
                            )
                        except Exception as exc:
                            try:
                                from xdmovies.cookie_store import CFChallengeError as _CFErr2
                                if isinstance(exc, _CFErr2):
                                    host2 = getattr(exc, "host", "") or "latestnewsonline.sbs"
                                    blocked2 = exc.url or url2
                                    try: self._last_cf_url[job.user_id] = blocked2
                                    except: pass
                                    token2, msg_id2 = await self._send_solve_link(job.chat_id, blocked2, host2, job.user_id)
                                    if token2:
                                        _gate2 = "latestnews" in (host2 or "")
                                        _wait2 = 240.0 if _gate2 else 90.0
                                        _waitpill2 = await _send_transient_note(self._bot, job.chat_id, f"⏳ Waiting up to {int(_wait2)}s -- open solve link and tap if needed.")
                                        try:
                                            _solved2 = await self._wait_for_solve(token2, timeout=_wait2)
                                        finally:
                                            await _drop_msg(self._bot, job.chat_id, _waitpill2)
                                        if _solved2:
                                            if msg_id2:
                                                try: await self._bot.delete_message(chat_id=job.chat_id, message_id=msg_id2)
                                                except: pass
                                            try:
                                                from xdmovies.solve_server import get_solved_result as _gsr2
                                                _r2 = _gsr2(token2) or {}
                                                if (_r2.get("final_url") or "").strip():
                                                    dest = _r2["final_url"].strip()
                                                    break
                                            except: pass
                                            try:
                                                dest = await _execute_button_flow(url2, session_id=shared_session, headless=False)  # type: ignore
                                                if dest: break
                                            except: pass
                                    err = f"CF challenge on {host2} -- opened solve link {blocked2}, still blocked"
                                    break
                            except Exception:
                                pass
                            err = f"button flow: {exc.__class__.__name__}: {exc}"
                            _LOG.exception("button flow failed for %s", url2)
                            break
                        if dest:
                            break
                        if _btn_attempt == 0:
                            _LOG.warning("button flow returned empty for %s, retrying once", url2)
                            await asyncio.sleep(2.0)
                        else:
                            err = f"button flow returned empty (final status {status2})"
            except Exception as exc:
                try:
                    from xdmovies.cookie_store import CFChallengeError as _CFErrF
                    if isinstance(exc, _CFErrF):
                        hostf = getattr(exc, "host", "") or "xdmovies.wtf"
                        blockedf = exc.url or card.url
                        try: self._last_cf_url[job.user_id] = blockedf
                        except: pass
                        tokenf, msg_idf = await self._send_solve_link(job.chat_id, blockedf, hostf, job.user_id)
                        if tokenf:
                            _gatef = "latestnews" in (hostf or "")
                            _waitf = 240.0 if _gatef else 90.0
                            _waitpillf = await _send_transient_note(self._bot, job.chat_id, f"⏳ Waiting up to {int(_waitf)}s -- open solve link and tap if needed.")
                            try:
                                _solvedf = await self._wait_for_solve(tokenf, timeout=_waitf)
                            finally:
                                await _drop_msg(self._bot, job.chat_id, _waitpillf)
                            if _solvedf:
                                if msg_idf:
                                    try: await self._bot.delete_message(chat_id=job.chat_id, message_id=msg_idf)
                                    except: pass
                                try:
                                    from xdmovies.solve_server import get_solved_result as _gsrf
                                    _rf = _gsrf(tokenf) or {}
                                    if (_rf.get("final_url") or "").strip():
                                        dest = _rf["final_url"].strip()
                                        results.append((card, dest, None))
                                        job.resolved += 1
                                        try:
                                            await self._send_or_edit_batch(job, result.page_title, results, in_progress_idx=i)
                                        except Exception:
                                            _LOG.exception("batch edit failed at card %d/%d", i, n)
                                        continue
                                except: pass
                                try:
                                    url2r, _, _ = await _follow_download_link(card.url, session_id=shared_session, headless=self.headless)  # type: ignore
                                    if url2r:
                                        try:
                                            dest = await _execute_button_flow(url2r, session_id=shared_session, headless=False)  # type: ignore
                                            if dest:
                                                results.append((card, dest, None))
                                                job.resolved += 1
                                                try:
                                                    await self._send_or_edit_batch(job, result.page_title, results, in_progress_idx=i)
                                                except Exception:
                                                    _LOG.exception("batch edit failed at card %d/%d", i, n)
                                                continue
                                        except: pass
                                except: pass
                        err = f"CF challenge on {hostf} -- opened solve link {blockedf}, still blocked"
                    else:
                        err = f"{exc.__class__.__name__}: {exc}"
                except Exception:
                    err = f"{exc.__class__.__name__}: {exc}"
                _LOG.exception("resolve failed for %s", card.url)

            results.append((card, dest, err))
            if dest and not err:
                job.resolved += 1
            else:
                job.failed += 1
            # Edit the single batched message in place instead of sending
            # one Telegram message per card.
            try:
                await self._send_or_edit_batch(job, result.page_title, results, in_progress_idx=i)
            except Exception:
                _LOG.exception("batch edit failed at card %d/%d", i, n)

        # summary
        elapsed = time.time() - started
        mm, ss = divmod(int(elapsed), 60)
        _title_line = (
            f"🎬 <b>title:</b> <code>{_html_escape(result.page_title or '(untitled)')}</code>\n"
        )
        summary = (
            f"{_title_line}"
            f"⏱ <b>total_time_for_all:</b> {mm}m {ss}s\n"
            f"[OK] {job.resolved}/{n} resolved  [FAIL] {job.failed} failed"
        )
        try:
            await self._bot.send_message(
                chat_id=job.chat_id, text=summary, parse_mode=ParseMode.HTML
            )
        except Exception:
            _LOG.exception("summary send failed")

        job.status = "done" if job.failed == 0 else "done_with_errors"
        job.finished_at = time.time()
        self.last_solve_ago = 0.0
        # remember this post for keep-alive refresh (CF is on posts, not home)
        try:
            from xdmovies.keep_alive import remember_movie_url
            remember_movie_url(job.url)
        except Exception:
            pass

    async def _run_scrape_all(self, job: Job) -> None:
        from xdmovies.scrape_all import (
            catalog_page_url,
            extract_catalog_urls,
            extract_pagination_info,
            load_checkpoint,
            save_checkpoint,
        )

        kind = job.content_type
        state = load_checkpoint(kind)
        if state.finished:
            state.next_page = 1
            state.pages_completed = 0
        state.finished = False
        save_checkpoint(state)
        completed = set(state.completed_urls)
        session_id = f"tg_scrapeall_{kind}_{uuid.uuid4().hex[:8]}"
        job.started_at = time.time()
        job.status = "running"

        # Audit finding H11: route CF challenges on catalog pages through
        # the same solve flow as a normal job. Need the exception class
        # in scope before the try/except.
        from xdmovies.cookie_store import CFChallengeError

        try:
            _load_pipeline()
        except Exception as exc:
            job.error = f"pipeline-import: {exc!r}"
            await self._fail(job, session_id)
            return

        while not job.cancel_event.is_set():
            page_number = state.next_page
            page_url = catalog_page_url(kind, page_number)
            await self._set_progress(
                job,
                f"🔎 Scraping <b>{kind}</b> page <b>{page_number}</b>...\n"
                f"completed: <b>{len(completed)}</b>  failed: <b>{len(state.failed)}</b>",
            )
            try:
                html = await _fetch_rendered_html(  # type: ignore[misc]
                    page_url, session_id=session_id, headless=self.headless
                )
                page_urls = extract_catalog_urls(html, kind, page_url)
                pagination = extract_pagination_info(html)
            except CFChallengeError as cf_exc:
                # Audit finding H11: previously, a CF challenge on a
                # catalog page would be caught by the generic except and
                # abort the whole scrape. Now we route through the same
                # solve flow as a normal job: send a solve link, wait
                # for the user to solve, then re-loop the same
                # page_number.
                cf_host = getattr(cf_exc, "host", "") or "xdmovies.wtf"
                blocked = cf_exc.url or page_url
                token, _msg = await self._send_solve_link(
                    job.chat_id, blocked, cf_host, job.user_id
                )
                if token:
                    await _send_safe(
                        self._bot, job.chat_id,
                        f"🔒 Scraping <b>{kind}</b> catalog hit a CF challenge. "
                        f"Open the solve link if you see it; the scrape will resume.",
                        parse_mode=ParseMode.HTML,
                    )
                    if await self._wait_for_solve(token, timeout=120.0):
                        # Re-loop the same page number (do not advance).
                        continue
                # Either the user didn't solve, or solve didn't catch the
                # challenge. Save checkpoint and bail.
                save_checkpoint(state)
                job.error = f"cf_challenge on catalog page {page_number}"
                job.finished_at = time.time()
                await _send_safe(
                    self._bot, job.chat_id,
                    f"[FAIL] Could not bypass CF on catalog page {page_number}. "
                    f"Scrape paused. Send /scrapeall to resume after solving.",
                    parse_mode=ParseMode.HTML,
                )
                return
            except Exception as exc:
                job.error = f"catalog page {page_number}: {exc.__class__.__name__}: {exc}"
                save_checkpoint(state)
                await self._fail(job, session_id)
                return

            pending = [url for url in page_urls if url not in completed]
            if not page_urls:
                state.finished = True
                save_checkpoint(state)
                break

            for index, url in enumerate(pending, 1):
                if job.cancel_event.is_set():
                    break
                await self._set_progress(
                    job,
                    f"⏳ <b>{kind}</b> page {page_number}: item {index}/{len(pending)}\n"
                    f"total completed: <b>{len(completed)}</b>\n"
                    f"<code>{_html_escape(url)}</code>",
                )
                child = Job(user_id=job.user_id, chat_id=job.chat_id, url=url,
                            user_name=job.user_name, username=job.username)
                child.cancel_event = job.cancel_event
                try:
                    await self._run_for_user(child)
                    if child.status in {"done", "done_with_errors"}:
                        completed.add(url)
                        state.completed_urls.append(url)
                        state.failed.pop(url, None)
                        job.resolved += 1
                    else:
                        state.failed[url] = child.error or child.status
                        job.failed += 1
                except Exception as exc:
                    state.failed[url] = f"{exc.__class__.__name__}: {exc}"
                    job.failed += 1
                    _LOG.exception("scrapeall item failed: %s", url)
                save_checkpoint(state)

            if job.cancel_event.is_set():
                break
            state.pages_completed += 1
            if pagination and page_number >= pagination[1]:
                state.finished = True
                save_checkpoint(state)
                break
            state.next_page = page_number + 1
            save_checkpoint(state)

        if job.cancel_event.is_set():
            job.status = "cancelled"
            text = (
                f"🛑 <b>{kind}</b> scraping paused at page <b>{state.next_page}</b>.\n"
                "Run /scrapeall and choose the same type to resume."
            )
        else:
            job.status = "done" if not state.failed else "done_with_errors"
            text = (
                f"[OK] <b>{kind}</b> scraping complete.\n"
                f"pages: <b>{state.pages_completed}</b>  "
                f"completed: <b>{len(state.completed_urls)}</b>  "
                f"failed: <b>{len(state.failed)}</b>"
            )
        job.finished_at = time.time()
        await _send_safe(self._bot, job.chat_id, text, parse_mode=ParseMode.HTML)

    # --- batched results message ----------------------------------------
    def _format_batch_text(
        self,
        page_title: str,
        cards: List[Tuple["Card", Optional[str], Optional[str]]],
    ) -> str:
        """Render the entire batched results message in one HTML string.

        Layout (matches the user's expected format):
          {movie/series name}:
          <blank>
          1) <title> [size] [lang]
          ─────────────────────────
          → <hubcloud url>
          ─────────────────────────
          2) <title> ...
        """
        sep = "─" * 25
        title_esc = _html_escape(page_title or "(untitled)")
        out: List[str] = [f"<b>{title_esc}:</b>"]
        for i, (card, dest, err) in enumerate(cards, 1):
            title_text = _html_escape(card.title or "(untitled)")
            block = [f"<b>{i}) {title_text}</b>"]
            if card.size:
                block.append(f"Size: {_html_escape(card.size)}")
            if card.language:
                block.append(f"Language: {_html_escape(card.language)}")
            if card.file_name and card.file_name != card.title:
                block.append(f"File: {_html_escape(card.file_name)}")
            if dest:
                # Plain URL, no <code> tags (user preference): Telegram
                # clients auto-link bare URLs. Still entity-escaped so a
                # `&` in query strings can't trip HTML parsing; the
                # displayed/tappable link is unaffected.
                block.append(f"→ {_html_escape(dest)}")
            elif err:
                block.append(f"[FAIL] {_html_escape(err)}")
            else:
                block.append("⏳ resolving...")
            out.append("\n".join(block))
            out.append(sep)
        # remove trailing separator for clean look
        if out and out[-1] == sep:
            out.pop()
        return "\n\n".join(out)

    async def _send_or_edit_batch(
        self,
        job: Job,
        page_title: str,
        cards: List[Tuple["Card", Optional[str], Optional[str]]],
        in_progress_idx: Optional[int] = None,
    ) -> None:
        """Send the batched results message once, then edit it as cards complete.

        If the rendered text exceeds Telegram's per-message limit, we split
        into multiple messages and edit each in place.
        """
        if self._bot is None:
            _LOG.debug("batch skipped: bot not yet initialized")
            return
        full_text = self._format_batch_text(page_title, cards)
        chunks = _split_text(full_text)
        try:
            # If we already have N messages stored and the chunk count matches,
            # edit them in place. Otherwise send fresh and replace the list.
            existing = job.batch_msg_ids
            if existing and len(existing) == len(chunks):
                for msg_id, chunk in zip(existing, chunks):
                    try:
                        await self._bot.edit_message_text(
                            chat_id=job.chat_id,
                            message_id=msg_id,
                            text=chunk,
                            parse_mode=ParseMode.HTML,
                        )
                    except _TelegramError.BadRequest as exc:
                        # "message is not modified" is harmless when the
                        # text didn't change yet
                        if "not modified" in str(exc).lower():
                            continue
                        raise
            else:
                # Length changed (more cards added) -- replace the batch.
                # First, delete the previous batch if it existed.
                for old_id in existing:
                    try:
                        await self._bot.delete_message(chat_id=job.chat_id, message_id=old_id)
                    except Exception:
                        pass
                new_ids: List[int] = []
                for chunk in chunks:
                    try:
                        msg = await self._bot.send_message(
                            chat_id=job.chat_id, text=chunk, parse_mode=ParseMode.HTML
                        )
                        new_ids.append(msg.message_id)
                    except _TelegramError.BadRequest as exc:
                        _LOG.warning("batch HTML failed (%s) -- retrying plain", exc)
                        msg = await self._bot.send_message(chat_id=job.chat_id, text=chunk)
                        new_ids.append(msg.message_id)
                job.batch_msg_ids = new_ids
        except Exception:
            _LOG.exception("batch send/edit failed")


    async def _fail(self, job: Job, session_id: str) -> None:
        # Cancellation isn't an error -- bubble up as a status, not a
        # traceback.
        await self._drop_job_clutter(job)
        if job.cancel_event.is_set():
            job.status = "cancelled"
            job.finished_at = time.time()
            return
        self.error_count += 1
        job.status = "error"
        job.finished_at = time.time()
        try:
            with open(self.cfg.error_log_path, "a", encoding="utf-8") as f:
                f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} user={job.user_id} url={job.url} ===\n")
                traceback.print_exc(file=f)
        except Exception:
            pass
        _LOG.exception("job failed: %s", job.error)
        if self._bot is None:
            return
        # HTML-escape `job.error` because it may carry user-extracted
        # strings (e.g. card titles containing `<` characters). Falls back
        # to plain text on any parse failure.
        err_text = _html_escape(job.error or "unknown")
        try:
            await self._bot.send_message(
                chat_id=job.chat_id,
                text=(
                    f"[FAIL] error: {err_text}\n"
                    f"Full traceback: <code>{_html_escape(self.cfg.error_log_path)}</code>"
                ),
                parse_mode=ParseMode.HTML,
            )
        except _TelegramError.BadRequest as exc:
            _LOG.warning("error reply HTML failed (%s) -- retrying plain", exc)
            try:
                await self._bot.send_message(
                    chat_id=job.chat_id,
                    text=(
                        f"[FAIL] error: {job.error or 'unknown'}\n"
                        f"Full traceback: {self.cfg.error_log_path}"
                    ),
                )
            except Exception:
                _LOG.exception("plain error reply also failed")
        except Exception:
            _LOG.exception("error reply failed")

    # ---- dispatcher ------------------------------------------------------
    async def _dispatcher(self) -> None:
        _LOG.info("dispatcher started")
        while True:
            try:
                job = await self._pick_next()
                self.current = job
                _LOG.info(
                    "starting job user=%d url=%s queue_depth=%d",
                    job.user_id, job.url, self._queue_depth(),
                )
                try:
                    await self._run_for_user(job)
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    job.error = f"{exc.__class__.__name__}: {exc}"
                    # Cancellation isn't an error -- bubble up as a
                    # status, not a traceback.
                    if job.cancel_event.is_set():
                        job.status = "cancelled"
                    else:
                        job.status = "error"
                    job.finished_at = time.time()
                    if not job.cancel_event.is_set():
                        self.error_count += 1
                    _LOG.exception("dispatcher caught baseexception for job")
                    try:
                        with open(self.cfg.error_log_path, "a", encoding="utf-8") as f:
                            f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} OUTER user={job.user_id} status={job.status} ===\n")
                            traceback.print_exc(file=f)
                        if self._bot is not None and not job.cancel_event.is_set():
                            await self._bot.send_message(
                                chat_id=job.chat_id,
                                text=f"[FAIL] unhandled error: {job.error}\nFull traceback: {self.cfg.error_log_path}",
                            )
                    except Exception:
                        pass
                finally:
                    self.current = None
                    self.last_solve_ago = 0.0
                    # Every job ends here (done / error / cancelled / crash):
                    # drop its transient progress + enqueue notes so the
                    # chat keeps only results + summary.
                    try:
                        await self._drop_job_clutter(job)
                    except BaseException:
                        pass
            except asyncio.CancelledError:
                _LOG.info("dispatcher cancelled")
                break
            except Exception:
                _LOG.exception("dispatcher loop error; sleeping 1s")
                await asyncio.sleep(1.0)

    # ---- handlers --------------------------------------------------------
    async def _cmd_start(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
        if update.effective_user is None or update.effective_chat is None:
            return
        if not bot_config.is_allowed(update.effective_user.id, self.cfg):
            _LOG.warning("rejected /start from unauthorized Telegram user id=%d", update.effective_user.id)
            await _send_safe(
                context.bot,
                update.effective_chat.id,
                f"⛔ This bot is private. Your Telegram user ID is <code>{update.effective_user.id}</code>. "
                "Add it to TELEGRAM_OWNER_ID or TELEGRAM_ADMIN_IDS and restart the bot.",
                parse_mode=ParseMode.HTML,
            )
            return
        text = (
            "Welcome to the xdmovies bypass bot 👋\n\n"
            "Send me an xdmovies URL and I'll resolve every download link on it all the way "
            "to its final destination (HubCloud / GDrive / Gofile / etc.).\n\n"
            "/start -- show this help\n"
            "/status -- bot health + keep-alive\n"
            "/queue -- show who's waiting\n"
            "/scrapeall -- scrape every movie or series (owner + admins)\n"
            "/cancel -- stop your running/queued job\n"
        )
        await _send_safe(context.bot, update.effective_chat.id, text, parse_mode=ParseMode.HTML)

    async def _cmd_status(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
        if update.effective_user is None or update.effective_chat is None:
            return
        if not bot_config.is_allowed(update.effective_user.id, self.cfg):
            return
        last = "n/a"
        if self.last_solve_ago is not None:
            last = f"{int(self.last_solve_ago)}s ago"
        depth = self._queue_depth()
        cur = "idle" if self.current is None else f"running (user {self.current.user_id})"
        # cookie status
        try:
            from xdmovies.cookie_store import status_text as _cookie_status
            cst = _cookie_status()
        except Exception as e:
            cst = f"(cookie status error: {e})"
        # keep-alive state
        try:
            from xdmovies.keep_alive import _get_last_solve_ts, _load_state, _pick_refresh_target
            import time as _t
            ts = _get_last_solve_ts("xdmovies.wtf")
            st = _load_state()
            last_ok = st.get("xdmovies.wtf", {}).get("last_refresh_ok")
            ka = "no cookie yet"
            if ts:
                age_m = int((_t.time() - ts)//60)
                ka = f"{age_m}m ago (next refresh in {max(0, 25 - age_m)}m) target: {_pick_refresh_target()[:50]}..."
                if last_ok:
                    ka += f"\nlast keep-alive ok: {int((_t.time()-float(last_ok))//60)}m ago"
            keep_txt = f"\n<b>keep-alive:</b> {ka} (post-level, VPS fallback)\n"
        except Exception:
            keep_txt = ""
        # CloakBrowser status
        try:
            from xdmovies.cloak_stealth import cloak_version, cloak_available
            if cloak_available():
                # Try to get binary info too (version + platform)
                try:
                    import cloakbrowser as _cb
                    bi = _cb.binary_info() or {}
                    cb_ver = bi.get("version") or cloak_version() or "?"
                    cb_plat = bi.get("platform") or "?"
                    cloak_line = f"\n<b>cloak:</b> [OK] v{cb_ver} ({cb_plat}, C++ patches)\n"
                except Exception:
                    cloak_line = f"\n<b>cloak:</b> [OK] v{cloak_version() or 'unknown'} (C++ patches)\n"
            else:
                cloak_line = "\n<b>cloak:</b> [FAIL] not importable\n"
        except Exception as e:
            cloak_line = f"\n<b>cloak:</b> (error: {e})\n"
        # No VPN. Direct outbound from VPS IP. We surface this in
        # /status so the operator knows to expect reduced success on
        # /solve flows (Cloudflare fingerprints the Oracle Cloud ASN).
        vpn_line = "\n<b>vpn:</b> [FAIL] none (direct from VPS IP -- expect CF to flag this)\n"
        try:
            r = subprocess.run(["systemctl", "is-active", "xdmovies-vnc"], capture_output=True, timeout=2)
            if r.returncode == 0 and r.stdout.strip() == b"active":
                desktop_line = "\n<b>desktop:</b> [OK] XFCE on :0 (noVNC :6080)\n"
            else:
                desktop_line = "\n<b>desktop:</b> [FAIL] xdmovies-vnc not active (set XDMOVIES_ENABLE_VNC=1 in deploy env)\n"
        except Exception:
            desktop_line = ""
        text = (
            "🟢 <b>alive</b> (VPS token solve-server + CloakBrowser, no VPN layer)\n"
            f"queue: <b>{depth}</b>\n"
            f"current: {cur}\n"
            f"last solve: {last}\n"
            f"errors: {self.error_count} (see {self.cfg.error_log_path}){keep_txt}{cloak_line}{vpn_line}{desktop_line}\n"
            f"<b>cookies:</b>\n{cst}"
        )
        await _send_safe(context.bot, update.effective_chat.id, text, parse_mode=ParseMode.HTML)

    async def _cmd_queue(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
        if update.effective_user is None or update.effective_chat is None:
            return
        if not bot_config.is_allowed(update.effective_user.id, self.cfg):
            return
        if self.current is None and self._queue_depth() == 0:
            await _send_safe(context.bot, update.effective_chat.id,
                "📋 <b>Queue Status</b>\n\n✅ All clear -- nothing running.",
                parse_mode=ParseMode.HTML)
            return
        now = time.time()
        lines = ["📋 <b>Queue Status</b>"]
        if self.current is not None:
            c = self.current
            icon, title = _pretty_title(c.url)
            elapsed = f" • ⏱ {_fmt_dur(now - c.started_at)} in" if c.started_at else ""
            lines += [
                "",
                "▶️ <b>Now running</b>",
                f"{icon} {title}",
                f"👤 {_user_mention(c.user_id, c.user_name, c.username)}{elapsed}",
            ]
        # Audit finding M4: take an atomic snapshot under the queue
        # lock instead of draining/reputting. The previous drain loop
        # could race the dispatcher's popleft() and reorder jobs.
        items = await self._snapshot_queue()
        if items:
            lines += ["", f"⏳ <b>Up next ({len(items)})</b>"]
            for idx, j in enumerate(items, 1):
                icon, title = _pretty_title(j.url)
                lines.append(f"<b>{idx}.</b> {icon} {title}")
                lines.append(f"     👤 {_user_mention(j.user_id, j.user_name, j.username)} • waiting {_fmt_dur(now - j.enqueued_at)}")
        await _send_safe(context.bot, update.effective_chat.id, "\n".join(lines), parse_mode=ParseMode.HTML)

    async def _cmd_cancel(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
        if update.effective_user is None or update.effective_chat is None:
            return
        uid = update.effective_user.id
        if not bot_config.is_allowed(uid, self.cfg):
            return  # silent ignore

        cancelled_running = False
        cancelled_queued = 0
        removed_jobs: List[Tuple[int, str]] = []  # (depth, url)

        # 1. If the currently-running job belongs to this user, mark it
        #    cancelled. The dispatcher observes the cancel_event at the
        #    next per-card boundary and exits cleanly.
        if self.current is not None and self.current.user_id == uid:
            self.current.cancel_event.set()
            cancelled_running = True

        # 2. Audit finding M4: take an atomic snapshot of the queue
        # under the lock, then mutate the deque in place. This avoids
        # the previous drain-and-reput race where the dispatcher could
        # start a job mid-refill and reorder the queue.
        items = await self._snapshot_queue()
        kept: List[Job] = []
        async with self._queue_cond:
            self.queue.clear()
            for j in items:
                if j.user_id == uid:
                    j.cancel_event.set()
                    j.status = "cancelled"
                    j.finished_at = time.time()
                    cancelled_queued += 1
                    removed_jobs.append((cancelled_queued, j.url))
                else:
                    kept.append(j)
            for j in kept:
                self.queue.append(j)
            # Wake the dispatcher if it was blocked in _pick_next
            # waiting for a non-empty queue.
            self._queue_cond.notify_all()

        # 3. The real cancel signal is `Job.cancel_event` (asyncio.Event),
        #    set on the cancelled job above. The dispatcher and per-card
        #    loop already observe it. The redundant `_cancel_flags` dict
        #    (audit M1) was removed.

        # 4. Reply to the user with what we did.
        if cancelled_running and cancelled_queued:
            text = (
                f"🛑 cancelled your running job and removed "
                f"<b>{cancelled_queued}</b> queued job(s)."
            )
        elif cancelled_running:
            text = "🛑 cancelled your running job -- it'll stop after the current link."
        elif cancelled_queued:
            urls = "\n".join(
                f"  {i}. {u[:90]}{'...' if len(u) > 90 else ''}"
                for i, u in removed_jobs
            )
            text = (
                f"🛑 removed <b>{cancelled_queued}</b> queued job(s):\n{urls}"
            )
        else:
            text = "ℹ️ nothing to cancel -- no running or queued job for you."
        await _send_safe(context.bot, update.effective_chat.id, text,
                          parse_mode=ParseMode.HTML)

    async def _cmd_scrapeall(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
        if update.effective_user is None or update.effective_chat is None or update.message is None:
            return
        if not bot_config.is_allowed(update.effective_user.id, self.cfg):
            return
        keyboard = InlineKeyboardMarkup([  # type: ignore[misc]
            [InlineKeyboardButton("📚 All", callback_data="scrapeall:select:all")],  # type: ignore[misc]
            [
                InlineKeyboardButton("🎬 Movies", callback_data="scrapeall:select:movies"),  # type: ignore[misc]
                InlineKeyboardButton("🎞️ Series", callback_data="scrapeall:select:series"),  # type: ignore[misc]
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="scrapeall:cancel:-")],  # type: ignore[misc]
        ])
        await update.message.reply_text("What do you want to scrape?", reply_markup=keyboard)

    async def _on_scrapeall_choice(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        if not bot_config.is_allowed(query.from_user.id, self.cfg):
            return
        data = query.data or ""
        parts = data.split(":")
        if len(parts) != 3 or parts[0] != "scrapeall":
            return
        action, kind = parts[1], parts[2]
        if action == "cancel":
            # Fourth button: dismiss the choice prompt, start nothing.
            # Editing without reply_markup removes the keyboard.
            if query.message is None:
                return
            await query.edit_message_text("❌ Scrape choice cancelled -- nothing started.")
            return
        if action not in {"select", "resume", "start"}:
            return
        if kind not in {"all", "movies", "series"}:
            return
        if query.message is None:
            return
        from xdmovies.scrape_all import has_resumable_history, load_checkpoint
        state = load_checkpoint(kind)
        if action == "select" and has_resumable_history(state):
            label = {"all": "All", "movies": "Movies", "series": "Series"}[kind]
            keyboard = InlineKeyboardMarkup([[  # type: ignore[misc]
                InlineKeyboardButton("▶ Resume", callback_data=f"scrapeall:resume:{kind}"),  # type: ignore[misc]
                InlineKeyboardButton("↻ Start from beginning", callback_data=f"scrapeall:start:{kind}"),  # type: ignore[misc]
            ]])
            await query.edit_message_text(
                f"{label} has unfinished scrape history. What do you want to do?",
                reply_markup=keyboard,
            )
            return
        if action == "select":
            action = "start"

        if self.current is not None and self.current.job_type == "scrape_all":
            await query.edit_message_text("⏳ A scrape-all job is already running.")
            return
        items: List[Job] = []
        # Audit finding M4: atomic snapshot, mutate in place.
        items = await self._snapshot_queue()
        duplicate = any(job.job_type == "scrape_all" and job.content_type == kind for job in items)
        if duplicate:
            await query.edit_message_text(f"⏳ A {kind} scrape is already queued.")
            return
        from xdmovies.scrape_all import catalog_page_url
        if action == "start":
            from xdmovies.scrape_all import ScrapeCheckpoint, save_checkpoint
            state = ScrapeCheckpoint(kind=kind)
            save_checkpoint(state)
        was_finished = state.finished
        if was_finished and action == "resume":
            await query.edit_message_text(f"ℹ️ {kind.title()} has already finished; choose start again to restart it.")
            return
        if was_finished:
            state.next_page = 1
        _q_name, _q_uname = _display_name(query.from_user)
        job = Job(
            user_id=query.from_user.id,
            chat_id=query.message.chat.id,
            url=catalog_page_url(kind, state.next_page),
            job_type="scrape_all",
            content_type=kind,
            user_name=_q_name,
            username=_q_uname,
        )
        position = await self._enqueue(job)
        action_label = "Starting from beginning" if action == "start" else "Resuming"
        await query.edit_message_text(
            f"[OK] {action_label} {kind} scrape from page {state.next_page}. Queue position: {position}."
        )

    async def _on_text(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
        if update.effective_user is None or update.effective_chat is None or update.message is None:
            return
        uid = update.effective_user.id
        if not bot_config.is_allowed(uid, self.cfg):
            return  # silent ignore
        text = (update.message.text or "").strip()
        m = URL_RE.search(text)
        if not m:
            await _send_safe(
                context.bot, update.effective_chat.id,
                "[FAIL] not an xdmovies URL. Send something like `https://top.xdmovies.wtf/movies/<slug>`",
                parse_mode=ParseMode.HTML,
            )
            return
        url = m.group(0)

        # Audit finding M3: count jobs the user already has queued
        # (not just the one running) so the busy check covers the whole
        # queue. The `behind` math previously read "pos - 1" which gave
        # 0 even with a job running + this one queued, so the user saw
        # "queued behind 0 job(s)" while still waiting.
        _q_snapshot = await self._snapshot_queue()
        queued = sum(1 for j in _q_snapshot if j.user_id == uid)

        running = self.current is not None and self.current.user_id == uid
        if running or queued > 0:
            await _send_safe(
                context.bot, update.effective_chat.id,
                "⏳ busy on your previous job -- wait for the result",
            )
            return

        depth = self._queue_depth()
        _nm, _un = _display_name(update.effective_user)
        job = Job(user_id=uid, chat_id=update.effective_chat.id, url=url,
                  user_name=_nm, username=_un)
        pos = await self._enqueue(job)
        icon, title = _pretty_title(url)
        if self.current is None and pos == 1:
            job.enqueue_msg_id = await _send_safe(
                context.bot, update.effective_chat.id,
                f"🔄 <b>Starting now</b>\n{icon} {title}",
                parse_mode=ParseMode.HTML,
            )
        else:
            behind = (pos - 1) + (1 if self.current else 0)
            eta = pos * 35  # rough guess
            job.enqueue_msg_id = await _send_safe(
                context.bot, update.effective_chat.id,
                f"📋 <b>Queued</b> -- position #{behind + 1}\n{icon} {title}\n⏳ ETA ~{_fmt_dur(eta)}",
                parse_mode=ParseMode.HTML,
            )

    # ---- lifecycle -------------------------------------------------------
    async def _post_init(self, application: "Application") -> None:
        self._app = application
        self._bot = application.bot
        _LOG.info("bot initialized (token=...%s)", self.cfg.token[-6:])
        try:
            from xdmovies.solve_server import start_server
            await start_server()
        except Exception:
            _LOG.exception("solve_server start failed")
        # hybrid keep-alive -- post-level refresh every 25m (CF is on posts, not home)
        try:
            from xdmovies.keep_alive import start_keep_alive
            await start_keep_alive(application)
            _LOG.info("keep_alive started (post refresh 25m + VPS fallback)")
        except Exception:
            _LOG.exception("keep_alive start failed (non-fatal)")
        for h in application.handlers.values():
            for hh in h:
                _LOG.info("handler: %s", type(hh).__name__)
        try:
            from telegram import BotCommand
            await application.bot.set_my_commands([
                BotCommand("start", "show welcome card"),
                BotCommand("status", "show bot health"),
                BotCommand("queue", "show running + queued jobs"),
                BotCommand("cancel", "stop my running/queued job(s)"),
                BotCommand("scrapeall", "scrape all movies or series"),
            ])
        except Exception:
            _LOG.exception("set_my_commands failed (non-fatal)")
        await self._start_dispatcher_after_polling()

    async def _post_stop(self, _application: "Application") -> None:
        try:
            from xdmovies.keep_alive import stop_keep_alive
            await stop_keep_alive()
        except Exception:
            pass
        try:
            from xdmovies.solve_server import stop_server
            await stop_server()
        except Exception:
            pass
        if self._dispatcher_task is not None:
            self._dispatcher_task.cancel()
            try:
                await self._dispatcher_task
            except asyncio.CancelledError:
                pass
            self._dispatcher_task = None

    async def _send_solve_link(self, chat_id: int, url: str, host: str, user_id: int):
        try:
            from xdmovies.solve_server import create_session, get_public_url
            token = await create_session(url, user_id, host, chat_id)
            public_url = get_public_url(token)
            msg = await self._bot.send_message(chat_id=chat_id,
                text=(f"🔒 <b>Cloudflare blocked</b> -- <code>{host}</code>\n\n"
                      f"Link: <code>{url}</code>\n\n"
                      f"👉 <b>Tap to solve (auto-captures, no paste):</b>\n{public_url}\n\n"
                      "Open it, tap checkbox on screenshot if you see it. I’ll auto-capture and delete this message to hide VPS IP."),
                parse_mode=ParseMode.HTML)
            return token, msg.message_id
        except Exception as e:
            _LOG.warning(f"solve link failed: {e}")
            return None, None

    async def _wait_for_solve(self, token: str, timeout: float = 120.0) -> bool:
        try:
            from xdmovies.solve_server import wait_for_solve
            # Fix B: trust wait_for_solve's return value. The previous
            # check `get_session(token) and get_session(token).cf_value`
            # always returned False because wait_for_solve destroys the
            # session in its finally block BEFORE this line runs. The
            # cookie is already saved to disk by _poll_for_cookie before
            # it sets sess.solved=True and calls event.set(), so the
            # capture is durable -- we don't need to re-probe the session
            # here. This also lets the post-solve message delete at the
            # call sites actually fire (they were guarded by `if
            # await self._wait_for_solve(...)`, which used to evaluate
            # False even on success).
            return bool(await wait_for_solve(token, timeout=timeout))
        except Exception:
            return False

    def build_application(self) -> "Application":
        if not _PTB_AVAILABLE:
            raise RuntimeError(
                "python-telegram-bot is not installed. "
                "Run `pip install \"python-telegram-bot>=21,<22\"`."
            )
        app = (
            ApplicationBuilder()
            .token(self.cfg.token)
            .post_init(self._post_init)
            .post_stop(self._post_stop)
            .build()
        )
        app.add_handler(CommandHandler("start", self._cmd_start))
        app.add_handler(CommandHandler("status", self._cmd_status))
        app.add_handler(CommandHandler("queue", self._cmd_queue))
        app.add_handler(CommandHandler("cancel", self._cmd_cancel))
        app.add_handler(CommandHandler("scrapeall", self._cmd_scrapeall))
        app.add_handler(CallbackQueryHandler(
            self._on_scrapeall_choice,
            pattern=r"^scrapeall:(select|resume|start):(all|movies|series)$|^scrapeall:cancel:-$",
        ))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_text))
        return app

    async def _start_dispatcher_after_polling(self) -> None:
        if self._dispatcher_task is None or self._dispatcher_task.done():
            self._dispatcher_task = asyncio.create_task(self._dispatcher())

    def run(self) -> None:
        """Synchronous entry point -- uses PTB's `run_polling` so the loop
        and ownership semantics are owned by PTB (the only sanctioned way
        to get handlers firing reliably in v21). The custom dispatcher
        is spawned from `post_init` so it shares the loop.
        """
        if not _PTB_AVAILABLE:
            raise RuntimeError("python-telegram-bot missing -- see requirements.txt")
        app = self.build_application()
        app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
            close_loop=False,
            stop_signals=None,
        )


async def main_async() -> None:
    raise RuntimeError(
        "Bot.run() is now synchronous; call `Bot.run()` directly. "
        "`main_async` is kept only for shape-compatibility."
    )


def main() -> None:
    """Sync entry point -- wraps `Bot.run`."""
    global _LOCK, _BOT_MAX_SESSIONS
    _LOCK = _acquire_single_instance_lock()
    if _LOCK is None and os.path.isfile(_LOCK_PATH):
        # On Windows _LOCK is None even on success; distinguish by lockfile
        # existence (we wrote it). On POSIX _LOCK is the fd. Check both.
        pass
    # Belt-and-suspenders: on POSIX _LOCK is the fd when held. On Windows
    # _LOCK is None but we wrote the lockfile. We can't reliably detect a
    # stolen instance on Windows without psutil; assume success if the
    # lockfile write succeeded.
    cfg = bot_config.load_config()
    bot_config.configure_logging(cfg)
    _BOT_MAX_SESSIONS = cfg.max_sessions
    _LOG.info(
        "config loaded: owner=%d admin_ids=%s max_sessions=%d",
        cfg.owner_id,
        [a for a in cfg.admin_ids] or "[]",
        cfg.max_sessions,
    )
    bot = Bot(cfg)
    try:
        bot.run()
    finally:
        try:
            if _LOCK is not None:
                os.close(_LOCK)  # type: ignore[arg-type]
        except Exception:
            pass
        try:
            if os.path.isfile(_LOCK_PATH):
                os.remove(_LOCK_PATH)
        except OSError:
            pass

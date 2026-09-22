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
import json
import logging
import os
import re
import sys
import tempfile
import time
import traceback
import types
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

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

    Returns the fd on POSIX when the lock is OURS, else None. IMPORTANT:
    on POSIX, None means ANOTHER LIVE instance holds the flock (kernel
    releases it on process death, so it can never be stale) -- main()
    must refuse to start a second poller in that case. Two pollers split
    Telegram updates between them, so one instance can run a job with
    stale code while the other answers commands: silent wrong behavior
    with zero errors in either log.
    """
    try:
        import fcntl  # type: ignore[import-not-found]
        fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            try:
                os.close(fd)
            except Exception:
                pass
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


# Movie/series pages, intermediate download links, AND direct gate links.
# A pasted latestnews URL runs as a single-card gate job: no movie-page
# fetch, no follow hop, straight to the gate drive (saves a full browser +
# page load per link). A pasted link.xdmovies.wtf/download/<token> URL runs
# as a single-card follow+gate job: it redirects to latestnews, so we keep
# the follow hop then drive the gate (same path as a movie-page card).
URL_RE = re.compile(
    r"https?://(?:top\.)?xdmovies\.wtf/(?:movies|series)/[A-Za-z0-9_\-]+"
    r"|https?://link\.xdmovies\.wtf/download/[A-Za-z0-9_\-]+"
    r"|https?://latestnewsonline\.sbs/r/[A-Za-z0-9_-]+",
    re.IGNORECASE,
)

# Per Telegram hard limit. We split at 4000 to leave room for the
# "(...continued)" prefix.
MSG_LIMIT = 4000

# Budget per results message in the append-only batch flow. Stays under
# MSG_LIMIT with room for the title header (first message) and the live
# progress footer (open message). Block-granular sealing keeps every
# send/edit far below the sizes that were timing out on mega-series.
_BATCH_CHUNK_BUDGET = MSG_LIMIT - 400
_BATCH_SEP = "─" * 25


_DETAIL_KEYS = ("rating", "genres", "air_date", "sources", "tmdb")


def _card_detail_lines(job, card) -> Tuple[str, List[Tuple[str, str]], bool]:
    """(audios, details, show_size) for one result card.

    Reads the job snapshot (`detail_fields` from /settings +
    `page_meta` scraped once per page). `details` is an ordered list of
    (label, value) for the free-form /settings fields. Falls back to the
    legacy output (Audio + Size) when no snapshot exists.
    """
    try:
        fields = set(getattr(job, "detail_fields", None) or ())
    except Exception:
        fields = set()
    try:
        meta = dict(getattr(job, "page_meta", {}) or {})
    except Exception:
        meta = {}
    if not meta:
        try:
            pa = getattr(job, "page_audios", "") or ""
        except Exception:
            pa = ""
        if pa:
            meta = {"audios": pa}
    if not fields:
        return meta.get("audios", ""), [], True
    audios = meta.get("audios", "") if "audios" in fields else ""
    details: List[Tuple[str, str]] = []
    for key in _DETAIL_KEYS:
        if key in fields and meta.get(key):
            try:
                from xdmovies import settings_store as _ss
                label = _ss.field_label(key)
            except Exception:
                label = key
            details.append((label, meta[key]))
    try:
        show_size = "size" in fields
    except Exception:
        show_size = True
    return audios, details, show_size


def _format_card_block(idx: int, card: "Card", dest: Optional[str], err: Optional[str], audios: str = "", details=None, show_size: bool = True) -> str:
    """Render one numbered result block.

    Byte-identical to the per-card section of Bot._format_batch_text, so
    global numbering (1..N) is preserved when messages are appended
    incrementally across many Telegram messages. No caps: every card is
    rendered; large series simply span more messages.

    `audios` is the page-level audio list (same for every card on the
    page); rendered as the `Audio:` line right after the title.
    `details` is an ordered list of (label, value) free-form lines from
    /settings (Rating/Genre/First Air/Source/TMDB); rendered after Size.
    """
    title_text = _html_escape(card.title or "(untitled)")
    block = [f"<b>{idx}) {title_text}</b>"]
    if audios:
        block.append(f"Audio: {_html_escape(audios)}")
    if show_size and card.size:
        block.append(f"Size: {_html_escape(card.size)}")
    for label, value in details or []:
        if value:
            block.append(f"{_html_escape(label)}: {_html_escape(value)}")
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
    return "\n".join(block)


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


def _restart_notice_path() -> str:
    try:
        return os.path.join(tempfile.gettempdir(), "xdmovies_restart_notice.json")
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".xdmovies_restart_notice.json")


def _write_restart_notice(chat_id: int) -> None:
    try:
        with open(_restart_notice_path(), "w", encoding="utf-8") as f:
            json.dump({"chat_id": chat_id, "at": time.time()}, f)
    except Exception:
        pass


def _clear_restart_notice() -> None:
    try:
        os.remove(_restart_notice_path())
    except Exception:
        pass


async def _announce_restart_if_pending(bot) -> None:
    """Send the post-restart success message if /restart left one pending.

    Called once at boot. Stale (>10 min) or missing files are silently
    dropped so an unrelated reboot never sends a phantom success note.
    Never raises.
    """
    path = _restart_notice_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return
    _clear_restart_notice()
    try:
        if time.time() - float(data.get("at", 0)) > 600:
            return
        chat_id = int(data.get("chat_id"))
    except Exception:
        return
    try:
        await bot.send_message(
            chat_id=chat_id,
            text="✅ <b>Bot restarted successfully</b> -- fresh and ready. Send a link.",
            parse_mode=ParseMode.HTML,
        )
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


def _is_gate_url(url: str) -> bool:
    """True for direct latestnewsonline.sbs gate links (single-movie jobs
    and per-card routing). Host-based, so ad lookalikes can never match.
    """
    try:
        from xdmovies.link_exec import GATE_HOST as _gh
    except Exception:
        _gh = "latestnewsonline.sbs"
    try:
        from urllib.parse import urlparse
        h = (urlparse(url or "").hostname or "").lower()
        return bool(h) and (h == _gh or h.endswith("." + _gh))
    except Exception:
        return False


def _is_download_url(url: str) -> bool:
    """True for intermediate link.xdmovies.wtf/download/<token> links.

    These redirect to the latestnews gate, so a pasted one runs as a
    single-card follow+gate job (NOT gate-direct: the follow hop is
    required). Host + path based, so ad lookalikes can never match.
    """
    try:
        from urllib.parse import urlparse
        u = urlparse(url or "")
        h = (u.hostname or "").lower()
        if not h or (h != "link.xdmovies.wtf" and not h.endswith(".link.xdmovies.wtf")):
            return False
        path = (u.path or "").lower()
        return path.startswith("/download/") and len(path) > len("/download/")
    except Exception:
        return False


async def _enrich_hub_dest(dest: str, card) -> str:
    """Best-effort metadata enrichment for a hub /drive/ URL.

    Policy (operator choice): STOP at the hub page (hubcloud/hubdrive/...).
    Do NOT follow the "Generate Link" redirect to the gamerxyt token URL.
    Only fills real title/size from the hub page via the in-browser cache
    (filled when a flow already loaded that hub page behind CF) or one
    static HTTPS read. Returns dest unchanged (never the gamerxyt URL).
    Never raises (callers treat enrichment as optional)."""
    try:
        from xdmovies.link_exec import (
            resolve_hub_details as _rhd,
            get_hub_details as _ghd,
        )
        try:
            _cached = _ghd(dest)
        except Exception:
            _cached = None
        if _cached:
            _rf, _rt, _rs = _cached
        else:
            try:
                _rf, _rt, _rs = await _rhd(dest)
            except Exception:
                return dest
        # Keep the hub URL as-is; _rf (gamerxyt token URL) is
        # deliberately ignored — only _rt/_rs below are used.
        if _rf and _rf != dest:
            _LOG.info("hub dest kept at hub page (gamerxyt upgrade skipped): %s", dest[:100])
        try:
            if _rt and (not card.title or card.title in ("Gate link", "Download link")):
                card.title = _rt
                card.file_name = card.file_name or _rt
            if _rs and not card.size:
                card.size = _rs
        except Exception:
            pass
        return dest
    except Exception:
        return dest


async def _finalize_gate_card(card, dest: str, result) -> str:
    """Finish a gate-resolved card: upgrade to the final hub URL and fill
    the real title/size from the hub page (the gate page itself carries
    no movie name, so gate-direct cards start empty). Also points the
    batch header at the real title when it still shows the placeholder.
    Returns (possibly upgraded) dest. Never raises."""
    try:
        dest = await _enrich_hub_dest(dest, card)
    except Exception:
        pass
    try:
        if (result is not None
                and getattr(result, "page_title", "") in ("Gate link", "Download link", "", "(untitled)")
                and card.title and card.title not in ("Gate link", "Download link")):
            result.page_title = card.title
    except Exception:
        pass
    return dest


MAX_LINKS_PER_MESSAGE = 10000


def _extract_links(text: str, limit: int = MAX_LINKS_PER_MESSAGE) -> Tuple[List[str], int]:
    """All supported URLs in a message: (deduped list up to limit, dropped count).

    Lets users paste several links (one per line) in a single message;
    each becomes its own queued job, resolved one by one.
    """
    out: List[str] = []
    seen = set()
    try:
        for m in URL_RE.finditer(text or ""):
            u = m.group(0)
            if u in seen:
                continue
            seen.add(u)
            if len(out) < limit:
                out.append(u)
    except Exception:
        pass
    return out, max(0, len(seen) - len(out))


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


# /scrapeallchannel setup: user_id -> timestamp of the "forward a channel
# message" prompt. Entries expire after 10 minutes. In-memory only; a
# restart drops pending setups (user just re-sends the command).
_SCRAPECHAN_PENDING: Dict[int, float] = {}
_SCRAPECHAN_PENDING_TTL = 600.0


# /search pick cache: user_id -> (timestamp, query, [SearchResult]).
# Entries expire after 20 minutes -- must comfortably exceed the
# 15-minute channel-confirm window or late Yes taps would find the
# cache gone. In-memory only; a restart drops pending picks (user just
# re-sends /search).
_SEARCH_CACHE: Dict[int, Tuple[float, str, list]] = {}
_SEARCH_CACHE_TTL = 1200.0
_SEARCH_FETCH_LIMIT = 30
_SEARCH_PAGE_SIZE = 5

# Accept-all content choice: which cards of each series page to resolve.
# "all" keeps everything (previous behavior). "episodes"/"packs" keeps
# only cards whose title element was classed episode-title/pack-title.
# Shown after ✅ Accept all, before the scrape-to-channel question.
_CONTENT_FILTERS = ("all", "episodes", "packs")
_CONTENT_LABEL = {
    "all": "All (EP+Pack)",
    "episodes": "Episodes only",
    "packs": "Packs/Zips only",
}
_CONTENT_CALLBACK = {"all": "all", "ep": "episodes", "pack": "packs"}

# .txt batch search: max titles read per file. Each title costs a live
# search (~1s), so an 800-line file means ~15 min before the list
# appears -- raise via XDMOVIES_TXT_MAX_LINES if you accept the wait.
# Oversize files are rejected before download.
_TXT_MAX_LINES = int(os.environ.get("XDMOVIES_TXT_MAX_LINES", "10000") or "10000")
_TXT_MAX_BYTES = 65536


def _extract_forwarded_channel(message) -> Tuple[Optional[int], str]:
    """(channel_id, title) from a message forwarded out of a channel.

    Prefers the modern `forward_origin` (MessageOriginChannel), falls back
    to legacy `forward_from_chat`. Anything else (user forwards, hidden
    senders, plain messages) yields (None, ""). Never raises.
    """
    try:
        origin = getattr(message, "forward_origin", None)
        if origin is not None:
            otype = getattr(origin, "type", "") or ""
            clsname = type(origin).__name__
            if otype == "channel" or clsname == "MessageOriginChannel":
                chat = getattr(origin, "chat", None)
                cid = getattr(chat, "id", None)
                title = getattr(chat, "title", "") or ""
                if isinstance(cid, bool):
                    pass
                elif isinstance(cid, int) and cid < 0:
                    return cid, str(title)
        legacy = getattr(message, "forward_from_chat", None)
        if legacy is not None:
            cid = getattr(legacy, "id", None)
            title = getattr(legacy, "title", "") or ""
            if isinstance(cid, int) and not isinstance(cid, bool) and cid < 0:
                return cid, str(title)
    except Exception:
        pass
    return None, ""


# --------------------------------------------------------------------------
# Search pagination helpers (module-level; used by _cmd_search/_on_search_pick)
# --------------------------------------------------------------------------

# (no cap on accept-all per owner: unlimited, deduped; the single
# worker resolves serially so this only affects queue length)

# Accept-all channel confirm: user_id -> (token, chat_id, message_id,
# user_name, username). Tapping ✅ Accept all asks "to channel?" with a
# 15-minute timeout that defaults to NO (scrape in DM). The token
# invalidates stale timeout tasks (re-tap restarts the clock).
_SEARCH_CONFIRM: Dict[int, tuple] = {}
_SEARCH_CONFIRM_TIMEOUT = 900.0

# /qmode series quality picker: pid -> {uid, chat_id, msg_id,
# groups (List[QualityGroup]), selected (set[int]), event, verdict}.
# Entries live only for the pick window (2m); answered/timed-out
# pickers are popped. In-memory only -- the ON/OFF setting itself is
# persisted in qmode_store, the transient picker state is not.
_QPICK: Dict[str, dict] = {}
QPICK_TIMEOUT = 120.0
# Telegram caps a message at ~100 buttons; way below that we give up
# on choosing and resolve everything rather than paginating a picker.
QPICK_MAX_GROUPS = 40

# /dmode series section picker: pid -> {uid, chat_id, msg_id,
# groups (List[SectionGroup]), selected (set[int]), event, verdict}.
# Fully separate from _QPICK (different dict, different callback prefix
# `dp:`) so the two pickers can never collide, even when both run in
# sequence for one job. Same 2m window / transient semantics as qmode.
_DPICK: Dict[str, dict] = {}
DPICK_TIMEOUT = 120.0
DPICK_MAX_GROUPS = 40


def _fit_button(s: str, limit: int = 60) -> str:
    """Truncate to a byte budget (Bot API button text is 1-64 bytes;
    char counting overruns on non-ASCII scraped titles)."""
    if len(s.encode("utf-8")) <= limit:
        return s
    out = s
    while out and len((out + "...").encode("utf-8")) > limit:
        out = out[:-1]
    return (out + "...") if out else s[:0]


def _qpick_keyboard(pid: str, groups, selected) -> "InlineKeyboardMarkup":
    """Toggle grid, 2 buttons per row + Select All / Done / Cancel footer.
    Callback payloads stay far under the 64-byte Telegram limit
    (`qp:<8hex>:t:<gi>`)."""
    rows = []
    row = []
    for gi, g in enumerate(groups):
        label = _fit_button(f"{'✅ ' if gi in selected else ''}{g.label}")
        row.append(InlineKeyboardButton(label, callback_data=f"qp:{pid}:t:{gi}"))  # type: ignore[misc]
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([
        InlineKeyboardButton("☑️ Select All", callback_data=f"qp:{pid}:all"),  # type: ignore[misc]
    ])
    rows.append([
        InlineKeyboardButton("✅ Done", callback_data=f"qp:{pid}:done"),  # type: ignore[misc]
        InlineKeyboardButton("❌ Cancel", callback_data=f"qp:{pid}:cancel"),  # type: ignore[misc]
    ])
    return InlineKeyboardMarkup(rows)  # type: ignore[misc]


def _best_picker_title(page_title: str, job_url: str = "") -> str:
    """Full title for the picker header.

    og:title is cleanest, but when it parses short (e.g. "India" for
    "Indias Got Latent" -- apostrophe/entity split) the URL slug still
    holds the full name. Prefer the longer informative one.
    """
    try:
        base = (page_title or "").strip()
        slug_title = ""
        try:
            from xdmovies.pipeline import _title_from_slug
            slug_title = (_title_from_slug(job_url or "") or "").strip()
        except Exception:
            slug_title = ""
        if slug_title:
            if not base:
                return slug_title
            try:
                if len(slug_title.split()) > len(base.split()) and (
                    len(base.split()) <= 1 or len(slug_title) > len(base) + 6
                ):
                    return slug_title
            except Exception:
                pass
        return base
    except Exception:
        return (page_title or "").strip()


def _qpick_text(page_title: str, groups) -> str:
    """Picker body (HTML): season headers + usage line. Group labels
    are page-scraped text, so they are escaped at render time."""
    lines = ["What quality you want?"]
    if page_title:
        lines.append(f"<b>{_html_escape(page_title.strip()[:80])}</b>")
    last_section = None
    for g in groups:
        if g.section != last_section:
            lines.append(f"<b>{_html_escape(g.section)}:</b>")
            last_section = g.section
    lines.append("Tap to select (✅) or Select All. Done = resolve picked (empty = all). 2m no choice = cancel.")
    return "\n".join(lines)


def _dpick_keyboard(pid: str, groups, selected) -> "InlineKeyboardMarkup":
    """Toggle grid for the /dmode section picker. Separate `dp:` callback
    namespace from the qmode `qp:` one so taps can never cross-talk.
    Callback payloads stay far under the 64-byte Telegram limit
    (`dp:<8hex>:t:<gi>`)."""
    rows = []
    row = []
    for gi, g in enumerate(groups):
        label = _fit_button(f"{'✅ ' if gi in selected else ''}{g.label}")
        row.append(InlineKeyboardButton(label, callback_data=f"dp:{pid}:t:{gi}"))  # type: ignore[misc]
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([
        InlineKeyboardButton("☑️ Select All", callback_data=f"dp:{pid}:all"),  # type: ignore[misc]
    ])
    rows.append([
        InlineKeyboardButton("✅ Done", callback_data=f"dp:{pid}:done"),  # type: ignore[misc]
        InlineKeyboardButton("❌ Cancel", callback_data=f"dp:{pid}:cancel"),  # type: ignore[misc]
    ])
    return InlineKeyboardMarkup(rows)  # type: ignore[misc]


def _dpick_text(page_title: str, groups) -> str:
    """Picker body (HTML) for /dmode: season headers + usage line."""
    lines = ["What sections you want? (Packs vs Episodes per season)"]
    if page_title:
        lines.append(f"<b>{_html_escape(page_title.strip()[:80])}</b>")
    last_section = None
    for g in groups:
        if g.section_header != last_section:
            lines.append(f"<b>{_html_escape(g.section_header)}:</b>")
            last_section = g.section_header
    lines.append("Tap to select (✅) or Select All. Done = resolve picked (empty = all). 2m no choice = cancel.")
    return "\n".join(lines)


def _settings_keyboard(prefs) -> "InlineKeyboardMarkup":
    """Toggle grid for /settings. Instant-apply: every tap flips one
    field and the message markup re-renders. Callback payloads
    (`settings:toggle:<key>`) stay far under the 64-byte limit."""
    try:
        from xdmovies import settings_store as _ss
        fields = list(_ss.ALL_FIELDS)
    except Exception:
        return InlineKeyboardMarkup([])  # type: ignore[misc]
    rows = []
    row = []
    for key, label, emoji in fields:
        on = bool((prefs or {}).get(key, False))
        row.append(InlineKeyboardButton(
            f"{'✅ ' if on else ''}{emoji} {label}",
            callback_data=f"settings:toggle:{key}"))  # type: ignore[misc]
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([
        InlineKeyboardButton("☑️ Select All", callback_data="settings:all"),  # type: ignore[misc]
    ])
    rows.append([
        InlineKeyboardButton("❌ Close", callback_data="settings:close"),  # type: ignore[misc]
    ])
    return InlineKeyboardMarkup(rows)  # type: ignore[misc]


def _settings_text() -> str:
    return "What details you want in result cards?"


async def _send_markup_safe(bot, chat_id: int, text: str, reply_markup) -> Optional[int]:
    """_send_safe equivalent for messages carrying an inline keyboard:
    HTML first, plain-text retry (markup kept) on BadRequest. Returns
    the message_id or None."""
    if bot is None:
        return None
    try:
        msg = await bot.send_message(
            chat_id=chat_id, text=text,
            reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        return msg.message_id
    except _TelegramError.BadRequest:
        _LOG.warning("picker send BadRequest -- retrying plain")
        try:
            msg = await bot.send_message(
                chat_id=chat_id,
                text=text.replace("<b>", "").replace("</b>", ""),
                reply_markup=reply_markup)
            return msg.message_id
        except Exception:
            _LOG.exception("picker plain-text retry also failed")
            return None
    except Exception:
        _LOG.exception("picker send failed")
        return None

def _search_confirm_human_timeout() -> str:
    if _SEARCH_CONFIRM_TIMEOUT >= 60:
        mins = _SEARCH_CONFIRM_TIMEOUT / 60
        return f"{mins:.0f} min" if mins == int(mins) else f"{mins:.1f} min"
    return f"{_SEARCH_CONFIRM_TIMEOUT:.0f}s"

def _search_page_keyboard(results, page: int):
    """(page, total_pages, InlineKeyboardMarkup) for one 5-per-page slice.
    Page is 1-based and clamped. Module-level (must stay outside class Bot)."""
    total_pages = max(1, (len(results) + _SEARCH_PAGE_SIZE - 1) // _SEARCH_PAGE_SIZE)
    page = max(1, min(page, total_pages))
    start = (page - 1) * _SEARCH_PAGE_SIZE
    rows = []
    for idx in range(start, min(start + _SEARCH_PAGE_SIZE, len(results))):
        res = results[idx]
        icon = "🎬" if res.kind == "movie" else "📺"
        title = (res.title or "").strip()
        year = (res.year or "").strip()
        # API titles sometimes already end with the year ("Sweetpea
        # (2024)") -- don't print it twice.
        if year and not title.endswith(f"({year})"):
            label = f"{icon} {title} ({year})"
        else:
            label = f"{icon} {title}"
        if len(label) > 60:
            label = label[:57] + "..."
        rows.append([InlineKeyboardButton(label, callback_data=f"search:pick:{idx}")])  # type: ignore[misc]
    nav = []
    if len(results) > 1:
        # Bulk row above the nav: enqueue everything (capped). Only
        # shown when there is actually a choice to bulk-accept.
        nav.append(InlineKeyboardButton("✅ Accept all", callback_data="search:acceptall"))  # type: ignore[misc]
        rows.append(nav)
        nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"search:page:{page - 1}"))  # type: ignore[misc]
    nav.append(InlineKeyboardButton("❌ Close", callback_data="search:close"))  # type: ignore[misc]
    if page < total_pages:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"search:page:{page + 1}"))  # type: ignore[misc]
    rows.append(nav)
    return page, total_pages, InlineKeyboardMarkup(rows)  # type: ignore[misc]


def _search_page_text(query: str, results, page: int, total_pages: int) -> str:
    n_series = sum(1 for r in results if getattr(r, "kind", "") == "series")
    n_movies = len(results) - n_series
    text = (f"🔎search query:{_html_escape(query)}\n"
            f"📺Series:{n_series}\n"
            f"🎥Movies:{n_movies}\n"
            f"\n📑current page:{page}")
    if total_pages > 1:
        text += f"/{total_pages}"
    return text


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
    # Channel-scrape routing (/scrapeallchannel): results post to this
    # chat instead of the user's chat. silent=True suppresses per-card
    # progress + batch + summary chatter (solve-link taps and FAILs still
    # reach the user -- they need a human).
    post_to_channel: int = 0
    silent: bool = False
    # Batched results message -- we edit a single Telegram message instead of
    # sending one per card. List because long outputs may need >1 message.
    batch_msg_ids: List[int] = field(default_factory=list)
    # Incremental batch state (append-only results delivery): number of
    # cards already sealed into sent messages. _send_or_edit_batch only
    # renders/sends cards[sealed:] so per-card Telegram work stays O(1)
    # no matter how big the series gets. No caps: every card is still
    # delivered, just across more messages instead of re-sending all.
    batch_sealed_count: int = 0
    # Running card progress for /queue + per-card INFO logs.
    cards_total: int = 0
    cards_done: int = 0
    # Accept-all content choice: "all" | "episodes" | "packs". Filters the
    # extracted cards before resolution (episode-title vs pack-title
    # cards). Default "all" preserves previous behavior everywhere
    # (single picks, pasted URLs, scrape-all children).
    card_filter: str = "all"
    # Page-level audio list (e.g. "Hindi, English, Tamil") scraped once
    # from the movie/series info block; rendered as the `Audio:` line in
    # every result card. Empty for gate/download-direct jobs (no page).
    page_audios: str = ""
    # Free-form detail snapshot for /settings: page-level meta
    # {audios, rating, genres, air_date, sources, tmdb} + the user's
    # enabled field keys at job start. Rendered by _card_detail_lines.
    page_meta: dict = field(default_factory=dict)
    detail_fields: List[str] = field(default_factory=list)
    # Urgent lane: 0 = normal FIFO, 1 = urgent (user link sent while a
    # channel scrape runs). Urgent jobs are stored in Bot._urgent and
    # drained at movie boundaries inside _run_scrape_all, then the
    # scrape resumes. Never set directly — use _enqueue(..., urgent=True).
    priority: int = 0


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
        # Urgent lane: resolve jobs sent while a scrape_all is running.
        # Shares _queue_cond/_queue_lock so snapshots + waits stay atomic.
        # Drained movie-by-movie inside _run_scrape_all; _pick_next also
        # prefers it so a queued-but-not-started scrape can't starve it.
        self._urgent: collections.deque[Job] = collections.deque()
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

    def _urgent_depth(self) -> int:
        return len(self._urgent)

    def _total_depth(self) -> int:
        return len(self.queue) + len(self._urgent)

    def _is_scrape_running(self) -> bool:
        cur = self.current
        return cur is not None and getattr(cur, "job_type", "") == "scrape_all"

    async def _enqueue(self, job: Job, urgent: bool = False) -> int:
        async with self._queue_cond:
            if urgent:
                job.priority = 1
                self._urgent.append(job)
            else:
                job.priority = 0
                self.queue.append(job)
            # Wake the dispatcher (if blocked in _pick_next waiting for
            # the queue to become non-empty).
            self._queue_cond.notify_all()
            return len(self._urgent) + len(self.queue)

    async def _pop_urgent(self) -> Optional[Job]:
        """Pop one urgent job (FIFO). None when empty. Never blocks."""
        async with self._queue_cond:
            if not self._urgent:
                return None
            return self._urgent.popleft()

    def _has_urgent_sync(self) -> bool:
        return len(self._urgent) > 0

    async def _pick_next(self) -> Job:
        # Block until either lane has a job, then atomically pop.
        # Urgent lane first so a queued-but-not-started scrape can't
        # starve an urgent link. Replaces the previous `popleft()` which
        # raised IndexError on every empty-queue iteration, flooding the
        # log with `dispatcher loop error; sleeping 1s`.
        async with self._queue_cond:
            while not self.queue and not self._urgent:
                await self._queue_cond.wait()
            if self._urgent:
                return self._urgent.popleft()
            return self.queue.popleft()

    async def _snapshot_queue(self) -> List[Job]:
        """Atomic snapshot under the queue lock."""
        async with self._queue_lock:
            return list(self.queue)

    async def _snapshot_urgent(self) -> List[Job]:
        """Atomic snapshot of the urgent lane."""
        async with self._queue_lock:
            return list(self._urgent)

    async def _drain_urgent(self, parent_job: Job) -> int:
        """Run every queued urgent job inline, then return count.

        Called at movie boundaries inside _run_scrape_all (after the
        current movie hit 10/10 + save_checkpoint). Keeps a single
        `current` (the scrape stays current) so no extra browser slots
        are used and the dispatcher stays blocked. Each urgent runs the
        full _run_for_user flow (solve-links, batch, summary) with its
        own fresh cancel_event. Cancelled urgents are skipped. Never
        raises — one failing urgent never aborts the scrape.
        """
        ran = 0
        while True:
            if parent_job.cancel_event.is_set():
                break
            urgent = await self._pop_urgent()
            if urgent is None:
                break
            if urgent.cancel_event.is_set():
                continue
            try:
                if self._bot is not None and not getattr(urgent, "silent", False):
                    try:
                        await self._set_progress(
                            urgent,
                            "⚡ <b>Urgent link</b> — running now (channel scrape paused).",
                        )
                    except Exception:
                        pass
                await self._drop_enqueue_note(urgent)
                try:
                    await self._run_for_user(urgent)
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    _LOG.exception("urgent job failed: %s", urgent.url)
                    try:
                        urgent.error = f"{exc.__class__.__name__}: {exc}"
                        urgent.status = "cancelled" if urgent.cancel_event.is_set() else "error"
                        urgent.finished_at = time.time()
                    except Exception:
                        pass
                finally:
                    try:
                        await self._drop_job_clutter(urgent)
                    except Exception:
                        pass
                ran += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOG.exception("urgent drain loop error (non-fatal)")
                continue
        return ran

    # ---- progress messages ----------------------------------------------
    async def _set_progress(self, job: Job, text: str) -> None:
        if self._bot is None:
            _LOG.debug("progress skipped: bot not yet initialized")
            return
        if getattr(job, "silent", False):
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
            if _is_gate_url(job.url):
                # Direct gate link pasted in chat: single-card job. No
                # movie page fetch (nothing to extract) and the per-card
                # loop below skips the follow hop for gate hosts --
                # straight to the drive.
                cards = [Card(title="", file_name="", url=job.url)]
                result = types.SimpleNamespace(page_title="Gate link", html_len=0, cards=cards)
            elif _is_download_url(job.url):
                # Intermediate download link pasted in chat: single-card
                # job with NO movie page fetch, but KEEP the follow hop
                # (it redirects to the latestnews gate). The per-card
                # loop below follows it then drives the gate -- same as
                # a movie-page card. Title starts empty so hub enrichment
                # fills the real filename (same as gate-direct).
                cards = [Card(title="", file_name="", url=job.url)]
                result = types.SimpleNamespace(page_title="Gate link", html_len=0, cards=cards)
            else:
                result = await _extract_with_diagnostics(  # type: ignore[misc]
                job.url, session_id=session_id, headless=self.headless
            )
            cards = result.cards
            try:
                job.page_audios = getattr(result, "audios", "") or ""
            except Exception:
                pass
            try:
                from xdmovies import settings_store as _ss
                from xdmovies.extractor import extract_tmdb_code
                job.detail_fields = _ss.enabled_fields(job.user_id)
                job.page_meta = {
                    "audios": getattr(result, "audios", "") or "",
                    "rating": getattr(result, "rating", "") or "",
                    "genres": getattr(result, "genres", "") or "",
                    "air_date": getattr(result, "air_date", "") or "",
                    "sources": getattr(result, "sources", "") or "",
                    "tmdb": extract_tmdb_code(job.url or ""),
                }
            except Exception:
                pass
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
                        result = await _extract_with_diagnostics(job.url, session_id=session_id, headless=False)  # type: ignore[misc]
                        cards = result.cards
                        try:
                            job.page_audios = getattr(result, "audios", "") or ""
                        except Exception:
                            pass
                        try:
                            from xdmovies import settings_store as _ss2
                            from xdmovies.extractor import extract_tmdb_code as _tmdb2
                            job.detail_fields = _ss2.enabled_fields(job.user_id)
                            job.page_meta = {
                                "audios": getattr(result, "audios", "") or "",
                                "rating": getattr(result, "rating", "") or "",
                                "genres": getattr(result, "genres", "") or "",
                                "air_date": getattr(result, "air_date", "") or "",
                                "sources": getattr(result, "sources", "") or "",
                                "tmdb": _tmdb2(job.url or ""),
                            }
                        except Exception:
                            pass
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
                _vps = os.environ.get("SOLVE_SERVER_PUBLIC_BASE", "http://VPS:8080").rstrip("/")
                await _send_safe(self._bot, job.chat_id, f"🔒 <b>Cloudflare blocked</b> -- <code>{host}</code>\nLink: <code>{blocked_url}</code>\nCould not create solve link -- check VPS {_vps}", parse_mode=ParseMode.HTML)
                job.status = "error"
                job.error = f"cf_challenge:{host}"
                job.finished_at = time.time()
                return
        except Exception as exc:
            job.error = f"extract: {exc!r}"
            await self._fail(job, session_id)
            return

        # Accept-all content choice (Episodes / Packs-Zips / All): narrow
        # the extracted cards before resolving. Pages with no kind info
        # (and gate-direct single cards) pass through untouched.
        if getattr(job, "card_filter", "all") in ("episodes", "packs"):
            from xdmovies.extractor import filter_cards_by_content
            _kept = filter_cards_by_content(cards, job.card_filter)
            if not _kept and cards:
                _label = _CONTENT_LABEL.get(job.card_filter, job.card_filter)
                _LOG.info(
                    "job card filter %s matched 0/%d cards for %s",
                    job.card_filter, len(cards), job.url,
                )
                await _send_safe(
                    self._bot, job.chat_id,
                    f"[FAIL] no {_html_escape(_label)} links found on that page "
                    f"(title: <code>{_html_escape((result.page_title or '').strip()[:80])}</code>). "
                    f"The page exists but has no {_html_escape(_label.lower())} section.",
                    parse_mode=ParseMode.HTML,
                )
                job.status = "done"
                job.finished_at = time.time()
                self.last_solve_ago = 0.0
                return
            if len(_kept) != len(cards):
                _LOG.info(
                    "job card filter %s: %d/%d cards kept for %s",
                    job.card_filter, len(_kept), len(cards), job.url,
                )
            cards = _kept
            result.cards = _kept

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

        # /dmode section picker (series only, dmode ON, 2+ groups):
        # narrow the card list before the resolve loop. None = the user
        # cancelled at the picker (or /cancel fired mid-pick).
        # Runs FIRST so /qmode (when also ON) only offers qualities
        # present in the chosen sections.
        try:
            _dpicked = await self._ask_download(
                job, cards, page_title=(result.page_title or ""))
        except Exception:
            _LOG.exception("section picker failed (non-fatal) -- resolving all")
            _dpicked = list(cards)
        if _dpicked is None:
            job.status = "cancelled"
            job.finished_at = time.time()
            return
        if [c.url for c in _dpicked] != [c.url for c in cards]:
            _LOG.info(
                "section picker: %d/%d cards kept for %s",
                len(_dpicked), len(cards), job.url,
            )
            cards = _dpicked
            try:
                result.cards = _dpicked
            except Exception:
                pass

        # /qmode quality picker (series only, qmode ON, 2+ groups):
        # narrow the card list before the resolve loop. None = the user
        # cancelled at the picker (or /cancel fired mid-pick).
        try:
            _picked = await self._ask_quality(
                job, cards, page_title=(result.page_title or ""))
        except Exception:
            _LOG.exception("quality picker failed (non-fatal) -- resolving all")
            _picked = list(cards)
        if _picked is None:
            job.status = "cancelled"
            job.finished_at = time.time()
            return
        if not _picked and cards:
            # Empty intersection: qmode matched nothing inside the dmode
            # set. Keep the dmode selection rather than resolving zero.
            _LOG.info(
                "quality picker matched 0/%d section-picked cards for %s -- keeping section set",
                len(cards), job.url,
            )
            try:
                await _send_safe(
                    self._bot, job.chat_id,
                    f"⚠️ quality matched nothing in your sections -- resolving the "
                    f"<b>{len(cards)}</b> section links instead.",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            _picked = list(cards)
        if [c.url for c in _picked] != [c.url for c in cards]:
            _LOG.info(
                "quality picker: %d/%d cards kept for %s",
                len(_picked), len(cards), job.url,
            )
            cards = _picked
            try:
                result.cards = _picked
            except Exception:
                pass

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
        # Progress counters for /queue + per-card INFO logs. The batch flow
        # reads cards_total for its "x/y delivered" footer.
        job.cards_total = n
        job.cards_done = 0
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
                    # no latestnews gate. Enrich to the final URL and
                    # fill any missing title/size from the hub page.
                    dest = await _finalize_gate_card(
                        card, card.url,
                        types.SimpleNamespace(page_title="", html_len=0, cards=[card]))
                    _LOG.info("direct dest card, skipping follow+gate: %s", card.url[:100])
                    results.append((card, dest, None))
                    job.resolved += 1
                    try:
                        await self._send_or_edit_batch(job, result.page_title, results, in_progress_idx=i)
                    except Exception:
                        _LOG.debug("batch edit failed at card %d/%d", i, n)
                    continue
            except Exception:
                pass
            try:
                if _is_gate_url(card.url):
                    # Already a gate link (incl. direct-paste jobs): the
                    # follow hop would just reload this same page in
                    # another browser. Skip straight to the gate drive.
                    url2, status2, page_text = card.url, 200, ""
                    _LOG.info("gate card, skipping follow hop: %s", card.url[:100])
                else:
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
                                                    dest = await _finalize_gate_card(card, dest, result)
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
                            dest = await _finalize_gate_card(card, dest, result)
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
                                        dest = await _finalize_gate_card(card, dest, result)
                                        results.append((card, dest, None))
                                        job.resolved += 1
                                        try:
                                            await self._send_or_edit_batch(job, result.page_title, results, in_progress_idx=i)
                                        except Exception:
                                            _LOG.debug("batch edit failed at card %d/%d", i, n)
                                        continue
                                except: pass
                                try:
                                    url2r, _, _ = await _follow_download_link(card.url, session_id=shared_session, headless=self.headless)  # type: ignore
                                    if url2r:
                                        try:
                                            dest = await _execute_button_flow(url2r, session_id=shared_session, headless=False)  # type: ignore
                                            if dest:
                                                dest = await _finalize_gate_card(card, dest, result)
                                                results.append((card, dest, None))
                                                job.resolved += 1
                                                try:
                                                    await self._send_or_edit_batch(job, result.page_title, results, in_progress_idx=i)
                                                except Exception:
                                                    _LOG.debug("batch edit failed at card %d/%d", i, n)
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
                _LOG.debug("batch edit failed at card %d/%d", i, n)

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
            if not getattr(job, "silent", False):
                await self._bot.send_message(
                    chat_id=job.chat_id, text=summary, parse_mode=ParseMode.HTML
                )
        except Exception:
            _LOG.exception("summary send failed")
        # Auto-channel mirror: same batch + summary to the preset channel.
        try:
            await self._mirror_job_to_channel(job, result.page_title, results, summary)
        except Exception:
            pass

        job.status = "done" if job.failed == 0 else "done_with_errors"
        job.finished_at = time.time()
        self.last_solve_ago = 0.0
        # remember movie posts for keep-alive refresh (CF is on posts, not
        # home). Gate-direct and download-direct jobs have no post page --
        # nothing to remember.
        if not _is_gate_url(job.url) and not _is_download_url(job.url):
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
                            user_name=job.user_name, username=job.username,
                            post_to_channel=job.post_to_channel,
                            silent=bool(job.post_to_channel))
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
                # Urgent lane (movie boundary): the current movie just hit
                # 10/10 + checkpoint. If a user sent an urgent link while
                # it resolved, run all urgents now, then resume the scrape
                # with the next movie. Never aborts the finished movie.
                try:
                    if self._has_urgent_sync() and not job.cancel_event.is_set():
                        try:
                            if self._bot is not None:
                                await _send_safe(
                                    self._bot, job.chat_id,
                                    "⏸ <b>Channel scrape paused</b> — ⚡ urgent link(s) running now. "
                                    "Resuming right after.",
                                    parse_mode=ParseMode.HTML,
                                )
                        except Exception:
                            pass
                        try:
                            _n_urg = await self._drain_urgent(job)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            _LOG.exception("urgent drain failed (non-fatal)")
                            _n_urg = 0
                        if not job.cancel_event.is_set():
                            try:
                                if self._bot is not None:
                                    await _send_safe(
                                        self._bot, job.chat_id,
                                        f"▶️ <b>Channel scrape resumed</b> — {int(_n_urg or 0)} urgent job(s) done. "
                                        f"Continuing <b>{kind}</b> page <b>{page_number}</b>.",
                                        parse_mode=ParseMode.HTML,
                                    )
                            except Exception:
                                pass
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _LOG.exception("urgent pause/resume note failed (non-fatal)")

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
            if getattr(job, "post_to_channel", 0):
                # Channel mode: links already posted per movie; user chat
                # gets one completion line, no counts/summary chatter.
                text = f"✅ <b>{kind}</b> scrape done -- posted to the channel."
            else:
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
        audios: str = "",
        details=None,
    ) -> str:
        """Render the entire batched results message in one HTML string.

        Layout (matches the user's expected format):
          {movie/series name}:
          <blank>
          1) <title> [audio] [size] [details...] [lang]
          ─────────────────────────
          → <hubcloud url>
          ─────────────────────────
          2) <title> ...
        """
        sep = _BATCH_SEP
        title_esc = _html_escape(page_title or "(untitled)")
        out: List[str] = [f"<b>{title_esc}:</b>"]
        for i, (card, dest, err) in enumerate(cards, 1):
            out.append(_format_card_block(i, card, dest, err, audios, details))
            out.append(sep)
        # remove trailing separator for clean look
        if out and out[-1] == sep:
            out.pop()
        return "\n\n".join(out)

    async def _tg_send_text(self, target: int, text: str) -> Optional[int]:
        """Send one text chunk with TimedOut backoff + BadRequest fallback.

        TimedOut (the mega-series batch killer) is retried 3x with
        exponential backoff and logged at WARNING, not ERROR, so one
        slow Telegram roundtrip no longer floods xdmovies_bot_errors.log.
        BadRequest falls back to plain text once. Never raises.
        """
        if not text or self._bot is None:
            return None
        delay = 2.0
        for attempt in range(3):
            try:
                msg = await self._bot.send_message(
                    chat_id=target, text=text, parse_mode=ParseMode.HTML
                )
                try:
                    return msg.message_id
                except Exception:
                    return None
            except _TelegramError.BadRequest as exc:
                _LOG.warning("batch HTML send failed (%s) -- retrying plain", exc)
                try:
                    msg = await self._bot.send_message(chat_id=target, text=text)
                    try:
                        return msg.message_id
                    except Exception:
                        return None
                except Exception as exc2:
                    _LOG.warning("batch plain send failed: %r", exc2)
                    return None
            except _TelegramError.TimedOut:
                _LOG.warning(
                    "batch send TimedOut (attempt %d/3) -- backing off %.0fs",
                    attempt + 1, delay,
                )
                await asyncio.sleep(delay)
                delay *= 2
                continue
            except Exception as exc:
                _LOG.warning("batch send failed: %r", exc)
                return None
        _LOG.warning("batch send gave up after 3 TimedOut attempts (non-fatal)")
        return None

    async def _tg_edit_text(self, target: int, message_id: int, text: str) -> bool:
        """Edit one message with TimedOut backoff. Never raises."""
        if not text or self._bot is None:
            return False
        delay = 2.0
        for attempt in range(3):
            try:
                await self._bot.edit_message_text(
                    chat_id=target,
                    message_id=message_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                )
                return True
            except _TelegramError.BadRequest as exc:
                # "message is not modified" is harmless when the
                # text didn't change yet
                if "not modified" in str(exc).lower():
                    return True
                _LOG.warning("batch HTML edit failed (%s) -- retrying plain", exc)
                try:
                    await self._bot.edit_message_text(
                        chat_id=target, message_id=message_id, text=text
                    )
                    return True
                except Exception as exc2:
                    _LOG.warning("batch plain edit failed: %r", exc2)
                    return False
            except _TelegramError.TimedOut:
                _LOG.warning(
                    "batch edit TimedOut (attempt %d/3) -- backing off %.0fs",
                    attempt + 1, delay,
                )
                await asyncio.sleep(delay)
                delay *= 2
                continue
            except Exception as exc:
                _LOG.warning("batch edit failed: %r", exc)
                return False
        _LOG.warning("batch edit gave up after 3 TimedOut attempts (non-fatal)")
        return False

    async def _send_or_edit_batch(
        self,
        job: Job,
        page_title: str,
        cards: List[Tuple["Card", Optional[str], Optional[str]]],
        in_progress_idx: Optional[int] = None,
    ) -> None:
        """Append-only results delivery: sealed messages are never touched.

        Previously every card re-rendered the ENTIRE accumulated text and,
        once it spanned N messages, deleted all N and re-sent them -- O(total)
        Telegram traffic per card. On a 200+ episode series each update grew
        to megabytes of sends that timed out every ~60s for hours (the
        "scraping again and again" stall; the job was actually progressing).

        Now only cards[job.batch_sealed_count:] are rendered. Full leading
        chunks are sent once and sealed; only the small open tail message is
        edited per card. Same content, no caps -- big series just span more
        messages. Never raises: Telegram-side failures are WARNING-level and
        retried on the next card.
        """
        if self._bot is None:
            _LOG.debug("batch skipped: bot not yet initialized")
            return
        # Channel-scrape children post each movie's batch straight to the
        # channel as it completes (per-movie-live); user chat stays clean.
        # Silent without a channel target means nothing to do.
        target = job.post_to_channel or job.chat_id
        if getattr(job, "silent", False) and not job.post_to_channel:
            return
        try:
            job.cards_done = len(cards)
        except Exception:
            pass
        try:
            sealed = max(0, int(getattr(job, "batch_sealed_count", 0) or 0))
        except Exception:
            sealed = 0
        tail = list(cards[sealed:])
        if not tail:
            if job.batch_msg_ids:
                return  # duplicate tick, nothing new -- skip Telegram entirely
            if not cards:
                return
            # Sealed pointer ran past the list (shouldn't happen); re-render all.
            sealed = 0
            tail = list(cards)
        try:
            first_ever = not job.batch_msg_ids
            title_esc = _html_escape(page_title or "(untitled)")
            title_header = f"<b>{title_esc}:</b>"
            blocks = []
            for k, (card, dest, err) in enumerate(tail):
                try:
                    _audios, _details, _show_size = _card_detail_lines(job, card)
                except Exception:
                    _audios, _details, _show_size = "", [], True
                blocks.append(
                    _format_card_block(sealed + k + 1, card, dest, err, _audios, _details, _show_size)
                )
            # Greedy block-granular chunking of the tail. Deterministic from
            # the same sealed start, so the previous open message is always a
            # prefix of chunk 0 -- it can be edited, never re-sent.
            join_sep = f"\n\n{_BATCH_SEP}\n\n"
            chunks: List[List[int]] = []  # tail indexes per chunk
            cur: List[int] = []
            cur_len = (len(title_header) + 2) if first_ever else 0
            for j, blk in enumerate(blocks):
                if not cur:
                    # First block of a chunk: bare, except the very first
                    # chunk which follows the title header ("\n\n").
                    add = len(blk) + (len("\n\n") if (first_ever and not chunks) else 0)
                else:
                    add = len(join_sep) + len(blk)
                if cur and cur_len + add > _BATCH_CHUNK_BUDGET:
                    chunks.append(cur)
                    cur = [j]
                    cur_len = len(blk)
                else:
                    cur.append(j)
                    cur_len += add
            if cur:
                chunks.append(cur)

            def _render_chunk(ci: int) -> str:
                idxs = chunks[ci]
                body = join_sep.join(blocks[j] for j in idxs)
                if ci == 0 and first_ever:
                    return f"{title_header}\n\n{body}"
                return body

            total = int(getattr(job, "cards_total", 0) or 0)
            done_open = sealed + sum(len(c) for c in chunks)
            footer = ""
            if total and done_open < total:
                footer = f"\n\n⏳ <i>{done_open}/{total} delivered so far…</i>"

            if len(chunks) == 1:
                text = _render_chunk(0) + footer
                if len(text) > MSG_LIMIT:
                    # One pathological block (giant URL): transport-split it,
                    # content still fully delivered, cards still sealed.
                    for piece in _split_text(text):
                        mid = await self._tg_send_text(target, piece)
                        if mid is not None:
                            job.batch_msg_ids.append(mid)
                    job.batch_sealed_count = sealed + len(tail)
                elif first_ever:
                    mid = await self._tg_send_text(target, text)
                    if mid is not None:
                        job.batch_msg_ids = [mid]
                else:
                    await self._tg_edit_text(target, job.batch_msg_ids[-1], text)
            else:
                # Chunk 0 overlaps the previous open message: edit it.
                # Middle chunks are full: send + seal. Last chunk is the
                # new open tail (sent fresh; edited from here on).
                head = _render_chunk(0)
                if len(head) > MSG_LIMIT:
                    # Pathological single block (giant URL): transport-split
                    # it so sealing still advances; content fully delivered.
                    pieces = _split_text(head)
                    if not first_ever and pieces:
                        # Old open message holds this chunk's prefix: edit it
                        # with the first piece instead of duplicating.
                        ok = await self._tg_edit_text(target, job.batch_msg_ids[-1], pieces[0])
                        if not ok:
                            return  # retry next card; nothing sealed
                        for piece in pieces[1:]:
                            mid = await self._tg_send_text(target, piece)
                            if mid is not None:
                                job.batch_msg_ids.append(mid)
                    else:
                        for piece in pieces:
                            mid = await self._tg_send_text(target, piece)
                            if mid is not None:
                                if not job.batch_msg_ids:
                                    job.batch_msg_ids = [mid]
                                else:
                                    job.batch_msg_ids.append(mid)
                    sealed_now = sealed + len(chunks[0])
                elif first_ever:
                    mid0 = await self._tg_send_text(target, head)
                    if mid0 is not None:
                        job.batch_msg_ids = [mid0]
                    else:
                        return  # send failed, retry next card; nothing sealed
                    sealed_now = sealed + len(chunks[0])
                else:
                    ok = await self._tg_edit_text(target, job.batch_msg_ids[-1], head)
                    if not ok:
                        return  # edit failed, retry next card; nothing sealed
                    sealed_now = sealed + len(chunks[0])
                for ci in range(1, len(chunks) - 1):
                    mid = await self._tg_send_text(target, _render_chunk(ci))
                    if mid is None:
                        job.batch_sealed_count = sealed_now
                        return  # partial progress sealed; rest next card
                    job.batch_msg_ids.append(mid)
                    sealed_now += len(chunks[ci])
                job.batch_sealed_count = sealed_now
                last_idx = len(chunks) - 1
                last_text = _render_chunk(last_idx) + footer
                if len(last_text) > MSG_LIMIT:
                    for piece in _split_text(last_text):
                        mid = await self._tg_send_text(target, piece)
                        if mid is not None:
                            job.batch_msg_ids.append(mid)
                    job.batch_sealed_count = sealed + len(tail)
                else:
                    mid = await self._tg_send_text(target, last_text)
                    if mid is not None:
                        job.batch_msg_ids.append(mid)
                        # Open tail cards stay unsealed (edited in place next
                        # card); only the middle full chunks sealed above.
            _LOG.info(
                "batch updated: %d cards in %d msg(s): %.60s",
                len(cards), len(job.batch_msg_ids), (page_title or ""),
            )
        except Exception as exc:
            # Non-fatal by design: the card is already resolved and kept in
            # `results`; the next card's tick re-renders the tail. WARNING,
            # not ERROR -- Telegram slowness must not flood the errors log.
            _LOG.warning("batch update failed (non-fatal, retried next card): %r", exc)


    async def _mirror_job_to_channel(self, job: "Job", page_title: str, results, summary: str) -> None:
        """Mirror a finished paste-link job to the preset channel.

        Auto-channel mode (user choice): every movie/series/gate/download
        result goes to the channel set via /scrapeallchannel, in addition
        to the user's DM -- no modes, no opt-in. Channel jobs themselves
        already target the channel, so they are skipped (no double post).
        Channel gets clean links only (no timing/summary footer -- that
        stays in the user's DM). Failures are silent + logged (bot may
        not be admin yet). Never raises.
        """
        try:
            if getattr(job, "silent", False) or getattr(job, "post_to_channel", 0):
                return
            if not results:
                return
            try:
                from xdmovies.scrape_all import load_channel
                ch = load_channel()
            except Exception:
                ch = None
            cid = getattr(ch, "channel_id", 0) if ch else 0
            if not cid or cid == job.chat_id:
                return
            if self._bot is None:
                return
            try:
                mjob = Job(user_id=job.user_id, chat_id=cid, url=job.url)
                mjob.page_audios = getattr(job, "page_audios", "") or ""
                mjob.page_meta = dict(getattr(job, "page_meta", {}) or {})
                mjob.detail_fields = list(getattr(job, "detail_fields", []) or [])
            except Exception:
                return
            try:
                mjob.cards_total = int(getattr(job, "cards_total", 0) or len(results))
            except Exception:
                pass
            try:
                await self._send_or_edit_batch(mjob, page_title, results)
            except Exception:
                _LOG.warning("channel mirror batch failed (non-fatal)", exc_info=True)
                return
            # No summary footer to the channel -- clean links only.
            # (Timing + OK/FAIL stats still go to the user's DM.)
        except Exception:
            _LOG.warning("channel mirror failed (non-fatal)", exc_info=True)


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

    async def _drop_enqueue_note(self, job: Job) -> None:
        """Delete a job's "Queued position #N" / "Starting now" note.

        Called when the job starts running (the progress message takes
        over), so the chat never shows a stale position. Idempotent and
        never raises; also clears the id so terminate-time cleanup skips it.
        """
        try:
            if job.enqueue_msg_id and self._bot is not None:
                try:
                    await _drop_msg(self._bot, job.chat_id, job.enqueue_msg_id)
                except Exception:
                    pass
                job.enqueue_msg_id = None
        except Exception:
            pass

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
                # Clean chat: the "Queued position #N" / "Starting now"
                # note served its purpose once the job actually starts --
                # the progress message takes over from here. Drop it now
                # (not at terminate) so waiting users don't stare at a
                # stale position. Never breaks the job if delete fails.
                await self._drop_enqueue_note(job)
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
            "/search -- find a movie/series, tap to scrape (e.g. /search lucky baskhar)\n"
            "Tip: send a .txt file (one title per line) to search them all at once\n"
            "/status -- bot health + keep-alive\n"
            "/queue -- show who's waiting\n"
            "/scrapeall -- scrape every movie or series (owner + admins)\n"
            "/scrapeallchannel -- scrape to your channel (owner only)\n"
            "/cancel -- stop your running/queued job\n"
            "/qmode -- toggle quality picker for series\n"
            "/dmode -- toggle section (Packs/Episodes) picker for series\n"
            "/settings -- choose which details result cards show\n"
            "/autodetect -- auto-detect new posts, resolve, post to channel (owner only)\n"
            "/restart -- restart the whole bot (owner only)\n"
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
        depth = self._total_depth()
        try:
            _urg_n = self._urgent_depth()
        except Exception:
            _urg_n = 0
        cur = "idle" if self.current is None else f"running (user {self.current.user_id})"
        if _urg_n:
            cur += f" + ⚡{_urg_n} urgent waiting"
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
        # Picker modes for the requesting user + preset channel. A preset
        # channel suppresses the /qmode + /dmode pickers by design, so it
        # is shown here to explain "why didn't it ask me".
        try:
            from xdmovies import qmode_store as _qm, dmode_store as _dm
            _modes_line = (
                f"\n<b>modes (you):</b> qmode="
                f"{'ON' if _qm.is_qmode_on(update.effective_user.id) else 'OFF'} "
                f"dmode={'ON' if _dm.is_dmode_on(update.effective_user.id) else 'OFF'}\n"
            )
        except Exception:
            _modes_line = ""
        try:
            from xdmovies.scrape_all import load_channel as _lc
            _ch = _lc()
            if _ch is not None and getattr(_ch, "channel_id", 0):
                _chan_line = (
                    f"\n<b>channel:</b> preset to "
                    f"{_html_escape(str(getattr(_ch, 'title', '') or _ch.channel_id))} "
                    f"-- results mirror there; pickers still ask\n"
                )
            else:
                _chan_line = "\n<b>channel:</b> none preset\n"
        except Exception:
            _chan_line = ""
        # Auto-detect state (30m catalog poll -> channel)
        try:
            from xdmovies.autodetect_store import status_text as _ad_status
            ad_line = f"\n<b>autodetect:</b> {_html_escape(_ad_status())}\n"
        except Exception:
            ad_line = ""
        text = (
            "🟢 <b>alive</b> (VPS token solve-server + CloakBrowser, no VPN layer)\n"
            f"queue: <b>{depth}</b>\n"
            f"current: {cur}\n"
            f"last solve: {last}\n"
            f"errors: {self.error_count} (see {self.cfg.error_log_path}){keep_txt}{cloak_line}{vpn_line}{desktop_line}{_modes_line}{_chan_line}{ad_line}\n"
            f"<b>cookies:</b>\n{cst}"
        )
        await _send_safe(context.bot, update.effective_chat.id, text, parse_mode=ParseMode.HTML)

    async def _cmd_queue(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
        if update.effective_user is None or update.effective_chat is None:
            return
        if not bot_config.is_allowed(update.effective_user.id, self.cfg):
            return
        if self.current is None and self._total_depth() == 0:
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
            # Card-level progress so a mega-series (100+ links, hours of
            # work) never looks stuck: "📦 cards 137/240 delivered".
            try:
                total = int(getattr(c, "cards_total", 0) or 0)
            except Exception:
                total = 0
            if total:
                try:
                    done = int(getattr(c, "cards_done", 0) or 0)
                except Exception:
                    done = 0
                lines.append(f"📦 cards <b>{done}/{total}</b> delivered")
        # Audit finding M4: take an atomic snapshot under the queue
        # lock instead of draining/reputting. The previous drain loop
        # could race the dispatcher's popleft() and reorder jobs.
        try:
            urgents = await self._snapshot_urgent()
        except Exception:
            urgents = []
        if urgents:
            lines += ["", f"⚡ <b>Urgent up next ({len(urgents)})</b> — runs after current movie 10/10"]
            for idx, j in enumerate(urgents, 1):
                icon, title = _pretty_title(j.url)
                lines.append(f"<b>{idx}.</b> {icon} {title}")
                lines.append(f"     👤 {_user_mention(j.user_id, j.user_name, j.username)} • waiting {_fmt_dur(now - j.enqueued_at)}")
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
        _SCRAPECHAN_PENDING.pop(uid, None)

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
        try:
            urgent_items = await self._snapshot_urgent()
        except Exception:
            urgent_items = []
        kept: List[Job] = []
        kept_urgent: List[Job] = []
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
            # Urgent lane: same per-user cancel so a ⚡ job can be
            # withdrawn while the scrape is still paused on it.
            try:
                self._urgent.clear()
                for j in urgent_items:
                    if j.user_id == uid:
                        j.cancel_event.set()
                        j.status = "cancelled"
                        j.finished_at = time.time()
                        cancelled_queued += 1
                        removed_jobs.append((cancelled_queued, j.url))
                    else:
                        kept_urgent.append(j)
                for j in kept_urgent:
                    self._urgent.append(j)
            except Exception:
                pass
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

    async def _cmd_restart(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
        """Owner-only full restart: stop every job, then re-exec the process.

        Cancels the running job + clears the whole queue (all users),
        destroys solve-server sessions (frees their headed browsers) and
        stops keep-alive; then replaces this process image via os.execv so
        the bot boots fresh. systemd sees the same PID; an xvfb-run parent
        (if any) is untouched. The single-instance lock survives exec.
        """
        if update.effective_user is None or update.effective_chat is None:
            return
        if not self.cfg.is_owner(update.effective_user.id):
            return  # owner only, silent ignore
        chat_id = update.effective_chat.id
        try:
            if self._bot is not None:
                await self._bot.send_message(
                    chat_id=chat_id,
                    text="🔄 <b>Restarting bot</b> -- stopping all jobs...",
                    parse_mode=ParseMode.HTML,
                )
        except Exception:
            pass
        # 1. Stop everything: running job + every queued job, all users.
        try:
            if self.current is not None:
                try:
                    self.current.cancel_event.set()
                except Exception:
                    pass
            try:
                items = await self._snapshot_queue()
            except Exception:
                items = []
            try:
                try:
                    urgent_items = await self._snapshot_urgent()
                except Exception:
                    urgent_items = []
                async with self._queue_cond:
                    for j in items:
                        try:
                            j.cancel_event.set()
                            j.status = "cancelled"
                            j.finished_at = time.time()
                        except Exception:
                            pass
                    for j in urgent_items:
                        try:
                            j.cancel_event.set()
                            j.status = "cancelled"
                            j.finished_at = time.time()
                        except Exception:
                            pass
                    self.queue.clear()
                    try:
                        self._urgent.clear()
                    except Exception:
                        pass
                    try:
                        self._queue_cond.notify_all()
                    except Exception:
                        pass
            except Exception:
                pass
        except Exception:
            pass
        # 2. Tear down solve server (closes session browsers) + keep-alive.
        # Fresh copies boot in _post_init after the re-exec.
        try:
            from xdmovies.solve_server import stop_server
            await stop_server()
        except Exception:
            pass
        try:
            from xdmovies.keep_alive import stop_keep_alive
            await stop_keep_alive()
        except Exception:
            pass
        # 3. Re-exec: every launcher in this repo (systemd unit, run.py,
        # start_bot.cmd, start.sh) ends up in `python -m xdmovies ...`,
        # i.e. sys.argv[0] is the package __main__.py. Replay any other
        # launch verbatim (covers `python run.py ...` style invocations).
        # CRITICAL: __main__.py does os.chdir into xdmovies/ at boot, so
        # our CWD is NOT the repo root here -- and `python -m xdmovies`
        # resolves the package from CWD. Without normalizing, the new
        # process dies with "No module named xdmovies". chdir back plus
        # PYTHONPATH as belt and suspenders.
        try:
            _here = os.path.dirname(os.path.abspath(__file__))
            _repo_root = os.path.dirname(_here)
            try:
                os.chdir(_repo_root)
            except Exception:
                pass
            try:
                _pp = os.environ.get("PYTHONPATH", "")
                if _repo_root not in _pp.split(os.pathsep):
                    os.environ["PYTHONPATH"] = _repo_root + (os.pathsep + _pp if _pp else "")
            except Exception:
                pass
            argv0 = sys.argv[0] if sys.argv else ""
            if os.path.basename(argv0) == "__main__.py":
                new_argv = [sys.executable, "-m", "xdmovies"] + list(sys.argv[1:])
            else:
                new_argv = [sys.executable] + list(sys.argv)
            _LOG.warning("restart command: re-executing %s (cwd=%s)", new_argv, _repo_root)
            _write_restart_notice(chat_id)
            os.execv(sys.executable, new_argv)
        except Exception as exc:
            _LOG.exception("restart re-exec failed")
            _clear_restart_notice()
            try:
                if self._bot is not None:
                    await self._bot.send_message(
                        chat_id=chat_id,
                        text=f"[FAIL] restart failed ({exc.__class__.__name__}); all jobs were stopped, send a link to start fresh.",
                    )
            except Exception:
                pass

    async def _cmd_qmode(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
        if update.effective_user is None or update.effective_chat is None or update.message is None:
            return
        if not bot_config.is_allowed(update.effective_user.id, self.cfg):
            return  # silent (bot convention)
        from xdmovies import qmode_store
        on = qmode_store.is_qmode_on(update.effective_user.id)
        keyboard = InlineKeyboardMarkup([  # type: ignore[misc]
            [InlineKeyboardButton("✅ Yes", callback_data="qmode:set:yes")],  # type: ignore[misc]
            [InlineKeyboardButton("❌ No", callback_data="qmode:set:no")],  # type: ignore[misc]
        ])
        text = (
            "Do you want to enable quality mode?\n"
            f"Currently: <b>{'ON' if on else 'OFF'}</b>\n"
            "ON = series links show a quality picker before resolving. "
            "OFF = resolve everything as today."
        )
        try:
            await update.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        except _TelegramError.BadRequest:
            try:
                await update.message.reply_text(
                    text.replace("<b>", "").replace("</b>", ""),
                    reply_markup=keyboard)
            except Exception:
                pass
        except Exception:
            pass

    async def _on_qmode_choice(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        if update.effective_user is None or update.effective_chat is None:
            return
        if not bot_config.is_allowed(query.from_user.id, self.cfg):
            return  # silent ignore
        data = query.data or ""
        if data not in ("qmode:set:yes", "qmode:set:no"):
            return
        from xdmovies import qmode_store
        on = qmode_store.set_qmode(query.from_user.id, data == "qmode:set:yes")
        text = (
            f"Quality mode: <b>{'ON' if on else 'OFF'}</b>\n"
            + ("Series links will now ask which quality to resolve."
               if on else "Series links resolve everything as before.")
        )
        if query.message is not None:
            try:
                await query.edit_message_text(text, parse_mode=ParseMode.HTML)
            except _TelegramError.BadRequest:
                try:
                    await query.edit_message_text(text.replace("<b>", "").replace("</b>", ""))
                except Exception:
                    pass
            except Exception:
                pass

    async def _on_qpick(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        if update.effective_user is None:
            return
        if not bot_config.is_allowed(query.from_user.id, self.cfg):
            return  # silent ignore
        parts = (query.data or "").split(":")
        if len(parts) < 3 or parts[0] != "qp":
            return
        pid = parts[1]
        st = _QPICK.get(pid)
        if st is None:
            if query.message is not None:
                try:
                    await query.edit_message_text("⌛ picker expired -- resend the link.")
                except Exception:
                    pass
            return
        if query.from_user.id != st.get("uid"):
            return  # only the requester's taps count
        action = parts[2]
        if action == "done" and len(parts) == 3:
            st["verdict"] = "done"
            st["event"].set()
        elif action == "cancel" and len(parts) == 3:
            st["verdict"] = "cancel"
            st["event"].set()
        elif action == "all" and len(parts) == 3:
            try:
                groups = st.get("groups") or []
                sel = st.get("selected")
                if sel is None:
                    sel = st["selected"] = set()
                for gi in range(len(groups)):
                    sel.add(gi)
                try:
                    await query.edit_message_reply_markup(
                        reply_markup=_qpick_keyboard(pid, groups, sel))
                except _TelegramError.BadRequest:
                    pass  # MessageNotModified on rapid double-tap
                except Exception:
                    pass
            except Exception:
                pass
        elif action == "t" and len(parts) == 4 and parts[3].isdigit():
            gi = int(parts[3])
            groups = st.get("groups") or []
            if 0 <= gi < len(groups):
                sel = st.get("selected")
                if sel is None:
                    sel = st["selected"] = set()
                if gi in sel:
                    sel.discard(gi)
                else:
                    sel.add(gi)
                try:
                    await query.edit_message_reply_markup(
                        reply_markup=_qpick_keyboard(pid, groups, sel))
                except _TelegramError.BadRequest:
                    pass  # MessageNotModified on rapid double-tap
                except Exception:
                    pass
        # anything else: malformed payload, ignore

    async def _ask_quality(self, job: "Job", cards, page_title: str = "") -> Optional[list]:
        """Series quality picker. Returns the cards to resolve, or None
        when the user cancelled at the picker.

        Skipped (all cards returned) when: job is silent/channel-routed
        (scrape-all children must never stall), URL is not /series/,
        qmode is OFF for this user, or fewer than 2 quality groups.
        """
        from xdmovies.extractor import group_cards_by_quality
        from xdmovies import qmode_store
        try:
            _LOG.info(
                "qmode picker check: pid=%d user=%d url=%s cards=%d qmode=%s",
                os.getpid(), job.user_id, job.url, len(cards or []),
                "ON" if qmode_store.is_qmode_on(job.user_id) else "OFF",
            )
        except Exception:
            pass
        try:
            if job.silent or job.post_to_channel:
                _LOG.info("qmode picker skipped (silent/channel-routed job) for %s", job.url)
                return list(cards)
            if "/series/" not in (job.url or "").lower():
                _LOG.info("qmode picker skipped (not a series URL) for %s", job.url)
                return list(cards)
            if not qmode_store.is_qmode_on(job.user_id):
                _LOG.info("qmode picker skipped (qmode OFF for user %d) for %s", job.user_id, job.url)
                return list(cards)
        except Exception:
            return list(cards)
        # NOTE: a preset scrape channel does NOT skip this picker. It only
        # controls where results land (mirrored after resolve). Silent /
        # channel-routed jobs (scrape-all children) already returned above
        # -- an explicit per-user /qmode ON always gets asked.
        try:
            groups = group_cards_by_quality(cards)
        except Exception:
            _LOG.exception("quality grouping failed (non-fatal) -- resolving all")
            return list(cards)
        if len(groups) <= 1:
            _LOG.info("qmode picker skipped (%d quality group) for %s", len(groups), job.url)
            return list(cards)
        if len(groups) > QPICK_MAX_GROUPS:
            await _send_safe(
                self._bot, job.chat_id,
                f"Too many quality groups ({len(groups)}) to pick from -- resolving all {len(cards)} links.",
                parse_mode=ParseMode.HTML,
            )
            return list(cards)
        # Exact-error trap (same contract as the dmode picker): unexpected
        # crashes surface the exception (log + user message) instead of
        # silently resolving everything.
        try:
            _LOG.info(
                "qmode picker showing: pid=%d groups=%d cards=%d for %s",
                os.getpid(), len(groups), len(cards), job.url,
            )
            pid = uuid.uuid4().hex[:8]
            while pid in _QPICK:  # 32-bit id; re-roll on the freak collision
                pid = uuid.uuid4().hex[:8]
            ev = asyncio.Event()
            _QPICK[pid] = {
                "uid": job.user_id, "chat_id": job.chat_id, "msg_id": None,
                "groups": groups, "selected": set(), "event": ev, "verdict": None,
            }
            try:
                msg_id = await _send_markup_safe(
                    self._bot, job.chat_id,
                    _qpick_text(_best_picker_title(page_title, job.url), groups),
                    _qpick_keyboard(pid, groups, set()),
                )
            except Exception as send_exc:
                msg_id = None
                _QPICK.pop(pid, None)
                raise RuntimeError(f"picker send raised: {send_exc!r}") from send_exc
            if msg_id is None:
                _QPICK.pop(pid, None)
                raise RuntimeError("picker send returned None (see 'picker send failed' traceback just above in log)")
            _QPICK[pid]["msg_id"] = msg_id
            verdict = None
            selected: set = set()
            try:
                deadline = time.time() + QPICK_TIMEOUT
                while time.time() < deadline:
                    if job.cancel_event.is_set():
                        _QPICK.get(pid, {})["verdict"] = "cancel"
                        break
                    try:
                        await asyncio.wait_for(ev.wait(), 1.0)
                        break
                    except asyncio.TimeoutError:
                        continue
                st = _QPICK.pop(pid, None) or {}
                verdict = st.get("verdict")
                selected = set(st.get("selected") or set())
            finally:
                # Cleanup runs even on task-cancel/shutdown so the picker
                # entry never leaks and the message is never orphaned.
                _QPICK.pop(pid, None)
                await _drop_msg(self._bot, job.chat_id, msg_id)
            if verdict == "cancel" or job.cancel_event.is_set():
                await _send_safe(self._bot, job.chat_id, "🛑 quality pick cancelled -- job stopped.")
                return None
            if verdict == "done" and selected:
                picked = [c for gi, g in enumerate(groups) if gi in selected for c in g.cards]
                if not picked:
                    return list(cards)
                await _send_safe(
                    self._bot, job.chat_id,
                    f"✅ picked <b>{len(picked)}/{len(cards)}</b> links -- resolving those.",
                    parse_mode=ParseMode.HTML,
                )
                return picked
            if verdict != "done":
                await _send_safe(
                    self._bot, job.chat_id,
                    "⏳ no choice in 2m -- job cancelled.",
                    parse_mode=ParseMode.HTML,
                )
                return None
            return list(cards)
        except Exception as exc:
            _LOG.exception(
                "qmode picker CRASHED (pid=%d user=%d url=%s cards=%d): %r",
                os.getpid(), job.user_id, job.url, len(cards or []), exc,
            )
            try:
                await _send_safe(
                    self._bot, job.chat_id,
                    f"⚠️ quality picker crashed: "
                    f"<code>{_html_escape(exc.__class__.__name__ + ': ' + str(exc)[:200])}</code> "
                    f"-- resolving all <b>{len(cards)}</b> links. "
                    f"Forward this + /status to the owner.",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            try:
                _QPICK.pop(pid, None)  # type: ignore[possibly-unbound]
            except Exception:
                pass
            return list(cards)

    async def _cmd_dmode(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
        if update.effective_user is None or update.effective_chat is None or update.message is None:
            return
        if not bot_config.is_allowed(update.effective_user.id, self.cfg):
            return  # silent (bot convention)
        from xdmovies import dmode_store
        on = dmode_store.is_dmode_on(update.effective_user.id)
        keyboard = InlineKeyboardMarkup([  # type: ignore[misc]
            [InlineKeyboardButton("✅ Yes", callback_data="dmode:set:yes")],  # type: ignore[misc]
            [InlineKeyboardButton("❌ No", callback_data="dmode:set:no")],  # type: ignore[misc]
        ])
        text = (
            "Do you want to enable download mode?\n"
            f"Currently: <b>{'ON' if on else 'OFF'}</b>\n"
            "ON = series links show a section picker (Season N Packs vs "
            "Season N Episodes) before resolving. "
            "OFF = resolve everything as today."
        )
        try:
            await update.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        except _TelegramError.BadRequest:
            try:
                await update.message.reply_text(
                    text.replace("<b>", "").replace("</b>", ""),
                    reply_markup=keyboard)
            except Exception:
                pass
        except Exception:
            pass

    async def _on_dmode_choice(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        if update.effective_user is None or update.effective_chat is None:
            return
        if not bot_config.is_allowed(query.from_user.id, self.cfg):
            return  # silent ignore
        data = query.data or ""
        if data not in ("dmode:set:yes", "dmode:set:no"):
            return
        from xdmovies import dmode_store
        on = dmode_store.set_dmode(query.from_user.id, data == "dmode:set:yes")
        text = (
            f"Download mode: <b>{'ON' if on else 'OFF'}</b>\n"
            + ("Series links will now ask which sections (Packs/Episodes) to resolve."
               if on else "Series links resolve everything as before.")
        )
        if query.message is not None:
            try:
                await query.edit_message_text(text, parse_mode=ParseMode.HTML)
            except _TelegramError.BadRequest:
                try:
                    await query.edit_message_text(text.replace("<b>", "").replace("</b>", ""))
                except Exception:
                    pass
            except Exception:
                pass

    async def _on_dpick(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        if update.effective_user is None:
            return
        if not bot_config.is_allowed(query.from_user.id, self.cfg):
            return  # silent ignore
        parts = (query.data or "").split(":")
        if len(parts) < 3 or parts[0] != "dp":
            return
        pid = parts[1]
        st = _DPICK.get(pid)
        if st is None:
            if query.message is not None:
                try:
                    await query.edit_message_text("⌛ picker expired -- resend the link.")
                except Exception:
                    pass
            return
        if query.from_user.id != st.get("uid"):
            return  # only the requester's taps count
        action = parts[2]
        if action == "done" and len(parts) == 3:
            st["verdict"] = "done"
            st["event"].set()
        elif action == "cancel" and len(parts) == 3:
            st["verdict"] = "cancel"
            st["event"].set()
        elif action == "all" and len(parts) == 3:
            try:
                groups = st.get("groups") or []
                sel = st.get("selected")
                if sel is None:
                    sel = st["selected"] = set()
                for gi in range(len(groups)):
                    sel.add(gi)
                try:
                    await query.edit_message_reply_markup(
                        reply_markup=_dpick_keyboard(pid, groups, sel))
                except _TelegramError.BadRequest:
                    pass  # MessageNotModified on rapid double-tap
                except Exception:
                    pass
            except Exception:
                pass
        elif action == "t" and len(parts) == 4 and parts[3].isdigit():
            gi = int(parts[3])
            groups = st.get("groups") or []
            if 0 <= gi < len(groups):
                sel = st.get("selected")
                if sel is None:
                    sel = st["selected"] = set()
                if gi in sel:
                    sel.discard(gi)
                else:
                    sel.add(gi)
                try:
                    await query.edit_message_reply_markup(
                        reply_markup=_dpick_keyboard(pid, groups, sel))
                except _TelegramError.BadRequest:
                    pass  # MessageNotModified on rapid double-tap
                except Exception:
                    pass
        # anything else: malformed payload, ignore

    async def _ask_download(self, job: "Job", cards, page_title: str = "") -> Optional[list]:
        """Series section picker (/dmode). Returns the cards to resolve, or
        None when the user cancelled at the picker.

        Skipped (all cards returned) when: job is silent/channel-routed
        (scrape-all children must never stall), URL is not /series/,
        dmode is OFF for this user, or fewer than 2 section groups.
        Runs BEFORE _ask_quality when both modes are ON.
        """
        from xdmovies.extractor import group_cards_by_section
        from xdmovies import dmode_store
        try:
            _LOG.info(
                "dmode picker check: pid=%d user=%d url=%s cards=%d dmode=%s",
                os.getpid(), job.user_id, job.url, len(cards or []),
                "ON" if dmode_store.is_dmode_on(job.user_id) else "OFF",
            )
        except Exception:
            pass
        try:
            if job.silent or job.post_to_channel:
                _LOG.info("dmode picker skipped (silent/channel-routed job) for %s", job.url)
                return list(cards)
            if "/series/" not in (job.url or "").lower():
                _LOG.info("dmode picker skipped (not a series URL) for %s", job.url)
                return list(cards)
            if not dmode_store.is_dmode_on(job.user_id):
                _LOG.info("dmode picker skipped (dmode OFF for user %d) for %s", job.user_id, job.url)
                return list(cards)
        except Exception:
            return list(cards)
        # NOTE: a preset scrape channel does NOT skip this picker. It only
        # controls where results land (mirrored after resolve). Silent /
        # channel-routed jobs (scrape-all children) already returned above
        # -- an explicit per-user /dmode ON always gets asked.
        try:
            groups = group_cards_by_section(cards)
        except Exception:
            _LOG.exception("section grouping failed (non-fatal) -- resolving all")
            return list(cards)
        if len(groups) <= 1:
            _LOG.info("dmode picker skipped (%d section group) for %s", len(groups), job.url)
            return list(cards)
        if len(groups) > DPICK_MAX_GROUPS:
            await _send_safe(
                self._bot, job.chat_id,
                f"Too many section groups ({len(groups)}) to pick from -- resolving all {len(cards)} links.",
                parse_mode=ParseMode.HTML,
            )
            return list(cards)
        # Exact-error trap: ANY unexpected crash below must surface the
        # exception (log + user message) instead of silently resolving
        # everything. Normal paths (cancel / timeout / done / empty=all)
        # return from inside the try untouched.
        try:
            _LOG.info(
                "dmode picker showing: pid=%d groups=%d cards=%d for %s",
                os.getpid(), len(groups), len(cards), job.url,
            )
            pid = uuid.uuid4().hex[:8]
            while pid in _DPICK:  # 32-bit id; re-roll on the freak collision
                pid = uuid.uuid4().hex[:8]
            ev = asyncio.Event()
            _DPICK[pid] = {
                "uid": job.user_id, "chat_id": job.chat_id, "msg_id": None,
                "groups": groups, "selected": set(), "event": ev, "verdict": None,
            }
            try:
                msg_id = await _send_markup_safe(
                    self._bot, job.chat_id,
                    _dpick_text(_best_picker_title(page_title, job.url), groups),
                    _dpick_keyboard(pid, groups, set()),
                )
            except Exception as send_exc:
                msg_id = None
                _DPICK.pop(pid, None)
                raise RuntimeError(f"picker send raised: {send_exc!r}") from send_exc
            if msg_id is None:
                _DPICK.pop(pid, None)
                raise RuntimeError("picker send returned None (see 'picker send failed' traceback just above in log)")
            _DPICK[pid]["msg_id"] = msg_id
            verdict = None
            selected: set = set()
            try:
                deadline = time.time() + DPICK_TIMEOUT
                while time.time() < deadline:
                    if job.cancel_event.is_set():
                        _DPICK.get(pid, {})["verdict"] = "cancel"
                        break
                    try:
                        await asyncio.wait_for(ev.wait(), 1.0)
                        break
                    except asyncio.TimeoutError:
                        continue
                st = _DPICK.pop(pid, None) or {}
                verdict = st.get("verdict")
                selected = set(st.get("selected") or set())
            finally:
                # Cleanup runs even on task-cancel/shutdown so the picker
                # entry never leaks and the message is never orphaned.
                _DPICK.pop(pid, None)
                await _drop_msg(self._bot, job.chat_id, msg_id)
            if verdict == "cancel" or job.cancel_event.is_set():
                await _send_safe(self._bot, job.chat_id, "🛑 section pick cancelled -- job stopped.")
                return None
            if verdict == "done" and selected:
                picked = [c for gi, g in enumerate(groups) if gi in selected for c in g.cards]
                # De-dupe: untagged cards appear under both sections.
                seen = set()
                uniq = []
                for c in picked:
                    try:
                        key = c.url
                    except Exception:
                        key = id(c)
                    if key in seen:
                        continue
                    seen.add(key)
                    uniq.append(c)
                picked = uniq
                if not picked:
                    return list(cards)
                await _send_safe(
                    self._bot, job.chat_id,
                    f"✅ picked <b>{len(picked)}/{len(cards)}</b> links -- resolving those.",
                    parse_mode=ParseMode.HTML,
                )
                return picked
            if verdict != "done":
                await _send_safe(
                    self._bot, job.chat_id,
                    "⏳ no choice in 2m -- job cancelled.",
                    parse_mode=ParseMode.HTML,
                )
                return None
            return list(cards)
        except Exception as exc:
            _LOG.exception(
                "dmode picker CRASHED (pid=%d user=%d url=%s cards=%d): %r",
                os.getpid(), job.user_id, job.url, len(cards or []), exc,
            )
            try:
                await _send_safe(
                    self._bot, job.chat_id,
                    f"⚠️ section picker crashed: "
                    f"<code>{_html_escape(exc.__class__.__name__ + ': ' + str(exc)[:200])}</code> "
                    f"-- resolving all <b>{len(cards)}</b> links. "
                    f"Forward this + /status to the owner.",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            try:
                _DPICK.pop(pid, None)  # type: ignore[possibly-unbound]
            except Exception:
                pass
            return list(cards)

    async def _cmd_settings(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
        if update.effective_user is None or update.effective_chat is None or update.message is None:
            return
        if not bot_config.is_allowed(update.effective_user.id, self.cfg):
            return  # silent (bot convention)
        from xdmovies import settings_store
        prefs = settings_store.get_settings(update.effective_user.id)
        try:
            await update.message.reply_text(
                _settings_text(),
                reply_markup=_settings_keyboard(prefs),
                parse_mode=ParseMode.HTML)
        except _TelegramError.BadRequest:
            try:
                await update.message.reply_text(
                    _settings_text(),
                    reply_markup=_settings_keyboard(prefs))
            except Exception:
                pass
        except Exception:
            pass

    async def _on_settings(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        if update.effective_user is None:
            return
        if not bot_config.is_allowed(query.from_user.id, self.cfg):
            return  # silent ignore
        from xdmovies import settings_store
        data = query.data or ""
        if data == "settings:close":
            if query.message is not None:
                try:
                    await query.message.delete()
                except Exception:
                    pass
            return
        if data == "settings:all":
            prefs = settings_store.get_settings(query.from_user.id)
            for key, _label, _emoji in settings_store.ALL_FIELDS:
                prefs = settings_store.set_field(query.from_user.id, key, True)
        elif data.startswith("settings:toggle:"):
            key = data.split(":", 2)[-1]
            prefs = settings_store.toggle_field(query.from_user.id, key)
        else:
            return
        if query.message is not None:
            try:
                await query.edit_message_reply_markup(
                    reply_markup=_settings_keyboard(prefs))
            except _TelegramError.BadRequest:
                pass  # MessageNotModified on rapid double-tap
            except Exception:
                pass

    # ---- /autodetect (owner-only, 30m catalog poll -> channel) -----------
    _AD_FEED_LABELS = {"movies": "🎬 Movies", "series": "🎞️ Series", "all": "🌐 All (homepage)"}

    def _autodetect_feed_keyboard(self):
        from xdmovies import autodetect_store as _ad
        cur = _ad.get_feed()
        rows = []
        for key in ("movies", "series", "all"):
            label = self._AD_FEED_LABELS[key]
            if key == cur:
                label = f"✅ {label}"
            rows.append([InlineKeyboardButton(label, callback_data=f"autodetect:feed:{key}")])  # type: ignore[misc]
        return InlineKeyboardMarkup(rows)  # type: ignore[misc]

    def _autodetect_feed_text(self) -> str:
        from xdmovies import autodetect_store as _ad
        cur = _ad.get_feed()
        state = "ON" if _ad.is_on() else "OFF"
        return (
            "Auto-detect: poll xdmovies for new posts every 30m, "
            "resolve them, and post to your channel.\n\n"
            f"Current feed: <b>{self._AD_FEED_LABELS[cur]}</b>\n"
            f"State: <b>{state}</b>\n\n"
            "Pick the feed to watch:"
        )

    def _autodetect_toggle_keyboard(self):
        from xdmovies import autodetect_store as _ad
        cur = _ad.get_feed()
        on = _ad.is_on()
        rows = []
        if not on:
            rows.append([InlineKeyboardButton("🟢 Turn ON", callback_data="autodetect:set:yes")])  # type: ignore[misc]
        else:
            rows.append([InlineKeyboardButton("🔴 Turn OFF", callback_data="autodetect:set:no")])  # type: ignore[misc]
        if cur == "series":
            rows.append([InlineKeyboardButton("🎞️ Content filter", callback_data="autodetect:content:show")])  # type: ignore[misc]
        rows.append([InlineKeyboardButton("⬅️ Change feed", callback_data="autodetect:back")])  # type: ignore[misc]
        return InlineKeyboardMarkup(rows)  # type: ignore[misc]

    def _autodetect_toggle_text(self) -> str:
        from xdmovies import autodetect_store as _ad
        from xdmovies.scrape_all import load_channel
        cur = _ad.get_feed()
        on = _ad.is_on()
        ch = load_channel()
        cid = getattr(ch, "channel_id", 0) if ch else 0
        ch_txt = (
            f"<b>{_html_escape(str(getattr(ch, 'title', '') or cid))}</b>"
            if cid else "<b>none (run /scrapeallchannel first)</b>"
        )
        interval = "30m"
        content_line = ""
        if cur == "series":
            cf = _ad.get_card_filter()
            content_line = f"\nContent: <b>{_html_escape(_CONTENT_LABEL.get(cf, cf))}</b>"
        return (
            f"Auto detect: <b>{'ON' if on else 'OFF'}</b>\n"
            f"Feed: <b>{self._AD_FEED_LABELS[cur]}</b>\n"
            f"Interval: <b>{interval}</b>{content_line}\n"
            f"Channel: {ch_txt}\n"
            + (
                "New posts will be resolved and posted to the channel."
                if on
                else "Tap Turn ON to start watching."
            )
        )

    def _autodetect_content_keyboard(self):
        from xdmovies import autodetect_store as _ad
        cur = _ad.get_card_filter()
        # Same three choices as the search accept-all content picker.
        rows = []
        for key, cb, emoji in (
            ("all", "all", "✅"),
            ("episodes", "ep", "🎞️"),
            ("packs", "pack", "🗜️"),
        ):
            label = f"{emoji} {_CONTENT_LABEL[key]}"
            if key == cur:
                label = f"✅ {label}" if key != "all" else label
            rows.append([InlineKeyboardButton(label, callback_data=f"autodetect:content:{cb}")])  # type: ignore[misc]
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="autodetect:back")])  # type: ignore[misc]
        return InlineKeyboardMarkup(rows)  # type: ignore[misc]

    def _autodetect_content_text(self) -> str:
        from xdmovies import autodetect_store as _ad
        cf = _ad.get_card_filter()
        return (
            "Series detected -- what should auto-detect resolve?\n\n"
            f"Current: <b>{_html_escape(_CONTENT_LABEL.get(cf, cf))}</b>\n\n"
            "• All = episodes + packs/zips\n"
            "• Episodes only = single-episode cards\n"
            "• Packs/Zips only = season/full .zip packs"
        )

    async def _cmd_autodetect(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
        if update.effective_user is None or update.effective_chat is None or update.message is None:
            return
        if not self.cfg.is_owner(update.effective_user.id):
            return  # owner-only, silent (convention from /restart)
        text = self._autodetect_feed_text()
        kb = self._autodetect_feed_keyboard()
        try:
            await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except _TelegramError.BadRequest:
            try:
                await update.message.reply_text(
                    text.replace("<b>", "").replace("</b>", ""), reply_markup=kb)
            except Exception:
                pass
        except Exception:
            pass

    async def _on_autodetect_feed(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        if update.effective_user is None:
            return
        if not self.cfg.is_owner(query.from_user.id):
            return  # silent
        parts = (query.data or "").split(":")
        if len(parts) != 3 or parts[0] != "autodetect" or parts[1] != "feed":
            return
        feed = parts[2]
        if feed not in ("movies", "series", "all"):
            return
        from xdmovies import autodetect_store as _ad
        _ad.set_feed(feed)
        if query.message is not None:
            try:
                await query.edit_message_text(
                    self._autodetect_toggle_text(),
                    reply_markup=self._autodetect_toggle_keyboard(),
                    parse_mode=ParseMode.HTML)
            except _TelegramError.BadRequest:
                try:
                    await query.edit_message_text(
                        self._autodetect_toggle_text().replace("<b>", "").replace("</b>", ""),
                        reply_markup=self._autodetect_toggle_keyboard())
                except Exception:
                    pass
            except Exception:
                pass

    async def _on_autodetect_set(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        if update.effective_user is None:
            return
        if not self.cfg.is_owner(query.from_user.id):
            return
        data = query.data or ""
        if data not in ("autodetect:set:yes", "autodetect:set:no"):
            return
        from xdmovies import autodetect_store as _ad
        want_on = data.endswith(":yes")
        # Turning ON with no channel is useless — warn but still store choice.
        _ad.set_on(want_on)
        if query.message is not None:
            # Series ON -> ask content filter first (All / Episodes / Packs),
            # same choice as the search accept-all flow. The content callback
            # lands back on the toggle screen.
            if want_on and _ad.get_feed() == "series":
                try:
                    await query.edit_message_text(
                        self._autodetect_content_text(),
                        reply_markup=self._autodetect_content_keyboard(),
                        parse_mode=ParseMode.HTML)
                except _TelegramError.BadRequest:
                    try:
                        await query.edit_message_text(
                            self._autodetect_content_text().replace("<b>", "").replace("</b>", ""),
                            reply_markup=self._autodetect_content_keyboard())
                    except Exception:
                        pass
                except Exception:
                    pass
                return
            try:
                await query.edit_message_text(
                    self._autodetect_toggle_text(),
                    reply_markup=self._autodetect_toggle_keyboard(),
                    parse_mode=ParseMode.HTML)
            except _TelegramError.BadRequest:
                try:
                    await query.edit_message_text(
                        self._autodetect_toggle_text().replace("<b>", "").replace("</b>", ""),
                        reply_markup=self._autodetect_toggle_keyboard())
                except Exception:
                    pass
            except Exception:
                pass

    async def _on_autodetect_content(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        if update.effective_user is None or not self.cfg.is_owner(query.from_user.id):
            return  # silent
        data = query.data or ""
        if data == "autodetect:content:show":
            # Revisit the content picker from the toggle screen.
            if query.message is not None:
                try:
                    await query.edit_message_text(
                        self._autodetect_content_text(),
                        reply_markup=self._autodetect_content_keyboard(),
                        parse_mode=ParseMode.HTML)
                except _TelegramError.BadRequest:
                    try:
                        await query.edit_message_text(
                            self._autodetect_content_text().replace("<b>", "").replace("</b>", ""),
                            reply_markup=self._autodetect_content_keyboard())
                    except Exception:
                        pass
                except Exception:
                    pass
            return
        parts = data.split(":")
        if len(parts) != 3 or parts[1] != "content" or parts[2] not in ("all", "ep", "pack"):
            return
        from xdmovies import autodetect_store as _ad
        _ad.set_card_filter(_CONTENT_CALLBACK[parts[2]])
        if query.message is not None:
            try:
                await query.edit_message_text(
                    self._autodetect_toggle_text(),
                    reply_markup=self._autodetect_toggle_keyboard(),
                    parse_mode=ParseMode.HTML)
            except _TelegramError.BadRequest:
                try:
                    await query.edit_message_text(
                        self._autodetect_toggle_text().replace("<b>", "").replace("</b>", ""),
                        reply_markup=self._autodetect_toggle_keyboard())
                except Exception:
                    pass
            except Exception:
                pass

    async def _on_autodetect_back(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        if update.effective_user is None or not self.cfg.is_owner(query.from_user.id):
            return
        if (query.data or "") != "autodetect:back":
            return
        if query.message is not None:
            try:
                await query.edit_message_text(
                    self._autodetect_feed_text(),
                    reply_markup=self._autodetect_feed_keyboard(),
                    parse_mode=ParseMode.HTML)
            except _TelegramError.BadRequest:
                try:
                    await query.edit_message_text(
                        self._autodetect_feed_text().replace("<b>", "").replace("</b>", ""),
                        reply_markup=self._autodetect_feed_keyboard())
                except Exception:
                    pass
            except Exception:
                pass

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
            _failed = f", <b>{len(state.failed)}</b> failed" if state.failed else ""
            await query.edit_message_text(
                f"{label} has unfinished scrape history.\n"
                f"Progress: page <b>{state.next_page}</b>, "
                f"<b>{len(state.completed_urls)}</b> done{_failed}.\n"
                f"Resume continues there; Start wipes it. What do you want to do?",
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

    async def _try_channel_setup(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> bool:
        """Consume a forwarded channel message as /scrapeallchannel setup.

        Returns True when the message was consumed (caller must return).
        Only active while the user has a pending setup flag (10 min TTL).
        Verifies the bot is admin in the channel before saving.
        """
        uid = update.effective_user.id
        ts = _SCRAPECHAN_PENDING.get(uid)
        if ts is None:
            return False
        if time.time() - ts > _SCRAPECHAN_PENDING_TTL:
            _SCRAPECHAN_PENDING.pop(uid, None)
            return False
        message = update.message
        cid, title = _extract_forwarded_channel(message)
        if cid is None:
            try:
                txt = ((getattr(message, "text", None) or getattr(message, "caption", None) or "").strip())
                import re as _re
                if _re.match(r"^-100\d{5,}$", txt):
                    cid, title = int(txt), ""
            except Exception:
                pass
        if cid is None:
            await _send_safe(
                context.bot, update.effective_chat.id,
                "⚠️ That doesn't look like a channel forward. Forward any message "
                "<b>from the channel itself</b> (not from a user), or reply with the "
                "channel id (<code>-100…</code>).",
                parse_mode=ParseMode.HTML,
            )
            return True
        try:
            me = await context.bot.get_me()
            member = await context.bot.get_chat_member(chat_id=cid, user_id=me.id)
            if getattr(member, "status", "") not in ("administrator", "creator"):
                raise PermissionError(getattr(member, "status", "unknown"))
        except PermissionError as exc:
            await _send_safe(
                context.bot, update.effective_chat.id,
                f"⚠️ I'm not an admin in <b>{_html_escape(title or str(cid))}</b> ({exc}). "
                f"Make me admin (Post Messages), then forward again.",
                parse_mode=ParseMode.HTML,
            )
            return True
        except Exception as exc:
            _LOG.warning("channel admin check failed: %r", exc)
            await _send_safe(
                context.bot, update.effective_chat.id,
                "⚠️ I can't see that channel -- add me to it first, make me admin, then forward again.",
                parse_mode=ParseMode.HTML,
            )
            return True
        try:
            from xdmovies.scrape_all import ScrapeChannel, save_channel
            save_channel(ScrapeChannel(channel_id=cid, title=title, set_by=uid))
        except Exception as exc:
            _LOG.exception("channel save failed")
            await _send_safe(
                context.bot, update.effective_chat.id,
                f"[FAIL] could not save channel ({exc.__class__.__name__}). Try again.",
                parse_mode=ParseMode.HTML,
            )
            return True
        _SCRAPECHAN_PENDING.pop(uid, None)
        shown = _html_escape(title) if title else f"<code>{cid}</code>"
        await _send_safe(
            context.bot, update.effective_chat.id,
            f"✅ Channel saved: <b>{shown}</b>\nRun /scrapeallchannel again to choose what to scrape.",
            parse_mode=ParseMode.HTML,
        )
        return True

    async def _cmd_scrapeallchannel(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
        if update.effective_user is None or update.effective_chat is None or update.message is None:
            return
        if not self.cfg.is_owner(update.effective_user.id):
            return  # owner only, silent ignore
        try:
            from xdmovies.scrape_all import load_channel
            ch = load_channel()
        except Exception:
            ch = None
        if ch is None:
            _SCRAPECHAN_PENDING[update.effective_user.id] = time.time()
            await update.message.reply_text(
                "📢 No channel set yet.\n\nForward any message from your channel "
                "here within 10 minutes and I'll save it as the scrape target."
            )
            return
        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        except Exception:
            InlineKeyboardButton = InlineKeyboardMarkup = None  # type: ignore
        if InlineKeyboardMarkup is None:
            return
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📚 All", callback_data="scrapechan:select:all")],
            [
                InlineKeyboardButton("🎬 Movies", callback_data="scrapechan:select:movies"),
                InlineKeyboardButton("🎞️ Series", callback_data="scrapechan:select:series"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="scrapechan:cancel:-")],
        ])
        shown = ch.title or str(ch.channel_id)
        await update.message.reply_text(
            f"What do you want to scrape into <b>{_html_escape(shown)}</b>?",
            reply_markup=keyboard, parse_mode=ParseMode.HTML,
        )

    async def _on_scrapechan_choice(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        if not self.cfg.is_owner(query.from_user.id):
            return  # owner only, silent ignore
        data = query.data or ""
        parts = data.split(":")
        if len(parts) != 3 or parts[0] != "scrapechan":
            return
        action, kind = parts[1], parts[2]
        if action == "cancel":
            if query.message is None:
                return
            await query.edit_message_text("❌ Channel scrape choice cancelled -- nothing started.")
            return
        if action not in {"select", "resume", "start"}:
            return
        if kind not in {"all", "movies", "series"}:
            return
        if query.message is None:
            return
        try:
            from xdmovies.scrape_all import load_channel
            ch = load_channel()
        except Exception:
            ch = None
        if ch is None:
            await query.edit_message_text("⚠️ No channel set. Run /scrapeallchannel and forward a channel message first.")
            return
        from xdmovies.scrape_all import has_resumable_history, load_checkpoint
        state = load_checkpoint(kind)
        if action == "select" and has_resumable_history(state):
            label = {"all": "All", "movies": "Movies", "series": "Series"}[kind]
            try:
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            except Exception:
                InlineKeyboardButton = InlineKeyboardMarkup = None  # type: ignore
            if InlineKeyboardMarkup is None:
                return
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("▶ Resume", callback_data=f"scrapechan:resume:{kind}"),
                InlineKeyboardButton("↻ Start from beginning", callback_data=f"scrapechan:start:{kind}"),
            ]])
            _failed_c = f", <b>{len(state.failed)}</b> failed" if state.failed else ""
            await query.edit_message_text(
                f"{label} has unfinished scrape history.\n"
                f"Progress: page <b>{state.next_page}</b>, "
                f"<b>{len(state.completed_urls)}</b> done{_failed_c}.\n"
                f"Resume continues there; Start wipes it. What do you want to do?",
                reply_markup=keyboard,
            )
            return
        if action == "select":
            action = "start"
        if self.current is not None and self.current.job_type == "scrape_all":
            await query.edit_message_text("⏳ A scrape-all job is already running.")
            return
        items: List[Job] = []
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
            post_to_channel=ch.channel_id,
        )
        position = await self._enqueue(job)
        action_label = "Starting from beginning" if action == "start" else "Resuming"
        shown = ch.title or str(ch.channel_id)
        await query.edit_message_text(
            f"[OK] {action_label} {kind} scrape from page {state.next_page} → "
            f"<b>{_html_escape(shown)}</b>. Queue position: {position}."
        )

    async def _confirm_enqueued(self, context: "ContextTypes.DEFAULT_TYPE",
                              chat_id: int, url: str, pos: int) -> Optional[int]:
        """Single-link enqueue note. Returns message_id for clutter cleanup."""
        icon, title = _pretty_title(url)
        if self.current is None and pos == 1:
            return await _send_safe(
                context.bot, chat_id,
                f"🔄 <b>Starting now</b>\n{icon} {title}",
                parse_mode=ParseMode.HTML,
            )
        behind = (pos - 1) + (1 if self.current else 0)
        eta = pos * 35  # rough guess
        return await _send_safe(
            context.bot, chat_id,
            f"📋 <b>Queued</b> -- position #{behind + 1}\n{icon} {title}\n⏳ ETA ~{_fmt_dur(eta)}",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_search(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
        if update.effective_user is None or update.effective_chat is None or update.message is None:
            return
        uid = update.effective_user.id
        if not bot_config.is_allowed(uid, self.cfg):
            return  # silent ignore
        query = " ".join(context.args or []).strip()
        if not query:
            await _send_safe(
                context.bot, update.effective_chat.id,
                "Usage: <code>/search lucky baskhar</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        try:
            from xdmovies.search import SearchUnavailable, search_titles
            results = await search_titles(query, limit=_SEARCH_FETCH_LIMIT)
        except Exception as exc:
            _LOG.warning("search failed for %r: %r", query, exc)
            await _send_safe(
                context.bot, update.effective_chat.id,
                "[FAIL] search is unavailable right now -- try again later.",
                parse_mode=ParseMode.HTML,
            )
            return
        if not results:
            await _send_safe(
                context.bot, update.effective_chat.id,
                f"Not found: <code>{_html_escape(query)}</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        _SEARCH_CACHE[uid] = (time.time(), query, results)
        page, total_pages, keyboard = _search_page_keyboard(results, 1)
        await update.message.reply_text(
            _search_page_text(query, results, page, total_pages),
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )

    async def _acceptall_enqueue(self, uid: int, chat_id: int, results,
                                 user_name: str, username: str,
                                 to_channel: int = 0,
                                 card_filter: str = "all") -> int:
        """Enqueue every result (deduped, unlimited) + send the summary.

        to_channel=0 scrapes to the user's DM; otherwise each job carries
        post_to_channel + silent (same shape as /scrapeallchannel
        children) so batches land in the channel. card_filter ("all" |
        "episodes" | "packs") rides on each Job so _run_for_user resolves
        only episode/pack cards. Returns jobs enqueued.
        Never raises (summary send is best-effort).
        """
        if card_filter not in _CONTENT_FILTERS:
            card_filter = "all"
        seen_urls: set = set()
        jobs = []
        for res in results:
            if res.url in seen_urls:
                continue
            seen_urls.add(res.url)
            job = Job(user_id=uid, chat_id=chat_id, url=res.url,
                      user_name=user_name, username=username,
                      post_to_channel=to_channel, silent=bool(to_channel),
                      card_filter=card_filter)
            try:
                await self._enqueue(job)
            except Exception:
                continue
            jobs.append(job)
        skipped = len(results) - len(jobs)
        lines = [f"📥 <b>Queued {len(jobs)} links</b> -- resolving one by one."]
        if card_filter != "all":
            lines[0] += f" ({_html_escape(_CONTENT_LABEL[card_filter])})"
        for idx, job in enumerate(jobs, 1):
            try:
                icon, title = _pretty_title(job.url)
            except Exception:
                icon, title = "🔗", job.url
            lines.append(f"{idx}. {icon} {title}")
        if skipped > 0:
            lines.append(f"⚠️ +{skipped} duplicates skipped.")
        try:
            if self._bot is not None:
                await _send_safe(self._bot, chat_id, "\n".join(lines),
                                 parse_mode=ParseMode.HTML)
        except Exception:
            pass
        return len(jobs)

    async def _acceptall_confirm_timeout(self, uid: int, token) -> None:
        """60s default-NO for the channel confirm: no answer in time means
        scrape in DM. Token-invalidated by re-taps/answers. Never raises."""
        try:
            await asyncio.sleep(_SEARCH_CONFIRM_TIMEOUT)
        except Exception:
            return
        try:
            cur = _SEARCH_CONFIRM.get(uid)
            if cur is None or cur[0] is not token:
                return  # answered in time or superseded by a re-tap
            _, chat_id, message_id, user_name, username = cur[:5]
            _filter_to = cur[5] if len(cur) > 5 else "all"
            if _filter_to not in _CONTENT_FILTERS:
                _filter_to = "all"
            _SEARCH_CONFIRM.pop(uid, None)
            entry = _SEARCH_CACHE.pop(uid, None)
            if entry is None:
                return
            _ts, _label, results = entry
            if self._bot is None:
                return
            try:
                await self._bot.delete_message(chat_id=chat_id, message_id=message_id)
            except Exception:
                pass
            # Timeout default is NO (scrape here in DM) -- queued, never
            # rejected: every link queues.
            await self._acceptall_enqueue(uid, chat_id, results, user_name, username,
                                          to_channel=0, card_filter=_filter_to)
        except Exception:
            pass

    async def _on_search_pick(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        if update.effective_user is None or update.effective_chat is None:
            return
        if not bot_config.is_allowed(query.from_user.id, self.cfg):
            return  # silent ignore
        uid = query.from_user.id
        data = query.data or ""
        parts = data.split(":")
        if parts[:2] == ["search", "close"]:
            # ❌ Close: remove the whole results list, drop the cache
            # (and any pending channel-confirm for it).
            _SEARCH_CACHE.pop(uid, None)
            _SEARCH_CONFIRM.pop(uid, None)
            if query.message is not None:
                try:
                    await query.message.delete()
                except Exception:
                    pass
            return
        if parts[:2] == ["search", "acceptall"]:
            # ✅ Accept all: don't enqueue yet -- first ask WHAT to scrape
            # (All / Episodes / Packs-Zips), then "scrape to channel?".
            # Cache is kept until the answers (or timeout) consume it.
            entry_all = _SEARCH_CACHE.get(uid)
            if entry_all is None or time.time() - entry_all[0] > _SEARCH_CACHE_TTL:
                _SEARCH_CACHE.pop(uid, None)
                if query.message is not None:
                    try:
                        await query.edit_message_text("⌛ Search expired -- send /search again.")
                    except Exception:
                        pass
                return
            _ts_all, _label_all, _results_all = entry_all
            _kb_content = InlineKeyboardMarkup([  # type: ignore[misc]
                [InlineKeyboardButton("✅ All (EP+Pack)", callback_data="search:content:all")],  # type: ignore[misc]
                [InlineKeyboardButton("🎞️ Episodes only", callback_data="search:content:ep")],  # type: ignore[misc]
                [InlineKeyboardButton("🗜️ Packs/Zips only", callback_data="search:content:pack")],  # type: ignore[misc]
                [InlineKeyboardButton("❌ Close", callback_data="search:close")],  # type: ignore[misc]
            ])
            if query.message is not None:
                try:
                    await query.edit_message_text(
                        f"Scrape <b>{len(_results_all)} links</b> -- what should I resolve?",
                        reply_markup=_kb_content,
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
            return
        if len(parts) == 3 and parts[0] == "search" and parts[1] == "content" \
                and parts[2] in ("all", "ep", "pack"):
            # Content choice: store the filter, then ask "scrape to
            # channel?" exactly as before. The confirm entry carries the
            # filter through to enqueue (and to the timeout default).
            entry_ct = _SEARCH_CACHE.get(uid)
            if entry_ct is None or time.time() - entry_ct[0] > _SEARCH_CACHE_TTL:
                _SEARCH_CACHE.pop(uid, None)
                if query.message is not None:
                    try:
                        await query.edit_message_text("⌛ Search expired -- send /search again.")
                    except Exception:
                        pass
                return
            _ts_ct, _label_ct, _results_ct = entry_ct
            _filter = _CONTENT_CALLBACK[parts[2]]
            _tok = object()
            _nm0, _un0 = _display_name(update.effective_user)
            _SEARCH_CONFIRM[uid] = (_tok, update.effective_chat.id,
                                    query.message.message_id if query.message is not None else 0,
                                    _nm0, _un0, _filter)
            _kb_yn = InlineKeyboardMarkup([[  # type: ignore[misc]
                InlineKeyboardButton("✅ Yes, to channel", callback_data="search:tochan:yes"),  # type: ignore[misc]
                InlineKeyboardButton("❌ No, here in DM", callback_data="search:tochan:no"),  # type: ignore[misc]
            ]])
            if query.message is not None:
                try:
                    await query.edit_message_text(
                        f"Scrape <b>{len(_results_ct)} links</b> "
                        f"({_html_escape(_CONTENT_LABEL[_filter])}) to your channel?"
                        f" (No answer in {_search_confirm_human_timeout()} = No, scrape here.)",
                        reply_markup=_kb_yn,
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
            try:
                asyncio.get_running_loop().create_task(
                    self._acceptall_confirm_timeout(uid, _tok))
            except Exception:
                pass
            return
        if len(parts) == 3 and parts[0] == "search" and parts[1] == "tochan" \
                and parts[2] in ("yes", "no"):
            # Answer (or nothing -- the timeout task owns the default-NO).
            _cur = _SEARCH_CONFIRM.pop(uid, None)
            if _cur is None:
                return  # already answered / timed out; ignore double taps
            _tok_got = _cur[0]
            # 6-tuple (token, chat_id, message_id, name, username, filter);
            # len-guard keeps it tolerant if the shape ever changes.
            _filter_yn = _cur[5] if len(_cur) > 5 else "all"
            if _filter_yn not in _CONTENT_FILTERS:
                _filter_yn = "all"
            entry_yn = _SEARCH_CACHE.get(uid)
            if entry_yn is None or time.time() - entry_yn[0] > _SEARCH_CACHE_TTL:
                _SEARCH_CACHE.pop(uid, None)
                if query.message is not None:
                    try:
                        await query.edit_message_text("⌛ Search expired -- send /search again.")
                    except Exception:
                        pass
                return
            _ts_yn, _label_yn, _results_yn = entry_yn
            _to_channel = 0
            _chan_note = ""
            if parts[2] == "yes":
                try:
                    from xdmovies.scrape_all import load_channel
                    _ch = load_channel()
                except Exception:
                    _ch = None
                if _ch is None or not int(getattr(_ch, "channel_id", 0) or 0):
                    _SEARCH_CACHE.pop(uid, None)
                    if query.message is not None:
                        try:
                            await query.edit_message_text(
                                "No channel set -- send /scrapeallchannel and forward "
                                "a channel message first, then accept again.")
                        except Exception:
                            pass
                    return
                _to_channel = int(_ch.channel_id)
                try:
                    _chan_note = f" → <b>{_html_escape(getattr(_ch, 'title', '') or str(_to_channel))}</b>"
                except Exception:
                    _chan_note = ""
            _SEARCH_CACHE.pop(uid, None)
            if query.message is not None:
                try:
                    await query.message.delete()
                except Exception:
                    pass
            _nm_yn, _un_yn = _display_name(update.effective_user)
            _n = await self._acceptall_enqueue(uid, update.effective_chat.id,
                                               _results_yn, _nm_yn, _un_yn,
                                               to_channel=_to_channel,
                                               card_filter=_filter_yn)
            if _to_channel and _n:
                try:
                    if self._bot is not None:
                        await _send_safe(self._bot, update.effective_chat.id,
                                         f"📢 Scraping to channel{_chan_note}. Progress lands there; "
                                         f"this chat stays quiet.",
                                         parse_mode=ParseMode.HTML)
                except Exception:
                    pass
            return
        if len(parts) != 3 or parts[0] != "search" or parts[1] not in ("pick", "page"):
            return
        try:
            idx = int(parts[2])
        except ValueError:
            return
        entry = _SEARCH_CACHE.get(uid)
        if entry is None or time.time() - entry[0] > _SEARCH_CACHE_TTL:
            _SEARCH_CACHE.pop(uid, None)
            if query.message is not None:
                try:
                    await query.edit_message_text("⌛ Search expired -- send /search again.")
                except Exception:
                    pass
            return
        _ts, cached_query, results = entry
        if parts[1] == "page":
            page, total_pages, keyboard = _search_page_keyboard(results, idx)
            if query.message is not None:
                try:
                    await query.edit_message_text(
                        _search_page_text(cached_query, results, page, total_pages),
                        reply_markup=keyboard,
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
            return
        if idx < 0 or idx >= len(results):
            return
        picked = results[idx]
        # Urgent lane: search pick while a channel scrape runs jumps
        # ahead (movie-boundary preempt). Otherwise the pick just queues.
        if self._is_scrape_running():
            _nm, _un = _display_name(update.effective_user)
            job = Job(user_id=uid, chat_id=update.effective_chat.id, url=picked.url,
                      user_name=_nm, username=_un, priority=1)
            try:
                pos = await self._enqueue(job, urgent=True)
            except Exception:
                pos = 1
            _SEARCH_CACHE.pop(uid, None)
            _SEARCH_CONFIRM.pop(uid, None)
            if query.message is not None:
                try:
                    await query.message.delete()
                except Exception:
                    pass
            try:
                job.enqueue_msg_id = await _send_safe(
                    self._bot, update.effective_chat.id,
                    "⚡ <b>Urgent queued</b> — runs after the current movie finishes 10/10, "
                    "then the channel scrape resumes.",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            return
        # Every pick queues -- no per-user busy rejection.
        _nm, _un = _display_name(update.effective_user)
        job = Job(user_id=uid, chat_id=update.effective_chat.id, url=picked.url,
                  user_name=_nm, username=_un)
        pos = await self._enqueue(job)
        _SEARCH_CACHE.pop(uid, None)
        _SEARCH_CONFIRM.pop(uid, None)
        # Clean chat: delete the results list now that a pick was made.
        # The confirm note below still shows what was queued. Delete may
        # fail (message too old, rights); never let that break the pick.
        if query.message is not None:
            try:
                await query.message.delete()
            except Exception:
                pass
        job.enqueue_msg_id = await self._confirm_enqueued(context, update.effective_chat.id, picked.url, pos)

    async def _on_document(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
        """Handle an uploaded .txt file: one movie/series title per line.

        Searches every title and replies with ONE merged paginated
        pick-list (same buttons/cache/pick flow as /search). ✅ Accept all
        asks WHAT to resolve (All / Episodes / Packs-Zips) and then the
        scrape-to-channel question. Skips blanks,
        caps lines at _TXT_MAX_LINES. Not a command, so the menu is
        untouched; access control identical to /search (silent ignore).
        """
        if update.effective_user is None or update.effective_chat is None or update.message is None:
            return
        uid = update.effective_user.id
        if not bot_config.is_allowed(uid, self.cfg):
            return  # silent ignore
        doc = getattr(update.message, "document", None)
        file_name = str(getattr(doc, "file_name", "") or "")
        if doc is None or not file_name.lower().endswith(".txt"):
            await _send_safe(
                context.bot, update.effective_chat.id,
                "Send a <code>.txt</code> file with one movie/series title per line.",
                parse_mode=ParseMode.HTML,
            )
            return
        try:
            if int(getattr(doc, "file_size", 0) or 0) > _TXT_MAX_BYTES:
                await _send_safe(
                    context.bot, update.effective_chat.id,
                    "[FAIL] .txt too big (max 64KB). Split it and resend.",
                    parse_mode=ParseMode.HTML,
                )
                return
            tg_file = await context.bot.get_file(doc.file_id)
            raw = await tg_file.download_as_bytearray()
        except Exception as exc:
            _LOG.warning("txt download failed: %r", exc)
            await _send_safe(
                context.bot, update.effective_chat.id,
                "[FAIL] could not read that file -- try again.",
                parse_mode=ParseMode.HTML,
            )
            return
        try:
            text = bytes(raw).decode("utf-8", errors="replace")
        except Exception:
            text = ""
        titles = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                titles.append(line)
        dropped = max(0, len(titles) - _TXT_MAX_LINES)
        titles = titles[:_TXT_MAX_LINES]
        if not titles:
            await _send_safe(
                context.bot, update.effective_chat.id,
                "Empty file -- put one movie/series title per line, e.g.\n"
                "<code>Lucky Baskhar\nThe Assessment</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        try:
            from xdmovies.search import search_titles
        except Exception as exc:
            _LOG.warning("search import failed: %r", exc)
            await _send_safe(
                context.bot, update.effective_chat.id,
                "[FAIL] search is unavailable right now -- try again later.",
                parse_mode=ParseMode.HTML,
            )
            return
        flat = []
        no_match = []
        try:
            for title in titles:
                try:
                    found = await search_titles(title, limit=5)
                except Exception as exc:
                    raise exc
                if found:
                    flat.extend(found)
                else:
                    no_match.append(title)
        except Exception as exc:
            _LOG.warning("txt batch search failed: %r", exc)
            await _send_safe(
                context.bot, update.effective_chat.id,
                "[FAIL] search is unavailable right now -- try again later.",
                parse_mode=ParseMode.HTML,
            )
            return
        if not flat:
            msg = "Not found."
            if dropped:
                msg += f"\n⚠️ +{dropped} lines ignored (max {_TXT_MAX_LINES})."
            await _send_safe(
                context.bot, update.effective_chat.id,
                msg,
                parse_mode=ParseMode.HTML,
            )
            return
        label = f"📄 {file_name} ({len(titles)} titles)"
        _SEARCH_CACHE[uid] = (time.time(), label, flat)
        page, total_pages, keyboard = _search_page_keyboard(flat, 1)
        msg_text = _search_page_text(label, flat, page, total_pages)
        # NOTE: suffix lines below show only on page 1 (page turns
        # re-render via _search_page_text). Accepted: keeps nav simple.
        suffix = ""
        if no_match:
            shown = ", ".join(no_match[:5])
            more = f" +{len(no_match) - 5} more" if len(no_match) > 5 else ""
            suffix += f"\n❌ Not found: {shown}{more}"
        if dropped:
            suffix += f"\n⚠️ +{dropped} lines ignored (max {_TXT_MAX_LINES})."
        await update.message.reply_text(
            msg_text + suffix,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )

    async def _enqueue_urgent_links(self, uid: int, chat_id: int,
                                    urls: List[str], user_name: str = "",
                                    username: str = "") -> List[Job]:
        """Enqueue resolve URLs into the urgent lane (FIFO). Never raises."""
        jobs: List[Job] = []
        try:
            for u in urls or []:
                try:
                    job = Job(user_id=uid, chat_id=chat_id, url=u,
                              user_name=user_name, username=username,
                              priority=1)
                    await self._enqueue(job, urgent=True)
                    jobs.append(job)
                except Exception:
                    continue
        except Exception:
            pass
        return jobs

    async def _resolve_channel_title(self, uid: int, chat_id: int, raw_title: str,
                                     user_name: str = "", username: str = "") -> bool:
        """Search + exact-match enqueue for a channel-post title.

        Exact normalized-title match auto-enqueues (normal or urgent).
        When nothing matches exactly, shows the search picker instead of
        blindly taking the top fuzzy hit (which scraped the wrong movie,
        e.g. "Dark" -> "Darkness Falls"). Returns True when handled
        (enqueued or picker shown), False on search failure/empty
        (caller already replied). Never raises — all failures reply inline.
        """
        try:
            from xdmovies.channel_post import find_exact_match
            from xdmovies.search import SearchUnavailable, search_titles
        except Exception as exc:
            _LOG.warning("channel title search import failed: %r", exc)
            await _send_safe(
                self._bot, chat_id,
                "[FAIL] search is unavailable right now -- try again later.",
                parse_mode=ParseMode.HTML,
            )
            return False
        try:
            results = await search_titles(raw_title, limit=_SEARCH_FETCH_LIMIT)
        except Exception as exc:
            _LOG.warning("channel title search failed for %r: %r", raw_title, exc)
            await _send_safe(
                self._bot, chat_id,
                "[FAIL] search is unavailable right now -- try again later.",
                parse_mode=ParseMode.HTML,
            )
            return False
        if not results:
            await _send_safe(
                self._bot, chat_id,
                f"Not found: <code>{_html_escape(raw_title)}</code>",
                parse_mode=ParseMode.HTML,
            )
            return False
        try:
            picked = find_exact_match(raw_title, results)
        except Exception:
            picked = None
        if picked is None:
            # No exact match — let the user pick instead of scraping the
            # wrong title. Reuses the /search picker flow (cache + buttons).
            try:
                _SEARCH_CACHE[uid] = (time.time(), raw_title, results)
                page, total_pages, keyboard = _search_page_keyboard(results, 1)
                text = (
                    f"⚠️ No exact match for <code>{_html_escape(raw_title)}</code> — "
                    f"tap the right one below (wrong auto-pick disabled).\n"
                    + _search_page_text(raw_title, results, page, total_pages)
                )
                await _send_markup_safe(self._bot, chat_id, text, keyboard)
            except Exception:
                _LOG.exception("channel title picker failed")
                await _send_safe(
                    self._bot, chat_id,
                    f"Not found: <code>{_html_escape(raw_title)}</code>",
                    parse_mode=ParseMode.HTML,
                )
            return True
        try:
            url = getattr(picked, "url", "")
            title = getattr(picked, "title", "") or raw_title
            if not url:
                raise ValueError("empty pick url")
        except Exception:
            await _send_safe(
                self._bot, chat_id,
                f"Not found: <code>{_html_escape(raw_title)}</code>",
                parse_mode=ParseMode.HTML,
            )
            return False
        urgent = self._is_scrape_running()
        job = Job(user_id=uid, chat_id=chat_id, url=url,
                  user_name=user_name, username=username,
                  priority=1 if urgent else 0)
        try:
            pos = await self._enqueue(job, urgent=urgent)
        except Exception:
            await _send_safe(self._bot, chat_id, "[FAIL] could not queue that title -- try again.")
            return False
        try:
            if urgent:
                job.enqueue_msg_id = await _send_safe(
                    self._bot, chat_id,
                    f"🔎 <code>{_html_escape(raw_title)}</code> → <b>{_html_escape(title)}</b>\n"
                    f"⚡ <b>Urgent queued</b> (position #{pos} urgent) — runs after the current movie finishes 10/10, "
                    f"then the channel scrape resumes.",
                    parse_mode=ParseMode.HTML,
                )
            else:
                try:
                    job.enqueue_msg_id = await self._confirm_enqueued(
                        types.SimpleNamespace(bot=self._bot), chat_id, url, pos)
                except Exception:
                    job.enqueue_msg_id = await _send_safe(
                        self._bot, chat_id,
                        f"🔎 <code>{_html_escape(raw_title)}</code> → <b>{_html_escape(title)}</b>\n"
                        f"📥 Queued.",
                        parse_mode=ParseMode.HTML,
                    )
        except Exception:
            pass
        return True

    async def _on_text(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
        if update.effective_user is None or update.effective_chat is None or update.message is None:
            return
        uid = update.effective_user.id
        if not bot_config.is_allowed(uid, self.cfg):
            return  # silent ignore
        # Channel-setup consumes forwarded channel messages (and only
        # while the user has a pending /scrapeallchannel setup).
        try:
            if await self._try_channel_setup(update, context):
                return
        except Exception:
            pass
        # Photo/video forwards carry the post in caption, plain DMs in
        # text. Read both so forwarded channel cards work.
        try:
            msg = update.message
            text = ((getattr(msg, "text", None) or getattr(msg, "caption", None) or "")).strip()
        except Exception:
            text = ""
        urls, dropped = _extract_links(text)
        _nm, _un = _display_name(update.effective_user)
        # Urgent lane: any allowed user sending a resolve link while a
        # channel scrape runs jumps ahead (movie-boundary preempt inside
        # _run_scrape_all).
        if urls and self._is_scrape_running():
            try:
                jobs = await self._enqueue_urgent_links(
                    uid, update.effective_chat.id, urls, _nm, _un)
            except Exception:
                jobs = []
            if not jobs:
                await _send_safe(
                    context.bot, update.effective_chat.id,
                    "[FAIL] could not queue those links -- try again.",
                    parse_mode=ParseMode.HTML,
                )
                return
            lines = ["⚡ <b>Urgent queued</b> — runs after the current movie finishes 10/10, then the channel scrape resumes."]
            for idx, job in enumerate(jobs, 1):
                try:
                    icon, title = _pretty_title(job.url)
                except Exception:
                    icon, title = "🔗", job.url
                lines.append(f"{idx}. {icon} {title}")
            if dropped:
                lines.append(f"⚠️ +{dropped} more ignored (max {MAX_LINKS_PER_MESSAGE}/message).")
            await _send_safe(context.bot, update.effective_chat.id, "\n".join(lines),
                             parse_mode=ParseMode.HTML)
            return
        if urls:
            # Every link queues -- no per-user busy rejection. The single
            # dispatcher resolves jobs FIFO; _confirm_enqueued tells the
            # user "Starting now" vs "Queued position #N".
            if len(urls) == 1:
                job = Job(user_id=uid, chat_id=update.effective_chat.id, url=urls[0],
                          user_name=_nm, username=_un)
                pos = await self._enqueue(job)
                job.enqueue_msg_id = await self._confirm_enqueued(context, update.effective_chat.id, urls[0], pos)
                return
            jobs = []
            for url in urls:
                job = Job(user_id=uid, chat_id=update.effective_chat.id, url=url,
                          user_name=_nm, username=_un)
                await self._enqueue(job)
                jobs.append(job)
            lines = [f"📥 <b>Queued {len(jobs)} links</b> -- resolving one by one."]
            for idx, job in enumerate(jobs, 1):
                icon, title = _pretty_title(job.url)
                lines.append(f"{idx}. {icon} {title}")
            if dropped:
                lines.append(f"⚠️ +{dropped} more ignored (max {MAX_LINKS_PER_MESSAGE}/message).")
            await _send_safe(context.bot, update.effective_chat.id, "\n".join(lines),
                             parse_mode=ParseMode.HTML)
            return

        # No URLs — maybe a forwarded XDMovies channel post
        # (photo+caption). Extract the title and auto-scrape it.
        try:
            from xdmovies.channel_post import parse_channel_title
            _chan_title = parse_channel_title(text)
        except Exception:
            _chan_title = None
        if _chan_title:
            # Queues like pasted URLs (urgent when a scrape runs -- handled
            # inside _resolve_channel_title). No busy rejection.
            try:
                await self._resolve_channel_title(
                    uid, update.effective_chat.id, _chan_title, _nm, _un)
            except Exception:
                _LOG.exception("channel title resolve failed")
                await _send_safe(
                    context.bot, update.effective_chat.id,
                    "[FAIL] could not resolve that title -- try again.",
                    parse_mode=ParseMode.HTML,
                )
            return
        await _send_safe(
            context.bot, update.effective_chat.id,
            "[FAIL] not a supported URL. Send a movie page like `https://top.xdmovies.wtf/movies/<slug>` "
            "or a download link like `https://link.xdmovies.wtf/download/<token>` "
            "or a direct gate link like `https://latestnewsonline.sbs/r/<code>`",
            parse_mode=ParseMode.HTML,
        )
        return

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
        # auto-detect — 30m catalog poll -> new posts -> channel
        try:
            from xdmovies.autodetect import start_autodetect
            await start_autodetect(self)
            _LOG.info("autodetect loop scheduled (30m interval)")
        except Exception:
            _LOG.exception("autodetect start failed (non-fatal)")
        for h in application.handlers.values():
            for hh in h:
                _LOG.info("handler: %s", type(hh).__name__)
        try:
            from telegram import BotCommand
            await application.bot.set_my_commands([
                BotCommand("start", "show welcome card"),
                BotCommand("search", "find a movie/series, tap to scrape"),
                BotCommand("status", "show bot health"),
                BotCommand("queue", "show running + queued jobs"),
                BotCommand("cancel", "stop my running/queued job(s)"),
                BotCommand("restart", "restart the whole bot (owner only)"),
                BotCommand("scrapeall", "scrape all movies or series"),
                BotCommand("scrapeallchannel", "scrape to channel (owner only)"),
                BotCommand("qmode", "toggle quality picker for series"),
                BotCommand("dmode", "toggle section picker for series"),
                BotCommand("settings", "choose result card details"),
                BotCommand("autodetect", "auto-detect new posts to channel (owner)"),
            ])
        except Exception:
            _LOG.exception("set_my_commands failed (non-fatal)")
        await self._start_dispatcher_after_polling()
        try:
            await _announce_restart_if_pending(application.bot)
        except Exception:
            pass

    async def _post_stop(self, _application: "Application") -> None:
        try:
            from xdmovies.autodetect import stop_autodetect
            await stop_autodetect()
        except Exception:
            pass
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
        app.add_handler(CommandHandler("restart", self._cmd_restart))
        app.add_handler(CommandHandler("scrapeall", self._cmd_scrapeall))
        app.add_handler(CommandHandler("scrapeallchannel", self._cmd_scrapeallchannel))
        app.add_handler(CommandHandler("search", self._cmd_search))
        app.add_handler(CallbackQueryHandler(
            self._on_search_pick,
            pattern=r"^search:(pick|page):\d+$|^search:(close|acceptall)$|^search:content:(all|ep|pack)$|^search:tochan:(yes|no)$",
        ))
        app.add_handler(CallbackQueryHandler(
            self._on_scrapeall_choice,
            pattern=r"^scrapeall:(select|resume|start):(all|movies|series)$|^scrapeall:cancel:-$",
        ))
        app.add_handler(CallbackQueryHandler(
            self._on_scrapechan_choice,
            pattern=r"^scrapechan:(select|resume|start):(all|movies|series)$|^scrapechan:cancel:-$",
        ))
        app.add_handler(CallbackQueryHandler(
            self._on_qmode_choice,
            pattern=r"^qmode:set:(yes|no)$",
        ))
        app.add_handler(CallbackQueryHandler(
            self._on_qpick,
            pattern=r"^qp:[0-9a-f]{8}:(t:\d+|done|cancel|all)$",
        ))
        app.add_handler(CommandHandler("qmode", self._cmd_qmode))
        app.add_handler(CallbackQueryHandler(
            self._on_dmode_choice,
            pattern=r"^dmode:set:(yes|no)$",
        ))
        app.add_handler(CallbackQueryHandler(
            self._on_dpick,
            pattern=r"^dp:[0-9a-f]{8}:(t:\d+|done|cancel|all)$",
        ))
        app.add_handler(CommandHandler("dmode", self._cmd_dmode))
        app.add_handler(CommandHandler("settings", self._cmd_settings))
        app.add_handler(CallbackQueryHandler(
            self._on_settings,
            pattern=r"^settings:(toggle:[a-z_]+|all|close)$",
        ))
        app.add_handler(CommandHandler("autodetect", self._cmd_autodetect))
        app.add_handler(CallbackQueryHandler(
            self._on_autodetect_feed,
            pattern=r"^autodetect:feed:(movies|series|all)$",
        ))
        app.add_handler(CallbackQueryHandler(
            self._on_autodetect_set,
            pattern=r"^autodetect:set:(yes|no)$",
        ))
        app.add_handler(CallbackQueryHandler(
            self._on_autodetect_content,
            pattern=r"^autodetect:content:(show|all|ep|pack)$",
        ))
        app.add_handler(CallbackQueryHandler(
            self._on_autodetect_back,
            pattern=r"^autodetect:back$",
        ))
        app.add_handler(MessageHandler(filters.Document.ALL, self._on_document))
        # TEXT covers plain DMs, CAPTION covers forwarded channel
        # photo+caption posts (filters are disjoint in PTB v21).
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_text))
        try:
            app.add_handler(MessageHandler(filters.CAPTION & ~filters.COMMAND, self._on_text))
        except Exception:
            pass
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
    if _LOCK is None and os.name == "posix":
        # Another LIVE bot holds the flock (kernel-managed: never stale).
        # Refuse to start: a second poller would split Telegram updates
        # with the running instance (commands answered by one, jobs run
        # by the other, possibly on older code) with zero errors logged.
        # /restart is safe: os.execv closes our non-inheritable fd first,
        # so the new image re-acquires cleanly.
        print(
            f"another xdmovies-bot instance is already running "
            f"(lock {_LOCK_PATH} held); refusing to start a second poller. "
            f"Stop it first (systemctl stop xdmovies-bot) or run "
            f"bash xdmovies_kill_all.sh",
            file=sys.stderr,
        )
        raise SystemExit(2)
    # On Windows _LOCK is None even on success (lockfile write is the
    # signal). We can't reliably detect a stolen instance there without
    # psutil; assume success if the lockfile write succeeded.
    cfg = bot_config.load_config()
    bot_config.configure_logging(cfg)
    _BOT_MAX_SESSIONS = cfg.max_sessions
    _LOG.info(
        "config loaded: pid=%d owner=%d admin_ids=%s max_sessions=%d",
        os.getpid(),
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

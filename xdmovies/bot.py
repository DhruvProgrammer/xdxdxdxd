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
import subprocess
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
_LOCK = None


class _SingleInstanceLock:
    def __init__(self, path: str) -> None:
        self.path = path
        self.fd: Optional[int] = None
        self.token = uuid.uuid4().hex
        self.owned = False

    def acquire(self) -> bool:
        fd = None
        try:
            flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0)
            fd = os.open(self.path, flags, 0o600)
            bot_config.ensure_private_file(self.path)
            if os.name == "nt":
                import msvcrt
                if os.fstat(fd).st_size < 1:
                    os.write(fd, b" ")
                    os.ftruncate(fd, 1)
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            payload = json.dumps(
                {"pid": os.getpid(), "token": self.token, "started_at": time.time()},
                separators=(",", ":"),
            ).encode("ascii")
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, payload)
            self.fd = fd
            self.owned = True
            return True
        except (BlockingIOError, OSError):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            return False

    def close(self) -> None:
        fd = self.fd
        if fd is None or not self.owned:
            return
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, 512)
            try:
                owned = json.loads(raw.decode("ascii")).get("token") == self.token
            except Exception:
                owned = False
            if owned:
                os.ftruncate(fd, 1)
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, b" ")
        except OSError:
            pass
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            self.fd = None
            self.owned = False


def _acquire_single_instance_lock() -> Optional[_SingleInstanceLock]:
    lock = _SingleInstanceLock(_LOCK_PATH)
    return lock if lock.acquire() else None

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


def _op_dm(job) -> int:
    """DM target for operational chatter (solve links, wait pills,
    timeouts, FAILs, error tracebacks).

    For channel-routed jobs the public channel must NEVER see the
    VPS-IP solve link or any solve/timeout chatter -- those go to the
    requester instead (job.user_id, the owner for autodetect jobs).
    Channel-routed = chat IS the channel (chat_id == post_to_channel).
    Scrape-channel children already run in the requester's DM
    (chat_id != post_to_channel), so they are unaffected.
    Result batches are NOT routed here -- they stay on the channel.
    """
    try:
        chan = getattr(job, "post_to_channel", 0) or 0
        chat = getattr(job, "chat_id", 0) or 0
        uid = getattr(job, "user_id", 0) or 0
        if chan and chat and chat == chan and uid:
            return int(uid)
        return int(chat)
    except Exception:
        try:
            return int(getattr(job, "chat_id", 0) or 0)
        except Exception:
            return 0


def _restart_notice_path() -> str:
    try:
        return os.path.join(tempfile.gettempdir(), "xdmovies_restart_notice.json")
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".xdmovies_restart_notice.json")


def _write_restart_notice(chat_id: int) -> None:
    try:
        with bot_config.open_private_text(_restart_notice_path(), "w", encoding="utf-8") as f:
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


def _parse_cancel_args(raw) -> Tuple[List[int], List[str]]:
    """Parse `/cancel 2` / `/cancel 2,4,8` args into queue positions.

    Splits on commas/whitespace. Returns (sorted unique 1-based indices,
    invalid tokens). Never raises.
    """
    want: List[int] = []
    bad: List[str] = []
    seen = set()
    try:
        toks: List[str] = []
        for tok in raw or []:
            toks.extend(str(tok).replace(",", " ").split())
        for piece in toks:
            piece = piece.strip()
            if not piece:
                continue
            try:
                v = int(piece)
            except ValueError:
                bad.append(piece)
                continue
            if v < 1 or v in seen:
                if v not in seen:
                    bad.append(piece)
                continue
            seen.add(v)
            want.append(v)
    except Exception:
        pass
    return sorted(want), bad


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


def _url_host_label(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return (urlparse(url or "").hostname or "unknown-host").lower()
    except Exception:
        return "unknown-host"


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
            _LOG.info("hub dest kept at hub page (gamerxyt upgrade skipped): %s", _url_host_label(dest))
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
_SCRAPE_PROMPTS: Dict[str, dict] = {}
_SCRAPE_PROMPT_TTL = 600.0


def _new_scrape_prompt(prefix: str, uid: int, route: str, chat_id: int, channel_id: int) -> str:
    now = time.time()
    for token, entry in tuple(_SCRAPE_PROMPTS.items()):
        if now - float(entry.get("created_at", 0)) > _SCRAPE_PROMPT_TTL:
            _SCRAPE_PROMPTS.pop(token, None)
        elif entry.get("uid") == uid and entry.get("route") == route:
            _SCRAPE_PROMPTS.pop(token, None)
    token = uuid.uuid4().hex[:8]
    while token in _SCRAPE_PROMPTS:
        token = uuid.uuid4().hex[:8]
    _SCRAPE_PROMPTS[token] = {
        "prefix": prefix,
        "uid": uid,
        "route": route,
        "chat_id": chat_id,
        "channel_id": channel_id,
        "message_id": 0,
        "created_at": now,
    }
    return token


def _scrape_prompt_entry(token: str, query) -> Optional[dict]:
    entry = _SCRAPE_PROMPTS.get(token)
    if entry is None:
        return None
    if time.time() - float(entry.get("created_at", 0)) > _SCRAPE_PROMPT_TTL:
        _SCRAPE_PROMPTS.pop(token, None)
        return None
    message = getattr(query, "message", None)
    message_id = int(getattr(message, "message_id", 0) or 0)
    if (
        int(getattr(getattr(query, "from_user", None), "id", 0) or 0) != int(entry.get("uid", 0))
        or int(getattr(message, "chat_id", 0) or 0) != int(entry.get("chat_id", 0))
        or message_id != int(entry.get("message_id", 0))
    ):
        return None
    return entry


# /search pick cache: user_id -> (timestamp, query, [SearchResult]).
# Entries expire after 20 minutes -- must comfortably exceed the
# 15-minute channel-confirm window or late Yes taps would find the
# cache gone. In-memory only; a restart drops pending picks (user just
# re-sends /search).
_SEARCH_CACHE: Dict[int, tuple] = {}
_SEARCH_CACHE_TTL = 1200.0


def _bounded_env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)).strip() or str(default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)).strip() or str(default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


_SEARCH_FETCH_LIMIT = 30
_SEARCH_PAGE_SIZE = 5
_SEARCH_TIMEOUT_TASKS: Dict[int, asyncio.Task] = {}
_TXT_SEARCH_CANCEL: Dict[int, asyncio.Event] = {}
SCRAPE_MAX_PAGES = max(1, int(os.environ.get("XDMOVIES_SCRAPE_MAX_PAGES", "1000") or "1000"))

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
_TXT_MAX_LINES = _bounded_env_int("XDMOVIES_TXT_MAX_LINES", 10000, 1, 10000)
_TXT_MAX_BYTES = 65536
_TXT_SEARCH_DEADLINE = _bounded_env_float(
    "XDMOVIES_TXT_DEADLINE", 60.0, 5.0, 600.0
)
_TXT_SEARCH_CONCURRENCY = _bounded_env_int(
    "XDMOVIES_TXT_CONCURRENCY", 4, 1, 8
)


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

def _search_cancel(uid: int) -> None:
    task = _SEARCH_TIMEOUT_TASKS.pop(uid, None)
    if task is not None and task is not asyncio.current_task():
        task.cancel()
    _SEARCH_CONFIRM.pop(uid, None)
    _SEARCH_CACHE.pop(uid, None)


def _new_search_session(uid: int, label: str, results, chat_id: int) -> str:
    _search_cancel(uid)
    token = uuid.uuid4().hex[:8]
    while any(len(entry) > 3 and entry[3] == token for entry in _SEARCH_CACHE.values()):
        token = uuid.uuid4().hex[:8]
    _SEARCH_CACHE[uid] = (time.time(), label, results, token, 0, chat_id)
    return token


def _search_session(uid: int, token: str, query) -> Optional[tuple]:
    entry = _SEARCH_CACHE.get(uid)
    if entry is None or len(entry) < 6 or entry[3] != token:
        return None
    if time.time() - entry[0] > _SEARCH_CACHE_TTL:
        _search_cancel(uid)
        return None
    message = getattr(query, "message", None)
    message_id = int(getattr(message, "message_id", 0) or 0)
    if message_id != int(entry[4] or 0) or int(getattr(message, "chat_id", 0) or 0) != int(entry[5] or 0):
        return None
    return entry


def _search_page_keyboard(token: str, results, page: int):
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
        rows.append([InlineKeyboardButton(label, callback_data=f"search:{token}:pick:{idx}")])  # type: ignore[misc]
    nav = []
    if len(results) > 1:
        # Bulk row above the nav: enqueue everything (capped). Only
        # shown when there is actually a choice to bulk-accept.
        nav.append(InlineKeyboardButton("✅ Accept all", callback_data=f"search:{token}:acceptall"))  # type: ignore[misc]
        rows.append(nav)
        nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"search:{token}:page:{page - 1}"))  # type: ignore[misc]
    nav.append(InlineKeyboardButton("❌ Close", callback_data=f"search:{token}:close"))  # type: ignore[misc]
    if page < total_pages:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"search:{token}:page:{page + 1}"))  # type: ignore[misc]
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
    scrape_route: str = ""
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
        self.max_queue = max(1, int(cfg.max_sessions))
        self.max_user_queue = max(1, min(self.max_queue, 2))
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
        self._active_scrape_children: Dict[str, Job] = {}
        self.current: Optional[Job] = None
        self.last_solve_ago: Optional[float] = None
        self.error_count: int = 0
        self._dispatcher_task: Optional[asyncio.Task] = None
        self._restarting = False
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

    async def _enqueue(self, job: Job, urgent: bool = False, dedupe: bool = False) -> int:
        async with self._queue_cond:
            active = [self.current, *self.queue, *self._urgent, *self._active_scrape_children.values()]
            if dedupe and any(
                item is not None and getattr(item, "url", "") == job.url
                for item in active
            ):
                return -1
            if len(self.queue) + len(self._urgent) >= self.max_queue:
                return -2
            user_depth = sum(1 for item in active if getattr(item, "user_id", None) == job.user_id)
            if user_depth >= self.max_user_queue:
                return -3
            if urgent:
                job.priority = 1
                self._urgent.append(job)
            else:
                job.priority = 0
                self.queue.append(job)
            self._queue_cond.notify_all()
            return len(self._urgent) + len(self.queue)

    async def _enqueue_scrape(self, job: Job, prepare=None) -> int:
        async with self._queue_cond:
            active = [self.current, *self.queue, *self._urgent]
            if any(
                item is not None
                and getattr(item, "job_type", "") == "scrape_all"
                and getattr(item, "content_type", "") == job.content_type
                and getattr(item, "scrape_route", "") == job.scrape_route
                for item in active
            ):
                return -1
            if len(self.queue) + len(self._urgent) >= self.max_queue:
                return -2
            user_depth = sum(1 for item in active if getattr(item, "user_id", None) == job.user_id)
            if user_depth >= self.max_user_queue:
                return -3
            if prepare is not None:
                await asyncio.to_thread(prepare)
            self.queue.append(job)
            self._queue_cond.notify_all()
            return len(self._urgent) + len(self.queue)

    async def _set_scrape_child(self, url: str, job: Job) -> None:
        async with self._queue_cond:
            self._active_scrape_children[url] = job
            self._queue_cond.notify_all()

    async def _clear_scrape_child(self, url: str, job: Job) -> None:
        async with self._queue_cond:
            if self._active_scrape_children.get(url) is job:
                self._active_scrape_children.pop(url, None)
            self._queue_cond.notify_all()

    async def _snapshot_live_urls(self) -> set[str]:
        async with self._queue_cond:
            urls = {
                getattr(job, "url", "")
                for job in [self.current, *self.queue, *self._urgent, *self._active_scrape_children.values()]
                if job is not None and getattr(job, "url", "")
            }
            return urls

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
                job = self._urgent.popleft()
            else:
                job = self.queue.popleft()
            self.current = job
            return job

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
                    _LOG.exception("urgent job failed for host %s", _url_host_label(urgent.url))
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
            # hybrid: try VPS token solve (posts need it, not home).
            # Operational chatter goes to the requester's DM (_op_dm),
            # never the public channel (it carries the VPS IP).
            token, msg_id = await self._send_solve_link(_op_dm(job), blocked_url, host, job.user_id)
            if token:
                _solved = await self._wait_for_solve_message(
                    job,
                    _op_dm(job),
                    token,
                    msg_id,
                    120.0,
                    "⏳ Waiting up to 120s -- open solve link and tap checkbox if you see it. Keep-alive will resume after.",
                )
                if job.cancel_event.is_set():
                    return
                if _solved:
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
                        await _send_safe(self._bot, _op_dm(job), f"[FAIL] Still blocked on <code>{host2}</code> after solve -- try again or /status", parse_mode=ParseMode.HTML)
                        if job.cancel_event.is_set():
                            job.status = "cancelled"
                            job.finished_at = time.time()
                            return
                        job.status = "error"
                        job.error = f"cf_challenge:{host2}"
                        job.finished_at = time.time()
                        return
                else:
                    await _send_safe(self._bot, _op_dm(job), f"⏰ Solve timed out. Resend the link to get a fresh solve link (keep-alive paused).", parse_mode=ParseMode.HTML)
                    if job.cancel_event.is_set():
                        job.status = "cancelled"
                        job.finished_at = time.time()
                        return
                    job.status = "error"
                    job.error = f"cf_challenge:{host}"
                    job.finished_at = time.time()
                    return
            elif msg_id is not None:
                self._stop_after_reported_solve_failure(job, f"solve_unavailable:{host}")
                return
            else:
                _vps = os.environ.get("SOLVE_SERVER_PUBLIC_BASE", "").rstrip("/")
                _vps_note = f"check VPS {_vps}" if _vps else "check the solve-server configuration"
                await _send_safe(self._bot, _op_dm(job), f"🔒 <b>Cloudflare blocked</b> -- <code>{host}</code>\nLink: <code>{blocked_url}</code>\nCould not create solve link -- {_vps_note}", parse_mode=ParseMode.HTML)
                if job.cancel_event.is_set():
                    job.status = "cancelled"
                    job.finished_at = time.time()
                    return
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
                    self._bot, _op_dm(job),
                    f"[FAIL] no {_html_escape(_label)} links found on that page "
                    f"(title: <code>{_html_escape((result.page_title or '').strip()[:80])}</code>). "
                    f"The page exists but has no {_html_escape(_label.lower())} section.",
                    parse_mode=ParseMode.HTML,
                )
                job.status = "cancelled" if job.cancel_event.is_set() else "done"
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
                    self._bot, _op_dm(job),
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
                    self._bot, _op_dm(job),
                    f"[FAIL] no download links found on that page "
                    f"(title: <code>{_html_escape(_title[:80])}</code>, "
                    f"<code>{result.html_len} bytes</code>). The page "
                    f"exists but the download section is empty.",
                    parse_mode=ParseMode.HTML,
                )
            if job.cancel_event.is_set():
                job.status = "cancelled"
                job.finished_at = time.time()
                return
            job.status = "error"
            job.error = "empty_page_stub" if _looks_like_error else "empty_download_section"
            job.finished_at = time.time()
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
                    _LOG.info("direct dest card, skipping follow+gate: %s", _url_host_label(card.url))
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
                    _LOG.info("gate card, skipping follow hop: %s", _url_host_label(card.url))
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
                                    token2, msg_id2 = await self._send_solve_link(_op_dm(job), blocked2, host2, job.user_id)
                                    if token2:
                                        _gate2 = "latestnews" in (host2 or "")
                                        _wait2 = 240.0 if _gate2 else 90.0
                                        _solved2 = await self._wait_for_solve_message(
                                            job,
                                            _op_dm(job),
                                            token2,
                                            msg_id2,
                                            _wait2,
                                            f"⏳ Waiting up to {int(_wait2)}s -- open solve link and tap if needed.",
                                        )
                                        if job.cancel_event.is_set():
                                            return
                                        if _solved2:
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
                                    elif msg_id2 is not None:
                                        self._stop_after_reported_solve_failure(job, f"solve_unavailable:{host2}")
                                        return
                                    err = f"CF challenge on {host2} -- opened solve link {blocked2}, still blocked"
                                    break

                            except Exception:
                                pass
                            err = f"button flow: {exc.__class__.__name__}: {exc}"
                            _LOG.exception("button flow failed for host %s", _url_host_label(url2))
                            break
                        if dest:
                            dest = await _finalize_gate_card(card, dest, result)
                            break
                        if _btn_attempt == 0:
                            _LOG.warning("button flow returned empty for host %s, retrying once", _url_host_label(url2))
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
                        tokenf, msg_idf = await self._send_solve_link(_op_dm(job), blockedf, hostf, job.user_id)
                        if tokenf:
                            _gatef = "latestnews" in (hostf or "")
                            _waitf = 240.0 if _gatef else 90.0
                            _solvedf = await self._wait_for_solve_message(
                                job,
                                _op_dm(job),
                                tokenf,
                                msg_idf,
                                _waitf,
                                f"⏳ Waiting up to {int(_waitf)}s -- open solve link and tap if needed.",
                            )
                            if job.cancel_event.is_set():
                                return
                            if _solvedf:
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
                        elif msg_idf is not None:
                            self._stop_after_reported_solve_failure(job, f"solve_unavailable:{hostf}")
                            return
                        err = f"CF challenge on {hostf} -- opened solve link {blockedf}, still blocked"
                    else:
                        err = f"{exc.__class__.__name__}: {exc}"
                except Exception:
                    err = f"{exc.__class__.__name__}: {exc}"
                _LOG.exception("resolve failed for host %s", _url_host_label(card.url))

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

        if job.cancel_event.is_set():
            job.status = "cancelled"
            job.finished_at = time.time()
            return

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

        if job.cancel_event.is_set():
            job.status = "cancelled"
            job.finished_at = time.time()
            return
        job.status = "done" if job.failed == 0 else "done_with_errors"
        job.finished_at = time.time()
        self.last_solve_ago = 0.0
        if not _is_gate_url(job.url) and not _is_download_url(job.url):
            try:
                from xdmovies.keep_alive import remember_movie_url
                await asyncio.to_thread(remember_movie_url, job.url)
            except Exception:
                pass

    async def _scrape_failure(self, job: Job, state, session_id: str, message: str) -> None:
        state.finished = False
        try:
            from xdmovies.scrape_all import save_checkpoint
            await asyncio.to_thread(save_checkpoint, state)
        except Exception:
            pass
        if job.cancel_event.is_set():
            job.status = "cancelled"
            job.finished_at = time.time()
            return
        job.status = "error"
        job.error = message
        job.finished_at = time.time()
        await _send_safe(self._bot, _op_dm(job), f"[FAIL] {message}", parse_mode=ParseMode.HTML)

    async def _run_scrape_all(self, job: Job) -> None:
        from xdmovies.scrape_all import (
            catalog_page_url,
            extract_catalog_urls,
            extract_pagination_info,
            load_checkpoint,
            save_checkpoint,
        )

        kind = job.content_type
        state = load_checkpoint(kind, route=job.scrape_route)
        if state.finished:
            from xdmovies.scrape_all import ScrapeCheckpoint
            state = ScrapeCheckpoint(kind=kind, route=job.scrape_route)
        state.finished = False
        await asyncio.to_thread(save_checkpoint, state)
        completed = set(state.completed_urls)
        session_id = f"tg_scrapeall_{kind}_{uuid.uuid4().hex[:8]}"
        job.started_at = time.time()
        job.status = "running"
        expected_total_pages: Optional[int] = None
        last_page_fingerprint: Optional[tuple[str, ...]] = None

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
            if page_number > SCRAPE_MAX_PAGES:
                await self._scrape_failure(
                    job,
                    state,
                    session_id,
                    f"Scraping stopped at the page cap ({SCRAPE_MAX_PAGES}).",
                )
                return
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
                token, message_id = await self._send_solve_link(
                    _op_dm(job), blocked, cf_host, job.user_id
                )
                if token:
                    solved = await self._wait_for_solve_message(
                        job,
                        _op_dm(job),
                        token,
                        message_id,
                        120.0,
                        f"⏳ Waiting up to 120s -- open the solve link if you see it.",
                    )
                    if job.cancel_event.is_set():
                        return
                    if solved:
                        continue
                if message_id is not None:
                    state.finished = False
                    try:
                        await asyncio.to_thread(save_checkpoint, state)
                    except Exception:
                        pass
                    self._stop_after_reported_solve_failure(job, f"solve_unavailable:{cf_host}")
                    return
                await self._scrape_failure(
                    job,
                    state,
                    session_id,
                    f"Could not bypass CF on catalog page {page_number}. "
                    f"Scrape paused. Send /scrapeall to resume after solving.",
                )
                return
            except Exception as exc:
                job.error = f"catalog page {page_number}: {exc.__class__.__name__}: {exc}"
                await asyncio.to_thread(save_checkpoint, state)
                await self._fail(job, session_id)
                return

            if not page_urls:
                await self._scrape_failure(
                    job,
                    state,
                    session_id,
                    f"Catalog page {page_number} contained no detail links; scrape paused.",
                )
                return
            if not pagination:
                await self._scrape_failure(
                    job,
                    state,
                    session_id,
                    f"Catalog pagination metadata is missing on page {page_number}; scrape paused.",
                )
                return
            current_page, total_pages = pagination
            if current_page != page_number or total_pages < current_page:
                await self._scrape_failure(
                    job,
                    state,
                    session_id,
                    f"Catalog pagination metadata is invalid on page {page_number}; scrape paused.",
                )
                return
            if expected_total_pages is not None and total_pages != expected_total_pages:
                await self._scrape_failure(
                    job,
                    state,
                    session_id,
                    f"Catalog page count changed during the scrape; scrape paused.",
                )
                return
            expected_total_pages = total_pages
            fingerprint = tuple(page_urls)
            if last_page_fingerprint == fingerprint:
                await self._scrape_failure(
                    job,
                    state,
                    session_id,
                    f"Catalog page {page_number} repeated the previous page; scrape paused.",
                )
                return
            last_page_fingerprint = fingerprint
            pending = [url for url in page_urls if url not in completed]

            for index, url in enumerate(pending, 1):
                if job.cancel_event.is_set():
                    break
                await self._set_progress(
                    job,
                    f"⏳ <b>{kind}</b> page {page_number}: item {index}/{len(pending)}\n"
                    f"total completed: <b>{len(completed)}</b>\n"
                    f"<code>{_html_escape(url)}</code>",
                )
                child = Job(user_id=job.user_id, chat_id=job.user_id, url=url,
                            user_name=job.user_name, username=job.username,
                            post_to_channel=job.post_to_channel,
                            silent=bool(job.post_to_channel),
                            scrape_route=job.scrape_route)
                child.cancel_event = job.cancel_event
                await self._set_scrape_child(url, child)
                try:
                    await self._run_for_user(child)
                    if job.cancel_event.is_set():
                        break
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
                    _LOG.exception("scrapeall item failed for host %s", _url_host_label(url))
                finally:
                    await self._clear_scrape_child(url, child)
                await asyncio.to_thread(save_checkpoint, state)

                # Urgent lane (movie boundary): the current movie just hit
                # 10/10 + checkpoint. If a user sent an urgent link while
                # it resolved, run all urgents now, then resume the scrape
                # with the next movie. Never aborts the finished movie.
                try:
                    if self._has_urgent_sync() and not job.cancel_event.is_set():
                        try:
                            if self._bot is not None:
                                await _send_safe(
                                    self._bot, _op_dm(job),
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
                                    self._bot, _op_dm(job),
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
            if page_number == total_pages:
                state.finished = True
                await asyncio.to_thread(save_checkpoint, state)
                break
            state.next_page = page_number + 1
            await asyncio.to_thread(save_checkpoint, state)

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
        await _send_safe(self._bot, _op_dm(job), text, parse_mode=ParseMode.HTML)

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
        _LOG.error(
            "job failed user=%s url=%s: %s\n%s",
            job.user_id,
            job.url,
            job.error or "unknown",
            traceback.format_exc(),
        )
        if self._bot is None:
            return
        # HTML-escape `job.error` because it may carry user-extracted
        # strings (e.g. card titles containing `<` characters). Falls back
        # to plain text on any parse failure.
        err_text = _html_escape(job.error or "unknown")
        try:
            await self._bot.send_message(
                chat_id=_op_dm(job),
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
                    chat_id=_op_dm(job),
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
                        if self._bot is not None and not job.cancel_event.is_set():
                            await self._bot.send_message(
                                chat_id=_op_dm(job),
                                text=f"[FAIL] unhandled error: {job.error}\nFull traceback: {self.cfg.error_log_path}",
                            )
                    except Exception:
                        pass
                finally:
                    async with self._queue_cond:
                        if self.current is job:
                            self.current = None
                        self._queue_cond.notify_all()
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
            "/cancel -- stop yours: bare = running + all queued, "
            "/cancel 2 or /cancel 2 4 8 (commas also work) = those /queue numbers only\n"
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
            r = await asyncio.to_thread(
                subprocess.run,
                ["systemctl", "is-active", "xdmovies-vnc"],
                capture_output=True,
                timeout=2,
            )
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
        txt_cancel = _TXT_SEARCH_CANCEL.get(uid)
        if txt_cancel is not None:
            txt_cancel.set()

        # Indexed cancel: `/cancel 2` or `/cancel 2,4,8`. Positions are
        # 1-based over YOUR OWN queued jobs (urgent lane first, then
        # normal FIFO -- the same order the dispatcher picks them up).
        # The running job is never indexed; bare `/cancel` keeps the old
        # cancel-everything-of-mine behavior.
        raw_args = list(getattr(context, "args", None) or [])
        if raw_args:
            await self._cmd_cancel_indexed(update, context, uid, raw_args)
            return

        cancelled_running = False
        cancelled_queued = 0
        removed_jobs: List[Tuple[int, str]] = []

        async with self._queue_cond:
            current = self.current
            if current is not None and current.user_id == uid:
                current.cancel_event.set()
                current.status = "cancelled"
                current.finished_at = time.time()
                cancelled_running = True
            for lane in (self._urgent, self.queue):
                kept = []
                for job in tuple(lane):
                    if job.user_id == uid:
                        job.cancel_event.set()
                        job.status = "cancelled"
                        job.finished_at = time.time()
                        cancelled_queued += 1
                        removed_jobs.append((cancelled_queued, job.url))
                    else:
                        kept.append(job)
                lane.clear()
                lane.extend(kept)
            self._queue_cond.notify_all()


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

    async def _cmd_cancel_indexed(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE",
                                  uid: int, raw_args: List[str]) -> None:
        """Cancel specific queued positions: `/cancel 2` / `/cancel 2,4,8`.

        Positions are 1-based over the requester's OWN queued jobs only
        (urgent lane first, then normal FIFO -- dispatcher pick order).
        Rebuilds both deques live under the queue lock (never resurrects
        a job the dispatcher popped mid-snapshot), then replies with what
        was cancelled plus the refreshed, re-numbered remainder.
        """
        want, bad = _parse_cancel_args(raw_args)
        try:
            urgent_items = await self._snapshot_urgent()
        except Exception:
            urgent_items = []
        try:
            items = await self._snapshot_queue()
        except Exception:
            items = []
        mine = [j for j in urgent_items if getattr(j, "user_id", None) == uid]
        mine += [j for j in items if getattr(j, "user_id", None) == uid]

        if not mine:
            await _send_safe(
                context.bot, update.effective_chat.id,
                "ℹ️ you have no queued jobs -- nothing to cancel by number. "
                "Send /queue to see the current list.",
                parse_mode=ParseMode.HTML)
            return

        valid = [i for i in want if 1 <= i <= len(mine)]
        out_of_range = [str(i) for i in want if not (1 <= i <= len(mine))]
        targets = {id(mine[i - 1]) for i in valid}
        cancelled: List[Tuple[int, str]] = []  # (position, url)

        async with self._queue_cond:
            for lane_name in ("_urgent", "queue"):
                try:
                    lane = getattr(self, lane_name)
                    live = list(lane)
                    lane.clear()
                    for j in live:
                        if id(j) in targets:
                            try:
                                j.cancel_event.set()
                                j.status = "cancelled"
                                j.finished_at = time.time()
                            except Exception:
                                pass
                        else:
                            lane.append(j)
                except Exception:
                    _LOG.exception("indexed cancel lane rebuild failed: %s", lane_name)
            self._queue_cond.notify_all()

        # Titles for the reply (best-effort; snapshot objects are ours).
        for i in valid:
            try:
                j = mine[i - 1]
                if id(j) in targets:
                    _icon, _title = _pretty_title(getattr(j, "url", "") or "")
                    cancelled.append((i, f"{_icon} {_title}"))
            except Exception:
                pass

        # Fresh remainder, re-numbered from 1.
        try:
            urg2 = await self._snapshot_urgent()
        except Exception:
            urg2 = []
        try:
            items2 = await self._snapshot_queue()
        except Exception:
            items2 = []
        mine2 = [j for j in urg2 if getattr(j, "user_id", None) == uid]
        mine2 += [j for j in items2 if getattr(j, "user_id", None) == uid]

        lines: List[str] = []
        if cancelled:
            got = ", ".join(f"#{i} {t}" for i, t in cancelled)
            lines.append(f"🛑 cancelled {got}.")
        else:
            lines.append("ℹ️ nothing cancelled.")
        bad_all = out_of_range + bad
        if bad_all:
            lines.append(
                f"⚠️ ignored: {', '.join(bad_all)} "
                f"(you have <b>{len(mine)}</b> queued).")
        if mine2:
            lines.append(f"\n📋 your remaining queue (<b>{len(mine2)}</b>):")
            for k, j in enumerate(mine2, 1):
                try:
                    _icon2, _title2 = _pretty_title(getattr(j, "url", "") or "")
                    lines.append(f"<b>{k}.</b> {_icon2} {_title2}")
                except Exception:
                    pass
        else:
            lines.append("\n📋 your queue is now empty.")
        lines.append("\nTip: /queue shows live positions; bare /cancel stops everything of yours.")
        await _send_safe(context.bot, update.effective_chat.id, "\n".join(lines),
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
        if self._restarting:
            return
        self._restarting = True
        try:
            if self._bot is not None:
                await self._bot.send_message(
                    chat_id=chat_id,
                    text="🔄 <b>Restarting bot</b> -- stopping all jobs...",
                    parse_mode=ParseMode.HTML,
                )
        except Exception:
            pass
        try:
            from xdmovies.autodetect import stop_autodetect
            await stop_autodetect()
        except Exception:
            pass
        async with self._queue_cond:
            active = [self.current, *self.queue, *self._urgent]
            for job in active:
                if job is None:
                    continue
                job.cancel_event.set()
                job.status = "cancelled"
                job.finished_at = time.time()
            self.queue.clear()
            self._urgent.clear()
            self._active_scrape_children.clear()
            self._queue_cond.notify_all()
        await self._stop_dispatcher()
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
        try:
            on = await asyncio.to_thread(
                qmode_store.set_qmode, query.from_user.id, data == "qmode:set:yes"
            )
        except Exception as exc:
            await _send_safe(
                self._bot,
                update.effective_chat.id,
                f"[FAIL] could not save quality mode: {_html_escape(exc.__class__.__name__)}",
            )
            return
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
                _LOG.info("qmode picker skipped (silent/channel-routed job) for host %s", _url_host_label(job.url))
                return list(cards)
            if "/series/" not in (job.url or "").lower():
                _LOG.info("qmode picker skipped (not a series URL) for host %s", _url_host_label(job.url))
                return list(cards)
            if not qmode_store.is_qmode_on(job.user_id):
                _LOG.info("qmode picker skipped (qmode OFF for user %d) for host %s", job.user_id, _url_host_label(job.url))
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
            _LOG.info("qmode picker skipped (%d quality group) for host %s", len(groups), _url_host_label(job.url))
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
        try:
            on = await asyncio.to_thread(
                dmode_store.set_dmode, query.from_user.id, data == "dmode:set:yes"
            )
        except Exception as exc:
            await _send_safe(
                self._bot,
                update.effective_chat.id,
                f"[FAIL] could not save download mode: {_html_escape(exc.__class__.__name__)}",
            )
            return
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
                _LOG.info("dmode picker skipped (silent/channel-routed job) for host %s", _url_host_label(job.url))
                return list(cards)
            if "/series/" not in (job.url or "").lower():
                _LOG.info("dmode picker skipped (not a series URL) for host %s", _url_host_label(job.url))
                return list(cards)
            if not dmode_store.is_dmode_on(job.user_id):
                _LOG.info("dmode picker skipped (dmode OFF for user %d) for host %s", job.user_id, _url_host_label(job.url))
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
            _LOG.info("dmode picker skipped (%d section group) for host %s", len(groups), _url_host_label(job.url))
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
        try:
            if data == "settings:all":
                prefs = settings_store.get_settings(query.from_user.id)
                for key, _label, _emoji in settings_store.ALL_FIELDS:
                    prefs = await asyncio.to_thread(
                        settings_store.set_field, query.from_user.id, key, True
                    )
            elif data.startswith("settings:toggle:"):
                key = data.split(":", 2)[-1]
                prefs = await asyncio.to_thread(
                    settings_store.toggle_field, query.from_user.id, key
                )
            else:
                return
        except Exception as exc:
            await _send_safe(
                self._bot,
                update.effective_chat.id,
                f"[FAIL] could not save settings: {_html_escape(exc.__class__.__name__)}",
            )
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
        rows.append([InlineKeyboardButton("❌ Close", callback_data="autodetect:close")])  # type: ignore[misc]
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
        if cur in ("series", "all"):
            rows.append([InlineKeyboardButton("🎞️ Content filter", callback_data="autodetect:content:show")])  # type: ignore[misc]
        rows.append([InlineKeyboardButton("⬅️ Change feed", callback_data="autodetect:back")])  # type: ignore[misc]
        rows.append([InlineKeyboardButton("❌ Close", callback_data="autodetect:close")])  # type: ignore[misc]
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
        if cur in ("series", "all"):
            cf = _ad.get_card_filter()
            content_line = f"\nContent: <b>{_html_escape(_CONTENT_LABEL.get(cf, cf))}</b> (series posts)"
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
        # Base emojis must NEVER be a checkmark -- only the current
        # selection gets the ✅ prefix, otherwise "All" always looked
        # selected (its old base emoji was ✅).
        rows = []
        for key, cb, emoji in (
            ("all", "all", "📋"),
            ("episodes", "ep", "🎞️"),
            ("packs", "pack", "🗜️"),
        ):
            label = f"{emoji} {_CONTENT_LABEL[key]}"
            if key == cur:
                label = f"✅ {label}"
            rows.append([InlineKeyboardButton(label, callback_data=f"autodetect:content:{cb}")])  # type: ignore[misc]
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="autodetect:back")])  # type: ignore[misc]
        rows.append([InlineKeyboardButton("❌ Close", callback_data="autodetect:close")])  # type: ignore[misc]
        return InlineKeyboardMarkup(rows)  # type: ignore[misc]

    def _autodetect_content_text(self) -> str:
        from xdmovies import autodetect_store as _ad
        cf = _ad.get_card_filter()
        feed = _ad.get_feed()
        scope = "Series posts" if feed == "all" else "Series detected"
        return (
            f"{scope} -- what should auto-detect resolve?\n\n"
            f"Current: <b>{_html_escape(_CONTENT_LABEL.get(cf, cf))}</b>\n\n"
            "• All = episodes + packs/zips\n"
            "• Episodes only = single-episode cards\n"
            "• Packs/Zips only = season/full .zip packs"
            + ("\n(Movie posts always resolve fully.)" if feed == "all" else "")
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
        try:
            await asyncio.to_thread(_ad.set_feed, feed)
        except Exception as exc:
            await _send_safe(
                self._bot,
                update.effective_chat.id,
                f"[FAIL] could not save auto-detect feed: {_html_escape(exc.__class__.__name__)}",
            )
            return
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
        try:
            await asyncio.to_thread(_ad.set_on, want_on)
        except Exception as exc:
            await _send_safe(
                self._bot,
                update.effective_chat.id,
                f"[FAIL] could not save auto-detect state: {_html_escape(exc.__class__.__name__)}",
            )
            return
        if query.message is not None:
            # Series/All ON -> ask content filter first (All / Episodes /
            # Packs), same choice as the search accept-all flow. Movies
            # need no filter (every movie card resolves fully anyway).
            # The content callback lands back on the toggle screen.
            if want_on and _ad.get_feed() in ("series", "all"):
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
        try:
            await asyncio.to_thread(
                _ad.set_card_filter, _CONTENT_CALLBACK[parts[2]]
            )
        except Exception as exc:
            await _send_safe(
                self._bot,
                update.effective_chat.id,
                f"[FAIL] could not save auto-detect filter: {_html_escape(exc.__class__.__name__)}",
            )
            return
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

    async def _on_autodetect_close(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        if update.effective_user is None or not self.cfg.is_owner(query.from_user.id):
            return  # silent
        if (query.data or "") != "autodetect:close":
            return
        if query.message is not None:
            try:
                await query.message.delete()
            except Exception:
                pass

    async def _cmd_scrapeall(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
        if update.effective_user is None or update.effective_chat is None or update.message is None:
            return
        if not bot_config.is_allowed(update.effective_user.id, self.cfg):
            return
        token = _new_scrape_prompt(
            "scrapeall", update.effective_user.id, f"dm:{update.effective_user.id}", update.effective_chat.id, 0
        )
        keyboard = InlineKeyboardMarkup([  # type: ignore[misc]
            [InlineKeyboardButton("📚 All", callback_data=f"scrapeall:{token}:select:all")],  # type: ignore[misc]
            [
                InlineKeyboardButton("🎬 Movies", callback_data=f"scrapeall:{token}:select:movies"),  # type: ignore[misc]
                InlineKeyboardButton("🎞️ Series", callback_data=f"scrapeall:{token}:select:series"),  # type: ignore[misc]
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"scrapeall:{token}:cancel:-")],  # type: ignore[misc]
        ])
        sent = await update.message.reply_text("What do you want to scrape?", reply_markup=keyboard)
        _SCRAPE_PROMPTS[token]["message_id"] = int(getattr(sent, "message_id", 0) or 0)

    async def _on_scrapeall_choice(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        if not bot_config.is_allowed(query.from_user.id, self.cfg):
            return
        data = query.data or ""
        parts = data.split(":")
        if len(parts) != 4 or parts[0] != "scrapeall":
            return
        token = parts[1]
        entry = _scrape_prompt_entry(token, query)
        if entry is None or entry.get("prefix") != "scrapeall":
            return
        action, kind = parts[2], parts[3]
        if action == "cancel":
            _SCRAPE_PROMPTS.pop(token, None)
            if query.message is None:
                return
            await query.edit_message_text("❌ Scrape choice cancelled -- nothing started.")
            return
        if action not in {"select", "resume", "start"}:
            return
        if kind not in {"all", "movies", "series"}:
            return
        if action in {"resume", "start"}:
            _SCRAPE_PROMPTS.pop(token, None)
        route = str(entry["route"])
        from xdmovies.scrape_all import has_resumable_history, load_checkpoint, save_checkpoint
        state = load_checkpoint(kind, route=route)
        if action == "select" and has_resumable_history(state):
            label = {"all": "All", "movies": "Movies", "series": "Series"}[kind]
            keyboard = InlineKeyboardMarkup([[  # type: ignore[misc]
                InlineKeyboardButton("▶ Resume", callback_data=f"scrapeall:{token}:resume:{kind}"),  # type: ignore[misc]
                InlineKeyboardButton("↻ Start from beginning", callback_data=f"scrapeall:{token}:start:{kind}"),  # type: ignore[misc]
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
        _SCRAPE_PROMPTS.pop(token, None)

        from xdmovies.scrape_all import catalog_page_url
        if action == "start":
            from xdmovies.scrape_all import ScrapeCheckpoint
            state = ScrapeCheckpoint(kind=kind, route=route)
        was_finished = state.finished
        if was_finished and action == "resume":
            await query.edit_message_text(f"ℹ️ {kind.title()} has already finished; choose start again to restart it.")
            return
        if was_finished:
            state.next_page = 1
        _q_name, _q_uname = _display_name(query.from_user)
        job = Job(
            user_id=query.from_user.id,
            chat_id=query.from_user.id,
            url=catalog_page_url(kind, state.next_page),
            job_type="scrape_all",
            content_type=kind,
            scrape_route=route,
            user_name=_q_name,
            username=_q_uname,
        )
        position = await self._enqueue_scrape(
            job,
            prepare=(lambda: save_checkpoint(state)) if action == "start" else None,
        )
        if position == -1:
            await query.edit_message_text(f"⏳ A {kind} scrape is already active for this route.")
            return
        if position in (-2, -3):
            await query.edit_message_text("⚠️ queue is full; wait for a running job or cancel one with /cancel.")
            return
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
        token = _new_scrape_prompt(
            "scrapechan", update.effective_user.id, f"channel:{ch.channel_id}", update.effective_chat.id, ch.channel_id
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📚 All", callback_data=f"scrapechan:{token}:select:all")],
            [
                InlineKeyboardButton("🎬 Movies", callback_data=f"scrapechan:{token}:select:movies"),
                InlineKeyboardButton("🎞️ Series", callback_data=f"scrapechan:{token}:select:series"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"scrapechan:{token}:cancel:-")],
        ])
        shown = ch.title or str(ch.channel_id)
        sent = await update.message.reply_text(
            f"What do you want to scrape into <b>{_html_escape(shown)}</b>?",
            reply_markup=keyboard, parse_mode=ParseMode.HTML,
        )
        _SCRAPE_PROMPTS[token]["message_id"] = int(getattr(sent, "message_id", 0) or 0)

    async def _on_scrapechan_choice(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        if not self.cfg.is_owner(query.from_user.id):
            return  # owner only, silent ignore
        data = query.data or ""
        parts = data.split(":")
        if len(parts) != 4 or parts[0] != "scrapechan":
            return
        token = parts[1]
        entry = _scrape_prompt_entry(token, query)
        if entry is None or entry.get("prefix") != "scrapechan":
            return
        action, kind = parts[2], parts[3]
        if action == "cancel":
            _SCRAPE_PROMPTS.pop(token, None)
            if query.message is None:
                return
            await query.edit_message_text("❌ Channel scrape choice cancelled -- nothing started.")
            return
        if action not in {"select", "resume", "start"}:
            return
        if kind not in {"all", "movies", "series"}:
            return
        if action in {"resume", "start"}:
            _SCRAPE_PROMPTS.pop(token, None)
        route = str(entry["route"])
        try:
            from xdmovies.scrape_all import load_channel
            ch = load_channel()
        except Exception:
            ch = None
        if ch is None or int(getattr(ch, "channel_id", 0) or 0) != int(entry.get("channel_id", 0) or 0):
            await query.edit_message_text("⚠️ Channel target changed. Run /scrapeallchannel again.")
            return
        from xdmovies.scrape_all import has_resumable_history, load_checkpoint, save_checkpoint
        state = load_checkpoint(kind, route=route)
        if action == "select" and has_resumable_history(state):
            label = {"all": "All", "movies": "Movies", "series": "Series"}[kind]
            try:
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            except Exception:
                InlineKeyboardButton = InlineKeyboardMarkup = None  # type: ignore
            if InlineKeyboardMarkup is None:
                return
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("▶ Resume", callback_data=f"scrapechan:{token}:resume:{kind}"),
                InlineKeyboardButton("↻ Start from beginning", callback_data=f"scrapechan:{token}:start:{kind}"),
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
        from xdmovies.scrape_all import catalog_page_url
        if action == "start":
            from xdmovies.scrape_all import ScrapeCheckpoint
            state = ScrapeCheckpoint(kind=kind, route=route)
        was_finished = state.finished
        if was_finished and action == "resume":
            await query.edit_message_text(f"ℹ️ {kind.title()} has already finished; choose start again to restart it.")
            return
        if was_finished:
            state.next_page = 1
        _q_name, _q_uname = _display_name(query.from_user)
        job = Job(
            user_id=query.from_user.id,
            chat_id=query.from_user.id,
            url=catalog_page_url(kind, state.next_page),
            job_type="scrape_all",
            content_type=kind,
            scrape_route=route,
            user_name=_q_name,
            username=_q_uname,
            post_to_channel=ch.channel_id,
        )
        position = await self._enqueue_scrape(
            job,
            prepare=(lambda: save_checkpoint(state)) if action == "start" else None,
        )
        if position == -1:
            await query.edit_message_text(f"⏳ A {kind} scrape is already active for this route.")
            return
        if position in (-2, -3):
            await query.edit_message_text("⚠️ queue is full; wait for a running job or cancel one with /cancel.")
            return
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
        token = _new_search_session(uid, query, results, update.effective_chat.id)
        page, total_pages, keyboard = _search_page_keyboard(token, results, 1)
        sent = await update.message.reply_text(
            _search_page_text(query, results, page, total_pages),
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )
        _SEARCH_CACHE[uid] = (
            _SEARCH_CACHE[uid][0], query, results, token,
            int(getattr(sent, "message_id", 0) or 0), update.effective_chat.id,
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
        capped = list(results or [])[:_SEARCH_FETCH_LIMIT]
        skipped_duplicates = 0
        skipped_full = 0
        for res in capped:
            if res.url in seen_urls:
                skipped_duplicates += 1
                continue
            seen_urls.add(res.url)
            job = Job(user_id=uid, chat_id=chat_id, url=res.url,
                      user_name=user_name, username=username,
                      post_to_channel=to_channel, silent=bool(to_channel),
                      card_filter=card_filter)
            try:
                position = await self._enqueue(job, dedupe=True)
            except Exception:
                continue
            if position == -1:
                skipped_duplicates += 1
                continue
            if position in (-2, -3):
                skipped_full += 1
                continue
            jobs.append(job)
        skipped = max(0, len(results or []) - len(capped))
        lines = [f"📥 <b>Queued {len(jobs)} links</b> -- resolving one by one."]
        if card_filter != "all":
            lines[0] += f" ({_html_escape(_CONTENT_LABEL[card_filter])})"
        for idx, job in enumerate(jobs, 1):
            try:
                icon, title = _pretty_title(job.url)
            except Exception:
                icon, title = "🔗", job.url
            lines.append(f"{idx}. {icon} {title}")
        if skipped:
            lines.append(f"⚠️ +{skipped} results ignored (search cap {_SEARCH_FETCH_LIMIT}).")
        if skipped_duplicates:
            lines.append(f"⚠️ +{skipped_duplicates} duplicates skipped.")
        if skipped_full:
            lines.append(f"⚠️ +{skipped_full} not queued because the queue is full.")
        try:
            if self._bot is not None:
                await _send_safe(self._bot, chat_id, "\n".join(lines),
                                 parse_mode=ParseMode.HTML)
        except Exception:
            pass
        return len(jobs)

    async def _acceptall_confirm_timeout(self, uid: int, session_token: str, confirm_token: str) -> None:
        try:
            await asyncio.sleep(_SEARCH_CONFIRM_TIMEOUT)
            current = _SEARCH_CONFIRM.get(uid)
            entry = _SEARCH_CACHE.get(uid)
            if (
                current is None
                or entry is None
                or len(current) < 7
                or current[0] != confirm_token
                or current[6] != session_token
                or len(entry) < 5
                or entry[3] != session_token
            ):
                return
            _, chat_id, message_id, user_name, username, card_filter = current[:6]
            results = entry[2]
            _SEARCH_CONFIRM.pop(uid, None)
            _SEARCH_CACHE.pop(uid, None)
            if self._bot is None:
                return
            try:
                await self._bot.delete_message(chat_id=chat_id, message_id=message_id)
            except Exception:
                pass
            await self._acceptall_enqueue(
                uid, chat_id, results, user_name, username,
                to_channel=0, card_filter=card_filter,
            )
        except asyncio.CancelledError:
            return
        except Exception:
            return
        finally:
            task = _SEARCH_TIMEOUT_TASKS.get(uid)
            if task is asyncio.current_task():
                _SEARCH_TIMEOUT_TASKS.pop(uid, None)

    async def _on_search_pick(self, update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        if update.effective_user is None or update.effective_chat is None:
            return
        if not bot_config.is_allowed(query.from_user.id, self.cfg):
            return
        uid = query.from_user.id
        parts = (query.data or "").split(":")
        if len(parts) < 3 or parts[0] != "search":
            return
        token = parts[1]
        entry = _search_session(uid, token, query)
        if entry is None:
            return
        action = parts[2]
        if action == "close" and len(parts) == 3:
            _search_cancel(uid)
            if query.message is not None:
                try:
                    await query.message.delete()
                except Exception:
                    pass
            return
        results = entry[2]
        if action == "acceptall" and len(parts) == 3:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ All (EP+Pack)", callback_data=f"search:{token}:content:all")],
                [InlineKeyboardButton("🎞️ Episodes only", callback_data=f"search:{token}:content:ep")],
                [InlineKeyboardButton("🗜️ Packs/Zips only", callback_data=f"search:{token}:content:pack")],
                [InlineKeyboardButton("❌ Close", callback_data=f"search:{token}:close")],
            ])
            if query.message is not None:
                try:
                    await query.edit_message_text(
                        f"Scrape <b>{len(results)} links</b> -- what should I resolve?",
                        reply_markup=keyboard,
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
            return
        if action == "content" and len(parts) == 4 and parts[3] in _CONTENT_CALLBACK:
            card_filter = _CONTENT_CALLBACK[parts[3]]
            confirm_token = uuid.uuid4().hex[:8]
            _SEARCH_CONFIRM[uid] = (
                confirm_token,
                update.effective_chat.id,
                int(getattr(query.message, "message_id", 0) or 0),
                *_display_name(update.effective_user),
                card_filter,
                token,
            )
            old_task = _SEARCH_TIMEOUT_TASKS.pop(uid, None)
            if old_task is not None:
                old_task.cancel()
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Yes, to channel", callback_data=f"search:{token}:tochan:{confirm_token}:yes"),
                InlineKeyboardButton("❌ No, here in DM", callback_data=f"search:{token}:tochan:{confirm_token}:no"),
            ]])
            if query.message is not None:
                try:
                    await query.edit_message_text(
                        f"Scrape <b>{len(results)} links</b> "
                        f"({_html_escape(_CONTENT_LABEL[card_filter])}) to your channel?"
                        f" (No answer in {_search_confirm_human_timeout()} = No, scrape here.)",
                        reply_markup=keyboard,
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
            task = asyncio.create_task(
                self._acceptall_confirm_timeout(uid, token, confirm_token)
            )
            _SEARCH_TIMEOUT_TASKS[uid] = task
            return
        if action == "tochan" and len(parts) == 5 and parts[4] in ("yes", "no"):
            current = _SEARCH_CONFIRM.get(uid)
            if current is None or current[0] != parts[3] or current[6] != token:
                return
            filter_value = current[5]
            chat_id = current[1]
            to_channel = 0
            channel_note = ""
            if parts[4] == "yes":
                try:
                    from xdmovies.scrape_all import load_channel
                    channel = load_channel()
                except Exception:
                    channel = None
                if channel is None or not int(getattr(channel, "channel_id", 0) or 0):
                    _search_cancel(uid)
                    if query.message is not None:
                        try:
                            await query.edit_message_text(
                                "No channel set -- send /scrapeallchannel and forward "
                                "a channel message first, then accept again."
                            )
                        except Exception:
                            pass
                    return
                to_channel = int(channel.channel_id)
                channel_note = f" → <b>{_html_escape(getattr(channel, 'title', '') or str(to_channel))}</b>"
            _search_cancel(uid)
            if query.message is not None:
                try:
                    await query.message.delete()
                except Exception:
                    pass
            name, username = current[3], current[4]
            count = await self._acceptall_enqueue(
                uid, chat_id, results, name, username,
                to_channel=to_channel, card_filter=filter_value,
            )
            if to_channel and count:
                await _send_safe(
                    self._bot, chat_id,
                    f"📢 Scraping to channel{channel_note}. Progress lands there; this chat stays quiet.",
                    parse_mode=ParseMode.HTML,
                )
            return
        if action not in {"pick", "page"} or len(parts) != 4:
            return
        try:
            index = int(parts[3])
        except ValueError:
            return
        if action == "page":
            page, total_pages, keyboard = _search_page_keyboard(token, results, index)
            if query.message is not None:
                try:
                    await query.edit_message_text(
                        _search_page_text(entry[1], results, page, total_pages),
                        reply_markup=keyboard,
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
            return
        if index < 0 or index >= len(results):
            return
        picked = results[index]
        urgent = self._is_scrape_running()
        name, username = _display_name(update.effective_user)
        job = Job(
            user_id=uid,
            chat_id=update.effective_chat.id,
            url=picked.url,
            user_name=name,
            username=username,
            priority=1 if urgent else 0,
        )
        _search_cancel(uid)
        if query.message is not None:
            try:
                await query.message.delete()
            except Exception:
                pass
        position = await self._enqueue(job, urgent=urgent, dedupe=True)
        if position == -1:
            await _send_safe(self._bot, update.effective_chat.id, "[FAIL] that link is already active.")
            return
        if position in (-2, -3):
            await _send_safe(
                self._bot,
                update.effective_chat.id,
                "[FAIL] queue is full; wait for a running job or cancel one with /cancel.",
            )
            return
        if urgent:
            job.enqueue_msg_id = await _send_safe(
                self._bot, update.effective_chat.id,
                "⚡ <b>Urgent queued</b> — runs after the current movie finishes 10/10, "
                "then the channel scrape resumes.",
                parse_mode=ParseMode.HTML,
            )
        else:
            job.enqueue_msg_id = await self._confirm_enqueued(
                context, update.effective_chat.id, picked.url, position
            )

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
            from xdmovies.search import search_titles_batch
        except Exception as exc:
            _LOG.warning("search import failed: %r", exc)
            await _send_safe(
                context.bot, update.effective_chat.id,
                "[FAIL] search is unavailable right now -- try again later.",
                parse_mode=ParseMode.HTML,
            )
            return
        cancel_event = asyncio.Event()
        _TXT_SEARCH_CANCEL[uid] = cancel_event
        progress_message = None
        try:
            progress_message = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"🔎 Searching {len(titles)} titles…",
            )

            async def progress(done: int, total: int) -> None:
                if progress_message is None:
                    return
                try:
                    await context.bot.edit_message_text(
                        chat_id=update.effective_chat.id,
                        message_id=progress_message.message_id,
                        text=f"🔎 Searching titles: {done}/{total}…",
                    )
                except Exception:
                    pass

            batch = await search_titles_batch(
                titles,
                per_title_limit=5,
                deadline=_TXT_SEARCH_DEADLINE,
                max_concurrency=_TXT_SEARCH_CONCURRENCY,
                result_cap=_SEARCH_FETCH_LIMIT,
                progress=progress,
                cancel_event=cancel_event,
            )
            flat = list(batch.results or [])
            no_match = list(batch.no_match or [])
            failed_titles = list(batch.failed or [])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOG.warning("txt batch search failed: %r", exc)
            await _send_safe(
                context.bot, update.effective_chat.id,
                "[FAIL] search is unavailable right now -- try again later.",
                parse_mode=ParseMode.HTML,
            )
            return
        finally:
            _TXT_SEARCH_CANCEL.pop(uid, None)
            if progress_message is not None:
                try:
                    await context.bot.delete_message(
                        chat_id=update.effective_chat.id,
                        message_id=progress_message.message_id,
                    )
                except Exception:
                    pass
        if batch.cancelled:
            await _send_safe(
                context.bot,
                update.effective_chat.id,
                "🛑 .txt search cancelled.",
                parse_mode=ParseMode.HTML,
            )
            return
        if batch.timed_out:
            await _send_safe(
                context.bot,
                update.effective_chat.id,
                f"⏱ Search deadline reached after {int(_TXT_SEARCH_DEADLINE)}s; showing partial results.",
                parse_mode=ParseMode.HTML,
            )
        if not flat:
            if failed_titles:
                msg = "[FAIL] search was unavailable for every title -- try again later."
            else:
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
        token = _new_search_session(uid, label, flat, update.effective_chat.id)
        page, total_pages, keyboard = _search_page_keyboard(token, flat, 1)
        msg_text = _search_page_text(label, flat, page, total_pages)
        # NOTE: suffix lines below show only on page 1 (page turns
        # re-render via _search_page_text). Accepted: keeps nav simple.
        suffix = ""
        if no_match:
            shown = ", ".join(no_match[:5])
            more = f" +{len(no_match) - 5} more" if len(no_match) > 5 else ""
            suffix += f"\n❌ Not found: {shown}{more}"
        if failed_titles:
            shown = ", ".join(failed_titles[:5])
            more = f" +{len(failed_titles) - 5} more" if len(failed_titles) > 5 else ""
            suffix += f"\n⚠️ Search failed: {shown}{more}"
        if dropped:
            suffix += f"\n⚠️ +{dropped} lines ignored (max {_TXT_MAX_LINES})."
        sent = await update.message.reply_text(
            msg_text + suffix,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )
        _SEARCH_CACHE[uid] = (
            _SEARCH_CACHE[uid][0], label, flat, token,
            int(getattr(sent, "message_id", 0) or 0), update.effective_chat.id,
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
                    position = await self._enqueue(job, urgent=True, dedupe=True)
                    if position < 0:
                        continue
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
                token = _new_search_session(uid, raw_title, results, chat_id)
                page, total_pages, keyboard = _search_page_keyboard(token, results, 1)
                text = (
                    f"⚠️ No exact match for <code>{_html_escape(raw_title)}</code> — "
                    f"tap the right one below (wrong auto-pick disabled).\n"
                    + _search_page_text(raw_title, results, page, total_pages)
                )
                sent = await _send_markup_safe(self._bot, chat_id, text, keyboard)
                if sent is not None:
                    _SEARCH_CACHE[uid] = (
                        _SEARCH_CACHE[uid][0], raw_title, results, token, sent, chat_id,
                    )
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
            pos = await self._enqueue(job, urgent=urgent, dedupe=True)
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
                if pos in (-2, -3):
                    await _send_safe(
                        context.bot,
                        update.effective_chat.id,
                        "[FAIL] queue is full; wait for a running job or cancel one with /cancel.",
                    )
                    return
                job.enqueue_msg_id = await self._confirm_enqueued(context, update.effective_chat.id, urls[0], pos)
                return
            jobs = []
            for url in urls:
                job = Job(user_id=uid, chat_id=update.effective_chat.id, url=url,
                          user_name=_nm, username=_un)
                pos = await self._enqueue(job)
                if pos in (-2, -3):
                    continue
                jobs.append(job)
            if not jobs:
                await _send_safe(
                    context.bot,
                    update.effective_chat.id,
                    "[FAIL] queue is full; wait for a running job or cancel one with /cancel.",
                )
                return
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
        _LOG.info("bot initialized")
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
                BotCommand("cancel", "cancel queued job(s) by /queue number, or all"),
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

    async def _stop_dispatcher(self) -> None:
        task = self._dispatcher_task
        self._dispatcher_task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            _LOG.exception("dispatcher shutdown failed")

    async def _post_stop(self, _application: "Application") -> None:
        await self._stop_dispatcher()
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

    def _stop_after_reported_solve_failure(self, job: Job, error: str) -> None:
        job.status = "cancelled" if job.cancel_event.is_set() else "error"
        job.error = error
        job.finished_at = time.time()

    async def _send_solve_link(self, chat_id: int, url: str, host: str, user_id: int):
        token = None
        try:
            from xdmovies.solve_server import SolveBusyError, create_session, get_public_url
            token = await create_session(url, user_id, host, chat_id)
            public_url = get_public_url(token)
            msg = await self._bot.send_message(chat_id=chat_id,
                text=(f"🔒 <b>Cloudflare blocked</b> -- <code>{_html_escape(host)}</code>\n\n"
                      f"Link: <code>{_html_escape(url)}</code>\n\n"
                      f"👉 <b>Tap to solve (auto-captures, no paste):</b>\n{_html_escape(public_url)}\n\n"
                      "Open it, tap checkbox on screenshot if you see it. I’ll auto-capture and delete this message to hide VPS IP."),
                parse_mode=ParseMode.HTML)
            try:
                message_id = int(msg.message_id)
            except (AttributeError, TypeError, ValueError):
                message_id = None
            return token, message_id
        except SolveBusyError:
            sent = await _send_safe(
                self._bot,
                chat_id,
                "⏳ <b>Solve server busy</b> — all solve slots are active. Wait for the current solve to finish, then retry.",
                parse_mode=ParseMode.HTML,
            )
            return None, int(getattr(sent, "message_id", sent)) if sent is not None else None
        except asyncio.CancelledError:
            if token:
                try:
                    from xdmovies.solve_server import destroy_session
                    await destroy_session(token)
                except Exception:
                    pass
            raise
        except Exception as exc:
            if token:
                try:
                    from xdmovies.solve_server import destroy_session
                    await destroy_session(token)
                except Exception:
                    pass
            _LOG.warning(f"solve link failed: {type(exc).__name__}")
            return None, None

    async def _wait_for_solve(
        self,
        token: str,
        timeout: float = 120.0,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> bool:
        from xdmovies.solve_server import wait_for_solve
        solve_task = asyncio.create_task(wait_for_solve(token, timeout=timeout))
        cancel_task = asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
        cancelled = False
        try:
            if cancel_task is None:
                return bool(await solve_task)
            done, _pending = await asyncio.wait(
                {solve_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            cancelled = cancel_task in done and not solve_task.done()
            if cancelled:
                solve_task.cancel()
            try:
                return bool(await solve_task)
            except asyncio.CancelledError:
                if cancelled:
                    return False
                raise
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        finally:
            tasks = [solve_task] + ([cancel_task] if cancel_task is not None else [])
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _wait_for_solve_message(
        self,
        job: Job,
        chat_id: int,
        token: str,
        message_id: Optional[int],
        timeout: float,
        note: str,
    ) -> bool:
        wait_message_id = None
        try:
            wait_message_id = await _send_transient_note(self._bot, chat_id, note)
            solved = await self._wait_for_solve(token, timeout=timeout, cancel_event=job.cancel_event)
        except BaseException:
            from xdmovies.solve_server import destroy_session
            await destroy_session(token)
            raise
        finally:
            await _drop_msg(self._bot, chat_id, wait_message_id)
            await _drop_msg(self._bot, chat_id, message_id)
        if job.cancel_event.is_set():
            job.status = "cancelled"
            job.finished_at = time.time()
        return solved

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
            pattern=r"^search:[0-9a-f]{8}:(pick|page):\d+$|^search:[0-9a-f]{8}:(close|acceptall)$|^search:[0-9a-f]{8}:content:(all|ep|pack)$|^search:[0-9a-f]{8}:tochan:[0-9a-f]{8}:(yes|no)$",
        ))
        app.add_handler(CallbackQueryHandler(
            self._on_scrapeall_choice,
            pattern=r"^scrapeall:[0-9a-f]{8}:(select|resume|start):(all|movies|series)$|^scrapeall:[0-9a-f]{8}:cancel:-$",
        ))
        app.add_handler(CallbackQueryHandler(
            self._on_scrapechan_choice,
            pattern=r"^scrapechan:[0-9a-f]{8}:(select|resume|start):(all|movies|series)$|^scrapechan:[0-9a-f]{8}:cancel:-$",
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
        app.add_handler(CallbackQueryHandler(
            self._on_autodetect_close,
            pattern=r"^autodetect:close$",
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
    if _LOCK is None:
        print(
            f"another xdmovies-bot instance is already running "
            f"(lock {_LOCK_PATH} held); refusing to start a second poller. "
            f"Stop it first (systemctl stop xdmovies-bot) or run "
            f"bash xdmovies_kill_all.sh",
            file=sys.stderr,
        )
        raise SystemExit(2)
    cfg = bot_config.load_config()
    bot_config.configure_logging(cfg)
    from xdmovies.cloak_stealth import configure_session_limit
    configure_session_limit(cfg.max_sessions)
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
        if _LOCK is not None:
            _LOCK.close()

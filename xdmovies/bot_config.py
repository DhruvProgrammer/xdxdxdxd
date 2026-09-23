"""Bot config: env parsing, owner/admin access control, logging setup.

Pulled out so the bot module itself stays focused on handlers.
Reads from:
    - process-environment (preferred)
    - xdmovies/.env (fallback) — loaded with the lightest possible parser.

Required:
    TELEGRAM_BOT_TOKEN     — issued by @BotFather
    TELEGRAM_OWNER_ID      — int, runs first if multiple users submit
                             in the same instant

Optional but supported:
    TELEGRAM_ADMIN_IDS     — comma/space-separated list of additional
                             user ids that may also use the bot. May be
                             empty or unset.

    Legacy aliases (still work):
        TELEGRAM_ADMIN_ID  — single-int fallback if TELEGRAM_ADMIN_IDS
                             is empty.

Anything else is silently ignored.
"""
from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, List, Optional, Tuple, Union
from urllib.parse import urlsplit


_LOG = logging.getLogger("xdmovies.bot.config")

_TELEGRAM_TOKEN_URL = re.compile(r"(/bot)[^/\s]+(/)")
_SOLVE_TOKEN_URL = re.compile(r"(/solve/)[A-Za-z0-9_-]{16,}(?=([/?#'\"]|$))")
_PROXY_USERINFO = re.compile(r"((?:https?|socks5h?|socks4)://)[^/\s@]+@", re.IGNORECASE)
_TRUE_VALUES = {"1", "true", "yes", "on"}


def redact_sensitive(value: object) -> str:
    message = str(value)
    message = _TELEGRAM_TOKEN_URL.sub(r"\1<redacted>\2", message)
    message = _SOLVE_TOKEN_URL.sub(r"\1<redacted>", message)
    return _PROXY_USERINFO.sub(r"\1<redacted>@", message)


class _RedactSensitive(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = redact_sensitive(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def validate_solve_public_base(value: Optional[str] = None) -> str:
    configured = os.environ.get("SOLVE_SERVER_PUBLIC_BASE", "") if value is None else value
    raw = (configured or "").strip()
    allow_insecure = os.environ.get("SOLVE_SERVER_ALLOW_INSECURE_HTTP", "").strip().lower() in _TRUE_VALUES
    if not raw:
        raise RuntimeError("missing required env var SOLVE_SERVER_PUBLIC_BASE")
    if configured != raw or any(ch.isspace() or ord(ch) < 32 for ch in raw):
        raise RuntimeError("SOLVE_SERVER_PUBLIC_BASE contains invalid whitespace or control characters")
    if any(ch in raw for ch in "\\?#<>\"'"):
        raise RuntimeError("SOLVE_SERVER_PUBLIC_BASE contains invalid URL characters")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError(f"SOLVE_SERVER_PUBLIC_BASE is malformed: {exc}") from exc
    if parsed.scheme != "https" and not (parsed.scheme == "http" and allow_insecure):
        raise RuntimeError("SOLVE_SERVER_PUBLIC_BASE must use HTTPS")
    if not parsed.hostname:
        raise RuntimeError("SOLVE_SERVER_PUBLIC_BASE must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise RuntimeError("SOLVE_SERVER_PUBLIC_BASE must not include credentials")
    if parsed.query or parsed.fragment:
        raise RuntimeError("SOLVE_SERVER_PUBLIC_BASE must not include a query or fragment")
    if port is not None and not (1 <= port <= 65535):
        raise RuntimeError("SOLVE_SERVER_PUBLIC_BASE has an invalid port")
    return raw.rstrip("/")


def ensure_private_directory(path: Union[str, os.PathLike]) -> None:
    target = os.fspath(path)
    os.makedirs(target, exist_ok=True)
    if os.name == "posix":
        os.chmod(target, 0o700)


def _ensure_private_parent_directory(path: Union[str, os.PathLike]) -> None:
    target = os.fspath(path)
    try:
        os.makedirs(target, mode=0o700)
    except FileExistsError:
        pass


def ensure_private_file(path: Union[str, os.PathLike]) -> None:
    if os.name == "posix" and os.path.exists(path):
        os.chmod(path, 0o600)


def open_private_text(
    path: Union[str, os.PathLike],
    mode: str = "r",
    *,
    encoding: str = "utf-8",
    errors: Optional[str] = None,
) -> IO[str]:
    target = os.fspath(path)
    if any(flag in mode for flag in ("w", "a", "x", "+")):
        ensure_private_directory(Path(target).parent)
    flags = os.O_RDONLY
    if "+" in mode:
        flags = os.O_RDWR
    if "w" in mode:
        flags |= os.O_CREAT | os.O_TRUNC
    elif "a" in mode:
        flags |= os.O_CREAT | os.O_APPEND
    elif "x" in mode:
        flags |= os.O_CREAT | os.O_EXCL
    fd = os.open(target, flags, 0o600)
    ensure_private_file(target)
    kwargs = {"encoding": encoding}
    if errors is not None:
        kwargs["errors"] = errors
    return os.fdopen(fd, mode, **kwargs)


@contextmanager
def _state_file_lock(path: Union[str, os.PathLike]):
    target = os.path.abspath(os.fspath(path))
    lock_path = target + ".lock"
    ensure_private_directory(Path(lock_path).parent)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            if os.fstat(fd).st_size < 1:
                os.write(fd, b" ")
                os.ftruncate(fd, 1)
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
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
            os.close(fd)


def _read_json_unlocked(path: str, default):
    if not os.path.isfile(path):
        return default
    try:
        ensure_private_file(path)
        with open_private_text(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, TypeError):
        return default


def _write_json_unlocked(path: str, payload, *, sort_keys: bool = False, indent: int = 2) -> None:
    target = os.path.abspath(os.fspath(path))
    ensure_private_directory(Path(target).parent)
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(target)}.", suffix=".tmp", dir=os.path.dirname(target))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=indent, sort_keys=sort_keys)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        if os.name == "posix":
            try:
                dir_fd = os.open(os.path.dirname(target), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
    finally:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass
        except OSError:
            pass


def atomic_write_json(
    path: Union[str, os.PathLike],
    payload,
    *,
    sort_keys: bool = False,
    indent: int = 2,
) -> None:
    target = os.path.abspath(os.fspath(path))
    with _state_file_lock(target):
        _write_json_unlocked(target, payload, sort_keys=sort_keys, indent=indent)


def update_json(
    path: Union[str, os.PathLike],
    update,
    default,
    *,
    sort_keys: bool = False,
    indent: int = 2,
):
    target = os.path.abspath(os.fspath(path))
    with _state_file_lock(target):
        data = _read_json_unlocked(target, default)
        result = update(data)
        payload = data if result is None else result
        _write_json_unlocked(target, payload, sort_keys=sort_keys, indent=indent)
        return result


def read_json(path: Union[str, os.PathLike], default=None):
    return _read_json_unlocked(os.path.abspath(os.fspath(path)), default)


def _prepare_private_file(path: str) -> None:
    _ensure_private_parent_directory(Path(path).parent)
    fd = os.open(path, os.O_CREAT | os.O_APPEND, 0o600)
    os.close(fd)
    ensure_private_file(path)


def _load_dotenv(path: str = ".env", override: bool = False) -> None:
    """Tiny .env parser — only fills env vars that aren't already set
    (unless override=True). Lines like ``KEY=value``; ``#`` comments and
    blank lines ignored. Quotes around value are stripped.
    """
    try:
        if not os.path.isfile(path):
            return
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if not k:
                    continue
                if override or k not in os.environ:
                    os.environ[k] = v
    except OSError as exc:
        _LOG.warning("could not read %s: %s", path, exc)


def _parse_single_int(name: str, raw: str) -> int:
    raw = raw.strip()
    if not raw:
        raise RuntimeError(f"empty value for env var {name}")
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"env var {name} must contain integer(s) only, got {raw!r}"
        ) from exc


def _parse_int_list(name: str, raw: str) -> List[int]:
    """Parse a single env var that may contain one or more ints.

    Accepted separators: comma, semicolon, whitespace. Any number of
    those — duplicates are deduped while preserving order.
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    ids: List[int] = []
    seen: set = set()
    for piece in re.split(r"[,\s;]+", raw):
        token = piece.strip()
        if not token:
            continue
        try:
            v = int(token)
        except ValueError as exc:
            raise RuntimeError(
                f"env var {name} contains non-integer token {token!r}"
            ) from exc
        if v not in seen:
            seen.add(v)
            ids.append(v)
    return ids


@dataclass(frozen=True)
class BotConfig:
    token: str
    owner_id: int
    admin_ids: Tuple[int, ...] = field(default_factory=tuple)
    log_path: str = "xdmovies_bot.log"
    error_log_path: str = "xdmovies_bot_errors.log"
    max_sessions: int = 8

    @property
    def allowed_ids(self) -> Tuple[int, ...]:
        return (self.owner_id, *self.admin_ids)

    def is_owner(self, user_id: int) -> bool:
        return user_id == self.owner_id


def load_config() -> BotConfig:
    """Load .env (if present) and assemble the immutable config object.

    Lookup order:
        1. .env next to this file (the canonical place)
        2. .env in the current working directory (developer convenience)
    """
    here = os.path.dirname(os.path.abspath(__file__))
    _load_dotenv(os.path.join(here, ".env"))
    _load_dotenv(os.path.join(os.path.dirname(here), ".env"))
    _load_dotenv(".env")  # cwd fallback

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("missing required env var TELEGRAM_BOT_TOKEN")

    owner_raw = os.environ.get("TELEGRAM_OWNER_ID", "").strip()
    if not owner_raw:
        raise RuntimeError(
            "missing required env var TELEGRAM_OWNER_ID. "
            "Add it to your .env or export it before starting the bot."
        )
    owner = _parse_single_int("TELEGRAM_OWNER_ID", owner_raw)

    # Optional multi-admin: try TELEGRAM_ADMIN_IDS first (list), then
    # fall back to TELEGRAM_ADMIN_ID for the single-int legacy case.
    admin_ids = _parse_int_list(
        "TELEGRAM_ADMIN_IDS",
        os.environ.get("TELEGRAM_ADMIN_IDS", "").strip(),
    )
    if not admin_ids:
        legacy = os.environ.get("TELEGRAM_ADMIN_ID", "").strip()
        if legacy:
            admin_ids = _parse_int_list("TELEGRAM_ADMIN_ID", legacy)

    # The owner is implicitly allowed; never include them in admin_ids
    # so they don't double-book a priority slot.
    admin_ids = tuple(a for a in admin_ids if a != owner)

    log_path = (
        os.environ.get("XDMOVIES_BOT_LOG", "xdmovies_bot.log").strip()
        or "xdmovies_bot.log"
    )
    error_log_path = (
        os.environ.get("XDMOVIES_BOT_ERROR_LOG", "xdmovies_bot_errors.log").strip()
        or "xdmovies_bot_errors.log"
    )

    solve_public_base = validate_solve_public_base()
    os.environ["SOLVE_SERVER_PUBLIC_BASE"] = solve_public_base

    # Optional: cap concurrent BrowserSessions. Default 8 — enough for one
    # busy job + 7 queued without OOM'ing a 4GB VPS.
    try:
        max_sessions = int(
            os.environ.get("XDMOVIES_MAX_SESSIONS", "8").strip() or "8"
        )
    except ValueError:
        max_sessions = 8
    max_sessions = max(1, max_sessions)

    return BotConfig(
        token=token,
        owner_id=owner,
        admin_ids=admin_ids,
        log_path=log_path,
        error_log_path=error_log_path,
        max_sessions=max_sessions,
    )


def is_allowed(user_id: int, cfg: BotConfig) -> bool:
    return user_id in cfg.allowed_ids


def configure_logging(cfg: BotConfig) -> None:
    """Two log files: a normal log + an errors-only log that the bot
    message points at when /status reports issues."""
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    redact = _RedactSensitive()

    for path in (cfg.log_path, cfg.error_log_path):
        _prepare_private_file(path)

    fh = RotatingFileHandler(
        cfg.log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setLevel(logging.INFO)
    fh.setFormatter(fmt)
    fh.addFilter(redact)
    root.addHandler(fh)

    eh = RotatingFileHandler(
        cfg.error_log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    eh.setLevel(logging.ERROR)
    eh.setFormatter(fmt)
    eh.addFilter(redact)
    root.addHandler(eh)

    sh = logging.StreamHandler(stream=sys.stderr)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    sh.addFilter(redact)
    root.addHandler(sh)

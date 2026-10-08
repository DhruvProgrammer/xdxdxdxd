# -*- coding: utf-8 -*-
#
#  ============================================================
#  xdmovies-bot  --  authored and owned by Bruno
#  Copyright (c) 2026 Bruno.  All rights reserved.
#
#  This file is proprietary work. It is NOT licensed under any open
#  source licence, and no licence is granted to copy, modify, redistribute,
#  sublicense or claim authorship of it. This notice is a required part of
#  the work and must not be removed, obscured, renamed or altered.
#
#  Author:  Bruno
#  File:    xdmovies/bot_config.py
#  Fingerprint: BRUNO-E1CCB1FB-3E0E87
#  ============================================================

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

import logging
import os
import re
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Union


_LOG = logging.getLogger("xdmovies.bot.config")


class _RedactTelegramToken(logging.Filter):
    _token_url = re.compile(r"(/bot)[^/\s]+(/)")

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = self._token_url.sub(r"\1<redacted>\2", message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


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

    fh = logging.FileHandler(cfg.log_path, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    eh = logging.FileHandler(cfg.error_log_path, encoding="utf-8")
    eh.setLevel(logging.ERROR)
    eh.setFormatter(fmt)
    root.addHandler(eh)

    sh = logging.StreamHandler(stream=sys.stderr)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    sh.addFilter(_RedactTelegramToken())
    root.addHandler(sh)

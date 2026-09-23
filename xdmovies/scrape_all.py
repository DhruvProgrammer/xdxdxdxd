"""Catalog parsing and durable progress for the /scrapeall command."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

from xdmovies.bot_config import atomic_write_json, ensure_private_directory, ensure_private_file


CATALOG_URLS = {
    "all": "https://top.xdmovies.wtf/",
    "movies": "https://top.xdmovies.wtf/?type=movies",
    "series": "https://top.xdmovies.wtf/?type=series",
}
STATE_DIR = Path(__file__).resolve().parent.parent / "xdmovies_state"


def catalog_page_url(kind: str, page: int) -> str:
    base = CATALOG_URLS[kind]
    if page <= 1:
        return base
    if kind == "all":
        return f"https://top.xdmovies.wtf/?page={page}"
    return f"{base}&page={page}"


def checkpoint_path(kind: str, route: str = "default") -> Path:
    if route in ("", "default"):
        return STATE_DIR / f"scrapeall_{kind}.json"
    route_id = hashlib.sha256(route.encode("utf-8")).hexdigest()[:16]
    return STATE_DIR / f"scrapeall_{kind}_{route_id}.json"


@dataclass
class ScrapeCheckpoint:
    kind: str
    route: str = "default"
    next_page: int = 1
    completed_urls: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    pages_completed: int = 0
    finished: bool = False
    updated_at: float = field(default_factory=time.time)
    schema_version: int = 1


def load_checkpoint(kind: str, route: str = "default") -> ScrapeCheckpoint:
    ensure_private_directory(STATE_DIR)
    path = checkpoint_path(kind, route)
    if not path.exists():
        return ScrapeCheckpoint(kind=kind, route=route)
    try:
        ensure_private_file(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema_version") != 1 or data.get("kind") != kind:
            raise ValueError("incompatible scrape checkpoint")
        return ScrapeCheckpoint(**data)
    except Exception as exc:
        raise RuntimeError(f"cannot read checkpoint {path}: {exc}") from exc


def save_checkpoint(state: ScrapeCheckpoint) -> None:
    ensure_private_directory(STATE_DIR)
    state.updated_at = time.time()
    path = checkpoint_path(state.kind, state.route)
    atomic_write_json(path, asdict(state), sort_keys=True)


def has_resumable_history(state: ScrapeCheckpoint) -> bool:
    return not state.finished and bool(
        state.pages_completed
        or state.completed_urls
        or state.failed
        or state.next_page > 1
    )


@dataclass
class ScrapeChannel:
    """Where /scrapeallchannel posts. Single global target, set by
    forwarding any channel message to the bot (owner only)."""
    channel_id: int = 0
    title: str = ""
    set_by: int = 0
    updated_at: float = field(default_factory=time.time)
    schema_version: int = 1


def channel_path() -> Path:
    return STATE_DIR / "scrape_channel.json"


def load_channel() -> Optional[ScrapeChannel]:
    ensure_private_directory(STATE_DIR)
    path = channel_path()
    if not path.exists():
        return None
    try:
        ensure_private_file(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema_version") != 1 or not int(data.get("channel_id") or 0):
            raise ValueError("incompatible scrape channel file")
        return ScrapeChannel(**data)
    except Exception:
        return None


def save_channel(cfg: ScrapeChannel) -> None:
    ensure_private_directory(STATE_DIR)
    cfg.updated_at = time.time()
    path = channel_path()
    atomic_write_json(path, asdict(cfg), sort_keys=True)


def _canonical_detail_url(href: str, base_url: str, kind: str) -> str | None:
    absolute = urljoin(base_url, href)
    parsed = urlsplit(absolute)
    if parsed.hostname not in {"xdmovies.wtf", "top.xdmovies.wtf"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[0].lower() not in {"movies", "series"} or not parts[1]:
        return None
    content_kind = parts[0].lower()
    if kind != "all" and content_kind != kind:
        return None
    return urlunsplit(("https", "top.xdmovies.wtf", f"/{content_kind}/{parts[1]}", "", ""))


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: Iterable[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self.hrefs.append(href)


def extract_catalog_urls(html: str, kind: str, base_url: str) -> list[str]:
    if kind not in CATALOG_URLS:
        raise ValueError(f"unsupported catalog kind: {kind}")
    parser = _LinkParser()
    parser.feed(html or "")
    seen: set[str] = set()
    result: list[str] = []
    for href in parser.hrefs:
        url = _canonical_detail_url(href, base_url, kind)
        if url and url not in seen:
            seen.add(url)
            result.append(url)
    return result


def extract_pagination_info(html: str) -> tuple[int, int] | None:
    block = re.search(
        r"PAGINATION_INFO\s*=\s*\{([^}]*)\}",
        html or "",
        re.IGNORECASE,
    )
    if not block:
        return None
    current = re.search(r"\bcurrentPage\s*:\s*(\d+)", block.group(1), re.IGNORECASE)
    total = re.search(r"\btotalPages\s*:\s*(\d+)", block.group(1), re.IGNORECASE)
    if not current or not total:
        return None
    current_page = int(current.group(1))
    total_pages = int(total.group(1))
    if current_page < 1 or total_pages < current_page:
        return None
    return current_page, total_pages

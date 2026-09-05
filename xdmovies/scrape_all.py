"""Catalog parsing and durable progress for the /scrapeall command."""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit


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


def checkpoint_path(kind: str) -> Path:
    return STATE_DIR / f"scrapeall_{kind}.json"


@dataclass
class ScrapeCheckpoint:
    kind: str
    next_page: int = 1
    completed_urls: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    pages_completed: int = 0
    finished: bool = False
    updated_at: float = field(default_factory=time.time)
    schema_version: int = 1


def load_checkpoint(kind: str) -> ScrapeCheckpoint:
    path = checkpoint_path(kind)
    if not path.exists():
        return ScrapeCheckpoint(kind=kind)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema_version") != 1 or data.get("kind") != kind:
            raise ValueError("incompatible scrape checkpoint")
        return ScrapeCheckpoint(**data)
    except Exception as exc:
        raise RuntimeError(f"cannot read checkpoint {path}: {exc}") from exc


def save_checkpoint(state: ScrapeCheckpoint) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state.updated_at = time.time()
    path = checkpoint_path(state.kind)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(asdict(state), handle, ensure_ascii=True, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def has_resumable_history(state: ScrapeCheckpoint) -> bool:
    return not state.finished and bool(
        state.pages_completed
        or state.completed_urls
        or state.failed
        or state.next_page > 1
    )


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
    match = re.search(
        r"PAGINATION_INFO\s*=\s*\{[^}]*currentPage\s*:\s*(\d+)[^}]*totalPages\s*:\s*(\d+)",
        html or "",
        re.IGNORECASE,
    )
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))

"""Single source of truth for extracting per-card metadata from an xdmovies page.

A page wraps each download URL inside a card whose metadata (title, size,
language, file name) is rendered as text in adjacent elements. Older
versions of the pipeline only captured the title/codec string. The bot
needs the full card to format per-link Telegram messages, so all four
fields live here and the rest of the project re-exports them.

Public surface:
    Card                  dataclass with title, size, language, file_name, url
    extract_cards(html)   -> List[Card]  (pure, no I/O)
    quick_extract(html)   -> List[dict]  (legacy shape — list of dicts only)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import List, Tuple


_LINK_XDMOVIES_RE = r"link\.xdmovies\.wtf/download/[A-Za-z0-9_\-]+"
# Direct hub links printed straight on newer movie/series pages
# (pack-card / episode-card anchors, e.g. hubcloud.one/drive/... or the
# newer hubcloud.one/video/<id> download-item cards seen 2026-09-07).
_DIRECT_HUB_RE = (
    r"(?:hubcloud|hubdrive|gdflix|gofile|vcloud|fdrive)\.[a-zA-Z.]+"
    r"/(?:drive|file|d|video)/[A-Za-z0-9_\-]+"
)
_GDRIVE_RE = r"drive\.google\.com/[A-Za-z0-9_/\-]+"
_HREF_URL_RE = r"https?://(?:" + _LINK_XDMOVIES_RE + r"|" + _DIRECT_HUB_RE + r"|" + _GDRIVE_RE + r")"

DOWNLOAD_RE = re.compile(_HREF_URL_RE)

_TITLE_LABELS = ("episode-title", "pack-title", "custom-title")
_CARD_A_RE = re.compile(
    r'<a\b[^>]*\bhref="(?P<href>' + _HREF_URL_RE + r')"',
    re.IGNORECASE,
)
_TITLE_OPEN_RE = re.compile(
    r"<([a-zA-Z][a-zA-Z0-9]*)\b[^>]*\bclass=\"(?P<cls>[^\"]*(?:"
    + "|".join(_TITLE_LABELS)
    + r")[^\"]*)\"",
    re.IGNORECASE,
)

_SIZE_RE = re.compile(
    # Pack cards wrap the size in <span class="size-badge"> AFTER the
    # download icon; episode cards print it as bare text after </i>.
    r"<i\s+class=\"[^\"]*fa-download[^\"]*\"[^>]*>\s*</i>\s*"
    r"(?:<span[^>]*>\s*)?([0-9]+(?:\.[0-9]+)?\s*(?:GB|MB|KB))",
    re.IGNORECASE,
)
_LANG_RE = re.compile(
    r"\b(?:Language|Audios?|Audio\s*Languages?|Languages?)\s*[:\-]\s*(<[^>]*>)*([^<]+?)(?=<|$|\n)",
    re.IGNORECASE,
)


@dataclass
class Card:
    """One download entry as the page renders it."""
    title: str = ""
    size: str = ""
    language: str = ""
    file_name: str = ""
    url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _strip_tags(s: str) -> str:
    no_tags = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", no_tags).strip()


@dataclass
class HubDetails:
    """What a HubCloud drive page states statically (no clicks needed).

    title/size come from <title> + the File Size row; final_url is the
    gamerxyt token URL from `var url = '...'` (fallback: a#download href)
    -- the same destination the gate flow produces after full automation.
    Empty strings mean "not found".
    """
    title: str = ""
    size: str = ""
    final_url: str = ""


_HUB_TITLE_RE = re.compile(r"<title[^>]*>([^<]{4,200})</title>", re.IGNORECASE)
_HUB_HEADER_RE = re.compile(
    r'class="card-header[^"]*"[^>]*>\s*([^<]{4,200}?)\s*</div>', re.IGNORECASE | re.DOTALL)
_HUB_SIZE_RE = re.compile(r"File Size<i[^>]*>([^<]{1,30})</i>", re.IGNORECASE)
_HUB_VAR_URL_RE = re.compile(r"var\s+url\s*=\s*'(https?://[^']+)'")
_HUB_DL_HREF_RE = re.compile(
    r"<a\b(?=[^>]*\bid=\"download\")[^>]*\bhref=\"([^\"]+)\"", re.IGNORECASE)


def extract_hub_details(html: str) -> HubDetails:
    """Parse a HubCloud /drive/<id> page. Pure function, no I/O."""
    if not html:
        return HubDetails()
    title = ""
    m = _HUB_TITLE_RE.search(html)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()
    if not title:
        m = _HUB_HEADER_RE.search(html)
        if m:
            title = re.sub(r"\s+", " ", m.group(1)).strip()
    size = ""
    m = _HUB_SIZE_RE.search(html)
    if m:
        size = re.sub(r"\s+", " ", m.group(1)).strip()
    final_url = ""
    m = _HUB_VAR_URL_RE.search(html)
    if m:
        final_url = m.group(1).strip()
    if not final_url:
        m = _HUB_DL_HREF_RE.search(html)
        if m:
            final_url = m.group(1).strip()
    return HubDetails(title=title, size=size, final_url=final_url)


def _read_tag_text(html: str, open_match_start: int) -> Tuple[str, int]:
    """Given the start of a matching open-tag, return (text, position_after_close)."""
    open_end = html.find(">", open_match_start)
    if open_end == -1:
        return "", open_match_start
    tag_match = re.match(r"<([a-zA-Z][a-zA-Z0-9]*)", html[open_match_start:open_end + 1])
    if not tag_match:
        return "", open_match_start
    tag = tag_match.group(1).lower()

    open_re = re.compile(r"<" + re.escape(tag) + r"\b", re.IGNORECASE)
    close_re = re.compile(r"</" + re.escape(tag) + r"\s*>", re.IGNORECASE)

    inner_start = open_end + 1
    depth = 1
    pos = inner_start
    while depth > 0:
        nxt_open = open_re.search(html, pos)
        nxt_close = close_re.search(html, pos)
        if nxt_close is None:
            return "", open_match_start
        if nxt_open is not None and nxt_open.start() < nxt_close.start():
            depth += 1
            pos = nxt_open.end()
        else:
            depth -= 1
            pos = nxt_close.end()
            if depth == 0:
                inner = html[inner_start:nxt_close.start()]
                return _strip_tags(inner), pos
    return "", open_match_start


def _find_size_in_window(window_html: str) -> str:
    m = _SIZE_RE.search(window_html)
    return m.group(1).strip() if m else ""


def _find_language_in_window(window_html: str) -> str:
    """Match `Language: <lang>` in the bounded card window using the raw
    HTML so the `<` boundaries stop the capture.
    """
    m = _LANG_RE.search(window_html)
    if not m:
        return ""
    raw = _strip_tags(m.group(0))
    raw = re.sub(r"\blanguage\s*[:\-]\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s+", " ", raw).strip(" ,;")
    cut = re.search(r"[.;]|,(?=[A-Z])", raw)
    if cut:
        raw = raw[: cut.start()]
    return raw


def extract_cards(html: str) -> List[Card]:
    """Walk the page in document order. For each download anchor, build a Card
    with nearest preceding title + size + language fields.

    Important: the per-card window is *bounded by neighbouring cards* so a
    giant block of repeated meta (size/lang) inside one parent doesn't leak
    across cards. Each card's window runs from the previous card's anchor
    end to the next card's anchor start (between anchors) — or falls back
    to a small ±600-char window around the anchor if no neighbour exists.
    """
    if not html:
        return []

    # Each anchor is recorded as (open_start, close_end, href) — i.e. the
    # span of the FULL <a ...>...</a> element, not just the href="..." attribute.
    # _CARD_A_RE ends at the closing " of the href, which sits mid-way through
    # the opening <a ...> tag, so the size text (<i class="fa-download"></i>
    # 2.06 GB) that lives *inside* the <a> between href="..." and </a> would
    # otherwise leak across cards. We widen to the full <a>...</a> span.
    anchors: List[Tuple[int, int, str]] = []
    for m in _CARD_A_RE.finditer(html):
        href = m.group("href")
        # find end of opening <a ...> tag — the next '>' after m.start()
        open_end = html.find(">", m.start())
        if open_end == -1:
            continue
        # find matching </a> — search forward, case-insensitive
        close_re = re.compile(r"</a\s*>", re.IGNORECASE)
        close_m = close_re.search(html, open_end + 1)
        if not close_m:
            continue
        anchors.append((m.start(), close_m.end(), href))
    if not anchors:
        return []

    title_results: List[Tuple[int, str, int]] = []
    for m in _TITLE_OPEN_RE.finditer(html):
        text, end_pos = _read_tag_text(html, m.start())
        if text and text.strip():
            title_results.append((m.start(), text, end_pos))

    out: List[Card] = []
    seen: set = set()

    # Pre-compute every title element's start position so we can binary-search
    # forward to skip past the CURRENT card's anchor region to find the next
    # border.
    title_starts = [t[0] for t in title_results]
    t_idx = 0

    for i, (a_start, a_end, url) in enumerate(anchors):
        if url in seen:
            continue
        seen.add(url)

        # The card window is bounded by neighbouring anchors. If no neighbour
        # exists (single card, last card), we still want to scan ±600 chars
        # around the anchor.
        prev_anchor_end = anchors[i - 1][1] if i > 0 else -1
        next_anchor_start = anchors[i + 1][0] if i + 1 < len(anchors) else len(html)
        window_left = max(prev_anchor_end + 1, a_start - 600)
        window_right = max(next_anchor_start - 1, a_end + 200)
        window = html[window_left:window_right]

        # Title: the most recent title element whose start is inside the
        # window AND before the anchor.
        title = ""
        while t_idx < len(title_results) and title_results[t_idx][0] <= a_start:
            t_start, t_text, _ = title_results[t_idx]
            if t_start >= window_left and t_text:
                title = t_text
            t_idx += 1
        title = title.strip() if title else ""

        size = _find_size_in_window(window)
        language = _find_language_in_window(window)

        out.append(Card(
            title=title,
            size=size,
            language=language,
            file_name=title,
            url=url,
        ))

    return out


def extract_cards_from_html(html: str) -> List[Card]:
    """Public alias — matches the wording in telegram.md."""
    return extract_cards(html)


def quick_extract(html: str) -> List[Tuple[str, str]]:
    """Legacy-shape helper: [(url, title), ...] for old call sites."""
    return [(c.url, c.title) for c in extract_cards(html)]

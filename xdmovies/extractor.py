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
    """One download entry as the page renders it.

    kind mirrors the title element's class: "episode" (episode-title),
    "pack" (pack-title), "custom" (custom-title), or "" when the page
    doesn't label it. Powers the Episodes / Packs-Zips content filter.
    """
    title: str = ""
    size: str = ""
    language: str = ""
    file_name: str = ""
    url: str = ""
    kind: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _kind_from_class(cls: str) -> str:
    low = (cls or "").lower()
    if "pack-title" in low:
        return "pack"
    if "episode-title" in low:
        return "episode"
    if "custom-title" in low:
        return "custom"
    return ""


def filter_cards_by_content(cards: List[Card], content: str) -> List[Card]:
    """Keep only episode or pack cards for the accept-all content choice.

    content is "all" | "episodes" | "packs". "all" (and anything unknown)
    returns everything.

    Mixed .txt lists (movies + series) are safe by design:
    - "custom" cards (movie pages / single custom links) are kept under
      BOTH filters -- movies are always fully extracted, whatever the
      user picked. Only series episode/pack sections get narrowed.
    - Cards with NO kind info (older markup, gate-direct single cards)
      are always kept: filtering is impossible, so everything is
      returned rather than wiping the job to zero.
    """
    if content not in ("episodes", "packs"):
        return list(cards)
    want = {"episode", "custom"} if content == "episodes" else {"pack", "custom"}
    kinds = {c.kind for c in cards if c.kind}
    if not kinds:
        return list(cards)
    return [c for c in cards if not c.kind or c.kind in want]


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

    title_results: List[Tuple[int, str, int, str]] = []
    for m in _TITLE_OPEN_RE.finditer(html):
        text, end_pos = _read_tag_text(html, m.start())
        if text and text.strip():
            title_results.append((m.start(), text, end_pos, m.group("cls") or ""))

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
        # window AND before the anchor. Its class also decides the card
        # kind (episode / pack / custom) for the content filter.
        title = ""
        kind = ""
        while t_idx < len(title_results) and title_results[t_idx][0] <= a_start:
            t_start, t_text, _, t_cls = title_results[t_idx]
            if t_start >= window_left and t_text:
                title = t_text
                kind = _kind_from_class(t_cls)
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
            kind=kind,
        ))

    return out


_AUDIO_INFO_RE = re.compile(
    r'<p\b[^>]*\bid=["\']audio-info["\'][^>]*>(.*?)</p>',
    re.IGNORECASE | re.DOTALL,
)


def extract_page_audios(html: str) -> str:
    """Page-level audio languages, e.g. "Hindi, English, Tamil".

    Parsed from `<p id="audio-info">... Audios: <span>Hindi, English,
    ...</span></p>` in the movie/series info block (same for every card
    on the page). Pure function, no I/O. Returns "" when absent.
    """
    if not html:
        return ""
    try:
        m = _AUDIO_INFO_RE.search(html)
        if not m:
            return ""
        text = _strip_tags(m.group(1))
        text = re.sub(r"^.*?\baud(?:io)?s?\s*[:\-]\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+", " ", text).strip(" ,;")
        return text[:200]
    except Exception:
        return ""


_META_LABEL_RES = {
    # <p><strong>Rating:</strong> 6.2 / 10 ⭐</p> — first <strong>-labelled
    # match wins (SEO footer reuses bare "Genres:" without <strong>).
    "rating": re.compile(
        r"<strong>\s*Rating\s*:?\s*</strong>\s*([\d.]+\s*(?:/\s*[\d.]+)?)",
        re.IGNORECASE),
    "genres": re.compile(
        r"<strong>\s*Genres?\s*:?\s*</strong>\s*([^<]{1,120})</p>",
        re.IGNORECASE),
    # Series pages say "First Air Date", movie pages "Release Date".
    "air_date": re.compile(
        r"<strong>\s*(?:First\s+Air\s+Date|Release\s+Date)\s*:?\s*</strong>\s*([^<]{1,40})</p>",
        re.IGNORECASE),
    "sources": re.compile(
        r'<p\b[^>]*\bid=["\']source-info["\'][^>]*>(.*?)</p>',
        re.IGNORECASE | re.DOTALL),
}

_TMDB_ID_RE = re.compile(r"-(\d+)\s*$")


def extract_page_meta(html: str) -> dict:
    """Page-level info-block details: rating/genres/air_date/sources.

    Keys are always present ("" when absent) so renderers can gate on
    per-user /settings prefs without extra plumbing. Pure, no I/O.
    """
    out = {"rating": "", "genres": "", "air_date": "", "sources": ""}
    if not html:
        return out
    try:
        m = _META_LABEL_RES["rating"].search(html)
        if m:
            out["rating"] = re.sub(r"\s+", " ", m.group(1)).strip(" ,;")[:40]
    except Exception:
        pass
    try:
        m = _META_LABEL_RES["genres"].search(html)
        if m:
            # Strip trailing "#tag #tag" tail the SEO paragraph appends.
            t = re.sub(r"\s+", " ", m.group(1)).strip(" ,;")
            t = re.split(r"\s+#", t, maxsplit=1)[0].strip(" ,;")
            out["genres"] = t[:120]
    except Exception:
        pass
    try:
        m = _META_LABEL_RES["air_date"].search(html)
        if m:
            out["air_date"] = re.sub(r"\s+", " ", m.group(1)).strip(" ,;")[:40]
    except Exception:
        pass
    try:
        m = _META_LABEL_RES["sources"].search(html)
        if m:
            t = _strip_tags(m.group(1))
            t = re.sub(r"^.*?\bsources?\s*[:\-]\s*", "", t, flags=re.IGNORECASE)
            out["sources"] = re.sub(r"\s+", " ", t).strip(" ,;")[:120]
    except Exception:
        pass
    return out


def extract_tmdb_code(url: str) -> str:
    """Numeric id trailing the page slug, e.g. `...-download-273207`
    -> "273207". Used for the optional TMDB detail line. Pure, no I/O."""
    try:
        from urllib.parse import urlsplit
        path = (urlsplit(url or "").path or "").rstrip("/")
        m = _TMDB_ID_RE.search(path)
        return m.group(1) if m else ""
    except Exception:
        return ""


def extract_cards_from_html(html: str) -> List[Card]:
    """Public alias — matches the wording in telegram.md."""
    return extract_cards(html)


def quick_extract(html: str) -> List[Tuple[str, str]]:
    """Legacy-shape helper: [(url, title), ...] for old call sites."""
    return [(c.url, c.title) for c in extract_cards(html)]


# ---------------------------------------------------------------------------
# Quality grouping for the /qmode series picker (/qmode ON + /series/ URL).
# Cards are bucketed by (season, quality, codec) parsed from the card
# title, e.g. "Season 1 1080p H.265". Titles that carry no recognizable
# markers land in the "Other" bucket (season 0, quality/codec "").
# Pure function, no I/O. Page order is preserved inside each bucket.
# ---------------------------------------------------------------------------

_SEASON_RES = (
    re.compile(r"\bseason\s*(\d{1,2})\b", re.IGNORECASE),
    # S01 in "S01", "S01E01", "S1" (lookahead: episode marker or word end;
    # plain \b fails on S01E01 since 1->E has no boundary).
    re.compile(r"\bS(\d{1,2})(?=E\d{1,3}\b|\b)"),
)
_QUALITY_RES = (
    ("2160p", re.compile(r"\b2160p\b|\b4k\b", re.IGNORECASE)),
    ("1080p", re.compile(r"\b1080p\b", re.IGNORECASE)),
    ("720p", re.compile(r"\b720p\b", re.IGNORECASE)),
    ("480p", re.compile(r"\b480p\b", re.IGNORECASE)),
)
_CODEC_RES = (
    ("H.265", re.compile(r"\b(?:h\.?265|x265|hevc)\b", re.IGNORECASE)),
    ("H.264", re.compile(r"\b(?:h\.?264|x264)\b", re.IGNORECASE)),
)

# Sort rank so buckets list best quality first (2160p H.265 at top).
_QUALITY_RANK = {"2160p": 0, "1080p": 1, "720p": 2, "480p": 3, "": 4}
_CODEC_RANK = {"H.265": 0, "H.264": 1, "": 2}


@dataclass
class QualityGroup:
    """One toggle row-set in the picker: all cards of one season +
    quality + codec. `cards` keeps page order."""
    season: int = 0  # 0 = unknown -> rendered as "Other" section
    quality: str = ""
    codec: str = ""
    cards: List[Card] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.cards is None:
            self.cards = []

    @property
    def label(self) -> str:
        if self.season <= 0 and not self.quality and not self.codec:
            return f"Other ({len(self.cards)})"
        parts = []
        if self.season > 0:
            parts.append(f"S{self.season}")
        if self.quality:
            parts.append(self.quality)
        if self.codec:
            parts.append(self.codec)
        return f"{' '.join(parts)} ({len(self.cards)})".strip()

    @property
    def section(self) -> str:
        return f"Season {self.season}" if self.season > 0 else "Other"


def _parse_quality_key(title: str) -> Tuple[int, str, str]:
    t = title or ""
    season = 0
    for rx in _SEASON_RES:
        m = rx.search(t)
        if m:
            try:
                season = int(m.group(1))
            except (TypeError, ValueError):
                season = 0
            break
    quality = ""
    for name, rx in _QUALITY_RES:
        if rx.search(t):
            quality = name
            break
    codec = ""
    for name, rx in _CODEC_RES:
        if rx.search(t):
            codec = name
            break
    return season, quality, codec


def group_cards_by_quality(cards: List[Card]) -> List[QualityGroup]:
    """Bucket cards into ordered QualityGroups for the picker.

    Buckets sort by season asc, then quality desc (2160p first), then
    codec (H.265 before H.264); the catch-all Other bucket sorts last.
    Cards that share identical (season, quality, codec) stay together
    in page order.
    """
    buckets: dict = {}
    order: List[Tuple[int, str, str]] = []
    for c in cards or []:
        key = _parse_quality_key(c.title)
        if key not in buckets:
            buckets[key] = QualityGroup(season=key[0], quality=key[1], codec=key[2])
            order.append(key)
        buckets[key].cards.append(c)

    def _rank(key: Tuple[int, str, str]):
        season, quality, codec = key
        season_rank = season if season > 0 else 10**6
        return (
            season_rank,
            _QUALITY_RANK.get(quality, 4),
            _CODEC_RANK.get(codec, 2),
            order.index(key),
        )

    return [buckets[k] for k in sorted(order, key=_rank)]


# ---------------------------------------------------------------------------
# Section grouping for /dmode (download mode) picker.
# Cards are bucketed by (season, section) where section is "packs" or
# "episodes" — mirroring the site's own "Season N Packs/Zips" /
# "Season N Episodes" toggle sections (e.g. Reacher: S1..S4 x packs/ep).
# Season is reused from _parse_quality_key (S01 / S01E05 markers);
# section comes from Card.kind (pack-title -> pack, episode-title ->
# episode). Pure function, no I/O. Page order preserved inside buckets.
# ---------------------------------------------------------------------------

@dataclass
class SectionGroup:
    """One toggle row in the dmode picker: all pack OR episode cards of
    one season. `cards` keeps page order."""
    season: int = 0  # 0 = unknown -> rendered under "Other"
    section: str = ""  # "packs" | "episodes"
    cards: List[Card] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.cards is None:
            self.cards = []

    @property
    def label(self) -> str:
        sec = "Packs" if self.section == "packs" else "Episodes"
        if self.season <= 0:
            return f"Other {sec} ({len(self.cards)})"
        return f"S{self.season} {sec} ({len(self.cards)})"

    @property
    def section_header(self) -> str:
        return f"Season {self.season}" if self.season > 0 else "Other"


_SECTION_RANK = {"packs": 0, "episodes": 1}


def _parse_section_key(title: str, kind: str) -> Tuple[int, str]:
    """(season, section) for one card. Unknown kind lands in BOTH sections
    via the caller (see group_cards_by_section) so movies/untagged markup
    are never filtered to zero."""
    season, _quality, _codec = _parse_quality_key(title)
    k = (kind or "").lower()
    if k == "pack":
        return season, "packs"
    if k == "episode":
        return season, "episodes"
    return season, ""


def group_cards_by_section(cards: List[Card]) -> List[SectionGroup]:
    """Bucket cards into ordered SectionGroups for the /dmode picker.

    Buckets sort by season asc (unknown last), packs before episodes
    within a season. Cards with no kind info (custom/movie markup) are
    kept under BOTH sections so section filtering can never wipe a job
    to zero — same rule as filter_cards_by_content.
    """
    buckets: dict = {}
    order: List[Tuple[int, str]] = []
    for c in cards or []:
        season, section = _parse_section_key(c.title, c.kind)
        keys = [(season, section)] if section else [(season, "packs"), (season, "episodes")]
        for key in keys:
            if key not in buckets:
                buckets[key] = SectionGroup(season=key[0], section=key[1])
                order.append(key)
            buckets[key].cards.append(c)

    def _rank(key: Tuple[int, str]):
        season, section = key
        season_rank = season if season > 0 else 10**6
        return (
            season_rank,
            _SECTION_RANK.get(section, 2),
            order.index(key),
        )

    return [buckets[k] for k in sorted(order, key=_rank)]

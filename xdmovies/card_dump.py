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
#  File:    xdmovies/card_dump.py
#  Fingerprint: BRUNO-E1CCB1FB-C4D05E11
#  ============================================================

"""Parse a pasted/forwarded result-card dump and re-emit it (with mirrors).

Pure logic. No network, no browser, no Telegram import -- the module can
be imported from anywhere (tests, CLI tooling, the bot handler) without
pulling in the CF-bypass chain.

WHY
---
The bot emits result cards in a fixed shape (``bot.py``):

    <b>1) Girls.Will.Be.Girls.2024.720p.AMZN.WEB-DL.DDP5.1.H.265-XDMovies.com.mkv</b>
    Audio: Hindi, English, Malayalam
    Size: 0.83 GB
    TMDB: 1108828
    -> https://hubcloud.ist/drive/imv2ibxqt9bjx6o

The owner pastes such a dump back into the chat; this module turns that
pasted text back into structured cards so the bot can re-emit them with
extra mirror lines appended.

CONSTANTS (kept in sync with the repo -- do not drift)
    bot.py:192  MSG_LIMIT            = 4000
    bot.py:198  _BATCH_CHUNK_BUDGET = MSG_LIMIT - 400  -> 3600
    bot.py:199  _BATCH_SEP           = "\\u2500" * 25    -> 25x U+2500
    bot.py:278  block.append(f"\\u2192 {_html_escape(dest)}")  -> U+2192 + SPACE

PUBLIC API
    BATCH_SEP, ARROW, MSG_LIMIT, CHUNK_BUDGET
    DumpLink, DumpCard, Dump                      (dataclasses)
    parse_dump(text) -> Dump
    render_card(card, mirrors=None, *, keep_hub=True) -> str
    render_dump(dump, mirrors_by_card=None, *, keep_hub=True) -> str
    chunk_dump(text, limit=MSG_LIMIT) -> list[str]
    html_escape(s) -> str
    normalize(s) -> str

CONVENTIONS
* ``Dump.page_title`` is stored WITHOUT its trailing colon; the renderer
  adds exactly one back (``<b>{title}:</b>``), so a parsed title
  round-trips byte-identically -- including a title that ends in ``::``.
* ``DumpCard.links`` keeps every link line of the card in SOURCE ORDER.
  A link with ``label is None`` is the card's hub link; a link with a
  label is a mirror line (``->pixeldrain: https://...``).
* ``DumpCard.extra`` holds lines that are neither a title, nor a known
  meta row, nor a link line (e.g. ``Note: https://tracker/x``).
* ``Dump.notes`` holds parse warnings only (a clean dump has none).
* TAGS: a pasted dump comes in two shapes -- the bot's RAW HTML source
  (``<b>1) Title</b>``) and the DISPLAY text the user copies out of
  Telegram (``1) Title``, tags already rendered away). Only on the RAW
  path is the bot's own ``<b>`` wrapper removed; a ``<b>`` INSIDE a title
  is literal text on the display path and survives as such. Entities are
  decoded AFTER the wrapper handling, so entity-escaped markup
  (``&lt;b&gt;``) stays visible text.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

__all__ = [
    "BATCH_SEP",
    "ARROW",
    "MSG_LIMIT",
    "CHUNK_BUDGET",
    "DumpLink",
    "DumpCard",
    "Dump",
    "parse_dump",
    "render_card",
    "render_dump",
    "chunk_dump",
    "html_escape",
    "normalize",
]

# ---------------------------------------------------------------- constants
BATCH_SEP = "─" * 25          # U+2500 x 25   (bot.py:199)
ARROW = "→"                   # U+2192        (bot.py:278)
MSG_LIMIT = 4000              # bot.py:192
CHUNK_BUDGET = MSG_LIMIT - 400  # 3600, bot.py:198

# How cards are glued together inside one results message (bot.py:2237).
_JOIN_SEP = "\n\n" + BATCH_SEP + "\n\n"

# Meta rows the repo can emit. settings_store.ALL_FIELDS gives
# Size/Rating/Genre/First Air/Audios/Sources/TMDB; bot.py adds
# Audio (page level), Language and File.
META_LABELS = (
    "Audio", "Audios", "Size", "TMDB", "Rating", "Genre", "Genres",
    "First Air", "Source", "Sources", "Language", "File",
)
#: The base label set (lower case). The LIVE set is _meta_label_set().
_META_LABEL_SET = frozenset(l.lower() for l in META_LABELS)
#: Extra labels registered at runtime through add_meta_label().
_EXTRA_META_LABELS: Tuple[str, ...] = ()
_meta_labels_cache: Optional[frozenset] = None

# Arrow-ish tokens accepted in front of a link.
_ARROW_TOKENS = (
    "→", "⇒", "➜", "➔", "▶", "›", "»", "—", "–",
    "->", "=>", "--", "=*", "-", "=", "*",
)

# ---------------------------------------------------------------- regexes
_ZERO_WIDTH_RE = re.compile(
    "[\u200b-\u200f\u2060\ufeff\u00ad\u180e\u202a-\u202e]"
)
_NBSP_RE = re.compile("[\u00a0\u202f\u2007]")
_ENTITIES = {
    "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
    "&apos;": "'", "&nbsp;": " ", "&#39;": "'", "&#x27;": "'",
}
_ENTITY_RE = re.compile(
    "|".join(re.escape(k) for k in sorted(_ENTITIES, key=len, reverse=True)),
    re.IGNORECASE,
)

# The bot wraps a card title in <b>...</b>. This is the ONLY tag the bot
# ever emits, so it is the ONLY tag that is ever removed.
_B_OPEN_RE = re.compile(r"^[ \t]*<b(?:\s[^>]*)?>", re.IGNORECASE)
_B_CLOSE_RE = re.compile(r"</b>[ \t]*$", re.IGNORECASE)

# "N) title" -- the bot's own numbering. `[ \t]+` instead of `\s+` so a
# header can never swallow the next line. The bot's numbering is
# unbounded (bot.py:2232), so 6 digits are accepted.
_HEADER_PAREN_RE = re.compile(r"^[ \t]*(\d{1,6})\)[ \t]+(.*)$", re.MULTILINE)
# "N. title" -- accepted only when the dump has no "N)" header at all.
_HEADER_DOT_RE = re.compile(r"^[ \t]*(\d{1,3})\.[ \t]+(.*)$", re.MULTILINE)
# Any "N)" anywhere disables the "N." form.
_HAS_PAREN_RE = re.compile(r"^[ \t]*\d{1,6}\)", re.MULTILINE)

# A meta row is "<label>: <value>". The label may hold letters, digits,
# spaces, dots and hyphens ("Top 10", "IMDB-Rating") -- the KNOWN label
# set is what keeps a random "Note: ..." line out of the meta section.
_META_RE = re.compile(
    r"^[ \t]*(?P<label>[A-Za-z][A-Za-z0-9 _.\-]{0,30})[ \t]*:[ \t]*(?P<value>.+)$"
)

# URL charset (RFC 3986 + the few sub-delims that show up in dump links),
# as a frozenset -- used by the LINEAR url scanner.
_URLCHARSET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    "._~:/?#[]@!$&'()*+,;=%-"
)
_HTTP_RE = re.compile("http", re.IGNORECASE)
_SCHEMES = ("https://", "http://")
_ARROW_RE = re.compile(
    r"^(?P<arrow>" + "|".join(re.escape(t) for t in _ARROW_TOKENS) + r")[ \t]*(?P<rest>.*)$"
)
#: A mirror label: starts with a letter OR a digit ("10Gbps"!), and is at
#: most 40 chars long -- the same ceiling hub_mirrors.label_for uses.
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,39}$")
#: A rendered link line ("-> url" / "->label: url").
_LINK_LINE_RE = re.compile(
    r"^[ \t]*(?:" + "|".join(re.escape(t) for t in _ARROW_TOKENS) + r")[ \t]*(?P<rest>.*)$"
)

_BOX_LO, _BOX_HI = "\u2500", "\u257f"
_ASCII_SEP_CHARS = "=-_*~"

#: The bot's link-slot lines. They occupy the LINK position of a card
#: block, not the "extra" position.
_LINK_SLOT_PREFIXES = ("[FAIL]", "⏳")


# ---------------------------------------------------------------- helpers
def html_escape(s: str) -> str:
    """Escape text for Telegram's HTML parse mode.

    Byte-identical to bot.py:290 ``_html_escape`` (``html.escape(s,
    quote=False)``): only ``&``, ``<`` and ``>`` are escaped. A URL with
    ``?a=1&b=2`` MUST become ``?a=1&amp;b=2`` or Telegram's HTML parser
    raises ``BadRequest("Can't parse entities")``.
    """
    if s is None:
        return ""
    return html.escape(str(s), quote=False)


def normalize(text: str) -> str:
    """Clean a pasted dump: line endings, BOM, invisible chars, HTML.

    Never raises. Idempotent: normalize(normalize(x)) == normalize(x) for
    every input this module cares about (raw-HTML dumps, display dumps
    and entity-escaped markup alike).
    """
    return _normalize(text or "")


def _looks_raw_html(text: str) -> bool:
    """True when the text carries the bot's raw ``<b>`` wrapper."""
    for line in (text or "").split("\n"):
        if _B_OPEN_RE.match(line):
            return True
    return False


def _strip_wrapper(line: str) -> str:
    """Remove the bot's own ``<b>`` wrapper from one line.

    Only the OUTER wrapper goes: a ``<b>`` INSIDE the title is literal
    text and must survive. A lone ``<b>`` (nothing behind it) is left
    alone so that normalize() stays idempotent for entity-escaped
    markup.
    """
    m = _B_OPEN_RE.match(line)
    if not m:
        return line
    rest = line[m.end():]
    if not rest.strip():
        return line
    return _B_CLOSE_RE.sub("", rest)


def _normalize(text: str) -> str:
    if not text:
        return ""
    s = text.replace("\r\n", "\n").replace("\r", "\n")
    s = s.replace("\ufeff", "")
    s = _ZERO_WIDTH_RE.sub("", s)
    s = _NBSP_RE.sub(" ", s)
    # The bot's own wrapper is removed ONLY on the raw-HTML path. On the
    # display path (what the user copies out of Telegram) every tag is
    # literal text and has to survive.
    if _looks_raw_html(s):
        s = "\n".join(_strip_wrapper(line) for line in s.split("\n"))
    # Entities last: escaped markup stays visible text.
    s = _ENTITY_RE.sub(lambda m: _ENTITIES[m.group(0).lower()], s)
    return s


def _meta_label_set() -> frozenset:
    """The known meta labels (lower case).

    Extensible: ``settings_store.ALL_FIELDS`` is folded in (so a new
    /settings field becomes a meta row instead of an "extra" line) and
    more labels can be registered with :func:`add_meta_label`. The
    settings_store import is lazy and MUST NOT break this module when it
    is unavailable.
    """
    global _meta_labels_cache
    if _meta_labels_cache is not None:
        return _meta_labels_cache
    labels = set(l.lower() for l in META_LABELS)
    labels.update(l.lower() for l in _EXTRA_META_LABELS)
    try:
        from xdmovies import settings_store as _ss
        for _key, _label, _emoji in getattr(_ss, "ALL_FIELDS", ()):
            if _label:
                labels.add(str(_label).lower())
    except Exception:
        pass
    _meta_labels_cache = frozenset(labels)
    return _meta_labels_cache


def add_meta_label(label: str) -> None:
    """Register an extra meta label (e.g. a new /settings field)."""
    global _EXTRA_META_LABELS, _meta_labels_cache
    label = (label or "").strip()
    if not label:
        return
    _EXTRA_META_LABELS = _EXTRA_META_LABELS + (label,)
    _meta_labels_cache = None


# ---------------------------------------------------------------- model
@dataclass
class DumpLink:
    """One link line of a card.

    ``label`` is None for the card's hub link (``-> https://x``) and the
    mirror name for a labelled mirror line (``->pixeldrain: https://x``).
    ``arrow`` keeps the arrow token that was found in the source text.
    """

    label: Optional[str] = None
    url: str = ""
    arrow: str = ""


@dataclass
class DumpCard:
    """One result card."""

    number: Optional[int] = None
    title: str = ""
    meta: List[Tuple[str, str]] = field(default_factory=list)
    links: List[DumpLink] = field(default_factory=list)
    extra: List[str] = field(default_factory=list)

    @property
    def hub(self) -> Optional[DumpLink]:
        """The card's primary (unlabelled) link, if any."""
        for link in self.links:
            if not link.label:
                return link
        return None

    @property
    def mirrors(self) -> List[DumpLink]:
        """Labelled links already present in the pasted card."""
        return [link for link in self.links if link.label]


@dataclass
class Dump:
    """A whole pasted dump."""

    page_title: Optional[str] = None
    preamble: List[str] = field(default_factory=list)
    cards: List[DumpCard] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


# ---------------------------------------------------------------- classify
def _is_separator(line: str) -> bool:
    """A separator line ends a card.

    Box-drawing runs (U+2500-U+257F) need >= 2 chars, ASCII runs of
    ``= - _ ~ *`` need >= 3.
    """
    core = line.strip().replace(" ", "").replace("\t", "")
    if not core:
        return False
    if len(core) >= 2 and all(_BOX_LO <= ch <= _BOX_HI for ch in core):
        return True
    if len(core) >= 3 and all(ch in _ASCII_SEP_CHARS for ch in core):
        return True
    return False


def _match_header(line: str, allow_dot: bool) -> Optional[Tuple[Optional[int], str]]:
    """``(number, title)`` if *line* starts a new card, else None."""
    m = _HEADER_PAREN_RE.match(line)
    if m:
        return int(m.group(1)), m.group(2).strip()
    if allow_dot:
        m = _HEADER_DOT_RE.match(line)
        if m:
            rest = m.group(2).strip()
            # "1.5 GB Movie.mkv" / "2024. Movie.mkv" are NOT headers.
            if rest and not rest[0].isdigit():
                return int(m.group(1)), rest
    return None


def _match_meta(line: str) -> Optional[Tuple[str, str]]:
    """``(label, value)`` for a known meta row, else None.

    The label text is preserved as written (case included) and the source
    order of the rows is preserved by the caller.
    """
    m = _META_RE.match(line)
    if not m:
        return None
    label = m.group("label").strip()
    if label.lower() not in _meta_label_set():
        return None
    value = m.group("value").strip()
    if not value:
        return None
    return label, value


def _clean_url(url: str) -> str:
    """Trim trailing punctuation that is not part of the URL."""
    u = url.strip()
    while u and u[-1] in ".,":
        u = u[:-1]
    if u.endswith(")") and u.count("(") < u.count(")"):
        u = u[:-1]
    return u


def _split_url(line: str) -> Optional[Tuple[str, str]]:
    """Split `line` into ``(url, pre)``.

    The URL is the trailing token of the line. The scan takes the maximal
    URL-character SUFFIX of the line and then looks for the first
    "http" inside it.

    It deliberately does NOT scan from the left with a lazy prefix: the
    old ``^[ \\t]*(?P<pre>.*?)[ \\t]*(?P<url>https?://...)$`` pattern was
    quadratic (16 KB -> 0.87 s, 72 KB -> 16.8 s, 204 KB -> 144 s), so a
    200 KB pasted dump blocked the bot for minutes.
    """
    s = (line or "").rstrip(" \t")
    if not s:
        return None
    i = len(s)
    while i > 0 and s[i - 1] in _URLCHARSET:
        i -= 1
    if i == len(s):
        return None
    for m in _HTTP_RE.finditer(s, i):
        p = m.start()
        for sch in _SCHEMES:
            if s[p:p + len(sch)].lower() == sch and len(s) - (p + len(sch)) >= 1:
                return s[p:], s[:p]
    return None


def _match_link(line: str) -> Optional[DumpLink]:
    """Parse a link line.

    A line is a link line iff the URL is the LAST token on the line AND
    everything before it is empty, an arrow token, or an arrow token
    followed by ``label:``.

        -> https://x/y                link, no label (hub)
        ->pixeldrain: https://x/y     link, label "pixeldrain" (mirror)
        https://x/y                   link, no label (hub)

    and NOT a link:

        Source: https://tracker/x     known meta label -> meta row
        A.mkv (https://cdn/cover.jpg) prose before the URL
        Note: https://tracker/x       unknown label, no arrow -> extra line
    """
    parts = _split_url(line)
    if not parts:
        return None
    url, pre_raw = parts
    url = _clean_url(url)
    if not url:
        return None
    pre = pre_raw.strip()
    if not pre:
        return DumpLink(label=None, url=url, arrow="")
    am = _ARROW_RE.match(pre)
    if not am:
        return None                      # prose before the URL
    arrow = am.group("arrow")
    rest = am.group("rest").strip()
    if not rest:
        return DumpLink(label=None, url=url, arrow=arrow)
    if not rest.endswith(":"):
        return None                      # arrow + something that is not a label
    label = rest[:-1].strip()
    if not _LABEL_RE.match(label):
        return None
    return DumpLink(label=label, url=url, arrow=arrow)


def _is_link_line(line: str) -> bool:
    """True for a rendered link line ("-> url" / "->label: url")."""
    m = _LINK_LINE_RE.match(line or "")
    if not m:
        return False
    rest = (m.group("rest") or "").strip()
    if not rest:
        return True
    if rest.endswith(":"):
        rest = rest[:-1].strip()
    return bool(rest) and "://" in rest


def _is_link_slot(text: str) -> bool:
    """True for the bot's link-slot lines ("[FAIL] ...", "-> resolving")."""
    t = (text or "").lstrip()
    return any(t.startswith(p) for p in _LINK_SLOT_PREFIXES)


# ---------------------------------------------------------------- parse
def parse_dump(text: str) -> Dump:
    """Parse a pasted multi-card dump. Never raises."""
    dump = Dump()
    try:
        source = _normalize(text or "")
    except Exception as exc:              # pragma: no cover - defensive
        dump.notes.append(f"normalization failed: {exc!r}")
        source = text or ""
    if not source.strip():
        dump.notes.append("empty input")
        return dump

    try:
        lines = source.split("\n")
        # "N." headers are only accepted when the dump has no "N)" header.
        allow_dot = not _HAS_PAREN_RE.search(source)

        cur: Optional[DumpCard] = None
        pre_buf: List[str] = []           # lines before the first card
        pending_title: Optional[str] = None   # a "...:" line before any card
        blank_gap = False                 # a blank line was just seen

        def end_card() -> None:
            nonlocal cur
            if cur is not None:
                dump.cards.append(cur)
                cur = None

        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                blank_gap = True
                continue
            if _is_separator(line):
                end_card()
                blank_gap = False
                continue

            header = _match_header(line, allow_dot)
            if header is not None:
                number, title = header
                end_card()
                # A "...:" line in front of a card HEADER is the page
                # title. In front of the card's own content it is that
                # card's title (see below).
                if pending_title is not None:
                    if dump.page_title is None:
                        dump.page_title = pending_title[:-1].strip()
                    pending_title = None
                cur = DumpCard(number=number, title=title)
                blank_gap = False
                continue

            meta = _match_meta(line)
            link = None if meta is not None else _match_link(line)

            if meta is not None or link is not None:
                if cur is None:
                    if pending_title is not None:
                        # The candidate belongs to THIS card -- it is its
                        # title, not the page title.
                        cur = DumpCard(number=None, title=pending_title)
                        pending_title = None
                    else:
                        # First card without a number: the last preamble
                        # line (if any) is its title.
                        title = pre_buf.pop() if pre_buf else ""
                        cur = DumpCard(number=None, title=title)
                if meta is not None:
                    cur.meta.append(meta)
                else:
                    cur.links.append(link)
                blank_gap = False
                continue

            # ---- unknown line ----
            if cur is not None:
                if blank_gap:
                    # A blank line followed by a line that is neither a
                    # meta row nor a link row ends the current card; the
                    # line becomes the title of the next one.
                    end_card()
                    cur = DumpCard(number=None, title=line)
                else:
                    cur.extra.append(line)
            else:
                if (
                    line.endswith(":")
                    and dump.page_title is None
                    and pending_title is None
                ):
                    # Candidate page title. It only BECOMES the page
                    # title when a card header follows it; when the
                    # card's own content follows, the line is that
                    # card's title and must not be swallowed.
                    pending_title = line
                else:
                    pre_buf.append(line)
            blank_gap = False

        end_card()
        if pending_title is not None:
            # Never followed by a card -> it is not a page title.
            pre_buf.append(pending_title)
            pending_title = None

        dump.preamble.extend(pre_buf)
        if not dump.cards:
            dump.notes.append("no cards found")
        for idx, card in enumerate(dump.cards, 1):
            if not card.title:
                dump.notes.append(f"card {idx}: no title")
            if not card.links:
                dump.notes.append(f"card {idx}: no link lines")
        return dump
    except Exception as exc:              # pragma: no cover - defensive
        dump.notes.append(f"parse error: {exc!r}")
        return dump


# ---------------------------------------------------------------- render
def render_card(card: DumpCard,
                mirrors: Optional[List[Tuple[str, str]]] = None,
                *,
                keep_hub: bool = True) -> str:
    """Render one card block in the bot's own style.

    * ``<b>N) title</b>`` -- the "N) " prefix is dropped for a card that
      had no number.
    * one ``Label: value`` line per meta row, in source order.
    * one line per extra line.
    * the link section LAST: one ``-> url`` (U+2192 + ONE space) per
      unlabelled link and one ``->label: url`` (U+2192 + NO space) per
      labelled link, in SOURCE ORDER. Injected ``mirrors`` are appended
      after the card's own links.

    The bot's link-slot lines (``[FAIL] ...``, ``resolving...``) are
    rendered in the link position, exactly like the bot does.

    A card without links and without mirrors gets NO link line at all --
    inventing a "resolving..." placeholder would be a lie.
    """
    lines: List[str] = []
    title = html_escape(card.title or "(untitled)")
    if card.number is not None:
        lines.append(f"<b>{card.number}) {title}</b>")
    else:
        lines.append(f"<b>{title}</b>")
    for label, value in card.meta:
        lines.append(f"{html_escape(label)}: {html_escape(value)}")
    # The link-slot lines belong to the link position, everything else
    # stays above it.
    slot = [e for e in card.extra if _is_link_slot(e)]
    other = [e for e in card.extra if not _is_link_slot(e)]
    for extra in other:
        lines.append(html_escape(extra))

    # Links in SOURCE ORDER -- the labelled / unlabelled distinction is
    # preserved, so parse -> render -> parse is stable.
    for link in card.links:
        if link.label:
            lines.append(f"{ARROW}{html_escape(link.label)}: {html_escape(link.url)}")
        elif keep_hub:
            lines.append(f"{ARROW} {html_escape(link.url)}")
    for label, url in mirrors or []:
        lines.append(f"{ARROW}{html_escape(label)}: {html_escape(url)}")
    for extra in slot:
        lines.append(html_escape(extra))
    return "\n".join(lines)


def render_dump(dump: Dump,
                mirrors_by_card: Optional[List[List[Tuple[str, str]]]] = None,
                *,
                keep_hub: bool = True,
                keep_preamble: bool = True) -> str:
    """Render a whole dump as one HTML message.

    Layout: optional preamble, then the optional page title header, then
    the cards joined with ``"\\n\\n" + BATCH_SEP + "\\n\\n"``
    (bot.py:2237).
    """
    head: List[str] = []
    if keep_preamble and dump.preamble:
        head.append("\n".join(html_escape(p) for p in dump.preamble))
    title = dump.page_title
    if title:
        # The stored title carries NO trailing colon (the parser removed
        # exactly one) -- put exactly one back, so "<b>Movie::</b>"
        # round-trips byte-identically.
        head.append(f"<b>{html_escape(title)}:</b>")

    mirrors_by_card = mirrors_by_card or []
    body: List[str] = []
    for idx, card in enumerate(dump.cards):
        mirrors = mirrors_by_card[idx] if idx < len(mirrors_by_card) else None
        body.append(render_card(card, mirrors, keep_hub=keep_hub))
    rendered = _JOIN_SEP.join(body)

    if head and rendered:
        return "\n\n".join(head) + "\n\n" + rendered
    if head:
        return "\n\n".join(head)
    return rendered


# ---------------------------------------------------------------- chunking
def _ulen(text: str) -> int:
    """Length in UTF-16 code units.

    Telegram counts UTF-16 code units, not code points: 2100 emoji are
    4200 units and blow the 4096 cap even though ``len()`` says 2100.
    """
    return len(text.encode("utf-16-le")) // 2


def _hard_split(text: str, limit: int) -> List[str]:
    """Split one over-long line into pieces of at most *limit* units."""
    out: List[str] = []
    cur: List[str] = []
    cur_len = 0
    for ch in text:
        width = 2 if ord(ch) > 0xFFFF else 1
        if cur and cur_len + width > limit:
            out.append("".join(cur))
            cur = [ch]
            cur_len = width
        else:
            cur.append(ch)
            cur_len += width
    if cur:
        out.append("".join(cur))
    return out


def _split_keepends(text: str) -> List[str]:
    """Split on "\\n" only, keeping the line break with its line.

    ``str.splitlines`` would also break on \\v, \\f, \\x85, \\u2028 and
    \\u2029 -- the pieces have to be contiguous slices of the input, so
    only the plain line feed may split.
    """
    parts = (text or "").split("\n")
    out: List[str] = []
    last = len(parts) - 1
    for idx, part in enumerate(parts):
        if idx < last:
            out.append(part + "\n")
        elif part:
            out.append(part)
    return out


def _split_lines(text: str, limit: int) -> List[str]:
    """Split one oversized block at LINE granularity.

    The pieces are CONTIGUOUS slices of the input -- the line breaks
    belong to the piece that ends with them -- so joining the pieces
    reproduces the input byte for byte.

    A URL always lives on its own line, so it is never cut in half -- as
    long as that line fits on its own. A single line longer than *limit*
    is hard-split: keeping a 5000 char URL whole would raise Telegram's
    ``BadRequest`` (4096 unit cap).
    """
    lines = _split_keepends(text)
    out: List[str] = []
    cur: List[str] = []
    cur_len = 0
    for line in lines:
        line_len = _ulen(line)
        if line_len > limit:
            if cur:
                out.append("".join(cur))
                cur = []
                cur_len = 0
            pieces = _hard_split(line, limit)
            out.extend(pieces[:-1])
            cur = [pieces[-1]]
            cur_len = _ulen(pieces[-1])
            continue
        if cur and cur_len + line_len > limit:
            out.append("".join(cur))
            cur = [line]
            cur_len = line_len
        else:
            cur.append(line)
            cur_len += line_len
    if cur:
        out.append("".join(cur))
    return out


def _split_card(block: str, limit: int) -> List[str]:
    """Split ONE card block that is bigger than the budget.

    Line granularity, but the card's header / meta lines stay together
    with as many of its link lines as fit -- the title is never left
    alone in a chunk of its own while the links go somewhere else. A URL
    is never cut in half (unless the single line is longer than the
    limit on its own, see _split_lines).

    The pieces are contiguous slices of `block`.
    """
    lines = _split_keepends(block)
    if len(lines) <= 1:
        return _split_lines(block, limit)
    split_at = -1
    for idx, line in enumerate(lines):
        if _is_link_line(line):
            split_at = idx
            break
    if split_at < 0:
        return _split_lines(block, limit)

    head = "".join(lines[:split_at])
    rest = lines[split_at:]
    out: List[str] = []

    first = rest[0]
    if _ulen(head) + _ulen(first) <= limit:
        # The header fits together with the first link line -> keep them
        # together and add as many further link lines as fit.
        piece = head + first
        piece_len = _ulen(piece)
        used = 1
        while used < len(rest):
            nxt = rest[used]
            if piece_len + _ulen(nxt) > limit:
                break
            piece = piece + nxt
            piece_len += _ulen(nxt)
            used += 1
        out.append(piece)
        rest = rest[used:]
    else:
        # The header alone already fills the budget: it has to be split.
        out.extend(_split_lines(head, limit))
    if rest:
        out.extend(_split_lines("".join(rest), limit))
    return out


def chunk_dump(text: str, limit: int = MSG_LIMIT) -> List[str]:
    """Split a rendered results message into Telegram-sized pieces.

    Block-granular, exactly like the batch flow in bot.py:2234-2256: a
    new message always starts at a card boundary, so a card is never cut
    in half. A single card that is bigger than *limit* is split at LINE
    granularity instead, so a URL is never cut in half either -- and the
    card's title stays together with the FIRST link line of that card
    (as long as the card's header block fits into one chunk; a card
    whose header alone exceeds the limit is split header first, and that
    is the only case where the title and the links can end up apart).

    The length is measured in UTF-16 code units -- the unit Telegram
    counts -- so 2100 emoji do not sneak past the cap.

    *limit* is the hard per-message budget (default ``MSG_LIMIT``). Use
    ``CHUNK_BUDGET`` (3600) when the caller still has to append a footer
    or a "(... continued)" header -- that is exactly what the repo's
    ``_BATCH_CHUNK_BUDGET`` is for.

    The chunks are contiguous slices of the input, so
    ``"".join(chunk_dump(text)) == text`` EXACTLY -- the caller does not
    have to re-insert the separator.

    NOTE: bot.py:310 ``_split_text`` is line-based and DOES cut cards in
    half -- it is deliberately not used here.
    """
    if not text:
        return []
    if limit <= 0:
        return [text]
    if _ulen(text) <= limit:
        return [text]

    sep = _JOIN_SEP
    total = len(text)
    # One span per block; every span carries the separator that follows
    # it (the last block has none), so the chunks stay contiguous slices
    # of the input.
    spans: List[Tuple[int, int]] = []
    pos = 0
    while True:
        idx = text.find(sep, pos)
        if idx < 0:
            spans.append((pos, total))
            break
        spans.append((pos, idx + len(sep)))
        pos = idx + len(sep)

    chunks: List[str] = []
    cur_start = -1
    cur_end = -1
    for start, end in spans:
        span = text[start:end]
        span_len = _ulen(span)
        if cur_start >= 0 and _ulen(text[cur_start:cur_end]) + span_len > limit:
            chunks.append(text[cur_start:cur_end])
            cur_start = -1
        if span_len > limit:
            # The block alone does not fit -> split it on its own.
            if cur_start >= 0:
                chunks.append(text[cur_start:cur_end])
                cur_start = -1
            pieces = _split_card(span, limit)
            for piece in pieces[:-1]:
                chunks.append(piece)
            last = pieces[-1]
            cur_start = end - len(last)
            cur_end = end
            continue
        if cur_start < 0:
            cur_start = start
        cur_end = end
    if cur_start >= 0:
        chunks.append(text[cur_start:cur_end])
    return chunks

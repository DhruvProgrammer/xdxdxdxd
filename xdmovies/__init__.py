"""xdmovies package — orchestration for xdmovies.wtf movie → final destination.

This file avoids eagerly importing the heavy CF-bypass chain at package
load time so `python -m xdmovies` can start without every dependency
being importable straight away. Submodules that *do* want the re-exports
(pipeline, extractor, etc.) should import them explicitly:

    from xdmovies import Card, extract_cards_async
    from xdmovies.pipeline import extract_download_links
"""
from __future__ import annotations

__all__ = [
    "Card",
    "extract_cards",
    "extract_cards_async",
    "extract_cards_from_html",
    "extract_download_links",
    "extract_download_links_with_titles",
    "quick_extract",
]


def __getattr__(name: str):  # PEP 562 -- module-level __getattr__
    """Lazy re-exports. Triggered only when *user code* accesses the
    attribute, not at `import xdmovies` time.
    """
    if name in {"Card", "extract_cards", "extract_cards_from_html", "quick_extract"}:
        from xdmovies.extractor import (
            Card,
            extract_cards,
            extract_cards_from_html,
            quick_extract,
        )
        return {
            "Card": Card,
            "extract_cards": extract_cards,
            "extract_cards_from_html": extract_cards_from_html,
            "quick_extract": quick_extract,
        }[name]
    if name in {"extract_download_links", "extract_download_links_with_titles", "extract_cards_async"}:
        from xdmovies.pipeline import (
            extract_cards_async,
            extract_download_links,
            extract_download_links_with_titles,
        )
        return {
            "extract_download_links": extract_download_links,
            "extract_download_links_with_titles": extract_download_links_with_titles,
            "extract_cards_async": extract_cards_async,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

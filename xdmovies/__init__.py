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
#  File:    xdmovies/__init__.py
#  Fingerprint: BRUNO-E1CCB1FB-7F19E5
#  ============================================================

"""xdmovies package — orchestration for xdmovies.wtf movie → final destination.

This file avoids eagerly importing the heavy CF-bypass chain at package
load time so `python -m xdmovies` can start without every dependency
being importable straight away. Submodules that *do* want the re-exports
(pipeline, extractor, etc.) should import them explicitly:

    from xdmovies import Card, extract_cards_async
    from xdmovies.pipeline import extract_download_links
"""
from __future__ import annotations

# --- authorship / provenance metadata -------------------------------------
# Machine-readable ownership record. Deliberately NOT listed in __all__, so
# star-imports behave exactly as before.
__author__ = "Bruno"
__copyright__ = "Copyright (c) 2026 Bruno. All rights reserved."
__license__ = "Proprietary - unlicensed, all rights reserved."
__watermark__ = "BRUNO-E1CCB1FB"
__origin__ = "Authored by Bruno. Copying or claiming authorship is prohibited."

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

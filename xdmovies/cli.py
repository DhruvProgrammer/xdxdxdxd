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
#  File:    xdmovies/cli.py
#  Fingerprint: BRUNO-E1CCB1FB-31FA55
#  ============================================================

"""CLI — MANUAL CF COOKIE MODE.

Usage:
    python -m xdmovies.cli "https://huh.xdmovies.wtf/movies/<slug>"
Requires a stored cookie: /setcookie or xdmovies_state/cf_cookies.json
If CF blocks, the CLI will tell you to solve and set cookie, then retry.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from typing import List, Dict, Any

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from xdmovies.pipeline import extract_download_links, extract_with_diagnostics
from xdmovies.link_follower import follow_download_link_in_browser_async
from xdmovies.link_exec import execute_button_flow
from xdmovies.cookie_store import CFChallengeError

QUALITY_RE = re.compile(r"\b(2160p|1080p|720p|480p|HDRip|BluRay|BRRip|WEB[- ]DL|WEBRip|DVDRip|x264|x265|HEVC|h264|h265|10bit|HDR10\+?)\b", re.I)

def _guess_qualities(page_html: str, n_links: int) -> List[List[str]]:
    snippets = []
    for m in re.finditer(r"(https?://link\.xdmovies\.wtf/download/[A-Za-z0-9_\-]+)", page_html):
        start = max(0, m.start() - 400)
        end = min(len(page_html), m.end() + 200)
        snippets.append(page_html[start:end])
    while len(snippets) < n_links:
        snippets.append("")
    out = []
    for snip in snippets:
        tokens = set(t.upper() if t.upper() in {"HDR10", "WEB-DL"} else t.replace(" ", "") for t in QUALITY_RE.findall(snip))
        useful = []
        for tok in sorted(tokens, key=str.upper):
            if tok.startswith("WEB-DL"):
                useful.append("WEB-DL")
            elif tok in {"1080p", "2160p", "720p", "480p"}:
                useful.append(tok)
            elif tok in {"x264", "x265", "h264", "h265", "HEVC", "10bit"}:
                useful.append(tok)
            elif tok in {"HDRip", "BluRay", "BRRip", "WEBRip", "DVDRip"}:
                useful.append(tok)
            elif "HDR10" in tok:
                useful.append("HDR10+")
        out.append(useful)
    return out

async def run(movie_url: str, headless: bool = False) -> Dict[str, Any]:
    _ = headless
    print(f"\n[xdmovies] fetching movie page with stored cookie...", file=sys.stderr)
    try:
        result = await extract_with_diagnostics(movie_url, session_id="cli_movie", headless=False)
        download_links = [c.url for c in result.cards]
        html = ""  # we already have cards; for quality we reuse result html_len not full html — fetch again is not needed, just use empty
        # quality guess without html is empty, but keep pipeline simple
        # try to get html via pipeline's fetch for quality
        try:
            from xdmovies.pipeline import fetch_rendered_html
            html = await fetch_rendered_html(movie_url, session_id="cli_movie", headless=False)
        except Exception:
            html = ""
    except CFChallengeError as e:
        return {"input_url": movie_url, "status": "cf_challenge", "reason": str(e), "host": getattr(e, "host", ""), "hint": f"Open {e.url} in your browser, solve, then: /setcookie {getattr(e,'host','xdmovies.wtf')} cf_clearance=..."}
    except Exception as e:
        return {"input_url": movie_url, "status": "no_download_links_found", "reason": f"{e.__class__.__name__}: {e}", "download_links": [], "destinations": []}

    if not download_links:
        return {"input_url": movie_url, "status": "no_download_links_found", "download_links": [], "destinations": []}

    print(f"[xdmovies] found {len(download_links)} download links", file=sys.stderr)
    quality_groups = _guess_qualities(html, len(download_links))

    destinations: List[Dict[str, Any]] = []
    for i, dlink in enumerate(download_links, 1):
        print(f"\n[xdmovies] link {i}/{len(download_links)}: resolving...", file=sys.stderr)
        try:
            url2, status2, _ = await follow_download_link_in_browser_async(dlink, session_id=f"cli_follow_{i}", headless=False)
        except CFChallengeError as e:
            destinations.append({"download_link": dlink, "qualities": quality_groups[i-1], "error": f"CF challenge: {e}", "host": getattr(e,"host",""), "hint": f"/setcookie {getattr(e,'host','')} cf_clearance=..."})
            continue
        except Exception as exc:
            destinations.append({"download_link": dlink, "qualities": quality_groups[i-1], "error": str(exc)})
            continue
        if not url2:
            destinations.append({"download_link": dlink, "qualities": quality_groups[i-1], "status": status2})
            continue
        print(f"  -> redirected to {url2}", file=sys.stderr)
        try:
            dest = await execute_button_flow(url2, session_id=f"cli_follow_{i}", headless=False)
            destinations.append({"download_link": dlink, "qualities": quality_groups[i-1], "intermediate_url": url2, "final_destination": dest})
            print(f"  -> FINAL: {dest}", file=sys.stderr)
        except CFChallengeError as e:
            destinations.append({"download_link": dlink, "qualities": quality_groups[i-1], "intermediate_url": url2, "error": f"CF challenge: {e}", "host": getattr(e,"host",""), "hint": f"/setcookie {getattr(e,'host','')} cf_clearance=..."})
        except Exception as exc:
            destinations.append({"download_link": dlink, "qualities": quality_groups[i-1], "intermediate_url": url2, "error": str(exc)})
            print(f"  -> ERR: {exc}", file=sys.stderr)

    return {"input_url": movie_url, "status": "ok", "download_links": download_links, "destinations": destinations}

def _prompt_for_url() -> str:
    print("\n=== xdmovies bypass CLI (cookie mode) ===")
    print("Paste the movie URL (e.g. https://huh.xdmovies.wtf/movies/<slug>):")
    while True:
        url = input("URL> ").strip()
        if not url:
            print("URL required.")
            continue
        if "xdmovies.wtf/movies/" in url:
            return url
        print("URL doesn't look like an xdmovies movie URL. Try again.")

def main():
    ap = argparse.ArgumentParser(description="xdmovies movie → final destination (manual cookie mode)")
    ap.add_argument("url", nargs="?", help="movie URL")
    ap.add_argument("--headless", action="store_true", help="(ignored — forced headfull)")
    args = ap.parse_args()
    movie_url = args.url or _prompt_for_url()
    result = asyncio.run(run(movie_url, headless=False))
    sys.stdout.write(json.dumps(result, indent=2) + "\n")

if __name__ == "__main__":
    main()

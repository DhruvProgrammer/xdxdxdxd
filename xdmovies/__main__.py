"""Launch the Telegram bot as a module.

Usage:
    python -m xdmovies                       # run the bot
    python -m xdmovies --check               # offline import smoke test
    python -m xdmovies --test <movie-url>    # one-shot resolve (no Telegram) — needs stored cookie
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

os.chdir(_HERE)


def _check() -> int:
    import importlib.util as ilu

    here = os.path.dirname(os.path.abspath(__file__))
    failures = []

    for name in ("bot_config", "extractor", "cookie_store", "pipeline", "bot"):
        path = os.path.join(here, f"{name}.py")
        spec = ilu.spec_from_file_location(f"xdmovies_smoke_{name}", path)
        if spec is None or spec.loader is None:
            failures.append(f"spec missing for {name}")
            continue
        mod = ilu.module_from_spec(spec)
        sys.modules.setdefault(spec.name, mod)
        try:
            spec.loader.exec_module(mod)
            print(f"OK: loaded xdmovies.{name}")
        except Exception as exc:
            failures.append(f"{name}: {exc.__class__.__name__}: {exc}")
            print(f"FAIL: {name}: {exc.__class__.__name__}: {exc}", file=sys.stderr)

    try:
        from xdmovies.bot_config import load_config
        cfg = load_config()
        print(f"OK: bot_config load: owner={cfg.owner_id} admin_ids={list(cfg.admin_ids) or '[]'}")
    except Exception as exc:
        print(f"WARN: config not loadable ({exc.__class__.__name__}: {exc}). Expected until TELEGRAM_* in .env.", file=sys.stderr)

    try:
        from xdmovies.cookie_store import status_text
        print(f"Cookies: {status_text()}")
    except Exception as exc:
        print(f"WARN: cookie_store: {exc}", file=sys.stderr)

    return 1 if failures else 0


async def _resolve_one_async(url: str) -> dict:
    import time
    import uuid

    import sys as _sys
    pkg_dir = os.path.dirname(os.path.abspath(__file__))
    if pkg_dir not in _sys.path:
        _sys.path.insert(0, pkg_dir)

    from xdmovies.pipeline import extract_with_diagnostics
    from xdmovies.link_follower import follow_download_link_in_browser_async
    from xdmovies.link_exec import execute_button_flow
    from xdmovies.cookie_store import CFChallengeError

    t0 = time.time()
    base = f"resolve_{uuid.uuid4().hex[:6]}"

    print(f"[resolve] {url}", flush=True)
    print("[step 1] fetch movie page with stored cf_clearance ...", flush=True)
    try:
        result = await extract_with_diagnostics(url, session_id=f"{base}_movie", headless=False)
    except CFChallengeError as e:
        return {"url": url, "elapsed_s": round(time.time() - t0, 2), "step": "movie_cf", "status": "cf_challenge", "reason": str(e), "host": getattr(e, "host", ""), "hint": f"Solve {e.url} in your browser and run: /setcookie {getattr(e, 'host','xdmovies.wtf')} cf_clearance=..."}
    except Exception as e:
        return {"url": url, "elapsed_s": round(time.time() - t0, 2), "step": "movie_cf", "status": "failure", "reason": f"{e.__class__.__name__}: {e}"}

    print(f"        movie fetch OK: {len(result.cards)} cards, title={result.page_title!r}", flush=True)
    from xdmovies.extractor import extract_cards
    cards = result.cards
    print(f"[step 2] {len(cards)} cards extracted", flush=True)

    out: dict = {"url": url, "cards_total": len(cards), "cards": [], "elapsed_s": 0.0, "status": "ok"}
    for i, c in enumerate(cards, 1):
        print(f"[step 3.{i}/{len(cards)}] {c.title[:60]!r} ...", flush=True)
        shared = f"{base}_link_{i}"
        try:
            url2, status, _ = await follow_download_link_in_browser_async(c.url, session_id=shared, headless=False)
        except CFChallengeError as e:
            out["cards"].append({"title": c.title, "error": f"CF challenge: {e}", "host": getattr(e, "host", ""), "hint": f"/setcookie {getattr(e,'host','')} cf_clearance=..."})
            continue
        except Exception as exc:
            out["cards"].append({"title": c.title, "error": str(exc)})
            continue
        if not url2:
            out["cards"].append({"title": c.title, "error": "follow returned empty URL"})
            continue
        print(f"        follow -> {url2} (status={status})", flush=True)
        try:
            dest = await execute_button_flow(url2, session_id=shared, headless=False)
        except CFChallengeError as e:
            out["cards"].append({"title": c.title, "redirect": url2, "error": f"CF challenge: {e}", "host": getattr(e,"host",""), "hint": f"/setcookie {getattr(e,'host','')} cf_clearance=..."})
            continue
        except Exception as exc:
            out["cards"].append({"title": c.title, "redirect": url2, "error": f"button flow: {exc.__class__.__name__}: {exc}"})
            continue
        out["cards"].append({"title": c.title, "size": c.size, "language": c.language, "redirect": url2, "destination": dest})
        print(f"        destination: {dest!r}", flush=True)

    out["elapsed_s"] = round(time.time() - t0, 2)
    return out


async def _run_test(url: str) -> int:
    try:
        result = await _resolve_one_async(url)
        print()
        print("=== RESULT ===")
        print(json.dumps(result, indent=2, default=str))
    finally:
        pass
    return 0


def _run() -> int:
    # Audit finding C6: the previous implementation built a Bot
    # directly and called bot.run() without ever acquiring the
    # single-instance lock — meaning a second `python -m xdmovies`
    # would silently race the first on the Telegram getUpdates poll
    # and trigger "Conflict: terminated by other getUpdates". Defer
    # to bot.main() which acquires the lock first.
    from xdmovies.bot import main as bot_main
    return bot_main()


def main() -> int:
    args = sys.argv[1:]
    if "--check" in args:
        return _check()
    if "--test" in args:
        idx = args.index("--test")
        if idx + 1 >= len(args):
            print("usage: python -m xdmovies --test <movie-url>", file=sys.stderr)
            return 2
        url = args[idx + 1]
        return asyncio.run(_run_test(url))
    return _run()


if __name__ == "__main__":
    raise SystemExit(main())

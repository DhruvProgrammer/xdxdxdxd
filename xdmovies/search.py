"""Title search against the xdmovies catalog search API.

Endpoint (discovered live 2026-09-07 from js/search.js):
  GET /php/get_token.php                     -> {"token": "..."} (open, no auth)
  GET /php/search_api.php?query=<q>&fuzzy=true  (headers X-Auth-Token + X-Requested-With)
    -> [{"title", "release_year", "poster", "type" ("movie"|"tv"),
         "path" ("/movies/<slug>" | "/series/<slug>"), ...}]

Plain HTTPS works -- no browser, no CF clearance needed (verified live).
Fresh token per call; one retry with a new token on auth-shaped failures.
Never returns browser-dependent data; raises SearchUnavailable on
transport/auth errors so callers can distinguish "service down" from
"no matches" (empty list).
"""
from __future__ import annotations

import asyncio
import inspect
import os
import re
from dataclasses import dataclass
from typing import List
from urllib.parse import urlsplit

_SEARCH_HOSTS = {"xdmovies.wtf", "top.xdmovies.wtf"}
_SLUG_PATH = re.compile(r"^/(movies|series)/[A-Za-z0-9][A-Za-z0-9._-]*/?$")


def _validated_base(value: str) -> str:
    configured = value or ""
    raw = configured.strip()
    if not raw or configured != raw or any(ch.isspace() or ord(ch) < 32 for ch in raw) or any(ch in raw for ch in "\\?#<>\"'"):
        raise RuntimeError("XDMOVIES_SEARCH_BASE is invalid")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError(f"XDMOVIES_SEARCH_BASE is malformed: {exc}") from exc
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in _SEARCH_HOSTS:
        raise RuntimeError("XDMOVIES_SEARCH_BASE must be an HTTPS xdmovies host")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise RuntimeError("XDMOVIES_SEARCH_BASE must not include credentials, query, or fragment")
    if parsed.path not in ("", "/") or (port is not None and port != 443):
        raise RuntimeError("XDMOVIES_SEARCH_BASE must be the HTTPS origin")
    return "https://" + (parsed.hostname or "").lower() + (f":{port}" if port else "")


SEARCH_BASE = _validated_base(os.environ.get("XDMOVIES_SEARCH_BASE", "https://top.xdmovies.wtf"))


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)).strip() or str(default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _bounded_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)).strip() or str(default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


SEARCH_MAX_RESULTS = _bounded_int("XDMOVIES_SEARCH_MAX", 30, 1, 100)
BATCH_SEARCH_DEADLINE_SECONDS = _bounded_float(
    "XDMOVIES_TXT_DEADLINE", 60.0, 5.0, 600.0
)
BATCH_SEARCH_CONCURRENCY = _bounded_int(
    "XDMOVIES_TXT_CONCURRENCY", 4, 1, 8
)


class SearchUnavailable(RuntimeError):
    pass


@dataclass
class SearchResult:
    title: str
    url: str
    year: str = ""
    kind: str = ""  # "movie" | "series"


@dataclass
class BatchSearchResult:
    results: List[SearchResult]
    no_match: List[str]
    failed: List[str]
    processed: int
    timed_out: bool
    cancelled: bool


def _abs_url(path: str) -> str:
    p = path or ""
    if not p or p != p.strip() or any(ch in p for ch in "\\<>\"'"):
        return ""
    if p.startswith("https://"):
        try:
            parsed = urlsplit(p)
        except ValueError:
            return ""
        if (parsed.hostname or "").lower() not in _SEARCH_HOSTS:
            return ""
        if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            return ""
        try:
            port = parsed.port
        except ValueError:
            return ""
        if port not in (None, 443):
            return ""
        if not _SLUG_PATH.fullmatch(parsed.path):
            return ""
        return "https://" + (parsed.hostname or "").lower() + parsed.path
    if p.startswith("http://") or "://" in p or not p.startswith("/"):
        return ""
    if not _SLUG_PATH.fullmatch(p):
        return ""
    return SEARCH_BASE + p


def _response_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return ""
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in _SEARCH_HOSTS:
        return ""
    if parsed.username is not None or parsed.password is not None or port not in (None, 443):
        return ""
    return "https://" + (parsed.hostname or "").lower() + (f":{port}" if port else "")


def parse_search_items(items, limit: int = SEARCH_MAX_RESULTS) -> List[SearchResult]:
    """Pure parser: API JSON list -> SearchResult list. No I/O (testable)."""
    try:
        limit = min(SEARCH_MAX_RESULTS, max(1, int(limit)))
    except (TypeError, ValueError):
        limit = SEARCH_MAX_RESULTS
    out: List[SearchResult] = []
    try:
        seq = list(items or [])
    except Exception:
        return out
    for it in seq:
        if len(out) >= limit:
            break
        try:
            if not isinstance(it, dict):
                continue
            title = str(it.get("title") or "").strip()
            url = _abs_url(str(it.get("path") or ""))
            if not title or not url:
                continue
            year = str(it.get("release_year") or "").strip()
            raw_kind = str(it.get("type") or "").strip().lower()
            kind = "series" if raw_kind in ("tv", "show", "series") else "movie"
            out.append(SearchResult(title=title, url=url, year=year, kind=kind))
        except Exception:
            continue
    return out


async def _fetch_token(client) -> str:
    resp = await client.get(f"{SEARCH_BASE}/php/get_token.php")
    if _response_origin(str(resp.url)) != _response_origin(SEARCH_BASE):
        raise SearchUnavailable("token endpoint redirected off the configured origin")
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception as exc:
        raise SearchUnavailable(f"token endpoint returned non-JSON: {exc!r}")
    token = str((data or {}).get("token") or "").strip()
    if not token:
        raise SearchUnavailable("token endpoint returned no token")
    return token


async def _query(client, token: str, query: str):
    resp = await client.get(
        f"{SEARCH_BASE}/php/search_api.php",
        params={"query": query, "fuzzy": "true"},
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/json",
            "X-Auth-Token": token,
        },
    )
    if _response_origin(str(resp.url)) != _response_origin(SEARCH_BASE):
        raise SearchUnavailable("search endpoint redirected off the configured origin")
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception as exc:
        raise SearchUnavailable(f"search endpoint returned non-JSON: {exc!r}")


async def search_titles(
    query: str,
    limit: int = SEARCH_MAX_RESULTS,
    *,
    client=None,
    timeout: float = 20.0,
) -> List[SearchResult]:
    """Search the catalog; returns matches (possibly empty). Raises
    SearchUnavailable when the service itself is unreachable/broken."""
    import httpx

    q = (query or "").strip()
    if not q:
        return []
    owned = client is None
    cli = client or httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    try:
        try:
            token = await _fetch_token(cli)
            items = await _query(cli, token, q)
        except SearchUnavailable:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise SearchUnavailable(f"search request failed: {exc!r}")
        if isinstance(items, dict) and items.get("error"):
            token = await _fetch_token(cli)
            items = await _query(cli, token, q)
        if isinstance(items, dict) and items.get("error"):
            raise SearchUnavailable(f"search API error: {items.get('error')}")
        return parse_search_items(items, limit=limit)
    except SearchUnavailable:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise SearchUnavailable(f"search failed: {exc!r}")
    finally:
        if owned:
            await cli.aclose()


async def search_titles_batch(
    titles: List[str],
    *,
    per_title_limit: int = 5,
    deadline: float = BATCH_SEARCH_DEADLINE_SECONDS,
    max_concurrency: int = BATCH_SEARCH_CONCURRENCY,
    result_cap: int = SEARCH_MAX_RESULTS,
    progress=None,
    cancel_event: "asyncio.Event | None" = None,
) -> BatchSearchResult:
    import httpx

    values = []
    seen_titles = set()
    for title in titles or []:
        value = str(title or "").strip()
        if value and value not in seen_titles:
            seen_titles.add(value)
            values.append(value)
    if not values:
        return BatchSearchResult([], [], [], 0, False, False)

    loop = asyncio.get_running_loop()
    deadline_at = loop.time() + max(0.0, float(deadline))
    semaphore = asyncio.Semaphore(
        min(max(1, int(max_concurrency)), BATCH_SEARCH_CONCURRENCY, len(values))
    )
    cancelled = bool(cancel_event is not None and cancel_event.is_set())
    timed_out = False
    results: List[SearchResult] = []
    no_match: List[str] = []
    failed: List[str] = []
    seen_urls = set()
    processed = 0
    client = httpx.AsyncClient(
        timeout=20.0,
        follow_redirects=False,
        headers={"User-Agent": "Mozilla/5.0"},
    )

    async def run_one(title: str):
        if cancel_event is not None and cancel_event.is_set():
            return title, [], "cancelled", False
        async with semaphore:
            if cancel_event is not None and cancel_event.is_set():
                return title, [], "cancelled", False
            remaining = deadline_at - loop.time()
            if remaining <= 0:
                return title, [], "timeout", True
            try:
                items = await asyncio.wait_for(
                    search_titles(
                        title,
                        limit=per_title_limit,
                        client=client,
                        timeout=min(20.0, remaining),
                    ),
                    timeout=remaining,
                )
                return title, items, "ok", False
            except (asyncio.TimeoutError, TimeoutError):
                return title, [], "timeout", True
            except asyncio.CancelledError:
                raise
            except Exception:
                return title, [], "failed", False

    tasks = {loop.create_task(run_one(title)): title for title in values}
    cancel_wait = asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
    try:
        pending = set(tasks)
        while pending:
            remaining = deadline_at - loop.time()
            if remaining <= 0 or cancel_event is not None and cancel_event.is_set():
                timed_out = remaining <= 0
                cancelled = not timed_out
                break
            wait_set = set(pending)
            if cancel_wait is not None:
                wait_set.add(cancel_wait)
            done, pending = await asyncio.wait(
                wait_set,
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_wait is not None and cancel_wait in done:
                cancel_wait = None
                cancelled = True
                pending = set(pending) - set(done)
                break
            if not done:
                timed_out = True
                break
            for task in done:
                if task is cancel_wait:
                    continue
                title = tasks[task]
                try:
                    title, items, outcome, task_timed_out = task.result()
                except asyncio.CancelledError:
                    cancelled = True
                    continue
                processed += 1
                timed_out = timed_out or task_timed_out
                if outcome == "ok":
                    added = False
                    for item in items:
                        if len(results) >= max(0, int(result_cap)):
                            break
                        if item.url in seen_urls:
                            continue
                        seen_urls.add(item.url)
                        results.append(item)
                        added = True
                    if not items:
                        no_match.append(title)
                elif outcome == "timeout":
                    failed.append(title)
                elif outcome == "failed":
                    failed.append(title)
                elif outcome == "cancelled":
                    cancelled = True
            if progress is not None:
                value = progress(processed, len(values))
                if inspect.isawaitable(value):
                    await value
        if pending:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        return BatchSearchResult(
            results=results,
            no_match=no_match,
            failed=failed,
            processed=processed,
            timed_out=timed_out,
            cancelled=cancelled or bool(cancel_event is not None and cancel_event.is_set()),
        )
    except asyncio.CancelledError:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks.keys(), return_exceptions=True)
        raise
    finally:
        if cancel_wait is not None and not cancel_wait.done():
            cancel_wait.cancel()
            await asyncio.gather(cancel_wait, return_exceptions=True)
        await client.aclose()

"""Auto-detect loop — poll xdmovies catalog every 30m for new posts.

Owner toggles via /autodetect (feed picker: all | movies | series).
When ON:
  every CHECK_INTERVAL (30m) fetch the chosen catalog page with the
  stored cf_clearance (CloakBrowser via pipeline.fetch_rendered_html),
  extract canonical /movies|/series detail URLs, diff against
  autodetect_store.seen_urls + the live queue, and enqueue each truly
  new URL as a silent channel job (post_to_channel from
  scrape_all.load_channel). _run_for_user then does the full resolve
  (extract cards -> follow -> gate) and posts the batch to the channel.

30m (not 5m) deliberately: each tick costs one CloakBrowser page load;
30m keeps VPS bandwidth tiny while still catching posts the same day.

CF challenge on the catalog -> one owner solve-link + 120s wait, then
re-loop the same tick; if still blocked, skip this tick (never crash).
"""
from __future__ import annotations

import asyncio
import time
from typing import List, Optional

from xdmovies.logger_utils import logger

CHECK_INTERVAL = 30 * 60  # 30 minutes — bandwidth-friendly
STARTUP_GRACE = 20        # let PTB finish post_init before first check
MAX_NEW_PER_TICK = 5      # flood guard: oldest-first, rest next tick

_task: Optional[asyncio.Task] = None
_stop_event: Optional[asyncio.Event] = None
_bot: Optional[object] = None  # the Bot instance (xdmovies.bot.Bot)


def _catalog_url(feed: str) -> str:
    from xdmovies.scrape_all import CATALOG_URLS
    return CATALOG_URLS.get(feed, CATALOG_URLS["all"])


async def _collect_live_urls_async(bot) -> set:
    out: set = set()
    try:
        cur = getattr(bot, "current", None)
        if cur is not None and getattr(cur, "url", ""):
            out.add(cur.url)
    except Exception:
        pass
    try:
        if hasattr(bot, "_snapshot_queue"):
            for j in (await bot._snapshot_queue()) or []:
                if getattr(j, "url", ""):
                    out.add(j.url)
    except Exception:
        pass
    try:
        if hasattr(bot, "_snapshot_urgent"):
            for j in (await bot._snapshot_urgent()) or []:
                if getattr(j, "url", ""):
                    out.add(j.url)
    except Exception:
        pass
    return out


async def _check_once(bot) -> tuple[List[str], str]:
    """One poll. Returns (new_urls_to_enqueue, reason). Never raises."""
    try:
        from xdmovies import autodetect_store as store
    except Exception as exc:
        return [], f"store_import:{exc}"

    if not store.is_on():
        return [], "off"

    try:
        from xdmovies.scrape_all import load_channel
        ch = load_channel()
    except Exception:
        ch = None
    cid = getattr(ch, "channel_id", 0) if ch else 0
    if not cid:
        logger.info("[autodetect] ON but no channel preset — skipping tick")
        store.touch_check(0)
        return [], "no_channel"

    feed = store.get_feed()
    url = _catalog_url(feed)
    seen = set(store.load_seen())
    first_run = not seen  # empty seen -> baseline, no retroactive scrape

    try:
        from xdmovies.pipeline import fetch_rendered_html
        from xdmovies.scrape_all import extract_catalog_urls
        html = await fetch_rendered_html(url, session_id="autodetect", headless=False)
        page_urls = extract_catalog_urls(html, feed, url)
    except Exception as exc:
        # CFChallengeError or fetch failure: log + skip tick (loop survives).
        name = type(exc).__name__
        if name == "CFChallengeError":
            # One owner solve attempt, then re-check same tick once.
            try:
                from xdmovies.cookie_store import CFChallengeError  # noqa: F401
                owner = getattr(getattr(bot, "cfg", None), "owner_id", 0)
                token, _m = await bot._send_solve_link(
                    owner, getattr(exc, "url", "") or url, "xdmovies.wtf", owner
                )
                if token and await bot._wait_for_solve(token, timeout=120.0):
                    html = await fetch_rendered_html(url, session_id="autodetect", headless=False)
                    page_urls = extract_catalog_urls(html, feed, url)
                else:
                    store.touch_check(0)
                    return [], "cf_unsolved"
            except Exception as e2:
                logger.warning(f"[autodetect] CF solve retry failed: {e2}")
                store.touch_check(0)
                return [], "cf_error"
        else:
            logger.warning(f"[autodetect] catalog fetch failed: {name}: {exc}")
            store.touch_check(0)
            return [], f"fetch_{name}"

    if first_run:
        # Baseline: everything currently listed counts as already-seen.
        store.add_seen(page_urls)
        store.touch_check(0)
        logger.info(f"[autodetect] baseline seeded with {len(page_urls)} urls (feed={feed})")
        return [], "baselined"

    live = await _collect_live_urls_async(bot)
    new_urls = [u for u in page_urls if u not in seen and u not in live]
    new_urls = new_urls[:MAX_NEW_PER_TICK]
    if new_urls:
        store.add_seen(new_urls)  # mark seen at enqueue time (no re-queue on crash)
    store.touch_check(len(new_urls))
    return new_urls, "new" if new_urls else "clean"


def _enqueue_jobs(bot, urls: List[str], channel_id: int, owner_id: int,
                  card_filter: str = "all") -> None:
    """Build one silent channel Job per URL and push to the queue.

    Fire-and-forget via asyncio.create_task on the running loop's _enqueue.
    Reuses the scrape_all child pattern: post_to_channel + silent=True
    skips qmode/dmode pickers and progress chatter; batch posts to channel.
    card_filter ("all" | "episodes" | "packs") narrows series cards via
    the existing extractor.filter_cards_by_content path in _run_for_user.
    """
    try:
        from xdmovies.bot import Job  # local import to avoid cycle at module load
    except Exception:
        Job = None
    if Job is None or not urls:
        return
    if card_filter not in ("all", "episodes", "packs"):
        card_filter = "all"
    for u in urls:
        try:
            job = Job(
                user_id=owner_id,
                chat_id=channel_id,
                url=u,
                user_name="autodetect",
                username="",
                post_to_channel=channel_id,
                silent=True,
                card_filter=card_filter,
            )
            asyncio.get_running_loop().create_task(bot._enqueue(job))
        except Exception as e:
            logger.warning(f"[autodetect] enqueue failed for {u}: {e}")


async def _loop(bot) -> None:
    logger.info(
        f"[autodetect] started (check every {CHECK_INTERVAL // 60}m, "
        f"cap {MAX_NEW_PER_TICK}/tick)"
    )
    await asyncio.sleep(STARTUP_GRACE)
    while True:
        try:
            if _stop_event and _stop_event.is_set():
                break
            urls, reason = await _check_once(bot)
            if urls:
                try:
                    from xdmovies.scrape_all import load_channel
                    ch = load_channel()
                    cid = getattr(ch, "channel_id", 0) if ch else 0
                except Exception:
                    cid = 0
                owner = getattr(getattr(bot, "cfg", None), "owner_id", 0)
                # Series feed applies the owner's content filter (all /
                # episodes / packs); movies + all feeds resolve everything.
                try:
                    from xdmovies import autodetect_store as _ad
                    cf = _ad.get_card_filter() if _ad.get_feed() == "series" else "all"
                except Exception:
                    cf = "all"
                if cid:
                    _enqueue_jobs(bot, urls, cid, owner, card_filter=cf)
                    logger.info(
                        f"[autodetect] enqueued {len(urls)} new url(s) "
                        f"-> channel {cid} ({reason}, content={cf})"
                    )
            elif reason not in ("off", "clean", "baselined"):
                logger.debug(f"[autodetect] tick skipped: {reason}")
            await asyncio.sleep(CHECK_INTERVAL)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.exception(f"[autodetect] loop error: {e}")
            await asyncio.sleep(60)


async def start_autodetect(bot) -> None:
    """Schedule the loop. Pass the Bot instance (needs queue + cfg)."""
    global _task, _stop_event, _bot
    if _task and not _task.done():
        return
    _bot = bot
    _stop_event = asyncio.Event()
    _task = asyncio.create_task(_loop(bot))
    logger.info("[autodetect] task scheduled")


async def stop_autodetect() -> None:
    global _task, _stop_event
    if _stop_event:
        _stop_event.set()
    if _task:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
    logger.info("[autodetect] stopped")

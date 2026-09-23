from __future__ import annotations

import asyncio
import inspect
from typing import Iterable, Optional
from urllib.parse import urlsplit


GATE_HOST = "latestnewsonline.sbs"
DEST_HOST_SUFFIXES = (
    "hubcloud.one",
    "hubdrive.click",
    "hubdrive.pro",
    "gdflix.to",
    "gdflix.net",
    "gofile.io",
    "vcloud.me",
    "drive.google.com",
    "fdrive.link",
    "fdrive.icu",
    "pureddl.com",
    "faststream.to",
    "gamerxyt.com",
    "gamerxyt.net",
)
AD_HOST_HINTS = (
    "glovefork.info",
    "getsmartyapp.com",
    "aichouphaugn.com",
    "crn77.com",
    "dd133.com",
    "aqle3.com",
    "d2n5v45616cm6u.cloudfront.net",
    "sootoarathus.net",
)


class BrowserNavigationError(RuntimeError):
    pass


class UnsafeDestinationError(RuntimeError):
    pass


def url_host(url: str) -> str:
    try:
        parsed = urlsplit(str(url or "").strip())
        if parsed.scheme.lower() not in ("http", "https"):
            return ""
        return (parsed.hostname or "").lower().rstrip(".")
    except (TypeError, ValueError):
        return ""


def is_ad_url(url: str) -> bool:
    host = url_host(url)
    if not host:
        return False
    return any(host == item or host.endswith("." + item) for item in AD_HOST_HINTS)


def is_destination_url(url: str) -> bool:
    host = url_host(url)
    if not host or is_ad_url(url):
        return False
    return any(host == item or host.endswith("." + item) for item in DEST_HOST_SUFFIXES)


def is_gate_transit(url: str) -> bool:
    try:
        parsed = urlsplit(str(url or "").strip())
    except ValueError:
        return False
    return (
        parsed.scheme in ("http", "https")
        and (parsed.hostname or "").lower().rstrip(".") == GATE_HOST
        and parsed.path.startswith("/go/")
    )


def validate_destination_url(url: str) -> str:
    value = str(url or "").strip()
    if not is_destination_url(value):
        raise UnsafeDestinationError(f"refusing unknown or advertising destination: {value[:160]}")
    parsed = urlsplit(value)
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeDestinationError("destination URLs containing credentials are not allowed")
    return value


def validate_gate_destination_url(url: str) -> str:
    value = str(url or "").strip()
    host = url_host(value)
    parsed = urlsplit(value)
    if (
        not value
        or host != GATE_HOST
        or is_ad_url(value)
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise UnsafeDestinationError(f"follower ended at an invalid gate destination: {value[:160]}")
    return value


def _status_code(response) -> int:
    try:
        return int(getattr(response, "status", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _response_headers(response) -> dict:
    try:
        headers = getattr(response, "headers", None)
        if callable(headers):
            headers = headers()
        return {str(k).lower(): str(v) for k, v in dict(headers or {}).items()}
    except Exception:
        return {}


async def is_cf_challenge_page(page, response=None) -> bool:
    title = ""
    html = ""
    frame_urls = []
    selector_count = 0
    try:
        title = str(await page.title() or "")
    except Exception:
        pass
    try:
        html = str(await page.content() or "")
    except Exception:
        pass
    try:
        frame_urls = [str(frame.url or "").lower() for frame in page.frames]
    except Exception:
        pass
    try:
        selector_count = int(await page.locator(
            "#challenge-form, #cf-challenge-running, #challenge-running, "
            "form[action*='cdn-cgi/challenge-platform'], input[name='cf_challenge_response']"
        ).count() or 0)
    except Exception:
        pass
    title_lower = title.lower()
    html_lower = html.lower()
    status = _status_code(response)
    headers = _response_headers(response)
    if any(marker in title_lower for marker in (
        "just a moment",
        "checking your browser",
        "attention required",
        "ddos protection",
        "security check",
        "verify you are human",
        "verifying you are human",
    )):
        return True
    if any(marker in html_lower for marker in (
        'name="cf-mitigated"',
        "cf-browser-verification",
        "checking your browser before accessing",
        "enable javascript and cookies to continue",
        "verify you are human",
        "verifying you are human",
        "cf_chl_opt",
        "__cf_chl_",
    )):
        return True
    if selector_count > 0:
        return True
    active_platform_frame = any(
        "challenges.cloudflare.com" in item
        or "/cdn-cgi/challenge-platform/" in item
        or "/turnstile/" in item
        for item in frame_urls
    )
    if active_platform_frame and len(html.strip()) < 12000 and "cloudflare" in html_lower:
        return True
    if status in (403, 429, 503):
        if any(key in headers for key in ("cf-ray", "cf-mitigated", "cf-chl-bypass")):
            return True
        if "cloudflare" in html_lower and "ray id" in html_lower:
            return True
    return False


async def cancel_tasks(tasks: Iterable[Optional[asyncio.Task]], timeout: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, float(timeout))
    owned = []
    seen = set()
    for task in tasks:
        if task is None or id(task) in seen:
            continue
        seen.add(id(task))
        owned.append(task)
        if not task.done():
            task.cancel()
    if not owned:
        return
    _, pending = await asyncio.wait(owned, timeout=max(0.0, deadline - loop.time()))
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.wait(pending, timeout=min(1.0, max(0.0, deadline - loop.time())))
    for task in owned:
        if task.done() and not task.cancelled():
            task.exception()


async def close_browser_objects(objects: Iterable[object], timeout: float = 10.0) -> None:
    async def _close() -> None:
        closed = []
        per_object_timeout = max(0.0, float(timeout))
        for obj in objects:
            if obj is None or any(obj is item for item in closed):
                continue
            close = getattr(obj, "close", None)
            if not callable(close):
                continue
            closed.append(obj)
            try:
                result = close()
            except BaseException:
                continue
            if not inspect.isawaitable(result):
                continue
            operation = asyncio.create_task(result)
            done, _ = await asyncio.wait({operation}, timeout=per_object_timeout)
            if done:
                try:
                    operation.exception()
                except BaseException:
                    pass
            else:
                operation.cancel()
                await asyncio.wait({operation}, timeout=min(0.25, per_object_timeout))

    cleanup = asyncio.create_task(_close())
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        try:
            await cleanup
        finally:
            raise


async def await_task_bounded(task: asyncio.Task, timeout: float) -> object:
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=max(0.0, float(timeout)))
    except BaseException:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise


__all__ = [
    "AD_HOST_HINTS",
    "BrowserNavigationError",
    "DEST_HOST_SUFFIXES",
    "GATE_HOST",
    "UnsafeDestinationError",
    "await_task_bounded",
    "cancel_tasks",
    "close_browser_objects",
    "is_ad_url",
    "is_cf_challenge_page",
    "is_destination_url",
    "is_gate_transit",
    "url_host",
    "validate_destination_url",
    "validate_gate_destination_url",
]

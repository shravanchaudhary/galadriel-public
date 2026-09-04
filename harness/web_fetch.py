"""Read a web page: fast extractor API first, the paired browser as the fallback.

Two steps, in order:

  1. The self-hosted Trafilatura Lambda — stateless, cheap, no browser needed.
     Text only; it cannot return raw HTML.
  2. The user's own paired Chrome. One tab is opened on first use and reused for
     every later fetch, so reading pages never piles up tabs. This is the step
     that reaches JS-only, bot-checked and login-walled pages, because it runs
     in a browser the user is already signed into.

If no browser is online, the caller gets a note asking the user to turn it on.
"""

import asyncio
import json
import logging
import os
import re
import shlex
import time
from urllib.parse import urlsplit
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Keep in-flight requests below the Trafilatura Lambda's reserved concurrency
# (currently 10). 8 leaves headroom for bursts and avoids AWS throttling (429s).
_LAMBDA_SEMAPHORE = asyncio.Semaphore(8)


async def _trafilatura_get_text(url: str, timeout: float = 10.0) -> Optional[str]:
    """Extract page text via the self-hosted Trafilatura Lambda. Cheapest/fastest."""
    endpoint = os.environ.get("TRAFILATURA_ENDPOINT")
    api_key = os.environ.get("TRAFILATURA_API_KEY")
    if not endpoint or not api_key:
        return None
    async with _LAMBDA_SEMAPHORE:
        try:
            headers = {"x-api-key": api_key, "Content-Type": "application/json"}
            payload = {"url": url, "timeout": timeout}
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    endpoint, headers=headers, json=payload, timeout=timeout + 10.0
                )
                response.raise_for_status()
                result = response.json()
            if not result.get("success"):
                logger.warning(f"Trafilatura returned success=False for {url}")
                return None
            return result.get("content") or None
        except Exception as e:
            logger.error(f"Trafilatura extraction failed for {url}: {e}")
            return None


# What the browser step reads off the loaded page, by mode.
_PAGE_EXPR = {
    "text": "document.body.innerText",
    "raw": "document.documentElement.outerHTML",
}

# Give a page this long to finish loading before reading it anyway — a partly
# rendered page is still worth more than nothing.
_LOAD_TIMEOUT_SECONDS = 10.0

# The tab this module drives, per browser profile: profile_id -> (index, url).
# The index alone is not an identity — indices shift when the user closes a tab
# — so it is re-confirmed against the url we left there before every reuse. A
# mismatch means open a fresh tab, never navigate one we did not open.
_FETCH_TABS: dict[str, tuple[int, str]] = {}

# Reading one tab's location costs ~1s against a real extension; listing tabs
# costs ~15s and sometimes times out. So reuse is confirmed with a location
# read, and `tab list` is paid only when a tab has to be found.
_HREF = 'eval "location.href"'

_TAB_LINE = re.compile(r"^\[(\d+)\]\s+(.*)$")


def _parse_tabs(output: str) -> list[tuple[int, str, bool]]:
    """Parse `tab list` output — `[<index>] <title> — <url>`, `*` marks active."""
    tabs: list[tuple[int, str, bool]] = []
    for line in output.splitlines():
        match = _TAB_LINE.match(line.strip())
        if match:
            rest = match.group(2).rstrip()
            active = rest.endswith("*")
            url = rest.rstrip("* ").rsplit(" — ", 1)[-1].strip()
            tabs.append((int(match.group(1)), url, active))
    return tabs


def _same_url(a: str, b: str) -> bool:
    return a.rstrip("/").lower() == b.rstrip("/").lower()


def _host(url: str) -> str:
    return urlsplit(url).hostname.removeprefix("www.").lower() if urlsplit(url).hostname else ""


def _offline_note(url: str, status: dict) -> str:
    detail = status.get("error") or f"state: {status.get('state')}"
    return (
        f"[browser offline] Could not read {url}: fast extraction returned "
        f"nothing and no browser is available to open the page ({detail}).\n\n"
        "Please turn the browser on — open Chrome, click the extension and "
        "toggle Agent ON (status: Connected) — then retry. `browser_devices` "
        "with action=list shows what is paired."
    )


async def _fetch_tab(profile_id: str, url: str, run) -> tuple[int, bool, Optional[str]]:
    """(index, already_navigated, error) for the tab this module reads pages in.

    A fresh tab is opened straight at `url`, both to save a round trip and so it
    can be picked out of the listing that follows.
    """
    noted = _FETCH_TABS.get(profile_id)
    if noted and _same_url((await run(_HREF, noted[0])).strip(), noted[1]):
        return noted[0], False, None

    # `tab new` reports a timeout on an extension that opened the tab anyway,
    # so its ack is ignored — the listing is what identifies the tab.
    await run(f"tab new {shlex.quote(url)}")
    listing = await run("tab list")
    tabs = _parse_tabs(listing)
    if not tabs:
        listing = await run("tab list")  # one retry — listing can time out
        tabs = _parse_tabs(listing)
    if not tabs:
        return 0, False, f"[error] could not list browser tabs: {listing}"
    matches = [tab for tab in tabs if _same_url(tab[1], url)]
    picked = (
        next((tab for tab in matches if tab[2]), None)
        or (matches[-1] if matches else None)
        or next((tab for tab in tabs if tab[2]), None)
    )
    if picked is None:
        return 0, False, f"[error] browser did not open a tab for {url}."
    return picked[0], True, None


async def _browser_read(url: str, mode: str) -> str:
    """Load `url` in this module's tab and return its text or HTML."""
    from . import browser_devices
    from .tools import _lock_for, _run_browser

    status = await asyncio.to_thread(browser_devices.status)
    if not status.get("online"):
        return _offline_note(url, status)
    profile_id = status.get("profile_id") or "main"

    async def run(args: str, tab: Optional[int] = None) -> str:
        output = await _run_browser(args, None, tab)
        return (output if isinstance(output, str) else str(output)).strip()

    async with _lock_for(f"fetch-{profile_id}"):
        index, navigated, err = await _fetch_tab(profile_id, url, run)
        if err:
            return err

        if not navigated:
            opened = await run(f"open {shlex.quote(url)}", index)
            if opened.startswith("[error]"):
                return f"[error] browser could not open {url}: {opened}"

        deadline = time.monotonic() + _LOAD_TIMEOUT_SECONDS
        while True:
            ready = (await run('eval "document.readyState"', index)).strip()
            if ready == "complete" or ready.startswith("[error]"):
                break
            if time.monotonic() >= deadline:
                logger.info(f"fetch_url_data: {url} still {ready!r} after 10s — reading anyway")
                break
            await asyncio.sleep(0.5)

        # location.href comes back with the content so the tab can be identified
        # by where it actually landed (redirects included) on the next fetch.
        js = f"JSON.stringify({{url: location.href, content: ({_PAGE_EXPR[mode]}) || ''}})"
        payload = await run(f"eval {shlex.quote(js)}", index)
        try:
            data = json.loads(payload)
        except ValueError:
            return f"[error] browser could not read {url}: {payload}"
        landed = data.get("url") or url
        if _host(landed) != _host(url):
            # The browser is shared — the user and other agents drive the same
            # window — so the page under us can change between opening it and
            # reading it. Returning that content as if it were the requested
            # page is the one failure that must never be silent, and noting the
            # tab would hand a stranger's tab to the next fetch.
            return (
                f"[wrong page] Asked for {url} but the browser is showing "
                f"{landed}. This window is shared, so it may have moved while "
                "the page was loading — retry. If the url legitimately "
                f"redirects there, fetch {landed} directly."
            )
        _FETCH_TABS[profile_id] = (index, landed)

    content = (data.get("content") or "").strip()
    if not content:
        return (
            f"[no content] The browser loaded {data.get('url') or url} but the page "
            "has no readable content. It may be blocked (login wall, CAPTCHA, bot "
            "check). Inspect it with the browser tool, or — if the page is "
            "essential — ask the user to unblock it in their live window."
        )
    return content


async def fetch_url_data(url: str, mode: str = "text") -> str:
    """Read a page as text (`mode="text"`) or full HTML (`mode="raw"`).

    Trafilatura first: stateless and fast, but text-only, so `raw` goes straight
    to the browser. Whatever Trafilatura cannot reach falls through to the tab.
    Always returns something the model can act on — content, or a note saying
    what to do about it.
    """
    mode = (mode or "text").strip().lower()
    if mode not in _PAGE_EXPR:
        return f"[error] mode must be 'text' or 'raw', got {mode!r}."
    if mode == "text":
        content = await _trafilatura_get_text(url)
        if content and content.strip():
            logger.info(f"fetch_url_data: trafilatura extracted {url}")
            return content
    logger.info(f"fetch_url_data: reading {url} in the browser (mode={mode})")
    return await _browser_read(url, mode)

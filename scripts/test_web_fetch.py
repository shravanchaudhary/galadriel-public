#!/usr/bin/env python3
"""Regression tests for harness.web_fetch.fetch_url_data.

Covers the two-step contract: Trafilatura first for text, the browser tab for
everything it can't reach and for every `raw` fetch — plus the tab-identity rule
that keeps the module from ever navigating a tab it did not open.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import web_fetch  # noqa: E402


def test_trafilatura_short_circuits_browser() -> bool:
    """A usable Trafilatura result must be returned without touching the browser."""
    print("=== test: trafilatura hit skips the browser ===")

    async def _run() -> bool:
        traf = AsyncMock(return_value="  hello from page  ")
        browser = AsyncMock(return_value="should not be called")
        with patch.object(web_fetch, "_trafilatura_get_text", traf), \
             patch.object(web_fetch, "_browser_read", browser):
            out = await web_fetch.fetch_url_data("https://example.com")
        if out != "  hello from page  ":
            print(f"FAIL: unexpected content: {out!r}")
            return False
        traf.assert_awaited_once_with("https://example.com")
        browser.assert_not_awaited()
        print("PASS")
        return True

    return asyncio.run(_run())


def test_falls_through_to_browser() -> bool:
    print("\n=== test: empty trafilatura falls through to the browser ===")

    async def _run() -> bool:
        traf = AsyncMock(return_value=None)
        browser = AsyncMock(return_value="page text from the tab")
        with patch.object(web_fetch, "_trafilatura_get_text", traf), \
             patch.object(web_fetch, "_browser_read", browser):
            out = await web_fetch.fetch_url_data("https://example.com")
        if out != "page text from the tab":
            print(f"FAIL: unexpected content: {out!r}")
            return False
        browser.assert_awaited_once_with("https://example.com", "text")
        print("PASS")
        return True

    return asyncio.run(_run())


def test_raw_mode_skips_trafilatura() -> bool:
    """Trafilatura cannot return HTML, so `raw` must go straight to the browser."""
    print("\n=== test: mode=raw bypasses trafilatura ===")

    async def _run() -> bool:
        traf = AsyncMock(return_value="text, not html")
        browser = AsyncMock(return_value="<html>...</html>")
        with patch.object(web_fetch, "_trafilatura_get_text", traf), \
             patch.object(web_fetch, "_browser_read", browser):
            out = await web_fetch.fetch_url_data("https://example.com", "raw")
        if out != "<html>...</html>":
            print(f"FAIL: unexpected content: {out!r}")
            return False
        traf.assert_not_awaited()
        browser.assert_awaited_once_with("https://example.com", "raw")
        print("PASS")
        return True

    return asyncio.run(_run())


def test_bad_mode_is_rejected() -> bool:
    print("\n=== test: unknown mode returns an error, not a fetch ===")

    async def _run() -> bool:
        browser = AsyncMock(return_value="nope")
        with patch.object(web_fetch, "_browser_read", browser):
            out = await web_fetch.fetch_url_data("https://example.com", "markdown")
        if not out.startswith("[error] mode must be"):
            print(f"FAIL: unexpected output: {out!r}")
            return False
        browser.assert_not_awaited()
        print("PASS")
        return True

    return asyncio.run(_run())


def test_parse_tabs() -> bool:
    """`tab list` lines must parse into (index, url) — the tab's identity."""
    print("\n=== test: tab list parsing ===")
    listing = (
        "[0] Inbox (12) — https://mail.example.com/u/0 *\n"
        "[1] Some Article — Long Title — https://news.example.com/a/1\n"
        "(ignored line)\n"
    )
    tabs = web_fetch._parse_tabs(listing)
    expected = [
        (0, "https://mail.example.com/u/0", True),
        (1, "https://news.example.com/a/1", False),
    ]
    if tabs != expected:
        print(f"FAIL: {tabs!r} != {expected!r}")
        return False
    print("PASS")
    return True


def test_reuses_noted_tab() -> bool:
    """A noted tab still holding our url is reused — cheaply, without listing."""
    print("\n=== test: noted tab is reused ===")

    async def _run() -> bool:
        calls: list[tuple[str, int | None]] = []

        async def run(args: str, tab: int | None = None) -> str:
            calls.append((args, tab))
            return "https://fetched.example/"

        web_fetch._FETCH_TABS["main"] = (1, "https://fetched.example")
        index, navigated, err = await web_fetch._fetch_tab("main", "https://new.example", run)
        if err or index != 1 or navigated:
            print(f"FAIL: index={index!r} navigated={navigated!r} err={err!r}")
            return False
        if calls != [(web_fetch._HREF, 1)]:
            print(f"FAIL: unexpected browser calls: {calls!r}")
            return False
        print("PASS")
        return True

    try:
        return asyncio.run(_run())
    finally:
        web_fetch._FETCH_TABS.pop("main", None)


def test_opens_new_tab_when_note_is_stale() -> bool:
    """A tab that no longer holds our url must never be hijacked."""
    print("\n=== test: stale note opens a new tab ===")

    async def _run() -> bool:
        calls: list[str] = []

        async def run(args: str, tab: int | None = None) -> str:
            calls.append(args)
            if args == web_fetch._HREF:
                return "https://someone-elses-page.example"
            if args == "tab list":
                return (
                    "[0] Theirs — https://someone-elses-page.example\n"
                    "[1] Target — https://new.example *"
                )
            return "OK"

        web_fetch._FETCH_TABS["main"] = (0, "https://fetched.example")
        index, navigated, err = await web_fetch._fetch_tab("main", "https://new.example", run)
        if err or index != 1 or not navigated:
            print(f"FAIL: index={index!r} navigated={navigated!r} err={err!r}")
            return False
        if "tab new https://new.example" not in calls:
            print(f"FAIL: no new tab opened: {calls!r}")
            return False
        print("PASS")
        return True

    try:
        return asyncio.run(_run())
    finally:
        web_fetch._FETCH_TABS.pop("main", None)


def test_listing_retried_once() -> bool:
    """`tab list` times out against a busy extension — retry before giving up."""
    print("\n=== test: tab listing is retried once ===")

    async def _run() -> bool:
        listings = [
            "[error] The read operation timed out",
            "[0] Target — https://new.example *",
        ]

        async def run(args: str, tab: int | None = None) -> str:
            return listings.pop(0) if args == "tab list" else "OK"

        index, navigated, err = await web_fetch._fetch_tab("main", "https://new.example", run)
        if err or index != 0 or not navigated:
            print(f"FAIL: index={index!r} navigated={navigated!r} err={err!r}")
            return False
        print("PASS")
        return True

    return asyncio.run(_run())


def test_browser_read_sequence() -> bool:
    """The browser step: confirm the tab, open, wait for load, read, re-note."""
    print("\n=== test: browser read drives the tab correctly ===")

    async def _run() -> bool:
        from harness import browser_devices, tools

        calls: list[tuple[str, int | None]] = []
        ready = ["loading", "complete"]

        async def fake_run_browser(args, profile=None, tab=None):
            calls.append((args, tab))
            if args == web_fetch._HREF:
                return "https://old.example"
            if args.startswith("eval") and "readyState" in args:
                return ready.pop(0)
            if args.startswith("eval"):
                return '{"url": "https://example.com/final", "content": "the page text"}'
            return "OK"

        web_fetch._FETCH_TABS["main"] = (1, "https://old.example")
        with patch.object(tools, "_run_browser", fake_run_browser), \
             patch.object(browser_devices, "status",
                          lambda *a, **k: {"online": True, "profile_id": "main"}), \
             patch.object(web_fetch, "_LOAD_TIMEOUT_SECONDS", 2.0):
            out = await web_fetch._browser_read("https://example.com", "text")

        if out != "the page text":
            print(f"FAIL: unexpected content: {out!r}")
            return False
        if ("tab list", None) in calls:
            print(f"FAIL: listed tabs despite a valid note: {calls!r}")
            return False
        if ("open https://example.com", 1) not in calls:
            print(f"FAIL: did not open the url on the noted tab: {calls!r}")
            return False
        if ready:
            print("FAIL: did not wait for readyState=complete")
            return False
        if web_fetch._FETCH_TABS["main"] != (1, "https://example.com/final"):
            print(f"FAIL: tab note not updated: {web_fetch._FETCH_TABS['main']!r}")
            return False
        print("PASS")
        return True

    try:
        return asyncio.run(_run())
    finally:
        web_fetch._FETCH_TABS.pop("main", None)


def test_anchor_ack_is_not_in_output() -> bool:
    """`_run_browser` must not leak its own `tab switch` ack into the result.

    It corrupted every anchored read: JSON parses failed and the readyState
    check never matched, so each fetch burned the full load timeout.
    """
    print("\n=== test: tab-switch ack is stripped by the runner ===")

    async def _run() -> bool:
        from harness import tools

        seen: list[list[str]] = []

        def fake_run_argv(cmd, pairing_code=None, ensure_online=True):
            seen.append(cmd)
            if cmd[0] == "tab":
                return '{\n  "tab_id": 300855216\n}', True
            return "complete", True

        with patch.object(tools, "run_argv", fake_run_argv, create=True), \
             patch("harness.bce_cli.run_argv", fake_run_argv), \
             patch.object(tools, "_resolve_bce_pairing_code",
                          lambda profile: ("AAAA-BBBB", None)):
            out = await tools._run_browser_bce('eval "document.readyState"', None, 7)

        if out != "complete":
            print(f"FAIL: ack leaked into output: {out!r}")
            return False
        if seen[0] != ["tab", "switch", "7"]:
            print(f"FAIL: call was not anchored: {seen!r}")
            return False
        print("PASS")
        return True

    return asyncio.run(_run())


def test_anchor_failure_is_reported() -> bool:
    """A failing anchor must still surface — it is why the batch stopped."""
    print("\n=== test: a failed tab switch is reported ===")

    async def _run() -> bool:
        from harness import tools

        def fake_run_argv(cmd, pairing_code=None, ensure_online=True):
            if cmd[0] == "tab":
                return "[error] COMMAND_FAILED: no such tab", False
            return "should not run", True

        with patch("harness.bce_cli.run_argv", fake_run_argv), \
             patch.object(tools, "_resolve_bce_pairing_code",
                          lambda profile: ("AAAA-BBBB", None)):
            out = await tools._run_browser_bce('eval "1+1"', None, 99)

        if "no such tab" not in out:
            print(f"FAIL: anchor failure swallowed: {out!r}")
            return False
        print("PASS")
        return True

    return asyncio.run(_run())


def test_wrong_page_is_not_returned() -> bool:
    """A page that moved under us must not be returned as the requested one."""
    print("\n=== test: content from a different host is refused ===")

    async def _run() -> bool:
        from harness import browser_devices, tools

        async def fake_run_browser(args, profile=None, tab=None):
            if args == web_fetch._HREF:
                return "https://old.example"
            if "readyState" in args:
                return "complete"
            if args.startswith("eval"):
                return '{"url": "https://www.youtube.com/watch?v=x", "content": "12:01 / 26:56"}'
            return "OK"

        web_fetch._FETCH_TABS["main"] = (1, "https://old.example")
        with patch.object(tools, "_run_browser", fake_run_browser), \
             patch.object(browser_devices, "status",
                          lambda *a, **k: {"online": True, "profile_id": "main"}):
            out = await web_fetch._browser_read("https://news.ycombinator.com", "text")

        if not out.startswith("[wrong page]"):
            print(f"FAIL: returned the wrong page's content: {out!r}")
            return False
        if web_fetch._FETCH_TABS["main"] != (1, "https://old.example"):
            print(f"FAIL: noted a tab we do not own: {web_fetch._FETCH_TABS['main']!r}")
            return False
        print("PASS")
        return True

    try:
        return asyncio.run(_run())
    finally:
        web_fetch._FETCH_TABS.pop("main", None)


def test_browser_offline_note() -> bool:
    """No browser online → a note telling the user to turn it on, not a crash."""
    print("\n=== test: offline browser returns a turn-it-on note ===")

    async def _run() -> bool:
        from harness import browser_devices

        with patch.object(browser_devices, "status",
                          lambda *a, **k: {"online": False, "state": "offline"}):
            out = await web_fetch._browser_read("https://example.com", "text")
        if not out.startswith("[browser offline]"):
            print(f"FAIL: unexpected output: {out!r}")
            return False
        if "Agent ON" not in out:
            print(f"FAIL: note does not say how to turn it on: {out!r}")
            return False
        print("PASS")
        return True

    return asyncio.run(_run())


def main() -> int:
    results = [
        test_browser_read_sequence(),
        test_browser_offline_note(),
        test_wrong_page_is_not_returned(),
        test_anchor_ack_is_not_in_output(),
        test_anchor_failure_is_reported(),
        test_trafilatura_short_circuits_browser(),
        test_falls_through_to_browser(),
        test_raw_mode_skips_trafilatura(),
        test_bad_mode_is_rejected(),
        test_parse_tabs(),
        test_reuses_noted_tab(),
        test_opens_new_tab_when_note_is_stale(),
        test_listing_retried_once(),
    ]
    passed = sum(results)
    print(f"\n{passed}/{len(results)} tests passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

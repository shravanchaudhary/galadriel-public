#!/usr/bin/env python3
"""Regression tests for harness.web_fetch.fetch_url_data.

Catches the turn-killer where ``for extractor in (_trafilatura_get_text):``
iterates a function instead of a one-element tuple (missing trailing comma).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import web_fetch  # noqa: E402


def test_extractors_are_iterable() -> bool:
    """The waterfall must iterate callables, not the function object itself."""
    print("=== test: extractors tuple is iterable of callables ===")
    # Mirror the construct in fetch_url_data — a missing comma makes this a
    # bare function and ``for x in fn`` raises TypeError.
    extractors = (web_fetch._trafilatura_get_text,)
    try:
        names = [e.__name__ for e in extractors]
    except TypeError as e:
        print(f"FAIL: extractors not iterable: {e}")
        return False
    if names != ["_trafilatura_get_text"]:
        print(f"FAIL: unexpected extractors: {names}")
        return False
    print("PASS")
    return True


def test_fetch_url_data_runs_extractor() -> bool:
    """fetch_url_data must enter the loop and return the first usable result."""
    print("\n=== test: fetch_url_data calls extractor (no TypeError) ===")

    async def _run() -> bool:
        mock = AsyncMock(return_value="  hello from page  ")
        with patch.object(web_fetch, "_trafilatura_get_text", mock):
            out = await web_fetch.fetch_url_data("https://example.com")
        if out != "  hello from page  ":
            print(f"FAIL: unexpected content: {out!r}")
            return False
        mock.assert_awaited_once_with("https://example.com")
        print("PASS")
        return True

    try:
        return asyncio.run(_run())
    except TypeError as e:
        print(f"FAIL: TypeError from fetch_url_data (likely missing comma): {e}")
        return False


def test_fetch_url_data_all_fail() -> bool:
    print("\n=== test: fetch_url_data returns None when extractors fail ===")

    async def _run() -> bool:
        mock = AsyncMock(return_value=None)
        with patch.object(web_fetch, "_trafilatura_get_text", mock):
            out = await web_fetch.fetch_url_data("https://example.com")
        if out is not None:
            print(f"FAIL: expected None, got {out!r}")
            return False
        print("PASS")
        return True

    return asyncio.run(_run())


def main() -> int:
    results = [
        test_extractors_are_iterable(),
        test_fetch_url_data_runs_extractor(),
        test_fetch_url_data_all_fail(),
    ]
    passed = sum(results)
    print(f"\n{passed}/{len(results)} tests passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

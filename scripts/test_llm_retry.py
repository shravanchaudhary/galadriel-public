"""Unit tests for harness.providers.llm_retry — no live API calls."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.providers.llm_retry import (  # noqa: E402
    is_transient_llm_error,
    retry_after_seconds,
    stream_with_llm_retry,
    wait_seconds,
    with_llm_retry,
)


class _FakeAPIError(Exception):
    def __init__(self, code: int, message: str = "", response=None):
        self.code = code
        self.message = message
        self.response = response
        self.details = {"error": {"code": code, "message": message, "status": "UNAVAILABLE"}}
        super().__init__(f"{code} UNAVAILABLE. {message}")


class _FakeAnthropicError(Exception):
    def __init__(self, status_code: int, body=None):
        self.status_code = status_code
        self.body = body
        super().__init__(f"Error code: {status_code}")


class TransientDetectionTests(unittest.TestCase):
    def test_503_is_transient(self):
        self.assertTrue(is_transient_llm_error(_FakeAPIError(503)))

    def test_429_is_transient(self):
        self.assertTrue(is_transient_llm_error(_FakeAPIError(429)))

    def test_529_is_transient(self):
        self.assertTrue(is_transient_llm_error(_FakeAnthropicError(529)))

    def test_400_is_not_transient(self):
        self.assertFalse(is_transient_llm_error(_FakeAPIError(400, "bad request")))

    def test_cancelled_is_not_transient(self):
        self.assertFalse(is_transient_llm_error(asyncio.CancelledError()))


class RetryAfterParsingTests(unittest.TestCase):
    def test_header_seconds(self):
        resp = MagicMock()
        resp.headers = {"retry-after": "12"}
        self.assertEqual(retry_after_seconds(_FakeAPIError(503, response=resp)), 12.0)

    def test_body_retry_in(self):
        exc = _FakeAPIError(503, message="Please retry in 8.5s.")
        self.assertEqual(retry_after_seconds(exc), 8.5)

    def test_wait_prefers_retry_after(self):
        resp = MagicMock()
        resp.headers = {"retry-after": "7"}
        with patch("harness.providers.llm_retry.random.uniform", return_value=0.0):
            delay = wait_seconds(1, _FakeAPIError(503, response=resp))
        self.assertEqual(delay, 7.0)

    def test_wait_exponential_without_hint(self):
        with patch("harness.providers.llm_retry.random.uniform", return_value=0.0):
            self.assertEqual(wait_seconds(1, _FakeAPIError(503)), 1.0)
            self.assertEqual(wait_seconds(2, _FakeAPIError(503)), 2.0)
            self.assertEqual(wait_seconds(3, _FakeAPIError(503)), 4.0)


class WithLlmRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovers_after_503s(self):
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise _FakeAPIError(503, message="UNAVAILABLE")
            return "ok"

        with patch("tenacity.nap.sleep", new_callable=AsyncMock):
            # tenacity async uses asyncio.sleep; patch that too
            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await with_llm_retry(flaky, attempts=5)

        self.assertEqual(result, "ok")
        self.assertEqual(calls["n"], 3)

    async def test_exhausts_and_reraises(self):
        async def always_fail():
            raise _FakeAPIError(503)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with self.assertRaises(_FakeAPIError):
                await with_llm_retry(always_fail, attempts=3)

    async def test_does_not_retry_client_errors(self):
        calls = {"n": 0}

        async def bad_request():
            calls["n"] += 1
            raise _FakeAPIError(400, message="INVALID_ARGUMENT")

        with self.assertRaises(_FakeAPIError):
            await with_llm_retry(bad_request, attempts=5)
        self.assertEqual(calls["n"], 1)


class StreamRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_before_first_yield(self):
        calls = {"n": 0}

        async def factory():
            calls["n"] += 1
            if calls["n"] == 1:
                raise _FakeAPIError(503)
                yield  # make this an async generator  # noqa: unreachable
            yield ("text", "hi")
            yield ("message", "done")

        with patch("asyncio.sleep", new_callable=AsyncMock):
            items = [item async for item in stream_with_llm_retry(factory, attempts=4)]

        self.assertEqual(items, [("text", "hi"), ("message", "done")])
        self.assertEqual(calls["n"], 2)

    async def test_no_retry_after_yield(self):
        calls = {"n": 0}

        async def factory():
            calls["n"] += 1
            yield ("text", "partial")
            raise _FakeAPIError(503)

        with self.assertRaises(_FakeAPIError):
            async for _ in stream_with_llm_retry(factory, attempts=4):
                pass
        self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main()

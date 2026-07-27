"""Unit tests for LLM chat title helpers."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from harness.chat_title import generate_chat_title, normalize_title


class NormalizeTitleTests(unittest.TestCase):
    def test_strips_quotes_and_trailing_punct(self):
        self.assertEqual(normalize_title('"Launch Plan."'), "Launch Plan")

    def test_first_line_only(self):
        self.assertEqual(normalize_title("Short Title\n(extra note)"), "Short Title")

    def test_truncates(self):
        title = normalize_title("x" * 100)
        self.assertTrue(title.endswith("…"))
        self.assertLessEqual(len(title), 72)


class GenerateChatTitleTests(unittest.TestCase):
    def test_uses_provider_text(self):
        response = SimpleNamespace(content=[
            SimpleNamespace(type="text", text="  'Weekly Standup'  "),
        ])
        provider = SimpleNamespace(create_message=AsyncMock(return_value=response))

        async def run():
            with patch("harness.model_registry.get_provider", return_value=provider), \
                 patch("harness.model_registry.model_for", return_value="gemini-2.5-flash"):
                return await generate_chat_title("[Tower]: schedule the standup")

        self.assertEqual(asyncio.run(run()), "Weekly Standup")
        kwargs = provider.create_message.await_args.kwargs
        self.assertEqual(kwargs["messages"][0]["content"], "schedule the standup")
        self.assertIn("short chat title", kwargs["system"].lower())

    def test_returns_none_on_failure(self):
        provider = SimpleNamespace(
            create_message=AsyncMock(side_effect=RuntimeError("boom")),
        )

        async def run():
            with patch("harness.model_registry.get_provider", return_value=provider), \
                 patch("harness.model_registry.model_for", return_value="gemini-2.5-flash"):
                return await generate_chat_title("hello")

        self.assertIsNone(asyncio.run(run()))


if __name__ == "__main__":
    unittest.main()

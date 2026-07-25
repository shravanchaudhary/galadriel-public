#!/usr/bin/env python3
"""Regression checks for durable shared conversation-run observability."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from flask import Flask

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import conversation_run_store  # noqa: E402
from tower.chats_board import register_chats_board  # noqa: E402


class SanitizationTests(unittest.TestCase):
    def test_secrets_and_images_are_not_persisted(self):
        stats = {"redactions": 0, "images_omitted": 0}
        safe = conversation_run_store.sanitize(
            {"password": "nope", "content": [{"type": "image", "source": {"data": "x" * 2048}}]},
            stats,
        )
        self.assertEqual(safe["password"], "[redacted]")
        self.assertEqual(safe["content"][0]["type"], "image_omitted")
        self.assertEqual(stats, {"redactions": 1, "images_omitted": 1})


class ChatsBoardTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__, template_folder=str(ROOT / "tower" / "templates"))
        app.secret_key = "test"
        app.context_processor(lambda: {"page_context": {}})
        register_chats_board(app)
        self.client = app.test_client()

    def test_invalid_kind_is_rejected(self):
        self.assertEqual(self.client.get("/chats?kind=nope").status_code, 400)

    def test_list_and_detail_render(self):
        run = {
            "run_id": "run-1",
            "channel_id": "main",
            "state": "active",
            "started_at": datetime.now(timezone.utc),
            "sources": ["slack"],
            "event_count": 2,
            "token_total": 42,
            "cost_total": 0.01,
        }
        with patch.object(conversation_run_store, "is_configured", return_value=True), \
             patch.object(conversation_run_store, "recent_runs", return_value=[run]), \
             patch.object(conversation_run_store, "active_run", return_value=run), \
             patch.object(conversation_run_store, "get_run", return_value=run), \
             patch.object(conversation_run_store, "events_for_run", return_value=[]), \
             patch.object(conversation_run_store, "checkpoints_for_run", return_value=[]), \
             patch.object(conversation_run_store, "calls_for_run", return_value=[]) as calls_mock:
            listed = self.client.get("/chats?kind=chat", follow_redirects=True)
            self.assertEqual(listed.status_code, 200)
            self.assertIn(b"Today", listed.data)
            self.assertIn(b"runs-shell", listed.data)
            # List shell must not pull transcript internals.
            self.assertFalse(calls_mock.called)
            shell = self.client.get("/chats?kind=chat&id=run-1")
            self.assertEqual(shell.status_code, 200)
            self.assertIn(b"runs-shell", shell.data)
            self.assertFalse(calls_mock.called)
            detail = self.client.get("/chats/detail?kind=chat&id=run-1")
            self.assertEqual(detail.status_code, 200)
            body = detail.get_json()
            self.assertEqual(body["id"], "run-1")
            self.assertIn("history", body)
            self.assertTrue(calls_mock.called)
            redirect_detail = self.client.get("/runs/user/run-1", follow_redirects=True)
            self.assertEqual(redirect_detail.status_code, 200)
            # legacy kind=main aliases to chat
            legacy = self.client.get("/runs?kind=main&id=run-1", follow_redirects=True)
            self.assertEqual(legacy.status_code, 200)
        self.assertEqual(self.client.get("/runs/user/missing").status_code, 404)
        self.assertEqual(self.client.get("/chats/detail?kind=chat&id=missing").status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)

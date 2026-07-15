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
from tower.runs_board import register_runs_board  # noqa: E402


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


class RunsBoardTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__, template_folder=str(ROOT / "tower" / "templates"))
        app.secret_key = "test"
        app.context_processor(lambda: {"page_context": {}})
        register_runs_board(app)
        self.client = app.test_client()

    def test_invalid_day_is_rejected(self):
        self.assertEqual(self.client.get("/runs?date=nope").status_code, 400)

    def test_list_and_detail_render(self):
        run = {
            "run_id": "run-1",
            "channel_id": "main",
            "state": "active",
            "started_at": datetime(2026, 7, 14, 8, tzinfo=timezone.utc),
            "sources": ["slack"],
            "event_count": 2,
            "token_total": 42,
            "cost_total": 0.01,
        }
        with patch.object(conversation_run_store, "is_configured", return_value=True), \
             patch.object(conversation_run_store, "runs_for_day", return_value=[run]), \
             patch.object(conversation_run_store, "active_run", return_value=run), \
             patch.object(conversation_run_store, "get_run", return_value=run), \
             patch.object(conversation_run_store, "events_for_run", return_value=[]), \
             patch.object(conversation_run_store, "checkpoints_for_run", return_value=[]), \
             patch.object(conversation_run_store, "calls_for_run", return_value=[]):
            self.assertEqual(self.client.get("/runs?date=2026-07-14").status_code, 200)
            self.assertEqual(self.client.get("/runs/user/run-1").status_code, 200)
        self.assertEqual(self.client.get("/runs/user/missing").status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)

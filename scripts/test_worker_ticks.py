#!/usr/bin/env python3
"""Local regression checks for Worker Tick Observatory.

Usage:
    python3 scripts/test_worker_ticks.py
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from flask import Flask  # noqa: E402

from harness import worker_tick_store  # noqa: E402
from harness.worker import WorkerLoop  # noqa: E402
from tower.chats_board import register_chats_board  # noqa: E402
from tower.todo_board import register_todo_board  # noqa: E402


class _Recorder:
    instances = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.started = False
        self.final = None
        _Recorder.instances.append(self)

    async def start(self):
        self.started = True

    async def finalize(self, **kwargs):
        self.final = kwargs


class _Agent:
    headroom_enabled = False

    def __init__(self, response="<<WORKER_STATUS: idle>>", error=None):
        self.response = response
        self.error = error
        self.reset_calls = []
        self.recorder = None

    def reset_channel(self, channel):
        self.reset_calls.append(channel)

    def model_for_channel(self, _channel):
        return "gemini-2.5-flash"

    async def respond(self, _prompt, **kwargs):
        self.recorder = kwargs["tick_recorder"]
        if self.error:
            raise self.error
        return self.response


class WorkerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _Recorder.instances.clear()
        self.patch = patch("harness.worker.worker_tick_store.WorkerTickRecorder", _Recorder)
        self.patch.start()

    async def asyncTearDown(self):
        self.patch.stop()

    async def test_idle_tick_is_started_and_completed(self):
        agent = _Agent()
        loop = WorkerLoop(agent, working_dir=str(ROOT))
        result = await loop._run_turn()
        self.assertEqual(result, "idle")
        self.assertEqual(agent.reset_calls, ["worker"])
        self.assertTrue(agent.recorder.started)
        self.assertEqual(agent.recorder.final["state"], "completed")
        self.assertEqual(agent.recorder.final["worker_status"], "idle")

    async def test_failed_tick_is_finalized_as_error(self):
        agent = _Agent(error=RuntimeError("provider unavailable"))
        loop = WorkerLoop(agent, working_dir=str(ROOT))
        result = await loop._run_turn()
        self.assertEqual(result, "idle")
        self.assertEqual(agent.recorder.final["state"], "error")
        self.assertIn("provider unavailable", agent.recorder.final["error"])


class SanitizationTests(unittest.TestCase):
    def test_binary_and_secrets_are_removed(self):
        stats = {"images_omitted": 0, "redactions": 0}
        value = {
            "password": "do-not-store",
            "content": [{"type": "image", "source": {"data": "a" * 2048}}],
        }
        safe = worker_tick_store._safe_value(value, stats)
        self.assertEqual(safe["password"], "[redacted]")
        self.assertEqual(safe["content"][0]["type"], "image_omitted")
        self.assertEqual(stats, {"images_omitted": 1, "redactions": 1})


class WorkerBoardTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__, template_folder=str(ROOT / "tower" / "templates"))
        app.secret_key = "test"
        app.context_processor(lambda: {"page_context": {}})
        register_todo_board(app)
        register_chats_board(app)
        self.client = app.test_client()

    def test_invalid_kind_is_rejected(self):
        response = self.client.get("/chats?kind=not-a-channel")
        self.assertEqual(response.status_code, 400)

    def test_selected_day_and_detail_render(self):
        tick = {
            "tick_id": "tick-1",
            "day_cet": "2026-07-14",
            "state": "completed",
            "worker_status": "worked",
            "started_at": datetime.now(timezone.utc),
            "duration_ms": 1250,
            "model": "gemini-2.5-flash",
            "provider": "gemini",
            "tokens": {},
            "token_total": 42,
            "cost_total": 0.01,
            "system_prompt_versions": [],
            "user_prompt": "Do the work",
        }
        with patch.object(worker_tick_store, "is_configured", return_value=True), \
             patch.object(worker_tick_store, "recent_ticks", return_value=[tick]), \
             patch.object(worker_tick_store, "get_tick", return_value=tick), \
             patch.object(worker_tick_store, "events_for_tick", return_value=[]), \
             patch.object(worker_tick_store, "calls_for_tick", return_value=[]) as calls_mock:
            listed = self.client.get("/worker-runs", follow_redirects=False)
            self.assertEqual(listed.status_code, 302)
            self.assertIn("/chats", listed.headers["Location"])
            self.assertIn("kind=worker", listed.headers["Location"])
            followed = self.client.get("/worker-runs", follow_redirects=True)
            self.assertEqual(followed.status_code, 200)
            self.assertIn(b"Today", followed.data)
            self.assertIn(b"skip-link", followed.data)
            self.assertIn(b"/static/ui.js", followed.data)
            self.assertFalse(calls_mock.called)
            shell = self.client.get("/chats?kind=worker&id=tick-1")
            self.assertEqual(shell.status_code, 200)
            self.assertIn(b"runs-shell", shell.data)
            self.assertFalse(calls_mock.called)
            detail = self.client.get("/chats/detail?kind=worker&id=tick-1")
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(detail.get_json()["id"], "tick-1")
            self.assertTrue(calls_mock.called)
            legacy = self.client.get("/worker-runs/tick-1", follow_redirects=False)
            self.assertEqual(legacy.status_code, 302)
            self.assertIn("/chats", legacy.headers["Location"])
            self.assertIn("id=tick-1", legacy.headers["Location"])
            followed_detail = self.client.get("/worker-runs/tick-1", follow_redirects=True)
            self.assertEqual(followed_detail.status_code, 200)
        self.assertEqual(self.client.get("/worker-runs/missing").status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)

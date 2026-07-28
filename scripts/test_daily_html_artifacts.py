#!/usr/bin/env python3
"""Regression and security checks for daily HTML plan/progress artifacts."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from harness import loop_prompts
from tower import todo_board


ROOT = Path(__file__).resolve().parents[1]
SAMPLE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Daily</title>
<style>body { color: CanvasText; }</style></head>
<body><main><h1>Daily artifact</h1></main></body></html>"""


class DailyArtifactRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.plan_dir = root / "plan"
        self.progress_dir = root / "progress"
        self.plan_dir.mkdir()
        self.progress_dir.mkdir()
        (self.plan_dir / "2026-07-28.html").write_text(
            SAMPLE_HTML, encoding="utf-8"
        )

        self.patches = (
            patch.object(todo_board, "PLAN_DIR", self.plan_dir),
            patch.object(todo_board, "PROGRESS_DIR", self.progress_dir),
            patch.object(todo_board, "_today", return_value="2026-07-28"),
        )
        for active_patch in self.patches:
            active_patch.start()

        app = Flask(__name__)
        todo_board.register_todo_board(app)
        self.app = app
        self.client = app.test_client()

    def tearDown(self) -> None:
        for active_patch in reversed(self.patches):
            active_patch.stop()
        self.tempdir.cleanup()

    def test_serves_artifact_with_locked_down_headers(self) -> None:
        response = self.client.get("/todo/artifact/plan/2026-07-28.html")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/html")
        self.assertIn(b"Daily artifact", response.data)
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        csp = response.headers["Content-Security-Policy"]
        self.assertIn("default-src 'none'", csp)
        self.assertIn("form-action 'none'", csp)
        self.assertIn("sandbox allow-popups", csp)

    def test_rejects_missing_invalid_and_traversal_paths(self) -> None:
        paths = (
            "/todo/artifact/progress/2026-07-28.html",
            "/todo/artifact/other/2026-07-28.html",
            "/todo/artifact/plan/2026-02-30.html",
            "/todo/artifact/plan/../../README.html",
        )
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_write_endpoints_are_retired(self) -> None:
        self.assertEqual(self.client.post("/todo/save").status_code, 404)
        self.assertEqual(self.client.post("/actions/save").status_code, 404)

    def test_dashboard_vars_point_to_html_and_report_missing_state(self) -> None:
        with self.app.test_request_context():
            values = todo_board.dashboard_todo_vars()

        self.assertTrue(values["plan_relpath"].endswith("2026-07-28.html"))
        self.assertTrue(values["progress_relpath"].endswith("2026-07-28.html"))
        self.assertTrue(values["plan_artifact_url"].endswith(".html"))
        self.assertTrue(values["plan_exists"])
        self.assertFalse(values["progress_exists"])


class DailyArtifactContractTests(unittest.TestCase):
    def test_any_dated_ledgers_are_html_only_and_standalone(self) -> None:
        for directory in (ROOT / "state" / "plan", ROOT / "state" / "progress"):
            dated_files = [
                path for path in directory.iterdir() if path.name[:4].isdigit()
            ]
            for path in dated_files:
                with self.subTest(path=path):
                    self.assertEqual(path.suffix, ".html")
                    text = path.read_text(encoding="utf-8").lower()
                    self.assertIn("<!doctype html>", text)
                    self.assertIn("<html", text)
                    self.assertIn("<meta name=\"viewport\"", text)
                    self.assertIn("prefers-color-scheme:dark", text)
                    self.assertNotIn("<script", text)
                    self.assertNotIn("<form", text)

    def test_agent_prompts_and_references_use_html(self) -> None:
        prompt_source = (ROOT / "harness" / "loop_prompts.py").read_text(
            encoding="utf-8"
        )
        worker_source = (ROOT / "harness" / "worker.py").read_text(encoding="utf-8")
        recall = (ROOT / "config" / "RECALL.md").read_text(encoding="utf-8")
        combined = prompt_source + worker_source + recall
        self.assertNotRegex(
            combined,
            r"state/(?:plan|progress)/(?:\{today\}|<today>|\d{4}-\d{2}-\d{2})\.md",
        )
        self.assertIn("state/plan/{today}.html", prompt_source)
        self.assertIn("state/progress/{today}.html", prompt_source)
        self.assertIn("preserve the document shell/styles", prompt_source)

    def test_chat_template_is_read_only_and_sandboxed(self) -> None:
        template = (ROOT / "tower" / "templates" / "chats" / "chat.html").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("/todo/save", template)
        self.assertNotIn("plan_content", template)
        self.assertNotIn("progress_content", template)
        self.assertIn('sandbox="allow-popups"', template)
        self.assertIn("daily-artifact-frame", template)

        stylesheet = (ROOT / "tower" / "static" / "style.css").read_text(
            encoding="utf-8"
        )
        self.assertIn("@media (max-width: 760px)", stylesheet)
        self.assertRegex(
            stylesheet,
            r"\.actions-board-grid\.new-chat-todo\s*\{[^}]*grid-template-columns:\s*1fr;",
        )


if __name__ == "__main__":
    unittest.main()

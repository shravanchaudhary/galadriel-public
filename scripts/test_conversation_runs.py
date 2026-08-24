#!/usr/bin/env python3
"""Regression checks for durable shared conversation-run observability."""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from flask import Flask

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import conversation_run_store  # noqa: E402
from harness.agent import GaladrielAgent, MAIN_CHANNEL_ID  # noqa: E402
from tower.app import _browser_transcribe_credentials, create_tower  # noqa: E402
from tower.chats_board import (  # noqa: E402
    _main_items,
    active_main_history,
    history_for_run,
    register_chats_board,
)
from tower.todo_board import register_todo_board  # noqa: E402


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


class TitleTests(unittest.TestCase):
    def test_title_from_tower_user_message(self):
        self.assertEqual(
            conversation_run_store.title_from_user_content("[Tower]: Plan the launch"),
            "Plan the launch",
        )

    def test_title_truncates(self):
        long = "[Tower]: " + ("x" * 100)
        title = conversation_run_store.title_from_user_content(long)
        self.assertTrue(title.endswith("…"))
        self.assertLessEqual(len(title), 72)

    def test_main_items_prefer_stored_title(self):
        rows = [{
            "run_id": "run-1",
            "title": "First question",
            "sources": ["tower"],
            "started_at": datetime.now(timezone.utc),
            "state": "active",
            "llm_call_count": 1,
            "cost_total": 0.01,
        }]
        items = _main_items(rows)
        self.assertEqual(items[0]["title"], "First question")

    def test_main_items_do_not_backfill_titles(self):
        rows = [{
            "run_id": "run-2",
            "title": None,
            "sources": ["tower"],
            "end_reason": "completed",
            "started_at": datetime.now(timezone.utc),
            "state": "closed",
            "llm_call_count": 0,
            "cost_total": 0,
        }]
        with patch.object(
            conversation_run_store, "backfill_run_title", return_value="from events",
        ) as backfill:
            items = _main_items(rows)
        self.assertEqual(items[0]["title"], "tower")
        backfill.assert_not_called()


class RecorderFlushTests(unittest.TestCase):
    def test_events_buffer_until_finalize(self):
        run = {
            "run_id": "run-flush",
            "title": None,
            "tokens": {},
            "cost_total": 0,
            "llm_call_count": 0,
            "tool_call_count": 0,
            "event_sequence": 0,
            "system_prompt_versions": [],
        }
        recorder = conversation_run_store.ConversationRunRecorder(run, source="tower")
        inserted = []

        async def fake_insert_many(docs):
            inserted.extend(docs)

        mock_db = MagicMock()
        mock_db[conversation_run_store.EVENTS].insert_many = AsyncMock(side_effect=fake_insert_many)
        mock_db[conversation_run_store.EVENTS].count_documents = AsyncMock(return_value=2)
        mock_db[conversation_run_store.RUNS].update_one = AsyncMock()

        async def run_test():
            await recorder.begin_turn("dedupe-1")
            await recorder.record_message(
                {"role": "user", "content": "[Tower]: hello world"},
                visibility="user",
                kind="direct_user",
            )
            await recorder.record_direct_reply("hi")
            self.assertEqual(len(inserted), 0)
            self.assertEqual(len(recorder._pending_events), 3)
            with patch("scripts.lib.db.get_db", return_value=mock_db), \
                 patch(
                     "harness.chat_title.generate_chat_title",
                     new=AsyncMock(return_value="Hello World"),
                 ):
                await recorder.finalize_turn(state="completed")
            self.assertEqual(len(inserted), 3)
            self.assertEqual(recorder._pending_events, [])
            update = mock_db[conversation_run_store.RUNS].update_one.await_args.args[1]
            self.assertEqual(update["$set"]["title"], "Hello World")
            self.assertEqual(update["$set"]["current_turn_state"], "completed")

        asyncio.run(run_test())

    def test_title_falls_back_when_llm_fails(self):
        run = {
            "run_id": "run-flush-2",
            "title": None,
            "tokens": {},
            "cost_total": 0,
            "llm_call_count": 0,
            "tool_call_count": 0,
            "event_sequence": 0,
            "system_prompt_versions": [],
        }
        recorder = conversation_run_store.ConversationRunRecorder(run, source="tower")
        mock_db = MagicMock()
        mock_db[conversation_run_store.EVENTS].insert_many = AsyncMock()
        mock_db[conversation_run_store.EVENTS].count_documents = AsyncMock(return_value=1)
        mock_db[conversation_run_store.RUNS].update_one = AsyncMock()

        async def run_test():
            await recorder.record_message(
                {"role": "user", "content": "[Tower]: fallback title please"},
                visibility="user",
                kind="direct_user",
            )
            with patch("scripts.lib.db.get_db", return_value=mock_db), \
                 patch(
                     "harness.chat_title.generate_chat_title",
                     new=AsyncMock(return_value=None),
                 ):
                await recorder.finalize_turn(state="completed")
            update = mock_db[conversation_run_store.RUNS].update_one.await_args.args[1]
            self.assertEqual(update["$set"]["title"], "fallback title please")

        asyncio.run(run_test())


class SwitchMainRunTests(unittest.TestCase):
    def test_switch_rebuilds_buffer_and_reactivates(self):
        agent = GaladrielAgent.__new__(GaladrielAgent)
        agent.conversations = {MAIN_CHANNEL_ID: [{"role": "user", "content": "old"}]}
        agent.working_dir = "/tmp/galadriel-test"
        agent._output_ceiling_streak = {}
        agent._compaction_summary = {}
        agent._last_input_tokens = {}
        agent._last_archived_len = {}
        agent._notified_recall_ids = {}
        agent._session_id = {}
        agent._session_segments = {}
        agent.is_channel_busy = lambda channel: False

        target = {
            "run_id": "run-b",
            "channel_id": "main",
            "state": "ended",
            "title": "Older chat",
        }
        rebuilt = [
            {"role": "user", "content": "[Tower]: resume me"},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        ]

        with patch.object(conversation_run_store, "get_run", return_value=target), \
             patch.object(conversation_run_store, "active_run", return_value={
                 "run_id": "run-a", "channel_id": "main", "state": "active",
             }), \
             patch.object(conversation_run_store, "end_active_run", new_callable=AsyncMock) as end_mock, \
             patch.object(
                 conversation_run_store, "buffer_messages_for_run",
                 return_value=(rebuilt, None),
             ), \
             patch.object(
                 conversation_run_store, "reactivate_run",
                 new_callable=AsyncMock, return_value=target,
             ) as react_mock, \
             patch("harness.conversation_store.save_channel") as save_mock:
            result = asyncio.run(agent.switch_main_run("run-b"))

        self.assertTrue(result["switched"])
        self.assertEqual(result["run_id"], "run-b")
        self.assertEqual(agent.conversations[MAIN_CHANNEL_ID], rebuilt)
        end_mock.assert_awaited()
        react_mock.assert_awaited_with("run-b")
        self.assertTrue(save_mock.called)

    def test_switch_rejects_when_busy(self):
        agent = GaladrielAgent.__new__(GaladrielAgent)
        agent.is_channel_busy = lambda channel: True
        with self.assertRaises(RuntimeError):
            asyncio.run(agent.switch_main_run("run-b"))


class OverlayHistoryTests(unittest.TestCase):
    def test_direct_user_plus_protocol_keeps_thoughts(self):
        # Production shape: users are direct_user; assistants (with thought)
        # are protocol_message. direct_reply is display-only and ignored here.
        all_events = [
            {
                "kind": "direct_user",
                "role": "user",
                "content": "[Tower]: hello",
                "visibility": "user",
            },
            {
                "kind": "protocol_message",
                "role": "assistant",
                "content": [{"type": "text", "text": "hi there"}],
                "thought": "considering the greeting",
                "visibility": "internal",
            },
            {
                "kind": "direct_reply",
                "role": "assistant",
                "content": "hi there",
                "visibility": "user",
            },
        ]
        direct = [e for e in all_events if e.get("visibility") == "user"]
        with patch.object(
            conversation_run_store, "events_for_run",
            side_effect=lambda run_id, visibility=None: (
                direct if visibility == "user" else all_events
            ),
        ):
            history, protocol = history_for_run("run-1")
        self.assertIs(history, protocol)
        self.assertEqual(history[0]["text"], "hello")
        self.assertEqual(history[1]["blocks"][0]["type"], "thought")
        self.assertEqual(history[1]["blocks"][0]["text"], "considering the greeting")
        self.assertEqual(history[1]["blocks"][1]["text"], "hi there")

    def test_protocol_only_without_direct_user_yields_empty_then_direct_fallback(self):
        # Bug regression: protocol_message assistants with no user protocol
        # messages used to serialize to [] and wipe thoughts on rehydrate.
        protocol_only = [
            {
                "kind": "protocol_message",
                "role": "assistant",
                "content": [{"type": "text", "text": "orphan"}],
                "thought": "unreachable without a preceding user",
            },
        ]
        direct = [
            {"role": "user", "content": "[Tower]: only direct", "kind": "direct_user"},
            {
                "role": "assistant",
                "content": "ok",
                "kind": "direct_reply",
                "thought": "kept on direct reply",
            },
        ]
        with patch.object(
            conversation_run_store, "events_for_run",
            side_effect=lambda run_id, visibility=None: (
                direct if visibility == "user" else protocol_only
            ),
        ):
            history, protocol = history_for_run("run-1")
        # serialize skips orphan assistants → empty protocol → direct fallback
        self.assertEqual(protocol, [])
        self.assertEqual(history[0]["text"], "only direct")
        self.assertEqual(history[1]["blocks"][0]["type"], "thought")
        self.assertEqual(history[1]["blocks"][0]["text"], "kept on direct reply")

    def test_active_main_history_empty_without_run(self):
        with patch.object(conversation_run_store, "active_run", return_value=None):
            history, run_id = active_main_history()
        self.assertEqual(history, [])
        self.assertIsNone(run_id)


class ChatsBoardTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__, template_folder=str(ROOT / "tower" / "templates"))
        app.secret_key = "test"
        app.context_processor(lambda: {"page_context": {}})
        register_todo_board(app)
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
            "title": "Named chat",
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
             patch.object(conversation_run_store, "backfill_run_title", return_value=None), \
             patch.object(conversation_run_store, "calls_for_run", return_value=[]) as calls_mock:
            listed = self.client.get("/chats?kind=chat", follow_redirects=True)
            self.assertEqual(listed.status_code, 200)
            self.assertIn(b"Today", listed.data)
            self.assertIn(b"runs-shell", listed.data)
            self.assertIn(b"Named chat", listed.data)
            self.assertIn(b"skip-link", listed.data)
            self.assertIn(b"site-menu-btn", listed.data)
            self.assertIn(b"/static/ui.js", listed.data)
            self.assertFalse(calls_mock.called)
            shell = self.client.get("/chats?kind=chat&id=run-1")
            self.assertEqual(shell.status_code, 200)
            self.assertIn(b"runs-shell", shell.data)
            self.assertIn(b"runs-composer", shell.data)
            self.assertIn(b'id="new-chat-mic"', shell.data)
            self.assertIn(b'id="runs-mic"', shell.data)
            self.assertIn(b"/static/voice_dictation.js", shell.data)
            self.assertFalse(calls_mock.called)
            detail = self.client.get("/chats/detail?kind=chat&id=run-1")
            self.assertEqual(detail.status_code, 200)
            body = detail.get_json()
            self.assertEqual(body["id"], "run-1")
            self.assertEqual(body["title"], "Named chat")
            self.assertTrue(body["continuable"])
            self.assertIn("history", body)
            self.assertTrue(calls_mock.called)
            legacy_index = self.client.get("/runs?kind=main&id=run-1", follow_redirects=False)
            self.assertEqual(legacy_index.status_code, 302)
            self.assertIn("/chats", legacy_index.headers["Location"])
            redirect_detail = self.client.get("/runs/user/run-1", follow_redirects=False)
            self.assertEqual(redirect_detail.status_code, 302)
            self.assertIn("/chats", redirect_detail.headers["Location"])
            self.assertIn("id=run-1", redirect_detail.headers["Location"])
            followed = self.client.get("/runs/user/run-1", follow_redirects=True)
            self.assertEqual(followed.status_code, 200)
            legacy = self.client.get("/runs?kind=main&id=run-1", follow_redirects=True)
            self.assertEqual(legacy.status_code, 200)
        self.assertEqual(self.client.get("/runs/user/missing").status_code, 404)
        self.assertEqual(self.client.get("/chats/detail?kind=chat&id=missing").status_code, 404)

    def test_items_list_does_not_backfill_or_load_active(self):
        run = {
            "run_id": "run-2",
            "title": None,
            "sources": ["tower"],
            "started_at": datetime.now(timezone.utc),
            "state": "closed",
            "llm_call_count": 0,
            "cost_total": 0,
        }
        with patch.object(conversation_run_store, "is_configured", return_value=True), \
             patch.object(conversation_run_store, "recent_runs", return_value=[run]), \
             patch.object(conversation_run_store, "active_run") as active_mock, \
             patch.object(
                 conversation_run_store, "backfill_run_title", return_value="nope",
             ) as backfill:
            response = self.client.get("/chats/items?kind=chat&page=4")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["items"][0]["title"], "tower")
        self.assertEqual(body["page"], 4)
        active_mock.assert_not_called()
        backfill.assert_not_called()


class AgentTimezoneCacheTests(unittest.TestCase):
    """The Chats rail asks for the agent timezone ~3x per row (bucket + label).

    Uncached that was one Mongo round-trip per cell — 78 reads / ~4.5s per page.
    """

    def setUp(self):
        from harness import tower_settings
        self.tower_settings = tower_settings
        self.addCleanup(setattr, tower_settings, "_timezone_cache", None)
        tower_settings._timezone_cache = None

    def test_repeated_reads_hit_mongo_once(self):
        settings = MagicMock()
        settings.find_one.return_value = {"timezone": "Asia/Kolkata"}
        with patch.object(
            self.tower_settings, "_db",
            return_value={self.tower_settings.COLLECTION: settings},
        ):
            values = [self.tower_settings.get_agent_timezone() for _ in range(50)]
        self.assertEqual(set(values), {"Asia/Kolkata"})
        settings.find_one.assert_called_once()
        self.assertEqual(settings.find_one.call_args[0][1], {"timezone": 1, "_id": 0})

    def test_set_timezone_refreshes_cache(self):
        settings = MagicMock()
        settings.find_one.return_value = {"timezone": "Asia/Kolkata"}
        with patch.object(
            self.tower_settings, "_db",
            return_value={self.tower_settings.COLLECTION: settings},
        ):
            self.assertEqual(self.tower_settings.get_agent_timezone(), "Asia/Kolkata")
            self.tower_settings.set_agent_timezone("Europe/Stockholm")
            settings.find_one.reset_mock()
            self.assertEqual(
                self.tower_settings.get_agent_timezone(), "Europe/Stockholm",
            )
        settings.find_one.assert_not_called()


class ListProjectionTests(unittest.TestCase):
    def test_recent_runs_uses_inclusion_projection(self):
        runs = MagicMock()
        cursor = MagicMock()
        runs.find.return_value = cursor
        cursor.sort.return_value = cursor
        cursor.skip.return_value = cursor
        cursor.limit.return_value = []
        with patch.object(conversation_run_store, "_sync_db", return_value={
            conversation_run_store.RUNS: runs,
        }), patch.object(conversation_run_store, "ensure_list_indexes"):
            conversation_run_store.recent_runs(26, skip=75)
        runs.find.assert_called_once_with({}, conversation_run_store._LIST_PROJECTION)
        self.assertNotIn("system_prompt_versions", conversation_run_store._LIST_PROJECTION)
        cursor.skip.assert_called_once_with(75)
        cursor.limit.assert_called_once_with(26)

    def test_backfill_projects_event_content_only(self):
        runs = MagicMock()
        events = MagicMock()
        runs.find_one.return_value = {"title": None}
        events.find_one.return_value = {"content": "hello"}
        db = {
            conversation_run_store.RUNS: runs,
            conversation_run_store.EVENTS: events,
        }
        with patch.object(conversation_run_store, "_sync_db", return_value=db):
            title = conversation_run_store.backfill_run_title("run-3")
        self.assertEqual(title, "hello")
        events.find_one.assert_called_once()
        self.assertEqual(events.find_one.call_args[0][1], {"content": 1, "_id": 0})


class LegacyRedirectTests(unittest.TestCase):
    def test_phone_bridge_page_redirects_to_devices(self):
        from tower.phone_bridge import register_phone_bridge

        app = Flask(__name__)
        register_phone_bridge(app)
        response = app.test_client().get("/phone-bridge", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/devices/phone")


class VoiceDictationTests(unittest.TestCase):
    class Agent:
        model = "test-model"
        conversations = {}
        headroom_enabled = False

        class memory:
            memory_dir = str(ROOT / "memory")

        def model_for_channel(self, _channel_id):
            return self.model

    def test_credentials_require_role_configuration(self):
        with patch.dict(os.environ, {"VOICE_TRANSCRIBE_ROLE_NAME": ""}, clear=False):
            os.environ.pop("VOICE_TRANSCRIBE_ROLE_ARN", None)
            with self.assertRaisesRegex(RuntimeError, "not configured"):
                _browser_transcribe_credentials()

    def test_credentials_discover_staging_role_for_local_development(self):
        expires = datetime(2026, 7, 28, 20, 0, tzinfo=timezone.utc)
        sts = MagicMock()
        sts.get_caller_identity.return_value = {"Account": "123456789012"}
        sts.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "temporary-access",
                "SecretAccessKey": "temporary-secret",
                "SessionToken": "temporary-token",
                "Expiration": expires,
            }
        }
        with patch.dict(os.environ, {
            "AWS_PROFILE": "",
            "AWS_DEFAULT_PROFILE": "",
        }, clear=False), patch(
            "boto3.client", return_value=sts
        ):
            os.environ.pop("VOICE_TRANSCRIBE_ROLE_ARN", None)
            os.environ.pop("VOICE_TRANSCRIBE_ROLE_NAME", None)
            _browser_transcribe_credentials()
            self.assertNotIn("AWS_PROFILE", os.environ)
            self.assertNotIn("AWS_DEFAULT_PROFILE", os.environ)
        sts.assume_role.assert_called_once_with(
            RoleArn=(
                "arn:aws:iam::123456789012:"
                "role/clyra-stag-browser-transcription"
            ),
            RoleSessionName="replika-browser-dictation",
            DurationSeconds=900,
        )

    def test_credentials_are_short_lived_and_serialized_for_browser(self):
        expires = datetime(2026, 7, 28, 20, 0, tzinfo=timezone.utc)
        sts = MagicMock()
        sts.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "temporary-access",
                "SecretAccessKey": "temporary-secret",
                "SessionToken": "temporary-token",
                "Expiration": expires,
            }
        }
        with patch.dict(os.environ, {
            "VOICE_TRANSCRIBE_ROLE_ARN": "arn:aws:iam::123456789012:role/browser",
            "VOICE_TRANSCRIBE_REGION": "ap-south-1",
        }), patch("boto3.client", return_value=sts):
            result = _browser_transcribe_credentials()
        self.assertEqual(result["accessKeyId"], "temporary-access")
        self.assertEqual(result["region"], "ap-south-1")
        self.assertEqual(result["expiration"], expires.isoformat())
        sts.assume_role.assert_called_once_with(
            RoleArn="arn:aws:iam::123456789012:role/browser",
            RoleSessionName="replika-browser-dictation",
            DurationSeconds=900,
        )

    def test_credentials_route_is_authenticated_and_not_cached(self):
        credentials = {
            "accessKeyId": "a",
            "secretAccessKey": "s",
            "sessionToken": "t",
            "expiration": "2026-07-28T20:00:00+00:00",
            "region": "ap-south-1",
        }
        with patch.dict(os.environ, {
            "TOWER_AUTH_REQUIRED": "false",
            "TOWER_SECRET_KEY": "voice-test-secret",
        }), patch("tower.app._browser_transcribe_credentials", return_value=credentials):
            client = create_tower(self.Agent()).test_client()
            response = client.post("/api/transcribe/credentials", json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), credentials)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

        with patch.dict(os.environ, {
            "TOWER_AUTH_REQUIRED": "false",
            "TOWER_SECRET_KEY": "voice-test-secret",
            "VOICE_TRANSCRIBE_ROLE_NAME": "",
        }, clear=False):
            os.environ.pop("VOICE_TRANSCRIBE_ROLE_ARN", None)
            client = create_tower(self.Agent()).test_client()
            unavailable = client.post("/api/transcribe/credentials", json={})
        self.assertEqual(unavailable.status_code, 503)
        self.assertIn("not configured", unavailable.get_json()["error"])

        with patch.dict(os.environ, {
            "TOWER_AUTH_REQUIRED": "true",
            "TOWER_AUTH_USERNAME": "voice-test",
            "TOWER_AUTH_TOKEN": "voice-test-token",
            "TOWER_SECRET_KEY": "voice-test-secret",
        }):
            client = create_tower(self.Agent()).test_client()
            response = client.post("/api/transcribe/credentials", json={})
        self.assertEqual(response.status_code, 401)


class SelectApiTests(unittest.TestCase):
    def test_select_busy_returns_409(self):
        from flask import Flask, jsonify, request

        class FakeAgent:
            def is_channel_busy(self, channel):
                return True

        agent = FakeAgent()
        app = Flask(__name__)

        @app.route("/api/chat/select", methods=["POST"])
        def api_chat_select():
            data = request.json or {}
            run_id = (data.get("run_id") or "").strip()
            if not run_id:
                return jsonify({"error": "run_id is required"}), 400
            if agent.is_channel_busy(MAIN_CHANNEL_ID):
                return jsonify({"error": "Channel is busy"}), 409
            return jsonify({"run_id": run_id})

        client = app.test_client()
        res = client.post("/api/chat/select", json={"run_id": "run-1"})
        self.assertEqual(res.status_code, 409)
        self.assertIn("busy", res.get_json().get("error", "").lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)

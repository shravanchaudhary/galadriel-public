"""Focused Slack observation, reply-gate, and archive checks."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from flask import Flask  # noqa: E402
from slack_sdk.web.async_client import AsyncWebClient  # noqa: E402

from harness.slack_observations import (  # noqa: E402
    DuplicateKeyError,
    MemorySlackObservationStore,
    MongoSlackObservationStore,
    ReplyDecision,
    ReplyGate,
    SlackObservationArchiver,
    observation_key,
    observation_overlay,
)
from slack_bot.bot import create_bot  # noqa: E402
from tower.slack_integration import signed_internal_headers  # noqa: E402
from tower.slack_runtime import register_slack_runtime  # noqa: E402


def check(condition, message):
    if not condition:
        raise AssertionError(message)


class RacingIndexCollection:
    def __init__(self, peer_completes=True):
        self.indexes = {}
        self.peer_completes = peer_completes
        self.first = True

    def create_index(self, _keys, **kwargs):
        name = kwargs["name"]
        if self.first:
            self.first = False
            if self.peer_completes:
                self.indexes[name] = {}
            raise DuplicateKeyError("concurrent index create")
        self.indexes[name] = {}

    def index_information(self):
        return self.indexes


class RacingIndexDatabase:
    def __init__(self, collection):
        self.collection = collection

    def __getitem__(self, _name):
        return self.collection


racing_collection = RacingIndexCollection()
MongoSlackObservationStore(RacingIndexDatabase(racing_collection))
check(
    "unique_slack_observation" in racing_collection.indexes,
    "a completed concurrent DocumentDB index create is accepted",
)

try:
    MongoSlackObservationStore(RacingIndexDatabase(RacingIndexCollection(False)))
except DuplicateKeyError:
    pass
else:
    raise AssertionError("an uncreated index must not hide a real duplicate-key failure")


def message(ts: str, text: str, user: str = "U1"):
    return {
        "type": "message",
        "channel": "C1",
        "channel_type": "channel",
        "user": user,
        "text": text,
        "ts": ts,
    }


store = MemorySlackObservationStore()
original = store.observe(
    tenant_id="tenant",
    workspace_id="T1",
    channel_id="C1",
    event=message("1.0", "original"),
    event_id="E1",
)
check(original["revision"] == 1, "initial observation persisted")
changed = store.observe(
    tenant_id="tenant",
    workspace_id="T1",
    channel_id="C1",
    event={
        "type": "message",
        "subtype": "message_changed",
        "channel": "C1",
        "message": {"ts": "1.0", "user": "U1", "text": "edited"},
    },
    event_id="E2",
)
check(changed["revision"] == 2 and changed["text"] == "edited", "edit creates revision")
deleted = store.observe(
    tenant_id="tenant",
    workspace_id="T1",
    channel_id="C1",
    event={
        "type": "message",
        "subtype": "message_deleted",
        "channel": "C1",
        "deleted_ts": "1.0",
        "previous_message": {"ts": "1.0", "user": "U1", "text": "edited"},
    },
    event_id="E3",
)
check(deleted["revision"] == 3 and deleted["tombstone"], "delete creates tombstone")
check(not store.recent("T1", "C1"), "tombstoned text is not current")
check(
    not store.search_current("T1", "C1", "edited"),
    "exact-current search excludes tombstoned Slack text",
)
duplicate = store.observe(
    tenant_id="tenant",
    workspace_id="T1",
    channel_id="C1",
    event=message("1.0", "ignored duplicate"),
    event_id="E3",
)
check(duplicate["revision"] == 3, "event id deduplicates revisions")

for index in range(30):
    store.observe(
        tenant_id="tenant",
        workspace_id="T1",
        channel_id="C1",
        event=message(str(index + 2), "x" * 100, f"U{index}"),
        event_id=f"E{index + 10}",
    )
overlay = observation_overlay(store.recent("T1", "C1", limit=30), max_items=5, max_chars=450)
check(len(overlay) <= 500 and overlay.count("\n- [") <= 5, "recent overlay is bounded")


class ToolProvider:
    async def create_message(self, **kwargs):
        block = SimpleNamespace(
            type="tool_use",
            name="decide_slack_reply",
            input={
                "should_respond": False,
                "reason_code": "human_information",
            },
        )
        return SimpleNamespace(content=[block])


class FailingProvider:
    async def create_message(self, **kwargs):
        raise RuntimeError("provider unavailable")


async def gate_checks():
    current = {"text": "FYI, deploy is complete", "sender_id": "U1"}
    decision = await ReplyGate(
        ToolProvider(), min_interval_seconds=0
    ).decide(
        tenant_id="tenant",
        current=current,
        recent=[current],
        inbox_status={"busy": False, "paused": False, "depth": 0},
    )
    check(
        decision == ReplyDecision(False, "human_information"),
        "structured no-reply decision validated",
    )
    fail_open = await ReplyGate(
        FailingProvider(), min_interval_seconds=0
    ).decide(
        tenant_id="tenant",
        current=current,
        recent=[current],
        inbox_status={"busy": True, "paused": True, "depth": 2},
    )
    check(fail_open.should_respond, "classifier failure defaults to respond")


asyncio.run(gate_checks())


class FakePalace:
    def __init__(self, observation_store, root):
        self.store = observation_store
        self.root = Path(root)
        self.writes = []
        self.mine_results = [False, True, True]

    def write_slack_observation_batch(self, rows):
        check(len(rows) <= 2, "archive batch is bounded")
        path = self.root / f"batch-{len(self.writes)}"
        path.mkdir()
        self.writes.append((path, list(rows)))
        return path

    async def mine_batch_dir(self, path, agent):
        rows = next(rows for batch, rows in self.writes if batch == path)
        check(
            all(
                self.store.get(row["key"])["archive_state"] == "staged"
                for row in rows
            ),
            "observations are durably staged before mining",
        )
        return self.mine_results.pop(0)


archive_store = MemorySlackObservationStore()
for index in range(3):
    archive_store.observe(
        tenant_id="tenant",
        workspace_id="T1",
        channel_id="C1",
        event=message(str(index), f"archive {index}"),
        event_id=f"A{index}",
    )


async def archive_checks():
    with tempfile.TemporaryDirectory() as root:
        palace = FakePalace(archive_store, root)
        archiver = SlackObservationArchiver(
            archive_store, palace_module=palace, debounce_seconds=0, batch_size=2
        )
        check(await archiver.flush() == 0, "failed mine remains staged")
        staged = archive_store.staged_batches()
        check(len(staged) == 1, "staged batch retained for recovery")
        check(await archiver.flush() == 2, "staged archive resumes before new batch")
        check(not archive_store.staged_batches(), "recovered batch marked mined")
        check(
            len(palace.writes) == 2 and len(palace.writes[0][1]) == 2,
            "pending observations are archived in bounded batches",
        )


asyncio.run(archive_checks())


class FakeArchiver:
    def __init__(self):
        self.scheduled = 0

    def schedule(self):
        self.scheduled += 1


class RuntimeGate:
    def __init__(self, observation_store, should_respond):
        self.store = observation_store
        self.should_respond = should_respond
        self.calls = []

    async def decide(self, **kwargs):
        current = kwargs["current"]
        check(
            self.store.get(current["key"]) is not None,
            "observation is persisted before classification",
        )
        self.calls.append(kwargs)
        if kwargs["explicit_override"]:
            return ReplyDecision(True, "explicit_override")
        return ReplyDecision(self.should_respond, (
            "agent_input_needed" if self.should_respond else "human_information"
        ))


class RuntimeAgent:
    def __init__(self):
        self.enqueued = []
        self.conversation_queue = SimpleNamespace(
            status=lambda channel: {"busy": False, "paused": False, "depth": 0}
        )

    async def enqueue(self, payload, **kwargs):
        self.enqueued.append((payload, kwargs))
        return {"id": str(len(self.enqueued))}

    async def await_enqueued(self, item_id):
        return ""

    def request_stop(self, channel):
        return True


loop = asyncio.new_event_loop()
loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
loop_thread.start()


def runtime(replika_type: str, observation_store, gate, archiver):
    app = Flask(__name__)
    agent = RuntimeAgent()
    app.config.update(
        REPLIKA_TENANT_ID="tenant",
        REPLIKA_TYPE=replika_type,
        SLACK_TENANT_AUTH_SECRET="secret",
        SLACK_CONTROL_PLANE_URL="https://control.example",
        SLACK_OBSERVATION_STORE=observation_store,
        SLACK_REPLY_GATE=gate,
        SLACK_OBSERVATION_ARCHIVER=archiver,
    )
    register_slack_runtime(app, agent, SimpleNamespace(_loop=loop))
    return app.test_client(), agent


def ingress(client, replika_type, event, dedupe):
    data = {
        "tenant_id": "tenant",
        "team_id": "T1",
        "replika_type": replika_type,
        "installer_user_id": "UOWNER",
        "selected_channel": {"id": "C1"} if replika_type == "organization" else None,
        "bot_user_id": "B1",
        "kind": "event",
        "dedupe_key": dedupe,
        "payload": {"event_id": dedupe, "event_time": 123, "event": event},
    }
    body = json.dumps(data, separators=(",", ":")).encode()
    return client.post(
        "/internal/slack/ingress",
        data=body,
        content_type="application/json",
        headers=signed_internal_headers("tenant", body, "secret"),
    )


runtime_store = MemorySlackObservationStore()
archiver = FakeArchiver()
gate = RuntimeGate(runtime_store, should_respond=False)
client, agent = runtime("organization", runtime_store, gate, archiver)
check(
    ingress(client, "organization", message("100", "FYI only"), "NONMENTION")
    .get_json()["accepted"],
    "active non-mentioned organization message is received",
)
time.sleep(0.1)
check(not agent.enqueued, "observe-only message creates no queue entry")
check(gate.calls, "organization message is classified")

check(
    ingress(client, "organization", message("101", "<@B1> help"), "MENTION")
    .get_json()["accepted"],
    "explicit mention accepted",
)
deadline = time.time() + 1
while not agent.enqueued and time.time() < deadline:
    time.sleep(0.01)
check(len(agent.enqueued) == 1, "explicit mention overrides no-reply classifier")
check(
    "Recent Slack channel observations" in agent.enqueued[0][1]["overlay_context"],
    "actionable turn receives ephemeral recent context",
)


class ForbiddenStore:
    def observe(self, **kwargs):
        raise AssertionError("individual DM must bypass observation/classifier")


class ForbiddenGate:
    async def decide(self, **kwargs):
        raise AssertionError("individual DM must bypass classifier")


dm_client, dm_agent = runtime(
    "individual", ForbiddenStore(), ForbiddenGate(), FakeArchiver()
)
dm = message("200", "private", "UOWNER")
dm.update(channel="D1", channel_type="im")
check(ingress(dm_client, "individual", dm, "DM").get_json()["accepted"], "DM accepted")
deadline = time.time() + 1
while not dm_agent.enqueued and time.time() < deadline:
    time.sleep(0.01)
check(len(dm_agent.enqueued) == 1, "DM bypasses gate and enqueues directly")

loop.call_soon_threadsafe(loop.stop)
loop_thread.join(timeout=2)
loop.close()


class ManualAgent:
    def __init__(self):
        self.enqueued = []
        self.conversation_queue = SimpleNamespace(
            status=lambda channel: {"busy": False, "paused": False, "depth": 0}
        )

    async def enqueue_and_await(self, payload, **kwargs):
        self.enqueued.append((payload, kwargs))
        return "manual reply"


class ManualClient(AsyncWebClient):
    def __init__(self):
        super().__init__(token="xoxb-test")
        self.posts = []
        self.updates = []
        self.ephemeral = []

    async def users_info(self, user):
        return {"user": {"real_name": "Person", "profile": {}}}

    async def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"ts": f"P{len(self.posts)}", "channel": kwargs["channel"]}

    async def chat_update(self, **kwargs):
        self.updates.append(kwargs)

    async def chat_delete(self, **kwargs):
        pass

    async def chat_postEphemeral(self, **kwargs):
        self.ephemeral.append(kwargs)


async def manual_checks():
    os.environ.update({
        "SLACK_BOT_TOKEN": "xoxb-test",
        "SLACK_CHANNEL_ID": "C1",
        "SLACK_TEAM_ID": "T1",
        "REPLIKA_TYPE": "organization",
        "REPLIKA_TENANT_ID": "tenant",
        "SLACK_ADMIN_USER_IDS": "UADMIN",
    })
    manual_store = MemorySlackObservationStore()
    manual_gate = RuntimeGate(manual_store, should_respond=False)
    manual_agent = ManualAgent()
    manual_client = ManualClient()
    app = create_bot(
        manual_agent,
        observation_store=manual_store,
        reply_gate=manual_gate,
        observation_archiver=FakeArchiver(),
        slack_client=manual_client,
    )
    app.set_bot_identity("B1")
    await app.handle_incoming(message("300", "FYI only"), manual_client)
    check(not manual_client.posts, "observe-only manual message posts no placeholder")
    check(not manual_agent.enqueued, "observe-only manual message is not queued")
    await app.handle_incoming(message("301", "<@B1> help"), manual_client)
    check(len(manual_agent.enqueued) == 1, "manual explicit mention is actionable")
    check(
        manual_client.posts and manual_client.updates,
        "actionable manual message keeps placeholder delivery behavior",
    )
    approval = asyncio.create_task(manual_agent.approval_callback("rm protected", "red"))
    await asyncio.sleep(0)
    await app.resolve_approval(
        {
            "user": {"id": "UMEMBER"},
            "channel": {"id": "C1"},
            "actions": [{"value": "rm protected"}],
        },
        manual_client,
        True,
    )
    check(not approval.done(), "unauthorized approval click must not resolve future")
    check(manual_client.ephemeral, "unauthorized click receives ephemeral denial")
    await app.resolve_approval(
        {
            "user": {"id": "UADMIN", "name": "Admin"},
            "channel": {"id": "C1"},
            "actions": [{"value": "rm protected"}],
        },
        manual_client,
        False,
    )
    check(await approval is False, "configured admin can resolve approval")


asyncio.run(manual_checks())

check(
    store.get(observation_key("T1", "C1", "1.0"))["tombstone"],
    "exact lookup retains tombstone",
)
print("Slack observation and reply-gate checks passed.")

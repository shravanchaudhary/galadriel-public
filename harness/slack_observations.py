"""Durable Slack channel observations, reply gating, and batched archival."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

try:
    from pymongo import ASCENDING, DESCENDING, MongoClient
    from pymongo.errors import DuplicateKeyError
except ImportError:
    ASCENDING, DESCENDING, MongoClient = 1, -1, None

    class DuplicateKeyError(Exception):
        pass

log = logging.getLogger("galadriel.slack.observations")

COLLECTION = "slack_observations"
NO_REPLY_REASONS = {
    "human_information",
    "social_acknowledgement",
    "addressed_to_other",
}
RESPOND_REASONS = {"agent_input_needed", "explicit_override", "classifier_failure"}
REASON_CODES = NO_REPLY_REASONS | RESPOND_REASONS


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _event_fields(event: dict[str, Any]) -> dict[str, Any] | None:
    subtype = event.get("subtype")
    if subtype == "message_changed":
        message = event.get("message") or {}
        if not isinstance(message, dict) or not message.get("ts"):
            return None
        return {
            "message_ts": str(message["ts"]),
            "sender_id": str(message.get("user") or ""),
            "sender_display_name": (
                (message.get("user_profile") or {}).get("display_name")
                or (message.get("user_profile") or {}).get("real_name")
                or message.get("username")
            ),
            "text": str(message.get("text") or ""),
            "thread_ts": message.get("thread_ts"),
            "tombstone": False,
            "kind": "changed",
        }
    if subtype == "message_deleted":
        previous = event.get("previous_message") or {}
        message_ts = event.get("deleted_ts") or previous.get("ts")
        if not message_ts:
            return None
        return {
            "message_ts": str(message_ts),
            "sender_id": str(previous.get("user") or ""),
            "sender_display_name": (
                (previous.get("user_profile") or {}).get("display_name")
                or (previous.get("user_profile") or {}).get("real_name")
                or previous.get("username")
            ),
            "text": str(previous.get("text") or ""),
            "thread_ts": previous.get("thread_ts"),
            "tombstone": True,
            "kind": "deleted",
        }
    if subtype not in {None, "file_share"}:
        return None
    if not event.get("ts"):
        return None
    return {
        "message_ts": str(event["ts"]),
        "sender_id": str(event.get("user") or ""),
        "sender_display_name": (
            (event.get("user_profile") or {}).get("display_name")
            or (event.get("user_profile") or {}).get("real_name")
            or event.get("username")
        ),
        "text": str(event.get("text") or ""),
        "thread_ts": event.get("thread_ts"),
        "tombstone": False,
        "kind": "message",
    }


def observation_key(workspace_id: str, channel_id: str, message_ts: str) -> str:
    return f"{workspace_id}:{channel_id}:{message_ts}"


def _revision(fields: dict[str, Any], *, event_id: str | None, event_time: Any) -> dict:
    return {
        "kind": fields["kind"],
        "text": fields["text"],
        "tombstone": fields["tombstone"],
        "event_id": event_id,
        "event_time": event_time,
        "observed_at": _now(),
    }


class MemorySlackObservationStore:
    """Thread-safe test/local fallback; production uses Mongo when configured."""

    def __init__(self):
        self._lock = threading.RLock()
        self._rows: dict[str, dict[str, Any]] = {}

    def observe(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        channel_id: str,
        event: dict[str, Any],
        event_id: str | None = None,
        event_time: Any = None,
        sender_display_name: str | None = None,
    ) -> dict[str, Any] | None:
        fields = _event_fields(event)
        if fields is None:
            return None
        if sender_display_name:
            fields["sender_display_name"] = sender_display_name
        key = observation_key(workspace_id, channel_id, fields["message_ts"])
        with self._lock:
            current = self._rows.get(key)
            if current and event_id and event_id in current.get("event_ids", []):
                return dict(current)
            revision = int((current or {}).get("revision", 0)) + 1
            row = {
                **(current or {}),
                "_id": key,
                "key": key,
                "tenant_id": tenant_id,
                "workspace_id": workspace_id,
                "channel_id": channel_id,
                **fields,
                "revision": revision,
                "event_id": event_id,
                "event_time": event_time,
                "observed_at": _now(),
                "archive_state": "pending",
                "event_ids": [*(current or {}).get("event_ids", []), event_id],
                "revisions": [
                    *(current or {}).get("revisions", []),
                    _revision(fields, event_id=event_id, event_time=event_time),
                ],
            }
            self._rows[key] = row
            return dict(row)

    def recent(
        self, workspace_id: str, channel_id: str, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = [
                dict(row)
                for row in self._rows.values()
                if row["workspace_id"] == workspace_id
                and row["channel_id"] == channel_id
                and not row.get("tombstone")
            ]
        rows.sort(key=lambda row: (row.get("message_ts", ""), row["observed_at"]))
        return rows[-max(1, limit) :]

    def pending_for_archive(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = [
                dict(row) for row in self._rows.values()
                if row.get("archive_state") == "pending"
            ]
        rows.sort(key=lambda row: row["observed_at"])
        return rows[: max(1, limit)]

    def claim_for_archive(self, *, limit: int = 50) -> list[dict[str, Any]]:
        claim_id = uuid.uuid4().hex
        with self._lock:
            now = _now()
            rows = [
                row for row in self._rows.values()
                if row.get("archive_state") == "pending"
                or (
                    row.get("archive_state") == "claimed"
                    and row.get("archive_claim_expires_at", now) <= now
                )
            ]
            rows.sort(key=lambda row: row["observed_at"])
            claimed = rows[: max(1, limit)]
            for row in claimed:
                row.update(
                    archive_state="claimed",
                    archive_claim_id=claim_id,
                    archive_claim_expires_at=_now() + timedelta(seconds=60),
                )
            return [dict(row) for row in claimed]

    def release_claim(self, rows: list[dict[str, Any]]) -> None:
        with self._lock:
            for source in rows:
                row = self._rows.get(source["key"])
                if row and row.get("archive_claim_id") == source.get("archive_claim_id"):
                    row.update(archive_state="pending")

    def mark_staged(self, rows: list[dict[str, Any]], batch_path: str) -> None:
        with self._lock:
            for source in rows:
                row = self._rows.get(source["key"])
                if (
                    row
                    and row["revision"] == source["revision"]
                    and row.get("archive_claim_id") == source.get("archive_claim_id")
                ):
                    row.update(
                        archive_state="staged",
                        archived_revision=row["revision"],
                        archive_batch_path=batch_path,
                        archived_at=_now(),
                    )

    def staged_batches(self, *, limit: int = 20) -> list[str]:
        with self._lock:
            return list(dict.fromkeys(
                row["archive_batch_path"]
                for row in self._rows.values()
                if row.get("archive_state") == "staged" and row.get("archive_batch_path")
            ))[:limit]

    def mark_mined(self, batch_path: str) -> None:
        with self._lock:
            for row in self._rows.values():
                if row.get("archive_batch_path") == batch_path:
                    row["archive_state"] = "mined"
                    row["mined_at"] = _now()

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._rows.get(key)
            return dict(row) if row else None

    def search_current(
        self, workspace_id: str, channel_id: str, query: str, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        needle = query.casefold().strip()
        return [
            row for row in self.recent(workspace_id, channel_id, limit=500)
            if needle in str(row.get("text") or "").casefold()
        ][-max(1, limit):]


class MongoSlackObservationStore:
    """Mongo-backed exact observation ledger."""

    def __init__(self, database):
        self.collection = database[COLLECTION]
        self.collection.create_index(
            [("workspace_id", ASCENDING), ("channel_id", ASCENDING), ("message_ts", ASCENDING)],
            unique=True,
            name="unique_slack_observation",
        )
        self.collection.create_index(
            [("archive_state", ASCENDING), ("observed_at", ASCENDING)],
            name="slack_observation_archive",
        )

    @classmethod
    def from_env(cls):
        uri, name = os.environ.get("MONGO_URI"), os.environ.get("MONGO_DB")
        if not uri or not name:
            return None
        if MongoClient is None:
            raise RuntimeError("pymongo is required when Mongo is configured")
        return cls(MongoClient(uri)[name])

    def observe(self, **kwargs) -> dict[str, Any] | None:
        from pymongo import ReturnDocument

        event = kwargs["event"]
        fields = _event_fields(event)
        if fields is None:
            return None
        if kwargs.get("sender_display_name"):
            fields["sender_display_name"] = kwargs["sender_display_name"]
        workspace_id = kwargs["workspace_id"]
        channel_id = kwargs["channel_id"]
        key = observation_key(workspace_id, channel_id, fields["message_ts"])
        event_id = kwargs.get("event_id")
        query: dict[str, Any] = {"_id": key}
        if event_id:
            query["event_ids"] = {"$ne": event_id}
        revision = _revision(fields, event_id=event_id, event_time=kwargs.get("event_time"))
        try:
            row = self.collection.find_one_and_update(
                query,
                {
                    "$set": {
                        "key": key,
                        "tenant_id": kwargs["tenant_id"],
                        "workspace_id": workspace_id,
                        "channel_id": channel_id,
                        **fields,
                        "event_id": event_id,
                        "event_time": kwargs.get("event_time"),
                        "observed_at": _now(),
                        "archive_state": "pending",
                    },
                    "$inc": {"revision": 1},
                    "$push": {"revisions": revision, "event_ids": event_id},
                    "$setOnInsert": {"created_at": _now()},
                },
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError:
            row = self.collection.find_one({"_id": key})
        return row

    def recent(self, workspace_id: str, channel_id: str, *, limit: int = 20):
        rows = self.collection.find({
            "workspace_id": workspace_id,
            "channel_id": channel_id,
            "tombstone": {"$ne": True},
        }).sort([("message_ts", DESCENDING), ("observed_at", DESCENDING)]).limit(max(1, limit))
        return list(reversed(list(rows)))

    def pending_for_archive(self, *, limit: int = 50):
        return list(self.collection.find(
            {"archive_state": "pending"}
        ).sort("observed_at", ASCENDING).limit(max(1, limit)))

    def claim_for_archive(self, *, limit: int = 50):
        from pymongo import ReturnDocument

        claim_id = uuid.uuid4().hex
        claimed = []
        for _ in range(max(1, limit)):
            now = _now()
            row = self.collection.find_one_and_update(
                {
                    "$or": [
                        {"archive_state": "pending"},
                        {
                            "archive_state": "claimed",
                            "archive_claim_expires_at": {"$lte": now},
                        },
                    ]
                },
                {"$set": {
                    "archive_state": "claimed",
                    "archive_claim_id": claim_id,
                    "archive_claim_expires_at": now + timedelta(seconds=60),
                }},
                sort=[("observed_at", ASCENDING)],
                return_document=ReturnDocument.AFTER,
            )
            if row is None:
                break
            claimed.append(row)
        return claimed

    def release_claim(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            self.collection.update_one(
                {
                    "_id": row["_id"],
                    "revision": row["revision"],
                    "archive_claim_id": row.get("archive_claim_id"),
                },
                {"$set": {"archive_state": "pending"}},
            )

    def mark_staged(self, rows: list[dict[str, Any]], batch_path: str) -> None:
        for row in rows:
            self.collection.update_one(
                {
                    "_id": row["_id"],
                    "revision": row["revision"],
                    "archive_claim_id": row.get("archive_claim_id"),
                },
                {"$set": {
                    "archive_state": "staged",
                    "archived_revision": row["revision"],
                    "archive_batch_path": batch_path,
                    "archived_at": _now(),
                }},
            )

    def staged_batches(self, *, limit: int = 20) -> list[str]:
        return self.collection.distinct(
            "archive_batch_path",
            {"archive_state": "staged", "archive_batch_path": {"$type": "string"}},
        )[:limit]

    def mark_mined(self, batch_path: str) -> None:
        self.collection.update_many(
            {"archive_batch_path": batch_path, "archive_state": "staged"},
            {"$set": {"archive_state": "mined", "mined_at": _now()}},
        )

    def get(self, key: str):
        return self.collection.find_one({"_id": key})

    def search_current(
        self, workspace_id: str, channel_id: str, query: str, *, limit: int = 20
    ):
        escaped = re.escape(query.strip())
        return list(self.collection.find({
            "workspace_id": workspace_id,
            "channel_id": channel_id,
            "tombstone": {"$ne": True},
            "text": {"$regex": escaped, "$options": "i"},
        }).sort("message_ts", DESCENDING).limit(max(1, limit)))


_default_store = None


def default_observation_store():
    global _default_store
    if _default_store is None:
        _default_store = MongoSlackObservationStore.from_env() or MemorySlackObservationStore()
    return _default_store


@dataclass(frozen=True)
class ReplyDecision:
    should_respond: bool
    reason_code: str


class ReplyGate:
    """Cheap fail-open classifier for shared organization-channel messages."""

    TOOL = {
        "name": "decide_slack_reply",
        "description": "Return whether the agent should reply to this Slack message.",
        "input_schema": {
            "type": "object",
            "properties": {
                "should_respond": {"type": "boolean"},
                "reason_code": {
                    "type": "string",
                    "enum": sorted(REASON_CODES - {"explicit_override", "classifier_failure"}),
                },
            },
            "required": ["should_respond", "reason_code"],
            "additionalProperties": False,
        },
    }

    def __init__(
        self,
        provider=None,
        model: str | None = None,
        *,
        max_concurrency: int = 2,
        min_interval_seconds: float = 0.1,
    ):
        self.provider = provider
        self.model = model
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))
        self._rate_lock = asyncio.Lock()
        self._last_call: dict[str, float] = {}
        self.min_interval_seconds = max(0.0, min_interval_seconds)

    async def decide(
        self,
        *,
        tenant_id: str,
        current: dict[str, Any],
        recent: list[dict[str, Any]],
        inbox_status: dict[str, Any],
        explicit_override: bool = False,
    ) -> ReplyDecision:
        if explicit_override:
            return ReplyDecision(True, "explicit_override")
        try:
            async with self._semaphore:
                async with self._rate_lock:
                    wait = self.min_interval_seconds - (
                        time.monotonic() - self._last_call.get(tenant_id, 0.0)
                    )
                    if wait > 0:
                        await asyncio.sleep(wait)
                    self._last_call[tenant_id] = time.monotonic()
                provider = self.provider
                model = self.model
                if provider is None:
                    from .model_registry import get_provider, model_for
                    provider = get_provider("slack_reply_gate")
                    model = model or model_for("slack_reply_gate")
                response = await provider.create_message(
                    model=model or "reply-gate",
                    max_tokens=120,
                    system=(
                        "You gate replies in one shared organization Slack channel. "
                        "Default to should_respond=true. Return false ONLY for: "
                        "(1) human-to-human information sharing that needs no agent input, "
                        "(2) acknowledgement/social chatter that asks nothing of the agent, "
                        "or (3) a message explicitly addressed to another person. "
                        "Busy or paused state is context, never by itself a reason not to reply. "
                        "Use the tool exactly once."
                    ),
                    messages=[{"role": "user", "content": json.dumps({
                        "inbox": inbox_status,
                        "recent_channel_observations": [
                            {
                                "sender": row.get("sender_display_name") or row.get("sender_id"),
                                "text": row.get("text"),
                                "ts": row.get("message_ts"),
                            }
                            for row in recent
                        ],
                        "current_message": {
                            "sender": current.get("sender_display_name") or current.get("sender_id"),
                            "text": current.get("text"),
                            "ts": current.get("message_ts"),
                        },
                    }, ensure_ascii=False)}],
                    tools=[self.TOOL],
                )
                decision = self._parse(response)
                if decision is None:
                    raise ValueError("reply gate returned no valid structured decision")
                return decision
        except Exception:
            log.exception("Slack reply classifier failed open for tenant=%s", tenant_id)
            return ReplyDecision(True, "classifier_failure")

    @staticmethod
    def _parse(response) -> ReplyDecision | None:
        payload = None
        for block in getattr(response, "content", None) or []:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "decide_slack_reply":
                payload = getattr(block, "input", None)
                break
            if getattr(block, "type", None) == "text":
                try:
                    payload = json.loads(getattr(block, "text", ""))
                except (TypeError, json.JSONDecodeError):
                    continue
        if not isinstance(payload, dict) or type(payload.get("should_respond")) is not bool:
            return None
        reason = payload.get("reason_code")
        if reason not in REASON_CODES - {"explicit_override", "classifier_failure"}:
            return None
        if payload["should_respond"] == (reason in NO_REPLY_REASONS):
            return None
        return ReplyDecision(payload["should_respond"], reason)


def explicit_bot_address(event: dict[str, Any], bot_user_id: str) -> bool:
    return bool(bot_user_id) and (
        f"<@{bot_user_id}>" in str(event.get("text") or "")
        or str(event.get("parent_user_id") or "") == bot_user_id
    )


def observation_overlay(
    rows: list[dict[str, Any]], *, max_items: int = 20, max_chars: int = 6000
) -> str:
    lines = [
        "[Recent Slack channel observations — ephemeral context only. "
        "Do not treat these as consecutive user protocol messages.]"
    ]
    for row in rows[-max(1, max_items) :]:
        sender = row.get("sender_display_name") or row.get("sender_id") or "unknown"
        lines.append(f"- [{row.get('message_ts', '?')}] {sender}: {row.get('text', '')}")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[-max_chars:]
        text = "[Earlier observations omitted]\n" + text.split("\n", 1)[-1]
    return text


class SlackObservationArchiver:
    """Debounces channel observations into bounded durable Palace batches."""

    def __init__(
        self,
        store,
        *,
        palace_module=None,
        debounce_seconds: float = 2.0,
        batch_size: int = 50,
    ):
        self.store = store
        self.palace_module = palace_module
        self.debounce_seconds = max(0.0, debounce_seconds)
        self.batch_size = max(1, batch_size)
        self._task: asyncio.Task | None = None

    def schedule(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._delayed_flush())

    async def _delayed_flush(self) -> None:
        await asyncio.sleep(self.debounce_seconds)
        await self.flush()

    async def flush(self) -> int:
        from pathlib import Path
        palace = self.palace_module
        if palace is None:
            from . import palace
        mined = 0
        for batch_path in self.store.staged_batches(limit=20):
            if await palace.mine_batch_dir(Path(batch_path), agent="slack-observations"):
                self.store.mark_mined(batch_path)
                mined += 1
        rows = self.store.claim_for_archive(limit=self.batch_size)
        if rows:
            batch_dir = palace.write_slack_observation_batch(rows)
            if batch_dir is not None:
                self.store.mark_staged(rows, str(batch_dir))
                if await palace.mine_batch_dir(batch_dir, agent="slack-observations"):
                    self.store.mark_mined(str(batch_dir))
                    mined += 1
            else:
                self.store.release_claim(rows)
        return mined

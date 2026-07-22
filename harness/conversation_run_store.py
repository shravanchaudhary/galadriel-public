"""Mongo-backed audit and recovery state for the shared user conversation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from pymongo import MongoClient, ReturnDocument

from .pricing import estimate_cost

log = logging.getLogger("galadriel.conversation_runs")

RUNS = "conversation_runs"
EVENTS = "conversation_events"
CHECKPOINTS = "conversation_checkpoints"
OUTBOX = "palace_outbox"

_SECRET_KEYS = {
    "api_key", "access_token", "auth_token", "authorization", "credential",
    "credentials", "password", "secret", "token", "totp", "totp_secret",
}
_SECRET_TEXT_RE = re.compile(
    r'(?i)(["\']?(?:api[_-]?key|access[_-]?token|auth(?:orization)?|password|'
    r'secret|token|totp(?:[_-]?secret)?)["\']?\s*[:=]\s*)(?:"[^"]*"|\'[^\']*\'|\S+)',
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def is_configured() -> bool:
    return bool(os.environ.get("MONGO_URI") and os.environ.get("MONGO_DB"))


_sync_database = None


def _sync_db():
    global _sync_database
    if _sync_database is not None:
        return _sync_database
    uri = os.environ.get("MONGO_URI")
    name = os.environ.get("MONGO_DB")
    if not uri or not name:
        return None
    _sync_database = MongoClient(uri)[name]
    return _sync_database


def _hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def sanitize(value: Any, stats: dict[str, int] | None = None, key: str = "") -> Any:
    """Remove secret values and binary images from durable observability data."""
    stats = stats if stats is not None else {"redactions": 0, "images_omitted": 0}
    if key.lower() in _SECRET_KEYS:
        stats["redactions"] += 1
        return "[redacted]"
    if isinstance(value, dict):
        if value.get("type") == "image" or (
            isinstance(value.get("data"), str) and len(value["data"]) > 1024
        ):
            stats["images_omitted"] += 1
            source = value.get("source") or {}
            return {
                "type": "image_omitted",
                "media_type": source.get("media_type"),
                "note": "[binary image omitted from conversation audit]",
            }
        return {str(k): sanitize(v, stats, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v, stats, key) for v in value]
    if isinstance(value, str):
        safe, count = _SECRET_TEXT_RE.subn(r"\1[redacted]", value)
        stats["redactions"] += count
        return safe
    return value


class ConversationRunRecorder:
    """Records one `respond()` turn inside the active logical main conversation."""

    def __init__(self, run: dict, *, source: str, turn_id: str | None = None):
        self.run_id = run["run_id"]
        self.turn_id = turn_id or str(uuid.uuid4())
        self.source = source
        self.model = run.get("model", "")
        self._system_hashes: set[str] = set()
        self._usage = {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0}
        self._cost_total = 0.0
        self._call_count = 0
        self._tool_count = 0
        self._images_omitted = 0
        self._redactions = 0
        self.request_context: dict[str, Any] = {}

    @classmethod
    async def start(
        cls,
        channel_id: str,
        *,
        source: str,
        model: str,
        provider: str,
        headroom_enabled: bool,
        client_dedup_key: str | None = None,
        request_context: dict[str, Any] | None = None,
    ) -> "ConversationRunRecorder | None":
        if not is_configured():
            return None
        try:
            from scripts.lib.db import get_db
            db = get_db()
            await ensure_indexes(db)
            now = _now()
            run = await db[RUNS].find_one({"channel_id": channel_id, "state": "active"})
            if run is None:
                run = {
                    "run_id": str(uuid.uuid4()),
                    "channel_id": channel_id,
                    "state": "active",
                    "started_at": now,
                    "last_active_at": now,
                    "sources": [source],
                    "event_count": 0,
                    "palace_cursor": 0,
                    "latest_checkpoint_id": None,
                    "system_prompt_versions": [],
                    "tokens": {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0},
                    "token_total": 0,
                    "cost_total": 0.0,
                    "model": model,
                    "provider": provider,
                    "headroom_enabled": bool(headroom_enabled),
                    "current_turn_id": None,
                }
                try:
                    await db[RUNS].insert_one(run)
                except Exception:
                    run = await db[RUNS].find_one({"channel_id": channel_id, "state": "active"})
                    if run is None:
                        raise
            await db[RUNS].update_one(
                {"run_id": run["run_id"]},
                {"$set": {"last_active_at": now, "current_turn_id": None}, "$addToSet": {"sources": source}},
            )
            recorder = cls(run, source=source)
            recorder.request_context = sanitize(request_context or {})
            return recorder
        except Exception as exc:
            log.warning("Conversation run start was not recorded: %s", exc)
            return None

    @classmethod
    async def for_active(cls, channel_id: str, *, source: str = "system") -> "ConversationRunRecorder | None":
        if not is_configured():
            return None
        try:
            from scripts.lib.db import get_db
            run = await get_db()[RUNS].find_one({"channel_id": channel_id, "state": "active"})
            return cls(run, source=source) if run else None
        except Exception as exc:
            log.warning("Could not load active conversation run: %s", exc)
            return None

    async def begin_turn(self, client_dedup_key: str | None = None) -> None:
        await self._update({"$set": {
            "current_turn_id": self.turn_id,
            "current_turn_state": "running",
            "last_active_at": _now(),
            "current_request_context": self.request_context,
        }})
        if client_dedup_key or self.request_context:
            await self._record_event(
                "turn_started", None, visibility="internal",
                meta={
                    "client_dedup_key": client_dedup_key,
                    "request_context": self.request_context,
                },
            )

    async def record_message(
        self,
        message: dict,
        *,
        visibility: str = "internal",
        kind: str = "protocol_message",
    ) -> int | None:
        stats = {"redactions": 0, "images_omitted": 0}
        safe = sanitize(message, stats)
        self._redactions += stats["redactions"]
        self._images_omitted += stats["images_omitted"]
        content = safe.get("content") if isinstance(safe, dict) else safe
        if isinstance(content, list):
            self._tool_count += sum(
                1 for block in content
                if isinstance(block, dict) and block.get("type") == "tool_use"
            )
        return await self._record_event(
            kind,
            content,
            visibility=visibility,
            role=safe.get("role") if isinstance(safe, dict) else None,
            thought=safe.get("_thought") if isinstance(safe, dict) else None,
        )

    async def record_direct_reply(self, text: str) -> None:
        await self._record_event(
            "direct_reply", sanitize(text), visibility="user", role="assistant",
        )

    async def record_system_blocks(self, blocks: list[dict]) -> None:
        stats = {"redactions": 0, "images_omitted": 0}
        safe = sanitize(blocks, stats)
        digest = _hash(safe)
        if digest in self._system_hashes:
            return
        self._system_hashes.add(digest)
        self._redactions += stats["redactions"]
        self._images_omitted += stats["images_omitted"]
        await self._update({"$push": {"system_prompt_versions": {
            "recorded_at": _now(), "hash": digest, "blocks": safe,
        }}})

    async def record_call(
        self,
        usage: dict[str, int],
        *,
        call_index: int,
        duration_ms: int,
        stop_reason: str,
        headroom_metrics: dict | None = None,
    ) -> None:
        for key in self._usage:
            self._usage[key] += int(usage.get(key, 0) or 0)
        cost = estimate_cost(self.model, usage)
        self._cost_total += float(cost.get("cost_total", 0) or 0)
        self._call_count = max(self._call_count, call_index + 1)
        token_total = sum(self._usage.values())
        await self._update({"$set": {
            "llm_call_count": self._call_count,
            "tool_call_count": self._tool_count,
            "tokens": dict(self._usage),
            "token_total": token_total,
            "cost_total": self._cost_total,
            "last_call": {
                "call_index": call_index, "duration_ms": duration_ms,
                "stop_reason": stop_reason, "headroom": headroom_metrics or {},
            },
        }})

    async def record_checkpoint(self, checkpoint: dict) -> None:
        """Persist a compaction/checkpoint boundary without ending this run."""
        try:
            from scripts.lib.db import get_db
            doc = {
                "checkpoint_id": str(uuid.uuid4()),
                "run_id": self.run_id,
                "created_at": _now(),
                **sanitize(checkpoint),
            }
            await get_db()[CHECKPOINTS].insert_one(doc)
            sequence = await self._record_event(
                "checkpoint", None, visibility="internal",
                meta={"checkpoint_id": doc["checkpoint_id"], **checkpoint},
            )
            await self._update({"$set": {
                "latest_checkpoint_id": doc["checkpoint_id"],
                "latest_checkpoint_sequence": sequence,
            }})
        except Exception as exc:
            log.warning("Conversation checkpoint was not recorded: %s", exc)

    async def finalize_turn(self, *, state: str, error: str | None = None) -> None:
        if state == "cancelled":
            try:
                from scripts.lib.db import get_db
                await get_db()[EVENTS].update_many(
                    {"run_id": self.run_id, "turn_id": self.turn_id},
                    {"$set": {"visibility": "internal", "cancelled": True}},
                )
            except Exception as exc:
                log.warning("Cancelled turn events could not be hidden from chat history: %s", exc)
        await self._update({"$set": {
            "current_turn_id": None,
            "current_turn_state": state,
            "last_active_at": _now(),
            "last_error": error,
            "event_count": await self._event_count(),
            "tool_call_count": self._tool_count,
            "llm_call_count": self._call_count,
            "tokens": dict(self._usage),
            "token_total": sum(self._usage.values()),
            "cost_total": self._cost_total,
            "images_omitted": self._images_omitted,
            "redaction_count": self._redactions,
        }})

    async def _event_count(self) -> int:
        try:
            from scripts.lib.db import get_db
            return int(await get_db()[EVENTS].count_documents({"run_id": self.run_id}))
        except Exception:
            return 0

    async def _record_event(
        self,
        kind: str,
        content: Any,
        *,
        visibility: str,
        role: str | None = None,
        thought: str | None = None,
        meta: dict | None = None,
    ) -> int | None:
        try:
            from scripts.lib.db import get_db
            db = get_db()
            counter = await db[RUNS].find_one_and_update(
                {"run_id": self.run_id},
                {"$inc": {"event_sequence": 1}, "$set": {"last_active_at": _now()}},
                return_document=ReturnDocument.AFTER,
            )
            if counter is None:
                return None
            sequence = int(counter.get("event_sequence", 0))
            event = {
                "run_id": self.run_id,
                "sequence": sequence,
                "turn_id": self.turn_id,
                "ts": _now(),
                "kind": kind,
                "visibility": visibility,
                "source": self.source,
                "role": role,
                "content": content,
            }
            if thought:
                event["thought"] = thought
            if meta:
                event["meta"] = sanitize(meta)
            await db[EVENTS].insert_one(event)
            return sequence
        except Exception as exc:
            log.warning("Conversation event was not recorded: %s", exc)
            return None

    async def _update(self, update: dict) -> None:
        try:
            from scripts.lib.db import get_db
            await get_db()[RUNS].update_one({"run_id": self.run_id}, update)
        except Exception as exc:
            log.warning("Conversation run update was not recorded: %s", exc)


async def ensure_indexes(db) -> None:
    """Idempotent indexes; repeated calls are harmless and avoid startup coupling."""
    await db[RUNS].create_index("run_id", unique=True)
    await db[RUNS].create_index(
        [("channel_id", 1), ("state", 1)],
        unique=True,
        partialFilterExpression={"state": "active"},
    )
    await db[EVENTS].create_index([("run_id", 1), ("sequence", 1)], unique=True)
    await db[EVENTS].create_index([("run_id", 1), ("ts", 1)])
    await db[CHECKPOINTS].create_index("checkpoint_id", unique=True)
    await db[CHECKPOINTS].create_index([("run_id", 1), ("created_at", 1)])
    await db[OUTBOX].create_index("batch_key", unique=True)
    await db[OUTBOX].create_index([("state", 1), ("created_at", 1)])


async def end_active_run(channel_id: str, reason: str) -> dict | None:
    try:
        from scripts.lib.db import get_db
        return await get_db()[RUNS].find_one_and_update(
            {"channel_id": channel_id, "state": "active"},
            {"$set": {"state": "ended", "ended_at": _now(), "end_reason": reason}},
            return_document=ReturnDocument.BEFORE,
        )
    except Exception as exc:
        log.warning("Conversation run close was not recorded: %s", exc)
        return None


async def mark_active_runs_interrupted() -> None:
    try:
        from scripts.lib.db import get_db
        await get_db()[RUNS].update_many(
            {"state": "active", "current_turn_state": "running"},
            {"$set": {"current_turn_state": "interrupted", "last_error": "Process stopped during turn."}},
        )
    except Exception as exc:
        log.warning("Could not reconcile active conversation runs: %s", exc)


def get_run(run_id: str) -> dict | None:
    db = _sync_db()
    return db[RUNS].find_one({"run_id": run_id}) if db is not None else None


def active_run(channel_id: str = "main") -> dict | None:
    db = _sync_db()
    return db[RUNS].find_one({"channel_id": channel_id, "state": "active"}) if db is not None else None


def runs_for_day(day: str) -> list[dict]:
    db = _sync_db()
    if db is None:
        return []
    start = datetime.fromisoformat(f"{day}T00:00:00+00:00")
    end = datetime.fromisoformat(f"{day}T23:59:59.999999+00:00")
    return list(db[RUNS].find({"started_at": {"$gte": start, "$lte": end}}).sort("started_at", -1))


def events_for_run(run_id: str, *, visibility: str | None = None) -> list[dict]:
    db = _sync_db()
    if db is None:
        return []
    query = {"run_id": run_id}
    if visibility:
        query["visibility"] = visibility
    return list(db[EVENTS].find(query).sort("sequence", 1))


def checkpoints_for_run(run_id: str) -> list[dict]:
    db = _sync_db()
    if db is None:
        return []
    return list(db[CHECKPOINTS].find({"run_id": run_id}).sort("created_at", 1))


def calls_for_run(run_id: str) -> list[dict]:
    db = _sync_db()
    if db is None:
        return []
    return list(db["llm_calls"].find({"run_id": run_id}).sort([("call_index", 1), ("ts", 1)]))


def recovery_state(channel_id: str = "main") -> tuple[dict | None, list[dict], dict | None]:
    """Return active run, protocol tail after its latest checkpoint, checkpoint."""
    run = active_run(channel_id)
    if run is None:
        return None, [], None
    db = _sync_db()
    if db is None:
        return run, [], None
    checkpoint = None
    checkpoint_id = run.get("latest_checkpoint_id")
    if checkpoint_id:
        checkpoint = db[CHECKPOINTS].find_one({"checkpoint_id": checkpoint_id})
    query: dict[str, Any] = {"run_id": run["run_id"], "kind": "protocol_message"}
    boundary = run.get("latest_checkpoint_sequence")
    if boundary is not None:
        query["sequence"] = {"$gt": boundary}
    tail = list(db[EVENTS].find(query).sort("sequence", 1))
    return run, tail, checkpoint

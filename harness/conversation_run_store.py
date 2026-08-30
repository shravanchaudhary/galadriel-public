"""Mongo-backed audit and recovery state for the shared user conversation."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from pymongo import MongoClient, ReturnDocument

from .pricing import estimate_cost

log = logging.getLogger("galadriel.conversation_runs")

RUNS = "conversation_runs"
EVENTS = "conversation_events"
CHECKPOINTS = "conversation_checkpoints"
OUTBOX = "palace_outbox"
# One doc per channel with a learning episode in flight, keyed by
# channel_id. Durable because the episode outlives the process: a
# restart mid-conversation must resume the SAME session, not split one
# chat into two (which would double-count its promotion confirmations
# and orphan the segments archived before the restart).
SESSIONS = "learning_sessions"

_SECRET_KEYS = {
    "api_key", "access_token", "auth_token", "authorization", "credential",
    "credentials", "password", "secret", "token", "totp", "totp_secret",
}
_SECRET_TEXT_RE = re.compile(
    r'(?i)(["\']?(?:api[_-]?key|access[_-]?token|auth(?:orization)?|password|'
    r'secret|token|totp(?:[_-]?secret)?)["\']?\s*[:=]\s*)(?:"[^"]*"|\'[^\']*\'|\S+)',
)
_TITLE_MAX = 72


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


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p)
    if content is None:
        return ""
    return str(content)


def title_from_user_content(content: Any) -> str | None:
    """Short list title from a direct user message body."""
    text = _content_text(content).strip()
    if not text:
        return None
    for prefix in ("[User instruction]\n",):
        if prefix in text:
            text = text.split(prefix, 1)[-1].strip()
    text = text.replace("\n", " ").strip()
    if not text:
        return None
    if len(text) > _TITLE_MAX:
        return text[: _TITLE_MAX - 1] + "…"
    return text


class ConversationRunRecorder:
    """Records one `respond()` turn inside the active logical main conversation.

    Conversation events and most run-doc metrics are buffered in memory and
    flushed once in ``finalize_turn`` to avoid per-tool-step Mongo writes.
    """

    def __init__(self, run: dict, *, source: str, turn_id: str | None = None):
        self.run_id = run["run_id"]
        self.turn_id = turn_id or str(uuid.uuid4())
        self.source = source
        self.model = run.get("model", "")
        self._has_title = bool(run.get("title"))
        self._system_hashes: set[str] = {
            v.get("hash") for v in (run.get("system_prompt_versions") or [])
            if isinstance(v, dict) and v.get("hash")
        }
        self._pending_system_versions: list[dict] = []
        self._usage = {
            "input": int((run.get("tokens") or {}).get("input", 0) or 0),
            "cache_read": int((run.get("tokens") or {}).get("cache_read", 0) or 0),
            "cache_write": int((run.get("tokens") or {}).get("cache_write", 0) or 0),
            "output": int((run.get("tokens") or {}).get("output", 0) or 0),
        }
        self._cost_total = float(run.get("cost_total", 0) or 0)
        self._call_count = int(run.get("llm_call_count", 0) or 0)
        self._tool_count = int(run.get("tool_call_count", 0) or 0)
        self._images_omitted = int(run.get("images_omitted", 0) or 0)
        self._redactions = int(run.get("redaction_count", 0) or 0)
        self._event_sequence = int(run.get("event_sequence", 0) or 0)
        self._pending_events: list[dict] = []
        self._title_seed: str | None = None
        self._last_call: dict | None = None
        self._latest_checkpoint_id = run.get("latest_checkpoint_id")
        self._latest_checkpoint_sequence = run.get("latest_checkpoint_sequence")
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
                    "event_sequence": 0,
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
                    "title": None,
                }
                try:
                    await db[RUNS].insert_one(run)
                except Exception:
                    run = await db[RUNS].find_one({"channel_id": channel_id, "state": "active"})
                    if run is None:
                        raise
            await db[RUNS].update_one(
                {"run_id": run["run_id"]},
                {"$set": {
                    "last_active_at": now,
                    "current_turn_id": None,
                    "model": model,
                    "provider": provider,
                    "headroom_enabled": bool(headroom_enabled),
                }, "$addToSet": {"sources": source}},
            )
            # Refresh fields that may have been written by concurrent flushes.
            run = await db[RUNS].find_one({"run_id": run["run_id"]}) or run
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
        # Turn bookkeeping stays in memory until finalize_turn.
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
        if (
            kind == "direct_user"
            and visibility == "user"
            and not self._has_title
            and not self._title_seed
        ):
            # Seed for LLM title at finalize; truncated text is the fallback.
            seed = _content_text(content).strip()
            if seed:
                self._title_seed = seed
        matched_ids = None
        event_kind = kind
        if isinstance(safe, dict):
            raw_ids = safe.get("matched_recall_ids")
            if isinstance(raw_ids, list):
                matched_ids = [str(x) for x in raw_ids if x]
            if kind == "recall_fire" or safe.get("kind") == "recall_fire":
                event_kind = "recall_fire"
        return await self._record_event(
            event_kind,
            content,
            visibility=visibility,
            role=safe.get("role") if isinstance(safe, dict) else None,
            thought=safe.get("_thought") if isinstance(safe, dict) else None,
            matched_recall_ids=matched_ids,
        )

    async def record_direct_reply(
        self, text: str, *, thought: str | None = None,
    ) -> None:
        await self._record_event(
            "direct_reply",
            sanitize(text),
            visibility="user",
            role="assistant",
            thought=(thought or "").strip() or None,
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
        self._pending_system_versions.append({
            "recorded_at": _now(), "hash": digest, "blocks": safe,
        })

    async def record_experiential_state(self, snapshot: dict) -> None:
        """Record the shared experiential lineage as internal audit evidence."""
        await self._record_event(
            "experiential_state",
            None,
            visibility="internal",
            meta={
                "version": int(snapshot.get("version", 0) or 0),
                "sequence": int(snapshot.get("sequence", 0) or 0),
                "dimensions": sanitize(snapshot.get("dimensions") or {}),
                "last_event": sanitize(snapshot.get("last_event") or {}),
            },
        )

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
        self._call_count += 1
        self._last_call = {
            "call_index": call_index, "duration_ms": duration_ms,
            "stop_reason": stop_reason, "headroom": headroom_metrics or {},
        }

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
            self._latest_checkpoint_id = doc["checkpoint_id"]
            self._latest_checkpoint_sequence = sequence
        except Exception as exc:
            log.warning("Conversation checkpoint was not recorded: %s", exc)

    async def finalize_turn(self, *, state: str, error: str | None = None) -> None:
        if state == "cancelled":
            for event in self._pending_events:
                if event.get("turn_id") == self.turn_id:
                    event["visibility"] = "internal"
                    event["cancelled"] = True
        await self._flush_pending(state=state, error=error)

    async def _flush_pending(self, *, state: str, error: str | None = None) -> None:
        try:
            from scripts.lib.db import get_db
            db = get_db()
            if self._pending_events:
                await db[EVENTS].insert_many(self._pending_events)
            fields: dict[str, Any] = {
                "current_turn_id": None,
                "current_turn_state": state,
                "last_active_at": _now(),
                "last_error": error,
                "event_sequence": self._event_sequence,
                "event_count": await self._event_count(),
                "tool_call_count": self._tool_count,
                "llm_call_count": self._call_count,
                "tokens": dict(self._usage),
                "token_total": sum(self._usage.values()),
                "cost_total": self._cost_total,
                "images_omitted": self._images_omitted,
                "redaction_count": self._redactions,
                "current_request_context": self.request_context,
            }
            if self._last_call is not None:
                fields["last_call"] = self._last_call
            if self._latest_checkpoint_id is not None:
                fields["latest_checkpoint_id"] = self._latest_checkpoint_id
                fields["latest_checkpoint_sequence"] = self._latest_checkpoint_sequence
            if self._title_seed and not self._has_title:
                title = None
                try:
                    import asyncio
                    from .chat_title import generate_chat_title

                    title = await asyncio.wait_for(
                        generate_chat_title(self._title_seed),
                        timeout=8,
                    )
                except Exception as exc:
                    log.warning("Chat title LLM skipped: %s", exc)
                if not title:
                    title = title_from_user_content(self._title_seed)
                if title:
                    fields["title"] = title
                    self._has_title = True
            update: dict[str, Any] = {"$set": fields}
            if self._pending_system_versions:
                update["$push"] = {
                    "system_prompt_versions": {"$each": self._pending_system_versions},
                }
            await db[RUNS].update_one({"run_id": self.run_id}, update)
            self._pending_events.clear()
            self._pending_system_versions.clear()
            self._title_seed = None
        except Exception as exc:
            log.warning("Conversation turn flush failed: %s", exc)

    async def _event_count(self) -> int:
        try:
            from scripts.lib.db import get_db
            return int(await get_db()[EVENTS].count_documents({"run_id": self.run_id}))
        except Exception:
            return len(self._pending_events)

    async def _record_event(
        self,
        kind: str,
        content: Any,
        *,
        visibility: str,
        role: str | None = None,
        thought: str | None = None,
        meta: dict | None = None,
        matched_recall_ids: list[str] | None = None,
    ) -> int | None:
        self._event_sequence += 1
        sequence = self._event_sequence
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
        if matched_recall_ids:
            event["matched_recall_ids"] = list(matched_recall_ids)
        self._pending_events.append(event)
        return sequence


async def ensure_indexes(db) -> None:
    """Idempotent indexes; repeated calls are harmless and avoid startup coupling."""
    await db[RUNS].create_index("run_id", unique=True)
    await db[RUNS].create_index([("started_at", -1)])
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


async def reactivate_run(run_id: str) -> dict | None:
    """Make an ended (or inactive) run the sole active run for its channel."""
    try:
        from scripts.lib.db import get_db
        db = get_db()
        run = await db[RUNS].find_one({"run_id": run_id})
        if run is None:
            return None
        channel_id = run.get("channel_id") or "main"
        if run.get("state") != "active":
            await end_active_run(channel_id, "switch")
            await db[RUNS].update_one(
                {"run_id": run_id},
                {"$set": {
                    "state": "active",
                    "last_active_at": _now(),
                    "current_turn_id": None,
                    "current_turn_state": None,
                }, "$unset": {"ended_at": "", "end_reason": ""}},
            )
        return await db[RUNS].find_one({"run_id": run_id})
    except Exception as exc:
        log.warning("Conversation run reactivate failed: %s", exc)
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


def protocol_tail_for_run(run_id: str) -> tuple[list[dict], dict | None]:
    """Protocol messages after the latest checkpoint, plus that checkpoint."""
    db = _sync_db()
    if db is None:
        return [], None
    run = db[RUNS].find_one({"run_id": run_id})
    if run is None:
        return [], None
    checkpoint = None
    checkpoint_id = run.get("latest_checkpoint_id")
    if checkpoint_id:
        checkpoint = db[CHECKPOINTS].find_one({"checkpoint_id": checkpoint_id})
    query: dict[str, Any] = {
        "run_id": run_id,
        "kind": {"$in": ["protocol_message", "recall_fire"]},
    }
    boundary = run.get("latest_checkpoint_sequence")
    if boundary is not None:
        query["sequence"] = {"$gt": boundary}
    tail = list(db[EVENTS].find(query).sort("sequence", 1))
    return tail, checkpoint


def buffer_messages_for_run(run_id: str) -> tuple[list[dict], dict | None]:
    """Rebuild the live agent message buffer from a run's durable events.

    Includes ``direct_user``, ``protocol_message``, and ``recall_fire`` events
    after the latest checkpoint (user turns are stored as direct_user, not
    protocol_message).
    """
    db = _sync_db()
    if db is None:
        return [], None
    run = db[RUNS].find_one({"run_id": run_id})
    if run is None:
        return [], None
    checkpoint = None
    checkpoint_id = run.get("latest_checkpoint_id")
    if checkpoint_id:
        checkpoint = db[CHECKPOINTS].find_one({"checkpoint_id": checkpoint_id})
    query: dict[str, Any] = {
        "run_id": run_id,
        "kind": {"$in": ["direct_user", "protocol_message", "recall_fire"]},
    }
    boundary = run.get("latest_checkpoint_sequence")
    if boundary is not None:
        query["sequence"] = {"$gt": boundary}
    messages: list[dict] = []
    for event in db[EVENTS].find(query).sort("sequence", 1):
        if event.get("role") is None or event.get("content") is None:
            continue
        message: dict[str, Any] = {
            "role": event["role"],
            "content": event.get("content"),
        }
        if event.get("thought"):
            message["_thought"] = event["thought"]
        if event.get("kind") == "recall_fire":
            message["kind"] = "recall_fire"
            ids = event.get("matched_recall_ids")
            if isinstance(ids, list) and ids:
                message["matched_recall_ids"] = [str(x) for x in ids if x]
        messages.append(message)
    return messages, checkpoint


def backfill_run_title(run_id: str) -> str | None:
    """Derive and persist title from the first user-visible event if missing."""
    db = _sync_db()
    if db is None:
        return None
    run = db[RUNS].find_one({"run_id": run_id}, {"title": 1})
    if run is None:
        return None
    if run.get("title"):
        return run["title"]
    event = db[EVENTS].find_one(
        {"run_id": run_id, "visibility": "user", "role": "user"},
        {"content": 1, "_id": 0},
        sort=[("sequence", 1)],
    )
    if event is None:
        return None
    title = title_from_user_content(event.get("content"))
    if not title:
        return None
    db[RUNS].update_one({"run_id": run_id, "title": {"$in": [None, ""]}}, {"$set": {"title": title}})
    return title


# Fields needed by the Chats rail. Inclusion projection — never pull
# system_prompt_versions or other blobs and strip them in Python.
_LIST_PROJECTION = {
    "_id": 0,
    "run_id": 1,
    "channel_id": 1,
    "state": 1,
    "started_at": 1,
    "sources": 1,
    "end_reason": 1,
    "llm_call_count": 1,
    "cost_total": 1,
    "title": 1,
}


def active_run(channel_id: str = "main", *, lean: bool = False) -> dict | None:
    """Return the active run. lean=True omits system_prompt_versions (list UI)."""
    db = _sync_db()
    if db is None:
        return None
    projection = dict(_LIST_PROJECTION) if lean else None
    return db[RUNS].find_one(
        {"channel_id": channel_id, "state": "active"},
        projection,
    )


def runs_for_day(day: str) -> list[dict]:
    db = _sync_db()
    if db is None:
        return []
    start = datetime.fromisoformat(f"{day}T00:00:00+00:00")
    end = datetime.fromisoformat(f"{day}T23:59:59.999999+00:00")
    return list(
        db[RUNS]
        .find({"started_at": {"$gte": start, "$lte": end}}, _LIST_PROJECTION)
        .sort("started_at", -1)
    )


def count_runs() -> int:
    """Metadata estimate — avoid exact count_documents round-trips on Atlas."""
    db = _sync_db()
    if db is None:
        return 0
    try:
        return int(db[RUNS].estimated_document_count())
    except Exception:
        return int(db[RUNS].count_documents({}))


_list_indexes_ready = False


def ensure_list_indexes() -> None:
    """Best-effort sync indexes for the Chats rail (once per process)."""
    global _list_indexes_ready
    if _list_indexes_ready:
        return
    db = _sync_db()
    if db is None:
        return
    try:
        db[RUNS].create_index([("started_at", -1)])
        _list_indexes_ready = True
    except Exception:
        pass


def recent_runs(limit: int = 100, *, skip: int = 0) -> list[dict]:
    db = _sync_db()
    if db is None:
        return []
    ensure_list_indexes()
    # Cap high enough for merged "all" pagination (offset + page + 1).
    limit = max(1, min(int(limit or 100), 5000))
    skip = max(0, int(skip or 0))
    return list(
        db[RUNS]
        .find({}, _LIST_PROJECTION)
        .sort("started_at", -1)
        .skip(skip)
        .limit(limit)
    )


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


# How long a record may sit before it stops being a resumable episode. This is
# a restart bridge, not an archive: a process that comes back a day later is not
# continuing the same occasion, and treating it as one would attribute fresh
# turns to an ancient episode. Also the backstop that keeps a crashed
# consolidation side channel, or a stray row from a local test run, from being
# adopted as real work forever.
SESSION_MAX_AGE_HOURS = 24


def load_session_states() -> dict[str, dict]:
    """Every RESUMABLE learning episode, as {channel_id: {session_id, segments}}.

    Read once at agent startup. Deliberately not consulted per call: the agent
    keeps the live copy in memory, and this is only the crash/restart bridge.
    Rows past SESSION_MAX_AGE_HOURS are dropped rather than returned, so the
    collection cannot accumulate abandoned episodes.
    """
    db = _sync_db()
    if db is None:
        return {}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=SESSION_MAX_AGE_HOURS)
    try:
        db[SESSIONS].delete_many({"updated_at": {"$lt": cutoff}})
    except Exception as e:
        log.warning(f"Session-state prune failed: {e}")
    try:
        return {
            doc["_id"]: {
                "session_id": doc.get("session_id"),
                "segments": list(doc.get("segments") or []),
            }
            for doc in db[SESSIONS].find({})
            # A consolidation side channel is disposable by construction: if a
            # crash left one behind, it is not an episode to resume.
            if doc.get("_id") and doc.get("session_id")
            and not str(doc["_id"]).startswith("__consolidate_")
        }
    except Exception as e:
        log.warning(f"Session-state load failed: {e}")
        return {}


def save_session_state(channel_id: str, session_id: str, segments: list) -> None:
    """Persist a channel's in-flight episode. Called when one is minted and
    whenever a compaction appends a segment to it — never per turn."""
    db = _sync_db()
    if db is None or not channel_id or not session_id:
        return
    try:
        db[SESSIONS].update_one(
            {"_id": channel_id},
            {"$set": {
                "session_id": session_id,
                "segments": list(segments or []),
                "updated_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
    except Exception as e:
        log.warning(f"Session-state save failed ({channel_id}): {e}")


def clear_session_state(channel_id: str) -> None:
    """Drop a channel's episode record — the episode is over (or was reset)."""
    db = _sync_db()
    if db is None or not channel_id:
        return
    try:
        db[SESSIONS].delete_one({"_id": channel_id})
    except Exception as e:
        log.warning(f"Session-state clear failed ({channel_id}): {e}")


def recovery_state(channel_id: str = "main") -> tuple[dict | None, list[dict], dict | None]:
    """Return active run, protocol tail after its latest checkpoint, checkpoint."""
    run = active_run(channel_id)
    if run is None:
        return None, [], None
    tail, checkpoint = buffer_messages_for_run(run["run_id"])
    return run, tail, checkpoint

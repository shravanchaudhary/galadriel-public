"""Durable, sanitized audit records for background-worker ticks."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

from pymongo import MongoClient

from .pricing import estimate_cost

log = logging.getLogger("galadriel.worker_ticks")

TICKS_COLLECTION = "worker_ticks"
EVENTS_COLLECTION = "worker_tick_events"
_SECRET_KEYS = {
    "api_key", "access_token", "auth_token", "authorization", "credential",
    "credentials", "password", "secret", "token", "totp", "totp_secret",
}
_SECRET_TEXT_RE = re.compile(
    r'(?i)(["\']?(?:api[_-]?key|access[_-]?token|auth(?:orization)?|password|'
    r'secret|token|totp(?:[_-]?secret)?)["\']?\s*[:=]\s*)(?:"[^"]*"|\'[^\']*\'|\S+)',
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _sync_db():
    uri = os.environ.get("MONGO_URI")
    name = os.environ.get("MONGO_DB")
    if not uri or not name:
        return None
    return MongoClient(uri)[name]


def is_configured() -> bool:
    return bool(os.environ.get("MONGO_URI") and os.environ.get("MONGO_DB"))


def _safe_value(value: Any, stats: dict[str, int], key: str = "") -> Any:
    """Remove secrets and binary media while retaining readable audit evidence."""
    if key.lower() in _SECRET_KEYS:
        stats["redactions"] += 1
        return "[redacted]"
    if isinstance(value, dict):
        block_type = value.get("type")
        if block_type == "image" or (
            "data" in value and isinstance(value["data"], str)
            and len(value["data"]) > 1024
        ):
            stats["images_omitted"] += 1
            return {
                "type": "image_omitted",
                "media_type": (value.get("source") or {}).get("media_type"),
                "note": "[binary image omitted from worker tick audit]",
            }
        return {str(k): _safe_value(v, stats, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_safe_value(v, stats, key) for v in value]
    if isinstance(value, str):
        safe, count = _SECRET_TEXT_RE.subn(r"\1[redacted]", value)
        stats["redactions"] += count
        return safe
    return value


def _json_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class WorkerTickRecorder:
    """Writes an append-only trace while one autonomous turn is executing.

    Used for the background worker and scheduler/completion loop channels
    (heartbeat, morning, reflection, …).
    """

    def __init__(
        self,
        tick_id: str,
        day_cet: str,
        started_at: datetime,
        user_prompt: str,
        *,
        model: str,
        provider: str,
        headroom_enabled: bool,
        tools_count: int = 0,
        channel_id: str = "worker",
    ):
        self.tick_id = tick_id
        self.day_cet = day_cet
        self.started_at = started_at
        self.user_prompt = user_prompt
        self.channel_id = channel_id or "worker"
        self.model = model
        self.provider = provider
        self.headroom_enabled = headroom_enabled
        self.tools_count = tools_count
        self._sequence = 0
        self._system_hashes: set[str] = set()
        self._experiential_versions: set[int] = set()
        self._usage = {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0}
        self._cost_total = 0.0
        self._call_count = 0
        self._tool_count = 0
        self._images_omitted = 0
        self._redactions = 0

    async def start(self) -> None:
        stats = {"images_omitted": 0, "redactions": 0}
        prompt = _safe_value(self.user_prompt, stats)
        self._images_omitted += stats["images_omitted"]
        self._redactions += stats["redactions"]
        doc = {
            "tick_id": self.tick_id,
            "channel_id": self.channel_id,
            "day_cet": self.day_cet,
            "started_at": self.started_at,
            "state": "running",
            "model": self.model,
            "provider": self.provider,
            "headroom_enabled": self.headroom_enabled,
            "tools_count": self.tools_count,
            "user_prompt": prompt,
            "prompt_hash": _json_hash(prompt),
            "system_prompt_versions": [],
            "experiential_states": [],
            "event_count": 0,
            "tool_call_count": 0,
            "llm_call_count": 0,
            "tokens": dict(self._usage),
            "token_total": 0,
            "cost_total": 0.0,
            "images_omitted": self._images_omitted,
            "redaction_count": self._redactions,
        }
        await self._insert_tick(doc)

    async def record_system_blocks(self, blocks: list[dict]) -> None:
        stats = {"images_omitted": 0, "redactions": 0}
        safe = _safe_value(blocks, stats)
        version_hash = _json_hash(safe)
        if version_hash in self._system_hashes:
            return
        self._system_hashes.add(version_hash)
        self._images_omitted += stats["images_omitted"]
        self._redactions += stats["redactions"]
        await self._update_tick({
            "$push": {"system_prompt_versions": {
                "recorded_at": _utcnow(),
                "hash": version_hash,
                "blocks": safe,
            }},
            "$set": {
                "system_hash": version_hash,
                "images_omitted": self._images_omitted,
                "redaction_count": self._redactions,
            },
        })

    async def record_experiential_state(self, snapshot: dict) -> None:
        """Attach compact shared-state lineage metadata to this loop tick."""
        version = int(snapshot.get("version", 0) or 0)
        if version in self._experiential_versions:
            return
        self._experiential_versions.add(version)
        compact = {
            "recorded_at": _utcnow(),
            "version": version,
            "sequence": int(snapshot.get("sequence", 0) or 0),
            "dimensions": _safe_value(snapshot.get("dimensions") or {}, {
                "images_omitted": 0, "redactions": 0,
            }),
            "last_event": _safe_value(snapshot.get("last_event") or {}, {
                "images_omitted": 0, "redactions": 0,
            }),
        }
        await self._update_tick({
            "$push": {"experiential_states": compact},
            "$set": {
                "experiential_state_version": version,
                "experiential_event_sequence": compact["sequence"],
            },
        })

    async def record_message(self, message: dict) -> None:
        stats = {"images_omitted": 0, "redactions": 0}
        safe = _safe_value(message, stats)
        self._images_omitted += stats["images_omitted"]
        self._redactions += stats["redactions"]
        content = safe.get("content") if isinstance(safe, dict) else safe
        if isinstance(content, list):
            self._tool_count += sum(
                1 for block in content
                if isinstance(block, dict) and block.get("type") == "tool_use"
            )
        event = {
            "tick_id": self.tick_id,
            "sequence": self._sequence,
            "ts": _utcnow(),
            "role": safe.get("role", "unknown") if isinstance(safe, dict) else "unknown",
            "content": content,
        }
        if isinstance(safe, dict):
            if safe.get("_thought"):
                event["thought"] = safe["_thought"]
            if safe.get("kind"):
                event["kind"] = safe["kind"]
            raw_ids = safe.get("matched_recall_ids")
            if isinstance(raw_ids, list):
                ids = [str(x) for x in raw_ids if x]
                if ids:
                    event["matched_recall_ids"] = ids
        self._sequence += 1
        await self._insert_event(event)
        await self._update_tick({"$set": {
            "event_count": self._sequence,
            "tool_call_count": self._tool_count,
            "images_omitted": self._images_omitted,
            "redaction_count": self._redactions,
        }})

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
        total_input = self._usage["input"] + self._usage["cache_read"] + self._usage["cache_write"]
        await self._update_tick({"$set": {
            "llm_call_count": self._call_count,
            "tokens": dict(self._usage),
            "token_total": total_input + self._usage["output"],
            "cost_total": self._cost_total,
            "last_call": {
                "call_index": call_index,
                "duration_ms": duration_ms,
                "stop_reason": stop_reason,
                "headroom": headroom_metrics or {},
            },
        }})

    async def finalize(
        self,
        *,
        state: str,
        finished_at: datetime,
        worker_status: str | None = None,
        notification: str = "",
        error: str | None = None,
    ) -> None:
        duration_ms = max(0, int((finished_at - self.started_at).total_seconds() * 1000))
        cache_denominator = self._usage["input"] + self._usage["cache_read"] + self._usage["cache_write"]
        fields = {
            "state": state,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "worker_status": worker_status,
            "notification": notification,
            "error": error,
            "cache_hit_ratio": (
                self._usage["cache_read"] / cache_denominator if cache_denominator else 0.0
            ),
            "event_count": self._sequence,
            "tool_call_count": self._tool_count,
            "llm_call_count": self._call_count,
            "tokens": dict(self._usage),
            "cost_total": self._cost_total,
            "images_omitted": self._images_omitted,
            "redaction_count": self._redactions,
        }
        await self._update_tick({"$set": fields})

    async def _insert_tick(self, doc: dict) -> None:
        try:
            from scripts.lib.db import get_db
            db = get_db()
            await db[TICKS_COLLECTION].create_index("tick_id", unique=True)
            await db[TICKS_COLLECTION].create_index([("started_at", -1)])
            await db[TICKS_COLLECTION].create_index([("channel_id", 1), ("started_at", -1)])
            await db[TICKS_COLLECTION].create_index([("day_cet", 1), ("started_at", -1)])
            await db[TICKS_COLLECTION].create_index(
                [("day_cet", 1), ("channel_id", 1), ("started_at", -1)]
            )
            await db[TICKS_COLLECTION].insert_one(doc)
        except Exception as exc:
            log.warning("Worker tick start was not recorded: %s", exc)

    async def _insert_event(self, doc: dict) -> None:
        try:
            from scripts.lib.db import get_db
            db = get_db()
            await db[EVENTS_COLLECTION].create_index(
                [("tick_id", 1), ("sequence", 1)], unique=True,
            )
            await db[EVENTS_COLLECTION].insert_one(doc)
        except Exception as exc:
            log.warning("Worker tick event was not recorded: %s", exc)

    async def _update_tick(self, update: dict) -> None:
        try:
            from scripts.lib.db import get_db
            await get_db()[TICKS_COLLECTION].update_one({"tick_id": self.tick_id}, update)
        except Exception as exc:
            log.warning("Worker tick update was not recorded: %s", exc)


async def mark_running_ticks_interrupted() -> None:
    """Close runs left open by a process crash before the worker restarts."""
    try:
        from scripts.lib.db import get_db
        now = _utcnow()
        await get_db()[TICKS_COLLECTION].update_many(
            {"state": "running"},
            {"$set": {
                "state": "interrupted",
                "finished_at": now,
                "error": "Process stopped before this worker tick completed.",
            }},
        )
    except Exception as exc:
        log.warning("Could not recover stale worker ticks: %s", exc)


def _channel_query(channel_id: str | None) -> dict:
    """Match ticks for a channel. Legacy docs without channel_id are worker."""
    if not channel_id:
        return {}
    if channel_id == "worker":
        return {"$or": [
            {"channel_id": "worker"},
            {"channel_id": {"$exists": False}},
        ]}
    return {"channel_id": channel_id}


def ticks_for_day(day_cet: str, channel_id: str | None = None) -> list[dict]:
    db = _sync_db()
    if db is None:
        return []
    query = {"day_cet": day_cet, **_channel_query(channel_id)}
    return list(db[TICKS_COLLECTION].find(query).sort("started_at", -1))


# Fields needed by the Chats rail; excludes full prompts / system versions.
_LIST_PROJECTION = {
    "_id": 0,
    "tick_id": 1,
    "channel_id": 1,
    "day_cet": 1,
    "state": 1,
    "worker_status": 1,
    "started_at": 1,
    "notification": 1,
    "llm_call_count": 1,
    "cost_total": 1,
    "token_total": 1,
    "duration_ms": 1,
}


def count_ticks(channel_id: str | None = None) -> int:
    db = _sync_db()
    if db is None:
        return 0
    query = _channel_query(channel_id)
    if not query:
        try:
            return int(db[TICKS_COLLECTION].estimated_document_count())
        except Exception:
            pass
    return int(db[TICKS_COLLECTION].count_documents(query))


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
        db[TICKS_COLLECTION].create_index([("started_at", -1)])
        db[TICKS_COLLECTION].create_index([("channel_id", 1), ("started_at", -1)])
        _list_indexes_ready = True
    except Exception:
        pass


def recent_ticks(
    limit: int = 100,
    channel_id: str | None = None,
    *,
    skip: int = 0,
) -> list[dict]:
    db = _sync_db()
    if db is None:
        return []
    ensure_list_indexes()
    # Cap high enough for merged "all" pagination (offset + page + 1).
    limit = max(1, min(int(limit or 100), 5000))
    skip = max(0, int(skip or 0))
    return list(
        db[TICKS_COLLECTION]
        .find(_channel_query(channel_id), _LIST_PROJECTION)
        .sort("started_at", -1)
        .skip(skip)
        .limit(limit)
    )


def get_tick(tick_id: str) -> dict | None:
    db = _sync_db()
    return db[TICKS_COLLECTION].find_one({"tick_id": tick_id}) if db is not None else None


def events_for_tick(tick_id: str) -> list[dict]:
    db = _sync_db()
    if db is None:
        return []
    return list(db[EVENTS_COLLECTION].find({"tick_id": tick_id}).sort("sequence", 1))


def calls_for_tick(tick_id: str) -> list[dict]:
    db = _sync_db()
    if db is None:
        return []
    return list(db["llm_calls"].find({"tick_id": tick_id}).sort([("call_index", 1), ("ts", 1)]))

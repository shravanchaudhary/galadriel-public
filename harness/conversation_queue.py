"""Durable, per-channel inbox and single-consumer turn coordinator."""

from __future__ import annotations

import asyncio
import copy
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

try:
    from pymongo import ASCENDING, MongoClient, ReturnDocument
    from pymongo.errors import DuplicateKeyError
except ImportError:  # Local unit tests may intentionally omit Mongo dependencies.
    ASCENDING = 1
    MongoClient = None
    ReturnDocument = None

    class DuplicateKeyError(Exception):
        pass


ITEMS = "conversation_inbox"
CHANNELS = "conversation_inbox_channels"
CLAIM_TTL = timedelta(seconds=30)
# Queue rail / reorder. Inclusion — payload can hold images; do not fetch-then-strip.
_LIST_PROJECTION = {
    "_id": 0,
    "id": 1,
    "channel": 1,
    "sequence": 1,
    "source": 1,
    "display_text": 1,
    "state": 1,
    "revision": 1,
    "created_at": 1,
    "updated_at": 1,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _public(doc: dict | None) -> dict | None:
    if doc is None:
        return None
    result = copy.deepcopy(doc)
    result.pop("_id", None)
    for key, value in list(result.items()):
        if isinstance(value, datetime):
            result[key] = value.isoformat()
    return result


def _new_item(
    *,
    channel: str,
    sequence: int,
    source: str,
    external_dedupe_key: str | None,
    sender: Any,
    payload: Any,
    display_text: str,
    reply_target: Any,
    overlay: str | None,
    request_context: dict[str, Any],
) -> dict:
    now = _now()
    return {
        "id": str(uuid.uuid4()),
        "channel": channel,
        "sequence": sequence,
        "source": source,
        "external_dedupe_key": external_dedupe_key,
        "sender": sender,
        "payload": payload,
        "display_text": display_text,
        "reply_target": reply_target,
        "overlay": overlay,
        "request_context": request_context,
        "state": "pending",
        "revision": 1,
        "created_at": now,
        "updated_at": now,
        "claimed_at": None,
        "completed_at": None,
    }


class MemoryConversationStore:
    """Thread-safe fallback used when Mongo is absent and by local tests."""

    def __init__(self):
        self._lock = threading.RLock()
        self._items: dict[str, dict] = {}
        self._channels: dict[str, dict] = {}

    def _channel(self, channel: str) -> dict:
        return self._channels.setdefault(channel, {
            "channel": channel, "next_sequence": 0, "paused": False,
            "busy": False, "consumer_id": None, "claim_id": None,
            "updated_at": _now(),
        })

    def enqueue(self, **fields) -> tuple[dict, bool]:
        with self._lock:
            fields.setdefault("request_context", {
                "source": fields["source"], "actor_id": "", "trusted": False,
                "trust_reason": "legacy_queue_item",
            })
            channel = fields["channel"]
            dedupe = fields.get("external_dedupe_key")
            if dedupe:
                for item in self._items.values():
                    if item["channel"] == channel and item.get("external_dedupe_key") == dedupe:
                        self._channel(channel)["paused"] = False
                        return _public(item), False
            state = self._channel(channel)
            state["next_sequence"] += 1
            item = _new_item(sequence=state["next_sequence"], **fields)
            self._items[item["id"]] = item
            state.update(paused=False, updated_at=_now())
            return _public(item), True

    def claim_pending(self, channel: str, consumer_id: str) -> list[dict]:
        with self._lock:
            state = self._channel(channel)
            if state["paused"] or (state["busy"] and state["consumer_id"] != consumer_id):
                return []
            pending = sorted(
                (i for i in self._items.values()
                 if i["channel"] == channel and i["state"] == "pending"),
                key=lambda i: i["sequence"],
            )
            if not pending:
                state.update(busy=False, consumer_id=None, claim_id=None)
                return []
            claim_id = str(uuid.uuid4())
            now = _now()
            for item in pending:
                item.update(state="claimed", claim_id=claim_id, claimed_at=now, updated_at=now)
            state.update(
                busy=True, consumer_id=consumer_id, claim_id=claim_id,
                lease_expires_at=now + CLAIM_TTL, updated_at=now,
            )
            return [_public(i) for i in pending]

    def renew_claim(self, channel: str, claim_id: str, consumer_id: str) -> None:
        with self._lock:
            state = self._channel(channel)
            if state.get("claim_id") == claim_id and state.get("consumer_id") == consumer_id:
                state["lease_expires_at"] = _now() + CLAIM_TTL

    def finish_claim(
        self,
        channel: str,
        claim_id: str,
        *,
        state: str,
        results: dict[str, str] | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            now = _now()
            for item in self._items.values():
                if item["channel"] == channel and item.get("claim_id") == claim_id:
                    item.update(
                        state=state,
                        result=(results or {}).get(item["id"]),
                        error=error,
                        completed_at=now if state == "completed" else None,
                        cancelled_at=now if state == "cancelled" else None,
                        failed_at=now if state == "failed" else None,
                        updated_at=now,
                    )
            channel_state = self._channel(channel)
            channel_state.update(busy=False, consumer_id=None, claim_id=None, updated_at=now)

    def pause(self, channel: str) -> bool:
        with self._lock:
            state = self._channel(channel)
            was_busy = bool(state["busy"])
            state.update(paused=True, updated_at=_now())
            return was_busy

    def status(self, channel: str) -> dict:
        with self._lock:
            state = self._channel(channel)
            depth = sum(
                1 for i in self._items.values()
                if i["channel"] == channel and i["state"] in {"pending", "claimed"}
            )
            return {
                "channel": channel, "paused": state["paused"],
                "busy": state["busy"], "depth": depth,
            }

    def list_pending(self, channel: str) -> list[dict]:
        with self._lock:
            return [
                _public(i) for i in sorted(self._items.values(), key=lambda x: x["sequence"])
                if i["channel"] == channel and i["state"] == "pending"
            ]

    def get(self, item_id: str) -> dict | None:
        with self._lock:
            return _public(self._items.get(item_id))

    def pending_channels(self) -> list[str]:
        with self._lock:
            return sorted({
                item["channel"] for item in self._items.values()
                if item["state"] == "pending"
                and not self._channel(item["channel"])["paused"]
            })

    def edit(self, item_id: str, revision: int, updates: dict) -> dict | None:
        with self._lock:
            item = self._items.get(item_id)
            if not item or item["state"] != "pending" or item["revision"] != revision:
                return None
            item.update(updates, revision=revision + 1, updated_at=_now())
            return _public(item)

    def delete(self, item_id: str, revision: int) -> dict | None:
        return self.edit(item_id, revision, {"state": "deleted", "deleted_at": _now()})

    def reorder(self, channel: str, ordered: list[dict]) -> list[dict] | None:
        with self._lock:
            current = self.list_pending(channel)
            if {i["id"] for i in current} != {i["id"] for i in ordered}:
                return None
            by_id = {i["id"]: i for i in current}
            if any(by_id[x["id"]]["revision"] != x.get("revision") for x in ordered):
                return None
            sequences = sorted(i["sequence"] for i in current)
            now = _now()
            for spec, sequence in zip(ordered, sequences):
                item = self._items[spec["id"]]
                item.update(sequence=sequence, revision=item["revision"] + 1, updated_at=now)
            return self.list_pending(channel)

    def recover(self) -> None:
        with self._lock:
            now = _now()
            for item in self._items.values():
                if item["state"] == "claimed":
                    paused = self._channel(item["channel"]).get("paused", False)
                    item.update(
                        state="cancelled" if paused else "pending",
                        claim_id=None,
                        claimed_at=None,
                        cancelled_at=now if paused else None,
                        result="(Stopped — turn cancelled.)" if paused else None,
                        updated_at=now,
                    )
            for state in self._channels.values():
                state.update(busy=False, consumer_id=None, claim_id=None, updated_at=now)


class MongoConversationStore:
    """Mongo implementation with atomic dedupe, claims, and revisions."""

    def __init__(self, database):
        self.db = database
        self._mutation_lock = threading.RLock()
        self.items = database[ITEMS]
        self.channels = database[CHANNELS]
        self.items.create_index("id", unique=True)
        self.items.create_index([("channel", ASCENDING), ("sequence", ASCENDING)], unique=True)
        self.items.create_index(
            [("channel", ASCENDING), ("external_dedupe_key", ASCENDING)],
            unique=True,
            partialFilterExpression={"external_dedupe_key": {"$type": "string"}},
        )
        self.items.create_index([("channel", ASCENDING), ("state", ASCENDING), ("sequence", ASCENDING)])
        self.channels.create_index("channel", unique=True)

    @classmethod
    def from_env(cls):
        uri, name = os.environ.get("MONGO_URI"), os.environ.get("MONGO_DB")
        if not uri or not name:
            return None
        if MongoClient is None:
            raise RuntimeError("pymongo is required when MONGO_URI/MONGO_DB are configured")
        return cls(MongoClient(uri)[name])

    def enqueue(self, **fields) -> tuple[dict, bool]:
        fields.setdefault("request_context", {
            "source": fields["source"], "actor_id": "", "trusted": False,
            "trust_reason": "legacy_queue_item",
        })
        channel = fields["channel"]
        counter = self.channels.find_one_and_update(
            {"channel": channel},
            {"$inc": {"next_sequence": 1}, "$setOnInsert": {
                "paused": False, "busy": False, "created_at": _now(),
            }},
            upsert=True, return_document=ReturnDocument.AFTER,
        )
        item = _new_item(sequence=int(counter["next_sequence"]), **fields)
        try:
            self.items.insert_one(item)
            created = True
        except DuplicateKeyError:
            dedupe = fields.get("external_dedupe_key")
            if not dedupe:
                raise
            item = self.items.find_one({
                "channel": channel, "external_dedupe_key": dedupe,
            })
            created = False
        # The item exists before unpause, so a consumer can never claim the old
        # backlog while missing the substantive message that resumed it.
        self.channels.update_one(
            {"channel": channel},
            {"$set": {"paused": False, "updated_at": _now()}},
        )
        return _public(item), created

    def claim_pending(self, channel: str, consumer_id: str) -> list[dict]:
        now = _now()
        self.items.update_many(
            {"channel": channel, "state": "claimed", "lease_expires_at": {"$lte": now}},
            {"$set": {"state": "pending", "claim_id": None, "claimed_at": None, "updated_at": now}},
        )
        state = self.channels.find_one_and_update(
            {
                "channel": channel, "paused": {"$ne": True},
                "$or": [
                    {"busy": {"$ne": True}},
                    {"consumer_id": consumer_id},
                    {"lease_expires_at": {"$lte": now}},
                ],
            },
            {"$set": {
                "busy": True, "consumer_id": consumer_id,
                "lease_expires_at": now + CLAIM_TTL, "updated_at": now,
            }},
            return_document=ReturnDocument.AFTER,
        )
        if state is None:
            return []
        pending = list(self.items.find(
            {"channel": channel, "state": "pending"},
        ).sort("sequence", ASCENDING))
        if not pending:
            self.channels.update_one(
                {"channel": channel, "consumer_id": consumer_id},
                {"$set": {"busy": False, "consumer_id": None, "claim_id": None}},
            )
            return []
        claim_id = str(uuid.uuid4())
        ids = [item["id"] for item in pending]
        self.items.update_many(
            {"id": {"$in": ids}, "state": "pending"},
            {"$set": {
                "state": "claimed", "claim_id": claim_id, "claimed_at": now,
                "lease_expires_at": now + CLAIM_TTL, "updated_at": now,
            }},
        )
        self.channels.update_one(
            {"channel": channel, "consumer_id": consumer_id},
            {"$set": {"claim_id": claim_id}},
        )
        return [_public(i) for i in self.items.find(
            {"claim_id": claim_id, "state": "claimed"},
        ).sort("sequence", ASCENDING)]

    def renew_claim(self, channel: str, claim_id: str, consumer_id: str) -> None:
        expires = _now() + CLAIM_TTL
        self.channels.update_one(
            {
                "channel": channel, "claim_id": claim_id,
                "consumer_id": consumer_id, "busy": True,
            },
            {"$set": {"lease_expires_at": expires, "updated_at": _now()}},
        )
        self.items.update_many(
            {"channel": channel, "claim_id": claim_id, "state": "claimed"},
            {"$set": {"lease_expires_at": expires}},
        )

    def finish_claim(
        self,
        channel: str,
        claim_id: str,
        *,
        state: str,
        results: dict[str, str] | None = None,
        error: str | None = None,
    ) -> None:
        now = _now()
        for item in self.items.find(
            {"channel": channel, "claim_id": claim_id},
            {"id": 1},
        ):
            self.items.update_one(
                {"_id": item["_id"], "state": "claimed", "claim_id": claim_id},
                {"$set": {
                    "state": state,
                    "result": (results or {}).get(item["id"]),
                    "error": error,
                    "completed_at": now if state == "completed" else None,
                    "cancelled_at": now if state == "cancelled" else None,
                    "failed_at": now if state == "failed" else None,
                    "updated_at": now,
                }},
            )
        self.channels.update_one(
            {"channel": channel, "claim_id": claim_id},
            {"$set": {
                "busy": False, "consumer_id": None, "claim_id": None,
                "lease_expires_at": None, "updated_at": now,
            }},
        )

    def pause(self, channel: str) -> bool:
        previous = self.channels.find_one_and_update(
            {"channel": channel},
            {"$set": {"paused": True, "updated_at": _now()},
             "$setOnInsert": {"next_sequence": 0, "busy": False}},
            upsert=True, return_document=ReturnDocument.BEFORE,
        )
        return bool(previous and previous.get("busy"))

    def status(self, channel: str) -> dict:
        state = self.channels.find_one({"channel": channel}) or {}
        depth = self.items.count_documents({
            "channel": channel, "state": {"$in": ["pending", "claimed"]},
        })
        return {
            "channel": channel, "paused": bool(state.get("paused")),
            "busy": bool(state.get("busy")), "depth": depth,
        }

    def list_pending(self, channel: str) -> list[dict]:
        return [_public(i) for i in self.items.find(
            {"channel": channel, "state": "pending"},
            _LIST_PROJECTION,
        ).sort("sequence", ASCENDING)]

    def get(self, item_id: str) -> dict | None:
        return _public(self.items.find_one({"id": item_id}))

    def pending_channels(self) -> list[str]:
        channels = self.items.distinct("channel", {"state": "pending"})
        return [
            channel for channel in channels
            if not (self.channels.find_one({"channel": channel}) or {}).get("paused")
        ]

    def edit(self, item_id: str, revision: int, updates: dict) -> dict | None:
        with self._mutation_lock:
            return _public(self.items.find_one_and_update(
                {"id": item_id, "state": "pending", "revision": revision},
                {"$set": {**updates, "updated_at": _now()}, "$inc": {"revision": 1}},
                return_document=ReturnDocument.AFTER,
            ))

    def delete(self, item_id: str, revision: int) -> dict | None:
        return self.edit(item_id, revision, {"state": "deleted", "deleted_at": _now()})

    def reorder(self, channel: str, ordered: list[dict]) -> list[dict] | None:
        with self._mutation_lock:
            return self._reorder_locked(channel, ordered)

    def _reorder_locked(self, channel: str, ordered: list[dict]) -> list[dict] | None:
        current = self.list_pending(channel)
        if {i["id"] for i in current} != {i["id"] for i in ordered}:
            return None
        by_id = {i["id"]: i for i in current}
        if any(by_id[x["id"]]["revision"] != x.get("revision") for x in ordered):
            return None
        sequences = sorted(i["sequence"] for i in current)
        # Temporary negative values avoid collisions with the unique sequence index.
        for index, spec in enumerate(ordered, 1):
            result = self.items.update_one(
                {"id": spec["id"], "state": "pending", "revision": spec["revision"]},
                {"$set": {"sequence": -index, "updated_at": _now()}, "$inc": {"revision": 1}},
            )
            if result.modified_count != 1:
                return None
        for spec, sequence in zip(ordered, sequences):
            self.items.update_one({"id": spec["id"], "state": "pending"}, {"$set": {"sequence": sequence}})
        return self.list_pending(channel)

    def recover(self) -> None:
        now = _now()
        paused_channels = self.channels.distinct("channel", {"paused": True})
        if paused_channels:
            self.items.update_many(
                {"state": "claimed", "channel": {"$in": paused_channels}},
                {"$set": {
                    "state": "cancelled", "claim_id": None,
                    "cancelled_at": now, "lease_expires_at": None,
                    "result": "(Stopped — turn cancelled.)", "updated_at": now,
                }},
            )
        self.items.update_many(
            {"state": "claimed", "lease_expires_at": {"$lte": now}},
            {"$set": {
                "state": "pending", "claim_id": None, "claimed_at": None,
                "lease_expires_at": None, "updated_at": now,
            }},
        )
        self.channels.update_many(
            {"busy": True, "lease_expires_at": {"$lte": now}},
            {"$set": {
                "busy": False, "consumer_id": None, "claim_id": None,
                "lease_expires_at": None, "updated_at": now,
            }},
        )


def default_store():
    mongo = MongoConversationStore.from_env()
    return mongo if mongo is not None else MemoryConversationStore()


class TurnStreamHub:
    """Fan-out live turn events to zero or more asyncio.Queue subscribers."""

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue] = []
        self.closed = False

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        if self.closed:
            q.put_nowait(None)
            return q
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    async def emit(self, event: Any) -> None:
        if self.closed:
            return
        for q in list(self._subscribers):
            await q.put(event)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for q in list(self._subscribers):
            q.put_nowait(None)
        self._subscribers.clear()


class ConversationQueue:
    """Coordinates durable storage with one asyncio consumer per channel."""

    def __init__(self, turn_runner: Callable, store=None, stop_callback: Callable | None = None):
        self.store = store or default_store()
        self.turn_runner = turn_runner
        self.stop_callback = stop_callback
        self.consumer_id = str(uuid.uuid4())
        self._consumers: dict[str, asyncio.Task] = {}
        self._active_turns: dict[str, asyncio.Task] = {}
        self._active_batches: dict[str, list[str]] = {}
        self._waiters: dict[str, list[asyncio.Future]] = {}
        self._hubs: dict[str, TurnStreamHub] = {}
        self.store.recover()

    def subscribe_stream(self, channel: str, *, create: bool = True) -> asyncio.Queue | None:
        """Subscribe to live turn events for ``channel``.

        When ``create`` is False, returns None if no open hub exists (attach path).
        """
        hub = self._hubs.get(channel)
        if hub is None or hub.closed:
            if not create:
                return None
            hub = TurnStreamHub()
            self._hubs[channel] = hub
        return hub.subscribe()

    def unsubscribe_stream(self, channel: str, q: asyncio.Queue) -> None:
        hub = self._hubs.get(channel)
        if hub is not None:
            hub.unsubscribe(q)

    def has_stream(self, channel: str) -> bool:
        hub = self._hubs.get(channel)
        return hub is not None and not hub.closed

    def open_stream(self, channel: str):
        """Ensure an open live-turn hub for ``channel`` and return its emit callback.

        Used by scheduler/loop channels that call ``agent.respond`` directly
        (not via the tower queue) so Tower can attach SSE mid-turn.
        """
        hub = self._hubs.get(channel)
        if hub is None or hub.closed:
            hub = TurnStreamHub()
            self._hubs[channel] = hub
        return hub.emit

    def close_stream(self, channel: str) -> None:
        """Close and drop the live-turn hub for ``channel``, if any."""
        hub = self._hubs.pop(channel, None)
        if hub is not None:
            hub.close()

    async def enqueue(
        self,
        payload: Any,
        *,
        channel: str,
        source: str,
        external_dedupe_key: str | None = None,
        sender: Any = None,
        display_text: str = "",
        reply_target: Any = None,
        overlay: str | None = None,
        request_context: dict[str, Any] | None = None,
    ) -> dict:
        if request_context is None:
            request_context = {
                "source": source,
                "actor_id": str((sender or {}).get("id") or ""),
                "trusted": source in {"tower", "discord", "direct"},
                "trust_reason": "trusted_surface" if source in {"tower", "discord", "direct"} else "unspecified",
            }
        request_context = copy.deepcopy(request_context)
        request_context["source"] = source
        item, _created = self.store.enqueue(
            channel=channel, source=source,
            external_dedupe_key=external_dedupe_key, sender=sender,
            payload=payload, display_text=display_text,
            reply_target=reply_target, overlay=overlay,
            request_context=request_context,
        )
        self._ensure_consumer(channel)
        return item

    def start(self) -> None:
        """Resume unpaused pending channels once the application loop is running."""
        for channel in self.store.pending_channels():
            self._ensure_consumer(channel)

    async def await_item(self, item_id: str) -> str:
        item = self.store.get(item_id)
        if (
            item
            and item["state"] in {"completed", "cancelled"}
            and isinstance(item.get("result"), str)
        ):
            return item["result"]
        if item and item["state"] == "failed":
            raise RuntimeError(item.get("error") or "Conversation turn failed")
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._waiters.setdefault(item_id, []).append(future)
        try:
            return await future
        finally:
            waiters = self._waiters.get(item_id)
            if waiters and future in waiters:
                waiters.remove(future)
                if not waiters:
                    self._waiters.pop(item_id, None)

    async def enqueue_and_wait(self, payload: Any, **kwargs) -> str:
        item = await self.enqueue(payload, **kwargs)
        return await self.await_item(item["id"])

    def _ensure_consumer(self, channel: str) -> None:
        task = self._consumers.get(channel)
        if task is None or task.done():
            self._consumers[channel] = asyncio.create_task(self._consume(channel))

    async def _consume(self, channel: str) -> None:
        while True:
            batch = self.store.claim_pending(channel, self.consumer_id)
            if not batch:
                return
            claim_id = batch[0]["claim_id"]
            newest = batch[-1]
            request_context = newest.get("request_context") or {
                "source": newest["source"],
                "actor_id": str((newest.get("sender") or {}).get("id") or ""),
                "trusted": newest["source"] in {"tower", "discord", "direct"},
                "trust_reason": "legacy_queue_item",
                "replika_type": "organization" if newest["source"] == "slack" else None,
            }
            emit = None
            if newest["source"] == "tower":
                hub = self._hubs.get(channel)
                if hub is None or hub.closed:
                    hub = TurnStreamHub()
                    self._hubs[channel] = hub
                emit = hub.emit
            payload = self._merge_payloads(batch)
            overlay = "\n\n".join(dict.fromkeys(
                item["overlay"] for item in batch if item.get("overlay")
            )) or None
            turn = asyncio.create_task(self.turn_runner(
                payload, channel_id=channel, emit=emit, overlay_context=overlay,
                run_source=newest["source"],
                client_dedup_key=newest.get("external_dedupe_key"),
                request_context=request_context,
            ))
            self._active_turns[channel] = turn
            self._active_batches[channel] = [item["id"] for item in batch]
            heartbeat = asyncio.create_task(self._heartbeat(channel, claim_id))
            outcome = "completed"
            error = None
            try:
                result = await turn
                if self.store.status(channel)["paused"]:
                    outcome = "cancelled"
            except asyncio.CancelledError:
                result = "(Stopped — turn cancelled.)"
                outcome = "cancelled"
            except Exception as exc:
                result = exc
                outcome = "failed"
                error = str(exc)
            finally:
                heartbeat.cancel()
                self._active_turns.pop(channel, None)
                self._active_batches.pop(channel, None)
                item_results = {
                    item["id"]: result if item["id"] == newest["id"] and isinstance(result, str) else ""
                    for item in batch
                }
                self.store.finish_claim(
                    channel,
                    claim_id,
                    state=outcome,
                    results=item_results,
                    error=error,
                )
                hub = self._hubs.pop(channel, None)
                if hub is not None:
                    # The enqueuing caller learns about a failure through its
                    # waiter, but everyone merely *attached* to the stream only
                    # saw the hub close and had no idea the turn had failed.
                    if outcome == "failed":
                        from .providers.llm_retry import error_detail, format_error

                        detail = error_detail(result)
                        await hub.emit({
                            "type": "error",
                            "error": format_error(detail),
                            "detail": detail,
                        })
                    hub.close()

            for item in batch:
                waiters = self._waiters.pop(item["id"], [])
                item_result = item_results[item["id"]]
                if outcome == "failed":
                    for waiter in waiters:
                        if not waiter.done():
                            waiter.set_exception(result)
                elif waiters:
                    for waiter in waiters:
                        if not waiter.done():
                            waiter.set_result(item_result)
            if outcome == "cancelled":
                return

    async def _heartbeat(self, channel: str, claim_id: str) -> None:
        try:
            while True:
                await asyncio.sleep(2)
                if self.store.status(channel)["paused"]:
                    if self.stop_callback is not None:
                        self.stop_callback(channel)
                    else:
                        turn = self._active_turns.get(channel)
                        if turn is not None and not turn.done():
                            turn.cancel()
                    return
                self.store.renew_claim(channel, claim_id, self.consumer_id)
        except asyncio.CancelledError:
            return

    @staticmethod
    def _merge_payloads(batch: list[dict]) -> Any:
        if len(batch) == 1:
            return batch[0]["payload"]
        blocks = []
        for index, item in enumerate(batch):
            if index:
                blocks.append({"type": "text", "text": "\n\n--- next queued message ---\n\n"})
            payload = item["payload"]
            if isinstance(payload, str):
                blocks.append({"type": "text", "text": payload})
            elif isinstance(payload, list):
                blocks.extend(payload)
            else:
                blocks.append({"type": "text", "text": str(payload)})
        return blocks

    def stop(self, channel: str) -> bool:
        was_busy = self.store.pause(channel)
        task = self._active_turns.get(channel)
        if task is not None and not task.done():
            if self.stop_callback is not None:
                self.stop_callback(channel)
            else:
                # Isolated queue tests may not have a GaladrielAgent cancel event.
                task.cancel()
            return True
        callback_stopped = self.stop_callback(channel) if self.stop_callback is not None else False
        return bool(was_busy or callback_stopped)

    def status(self, channel: str) -> dict:
        return self.store.status(channel)

    def pending(self, channel: str) -> list[dict]:
        return self.store.list_pending(channel)

    def edit(self, item_id: str, revision: int, *, display_text: str, payload: Any) -> dict | None:
        return self.store.edit(item_id, revision, {
            "display_text": display_text, "payload": payload,
        })

    def delete(self, item_id: str, revision: int) -> dict | None:
        return self.store.delete(item_id, revision)

    def reorder(self, channel: str, ordered: list[dict]) -> list[dict] | None:
        return self.store.reorder(channel, ordered)

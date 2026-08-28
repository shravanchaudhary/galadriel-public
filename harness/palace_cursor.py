"""Durable per-channel palace archive cursor.

One fact has to survive a restart for conversation archival to stay correct:
**how many messages of a channel's live buffer are already in the palace.**

This used to live in ``GaladrielAgent._last_archived_len`` (in-memory), so a
restart reset it to 0 and the shutdown archiver re-mined the whole buffer on top
of everything the scheduler had already checkpointed. Measured on the local
palace: 353 conversation drawers holding 181 distinct texts — 74% duplicates,
every sampled pair a `checkpoint` + `shutdown` collision.

One document per channel:

    {_id: "main", conversation_id: "<run_id|tick_id>", cursor: 42, updated_at: ...}

``conversation_id`` is the guard: when a channel starts a new conversation
(``/new``, a fresh worker tick), the id changes and the cursor restarts at 0 on
the next claim rather than needing an explicit reset call.

Chunk *numbering* deliberately does NOT live here. It is assigned by the miner
(``mongo_palace._reserve_chunk_numbers``), the only layer that knows how
many chunks a batch actually produces — this layer only ever knew a message
count, and reserving from that produced colliding numbers across checkpoints.

Both an async and a sync path exist because the shutdown archiver runs from an
atexit / signal handler where there is no event loop to await on.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from pymongo import MongoClient

log = logging.getLogger("galadriel.palace_cursor")

CURSORS = "palace_archive_cursors"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def is_configured() -> bool:
    return bool(os.environ.get("MONGO_URI") and os.environ.get("MONGO_DB"))


_sync_database = None


def _sync_db():
    """Sync handle for the shutdown path (atexit has no running loop)."""
    global _sync_database
    if _sync_database is not None:
        return _sync_database
    uri = os.environ.get("MONGO_URI")
    name = os.environ.get("MONGO_DB")
    if not uri or not name:
        return None
    _sync_database = MongoClient(uri)[name]
    return _sync_database


def _claim(document: dict | None, conversation_id: str,
           message_count: int) -> tuple[dict, dict]:
    """Pure claim arithmetic. Returns (claim, new_state).

    ``claim`` is the message slice [start, end) the caller must archive. A
    conversation_id mismatch (or no prior row) restarts the cursor — a new
    conversation never inherits the old one's position.
    """
    same = bool(document) and document.get("conversation_id") == conversation_id
    cursor = int(document.get("cursor", 0) or 0) if same else 0
    # A compaction/rollback can leave the buffer shorter than the cursor; treat
    # that as "nothing new" rather than claiming a negative slice.
    start = min(cursor, message_count)
    claim = {"start": start, "end": message_count, "reset": not same}
    new_state = {
        "conversation_id": conversation_id,
        "cursor": message_count,
        "updated_at": _now(),
    }
    return claim, new_state


async def claim_slice(
    channel_id: str,
    conversation_id: str,
    message_count: int,
) -> dict:
    """Claim the unarchived message slice for a channel and advance the cursor.

    Returns ``{start, end, reset}``. ``start == end`` means every message is
    already in the palace and the caller must not archive again.
    Fails open (claims the whole buffer) when Mongo is unreachable: re-archiving is recoverable, losing a conversation is not.
    """
    if not is_configured():
        return {"start": 0, "end": message_count, "reset": True}
    try:
        from scripts.lib.db import get_db
        collection = get_db()[CURSORS]
        document = await collection.find_one({"_id": channel_id})
        claim, new_state = _claim(document, conversation_id, message_count)
        await collection.update_one(
            {"_id": channel_id}, {"$set": new_state}, upsert=True,
        )
        return claim
    except Exception as exc:
        log.warning("Palace cursor claim failed (channel=%s): %s", channel_id, exc)
        return {"start": 0, "end": message_count, "reset": True}


def claim_slice_sync(
    channel_id: str,
    conversation_id: str,
    message_count: int,
) -> dict:
    """Synchronous ``claim_slice`` for the atexit/signal shutdown archiver."""
    database = _sync_db()
    if database is None:
        return {"start": 0, "end": message_count, "reset": True}
    try:
        collection = database[CURSORS]
        document = collection.find_one({"_id": channel_id})
        claim, new_state = _claim(document, conversation_id, message_count)
        collection.update_one({"_id": channel_id}, {"$set": new_state}, upsert=True)
        return claim
    except Exception as exc:
        log.warning("Palace cursor sync claim failed (channel=%s): %s", channel_id, exc)
        return {"start": 0, "end": message_count, "reset": True}


async def rewind(channel_id: str, conversation_id: str, cursor: int) -> None:
    """Put a channel's cursor back after a failed archive.

    ``claim_slice`` advances the cursor before the (slow, fallible) mine so two
    concurrent checkpoints cannot claim the same slice. If the archive then
    fails, the claim has to be undone or those messages are never mined again.
    Guarded on conversation_id so a rewind cannot resurrect a stale cursor into
    a conversation that has since been replaced.
    """
    if not is_configured():
        return
    try:
        from scripts.lib.db import get_db
        await get_db()[CURSORS].update_one(
            {"_id": channel_id, "conversation_id": conversation_id},
            {"$set": {"cursor": max(0, cursor), "updated_at": _now()}},
        )
    except Exception as exc:
        log.warning("Palace cursor rewind failed (channel=%s): %s", channel_id, exc)


def rewind_sync(channel_id: str, conversation_id: str, cursor: int) -> None:
    """Synchronous ``rewind`` for the shutdown archiver.

    The sync claim commits the cursor before the staging write, so a failed write
    would otherwise record those messages as archived and they would never be
    mined. There is no event loop at atexit, so this cannot reuse ``rewind``.
    """
    database = _sync_db()
    if database is None:
        return
    try:
        database[CURSORS].update_one(
            {"_id": channel_id, "conversation_id": conversation_id},
            {"$set": {"cursor": max(0, cursor), "updated_at": _now()}},
        )
    except Exception as exc:
        log.warning("Palace cursor sync rewind failed (channel=%s): %s", channel_id, exc)


async def reset_channel(channel_id: str) -> None:
    """Drop a channel's cursor so the next claim restarts from message 0.

    Called when the live buffer is wiped or replaced (``/new``, run switch,
    compaction rewrite) — the surviving messages are a different sequence, so a
    message-count cursor into the old one is meaningless.
    """
    if not is_configured():
        return
    try:
        from scripts.lib.db import get_db
        await get_db()[CURSORS].delete_one({"_id": channel_id})
    except Exception as exc:
        log.warning("Palace cursor reset failed (channel=%s): %s", channel_id, exc)


def peek(channel_id: str) -> dict | None:
    """Read a channel's cursor row (sync, diagnostics/tests only)."""
    database = _sync_db()
    if database is None:
        return None
    try:
        return database[CURSORS].find_one({"_id": channel_id})
    except Exception:
        return None

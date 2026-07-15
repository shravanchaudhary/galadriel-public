"""Durable Mongo outbox linking main-conversation ranges to MemPalace."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from . import conversation_run_store

log = logging.getLogger("galadriel.memory_sync")


def _now():
    return datetime.now(timezone.utc)


async def stage_and_mine_main(
    run_id: str,
    messages: list[dict],
    *,
    kind: str,
    agent: str,
) -> bool:
    """Stage a stable Palace batch, record it in Mongo, then mine it once."""
    if not messages:
        return True
    from . import palace

    batch_dir = palace.archive_conversation_durable("main", messages, kind=kind)
    if batch_dir is None:
        return False
    batch_key = batch_dir.name
    try:
        from scripts.lib.db import get_db
        db = get_db()
        await conversation_run_store.ensure_indexes(db)
        await db[conversation_run_store.OUTBOX].update_one(
            {"batch_key": batch_key},
            {"$setOnInsert": {
                "batch_key": batch_key,
                "run_id": run_id,
                "channel_id": "main",
                "kind": kind,
                "batch_path": str(batch_dir),
                "state": "staged",
                "attempts": 0,
                "created_at": _now(),
            }},
            upsert=True,
        )
    except Exception as exc:
        log.warning("Palace outbox stage was not recorded: %s", exc)

    ok = await palace.mine_batch_dir(batch_dir, agent=agent)
    try:
        from scripts.lib.db import get_db
        await get_db()[conversation_run_store.OUTBOX].update_one(
            {"batch_key": batch_key},
            {
                "$set": {
                    "state": "mined" if ok else "failed",
                    "mined_at": _now() if ok else None,
                    "last_error": None if ok else "mempalace mine failed",
                },
                "$inc": {"attempts": 1},
            },
        )
    except Exception as exc:
        log.warning("Palace outbox result was not recorded: %s", exc)
    return ok


async def drain_outbox(limit: int = 20) -> int:
    """Retry staged/failed main-conversation batches after restart."""
    if not conversation_run_store.is_configured():
        return 0
    try:
        from scripts.lib.db import get_db
        db = get_db()
        rows = await db[conversation_run_store.OUTBOX].find(
            {"state": {"$in": ["staged", "failed"]}},
        ).sort("created_at", 1).to_list(length=limit)
    except Exception as exc:
        log.warning("Palace outbox query failed: %s", exc)
        return 0

    from . import palace
    mined = 0
    for row in rows:
        batch_dir = Path(row.get("batch_path", ""))
        if not batch_dir.is_dir():
            await db[conversation_run_store.OUTBOX].update_one(
                {"_id": row["_id"]},
                {"$set": {"state": "failed", "last_error": "archive batch missing"}},
            )
            continue
        ok = await palace.mine_batch_dir(batch_dir, agent="outbox")
        await db[conversation_run_store.OUTBOX].update_one(
            {"_id": row["_id"]},
            {"$set": {
                "state": "mined" if ok else "failed",
                "mined_at": _now() if ok else None,
                "last_error": None if ok else "mempalace mine failed",
            }, "$inc": {"attempts": 1}},
        )
        mined += int(ok)
    return mined

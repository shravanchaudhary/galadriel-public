"""Durable Mongo outbox linking main-conversation ranges to the palace."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from . import conversation_run_store

log = logging.getLogger("galadriel.memory_sync")


def _now():
    return datetime.now(timezone.utc)


async def stage_main(
    run_id: str,
    messages: list[dict],
    *,
    kind: str,
    conversation_id: str | None = None,
) -> Path | None:
    """Durably stage a Palace batch + outbox row. Does not mine."""
    if not messages:
        log.info(
            f"[PalaceSync] stage skip run_id={run_id} kind={kind} "
            f"reason=empty_messages"
        )
        return None
    from . import palace

    log.info(
        f"[PalaceSync] staging run_id={run_id} kind={kind} "
        f"messages={len(messages)}"
    )
    batch_dir = palace.archive_conversation_durable(
        "main", messages, kind=kind,
        conversation_id=conversation_id or run_id,
    )
    if batch_dir is None:
        log.warning(
            f"[PalaceSync] stage failed run_id={run_id} kind={kind} "
            f"(archive_conversation_durable returned None)"
        )
        return None
    batch_key = batch_dir.name
    log.info(f"[PalaceSync] staged batch_key={batch_key} path={batch_dir}")
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
    return batch_dir


async def mine_staged_main(
    batch_dir: Path,
    *,
    agent: str,
    messages_count: int | None = None,
) -> bool:
    """Mine a previously staged batch and update the outbox row."""
    from . import palace

    batch_key = batch_dir.name
    ok = await palace.mine_batch_dir(batch_dir, agent=agent)
    n = messages_count if messages_count is not None else "?"
    log.info(
        f"[PalaceSync] mine {'ok' if ok else 'FAILED'} batch_key={batch_key} "
        f"agent={agent} messages={n}"
    )
    try:
        from scripts.lib.db import get_db
        await get_db()[conversation_run_store.OUTBOX].update_one(
            {"batch_key": batch_key},
            {
                "$set": {
                    "state": "mined" if ok else "failed",
                    "mined_at": _now() if ok else None,
                    "last_error": None if ok else "palace mine failed",
                },
                "$inc": {"attempts": 1},
            },
        )
    except Exception as exc:
        log.warning("Palace outbox result was not recorded: %s", exc)
    return ok


async def stage_and_mine_main(
    run_id: str,
    messages: list[dict],
    *,
    kind: str,
    agent: str,
    channel_id: str = "main",
    conversation_id: str | None = None,
) -> bool:
    """Stage a stable Palace batch, record it in Mongo, then mine it once."""
    if not messages:
        log.info(
            f"[PalaceSync] stage_and_mine skip run_id={run_id} kind={kind} "
            f"agent={agent} reason=empty_messages"
        )
        return True
    batch_dir = await stage_main(
        run_id, messages, kind=kind,
        conversation_id=conversation_id,
    )
    if batch_dir is None:
        return False
    return await mine_staged_main(
        batch_dir, agent=agent, messages_count=len(messages),
    )


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
                "last_error": None if ok else "palace mine failed",
            }, "$inc": {"attempts": 1}},
        )
        mined += int(ok)
    return mined


async def drain_slack_observations(limit: int = 50) -> int:
    """Resume staged/pending Slack observation batches after restart."""
    from .slack_observations import (
        SlackObservationArchiver,
        default_observation_store,
    )

    return await SlackObservationArchiver(
        default_observation_store(),
        debounce_seconds=0,
        batch_size=limit,
    ).flush()

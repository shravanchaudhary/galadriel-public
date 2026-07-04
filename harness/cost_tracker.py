"""Cost logging for every LLM API call.

Writes one doc per call to the `llm_calls` Mongo collection (infra log, not an
agent-facing workflow entity — no state machine, insert-only) so cumulative
cost can be sliced by day, channel, and model. Called from the two places an
API call actually happens: `GaladrielAgent._log_usage()` and
`compaction.compact_to_snapshot()`.

Writes are fire-and-forget (`asyncio.create_task`, wrapped in try/except) so a
Mongo hiccup never breaks an agent turn — this is a cost register, not the
operational system of record covered by config/DATA.md.
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from pymongo import MongoClient

from .pricing import estimate_cost

log = logging.getLogger("galadriel.cost")

COLLECTION = "llm_calls"

# Async side (writes) reuses the shared async connector, same as harness/db_ops.py.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


def log_call(channel_id: str, task: str, provider: str, model: str, usage: dict) -> None:
    """Fire-and-forget: cost a call's usage and insert it into `llm_calls`.

    `usage` is the same shape as `GaladrielAgent.last_usage`:
    {"input": int, "cache_read": int, "cache_write": int, "output": int}.
    Never raises — a logging failure must not break the turn.
    """
    try:
        cost = estimate_cost(model, usage)
        doc = {
            "ts": datetime.now(timezone.utc),
            "channel_id": channel_id,
            "task": task,
            "provider": provider,
            "model": model,
            "input_tokens": usage.get("input", 0),
            "cache_read_tokens": usage.get("cache_read", 0),
            "cache_write_tokens": usage.get("cache_write", 0),
            "output_tokens": usage.get("output", 0),
            **cost,
        }
        asyncio.create_task(_insert(doc))
    except Exception as e:
        log.warning(f"Cost logging failed (channel={channel_id}): {e}")


async def _insert(doc: dict) -> None:
    try:
        from lib.db import get_db  # noqa: E402  (path set above)
        db = get_db()
        await db[COLLECTION].insert_one(doc)
    except Exception as e:
        log.warning(f"Cost log insert failed: {e}")


# ── Read side (Tower UI) ────────────────────────────────────────────────
# Synchronous pymongo client, isolated from the async writer — same pattern
# as tower/workflows.py's `_db()`, since Flask routes are sync.

_sync_db = None


def _db():
    global _sync_db
    if _sync_db is not None:
        return _sync_db
    uri = os.environ.get("MONGO_URI")
    name = os.environ.get("MONGO_DB")
    if not uri or not name:
        return None
    _sync_db = MongoClient(uri)[name]
    return _sync_db


def is_configured() -> bool:
    """True when MONGO_URI/MONGO_DB are set, so the Tower page can distinguish
    'no data yet' from 'not wired up'."""
    return _db() is not None


def daily_totals(since: datetime | None = None) -> list[dict]:
    """Cost + tokens grouped by UTC date, most recent first.

    `since=None` returns all history. Each row: {date, calls, cost_total,
    input_tokens, output_tokens, cache_read_tokens, cache_write_tokens}.
    """
    db = _db()
    if db is None:
        return []
    match = {"ts": {"$gte": since}} if since else {}
    pipeline = [
        *([{"$match": match}] if match else []),
        {"$group": {
            "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$ts"}},
            "calls": {"$sum": 1},
            "cost_total": {"$sum": "$cost_total"},
            "input_tokens": {"$sum": "$input_tokens"},
            "output_tokens": {"$sum": "$output_tokens"},
            "cache_read_tokens": {"$sum": "$cache_read_tokens"},
            "cache_write_tokens": {"$sum": "$cache_write_tokens"},
        }},
        {"$sort": {"_id": -1}},
    ]
    rows = list(db[COLLECTION].aggregate(pipeline))
    for r in rows:
        r["date"] = r.pop("_id")
    return rows


def channel_totals(since: datetime | None = None) -> list[dict]:
    """Cost + tokens grouped by channel_id, highest cost first."""
    return _grouped_totals("$channel_id", "channel_id", since)


def model_totals(since: datetime | None = None) -> list[dict]:
    """Cost + tokens grouped by model, highest cost first."""
    return _grouped_totals("$model", "model", since)


def _grouped_totals(group_expr: str, key_name: str, since: datetime | None) -> list[dict]:
    db = _db()
    if db is None:
        return []
    match = {"ts": {"$gte": since}} if since else {}
    pipeline = [
        *([{"$match": match}] if match else []),
        {"$group": {
            "_id": group_expr,
            "calls": {"$sum": 1},
            "cost_total": {"$sum": "$cost_total"},
            "input_tokens": {"$sum": "$input_tokens"},
            "output_tokens": {"$sum": "$output_tokens"},
            "cache_read_tokens": {"$sum": "$cache_read_tokens"},
            "cache_write_tokens": {"$sum": "$cache_write_tokens"},
        }},
        {"$sort": {"cost_total": -1}},
    ]
    rows = list(db[COLLECTION].aggregate(pipeline))
    for r in rows:
        r[key_name] = r.pop("_id")
    return rows


def total_cost(since: datetime | None = None) -> float:
    """Single cumulative cost figure since `since` (or all-time if None)."""
    db = _db()
    if db is None:
        return 0.0
    match = {"ts": {"$gte": since}} if since else {}
    pipeline = [
        *([{"$match": match}] if match else []),
        {"$group": {"_id": None, "cost_total": {"$sum": "$cost_total"}}},
    ]
    rows = list(db[COLLECTION].aggregate(pipeline))
    return rows[0]["cost_total"] if rows else 0.0

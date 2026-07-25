"""Cost logging for every LLM API call.

Writes one doc per call to the `llm_calls` Mongo collection (infra log, not an
agent-facing workflow entity — no state machine, insert-only) so cumulative
cost can be sliced by day, channel, and model. Called from the two places an
API call actually happens: `GaladrielAgent._log_usage()` and
`compaction.compact_to_snapshot()`.

Writes are fire-and-forget (`asyncio.create_task`, wrapped in try/except) so a
Mongo hiccup never breaks an agent turn — this is a cost register, not the
operational system of record covered by knowledge/reference/data.md.
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


def log_call(
    channel_id: str,
    task: str,
    provider: str,
    model: str,
    usage: dict,
    *,
    headroom_enabled: bool = False,
    headroom_tokens_before: int = 0,
    headroom_tokens_after: int = 0,
    headroom_tokens_saved: int = 0,
    tick_id: str | None = None,
    run_id: str | None = None,
    turn_id: str | None = None,
    event_sequence: int | None = None,
    call_index: int | None = None,
    duration_ms: int | None = None,
    stop_reason: str | None = None,
) -> None:
    """Fire-and-forget: cost a call's usage and insert it into `llm_calls`.

    `usage` is the same shape as `GaladrielAgent.last_usage`:
    {"input": int, "cache_read": int, "cache_write": int, "output": int}.

    Headroom fields are always written (zeros when OFF / unavailable) so the
    Costs ON-vs-OFF aggregation stays simple. Legacy docs without these fields
    are treated as headroom_enabled=false.
    Never raises — a logging failure must not break the turn.
    """
    try:
        cost = estimate_cost(model, usage)
        doc = {
            "ts": datetime.now(timezone.utc),
            "tenant_id": os.environ.get("REPLIKA_TENANT_ID", "default"),
            "channel_id": channel_id,
            "task": task,
            "provider": provider,
            "model": model,
            "input_tokens": usage.get("input", 0),
            "cache_read_tokens": usage.get("cache_read", 0),
            "cache_write_tokens": usage.get("cache_write", 0),
            "output_tokens": usage.get("output", 0),
            "headroom_enabled": bool(headroom_enabled),
            "headroom_tokens_before": int(headroom_tokens_before or 0),
            "headroom_tokens_after": int(headroom_tokens_after or 0),
            "headroom_tokens_saved": int(headroom_tokens_saved or 0),
            **cost,
        }
        if tick_id is not None:
            doc["tick_id"] = tick_id
        if run_id is not None:
            doc["run_id"] = run_id
        if turn_id is not None:
            doc["turn_id"] = turn_id
        if event_sequence is not None:
            doc["event_sequence"] = int(event_sequence)
        if call_index is not None:
            doc["call_index"] = int(call_index)
        if duration_ms is not None:
            doc["duration_ms"] = int(duration_ms)
        if stop_reason is not None:
            doc["stop_reason"] = stop_reason
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
# as tower/apps.py's `_db()`, since Flask routes are sync.

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


def _match_stage(since: datetime | None, models: list[str] | None) -> dict | None:
    """Build a $match stage from optional time and model filters."""
    clauses = [{"tenant_id": os.environ.get("REPLIKA_TENANT_ID", "default")}]
    if since:
        clauses.append({"ts": {"$gte": since}})
    if models:
        clauses.append({"model": {"$in": models}})
    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def daily_totals(since: datetime | None = None, models: list[str] | None = None) -> list[dict]:
    """Cost + tokens grouped by UTC date, most recent first.

    `since=None` returns all history. Each row: {date, calls, cost_total,
    input_tokens, output_tokens, cache_read_tokens, cache_write_tokens}.
    """
    db = _db()
    if db is None:
        return []
    match = _match_stage(since, models)
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


def channel_totals(since: datetime | None = None, models: list[str] | None = None) -> list[dict]:
    """Cost + tokens grouped by channel_id, highest cost first."""
    return _grouped_totals("$channel_id", "channel_id", since, models)


def distinct_models(known_models: list[str] | tuple[str, ...] | None = None) -> list[str]:
    """All model names seen in the ledger, merged with `known_models`, sorted."""
    db = _db()
    seen: set[str] = set(known_models or [])
    if db is not None:
        for row in db[COLLECTION].distinct(
            "model",
            {"tenant_id": os.environ.get("REPLIKA_TENANT_ID", "default")},
        ):
            if row:
                seen.add(row)
    return sorted(seen)


def _empty_model_row(model: str) -> dict:
    return {
        "model": model,
        "calls": 0,
        "cost_input": 0.0,
        "cost_output": 0.0,
        "cost_cache_read": 0.0,
        "cost_cache_write": 0.0,
        "cost_total": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }


def model_totals(
    since: datetime | None = None,
    models: list[str] | None = None,
    include_models: list[str] | None = None,
) -> list[dict]:
    """Cost + tokens grouped by model, highest cost first.

    When `include_models` is set, every listed model appears (zero-filled if no
    calls in range). `models` filters which rows are returned.
    """
    rows = _grouped_totals("$model", "model", since, models)
    if include_models is None:
        return rows
    by_model = {row["model"]: row for row in rows}
    visible = include_models if models is None else [m for m in include_models if m in models]
    merged = [by_model.get(m) or _empty_model_row(m) for m in visible]
    merged.sort(key=lambda r: r["cost_total"], reverse=True)
    return merged


def _grouped_totals(
    group_expr: str,
    key_name: str,
    since: datetime | None,
    models: list[str] | None = None,
) -> list[dict]:
    db = _db()
    if db is None:
        return []
    match = _match_stage(since, models)
    pipeline = [
        *([{"$match": match}] if match else []),
        {"$group": {
            "_id": group_expr,
            "calls": {"$sum": 1},
            "cost_input": {"$sum": "$cost_input"},
            "cost_output": {"$sum": "$cost_output"},
            "cost_cache_read": {"$sum": "$cost_cache_read"},
            "cost_cache_write": {"$sum": "$cost_cache_write"},
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


def total_cost(since: datetime | None = None, models: list[str] | None = None) -> float:
    """Single cumulative cost figure since `since` (or all-time if None)."""
    db = _db()
    if db is None:
        return 0.0
    match = _match_stage(since, models)
    pipeline = [
        *([{"$match": match}] if match else []),
        {"$group": {"_id": None, "cost_total": {"$sum": "$cost_total"}}},
    ]
    rows = list(db[COLLECTION].aggregate(pipeline))
    return rows[0]["cost_total"] if rows else 0.0


def _empty_headroom_row(enabled: bool) -> dict:
    return {
        "headroom_enabled": enabled,
        "calls": 0,
        "cost_total": 0.0,
        "input_tokens": 0,
        "avg_input_tokens": 0.0,
        "headroom_tokens_saved": 0,
    }


def headroom_totals(
    since: datetime | None = None,
    models: list[str] | None = None,
) -> list[dict]:
    """Aggregate llm_calls by headroom_enabled for the Costs ON-vs-OFF card.

    Returns a 2-row list [ON, OFF], always — zero-filled when a bucket has no
    calls. Legacy docs missing `headroom_enabled` count as OFF.
    """
    on = _empty_headroom_row(True)
    off = _empty_headroom_row(False)
    db = _db()
    if db is None:
        return [on, off]
    match = _match_stage(since, models)
    pipeline = [
        *([{"$match": match}] if match else []),
        {"$group": {
            "_id": {"$ifNull": ["$headroom_enabled", False]},
            "calls": {"$sum": 1},
            "cost_total": {"$sum": {"$ifNull": ["$cost_total", 0]}},
            "input_tokens": {"$sum": {"$ifNull": ["$input_tokens", 0]}},
            "headroom_tokens_saved": {
                "$sum": {"$ifNull": ["$headroom_tokens_saved", 0]},
            },
        }},
    ]
    for row in db[COLLECTION].aggregate(pipeline):
        enabled = bool(row.get("_id"))
        calls = int(row.get("calls") or 0)
        input_tokens = int(row.get("input_tokens") or 0)
        bucket = {
            "headroom_enabled": enabled,
            "calls": calls,
            "cost_total": float(row.get("cost_total") or 0.0),
            "input_tokens": input_tokens,
            "avg_input_tokens": (input_tokens / calls) if calls else 0.0,
            "headroom_tokens_saved": int(row.get("headroom_tokens_saved") or 0),
        }
        if enabled:
            on = bucket
        else:
            off = bucket
    return [on, off]

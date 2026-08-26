#!/usr/bin/env python3
"""Is the learning system working? An operator tool, not a runtime path.

The stores each answer one question well and none answers this one: candidates
say what was written, retrieval_events what surfaced, memory_maintenance what
the harness did on its own. The number that actually says whether learning is
paying off — fired often, opened rarely — needs a join across two key
namespaces (`recall:<id>` for a fire, `memory:<id>` for an open) that no report
performs, because only `memory_candidates.trigger.recall_id` connects them.

Read a week of this before tuning any constant. The counts here are evidence;
the thresholds in harness/consolidation.py are guesses until they meet it.

    PYTHONPATH=. python3 scripts/memory_health.py [--days 7]

Read-only. Nothing here writes.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import consolidation  # noqa: E402

_TOP = 15


def _pct(part: int, whole: int) -> str:
    return "n/a" if not whole else f"{100 * part / whole:.0f}%"


async def _rows(name: str, query: dict) -> list[dict]:
    coll = await consolidation._collection(name)
    if coll is None:
        return []
    return [doc async for doc in coll.find(query)]


def _commits(candidates: list[dict]) -> list[str]:
    by_source: Counter = Counter()
    by_type: Counter = Counter()
    duplicates = errors = 0
    for c in candidates:
        status = c.get("status")
        if status == "duplicate":
            duplicates += 1
            continue
        if status != "committed":
            # A write that failed is the one a health report must not hide.
            errors += 1
            continue
        by_source[c.get("source") or "?"] += 1
        by_type[c.get("type") or "?"] += 1
    committed = sum(by_source.values())
    lines = [
        f"Committed: {committed}   Duplicates suppressed: {duplicates}   "
        f"Failed: {errors}"
    ]
    if by_source:
        lines.append(
            "  by source: "
            + ", ".join(f"{k}={v}" for k, v in by_source.most_common())
        )
        lines.append(
            "  by type:   "
            + ", ".join(f"{k}={v}" for k, v in by_type.most_common())
        )
    return lines


def _triggers(candidates: list[dict]) -> list[str]:
    """How many committed memories came out of the cue pass reachable."""
    eligible = [
        c for c in candidates
        if c.get("status") == "committed"
        and c.get("type") in consolidation.RECALL_ELIGIBLE_TYPES
    ]
    if not eligible:
        return ["Triggers: no eligible memories in window."]
    stamped = [c for c in eligible if isinstance(c.get("trigger"), dict)]
    # status "error"/"skipped" means generation ran and produced nothing — the
    # memory is inert, so counting it as "got a trigger" hides the failure.
    created = [c for c in stamped if c["trigger"].get("status") == "created"]
    failed = len(stamped) - len(created)
    never = len(eligible) - len(stamped)
    all_scores = [
        c["trigger"]["scores"] for c in created
        if isinstance(c["trigger"].get("scores"), dict)
    ]
    # `passed is None` means the score predates the field or the matcher was
    # disarmed — an absent measurement, so it stays out of BOTH sides of the
    # rate rather than counting as a failure.
    measured = [s for s in all_scores if s.get("passed") is not None]
    passed = [s for s in measured if s.get("passed")]
    unmeasured = len(created) - len(measured)
    repairs = sum(int(s.get("repair_rounds") or 0) for s in measured)
    return [
        f"Triggers: {len(created)}/{len(eligible)} eligible memories got one "
        f"({_pct(len(created), len(eligible))})."
        + (f"  generation failed={failed}" if failed else "")
        + (f"  never attempted={never}" if never else ""),
        f"  measured={len(measured)} passed={len(passed)} "
        f"({_pct(len(passed), len(measured))})"
        f"  unmeasured={unmeasured}  repair rounds spent={repairs}",
    ]


def _grading(events: list[dict]) -> list[str]:
    graded = [e for e in events if e.get("graded")]
    outcomes = Counter(e.get("outcome") or "?" for e in graded)
    used = sum(1 for e in graded if e.get("used"))
    by_kind = Counter(e.get("memory_kind") or "?" for e in events)
    return [
        f"Surfacings: {len(events)}  graded: {len(graded)} "
        f"({_pct(len(graded), len(events))} — ungraded means the episode "
        f"never ended, not that nothing was useful)",
        "  by kind: " + ", ".join(f"{k}={v}" for k, v in by_kind.most_common()),
        f"  used={used}/{len(graded)}  outcomes: "
        + ", ".join(f"{k}={v}" for k, v in outcomes.most_common()),
    ]


async def _fired_vs_opened(events: list[dict]) -> list[str]:
    """The join nothing else does: a recall fired N times, its memory opened M."""
    fires = Counter()
    opens = Counter()
    for e in events:
        key = e.get("memory_key") or ""
        if e.get("memory_kind") == "recall" and key.startswith("recall:"):
            fires[key[len("recall:"):]] += 1
        elif e.get("memory_kind") == "memory_open" and key.startswith("memory:"):
            # memory_open only. graph_expansion also lands under "memory:<id>",
            # but an inlined prerequisite is an edge's doing, not the trigger's
            # — counting it here would inflate the one number this exists for.
            opens[key[len("memory:"):]] += 1
    if not fires:
        return ["Fired vs opened: no recall fires in window."]
    backing = await consolidation.memory_ids_by_recall(list(fires))
    rows = []
    for recall_id, fired in fires.items():
        memory_id = backing.get(recall_id)
        rows.append((fired, opens.get(memory_id, 0), recall_id, memory_id))
    rows.sort(key=lambda r: (-r[0], r[1]))
    lines = [
        "Fired vs opened (a trigger that fires and is never opened is a cue "
        f"problem, not a content one). Top {_TOP}:",
    ]
    for fired, opened, recall_id, memory_id in rows[:_TOP]:
        target = memory_id or "(no backing memory)"
        lines.append(f"  {recall_id}: fired={fired} opened={opened} -> {target}")
    return lines


def _maintenance(rows: list[dict]) -> list[str]:
    by_kind = Counter(r.get("kind") or "?" for r in rows)
    pruned = sum(int(r.get("edges_pruned") or 0) for r in rows if r.get("kind") == "decay")
    weakened = sum(
        int(r.get("edges_weakened") or 0) for r in rows if r.get("kind") == "decay"
    )
    evicted = sum(
        len(r.get("evicted") or []) for r in rows if r.get("kind") == "promotion"
    )
    failures = by_kind.get("classify_failure", 0)
    return [
        "Maintenance: " + (
            ", ".join(f"{k}={v}" for k, v in by_kind.most_common()) or "(none)"
        ),
        f"  edges weakened={weakened} pruned={pruned}  "
        f"preferences evicted={evicted}  classifier failures={failures}",
        "  pruned edges are stored in full on their decay record — a prune "
        "that looks wrong can be read back and restored.",
    ]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7, help="window (default 7)")
    args = parser.parse_args()

    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    since_ts = since.timestamp()

    candidates = await _rows(
        consolidation.CANDIDATES_COLLECTION, {"created_at_ts": {"$gte": since_ts}}
    )
    events = await _rows(
        consolidation.RETRIEVAL_EVENTS_COLLECTION, {"ts": {"$gte": since}}
    )
    maintenance = await _rows(
        consolidation.MAINTENANCE_COLLECTION, {"ts": {"$gte": since}}
    )
    if not candidates and not events and not maintenance:
        print(
            "[memory health] nothing recorded in the last "
            f"{args.days} day(s) — or Mongo is not configured."
        )
        return 0

    blocks = [
        [f"[MEMORY HEALTH] last {args.days} day(s), since {since.date().isoformat()}"],
        _commits(candidates),
        _triggers(candidates),
        _grading(events),
        await _fired_vs_opened(events),
        _maintenance(maintenance),
    ]
    print("\n\n".join("\n".join(b) for b in blocks))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

#!/usr/bin/env python3
"""One-shot repairs for the 2026-09-03 memory consolidation. Dry-run by default.

1. Revert machine grades. `auto_grade_unopened` (removed by 3ef0f2c) and a
   verification incident (kb/learning-loop-measured.md) wrote used=false grades
   with grade_note "auto: fire's memory never opened this session". Every one
   pre-empted the model's own grade and inflated graded_count — four recalls
   were pushed into the bad-trigger bin by them alone. Revert = un-grade the
   event and walk graded_count back per memory_key. use_count never moved
   (all machine grades were used=false), so nothing else needs touching;
   last_graded is left as-is (the prior value is unrecoverable and it only
   nudges STALE aging).

2. Backfill KG-only candidate content. Committed memories stored as triplets
   with no prose were invisible to `memory(query=…)` and the edge shortlist
   (both filter on non-empty content). commit_candidate now renders triplets
   into content at commit time; this fills the rows written before that.

Usage: venv/bin/python scripts/repair_memory_telemetry.py [--apply]
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv
from pymongo import MongoClient

MACHINE_GRADE_NOTE = "auto: fire's memory never opened this session"


def render_triplets(triplets: list) -> str:
    return "; ".join(
        f"{t[0]} — {t[1]} — {t[2]}" for t in triplets if isinstance(t, (list, tuple)) and len(t) == 3
    )


def main() -> int:
    apply = "--apply" in sys.argv
    load_dotenv()
    client = MongoClient(os.environ["MONGO_URI"])
    db = client[os.environ.get("MONGO_DB", "galadriel")]
    events = db["retrieval_events"]
    stats = db["memory_stats"]
    candidates = db["memory_candidates"]

    # ── 1. machine-grade revert ──
    machine = list(events.find(
        {"grade_note": MACHINE_GRADE_NOTE, "graded": True},
        {"_id": 1, "retrieval_id": 1, "memory_key": 1, "used": 1},
    ))
    per_key: dict[str, int] = {}
    for e in machine:
        if e.get("used"):
            print(f"  SKIP {e.get('retrieval_id')}: used=true — not a machine grade shape, refusing")
            continue
        per_key[e["memory_key"]] = per_key.get(e["memory_key"], 0) + 1
    print(f"machine grades to revert: {sum(per_key.values())} events across {len(per_key)} keys")
    for key, n in sorted(per_key.items()):
        doc = stats.find_one({"memory_key": key}, {"graded_count": 1})
        current = int((doc or {}).get("graded_count", 0) or 0)
        target = max(0, current - n)
        print(f"  {key}: graded_count {current} -> {target} (-{n})")
        if apply:
            stats.update_one({"memory_key": key}, {"$set": {"graded_count": target}})
    if apply and machine:
        result = events.update_many(
            {"grade_note": MACHINE_GRADE_NOTE, "graded": True, "used": False},
            {"$unset": {"graded": "", "used": "", "outcome": "", "grade_note": ""}},
        )
        print(f"un-graded {result.modified_count} events")

    # ── 2. KG-only candidate content backfill ──
    kg_only = list(candidates.find(
        {"status": "committed", "kg_triplets.0": {"$exists": True},
         "$or": [{"content": ""}, {"content": None}]},
        {"_id": 1, "memory_id": 1, "kg_triplets": 1},
    ))
    print(f"KG-only candidates missing content: {len(kg_only)}")
    for doc in kg_only:
        content = render_triplets(doc.get("kg_triplets") or [])
        preview = content[:80]
        print(f"  {doc.get('memory_id')}: content <- {preview!r}")
        if apply and content:
            candidates.update_one({"_id": doc["_id"]}, {"$set": {"content": content}})

    if not apply:
        print("\nDRY RUN — re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

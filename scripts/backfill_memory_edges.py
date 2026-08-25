#!/usr/bin/env python3
"""Classify edges for committed memories that never went through the pass.

Edge classification normally runs in the background right after a commit, so
this exists for the memories that missed it: everything committed before the
graph shipped, and anything committed while `MEMORY_EDGE_AUTOGEN` was off or the
provider was down.

Idempotent. `_classify_edges` stamps `edges.classified_at` on the candidate
whether or not it found anything, so a memory that legitimately relates to
nothing is not re-billed a model call on every run. `--force` ignores the stamp.

    PYTHONPATH=. python3 scripts/backfill_memory_edges.py --dry-run
    PYTHONPATH=. python3 scripts/backfill_memory_edges.py

Writes are additive — a new edge document per relation found. Nothing is
deleted and no memory content is touched.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run", action="store_true",
        help="classify and print, write nothing",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="re-classify memories already stamped as classified",
    )
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()

    from harness import consolidation, memory_graph
    from harness.consolidation import CANDIDATES_COLLECTION, RECALL_ELIGIBLE_TYPES

    coll = await consolidation._collection(CANDIDATES_COLLECTION)
    if coll is None:
        print("No database configured — set MONGO_URI and MONGO_DB.")
        return 1

    query: dict = {
        "status": "committed",
        "type": {"$in": sorted(RECALL_ELIGIBLE_TYPES)},
    }
    if not args.force:
        query["edges.classified_at"] = {"$exists": False}
    cursor = coll.find(
        query, {"_id": 0, "memory_id": 1, "type": 1, "content": 1},
    ).sort("created_at_ts", 1).limit(args.limit)
    pending = [doc async for doc in cursor]

    if not pending:
        print("Nothing to classify — every committed memory has been through the pass.")
        return 0

    print(f"{len(pending)} memory/memories to classify"
          f"{' (dry run, no writes)' if args.dry_run else ''}.\n")
    total = 0
    # Dry run only: the write path resolves opposed asymmetric claims, so
    # without tracking them here the preview would overstate what lands.
    proposed: dict[tuple[str, str], tuple[str, float]] = {}
    for doc in pending:
        memory_id = doc.get("memory_id")
        text = (doc.get("content") or "").strip()
        if not memory_id or not text:
            continue
        head = " ".join(text.split())[:60]
        print(f"- {memory_id[:8]} [{doc.get('type')}] {head}")
        if args.dry_run:
            neighbours = await consolidation.shortlist_neighbours(
                text, exclude_id=memory_id,
            )
            edges = await memory_graph.classify_edges(
                memory_id, text, memory_type=doc.get("type") or "semantic",
                neighbours=neighbours,
            )
            for edge in edges:
                print(f"    would write {edge['relation']} -> {edge['to'][:8]} "
                      f"({edge['label']}, {edge['strength']})")
                if edge["relation"] not in memory_graph.ASYMMETRIC:
                    total += 1
                    continue
                rival = proposed.get((edge["to"], memory_id))
                if rival is None:
                    proposed[(memory_id, edge["to"])] = (
                        edge["relation"], edge["strength"],
                    )
                    total += 1
                else:
                    print(f"      ^ opposed to {rival[0]} {edge['to'][:8]} -> "
                          f"{memory_id[:8]}; collapses to RECALL_WITH")
            if not edges:
                print("    no relations")
            continue
        written = await consolidation._classify_edges(
            memory_id, doc.get("type") or "semantic", text, source="backfill",
        )
        print(f"    wrote {written} edge(s)")
        total += written

    print(f"\n{'Would write' if args.dry_run else 'Wrote'} {total} edge(s).")
    if not args.dry_run:
        print()
        print(await memory_graph.density_report())
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

#!/usr/bin/env python3
"""Measure how much memory graph actually exists. The go/no-go for expansion.

Traversal over an empty graph buys nothing, and the likeliest way this design
fails is not a bad algorithm — it is a classifier that finds almost nothing to
connect. That failure is invisible from the code: expansion would run, find no
edges, and behave exactly like a system without it.

So measure before trusting it. Run this against a real palace after the agent
has been learning for a while:

    PYTHONPATH=. python3 scripts/memory_graph_density.py

Read the output as:

  - **Coverage** — share of eligible memories with at least one outgoing edge.
    Below ~10% and expansion is dead weight; the bottleneck is extraction, not
    traversal, and the fix is the classifier prompt or the shortlist, not more
    graph code.
  - **Prerequisite share** — DEPENDS_ON + RECALL_BEFORE as a share of all edges.
    These are the only relations that change what the agent *does*. A graph made
    almost entirely of RECALL_WITH is a similarity index with extra steps, which
    the encoder already provides.
  - **Unused relations** — a relation the classifier never reaches for is either
    genuinely rare or badly described in the prompt.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import memory_graph  # noqa: E402


async def _coverage() -> str:
    """Share of edge-eligible memories that got at least one edge."""
    from harness.consolidation import CANDIDATES_COLLECTION, RECALL_ELIGIBLE_TYPES, _collection

    candidates = await _collection(CANDIDATES_COLLECTION)
    edges = await memory_graph._collection()
    if candidates is None or edges is None:
        return "Coverage: unavailable (no database configured)."
    try:
        eligible = await candidates.count_documents(
            {"status": "committed", "type": {"$in": sorted(RECALL_ELIGIBLE_TYPES)}},
        )
        connected = len(await edges.distinct("from"))
    except Exception as e:
        return f"Coverage: query failed ({type(e).__name__}: {e})."
    if not eligible:
        return "Coverage: no committed memories yet — nothing to connect."
    pct = 100.0 * connected / eligible
    verdict = "expansion is dead weight" if pct < 10 else "worth traversing"
    return (
        f"Coverage: {connected}/{eligible} eligible memories have an outgoing "
        f"edge ({pct:.1f}%) — {verdict}."
    )


async def _prerequisite_share() -> str:
    edges = await memory_graph._collection()
    if edges is None:
        return ""
    try:
        total = await edges.count_documents({})
        if not total:
            return ""
        prereq = await edges.count_documents(
            {"relation": {"$in": sorted(memory_graph.POLICY_PREREQUISITE)}},
        )
    except Exception:
        return ""
    pct = 100.0 * prereq / total
    note = (
        "mostly a similarity index" if pct < 20
        else "genuinely changes what the agent does"
    )
    return (
        f"Prerequisite share: {prereq}/{total} edges are DEPENDS_ON or "
        f"RECALL_BEFORE ({pct:.1f}%) — {note}."
    )


async def main() -> int:
    print(await memory_graph.density_report())
    print()
    print(await _coverage())
    share = await _prerequisite_share()
    if share:
        print(share)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

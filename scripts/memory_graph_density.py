#!/usr/bin/env python3
"""Print what the memory graph is worth. An operator tool, not a runtime path.

Density was the original go/no-go, on the assumption that the design's likeliest
failure was a classifier that connected almost nothing. Real data said otherwise:
connecting memories was easy, and being right about *how* they connect was the
hard part. A graph can be dense with edges no traversal follows, or dense with
edges that are followed and wrong — and both look identical to an edge count.

So the report separates what exists (edges), what operates (relations traversal
follows, and the memories they reach from), and what has helped (graded
expansion telemetry). It quotes no precision figure: nothing labels which
relations actually hold, and an invented percentage would read as evidence.

    PYTHONPATH=. python3 scripts/memory_graph_density.py

Read-only. Nothing here writes.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import memory_graph  # noqa: E402


async def main() -> int:
    print(await memory_graph.graph_report())
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

"""Unified learning: one conservative, typed, agent-facing tool.

The agent explicitly picks `type` (semantic | procedural | preference) and
states what to remember — there is no hidden internal LLM call deciding the
packaging anymore (previously a "cheap model" freeform-decomposition step).
Runtime `learn` calls are meant to be conservative and high-confidence
(explicit user corrections, clearly durable facts); broader judgment about
what else is worth keeping happens later, in the task-end and periodic
consolidators (see harness/consolidation.py), which propose candidates
through this exact same commit path — one write path, not seven.
"""

from __future__ import annotations

import logging

log = logging.getLogger("galadriel.learn")


async def learn(
    type: str,
    content: str = "",
    kg_triplets: list | None = None,
    topic: str | None = None,
    valid_from: str | None = None,
) -> str:
    """Package and store one learned item through the shared candidate pipeline.

    - type="semantic" + kg_triplets: crisp entity facts -> the knowledge graph.
    - type="semantic" + content (no kg_triplets): durable prose -> a palace drawer.
    - type="procedural": a reusable how-to -> knowledge/** + a palace drawer.
    - type="preference": how to behave for this user -> today's daily log + a
      palace drawer. Repeated statements are counted as confirmations and
      promoted into MEMORY.md at reflection time (see
      harness/consolidation.py:promote_preferences).

    See harness/consolidation.commit_candidate for the full validate/dedupe/
    commit/provenance path shared with the consolidators.
    """
    from . import consolidation

    result = await consolidation.commit_candidate(
        type=type,
        content=content,
        kg_triplets=kg_triplets,
        topic=topic,
        valid_from=valid_from,
        source="runtime",
    )
    if result["status"] == "error":
        return f"[error] {result['detail']}"
    return result["detail"]

"""Unified learning: one agent-facing tool that packages knowledge.

The agent describes WHAT it learned once; packaging decides the artifact mix:

  - KG triplets   → crisp entity facts / relationships (palace.kg_add)
  - palace drawer → durable prose worth re-reading (palace.add_drawer)
  - semantic recall → a when-to-recollect trigger (tools._learn_recall)

Two entry styles:
  - Explicit: the agent passes any of kg_triplets / drawer / recall and those
    are written verbatim (no inner LLM).
  - Freeform: only `content` is passed — one internal completion (cheap model,
    task "learn_packaging") decomposes it into the same structure.

Any combination may be produced, including recall-only or KG-only. Failures in
one artifact never block the others; the summary reports each outcome.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

log = logging.getLogger("galadriel.learn")

_DECOMPOSE_MAX_TOKENS = 1024

_DECOMPOSE_SYSTEM = (
    "You package one learned item into memory artifacts for a personal AI "
    "agent. Given CONTENT, return STRICT JSON only (no prose, no fences):\n"
    "{\n"
    '  "kg_triplets": [["subject", "predicate", "object"], ...],\n'
    '  "drawer": {"topic": "kebab-case-topic", "content": "..."} | null,\n'
    '  "recall": {"instruction": "...", "positive_examples": ["..."],\n'
    '             "lexical_cues": ["..."]} | null\n'
    "}\n"
    "Rules:\n"
    "- kg_triplets: only crisp entity-level facts or relationships (people, "
    "projects, tools, stable preferences). Empty list if none.\n"
    "- drawer: only when there is prose worth re-reading later (procedures, "
    "decisions with rationale, multi-line reference). Keep the agent's "
    "wording; do not pad. null if the content is a one-liner already covered "
    "by kg/recall.\n"
    "- recall: only when a clear future trigger moment exists where the agent "
    "should be reactively reminded. instruction = short action pointer (tool / "
    "file / palace topic), NOT an essay. positive_examples = 5-10 short "
    "realistic phrasings of that future moment (not paraphrases of the "
    "instruction). lexical_cues = 1-5 high-precision exact anchors. null if "
    "there is no reactive trigger.\n"
    "- Use any combination; unused parts are null / empty."
)


def _parse_decomposition(raw: str) -> dict | None:
    """Parse the internal completion's JSON (tolerating code fences)."""
    text = (raw or "").strip()
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except Exception:
        return None
    return data if isinstance(data, dict) else None


async def _decompose(content: str) -> dict | None:
    """One-shot cheap-model decomposition of freeform learned content."""
    try:
        from . import model_registry

        provider = model_registry.get_provider("learn_packaging")
        model = model_registry.model_for("learn_packaging")
        response = await provider.create_message(
            model=model,
            max_tokens=_DECOMPOSE_MAX_TOKENS,
            system=_DECOMPOSE_SYSTEM,
            messages=[{"role": "user", "content": f"CONTENT:\n{content[:4000]}"}],
            thinking=False,
        )
        parts = [
            getattr(block, "text", "") or ""
            for block in (getattr(response, "content", None) or [])
            if getattr(block, "type", None) == "text" or getattr(block, "text", None)
        ]
        return _parse_decomposition(" ".join(p for p in parts if p))
    except Exception as exc:
        log.warning("learn: decomposition completion failed: %s", exc)
        return None


def _clean_triplets(raw) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, dict):
            item = [item.get("subject"), item.get("predicate"), item.get("object")]
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            continue
        s, p, o = (str(x).strip() for x in item)
        if s and p and o:
            out.append((s, p, o))
    return out[:20]


async def learn(
    content: str,
    kg_triplets=None,
    drawer=None,
    recall=None,
) -> str:
    """Package and store one learned item. Returns a per-artifact summary."""
    body = (content or "").strip()
    if not body:
        return "[error] content is required — describe what was learned."

    explicit = any(x is not None for x in (kg_triplets, drawer, recall))
    if not explicit:
        plan = await _decompose(body)
        if plan is None:
            # Fail-safe: never lose the learning — archive the raw content.
            plan = {"drawer": {"topic": None, "content": body}}
            log.warning("learn: decomposition unavailable; archiving raw content to drawer")
        kg_triplets = plan.get("kg_triplets")
        drawer = plan.get("drawer")
        recall = plan.get("recall")

    lines: list[str] = []

    triplets = _clean_triplets(kg_triplets)
    if triplets:
        try:
            from . import palace

            loop = asyncio.get_running_loop()
            stored = 0
            for s, p, o in triplets:
                await loop.run_in_executor(
                    None, lambda s=s, p=p, o=o: palace.kg_add(subject=s, predicate=p, object=o)
                )
                stored += 1
            lines.append(f"kg: stored {stored} triplet(s).")
        except Exception as exc:
            lines.append(f"kg: [error] {exc}")

    if isinstance(drawer, dict) and (drawer.get("content") or "").strip():
        try:
            from . import palace

            result = await palace.add_drawer(
                content=str(drawer["content"]).strip(),
                topic=(drawer.get("topic") or None),
                wing="agent",
                room=(drawer.get("room") or None),
            )
            lines.append(f"drawer: {result}")
        except Exception as exc:
            lines.append(f"drawer: [error] {exc}")

    if isinstance(recall, dict) and (recall.get("instruction") or "").strip():
        try:
            from .tools import _learn_recall

            result = await _learn_recall(
                instruction=str(recall["instruction"]).strip(),
                recall_id=recall.get("recall_id"),
                positive_examples=recall.get("positive_examples"),
                negative_examples=recall.get("negative_examples"),
                lexical_cues=recall.get("lexical_cues"),
                positive_threshold=recall.get("positive_threshold"),
            )
            lines.append(f"recall: {result}")
        except Exception as exc:
            lines.append(f"recall: [error] {exc}")

    if not lines:
        return (
            "Nothing stored — packaging produced no artifacts. If this should "
            "be remembered, pass explicit kg_triplets / drawer / recall."
        )
    return "\n".join(lines)

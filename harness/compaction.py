"""Context compaction — snapshot the whole conversation into a compact memory block.

When a channel's context grows past the threshold (auto) or the user runs
`/compact` (manual), the entire conversation is replaced with a single
structured snapshot produced by the compaction model (default: gemini-2.5-flash;
Anthropic fallback: claude-haiku-4-5).

This module only produces the snapshot text. Where it lands is decided by the
agent: pre-turn / manual compaction stores it as its own system block (see
agent.compact_channel + agent.respond), while mid-loop compaction places it as
an assistant "progress" message (see agent._compact_midloop). Either way the
verbatim conversation is archived to the MemPalace first, so nothing is lost.
"""

import json
import logging

from . import cost_tracker
from . import model_registry
from .providers import BaseModelProvider

log = logging.getLogger("galadriel.compaction")

# Cap on the snapshot's own length. Large enough for a faithful structured
# summary of a long conversation, small enough to stay a fraction of context.
SNAPSHOT_MAX_TOKENS = 4000

COMPRESSION_MESSAGE = (
    "Context window limit reached. Produce a compressed memory snapshot so this task "
    "can continue in a fresh context without losing progress.\n\n"
    "Structure the output exactly as:\n"
    "GOAL: <the original user goal verbatim or faithfully paraphrased — this is the north star, never drop it>\n"
    "CONVERSATION FLOW: <ordered list of exchanges — 'user asked [brief intent] → agent [what was done/found]'. "
    "Reference user intent, do NOT quote messages verbatim. Keep each entry to one line.>\n"
    "FINDINGS: <what was discovered, confirmed, or ruled out — the 'what we now know'>\n"
    "LEARNED: <if learned recalls are listed below, cite them as pointers like "
    "'Learned X → recall(<id>)' instead of re-embedding the full fact prose>\n"
    "WORK DONE: <tools called, decisions made, results obtained — compact, no fluff>\n"
    "DEAD ENDS: <approaches tried that failed or were ruled out, so the next context doesn't retry them>\n"
    "CURRENT STATE: <exactly where things stand right now — ids, board state, partial results>\n"
    "NEXT: <what still needs to happen to close the goal>\n\n"
    "Be ruthlessly concise. Every word must earn its place. "
    "Preserve exact IDs, names, and numbers — those cannot be reconstructed from prose. "
    "Facts already stored via learn_recall must appear only as recall(<id>) pointers."
)


def _coerce_text(value) -> str:
    if isinstance(value, str):
        return value
    return str(value)


def _render_transcript(messages: list) -> str:
    """Flatten the API-format message list into a plain-text transcript for the
    compaction model. Images are omitted; tool payloads are truncated so a huge
    history doesn't blow up the compaction prompt (the verbatim copy lives in
    the palace archive)."""
    lines: list[str] = []
    for msg in messages:
        role = str(msg.get("role", "?")).upper()
        content = msg.get("content")
        if isinstance(content, str):
            lines.append(f"{role}: {content}")
            continue
        if not isinstance(content, list):
            lines.append(f"{role}: {_coerce_text(content)}")
            continue
        for block in content:
            if not isinstance(block, dict):
                lines.append(f"{role}: {_coerce_text(block)}")
                continue
            btype = block.get("type")
            if btype == "text":
                lines.append(f"{role}: {block.get('text', '')}")
            elif btype == "tool_use":
                try:
                    args = json.dumps(block.get("input", {}), ensure_ascii=False)
                except Exception:
                    args = str(block.get("input", {}))
                lines.append(f"{role} [tool_use {block.get('name', '?')}]: {args[:500]}")
            elif btype == "tool_result":
                result = block.get("content", "")
                if isinstance(result, list):
                    # Block-list result (text + image, e.g. screenshots) —
                    # keep the text, omit image payloads.
                    chunks = []
                    for b in result:
                        if isinstance(b, dict) and b.get("type") == "image":
                            chunks.append("[image omitted]")
                        elif isinstance(b, dict):
                            chunks.append(str(b.get("text", "")))
                        else:
                            chunks.append(str(b))
                    result = "\n".join(c for c in chunks if c)
                else:
                    result = _coerce_text(result)
                lines.append(f"{role} [tool_result]: {result[:1000]}")
            elif btype == "image":
                lines.append(f"{role} [image omitted]")
            else:
                lines.append(f"{role} [{btype}]")
    return "\n".join(lines)


def _log_compaction_cost(
    response, provider: BaseModelProvider, model: str, channel_id: str,
    run_id: str | None = None,
) -> None:
    try:
        usage = response.usage
        provider_name = type(provider).__name__.replace("Provider", "").lower()
        usage_dict = {
            "input": usage.input_tokens,
            "cache_read": getattr(usage, "cache_read_input_tokens", 0),
            "cache_write": getattr(usage, "cache_creation_input_tokens", 0),
            "output": usage.output_tokens,
        }
        cost_tracker.log_call(
            f"compaction:{channel_id}", "compaction", provider_name, model, usage_dict,
            run_id=run_id, stop_reason=getattr(response, "stop_reason", "end_turn"),
        )
    except Exception:
        log.debug("Could not log compaction usage", exc_info=True)


async def compact_to_snapshot(
    messages: list,
    prior_snapshot: str = "",
    api_key: str = None,
    provider: BaseModelProvider = None,
    channel_id: str = "compaction",
    run_id: str | None = None,
    learned_recall_ids: list[str] | None = None,
) -> dict:
    """Compress an entire conversation into one structured memory snapshot.

    If `prior_snapshot` is given (a snapshot from an earlier compaction of the
    same channel), it is folded in so cumulative compactions never lose ground.

    `learned_recall_ids` are recalls created/patched in the preceding learn pass;
    the snapshot should cite them as pointers rather than re-stating full prose.

    `channel_id` is only used to tag the cost log entry (the channel being
    compacted), so cost per channel stays accurate even though this call
    doesn't go through `GaladrielAgent.respond()`.

    Returns {"snapshot", "messages_before", "tokens_before", "tokens_after"}.
    """
    if not messages:
        return {
            "snapshot": prior_snapshot,
            "messages_before": 0,
            "tokens_before": 0,
            "tokens_after": len(prior_snapshot) // 4,
        }

    provider = provider or model_registry.get_provider("compaction", api_key=api_key)
    transcript = _render_transcript(messages)

    user_parts = [COMPRESSION_MESSAGE]
    if learned_recall_ids:
        ids = ", ".join(f"recall({rid})" for rid in learned_recall_ids if rid)
        if ids:
            user_parts.append(
                "\n\nLEARNED RECALLS FROM THIS BUFFER (cite as pointers in LEARNED; "
                f"do not restate their full content):\n{ids}"
            )
    if prior_snapshot:
        user_parts.append(
            "\n\nThere is an EXISTING snapshot from a prior compaction of this same "
            "conversation. Fold its still-relevant content into the new snapshot — "
            "do not lose anything important from it:\n\n"
            f"{prior_snapshot}"
        )
    user_parts.append(f"\n\nCONVERSATION TO COMPRESS:\n\n{transcript}")

    model = model_registry.model_for("compaction")
    response = await provider.create_message(
        model=model,
        max_tokens=SNAPSHOT_MAX_TOKENS,
        messages=[{"role": "user", "content": "".join(user_parts)}],
    )
    _log_compaction_cost(response, provider, model, channel_id, run_id=run_id)
    text_parts = [
        b.text for b in (response.content or [])
        if hasattr(b, "text") and getattr(b, "text", None)
    ]
    snapshot = "\n".join(text_parts).strip()
    if not snapshot:
        raise ValueError("compaction model returned an empty snapshot")

    # Rough token estimate: 4 chars ≈ 1 token
    tokens_before = sum(len(str(m.get("content", ""))) // 4 for m in messages)
    tokens_after = len(snapshot) // 4
    log.info(
        f"Snapshot compaction: {len(messages)} msgs, "
        f"~{tokens_before} → ~{tokens_after} tokens"
    )
    return {
        "snapshot": snapshot,
        "messages_before": len(messages),
        "tokens_before": tokens_before,
        "tokens_after": tokens_after,
    }

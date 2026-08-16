"""Context compaction — snapshot old conversation into a compact memory block.

When a channel's measured input context crosses the threshold (auto) or the user
runs `/compact` (manual), the conversation is summarized into one structured
snapshot. Automatic compaction summarizes only the *head*: `partition` splits at
the last real user turn so the instruction being worked on, and everything
gathered for it, survives verbatim. Manual `/compact` summarizes everything.

The snapshot is produced by the channel's own model summarizing in place (it can
see its own reasoning and reuses the prompt cache), falling back to the cheap
compaction model reading a rendered transcript (default: gemini-2.5-flash;
Anthropic fallback: claude-haiku-4-5).

This module only produces the snapshot text and decides where the cut goes. The
agent stores the snapshot as its own system block ahead of the surviving tail
(see agent.compact_channel + agent._assemble_system_blocks) and archives the
verbatim conversation to the MemPalace first, so nothing is lost.
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

# Budget for the in-conversation pass. Reasoning models bill thoughts against
# max_output_tokens and Gemini 3 Pro cannot switch thinking off, so the snapshot
# needs headroom above SNAPSHOT_MAX_TOKENS or it gets truncated before it starts.
IN_CONVERSATION_MAX_TOKENS = 16000

_SNAPSHOT_STRUCTURE = (
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
)

_SNAPSHOT_RULES = (
    "Be ruthlessly concise. Every word must earn its place. "
    "Preserve exact IDs, names, and numbers — those cannot be reconstructed from prose. "
    "Facts already stored via learn_recall must appear only as recall(<id>) pointers."
)

COMPRESSION_MESSAGE = (
    "Context window limit reached. Produce a compressed memory snapshot so this task "
    "can continue in a fresh context without losing progress.\n\n"
    + _SNAPSHOT_STRUCTURE
    + _SNAPSHOT_RULES
)

# Same job, asked of the model that is living the conversation rather than of a
# separate summarizer reading a transcript. Sent as the final user message after
# the messages being compacted, so "everything above" is literal.
IN_CONVERSATION_MESSAGE = (
    "[SYSTEM] Context window limit reached. The conversation above is about to be "
    "replaced by your summary of it — anything you leave out is gone. Later "
    "messages may be kept verbatim after your snapshot, so summarize only what "
    "is above. Do not call tools; output the snapshot and nothing else.\n\n"
    + _SNAPSHOT_STRUCTURE
    + _SNAPSHOT_RULES
)

# User-role messages the harness injects for protocol reasons. They are real
# turns as far as the API is concerned, but none of them is a human instruction,
# so none of them may define the compaction boundary (see partition).
SYNTHETIC_USER_KINDS = frozenset({"recall_fire", "truncation_notice"})


def _coerce_text(value) -> str:
    if isinstance(value, str):
        return value
    return str(value)


def estimate_tokens(msg: dict) -> int:
    """Rough size of one message. 4 chars ≈ 1 token; good enough for choosing
    where to cut, and it deliberately counts base64 image payloads as large."""
    return len(str(msg.get("content", ""))) // 4


def _has_tool_result(msg: dict) -> bool:
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    )


def is_real_user_turn(msg) -> bool:
    """True only for messages a human actually sent.

    Tool results and injected notices also ride in as role="user" (the API has
    no other slot for them), so role alone cannot identify a human turn. This is
    also where Gemini starts counting for thought-signature validation — "a turn
    begins with the most recent user message that is not a functionResponse" —
    so cutting here keeps every signature it validates inside the kept tail.
    """
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return False
    if msg.get("kind") in SYNTHETIC_USER_KINDS:
        return False
    return not _has_tool_result(msg)


def _snap_to_safe_start(messages: list, index: int) -> int:
    """Move `index` forward to a message that can legally open a conversation.

    A tool_result whose tool_use got summarized away is rejected by the API, so
    the tail may only start on an assistant message or a plain user message.
    """
    for i in range(index, len(messages)):
        if messages[i].get("role") == "assistant" or not _has_tool_result(messages[i]):
            return i
    return len(messages)


def partition(
    messages: list, *, full: bool = False, tail_max_tokens: int = 0,
) -> tuple[list, list]:
    """Split a conversation into (head to summarize, tail to keep verbatim).

    The tail begins at the last real user turn, so the instruction being worked
    on right now — and every tool result gathered for it — survives compaction
    untouched. Only what came before is summarized.

    `full=True` summarizes everything and keeps no tail. That is for manual
    `/compact`, where the previous turn is finished and the user is explicitly
    asking for a clean slate.

    `tail_max_tokens` caps the tail. Without it, a single instruction that
    generated hundreds of tool results would put the entire bloat in the tail
    and compaction would free nothing; when the cap is exceeded the cut moves
    forward into the cascade instead, keeping the most recent work.

    Returns ([], messages) when there is nothing worth summarizing.
    """
    if full:
        return list(messages), []
    if not messages:
        return [], []

    last_user = next(
        (i for i in range(len(messages) - 1, -1, -1) if is_real_user_turn(messages[i])),
        None,
    )
    if last_user is None:
        # Worker ticks and other machine-seeded buffers have no human turn.
        return list(messages), []

    boundary = last_user
    if tail_max_tokens > 0:
        tail_tokens = 0
        for i in range(len(messages) - 1, last_user - 1, -1):
            tail_tokens += estimate_tokens(messages[i])
            if tail_tokens <= tail_max_tokens:
                continue
            # Prefer an oversized tail over an empty one: if nothing after the
            # cut can legally start a conversation, keep the user-turn boundary.
            capped = _snap_to_safe_start(messages, i + 1)
            if capped < len(messages):
                boundary = max(boundary, capped)
            break

    return list(messages[:boundary]), list(messages[boundary:])


def _render_transcript(messages: list) -> str:
    """Flatten the API-format message list into a plain-text transcript for the
    compaction model. Images are omitted; tool payloads are truncated so a huge
    history doesn't blow up the compaction prompt (the verbatim copy lives in
    the palace archive)."""
    lines: list[str] = []
    for msg in messages:
        role = str(msg.get("role", "?")).upper()
        content = msg.get("content")
        # Reasoning is where plans and drafts live. Omitting it made the
        # summarizer describe conclusions it could not see the basis for.
        thought = (msg.get("_thought") or "").strip()
        if thought:
            lines.append(f"{role} [reasoning]: {thought[:4000]}")
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


def _extract_text(response) -> str:
    return "\n".join(
        b.text for b in (response.content or [])
        if hasattr(b, "text") and getattr(b, "text", None)
    ).strip()


def _instruction_suffix(
    prior_snapshot: str, learned_recall_ids: list[str] | None,
) -> list[str]:
    """The optional context appended to either compaction instruction."""
    parts: list[str] = []
    if learned_recall_ids:
        ids = ", ".join(f"recall({rid})" for rid in learned_recall_ids if rid)
        if ids:
            parts.append(
                "\n\nLEARNED RECALLS FROM THIS BUFFER (cite as pointers in LEARNED; "
                f"do not restate their full content):\n{ids}"
            )
    if prior_snapshot:
        parts.append(
            "\n\nThere is an EXISTING snapshot from a prior compaction of this same "
            "conversation. Fold its still-relevant content into the new snapshot — "
            "do not lose anything important from it:\n\n"
            f"{prior_snapshot}"
        )
    return parts


async def _summarize_in_conversation(
    messages: list,
    *,
    provider: BaseModelProvider,
    model: str,
    system=None,
    tools=None,
    prior_snapshot: str = "",
    learned_recall_ids: list[str] | None = None,
    channel_id: str = "compaction",
    run_id: str | None = None,
) -> str:
    """Ask the model that lived the conversation to summarize it in place.

    The messages go over as messages, not as a rendered transcript, which keeps
    reasoning blocks and their thought signatures intact and lets the request
    reuse the prompt cache the turn already paid for. Raises on failure; the
    caller falls back to the transcript summarizer.
    """
    request = list(messages)
    request.append({
        "role": "user",
        "content": IN_CONVERSATION_MESSAGE + "".join(
            _instruction_suffix(prior_snapshot, learned_recall_ids)
        ),
    })
    # System blocks and tools are passed through unchanged so this request
    # shares its prefix with the turn that just ran and reads from the cache.
    response = await provider.create_message(
        model=model,
        max_tokens=IN_CONVERSATION_MAX_TOKENS,
        system=system,
        tools=tools,
        messages=request,
    )
    _log_compaction_cost(response, provider, model, channel_id, run_id=run_id)
    snapshot = _extract_text(response)
    if not snapshot:
        raise ValueError(f"{model} returned no snapshot text (in-conversation)")
    return snapshot


async def compact_to_snapshot(
    messages: list,
    prior_snapshot: str = "",
    api_key: str = None,
    provider: BaseModelProvider = None,
    channel_id: str = "compaction",
    run_id: str | None = None,
    learned_recall_ids: list[str] | None = None,
    live_provider: BaseModelProvider = None,
    live_model: str = None,
    live_system=None,
    live_tools=None,
) -> dict:
    """Compress an entire conversation into one structured memory snapshot.

    When `live_provider` and `live_model` are given, the channel's own model
    summarizes the conversation in place (it can see its own reasoning, and the
    prompt cache still applies). Any failure there falls back to the cheap
    compaction model reading a rendered transcript.

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

    snapshot = ""
    if live_provider is not None and live_model:
        try:
            snapshot = await _summarize_in_conversation(
                messages,
                provider=live_provider,
                model=live_model,
                system=live_system,
                tools=live_tools,
                prior_snapshot=prior_snapshot,
                learned_recall_ids=learned_recall_ids,
                channel_id=channel_id,
                run_id=run_id,
            )
        except Exception as e:
            log.warning(
                f"In-conversation compaction failed ({e}); "
                f"falling back to the transcript summarizer"
            )

    if not snapshot:
        provider = provider or model_registry.get_provider("compaction", api_key=api_key)
        user_parts = [COMPRESSION_MESSAGE]
        user_parts.extend(_instruction_suffix(prior_snapshot, learned_recall_ids))
        user_parts.append(
            f"\n\nCONVERSATION TO COMPRESS:\n\n{_render_transcript(messages)}"
        )
        model = model_registry.model_for("compaction")
        response = await provider.create_message(
            model=model,
            max_tokens=SNAPSHOT_MAX_TOKENS,
            messages=[{"role": "user", "content": "".join(user_parts)}],
        )
        _log_compaction_cost(response, provider, model, channel_id, run_id=run_id)
        snapshot = _extract_text(response)
        if not snapshot:
            raise ValueError("compaction model returned an empty snapshot")

    tokens_before = sum(estimate_tokens(m) for m in messages)
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

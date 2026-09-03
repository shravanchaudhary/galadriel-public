"""Context compaction — snapshot old conversation into a compact memory block.

When a channel's context crosses the effective threshold (auto) or the user
runs `/compact` (manual), the conversation is summarized into one structured
snapshot. Automatic compaction summarizes only the *head*: `partition` splits at
the last real user turn so the instruction being worked on, and everything
gathered for it, survives verbatim. Manual `/compact` summarizes everything.

The snapshot is produced by the channel's own model summarizing in place — it
can see its own reasoning and reuses the prompt cache the turn already paid
for. There is deliberately NO fallback summarizer model (removed 2026-09-04,
shravan's call): a fallback only works when its context window is at least the
live model's, which no fixed cheap model can guarantee across the catalog. A
failed summarize raises instead, so the real error surfaces in the UI and the
failure mode gets seen and fixed rather than silently papered over. The
headroom the live summarizer needs is what caps the auto-trigger at 85% of the
window (agent._respond_locked_inner): past that, the head plus
IN_CONVERSATION_MAX_TOKENS of output would no longer fit.

This module only produces the snapshot text and decides where the cut goes. The
agent stores the snapshot as its own system block ahead of the surviving tail
(see agent.compact_channel + agent._assemble_system_blocks) and archives the
verbatim conversation to the palace first, so nothing is lost.
"""

import logging

from . import cost_tracker
from .providers import BaseModelProvider

log = logging.getLogger("galadriel.compaction")

# The unit authority: everything context-sized in this codebase deals in
# TOKENS; one token ≈ this many characters. Convert to chars only at string
# slicing/IO boundaries, never in limits, budgets, or messages.
CHARS_PER_TOKEN = 4

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

# Asked of the model that is living the conversation. Sent as the final user
# message after the messages being compacted, so "everything above" is literal.
IN_CONVERSATION_MESSAGE = (
    "[SYSTEM] Context window limit reached. The conversation above is about to be "
    "replaced by your summary of it — anything you leave out is gone. Later "
    "messages may be kept verbatim after your snapshot, so summarize only what "
    "is above. Do not call tools; output the snapshot and nothing else.\n\n"
    + _SNAPSHOT_STRUCTURE
    + _SNAPSHOT_RULES
)

# Harness-injected user-role messages (the tool_result half of a recall-fire
# exchange, truncation notices). Real turns as far as the API is concerned, but
# none of them is a human instruction, so none may define the compaction
# boundary (see partition).
SYNTHETIC_USER_KINDS = frozenset({"recall_fire", "truncation_notice"})


def estimate_tokens(msg: dict) -> int:
    """Rough size of one message in tokens; good enough for choosing where to
    cut, and it deliberately counts base64 image payloads as large.
    `_thought` counts too: the Mantle provider folds it into content on the
    wire, so ignoring it undersizes thought-heavy cascades (other providers
    get a mild overestimate, which only cuts earlier — the safe direction)."""
    return (
        len(str(msg.get("content", ""))) + len(msg.get("_thought") or "")
    ) // CHARS_PER_TOKEN


# Roughly what providers bill per image. `estimate_tokens` above deliberately
# counts base64 by length — over-counting only moves the *cut* earlier — but
# anything that decides whether to SEND or how much output fits cannot: one 5MB
# screenshot would estimate as ~1.7M tokens and fire compaction on every call
# (or floor max_tokens) for as long as it rode in the buffer.
IMAGE_EST_TOKENS = 1_600
_REASONING_BLOCKS = frozenset({"thinking", "redacted_thinking"})


def estimate_message_tokens(msg: dict, *, with_reasoning: bool) -> int:
    """Request-side token estimate for one message: text via CHARS_PER_TOKEN,
    images at nominal cost, reasoning counted at most ONCE and only when asked.

    Native Anthropic stores a turn's thinking twice — inline `thinking` blocks
    (kept for signature continuity) plus the `_thought` mirror — and bills only
    the current turn's thinking on input. Counting both, for every turn, made a
    thinking-heavy cascade look 2x its billed size and compacted prematurely.
    Callers pass `with_reasoning=True` for the newest assistant message only.
    """
    total = 0
    has_inline_thinking = False
    content = msg.get("content")
    if not isinstance(content, list):
        total += len(str(content or "")) // CHARS_PER_TOKEN
    else:
        for block in content:
            if not isinstance(block, dict):
                total += len(str(block)) // CHARS_PER_TOKEN
                continue
            btype = block.get("type")
            if btype == "image":
                total += IMAGE_EST_TOKENS
            elif btype in _REASONING_BLOCKS:
                has_inline_thinking = True
                if with_reasoning:
                    total += len(str(block)) // CHARS_PER_TOKEN
            elif btype == "tool_result" and isinstance(block.get("content"), list):
                for ib in block["content"]:
                    if isinstance(ib, dict) and ib.get("type") == "image":
                        total += IMAGE_EST_TOKENS
                    else:
                        total += len(str(ib)) // CHARS_PER_TOKEN
            else:
                total += len(str(block)) // CHARS_PER_TOKEN
    if with_reasoning and not has_inline_thinking:
        total += len(msg.get("_thought") or "") // CHARS_PER_TOKEN
    return total


def estimate_request_tokens(messages: list, system_blocks, tools) -> int:
    """Cheap pre-send size of a whole request (messages + system + tools).
    Deliberately rough — it exists to catch a request that would overfly the
    window, or to size the output budget, not to bill it."""
    last = len(messages) - 1
    total = sum(
        estimate_message_tokens(m, with_reasoning=(i == last))
        for i, m in enumerate(messages)
    )
    total += len(str(system_blocks or "")) // CHARS_PER_TOKEN
    total += len(str(tools or "")) // CHARS_PER_TOKEN
    return total


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
    reuse the prompt cache the turn already paid for. Raises on failure — there
    is no fallback; the error is meant to surface.
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
    # A snapshot cut off mid-way is a silent loss of history — the provider
    # may have fitted max_tokens below IN_CONVERSATION_MAX_TOKENS to make the
    # request fit. Failures surface; they are not absorbed.
    if getattr(response, "stop_reason", None) == "max_tokens":
        raise ValueError(
            f"{model} hit its output limit mid-snapshot; refusing a truncated one"
        )
    return snapshot


async def compact_to_snapshot(
    messages: list,
    prior_snapshot: str = "",
    channel_id: str = "compaction",
    run_id: str | None = None,
    learned_recall_ids: list[str] | None = None,
    live_provider: BaseModelProvider = None,
    live_model: str = None,
    live_system=None,
    live_tools=None,
) -> dict:
    """Compress the given messages into one structured memory snapshot.

    The channel's own model summarizes the conversation in place — it can see
    its own reasoning, and the prompt cache still applies. There is no
    fallback summarizer; a failure here raises so the caller surfaces the real
    error (see the module docstring for why).

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
            "tokens_after": len(prior_snapshot) // CHARS_PER_TOKEN,
        }
    if live_provider is None or not live_model:
        raise ValueError(
            "compact_to_snapshot requires the channel's live summarizer "
            "(live_provider + live_model); the fallback model was removed"
        )

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

    tokens_before = sum(estimate_tokens(m) for m in messages)
    tokens_after = len(snapshot) // CHARS_PER_TOKEN
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

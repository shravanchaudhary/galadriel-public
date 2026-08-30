"""Bedrock Mantle implementation of `BaseModelProvider`.

Mantle is Bedrock's OpenAI-compatible inference surface. It serves the open
models (GLM, Kimi, MiniMax, DeepSeek, Qwen, Devstral, Mistral, Nemotron, Gemma)
that have no native Anthropic-style API, so this provider adapts OpenAI Chat
Completions to the *Anthropic-shaped* response contract the rest of the harness
depends on — same job `gemini_provider.py` does for Gemini, and the same
returned surface:

  - `.content`     — blocks with `.type` ("text"|"tool_use"), `.text`,
                     `.id`/`.name`/`.input`, and `.model_dump(exclude_none=True)`.
  - `.usage`       — `.input_tokens`, `.output_tokens`,
                     `.cache_read_input_tokens`, `.cache_creation_input_tokens`.
  - `.stop_reason` — "end_turn" | "tool_use" | "max_tokens".

Endpoint. `https://bedrock-mantle.{region}.api.aws/v1`. Note `/v1`, NOT the
`/openai/v1` the AWS docs show — that path 404s. Model ids also
differ from the ones `bedrock list-foundation-models` reports (Mantle wants
`moonshotai.kimi-k2-thinking`, the control plane says `moonshot.`), so ids come
from `model_catalog.wire_id` which was populated from `GET /v1/models`.

Caching. Mantle caches automatically off the request prefix — there is no
`cache_control` breakpoint to set, exactly like Gemini implicit caching. So we
apply the same trick: stable system blocks become the leading `system` message
(a byte-identical prefix), and the dynamic block (timestamp, daily logs,
advisories) moves to the TAIL of the message list. Hits arrive as
`usage.prompt_tokens_details.cached_tokens` and map to
`cache_read_input_tokens`; there is no separate write charge, so
`cache_creation_input_tokens` is always 0.

Reasoning. Thinking models return their chain of thought on a non-standard
`reasoning` (sometimes `reasoning_content`) field rather than in `content`, so
it is read out of the SDK's `model_extra`, surfaced as ("thought", delta), and
stored on the history message as `_thought` — outside `content`, which is the
visible reply. Replay is the hard part: Mantle silently DROPS an assistant
`reasoning`/`reasoning_content`/`thinking` field on input (measured 2026-08-30:
an ~850-token blob moved prompt_tokens by 0 on every model probed — both
gpt-oss sizes, glm-5, kimi-k2-thinking, deepseek-v3.2, minimax-m2.5,
nemotron-super-3-120b — and the structured block shapes 400). So
`_messages_to_openai` folds `_thought` into the assistant `content` under a
`[prior reasoning]` marker, on tool-call turns only — Harmony's contract: CoT
is required input while a tool chain is open, dropped once a turn ends in a
final message. Without the fold the model re-derives its plan from your
message on every round of a cascade (glm-5 also hallucinated a tool call in
testing). The fold rule reads only the message itself, so a message's
serialization never changes between requests: history stays append-only and
byte-identical prefixes keep provider-side prompt caching hitting — which is
also why completed turns keep their fold instead of dropping it later. Probe
matrix: project-agent-kb/kb/mantle-reasoning-replay.md.
"""

import json
import os
import uuid

from openai import AsyncOpenAI

from .base import BaseModelProvider
from .llm_retry import stream_with_llm_retry, with_llm_retry


# us-east-1 serves a strict superset of every other region's model list (55 vs
# 38 in ap-south-1), and some models only work here: Nemotron 3 Super 120B hangs
# indefinitely in ap-south-1 but answers in ~1s from us-east-1.
DEFAULT_REGION = "us-east-1"

# Mantle takes OpenAI's coarse reasoning_effort, not a token budget, and
# accepts exactly "none", "low", "medium" and "high" — the wider OpenAI ladder
# ("minimal", "xhigh", "max") 400s here. Tower's effort vocabulary is finer, so
# it collapses onto those four. Never omit the field: leaving it unset lets a
# thinking model reason at full length, the opposite of what the caller asked.
_EFFORT_MAP = {
    "off": "none",
    "minimal": "none",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "dynamic": "high",
}

# Sent when a caller passes thinking=False (judges, gates, titles).
_THINKING_OFF = "none"

# Harmony-format models reject "none" outright, so the most minimal reasoning
# they can be held to is "low".
_THINKING_FLOOR = "low"

# Prior CoT is folded into assistant `content` behind this marker on tool-call
# turns (Mantle drops the out-of-band reasoning fields — module docstring).
# Part of the cached prefix: keep it short and never change it casually.
_PRIOR_REASONING_MARKER = "[prior reasoning]"

# Folded thoughts are model-visible input on every later request of the
# session (kept for prefix stability), so an unbounded one compounds. The cap
# is deterministic — applied from the stored `_thought` identically on every
# request — so serialization stays byte-stable across the cascade.
_FOLDED_THOUGHT_MAX_CHARS = 8_000


# ─── Anthropic-shaped response objects ───────────────────────────────


class _TextBlock:
    type = "text"

    def __init__(self, text: str):
        self.text = text

    def model_dump(self, exclude_none: bool = True) -> dict:
        return {"type": "text", "text": self.text}


class _ToolUseBlock:
    type = "tool_use"

    def __init__(self, id: str, name: str, input: dict):
        self.id = id
        self.name = name
        self.input = input

    def model_dump(self, exclude_none: bool = True) -> dict:
        return {
            "type": "tool_use",
            "id": self.id,
            "name": self.name,
            "input": self.input,
        }


class _Usage:
    def __init__(
        self,
        input_tokens: int,
        output_tokens: int,
        cache_read_input_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
    ):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read_input_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens


class _Message:
    def __init__(self, content: list, usage: _Usage, stop_reason: str, thought: str = ""):
        self.content = content
        self.usage = usage
        self.stop_reason = stop_reason
        # Reasoning rides alongside `content`, never inside it: `content` is
        # what gets serialized into conversation history, and thoughts must not
        # be replayed back to the model as prior assistant text.
        self.thought = thought


# ─── Anthropic → OpenAI input translation ────────────────────────────


# Mirrors the Gemini provider: frames the dynamic system context once it has
# been relocated out of the leading system message, so the model reads it as
# ambient context rather than as the user's literal message.
_DYNAMIC_CONTEXT_HEADER = (
    "[Ambient session context — current time, active project, and recent "
    "memory. Not part of the user's message.]"
)


def _flatten_blocks(blocks) -> str | None:
    parts = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif isinstance(block, str):
            parts.append(block)
    return "\n\n".join(p for p in parts if p) or None


def _split_system(system) -> tuple[str | None, str | None]:
    """Split Anthropic system blocks into (stable, dynamic) text.

    Blocks carrying `cache_control` are the cacheable prefix and stay in the
    leading system message; blocks without it change every call and would bust
    the automatic prefix cache, so they are returned separately for the caller
    to append at the tail.

    That split only makes sense when a cacheable prefix exists to protect. The
    agent always marks its stable block, but single-purpose callers (judges,
    gates, titles) pass one unmarked block — treating that as "dynamic" would
    demote their entire instruction set to a trailing user turn labelled as
    ambient context, and leave the request with no system message at all. With
    nothing marked, everything is stable.
    """
    if system is None:
        return None, None
    if isinstance(system, str):
        return (system or None), None
    if not any(isinstance(b, dict) and b.get("cache_control") for b in system):
        return _flatten_blocks(system), None
    stable = [b for b in system if isinstance(b, dict) and b.get("cache_control")]
    dynamic = [b for b in system if not (isinstance(b, dict) and b.get("cache_control"))]
    return _flatten_blocks(stable), _flatten_blocks(dynamic)


def _tools_to_openai(tools):
    """Anthropic tool defs → OpenAI function defs. `cache_control` is ignored."""
    if not tools:
        return None
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema") or {"type": "object"},
            },
        }
        for tool in tools
    ]


def _tool_result_text(content) -> str:
    """tool_result content is a string or a list of Anthropic blocks; coerce to
    text. OpenAI tool messages are text-only, so an image block is noted and
    its pixels are re-attached as a following user message."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "image":
                    chunks.append("[image attached]")
                else:
                    chunks.append(str(block.get("text", block.get("content", ""))))
            else:
                chunks.append(str(block))
        return "\n".join(chunks)
    return str(content)


def _image_parts(content) -> list:
    """Image blocks from a tool_result as OpenAI image_url parts.

    An OpenAI `tool` message may not carry images, so a browser screenshot has
    to ride along on a separate user message or the model never sees it.
    """
    if not isinstance(content, list):
        return []
    parts = []
    for block in content:
        if not (isinstance(block, dict) and block.get("type") == "image"):
            continue
        src = block.get("source", {})
        if src.get("type") == "base64" and src.get("data"):
            media = src.get("media_type", "image/png")
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{media};base64,{src['data']}"},
                }
            )
    return parts


def _is_tool_result_message(msg: dict) -> bool:
    content = msg.get("content")
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    )


def _ends_with_tool_result(messages: list) -> bool:
    """True when the tail is mid tool-cascade, deciding whether the dynamic
    context rides as the trailing user message.

    Synthetic recall-fire messages are skipped: a turn-START fire pair ends
    with a tool_result, but the turn is still at its start — suppressing the
    tail there costs the whole turn its timestamp/daily-log/advisories (the
    old user-message fire transport kept them). A genuinely mid-cascade tail
    (real tool results, or an assistant tool call whose recall-only round got
    kind-marked) still suppresses.
    """
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("kind") == "recall_fire":
            continue
        return _is_tool_result_message(msg) or msg.get("role") == "assistant"
    return False


def _messages_to_openai(messages: list, trailing_text: str | None = None) -> list:
    """Translate Anthropic-format messages into OpenAI chat messages.

    One Anthropic message can hold several blocks that OpenAI splits across
    messages: an assistant turn with text + two tool_use blocks is one message
    with a `tool_calls` array, but the matching tool_results are one `tool`
    message each. Images inside a tool_result move to a trailing user message
    because OpenAI tool messages are text-only.

    A tool-call turn's stored `_thought` is folded into its `content` behind
    `_PRIOR_REASONING_MARKER` — Mantle strips every out-of-band reasoning
    field on input, so visible content is the only carrier that reaches the
    model (module docstring has the probe numbers). Final turns send no
    thought, per Harmony. The fold depends only on the message itself, so a
    message's serialization never changes between requests: the byte-identical
    prefix holds and every cached token stays cached.

    The input `messages` are never mutated.
    """
    out: list[dict] = []

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")

        if isinstance(content, str):
            if content:
                out.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            continue

        text_parts: list[dict] = []
        tool_calls: list[dict] = []
        pending_images: list[dict] = []

        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")

            if btype == "text":
                if block.get("text"):
                    text_parts.append({"type": "text", "text": block["text"]})

            elif btype == "image":
                src = block.get("source", {})
                if src.get("type") == "base64" and src.get("data"):
                    media = src.get("media_type", "image/png")
                    text_parts.append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{media};base64,{src['data']}"
                            },
                        }
                    )

            elif btype == "tool_use":
                tool_calls.append(
                    {
                        "id": block.get("id"),
                        "type": "function",
                        "function": {
                            "name": block.get("name"),
                            "arguments": json.dumps(block.get("input") or {}),
                        },
                    }
                )

            elif btype == "tool_result":
                # Flush any assistant text collected before this point first.
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": block.get("tool_use_id"),
                        "content": _tool_result_text(block.get("content")),
                    }
                )
                pending_images.extend(_image_parts(block.get("content")))

        if role == "assistant" and (text_parts or tool_calls):
            text = "".join(p["text"] for p in text_parts if p["type"] == "text")
            thought = (msg.get("_thought") or "").strip()
            if tool_calls and thought:
                thought = thought[:_FOLDED_THOUGHT_MAX_CHARS]
                text = f"{_PRIOR_REASONING_MARKER}\n{thought}" + (
                    f"\n\n{text}" if text else ""
                )
            # A tool-only assistant turn with no thought sends content: null,
            # the shape OpenAI specifies. An empty string here made some
            # thinking models treat the turn as unfinished and resume it,
            # leaking raw <think> prose into the next reply.
            entry: dict = {
                "role": "assistant",
                "content": text or (None if tool_calls else ""),
            }
            if tool_calls:
                entry["tool_calls"] = tool_calls
            out.append(entry)
        elif role == "user" and text_parts:
            out.append({"role": "user", "content": text_parts})

        if pending_images:
            out.append({"role": "user", "content": pending_images})

    if trailing_text and not _ends_with_tool_result(messages):
        out.append(
            {
                "role": "user",
                "content": (
                    f"{_DYNAMIC_CONTEXT_HEADER}\n\n{trailing_text}\n\n"
                    "[End of ambient context. Reply to the conversation above.]"
                ),
            }
        )

    return out


# ─── OpenAI → Anthropic output translation ───────────────────────────


def _new_tool_id() -> str:
    return f"call_{uuid.uuid4().hex[:24]}"


def _parse_arguments(raw) -> dict:
    """Tool arguments arrive as a JSON *string*. A model can emit a truncated or
    malformed one, and a raised ValueError here would kill the whole turn, so
    an unparseable payload degrades to a diagnostic the agent can see."""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {"_malformed_arguments": str(raw)}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _extra(obj) -> dict:
    return getattr(obj, "model_extra", None) or {}


def _reasoning_text(obj) -> str:
    """Chain of thought, which Mantle returns outside the standard schema."""
    extra = _extra(obj)
    for key in ("reasoning", "reasoning_content"):
        value = extra.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _split_inline_thinking(text: str) -> tuple[str, str]:
    """Separate a `<think>…</think>` span from the visible reply.

    Thinking models normally return reasoning on the `reasoning` field, but they
    fall back to inline tags when a turn looks like a continuation, and that
    prose would otherwise be shown to the user as the assistant's answer. An
    unclosed tag means the reply was cut mid-thought, so everything after it is
    thinking too.
    """
    if "<think>" not in text:
        return text, ""
    thoughts: list[str] = []
    visible: list[str] = []
    rest = text
    while "<think>" in rest:
        before, _, after = rest.partition("<think>")
        visible.append(before)
        thought, closed, remainder = after.partition("</think>")
        thoughts.append(thought)
        if not closed:
            rest = ""
            break
        rest = remainder
    visible.append(rest)
    return "".join(visible).strip(), "".join(thoughts).strip()


class _InlineThinkingFilter:
    """Route streamed `<think>` spans to thoughts instead of visible text.

    Deltas arrive mid-token, so a tag can straddle two chunks. Anything that is
    still a viable prefix of a tag is held back rather than emitted, which is
    why this is stateful instead of a per-chunk regex.
    """

    OPEN = "<think>"
    CLOSE = "</think>"

    def __init__(self):
        self._buffer = ""
        self._in_thought = False

    @staticmethod
    def _partial_tag_len(text: str, tag: str) -> int:
        """Length of the trailing run of `text` that could still become `tag`."""
        for size in range(min(len(tag) - 1, len(text)), 0, -1):
            if text.endswith(tag[:size]):
                return size
        return 0

    def feed(self, delta: str) -> list[tuple[str, str]]:
        """Return ("text"|"thought", chunk) pairs safe to emit now."""
        self._buffer += delta
        out: list[tuple[str, str]] = []
        while self._buffer:
            if self._in_thought:
                head, closed, rest = self._buffer.partition(self.CLOSE)
                if not closed:
                    hold = self._partial_tag_len(self._buffer, self.CLOSE)
                    emit = self._buffer[: len(self._buffer) - hold]
                    self._buffer = self._buffer[len(self._buffer) - hold:]
                    if emit:
                        out.append(("thought", emit))
                    break
                if head:
                    out.append(("thought", head))
                self._in_thought = False
                self._buffer = rest
                continue

            head, opened, rest = self._buffer.partition(self.OPEN)
            if not opened:
                hold = self._partial_tag_len(self._buffer, self.OPEN)
                emit = self._buffer[: len(self._buffer) - hold]
                self._buffer = self._buffer[len(self._buffer) - hold:]
                if emit:
                    out.append(("text", emit))
                break
            if head:
                out.append(("text", head))
            self._in_thought = True
            self._buffer = rest
        return out

    def flush(self) -> list[tuple[str, str]]:
        """Emit whatever is still held once the stream ends."""
        if not self._buffer:
            return []
        kind = "thought" if self._in_thought else "text"
        out = [(kind, self._buffer)]
        self._buffer = ""
        return out


def _stop_reason(finish_reason: str | None, has_tool_call: bool) -> str:
    if has_tool_call:
        return "tool_use"
    if finish_reason == "length":
        return "max_tokens"
    return "end_turn"


def _map_usage(usage) -> _Usage:
    """Map OpenAI usage onto Anthropic usage fields.

    `prompt_tokens` includes cached tokens, so cached is subtracted to mirror
    Anthropic, where `input_tokens` excludes cache reads. Reasoning tokens are
    already inside `completion_tokens`.
    """
    if usage is None:
        return _Usage(0, 0)
    prompt = getattr(usage, "prompt_tokens", 0) or 0
    completion = getattr(usage, "completion_tokens", 0) or 0
    details = getattr(usage, "prompt_tokens_details", None)
    cached = (getattr(details, "cached_tokens", 0) or 0) if details else 0
    return _Usage(
        input_tokens=max(prompt - cached, 0),
        output_tokens=completion,
        cache_read_input_tokens=cached,
        cache_creation_input_tokens=0,
    )


# Most Mantle models treat `max_tokens` as a *reservation* against the
# context window rather than a plain output cap: the request is rejected unless
# `input + max_tokens <= context`. Measured in us-east-1, 15 of 17 models
# enforce this (both gpt-oss models are the exception and treat it as a pure
# cap). So a static per-model `max_output` is safe on a short prompt and a 400
# once the conversation grows past `context - max_output`.
#
# Rather than lower `max_output` — which would cap every short-prompt turn to
# protect against the rare long one — the ceiling is fitted to what actually
# fits. Omitting `max_tokens` is not the answer either: the models then apply
# their own default, which for gpt-oss is 8_192 (verified: `finish_reason
# == "length"` at exactly 8_192), well below what we want to allow.
#
# The input estimate is deliberately crude (bytes/3.5 over the serialized
# messages, generous for English) and paired with a wide margin, because
# guessing high only shortens a reply we were never going to use in full,
# while guessing low is a hard failure.
_TOKEN_BYTES = 3.5
_FIT_MARGIN = 4_096
# Never clamp below this: a request that cannot answer at all is worse than one
# that risks the 400 the clamp exists to avoid.
_FIT_FLOOR = 8_192


def _estimate_input_tokens(chat_messages: list[dict]) -> int:
    try:
        size = len(json.dumps(chat_messages, ensure_ascii=False).encode())
    except (TypeError, ValueError):
        size = sum(len(str(m)) for m in chat_messages)
    return int(size / _TOKEN_BYTES)


def _fit_max_tokens(max_tokens: int, chat_messages: list[dict], entry) -> int:
    """Shrink `max_tokens` to what the model's context can still hold."""
    context = getattr(entry, "context", None)
    if not context or not max_tokens:
        return max_tokens
    room = context - _estimate_input_tokens(chat_messages) - _FIT_MARGIN
    if room >= max_tokens:
        return max_tokens
    return max(_FIT_FLOOR, room)


def _blocks_from_message(message) -> tuple[list, bool, str]:
    """→ (content blocks, has_tool_call, thought).

    The thought is the out-of-band `reasoning` field plus any inline
    `<think>` span, and is returned separately so it never lands in `content`.
    """
    blocks = []
    thought = _reasoning_text(message)
    if getattr(message, "content", None):
        visible, inline_thought = _split_inline_thinking(message.content)
        if inline_thought:
            thought = f"{thought}\n{inline_thought}".strip() if thought else inline_thought
        if visible:
            blocks.append(_TextBlock(visible))
    tool_calls = getattr(message, "tool_calls", None) or []
    for call in tool_calls:
        blocks.append(
            _ToolUseBlock(
                id=getattr(call, "id", None) or _new_tool_id(),
                name=call.function.name,
                input=_parse_arguments(call.function.arguments),
            )
        )
    return blocks, bool(tool_calls), thought


# ─── Provider ─────────────────────────────────────────────────────────


class BedrockMantleProvider(BaseModelProvider):
    def __init__(self, api_key: str = None, region: str | None = None):
        key = api_key or os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
        self.region = region or os.environ.get("BEDROCK_REGION") or DEFAULT_REGION
        # max_retries=0: with_llm_retry / stream_with_llm_retry own backoff so
        # Retry-After and the attempt budget live in one place.
        self.client = AsyncOpenAI(
            base_url=f"https://bedrock-mantle.{self.region}.api.aws/v1",
            api_key=key,
            max_retries=0,
        )

    def _build_kwargs(
        self, model, max_tokens, messages, system, tools, *, thinking=True,
        temperature=None, effort=None, stream=False,
    ) -> dict:
        from .. import model_catalog

        entry = model_catalog.get(model)
        stable, dynamic = _split_system(system)

        chat_messages: list[dict] = []
        if stable:
            chat_messages.append({"role": "system", "content": stable})
        chat_messages.extend(_messages_to_openai(messages, trailing_text=dynamic))

        kwargs: dict = {
            "model": model_catalog.wire_id(model),
            "max_tokens": _fit_max_tokens(max_tokens, chat_messages, entry),
            "messages": chat_messages,
        }
        # Gemma ignores a tools array and answers in prose; sending one buys
        # nothing and only risks a 400 on models that validate it.
        if tools and (entry is None or entry.supports_tools):
            kwargs["tools"] = _tools_to_openai(tools)
        if temperature is not None:
            kwargs["temperature"] = temperature
        if entry is None or entry.supports_thinking:
            if thinking:
                wanted = _EFFORT_MAP.get(effort or "", "high")
            else:
                wanted = _THINKING_OFF
            # A model that cannot reach "none" is floored rather than 400'd.
            if wanted == "none" and entry is not None and not entry.can_disable_thinking:
                wanted = _THINKING_FLOOR
            kwargs["reasoning_effort"] = wanted
        if stream:
            kwargs["stream"] = True
            kwargs["stream_options"] = {"include_usage": True}
        return kwargs

    async def create_message(
        self, *, model, max_tokens, messages, system=None, tools=None, thinking=True,
        temperature=None, effort=None, attempts=None,
    ):
        kwargs = self._build_kwargs(
            model, max_tokens, messages, system, tools,
            thinking=thinking, temperature=temperature, effort=effort,
        )

        async def _once():
            response = await self.client.chat.completions.create(**kwargs)
            choice = response.choices[0]
            blocks, has_tool_call, thought = _blocks_from_message(choice.message)
            return _Message(
                blocks,
                _map_usage(getattr(response, "usage", None)),
                _stop_reason(choice.finish_reason, has_tool_call),
                thought=thought,
            )

        return await with_llm_retry(
            _once, provider="bedrock_mantle", model=model,
            **({} if attempts is None else {"attempts": attempts}),
        )

    async def stream_message(
        self, *, model, max_tokens, messages, system=None, tools=None, thinking=True,
        effort=None, temperature=None,
    ):
        """True chunk streaming. Yields ("thought"|"text", delta) as Mantle emits
        them, then ("message", _Message) assembled from the whole stream so the
        agent loop sees the same shape as `create_message`.
        """
        kwargs = self._build_kwargs(
            model, max_tokens, messages, system, tools,
            thinking=thinking, effort=effort, temperature=temperature,
            stream=True,
        )

        async def _stream_once():
            stream = await self.client.chat.completions.create(**kwargs)

            text_pieces: list[str] = []
            # Tool call arguments arrive as fragments keyed by index, and the
            # name may only appear on the first fragment.
            partial: dict[int, dict] = {}
            finish_reason = None
            usage = None
            thinking_filter = _InlineThinkingFilter()
            # Also accumulated onto the final _Message so a caller that only
            # reads the assembled response sees the same thought a delta
            # consumer saw.
            thought_pieces: list[str] = []

            async for chunk in stream:
                if getattr(chunk, "usage", None) is not None:
                    usage = chunk.usage
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
                delta = choice.delta
                if delta is None:
                    continue

                thought = _reasoning_text(delta)
                if thought:
                    thought_pieces.append(thought)
                    yield ("thought", thought)

                if delta.content:
                    for kind, piece in thinking_filter.feed(delta.content):
                        if kind == "text":
                            text_pieces.append(piece)
                        else:
                            thought_pieces.append(piece)
                        yield (kind, piece)

                for fragment in delta.tool_calls or []:
                    slot = partial.setdefault(
                        fragment.index, {"id": None, "name": None, "args": ""}
                    )
                    if fragment.id:
                        slot["id"] = fragment.id
                    function = getattr(fragment, "function", None)
                    if function is not None:
                        if function.name:
                            slot["name"] = function.name
                        if function.arguments:
                            slot["args"] += function.arguments

            for kind, piece in thinking_filter.flush():
                if kind == "text":
                    text_pieces.append(piece)
                else:
                    thought_pieces.append(piece)
                yield (kind, piece)

            blocks: list = []
            if text_pieces:
                blocks.append(_TextBlock("".join(text_pieces)))
            for _, slot in sorted(partial.items()):
                if not slot["name"]:
                    continue
                blocks.append(
                    _ToolUseBlock(
                        id=slot["id"] or _new_tool_id(),
                        name=slot["name"],
                        input=_parse_arguments(slot["args"]),
                    )
                )

            has_tool_call = any(b.type == "tool_use" for b in blocks)
            yield (
                "message",
                _Message(
                    blocks, _map_usage(usage),
                    _stop_reason(finish_reason, has_tool_call),
                    thought="".join(thought_pieces),
                ),
            )

        async for item in stream_with_llm_retry(
            _stream_once, provider="bedrock_mantle", model=model
        ):
            yield item

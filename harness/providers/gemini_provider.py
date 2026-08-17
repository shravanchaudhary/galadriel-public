"""Gemini implementation of `BaseModelProvider`.

Wraps the `google-genai` async client and adapts it to the *Anthropic-shaped*
response contract the rest of the harness depends on, so no other code needs
to know which backend is in use. Specifically it returns a response object
exposing:

  - `.content`     — list of blocks; each block has `.type` ("text"|"tool_use"),
                     `.text` (text blocks), `.id`/`.name`/`.input` (tool_use
                     blocks), and `.model_dump(exclude_none=True)`.
  - `.usage`       — `.input_tokens`, `.output_tokens`,
                     `.cache_read_input_tokens`, `.cache_creation_input_tokens`.
  - `.stop_reason` — "end_turn" | "tool_use" | "max_tokens".

Inputs (messages, system, tools) arrive in Anthropic format and are translated
to Gemini's `contents` / `system_instruction` / function declarations.

Caching. Gemini uses *implicit* caching (automatic for 2.5+ models, 90%
discount on cached tokens) rather than Anthropic's explicit `cache_control`
breakpoints — there is no marker to set. To make implicit caching actually
engage, the request prefix must stay byte-identical across calls. We therefore
split the system blocks by their `cache_control` marker: stable blocks become
`system_instruction` (a fixed prefix), while the dynamic block (timestamp,
daily logs, advisories) is moved to the TAIL of `contents`. This keeps the
growing conversation history a stable prefix, so cache hits accumulate as the
conversation grows. Hits surface via `cached_content_token_count`, mapped to
`cache_read_input_tokens` (Gemini has no separate cache-write charge, so
`cache_creation_input_tokens` is always 0). Minimum prefix for caching to engage:
Gemini 3 / 3.1 family (gemini-3.1-pro-preview, gemini-3.5-flash): 4,096 tokens;
Gemini 2.5 family (gemini-2.5-flash, gemini-2.5-pro): 2,048 tokens — per
https://ai.google.dev/gemini-api/docs/interactions/caching
"""

import os
import uuid
import base64

from google import genai
from google.genai import types

from .base import BaseModelProvider
from .llm_retry import stream_with_llm_retry, with_llm_retry
from ..thinking_effort import thinking_kwargs as _thinking_kwargs


# ─── Anthropic-shaped response objects ───────────────────────────────


class _TextBlock:
    type = "text"

    def __init__(self, text: str, thought_signature: str | None = None):
        self.text = text
        # For Gemini 3 text-only (no-functionCall) responses the model attaches
        # its thought_signature to the LAST part. Returning it on the next turn
        # is not validated (no 400) but is *recommended* — omitting it degrades
        # multi-step reasoning and is a known cause of the model re-deriving its
        # plan from scratch. base64-encoded so it survives JSON history.
        self.thought_signature = thought_signature

    def model_dump(self, exclude_none: bool = True) -> dict:
        block = {"type": "text", "text": self.text}
        if self.thought_signature:
            block["thought_signature"] = self.thought_signature
        return block


class _ToolUseBlock:
    type = "tool_use"

    def __init__(self, id: str, name: str, input: dict, thought_signature: str | None = None):
        self.id = id
        self.name = name
        self.input = input
        # Gemini's opaque per-call thinking token, base64-encoded so it survives
        # JSON serialization in history. Must be echoed back on the functionCall
        # part or Gemini rejects subsequent requests with a 400.
        self.thought_signature = thought_signature

    def model_dump(self, exclude_none: bool = True) -> dict:
        block = {
            "type": "tool_use",
            "id": self.id,
            "name": self.name,
            "input": self.input,
        }
        if self.thought_signature:
            block["thought_signature"] = self.thought_signature
        return block


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
    def __init__(self, content: list, usage: _Usage, stop_reason: str):
        self.content = content
        self.usage = usage
        self.stop_reason = stop_reason


# ─── Anthropic → Gemini input translation ────────────────────────────


# Frames the dynamic system context once it's relocated from `system_instruction`
# into a trailing user part, so the model reads it as ambient context rather
# than the user's literal message.
_DYNAMIC_CONTEXT_HEADER = (
    "[Ambient session context — current time, active project, and recent "
    "memory. Not part of the user's message.]"
)


def _flatten_blocks(blocks) -> str | None:
    """Join a list of Anthropic text blocks (or raw strings) into one string."""
    parts = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif isinstance(block, str):
            parts.append(block)
    return "\n\n".join(p for p in parts if p) or None


def _split_system(system) -> tuple[str | None, str | None]:
    """Split Anthropic system blocks into (stable, dynamic) text for Gemini.

    Blocks carrying `cache_control` are the stable, cacheable prefix → they
    become Gemini's `system_instruction`, which stays byte-identical every
    call and so engages Gemini implicit caching. Blocks WITHOUT `cache_control`
    (timestamp, daily logs, recovery advisories) are dynamic → returned
    separately so the caller can place them at the tail of `contents`, where a
    per-call change never busts the cached prefix as the conversation grows.

    A plain-string system is treated as fully stable; `None` yields (None, None).
    """
    if system is None:
        return None, None
    if isinstance(system, str):
        return (system or None), None
    stable = [b for b in system if isinstance(b, dict) and b.get("cache_control")]
    dynamic = [b for b in system if not (isinstance(b, dict) and b.get("cache_control"))]
    return _flatten_blocks(stable), _flatten_blocks(dynamic)


def _tools_to_gemini(tools):
    """Convert Anthropic tool defs to a single Gemini Tool of function
    declarations. `cache_control` and other extra keys are ignored."""
    if not tools:
        return None
    declarations = [
        types.FunctionDeclaration(
            name=tool["name"],
            description=tool.get("description", ""),
            parameters_json_schema=tool.get("input_schema"),
        )
        for tool in tools
    ]
    return [types.Tool(function_declarations=declarations)]


def _tool_result_text(content) -> str:
    """tool_result content is a string or a list of Anthropic blocks (text +
    image, e.g. browser screenshots); coerce to text. Image blocks are noted —
    their pixels travel separately via FunctionResponse.parts."""
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


def _tool_result_media_parts(content) -> list | None:
    """Extract image blocks from tool_result content as FunctionResponseParts.

    Gemini 3 series models accept multimodal function responses via
    `FunctionResponse.parts` (see ai.google.dev/gemini-api/docs/function-calling
    — "Multimodal function responses"); this is how a browser screenshot
    becomes actual vision input on the tool-result turn."""
    if not isinstance(content, list):
        return None
    parts = []
    for block in content:
        if not (isinstance(block, dict) and block.get("type") == "image"):
            continue
        src = block.get("source", {})
        if src.get("type") == "base64" and src.get("data"):
            parts.append(
                types.FunctionResponsePart(
                    inline_data=types.FunctionResponseBlob(
                        mime_type=src.get("media_type"),
                        data=base64.b64decode(src["data"]),
                    )
                )
            )
    return parts or None


def _ends_with_tool_result(messages: list) -> bool:
    """True when the last message is a function-response turn (mid tool cascade).

    Gemini decides the "current turn" by scanning newest→oldest for the most
    recent user message with standard (text) content; everything after it is the
    live reasoning turn. Appending the dynamic-context text turn after a
    functionResponse would plant a fresh turn boundary on every tool step,
    resetting the model's stateful reasoning each iteration — the root cause of
    the agent re-deriving its plan and looping. So we suppress it mid-cascade.
    """
    if not messages:
        return False
    content = messages[-1].get("content")
    if isinstance(content, list):
        return any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        )
    return False


def _messages_to_contents(messages: list, trailing_text: str | None = None) -> list:
    """Translate Anthropic-format messages into Gemini `Content` objects.

    A tool_result block only carries a `tool_use_id`, but Gemini's
    FunctionResponse needs the function *name*. Since the full message history
    is passed on every call, we first build an id→name map from all tool_use
    blocks, then resolve each tool_result against it.

    `trailing_text` (the dynamic system context) is appended as its own closing
    user turn so it sits behind the stable, cacheable prefix — but ONLY at the
    start of a turn, never mid tool-cascade (see `_ends_with_tool_result`).
    The input `messages` are never mutated — fresh `Content` objects are built.
    """
    id_to_name: dict[str, str] = {}
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    id_to_name[block.get("id")] = block.get("name")

    contents = []
    for msg in messages:
        role = "model" if msg.get("role") == "assistant" else "user"
        content = msg.get("content")
        parts = []

        if isinstance(content, str):
            if content:
                parts.append(types.Part(text=content))
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")

                if btype == "text":
                    sig_b64 = block.get("thought_signature")
                    parts.append(
                        types.Part(
                            text=block.get("text", ""),
                            thought_signature=base64.b64decode(sig_b64) if sig_b64 else None,
                        )
                    )

                elif btype == "image":
                    src = block.get("source", {})
                    if src.get("type") == "base64" and src.get("data"):
                        parts.append(
                            types.Part(
                                inline_data=types.Blob(
                                    mime_type=src.get("media_type"),
                                    data=base64.b64decode(src["data"]),
                                )
                            )
                        )

                elif btype == "tool_use":
                    sig_b64 = block.get("thought_signature")
                    parts.append(
                        types.Part(
                            function_call=types.FunctionCall(
                                id=block.get("id"),
                                name=block.get("name"),
                                args=block.get("input") or {},
                            ),
                            thought_signature=base64.b64decode(sig_b64) if sig_b64 else None,
                        )
                    )

                elif btype == "tool_result":
                    tool_use_id = block.get("tool_use_id")
                    result_content = block.get("content")
                    fr_kwargs = {}
                    media_parts = _tool_result_media_parts(result_content)
                    if media_parts:
                        fr_kwargs["parts"] = media_parts
                    parts.append(
                        types.Part(
                            function_response=types.FunctionResponse(
                                id=tool_use_id,
                                name=id_to_name.get(tool_use_id, tool_use_id),
                                response={"result": _tool_result_text(result_content)},
                                **fr_kwargs,
                            )
                        )
                    )

        if parts:
            contents.append(types.Content(role=role, parts=parts))

    if trailing_text and not _ends_with_tool_result(messages):
        # Place the dynamic context as its OWN trailing user turn, explicitly
        # closed. Appending it as a raw part onto the last content made Gemini
        # read the "field: value" ambient block as a template to keep filling in,
        # so it emitted a spurious echo part ("Active project: ...", "Recent
        # memory: ...") before the real answer — especially after a tool result.
        # A separate, closed turn keeps it as ambient context behind the cached
        # prefix without inviting continuation.
        framed = types.Part(
            text=(
                f"{_DYNAMIC_CONTEXT_HEADER}\n\n{trailing_text}\n\n"
                "[End of ambient context. Reply to the conversation above.]"
            )
        )
        contents.append(types.Content(role="user", parts=[framed]))

    return contents


# ─── Gemini → Anthropic output translation ───────────────────────────


def _finish_reason_name(finish_reason) -> str:
    return getattr(finish_reason, "name", None) or (str(finish_reason) if finish_reason else "")


def _map_usage(usage_metadata) -> _Usage:
    """Map Gemini usage_metadata onto Anthropic usage fields.

    Gemini's prompt_token_count is the full input incl. cached tokens, so we
    subtract cached to mirror Anthropic, where input_tokens excludes cache
    reads. Thinking tokens are billed as output, so they're folded in.
    """
    if usage_metadata is None:
        return _Usage(0, 0)
    prompt = getattr(usage_metadata, "prompt_token_count", 0) or 0
    cached = getattr(usage_metadata, "cached_content_token_count", 0) or 0
    candidates = getattr(usage_metadata, "candidates_token_count", 0) or 0
    thoughts = getattr(usage_metadata, "thoughts_token_count", 0) or 0
    return _Usage(
        input_tokens=max(prompt - cached, 0),
        output_tokens=candidates + thoughts,
        cache_read_input_tokens=cached,
        cache_creation_input_tokens=0,
    )


def _new_tool_id() -> str:
    return f"call_{uuid.uuid4().hex[:24]}"



def _recursive_dict(obj):
    if isinstance(obj, dict):
        return {k: _recursive_dict(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_recursive_dict(x) for x in obj]
    if hasattr(obj, "items"):
        return {k: _recursive_dict(v) for k, v in obj.items()}
    return obj


def _parts_to_blocks(parts) -> tuple[list, bool]:
    """Convert Gemini content parts to Anthropic content blocks.

    Returns (blocks, has_tool_call). Text is concatenated into one text block
    (mirroring Anthropic, which emits a single assistant text block); function
    calls become tool_use blocks. "thought" parts are dropped from content.
    """
    text_pieces = []
    tool_blocks = []
    # For a no-functionCall response the signature rides the LAST part (which may
    # be a thought part whose text we drop). Track the most recent one so we can
    # re-attach it to the merged text block. Ignored when the response carries
    # tool calls — there the signature lives on the first functionCall part.
    text_signature = None
    for part in parts or []:
        function_call = getattr(part, "function_call", None)
        if function_call is not None:
            sig = getattr(part, "thought_signature", None)
            tool_blocks.append(
                _ToolUseBlock(
                    id=getattr(function_call, "id", None) or _new_tool_id(),
                    name=function_call.name,
                    input=_recursive_dict(function_call.args) if function_call.args else {},
                    thought_signature=base64.b64encode(sig).decode() if sig else None,
                )
            )
            continue
        sig = getattr(part, "thought_signature", None)
        if sig:
            text_signature = base64.b64encode(sig).decode()
        text = getattr(part, "text", None)
        if text and not getattr(part, "thought", False):
            text_pieces.append(text)

    blocks = []
    if text_pieces:
        # Only round-trip the text-part signature for the no-functionCall case;
        # with tool calls the validated signature is on the functionCall block.
        blocks.append(
            _TextBlock(
                "".join(text_pieces),
                thought_signature=None if tool_blocks else text_signature,
            )
        )
    blocks.extend(tool_blocks)
    return blocks, bool(tool_blocks)


def _response_to_message(response) -> _Message:
    candidates = getattr(response, "candidates", None)
    candidate = candidates[0] if candidates else None

    parts = None
    if candidate is not None:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) if content else None

    blocks, has_tool_call = _parts_to_blocks(parts)

    if has_tool_call:
        stop_reason = "tool_use"
    elif _finish_reason_name(getattr(candidate, "finish_reason", None)) == "MAX_TOKENS":
        stop_reason = "max_tokens"
    else:
        stop_reason = "end_turn"

    return _Message(blocks, _map_usage(getattr(response, "usage_metadata", None)), stop_reason)


# ─── Provider ─────────────────────────────────────────────────────────


class GeminiProvider(BaseModelProvider):
    def __init__(self, api_key: str = None):
        key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        # Leave SDK HttpRetryOptions unset (google-genai defaults to no retry).
        # Transient 429/5xx/timeouts are handled by with_llm_retry /
        # stream_with_llm_retry so Retry-After is honoured in one place.
        self.client = genai.Client(api_key=key)

    @staticmethod
    def _coerce_thinking_level(name: str):
        enum = getattr(types, "ThinkingLevel", None)
        if enum is None:
            return name
        return getattr(enum, name.upper(), None) or getattr(enum, name, name)

    def _thinking_config(
        self, model: str, *, thinking: bool = True, effort: str | None = None
    ) -> types.ThinkingConfig | None:
        """Map Tower effort onto Gemini thinking_level / thinking_budget.

        Gemini 3 gets `thinking_level` only. Gemini 2.5 gets `thinking_budget`
        (`0` off, `-1` dynamic). Never both — Gemini 3 Pro misbehaves if a
        budget is sent.

        `include_thoughts=True` surfaces thought summaries as `thought` parts
        on real agent turns. ``thinking=False`` (titles, gates) uses the
        cheapest legal setting for that model and skips thought summaries.
        """
        raw = _thinking_kwargs(model, thinking=thinking, effort=effort)
        if raw is None:
            return None
        kwargs = dict(raw)
        level = kwargs.get("thinking_level")
        if isinstance(level, str):
            kwargs["thinking_level"] = self._coerce_thinking_level(level)
        return types.ThinkingConfig(**kwargs)

    def _build_config(
        self, model, stable_system, tools, max_tokens, *, thinking=True,
        temperature=None, effort=None,
    ):
        kwargs = dict(
            system_instruction=stable_system,
            max_output_tokens=max_tokens,
            tools=_tools_to_gemini(tools),
            # We declare tools manually and execute them ourselves; never let
            # the SDK auto-invoke anything.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        thinking_cfg = self._thinking_config(model, thinking=thinking, effort=effort)
        if thinking_cfg is not None:
            kwargs["thinking_config"] = thinking_cfg
        if temperature is not None:
            kwargs["temperature"] = temperature
        return types.GenerateContentConfig(**kwargs)

    async def create_message(
        self, *, model, max_tokens, messages, system=None, tools=None, thinking=True,
        temperature=None, effort=None,
    ):
        stable, dynamic = _split_system(system)
        contents = _messages_to_contents(messages, trailing_text=dynamic)
        config = self._build_config(
            model, stable, tools, max_tokens, thinking=thinking,
            temperature=temperature, effort=effort,
        )

        async def _once():
            response = await self.client.aio.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
            return _response_to_message(response)

        return await with_llm_retry(_once)

    async def stream_message(
        self, *, model, max_tokens, messages, system=None, tools=None, thinking=True,
        effort=None,
    ):
        """True chunk streaming. Yields ("thought"|"text", delta) as Gemini
        emits parts, then ("message", _Message) assembled from every part so
        the agent loop sees the same response shape as `create_message`.
        """
        stable, dynamic = _split_system(system)
        contents = _messages_to_contents(messages, trailing_text=dynamic)
        config = self._build_config(
            model, stable, tools, max_tokens, thinking=thinking, effort=effort
        )

        async def _stream_once():
            stream = await self.client.aio.models.generate_content_stream(
                model=model,
                contents=contents,
                config=config,
            )

            all_parts = []
            usage_metadata = None
            finish_reason = None

            async for chunk in stream:
                candidates = getattr(chunk, "candidates", None)
                candidate = candidates[0] if candidates else None
                if candidate is not None:
                    content = getattr(candidate, "content", None)
                    for part in (getattr(content, "parts", None) or []):
                        all_parts.append(part)
                        text = getattr(part, "text", None)
                        if text:
                            kind = "thought" if getattr(part, "thought", False) else "text"
                            yield (kind, text)
                    fr = getattr(candidate, "finish_reason", None)
                    if fr is not None:
                        finish_reason = fr
                if getattr(chunk, "usage_metadata", None) is not None:
                    usage_metadata = chunk.usage_metadata

            blocks, has_tool_call = _parts_to_blocks(all_parts)
            if has_tool_call:
                stop_reason = "tool_use"
            elif _finish_reason_name(finish_reason) == "MAX_TOKENS":
                stop_reason = "max_tokens"
            else:
                stop_reason = "end_turn"

            yield ("message", _Message(blocks, _map_usage(usage_metadata), stop_reason))

        async for item in stream_with_llm_retry(_stream_once):
            yield item

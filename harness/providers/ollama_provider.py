"""Ollama implementation of `BaseModelProvider`.

Wraps the official `ollama` AsyncClient and adapts it to the *Anthropic-shaped*
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
to Ollama's chat messages / OpenAI-style tool defs.

Ollama reuses KV-cache prefixes automatically — there is no explicit
`cache_control` to set. Gemini-only fields in stored history
(`thought_signature`) are ignored so mid-chat Gemini → Ollama switches stay
safe. Thinking models emit `message.thinking`, mapped onto the existing
`("thought", delta)` stream contract.

Config:
  OLLAMA_HOST     — default http://localhost:11434
  OLLAMA_NUM_CTX  — context window passed as options.num_ctx (default 65536).
                    qwen3-vl native is 256K, but KV-cache RAM is the practical
                    limit; set this explicitly because Ollama's default (~8K)
                    would truncate our system prompt.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from .base import BaseModelProvider


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
    def __init__(self, content: list, usage: _Usage, stop_reason: str):
        self.content = content
        self.usage = usage
        self.stop_reason = stop_reason


# ─── Anthropic → Ollama input translation ────────────────────────────


def _flatten_system(system) -> str | None:
    """Join Anthropic system blocks (or a plain string) into one system string.

    `cache_control` markers are stripped — Ollama has no explicit cache API.
    """
    if system is None:
        return None
    if isinstance(system, str):
        return system or None
    parts = []
    for block in system:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            if text:
                parts.append(text)
        elif isinstance(block, str) and block:
            parts.append(block)
    return "\n\n".join(parts) or None


def _tools_to_ollama(tools) -> list | None:
    """Convert Anthropic tool defs to OpenAI-style Ollama tool schemas."""
    if not tools:
        return None
    out = []
    for tool in tools:
        out.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
                },
            }
        )
    return out


def _tool_result_text(content) -> str:
    """tool_result content is a string or a list of Anthropic blocks; coerce to text."""
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


def _extract_images(content) -> list[str]:
    """Pull base64 image payloads out of Anthropic content blocks."""
    if not isinstance(content, list):
        return []
    images = []
    for block in content:
        if not (isinstance(block, dict) and block.get("type") == "image"):
            continue
        src = block.get("source", {})
        if src.get("type") == "base64" and src.get("data"):
            images.append(src["data"])
    return images


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


def _tool_call_to_ollama(block: dict) -> dict:
    """Anthropic tool_use block → Ollama tool_call dict."""
    return {
        "type": "function",
        "function": {
            "name": block.get("name"),
            "arguments": block.get("input") or {},
        },
    }


def _messages_to_ollama(messages: list, system: Any = None) -> list[dict]:
    """Translate Anthropic-format messages into Ollama chat messages.

    A tool_result block only carries a `tool_use_id`, but Ollama's tool role
    needs the function *name*. Since the full message history is passed on
    every call, we first build an id→name map from all tool_use blocks, then
    resolve each tool_result against it.

    Gemini-only `thought_signature` fields are ignored.
    """
    id_to_name: dict[str, str] = {}
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    id_to_name[block.get("id")] = block.get("name")

    out: list[dict] = []
    system_text = _flatten_system(system)
    if system_text:
        out.append({"role": "system", "content": system_text})

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")

        if isinstance(content, str):
            if content or role == "assistant":
                out.append({"role": role, "content": content or ""})
            continue

        if not isinstance(content, list):
            continue

        # tool_result blocks become separate role=tool messages.
        tool_results = [
            b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"
        ]
        if tool_results:
            for block in tool_results:
                tool_use_id = block.get("tool_use_id")
                result_content = block.get("content")
                tool_msg: dict = {
                    "role": "tool",
                    "tool_name": id_to_name.get(tool_use_id, tool_use_id),
                    "content": _tool_result_text(result_content),
                }
                images = _extract_images(result_content)
                if images:
                    tool_msg["images"] = images
                out.append(tool_msg)
            continue

        text_parts: list[str] = []
        images: list[str] = []
        tool_calls: list[dict] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text", "")
                if text:
                    text_parts.append(text)
            elif btype == "image":
                src = block.get("source", {})
                if src.get("type") == "base64" and src.get("data"):
                    images.append(src["data"])
            elif btype == "tool_use":
                tool_calls.append(_tool_call_to_ollama(block))

        ollama_msg: dict = {
            "role": "assistant" if role == "assistant" else "user",
            "content": "\n".join(text_parts),
        }
        if images:
            ollama_msg["images"] = images
        if tool_calls:
            ollama_msg["tool_calls"] = tool_calls
        # Skip empty user messages with nothing useful.
        if not ollama_msg["content"] and not images and not tool_calls:
            continue
        out.append(ollama_msg)

    return out


# ─── Ollama → Anthropic output translation ───────────────────────────


def _map_usage(response) -> _Usage:
    """Map Ollama prompt_eval_count / eval_count onto Anthropic usage fields."""
    prompt = getattr(response, "prompt_eval_count", None)
    if prompt is None and isinstance(response, dict):
        prompt = response.get("prompt_eval_count")
    eval_count = getattr(response, "eval_count", None)
    if eval_count is None and isinstance(response, dict):
        eval_count = response.get("eval_count")
    return _Usage(
        input_tokens=int(prompt or 0),
        output_tokens=int(eval_count or 0),
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )


def _tool_calls_from_message(message) -> list[_ToolUseBlock]:
    raw = getattr(message, "tool_calls", None)
    if raw is None and isinstance(message, dict):
        raw = message.get("tool_calls")
    if not raw:
        return []
    blocks = []
    for call in raw:
        fn = getattr(call, "function", None)
        if fn is None and isinstance(call, dict):
            fn = call.get("function") or {}
            name = fn.get("name") if isinstance(fn, dict) else None
            args = fn.get("arguments") if isinstance(fn, dict) else {}
        else:
            name = getattr(fn, "name", None)
            args = getattr(fn, "arguments", None) or {}
        call_id = getattr(call, "id", None)
        if call_id is None and isinstance(call, dict):
            call_id = call.get("id")
        blocks.append(
            _ToolUseBlock(
                id=call_id or _new_tool_id(),
                name=name or "unknown",
                input=_recursive_dict(args) if args else {},
            )
        )
    return blocks


def _message_content(message) -> str:
    if message is None:
        return ""
    text = getattr(message, "content", None)
    if text is None and isinstance(message, dict):
        text = message.get("content")
    return text or ""


def _message_thinking(message) -> str:
    if message is None:
        return ""
    text = getattr(message, "thinking", None)
    if text is None and isinstance(message, dict):
        text = message.get("thinking")
    return text or ""


def _done_reason(response) -> str | None:
    reason = getattr(response, "done_reason", None)
    if reason is None and isinstance(response, dict):
        reason = response.get("done_reason")
    return reason


def _response_to_message(response) -> _Message:
    message = getattr(response, "message", None)
    if message is None and isinstance(response, dict):
        message = response.get("message")

    tool_blocks = _tool_calls_from_message(message)
    text = _message_content(message)

    blocks: list = []
    if text:
        blocks.append(_TextBlock(text))
    blocks.extend(tool_blocks)

    if tool_blocks:
        stop_reason = "tool_use"
    elif _done_reason(response) == "length":
        stop_reason = "max_tokens"
    else:
        stop_reason = "end_turn"

    return _Message(blocks, _map_usage(response), stop_reason)


def _default_num_ctx() -> int:
    env = os.environ.get("OLLAMA_NUM_CTX")
    if env and env.isdigit():
        return int(env)
    return 65_536


# ─── Provider ─────────────────────────────────────────────────────────


class OllamaProvider(BaseModelProvider):
    def __init__(self, host: str | None = None, num_ctx: int | None = None):
        from ollama import AsyncClient

        self.host = host or os.environ.get("OLLAMA_HOST") or "http://localhost:11434"
        self.num_ctx = num_ctx if num_ctx is not None else _default_num_ctx()
        self.client = AsyncClient(host=self.host)

    def _options(self, max_tokens: int, temperature: float | None = None) -> dict:
        options = {
            "num_ctx": self.num_ctx,
            "num_predict": max_tokens,
        }
        if temperature is not None:
            options["temperature"] = temperature
        return options

    async def create_message(
        self, *, model, max_tokens, messages, system=None, tools=None, thinking=True,
        temperature=None,
    ):
        response = await self.client.chat(
            model=model,
            messages=_messages_to_ollama(messages, system=system),
            tools=_tools_to_ollama(tools),
            stream=False,
            think=thinking,
            options=self._options(max_tokens, temperature),
        )
        return _response_to_message(response)

    async def stream_message(
        self, *, model, max_tokens, messages, system=None, tools=None, thinking=True
    ):
        """True chunk streaming. Yields ("thought"|"text", delta) as Ollama
        emits thinking/content, then ("message", _Message) assembled from the
        accumulated fields so the agent loop sees the same response shape as
        `create_message`.
        """
        stream = await self.client.chat(
            model=model,
            messages=_messages_to_ollama(messages, system=system),
            tools=_tools_to_ollama(tools),
            stream=True,
            think=thinking,
            options=self._options(max_tokens),
        )

        thinking = ""
        content = ""
        tool_calls: list = []
        final_chunk = None

        async for chunk in stream:
            final_chunk = chunk
            message = getattr(chunk, "message", None)
            if message is None and isinstance(chunk, dict):
                message = chunk.get("message")

            thought_delta = _message_thinking(message)
            if thought_delta:
                thinking += thought_delta
                yield ("thought", thought_delta)

            text_delta = _message_content(message)
            if text_delta:
                content += text_delta
                yield ("text", text_delta)

            # tool_calls may arrive as a full list on one chunk; accumulate.
            chunk_tools = getattr(message, "tool_calls", None) if message is not None else None
            if chunk_tools is None and isinstance(message, dict):
                chunk_tools = message.get("tool_calls")
            if chunk_tools:
                tool_calls.extend(chunk_tools)

        # Build a synthetic response object from accumulated fields + final
        # chunk metadata (usage / done_reason live on the last chunk).
        class _AccumMessage:
            pass

        accum = _AccumMessage()
        accum.content = content
        accum.thinking = thinking
        accum.tool_calls = tool_calls

        class _AccumResponse:
            pass

        resp = _AccumResponse()
        resp.message = accum
        if final_chunk is not None:
            resp.prompt_eval_count = getattr(final_chunk, "prompt_eval_count", None)
            if resp.prompt_eval_count is None and isinstance(final_chunk, dict):
                resp.prompt_eval_count = final_chunk.get("prompt_eval_count")
            resp.eval_count = getattr(final_chunk, "eval_count", None)
            if resp.eval_count is None and isinstance(final_chunk, dict):
                resp.eval_count = final_chunk.get("eval_count")
            resp.done_reason = _done_reason(final_chunk)
        else:
            resp.prompt_eval_count = 0
            resp.eval_count = 0
            resp.done_reason = None

        yield ("message", _response_to_message(resp))

"""Claude-on-Bedrock implementation of `BaseModelProvider`.

Replaces the old direct-to-Anthropic provider. `AsyncAnthropicBedrock` talks to
`bedrock-runtime` (`POST /model/{modelId}/invoke`), which returns the *native*
Anthropic Messages response — `.content` blocks, `.usage`, `.stop_reason` — so
unlike the Gemini and Mantle providers this one needs no response adapter. The
SDK picks up `AWS_BEARER_TOKEN_BEDROCK` (or normal SigV4 credentials) itself.

Note the Claude models we use are `INFERENCE_PROFILE`-only on Bedrock: the bare
`anthropic.claude-opus-4-6-v1` is not invokable and the request must name a
cross-region profile (`global.anthropic.…`). Those ids live in
`model_catalog.wire_id`. Claude is deliberately NOT reached over the
`bedrock-mantle` endpoint — Mantle serves no Anthropic models in ap-south-1.

Unlike the provider this replaces, extended thinking and temperature are now
both honoured. Thinking blocks are left in `.content` on purpose: Anthropic
requires the `thinking` block and its `signature` to be echoed back on the next
turn of a tool-use cascade, and the agent's history serializer round-trips
whatever blocks it is given. Text extraction elsewhere filters on
`type == "text"`, so thought content never leaks into a user-visible reply.
"""

import os

from anthropic import AsyncAnthropicBedrock

from .base import BaseModelProvider
from .llm_retry import stream_with_llm_retry, with_llm_retry


DEFAULT_REGION = "us-east-1"

# Tower's effort vocabulary → extended-thinking budget in tokens. Anthropic
# rejects a budget below 1024, so "off" disables thinking rather than asking
# for a smaller one.
_EFFORT_BUDGET = {
    "off": None,
    "minimal": 1024,
    "low": 2048,
    "medium": 8192,
    "high": 16384,
    "dynamic": 16384,
}
_DEFAULT_BUDGET = 16384

# Thinking needs headroom for the visible reply on top of the budget; without
# this a max_tokens equal to the budget leaves nothing to answer with.
_MIN_REPLY_HEADROOM = 1024


class BedrockAnthropicProvider(BaseModelProvider):
    def __init__(self, api_key: str = None, region: str | None = None):
        self.region = region or os.environ.get("BEDROCK_REGION") or DEFAULT_REGION
        kwargs = {"aws_region": self.region, "max_retries": 0}
        # On the Bedrock client `api_key` IS the Bedrock bearer token. Left
        # unset, the SDK falls back to AWS_BEARER_TOKEN_BEDROCK and then to
        # ordinary SigV4 credentials (EC2/ECS role).
        token = api_key or os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
        if token:
            kwargs["api_key"] = token
        # max_retries=0: with_llm_retry owns cooldown, Retry-After, and the
        # attempt budget so they live in exactly one place.
        self.client = AsyncAnthropicBedrock(**kwargs)

    @staticmethod
    def _thinking_budget(model, max_tokens, thinking, effort) -> int | None:
        """Extended-thinking budget for this call, or None to leave it off."""
        from .. import model_catalog

        entry = model_catalog.get(model)
        if entry is not None and not entry.supports_thinking:
            return None
        if not thinking:
            return None
        budget = _EFFORT_BUDGET.get(effort or "", _DEFAULT_BUDGET)
        if budget is None:
            return None
        # Clamp so budget + reply always fits; below Anthropic's 1024 floor the
        # request would 400, so drop thinking instead of sending an illegal one.
        budget = min(budget, max_tokens - _MIN_REPLY_HEADROOM)
        return budget if budget >= 1024 else None

    def _build_kwargs(
        self, model, max_tokens, messages, system, tools, *, thinking=True,
        temperature=None, effort=None,
    ) -> dict:
        from .. import model_catalog

        kwargs = {
            "model": model_catalog.wire_id(model),
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system is not None:
            kwargs["system"] = system
        if tools is not None:
            kwargs["tools"] = tools

        budget = self._thinking_budget(model, max_tokens, thinking, effort)
        if budget is not None:
            # Anthropic forces temperature to 1 while thinking is enabled and
            # rejects the two together, so temperature is dropped here. Callers
            # that need determinism (judges, classifiers) pass thinking=False.
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
        elif temperature is not None:
            kwargs["temperature"] = temperature
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
            return await self.client.messages.create(**kwargs)

        return await with_llm_retry(
            _once, provider="bedrock_anthropic", model=model,
            **({} if attempts is None else {"attempts": attempts}),
        )

    async def stream_message(
        self, *, model, max_tokens, messages, system=None, tools=None, thinking=True,
        effort=None,
    ):
        """True token streaming. Yields ("thought"|"text", delta), then
        ("message", response) with the assembled native response.

        The base class fallback would have made one blocking call and emitted
        the whole reply at once, which is what Claude turns did before this.
        """
        kwargs = self._build_kwargs(
            model, max_tokens, messages, system, tools,
            thinking=thinking, effort=effort,
        )

        async def _stream_once():
            async with self.client.messages.stream(**kwargs) as stream:
                async for event in stream:
                    if event.type != "content_block_delta":
                        continue
                    delta = event.delta
                    kind = getattr(delta, "type", None)
                    if kind == "thinking_delta":
                        yield ("thought", delta.thinking)
                    elif kind == "text_delta":
                        yield ("text", delta.text)
                yield ("message", await stream.get_final_message())

        async for item in stream_with_llm_retry(
            _stream_once, provider="bedrock_anthropic", model=model
        ):
            yield item

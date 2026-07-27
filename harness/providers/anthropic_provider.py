"""Anthropic implementation of `BaseModelProvider`.

Thin wrapper over `AsyncAnthropic`. It passes arguments straight through and
returns the SDK's native response/stream objects, so behaviour is identical
to calling the SDK directly — except transient 429/5xx/timeouts are retried
here (SDK `max_retries` disabled so Retry-After lives in one place).
"""

import os
from anthropic import AsyncAnthropic

from .base import BaseModelProvider
from .llm_retry import with_llm_retry


class AnthropicProvider(BaseModelProvider):
    def __init__(self, api_key: str = None):
        # Disable the SDK's own retries; with_llm_retry owns cooldown + Retry-After.
        self.client = AsyncAnthropic(
            api_key=api_key or os.environ["ANTHROPIC_API_KEY"],
            max_retries=0,
        )

    @staticmethod
    def _build_kwargs(model, max_tokens, messages, system, tools) -> dict:
        kwargs = {"model": model, "max_tokens": max_tokens, "messages": messages}
        if system is not None:
            kwargs["system"] = system
        if tools is not None:
            kwargs["tools"] = tools
        return kwargs

    async def create_message(
        self, *, model, max_tokens, messages, system=None, tools=None, thinking=True
    ):
        del thinking  # Anthropic Messages API has no thinking toggle here.
        kwargs = self._build_kwargs(model, max_tokens, messages, system, tools)

        async def _once():
            return await self.client.messages.create(**kwargs)

        return await with_llm_retry(_once)

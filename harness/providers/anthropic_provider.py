"""Anthropic implementation of `BaseModelProvider`.

Thin wrapper over `AsyncAnthropic`. It passes arguments straight through and
returns the SDK's native response/stream objects, so behaviour is identical
to calling the SDK directly.
"""

import os
from anthropic import AsyncAnthropic

from .base import BaseModelProvider


class AnthropicProvider(BaseModelProvider):
    def __init__(self, api_key: str = None):
        self.client = AsyncAnthropic(api_key=api_key or os.environ["ANTHROPIC_API_KEY"])

    @staticmethod
    def _build_kwargs(model, max_tokens, messages, system, tools) -> dict:
        kwargs = {"model": model, "max_tokens": max_tokens, "messages": messages}
        if system is not None:
            kwargs["system"] = system
        if tools is not None:
            kwargs["tools"] = tools
        return kwargs

    async def create_message(
        self, *, model, max_tokens, messages, system=None, tools=None
    ):
        return await self.client.messages.create(
            **self._build_kwargs(model, max_tokens, messages, system, tools)
        )

"""Model-provider abstraction.

`GaladrielAgent` and the compaction routine talk to an LLM through a
`BaseModelProvider` rather than the Anthropic SDK directly, so the backend
is swappable. Responses are the native Anthropic SDK objects for now; the
agent reads `.usage`, `.content`, and `.stop_reason` off them.
"""

from abc import ABC, abstractmethod
from typing import Any


class BaseModelProvider(ABC):
    """Interface every model backend must implement."""

    @abstractmethod
    async def create_message(
        self,
        *,
        model: str,
        max_tokens: int,
        messages: list,
        system: Any = None,
        tools: list | None = None,
        thinking: bool = True,
        temperature: float | None = None,
        effort: str | None = None,
        attempts: int | None = None,
    ) -> Any:
        """Single-shot completion. Returns the provider's message response.

        ``thinking=False`` asks the backend to skip chain-of-thought when it
        supports that (Gemini thinking_budget=0, Ollama think=False).

        ``attempts=1`` disables the transient-error retry for callers on a hard
        deadline, where a backoff would burn the budget they were going to use
        for the answer. Everything else leaves it None for the default budget.

        ``temperature=None`` leaves the backend default alone. Deterministic
        callers — classifiers, judges, extractors, anything whose output is
        parsed rather than read — pass ``0.0``: the same input must produce the
        same verdict, or the eval numbers measure sampling noise as much as the
        model.
        """
        ...

    async def stream_message(
        self,
        *,
        model: str,
        max_tokens: int,
        messages: list,
        system: Any = None,
        tools: list | None = None,
        thinking: bool = True,
        effort: str | None = None,
    ):
        """Streaming variant. Async-yields ("text"|"thought", str) deltas as
        they arrive, then a final ("message", response) carrying the assembled
        response (same shape as `create_message`).

        Default implementation has no real streaming: it makes one
        `create_message` call and emits each text block in one shot. Providers
        can override for true token-by-token streaming.
        """
        msg = await self.create_message(
            model=model,
            max_tokens=max_tokens,
            messages=messages,
            system=system,
            tools=tools,
            thinking=thinking,
            effort=effort,
        )
        for block in getattr(msg, "content", None) or []:
            if getattr(block, "type", None) == "text":
                text = getattr(block, "text", "")
                if text:
                    yield ("text", text)
        yield ("message", msg)

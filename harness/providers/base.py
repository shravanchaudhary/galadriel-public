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
    ) -> Any:
        """Single-shot completion. Returns the provider's message response."""
        ...

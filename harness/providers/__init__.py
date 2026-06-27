"""Model providers for the Galadriel harness."""

from .base import BaseModelProvider
from .anthropic_provider import AnthropicProvider

__all__ = ["BaseModelProvider", "AnthropicProvider", "GeminiProvider"]


def __getattr__(name):
    # Import GeminiProvider lazily so running on Anthropic never requires the
    # google-genai package to be installed.
    if name == "GeminiProvider":
        from .gemini_provider import GeminiProvider

        return GeminiProvider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

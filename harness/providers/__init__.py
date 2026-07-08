"""Model providers for the Galadriel harness."""

from .base import BaseModelProvider
from .anthropic_provider import AnthropicProvider

__all__ = ["BaseModelProvider", "AnthropicProvider", "GeminiProvider", "OllamaProvider"]


def __getattr__(name):
    # Import GeminiProvider / OllamaProvider lazily so running on Anthropic
    # never requires the google-genai or ollama packages to be installed.
    if name == "GeminiProvider":
        from .gemini_provider import GeminiProvider

        return GeminiProvider
    if name == "OllamaProvider":
        from .ollama_provider import OllamaProvider

        return OllamaProvider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

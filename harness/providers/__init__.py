"""Model providers for the Galadriel harness."""

from .base import BaseModelProvider

__all__ = [
    "BaseModelProvider",
    "BedrockAnthropicProvider",
    "BedrockMantleProvider",
    "GeminiProvider",
    "OllamaProvider",
]

# Every provider is imported lazily so that running on one backend never
# requires the others' packages (google-genai, ollama, boto3/anthropic) to be
# installed.
_LAZY = {
    "BedrockAnthropicProvider": ".bedrock_anthropic_provider",
    "BedrockMantleProvider": ".bedrock_mantle_provider",
    "GeminiProvider": ".gemini_provider",
    "OllamaProvider": ".ollama_provider",
}


def __getattr__(name):
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(module, __name__), name)

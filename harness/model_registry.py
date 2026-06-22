"""Single source of truth for model-per-task selection.

Edit THIS FILE to choose which provider and model handles each task. Nothing
else in the harness hardcodes a model name — the agent loop and the compaction
summarizer both resolve their provider/model here.

Because every provider returns responses in the same shape (see
harness/providers/base.py), switching a task to a different provider requires
NO other code changes: just flip the entry below.

Default provider is Gemini. To switch a task back to Claude, copy from
ANTHROPIC_DEFAULTS into TASKS.
"""

import os

from .providers import BaseModelProvider

ANTHROPIC = "anthropic"
GEMINI = "gemini"
DEFAULT_PROVIDER = GEMINI

# ─────────────────────────────────────────────────────────────────────
# Task → (provider, model). This is the ONLY place models are chosen.
# ─────────────────────────────────────────────────────────────────────
TASKS: dict[str, tuple[str, str]] = {
    # The main conversational agent — tool use, streaming, the full loop.
    "agent": (GEMINI, "gemini-3.1-pro-preview"),
    # The cheap summarizer /compact uses to shrink old tool results.
    "compaction": (GEMINI, "gemini-2.5-flash"),
}

# Previous Anthropic defaults — drop any of these back into TASKS to switch a
# task back to Claude (no other code changes needed).
ANTHROPIC_DEFAULTS: dict[str, tuple[str, str]] = {
    "agent": (ANTHROPIC, "claude-opus-4-8"),
    "compaction": (ANTHROPIC, "claude-haiku-4-5-20251001"),
}

# Env var each provider reads its API key from.
_ENV_KEY = {
    ANTHROPIC: "ANTHROPIC_API_KEY",
    GEMINI: "GEMINI_API_KEY",
}


def model_for(task: str) -> str:
    """Model name configured for `task`."""
    return TASKS[task][1]


def provider_name_for(task: str) -> str:
    """Provider id ('anthropic' | 'gemini') configured for `task`."""
    return TASKS[task][0]


def build_provider(name: str, api_key: str | None = None) -> BaseModelProvider:
    """Instantiate a provider by id. Imports are lazy so that, e.g., running
    on Anthropic never requires the google-genai package to be installed.
    """
    if name == GEMINI:
        from .providers import GeminiProvider
        return GeminiProvider(api_key=api_key)
    from .providers import AnthropicProvider
    return AnthropicProvider(api_key=api_key)


def get_provider(task: str, api_key: str | None = None) -> BaseModelProvider:
    """Build the provider configured for `task`.

    An explicitly-passed `api_key` is only forwarded when it belongs to the
    selected provider (Anthropic); otherwise each provider reads its own env
    var. This keeps existing callers that pass ANTHROPIC_API_KEY from leaking
    the wrong key into a Gemini client.
    """
    name = provider_name_for(task)
    return build_provider(name, api_key=api_key if name == ANTHROPIC else None)


def _provider_key_present(provider: str) -> bool:
    """True when the configured provider has a usable API key in the env."""
    if os.environ.get(_ENV_KEY[provider]):
        return True
    # GeminiProvider also accepts GOOGLE_API_KEY.
    if provider == GEMINI and os.environ.get("GOOGLE_API_KEY"):
        return True
    return False


def required_env_keys() -> set[str]:
    """Primary API-key env vars for providers currently configured in TASKS."""
    return {_ENV_KEY[provider] for provider, _ in TASKS.values()}


def missing_env_keys() -> list[str]:
    """Env vars still missing for providers in TASKS.

    Only checks providers actually selected in TASKS — e.g. with both tasks on
    Gemini, ANTHROPIC_API_KEY is not required. For Gemini, either
    GEMINI_API_KEY or GOOGLE_API_KEY satisfies the check.
    """
    missing = []
    for provider in {provider for provider, _ in TASKS.values()}:
        if not _provider_key_present(provider):
            if provider == GEMINI:
                missing.append("GEMINI_API_KEY (or GOOGLE_API_KEY)")
            else:
                missing.append(_ENV_KEY[provider])
    return sorted(missing)

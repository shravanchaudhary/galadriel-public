"""Single source of truth for model-per-task selection.

Edit THIS FILE to choose which provider and model handles each task. Nothing
else in the harness hardcodes a model name — the agent loop and the compaction
summarizer both resolve their provider/model here.

Which models *exist*, what they cost, and what they can do lives in
`harness/model_catalog.py`; this module only assigns them to tasks and builds
clients. Because every provider returns responses in the same shape (see
harness/providers/base.py), switching a task to a different provider requires
NO other code changes: just flip the entry below.

Default provider is Bedrock Mantle (GLM-5). Claude and the open models both run
on Bedrock (Claude via bedrock-runtime, the rest via bedrock-mantle) and share
one credential, `AWS_BEARER_TOKEN_BEDROCK`. Runtime Tower model switches go
through `provider_for_model()`, so changing model mid-chat also changes
provider.
"""

import os

from . import model_catalog
from .model_catalog import (
    BEDROCK_ANTHROPIC,
    BEDROCK_MANTLE,
    GEMINI,
    OLLAMA,
)
from .providers import BaseModelProvider

DEFAULT_PROVIDER = BEDROCK_MANTLE

# ─────────────────────────────────────────────────────────────────────
# Task → (provider, model). This is the ONLY place models are chosen.
# ─────────────────────────────────────────────────────────────────────
TASKS: dict[str, tuple[str, str]] = {
    # The main conversational agent — tool use, streaming, the full loop.
    "agent": (BEDROCK_MANTLE, "glm-5"),
    # The cheap summarizer /compact uses to shrink old tool results.
    "compaction": (BEDROCK_MANTLE, "glm-5"),
    # Lightweight structured gate for shared Slack channel replies.
    "slack_reply_gate": (BEDROCK_MANTLE, "glm-5"),
    # One-shot short title for a new conversation run.
    "chat_title": (BEDROCK_MANTLE, "glm-5"),
    # Authors a stored memory's retrieval trigger — ~60 positive phrasings plus
    # held-out probes (harness/recall_cues.py). Pinned to the medium tier: cue
    # quality decides whether a memory is ever recalled at all, but its cost
    # must not silently follow whatever chat model the user selected.
    "recall_cues": (BEDROCK_MANTLE, "replika-medium"),
    # Decides how a new memory relates to its nearest neighbours
    # (harness/memory_graph.py). Same tier and same reasoning as recall_cues —
    # a wrong DEPENDS_ON injects an irrelevant memory every time its partner
    # fires.
    "memory_edges": (BEDROCK_MANTLE, "replika-medium"),
    # Judges whether a confirmed preference is crucial enough for the
    # always-on prompt (harness/consolidation.promote_preferences). One call
    # per newly-eligible preference, verdict stored forever — rare and small,
    # but the judgment shapes every future turn's prompt, so medium tier.
    "promotion_gate": (BEDROCK_MANTLE, "replika-medium"),
}

# Bedrock equivalents — drop any of these into TASKS to move a task onto Claude
# or an open model (no other code changes needed).
BEDROCK_DEFAULTS: dict[str, tuple[str, str]] = {
    "agent": (BEDROCK_ANTHROPIC, "claude-opus-4-6"),
    "compaction": (BEDROCK_ANTHROPIC, "claude-haiku-4-5"),
    "slack_reply_gate": (BEDROCK_MANTLE, "glm-4.7-flash"),
    "chat_title": (BEDROCK_MANTLE, "glm-4.7-flash"),
}

# Side tasks that follow the agent's live main-channel model instead of the
# pins above. Whatever model the user selected in the chat interface handles
# these too, so a capped/broken side-provider (e.g. Gemini billing cap) can't
# fail a task while the main conversation works fine. The TASKS pins remain
# the fallback before the agent has registered its model.
#
# recall_cues and memory_edges are deliberately NOT here any more: following
# the active model meant switching the chat to Opus silently repriced every
# cue-generation and edge call ~5-8x. They are pinned to replika-medium above,
# so learning cost moves only when someone retargets the tier.
#
# The task-end and periodic memory consolidators (harness/agent.py
# run_task_consolidation; ambient reflection/goodnight) are NOT listed here —
# they don't need a pin at all. Both run as full agent turns through
# _respond_locked_inner on a channel (a disposable side channel, or
# reflection/goodnight), which resolves its model via model_for_channel();
# any channel with no explicit override already falls back to the live
# main-channel model.
FOLLOW_ACTIVE_MODEL = frozenset({
    "compaction", "chat_title", "slack_reply_gate",
})

_active_model: str | None = None


def set_active_model(model: str | None) -> None:
    """Record the agent's current main-channel model (called on init/switch)."""
    global _active_model
    _active_model = model or None


# Env var each provider reads its credential from. Ollama needs none, and both
# Bedrock providers share one key.
_ENV_KEY = {
    BEDROCK_ANTHROPIC: "AWS_BEARER_TOKEN_BEDROCK",
    BEDROCK_MANTLE: "AWS_BEARER_TOKEN_BEDROCK",
    GEMINI: "GEMINI_API_KEY",
}


def model_for(task: str) -> str:
    """Model name configured for `task` (live main model for follower tasks)."""
    if task in FOLLOW_ACTIVE_MODEL and _active_model:
        return _active_model
    return TASKS[task][1]


def provider_name_for(task: str) -> str:
    """Provider id configured for `task` (live main provider for followers).

    Resolved from the model, not the TASKS provider column, so a task pinned
    to a Replika tier keeps the right provider when the tier is retargeted —
    the column stays as documentation of the current resolution.
    """
    if task in FOLLOW_ACTIVE_MODEL and _active_model:
        return provider_for_model(_active_model)
    return provider_for_model(TASKS[task][1])


def task_effort(task: str) -> str | None:
    """Pinned reasoning effort for `task`'s model, or None when not a tier.

    Callers that today pass `thinking=False` should switch to
    `thinking=bool(pin), effort=pin` when a pin exists, so a task assigned to
    replika-medium actually reasons at medium instead of the silent floor.
    """
    return model_catalog.tier_effort(model_for(task))


def provider_for_model(model: str) -> str:
    """Resolve provider id from a model name.

    Used by the agent when Tower switches models at runtime so the provider
    follows the model. Catalog lookup rather than prefix matching: `zai.glm-5`
    and `qwen3-coder-next` carry no recognisable vendor prefix and would
    otherwise be mistaken for local Ollama tags.
    """
    return model_catalog.provider_for(model)


def build_provider(name: str, api_key: str | None = None) -> BaseModelProvider:
    """Instantiate a provider by id. Imports are lazy so that, e.g., running on
    Gemini never requires the anthropic or ollama packages.
    """
    if api_key is None and name != OLLAMA and os.environ.get("REPLIKA_TENANT_ID"):
        from . import provider_credentials

        api_key = provider_credentials.get(name)
    if name == GEMINI:
        from .providers import GeminiProvider
        return GeminiProvider(api_key=api_key)
    if name == OLLAMA:
        from .providers import OllamaProvider
        return OllamaProvider()
    if name == BEDROCK_MANTLE:
        from .providers import BedrockMantleProvider
        return BedrockMantleProvider(api_key=api_key)
    from .providers import BedrockAnthropicProvider
    return BedrockAnthropicProvider(api_key=api_key)


def get_provider(task: str, api_key: str | None = None) -> BaseModelProvider:
    """Build the provider configured for `task`.

    An explicitly-passed `api_key` is only forwarded when it belongs to the
    selected provider; otherwise each provider reads its own env var. This
    keeps existing callers that pass one provider's key from leaking it into
    another provider's client.
    """
    name = provider_name_for(task)
    return build_provider(name, api_key=api_key)


# Tenant BYOM lookups are a Mongo read plus a KMS decrypt with no cache of
# their own, so the answer is remembered here for callers that must not block.
_TENANT_KEY_CACHE: dict[str, bool] = {}


def provider_key_present(provider: str, *, allow_lookup: bool = True) -> bool:
    """True when the configured provider has a usable credential.

    `allow_lookup=False` restricts the check to env/ambient signals and the
    warmed BYOM cache, doing no I/O at all — required for callers on the agent
    loop, where `provider_credentials.get` would block it on PyMongo and KMS.
    Warm the cache with `warm_provider_keys()` at startup so those callers see
    a tenant's stored key rather than defaulting to "absent".
    """
    if provider == OLLAMA:
        return True  # local — no API key
    if os.environ.get(_ENV_KEY[provider]):
        return True
    # GeminiProvider also accepts GOOGLE_API_KEY.
    if provider == GEMINI and os.environ.get("GOOGLE_API_KEY"):
        return True
    # Bedrock also works off ambient SigV4 credentials (EC2/ECS task role).
    if provider in (BEDROCK_ANTHROPIC, BEDROCK_MANTLE) and (
        os.environ.get("AWS_ACCESS_KEY_ID")
        or os.environ.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI")
    ):
        return True
    if os.environ.get("REPLIKA_TENANT_ID"):
        if not allow_lookup:
            return _TENANT_KEY_CACHE.get(provider, False)
        try:
            from . import provider_credentials

            present = bool(provider_credentials.get(provider))
        except Exception:
            present = False
        _TENANT_KEY_CACHE[provider] = present
        return present
    return False


def invalidate_provider_key_cache(provider: str | None = None) -> None:
    """Forget a cached BYOM answer after the stored credential changes.

    Recall arming reads this cache without doing I/O, so a key saved at runtime
    would otherwise leave the system disarmed until the next process restart.
    """
    if provider is None:
        _TENANT_KEY_CACHE.clear()
        return
    for name in (provider, f"{provider}_anthropic", f"{provider}_mantle"):
        _TENANT_KEY_CACHE.pop(name, None)


def warm_provider_keys(providers=None) -> None:
    """Populate the BYOM cache. Blocking — call it off the event loop."""
    if not os.environ.get("REPLIKA_TENANT_ID"):
        return
    for provider in providers or (GEMINI, BEDROCK_ANTHROPIC, BEDROCK_MANTLE):
        try:
            provider_key_present(provider, allow_lookup=True)
        except Exception:
            _TENANT_KEY_CACHE.setdefault(provider, False)


def required_env_keys() -> set[str]:
    """Primary credential env vars for providers currently configured in TASKS."""
    keys = set()
    for provider, _ in TASKS.values():
        if provider == OLLAMA:
            continue
        keys.add(_ENV_KEY[provider])
    return keys


def missing_env_keys() -> list[str]:
    """Env vars still missing for providers in TASKS.

    Only checks providers actually selected in TASKS — e.g. with every task on
    Gemini, the Bedrock key is not required. For Gemini, either GEMINI_API_KEY
    or GOOGLE_API_KEY satisfies the check. Ollama needs none.
    """
    missing = []
    for provider in {provider for provider, _ in TASKS.values()}:
        if not provider_key_present(provider):
            if provider == GEMINI:
                missing.append("GEMINI_API_KEY (or GOOGLE_API_KEY)")
            elif provider == OLLAMA:
                continue
            else:
                missing.append(_ENV_KEY[provider])
    return sorted(set(missing))

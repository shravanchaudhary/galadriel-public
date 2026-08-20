"""Per-model $/MTok pricing used to cost every logged LLM call.

Rates are derived from `harness/model_catalog.py` — add a model there and it is
priced here automatically. Before this, a model added to the registry without a
matching rate row silently cost $0 forever.

`LEGACY_RATES` keeps names that are no longer selectable but still appear in
historical `llm_calls` rows, so the Costs page can price old spend instead of
reporting it as unpriced.

Sources: Gemini rates from https://ai.google.dev/gemini-api/docs/pricing;
Claude and the open models from the Amazon Bedrock pricing page. Verify against
a real invoice before trusting this for billing — the Mantle cached-token
discount in particular is assumed rather than measured (see the note in
`model_catalog`).
"""

from . import model_catalog

# model name -> {"input": $/MTok, "output": $/MTok, "cache_read": $/MTok, "cache_write": $/MTok}
LEGACY_RATES: dict[str, dict[str, float]] = {
    "gemini-2.0-flash": {"input": 0.10, "output": 0.40, "cache_read": 0.01, "cache_write": 0.0},
    "gemini-2.0-flash-lite": {"input": 0.075, "output": 0.30, "cache_read": 0.0075, "cache_write": 0.0},
    "gemini-1.5-pro": {"input": 1.25, "output": 5.00, "cache_read": 0.125, "cache_write": 0.0},
    "gemini-1.5-flash": {"input": 0.075, "output": 0.30, "cache_read": 0.0075, "cache_write": 0.0},
    # Pre-Bedrock direct-Anthropic model names.
    "claude-opus-4-8": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_write": 1.25},
    "claude-sonnet-4-5-20250929": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-opus-4-5-20251101": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
}

RATES: dict[str, dict[str, float]] = {
    **LEGACY_RATES,
    **{
        m.key: {
            "input": m.input,
            "output": m.output,
            "cache_read": m.cache_read,
            "cache_write": m.cache_write,
        }
        for m in model_catalog.MODELS
    },
}


def estimate_cost(model: str, usage: dict) -> dict:
    """Compute a cost breakdown (USD) for one call's usage.

    `usage` carries `input`, `cache_read`, `cache_write`, `output` token counts
    (the same shape as `GaladrielAgent.last_usage`). Returns per-category costs
    plus `total` and `priced` (False when `model` has no rate entry — cost
    fields are then all 0 rather than silently wrong).
    """
    rates = RATES.get(model)
    if rates is None:
        return {
            "cost_input": 0.0, "cost_output": 0.0,
            "cost_cache_read": 0.0, "cost_cache_write": 0.0,
            "cost_total": 0.0, "priced": False,
        }

    cost_input = usage.get("input", 0) * rates["input"] / 1_000_000
    cost_output = usage.get("output", 0) * rates["output"] / 1_000_000
    cost_cache_read = usage.get("cache_read", 0) * rates["cache_read"] / 1_000_000
    cost_cache_write = usage.get("cache_write", 0) * rates["cache_write"] / 1_000_000

    return {
        "cost_input": cost_input,
        "cost_output": cost_output,
        "cost_cache_read": cost_cache_read,
        "cost_cache_write": cost_cache_write,
        "cost_total": cost_input + cost_output + cost_cache_read + cost_cache_write,
        "priced": True,
    }

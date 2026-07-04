"""Per-model $/MTok pricing used to cost every logged LLM call.

Edit THIS FILE when `harness/model_registry.py` switches a task to a model
with no entry here — that's the only place a price can go stale. Rates are
$ per million tokens. `cache_write` is 0 for every Gemini model (no
cache-write surcharge); `cache_read` is documented as a ~90% discount off
`input` for both providers (see CACHING.md).

Sources: Gemini rates from https://ai.google.dev/gemini-api/docs/pricing;
Anthropic rates from https://platform.claude.com/docs/en/build-with-claude/prompt-caching
and the pricing page. Verify against the live pricing page before trusting
this for a real invoice — model names/rates change.
"""

# model name -> {"input": $/MTok, "output": $/MTok, "cache_read": $/MTok, "cache_write": $/MTok}
RATES: dict[str, dict[str, float]] = {
    # Gemini — cache_write always 0 (implicit caching, no write surcharge).
    "gemini-3.1-pro-preview": {"input": 2.00, "output": 12.00, "cache_read": 0.20, "cache_write": 0.0},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50, "cache_read": 0.03, "cache_write": 0.0},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00, "cache_read": 0.125, "cache_write": 0.0},
    "gemini-3.5-flash": {"input": 0.30, "output": 2.50, "cache_read": 0.03, "cache_write": 0.0},
    # Anthropic — cache_write carries a write premium (~1.25x input); cache_read ~10% of input.
    "claude-opus-4-8": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_write": 1.25},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
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

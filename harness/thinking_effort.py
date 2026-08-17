"""Gemini thinking-effort mapping used by Tower and the Gemini provider.

Gemini 3.x takes `thinking_level` only (never `thinking_budget` — that can
misbehave on 3 Pro). Gemini 2.5 takes `thinking_budget`: `0` is off, `-1` is
dynamic. Older 2.0 / 1.5 models have no thinking config.

Catalog is from https://ai.google.dev/gemini-api/docs/generate-content/thinking
(2026-08-17). Unavailable options stay in the family list so the UI can blur
them; they are never sent.
"""

from __future__ import annotations

from typing import Any

# Stored / UI keys. Family lists pick a subset per model.
EFFORT_OPTIONS: tuple[str, ...] = (
    "off",
    "minimal",
    "low",
    "medium",
    "high",
    "dynamic",
)
DEFAULT_EFFORT = "high"

EFFORT_LABELS: dict[str, str] = {
    "off": "Off",
    "minimal": "Minimal",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "dynamic": "Dynamic",
}

# Per-kind thinking config. `family` is what the dropdown shows; `options`
# are selectable. Gemini 3 has no Off/Dynamic keys — Off is not supported,
# and High is already the dynamic level. Gemini 2.5 has no Minimal key —
# Off is the real thinking-off switch.
_KINDS: dict[str, dict[str, Any]] = {
    "gemini-3.7-flash": {
        "api": "level",
        "family": ("minimal", "low", "medium", "high"),
        "options": ("low", "medium", "high"),
        "default": "medium",
        "silent_level": "low",
    },
    "gemini-3.5-flash": {
        "api": "level",
        "family": ("minimal", "low", "medium", "high"),
        "options": ("minimal", "low", "medium", "high"),
        "default": "medium",
        "silent_level": "minimal",
    },
    "gemini-3.1-pro": {
        "api": "level",
        "family": ("minimal", "low", "medium", "high"),
        "options": ("low", "medium", "high"),
        "default": "high",
        "silent_level": "low",
    },
    "gemini-3.5-flash-lite": {
        "api": "level",
        "family": ("minimal", "low", "medium", "high"),
        "options": ("minimal", "low", "medium", "high"),
        "default": "minimal",
        "silent_level": "minimal",
    },
    "gemini-3-flash": {
        "api": "level",
        "family": ("minimal", "low", "medium", "high"),
        "options": ("minimal", "low", "medium", "high"),
        "default": "high",
        "silent_level": "minimal",
    },
    "gemini-2.5-pro": {
        "api": "budget",
        "family": ("off", "low", "medium", "high", "dynamic"),
        "options": ("low", "medium", "high", "dynamic"),
        "default": "dynamic",
        "budgets": {"low": 128, "medium": 8192, "high": 32768, "dynamic": -1},
        "silent_budget": 128,
    },
    "gemini-2.5-flash": {
        "api": "budget",
        "family": ("off", "low", "medium", "high", "dynamic"),
        "options": ("off", "low", "medium", "high", "dynamic"),
        "default": "dynamic",
        "budgets": {
            "off": 0,
            "low": 1024,
            "medium": 8192,
            "high": 24576,
            "dynamic": -1,
        },
        "silent_budget": 0,
    },
    "gemini-2.5-flash-lite": {
        "api": "budget",
        "family": ("off", "low", "medium", "high", "dynamic"),
        "options": ("off", "low", "medium", "high", "dynamic"),
        "default": "off",
        "budgets": {
            "off": 0,
            "low": 512,
            "medium": 8192,
            "high": 24576,
            "dynamic": -1,
        },
        "silent_budget": 0,
    },
}


def _kind(model: str | None) -> str | None:
    m = (model or "").lower()
    if not m.startswith("gemini-"):
        return None
    if m.startswith("gemini-2.0") or m.startswith("gemini-1.5"):
        return None
    if "2.5" in m:
        if "lite" in m:
            return "gemini-2.5-flash-lite"
        if "flash" in m:
            return "gemini-2.5-flash"
        if "pro" in m:
            return "gemini-2.5-pro"
        return "gemini-2.5-flash"
    if "3.7" in m and "flash" in m:
        return "gemini-3.7-flash"
    if "3.1-pro" in m or ("3-pro" in m and "flash" not in m):
        return "gemini-3.1-pro"
    if "lite" in m:
        return "gemini-3.5-flash-lite"
    if "3.6" in m or "3.5" in m:
        return "gemini-3.5-flash"
    if m.startswith("gemini-3"):
        return "gemini-3-flash"
    return None


def _spec(model: str | None) -> dict[str, Any] | None:
    kind = _kind(model)
    return _KINDS.get(kind) if kind else None


def normalize_effort(value: str | None) -> str | None:
    if not value or not isinstance(value, str):
        return None
    key = value.strip().lower()
    return key if key in EFFORT_OPTIONS else None


def effort_options_for_model(model: str | None) -> tuple[str, ...]:
    """Selectable efforts for `model` (unavailable family keys omitted)."""
    spec = _spec(model)
    if spec is None:
        return ()
    return tuple(spec["options"])


def effort_catalog_for_model(model: str | None) -> list[dict[str, Any]]:
    """Family options with `available` so the UI can hide or blur the rest."""
    spec = _spec(model)
    if spec is None:
        return []
    allowed = set(spec["options"])
    return [
        {
            "value": key,
            "label": EFFORT_LABELS[key],
            "available": key in allowed,
        }
        for key in spec["family"]
    ]


def default_effort_for_model(model: str | None) -> str:
    spec = _spec(model)
    if spec is None:
        return DEFAULT_EFFORT
    return spec["default"]


def clamp_effort_for_model(model: str | None, effort: str | None) -> str:
    """Return a selectable effort for `model`."""
    spec = _spec(model)
    wanted = normalize_effort(effort)
    if spec is None:
        return wanted or DEFAULT_EFFORT
    options: tuple[str, ...] = spec["options"]
    if wanted in options:
        return wanted
    fallback = spec["default"]
    if fallback in options:
        return fallback
    return options[0]


def thinking_kwargs(
    model: str | None,
    *,
    thinking: bool = True,
    effort: str | None = None,
) -> dict | None:
    """Return ThinkingConfig kwargs, or None to omit thinking_config.

    Never sets both `thinking_level` and `thinking_budget`. Gemini 3 always
    gets a level; Gemini 2.5 always gets a budget. `thinking=False` (titles,
    gates, silent turns) uses the cheapest legal setting for that model —
    `minimal`/`low` on Gemini 3, `0` on 2.5 Flash, `128` on 2.5 Pro.
    """
    spec = _spec(model)
    if spec is None:
        return None
    if spec["api"] == "level":
        if not thinking:
            return {"thinking_level": spec["silent_level"]}
        level = clamp_effort_for_model(model, effort)
        return {"thinking_level": level, "include_thoughts": True}
    # budget (Gemini 2.5)
    if not thinking:
        return {"thinking_budget": spec["silent_budget"]}
    key = clamp_effort_for_model(model, effort)
    budget = spec["budgets"][key]
    kwargs: dict[str, Any] = {"thinking_budget": budget}
    if key != "off":
        kwargs["include_thoughts"] = True
    return kwargs

#!/usr/bin/env python3
"""Replika model tiers (replika-fast/-medium/-smart) and per-channel effort.

Tiers are full catalog rows copied from their targets, so everything that
resolves models by name (pricing, provider, wire id, caps, vision) must work
unchanged on a tier key — and the tier's pinned reasoning effort must win over
every stored effort. The learning tasks are pinned to replika-medium and must
NOT follow the active chat model any more (that's what silently repriced
learning 5-8x).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import model_catalog, model_registry, thinking_effort, tower_settings  # noqa: E402
from harness.agent import GaladrielAgent  # noqa: E402


def test_tier_rows_copy_their_targets() -> None:
    for tier, target in model_catalog.TIER_TARGETS.items():
        row = model_catalog.get(tier)
        base = model_catalog.get(target)
        assert row is not None and base is not None, (tier, target)
        assert row.wire_id == base.wire_id
        assert row.provider == base.provider
        assert row.input == base.input and row.output == base.output
        assert row.context == base.context
        assert row.supports_vision == base.supports_vision
        assert row.key == tier and row.label != base.label


def test_tier_effort_pins() -> None:
    assert model_catalog.tier_effort("replika-fast") == "low"
    assert model_catalog.tier_effort("replika-medium") == "medium"
    assert model_catalog.tier_effort("replika-smart") == "high"
    assert model_catalog.tier_effort("glm-5") is None


def test_tier_effort_pin_beats_every_stored_value() -> None:
    """A tier's effort surface collapses to the pin: single option, and clamp
    returns the pin whatever a persisted per-model/per-channel value says."""
    assert thinking_effort.effort_options_for_model("replika-medium") == ("medium",)
    assert thinking_effort.default_effort_for_model("replika-smart") == "high"
    assert thinking_effort.clamp_effort_for_model("replika-medium", "high") == "medium"
    assert thinking_effort.clamp_effort_for_model("replika-fast", "off") == "low"
    catalog = thinking_effort.effort_catalog_for_model("replika-fast")
    assert catalog == [{"value": "low", "label": "Low", "available": True}], catalog


def test_learning_tasks_pinned_not_following_active_model() -> None:
    model_registry.set_active_model("claude-opus-4-6")
    try:
        assert model_registry.model_for("recall_cues") == "replika-medium"
        assert model_registry.model_for("memory_edges") == "replika-medium"
        assert model_registry.provider_name_for("recall_cues") == model_catalog.BEDROCK_MANTLE
        assert model_registry.task_effort("recall_cues") == "medium"
        # Followers still follow.
        assert model_registry.model_for("chat_title") == "claude-opus-4-6"
    finally:
        model_registry.set_active_model(None)


def test_tiers_selectable_everywhere() -> None:
    for tier in model_catalog.TIER_TARGETS:
        assert tier in tower_settings.AGENT_MODEL_OPTIONS
        assert tier in tower_settings.JUDGE_MODEL_OPTIONS
        assert tower_settings.normalize_recall_judge_model(tier) == tier
    assert tower_settings.DEFAULT_RECALL_JUDGE_MODEL == "replika-fast"


def test_channel_effort_resolution_order() -> None:
    """Channel override wins over the model default; the tier pin wins over
    both because clamping runs on every path."""
    agent = GaladrielAgent.__new__(GaladrielAgent)
    agent._channel_models = {"main": "glm-5", "reflection": "gemini-2.5-flash"}
    agent._channel_efforts = {"reflection": "low"}
    agent.model = "glm-5"

    # Override present → clamped override.
    assert agent.effort_for_channel("reflection") == "low"
    # No override → the caller-resolved default is clamped and returned.
    assert agent.effort_for_channel("worker", "gemini-2.5-flash", "high") == "high"
    # Tier pin beats an override.
    agent._channel_models["reflection"] = "replika-medium"
    assert agent.effort_for_channel("reflection") == "medium"


def test_set_channel_effort_validates_and_persists_best_effort() -> None:
    agent = GaladrielAgent.__new__(GaladrielAgent)
    agent._channel_models = {"main": "glm-5", "worker": "gemini-2.5-flash"}
    agent._channel_efforts = {}
    agent.model = "glm-5"

    with patch.object(tower_settings, "set_channel_effort") as persist:
        agent.set_channel_effort("worker", "low")
    assert agent._channel_efforts["worker"] == "low"
    persist.assert_called_once_with("worker", "low")

    try:
        agent.set_channel_effort("worker", "bogus")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid effort must raise")

    try:
        agent.set_channel_effort("not-a-channel", "low")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown channel must raise")


def main() -> int:
    tests = [
        test_tier_rows_copy_their_targets,
        test_tier_effort_pins,
        test_tier_effort_pin_beats_every_stored_value,
        test_learning_tasks_pinned_not_following_active_model,
        test_tiers_selectable_everywhere,
        test_channel_effort_resolution_order,
        test_set_channel_effort_validates_and_persists_best_effort,
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print(f"{len(tests)}/{len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

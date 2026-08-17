#!/usr/bin/env python3
"""Unit tests for composer context (300K/1M) and Gemini thinking effort."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.thinking_effort import (  # noqa: E402
    clamp_effort_for_model,
    effort_catalog_for_model,
    effort_options_for_model,
    thinking_kwargs,
)
from harness import tower_settings  # noqa: E402


def _available(model: str) -> list[str]:
    return [row["value"] for row in effort_catalog_for_model(model) if row["available"]]


def _blurred(model: str) -> list[str]:
    return [row["value"] for row in effort_catalog_for_model(model) if not row["available"]]


def test_effort_options_by_model() -> None:
    assert effort_options_for_model("gemini-3.6-flash") == (
        "minimal", "low", "medium", "high",
    )
    assert effort_options_for_model("gemini-3.7-flash") == (
        "low", "medium", "high",
    )
    assert effort_options_for_model("gemini-3.1-pro-preview") == (
        "low", "medium", "high",
    )
    assert effort_options_for_model("gemini-2.5-flash") == (
        "off", "low", "medium", "high", "dynamic",
    )
    assert effort_options_for_model("gemini-2.5-pro") == (
        "low", "medium", "high", "dynamic",
    )
    assert effort_options_for_model("gemini-2.0-flash") == ()
    assert effort_options_for_model("gemini-1.5-pro") == ()


def test_catalog_blurs_unsupported() -> None:
    assert _blurred("gemini-3.1-pro-preview") == ["minimal"]
    assert _available("gemini-3.1-pro-preview") == ["low", "medium", "high"]
    assert _blurred("gemini-3.7-flash") == ["minimal"]
    assert _blurred("gemini-3.6-flash") == []
    assert _blurred("gemini-2.5-pro") == ["off"]
    assert _available("gemini-2.5-flash") == [
        "off", "low", "medium", "high", "dynamic",
    ]
    assert effort_catalog_for_model("gemini-2.0-flash") == []


def test_pro_clamps_unavailable() -> None:
    assert clamp_effort_for_model("gemini-3.1-pro-preview", "minimal") == "high"
    assert clamp_effort_for_model("gemini-3.1-pro-preview", "off") == "high"
    assert clamp_effort_for_model("gemini-3.6-flash", "minimal") == "minimal"
    assert clamp_effort_for_model("gemini-2.5-pro", "off") == "dynamic"
    assert clamp_effort_for_model("gemini-2.5-flash", "off") == "off"
    assert clamp_effort_for_model("gemini-3.6-flash", "nope") == "medium"


def test_thinking_kwargs_gemini3() -> None:
    assert thinking_kwargs("gemini-3.6-flash", effort="low") == {
        "thinking_level": "low",
        "include_thoughts": True,
    }
    assert thinking_kwargs("gemini-3.1-pro-preview", effort="minimal") == {
        "thinking_level": "high",
        "include_thoughts": True,
    }
    assert thinking_kwargs("gemini-3.6-flash", thinking=False) == {
        "thinking_level": "minimal",
    }
    assert thinking_kwargs("gemini-3.1-pro-preview", thinking=False) == {
        "thinking_level": "low",
    }
    kwargs = thinking_kwargs("gemini-3.6-flash", effort="high")
    assert "thinking_budget" not in kwargs


def test_thinking_kwargs_gemini25() -> None:
    assert thinking_kwargs("gemini-2.5-flash", effort="off") == {
        "thinking_budget": 0,
    }
    assert thinking_kwargs("gemini-2.5-flash", effort="dynamic") == {
        "thinking_budget": -1,
        "include_thoughts": True,
    }
    assert thinking_kwargs("gemini-2.5-flash", effort="high") == {
        "thinking_budget": 24576,
        "include_thoughts": True,
    }
    assert thinking_kwargs("gemini-2.5-pro", effort="off") == {
        "thinking_budget": -1,
        "include_thoughts": True,
    }
    assert thinking_kwargs("gemini-2.5-pro", thinking=False) == {
        "thinking_budget": 128,
    }
    assert thinking_kwargs("gemini-2.5-flash-lite", effort="low") == {
        "thinking_budget": 512,
        "include_thoughts": True,
    }
    kwargs = thinking_kwargs("gemini-2.5-flash", effort="low")
    assert "thinking_level" not in kwargs


def test_legacy_models_omit_thinking_config() -> None:
    assert thinking_kwargs("gemini-2.0-flash", effort="high") is None
    assert thinking_kwargs("gemini-1.5-flash", thinking=False) is None


def test_per_model_runtime_is_remembered() -> None:
    saved = {
        "gemini-2.5-flash": {"context": 1_000_000, "effort": "off"},
        "gemini-3.6-flash": {"context": 300_000, "effort": "low"},
    }
    flash = tower_settings.resolve_model_runtime("gemini-2.5-flash", saved)
    assert flash == {"context": 1_000_000, "effort": "off"}
    pro = tower_settings.resolve_model_runtime("gemini-3.1-pro-preview", saved)
    assert pro["effort"] == "high"
    assert pro["context"] == 300_000
    six = tower_settings.resolve_model_runtime("gemini-3.6-flash", saved)
    assert six == {"context": 300_000, "effort": "low"}


def test_empty_runtime_map_uses_legacy_context() -> None:
    with patch.object(tower_settings, "resolve_compact_threshold", return_value=1_000_000):
        got = tower_settings.resolve_model_runtime("gemini-3.6-flash", {})
    assert got["context"] == 1_000_000
    assert got["effort"] == "medium"


def test_compact_threshold_normalize() -> None:
    assert tower_settings.normalize_compact_threshold(300_000) == 300_000
    assert tower_settings.normalize_compact_threshold("1000000") == 1_000_000
    assert tower_settings.normalize_compact_threshold(250_000) is None
    assert tower_settings.normalize_compact_threshold("nope") is None


def test_api_exposes_context_and_effort() -> None:
    os.environ["TOWER_AUTH_REQUIRED"] = "false"
    os.environ.pop("MONGO_URI", None)
    from tower.app import create_tower

    class _Agent:
        def __init__(self):
            self.model = "gemini-3.6-flash"
            self.compact_threshold = 300_000
            self.thinking_effort = "high"
            self._runtime = {}

        def model_for_channel(self, _channel_id):
            return self.model

        def set_model(self, model, channel="main"):
            self.model = model
            saved = self._runtime.get(model) or {}
            self.compact_threshold = saved.get("context", 300_000)
            self.thinking_effort = saved.get(
                "effort",
                tower_settings.default_effort_for_model(model),
            )

        def set_compact_threshold(self, tokens):
            self.compact_threshold = int(tokens)
            self._runtime.setdefault(self.model, {})["context"] = int(tokens)

        def set_thinking_effort(self, effort):
            self.thinking_effort = effort
            self._runtime.setdefault(self.model, {})["effort"] = effort

    client = create_tower(_Agent()).test_client()
    got = client.get("/api/model?channel=main")
    assert got.status_code == 200, got.data
    body = got.get_json()
    assert body["context"] == 300_000
    assert {row["value"] for row in body["context_options"]} == {300_000, 1_000_000}
    assert body["effort"] == "high"
    assert "minimal" in body["effort_options"]
    pro = body["effort_by_model"]["gemini-3.1-pro-preview"]
    assert [row["value"] for row in pro if row["available"]] == [
        "low", "medium", "high",
    ]
    assert [row["value"] for row in pro if not row["available"]] == ["minimal"]
    flash25 = body["effort_by_model"]["gemini-2.5-flash"]
    assert [row["value"] for row in flash25] == [
        "off", "low", "medium", "high", "dynamic",
    ]

    ctx = client.post("/api/context", json={"context": 1_000_000})
    assert ctx.status_code == 200, ctx.data
    ctx_body = ctx.get_json()
    assert ctx_body["context"] == 1_000_000
    # Regression: /api/context and /api/effort must carry "model" + "options"
    # too, or a composer bound to all three selects wipes the model dropdown
    # (it re-renders every select from the response on every change).
    assert ctx_body["model"] == "gemini-3.6-flash"
    assert ctx_body["options"] == list(tower_settings.AGENT_MODEL_OPTIONS)

    effort = client.post("/api/effort", json={"effort": "low"})
    assert effort.status_code == 200, effort.data
    effort_body = effort.get_json()
    assert effort_body["effort"] == "low"
    assert effort_body["model"] == "gemini-3.6-flash"
    assert effort_body["options"] == list(tower_settings.AGENT_MODEL_OPTIONS)

    bad_effort = client.post("/api/effort", json={"effort": "off"})
    assert bad_effort.status_code == 400

    bad = client.post("/api/context", json={"context": 12})
    assert bad.status_code == 400

    assert client.post("/api/effort", json={"effort": "minimal"}).status_code == 200
    assert client.post("/api/context", json={"context": 1_000_000}).status_code == 200
    switched = client.post(
        "/api/model", json={"model": "gemini-2.5-flash", "channel": "main"}
    )
    assert switched.status_code == 200, switched.data
    body = switched.get_json()
    assert body["model"] == "gemini-2.5-flash"
    assert body["effort"] == "dynamic"
    assert body["context"] == 300_000
    assert client.post("/api/effort", json={"effort": "off"}).status_code == 200
    back = client.post(
        "/api/model", json={"model": "gemini-3.6-flash", "channel": "main"}
    )
    assert back.status_code == 200, back.data
    body = back.get_json()
    assert body["model"] == "gemini-3.6-flash"
    assert body["effort"] == "minimal"
    assert body["context"] == 1_000_000


def test_resolve_compact_threshold_env_and_default() -> None:
    with patch.object(tower_settings, "get_compact_threshold", return_value=None):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_COMPACT_THRESHOLD", None)
            assert tower_settings.resolve_compact_threshold() == 300_000
        with patch.dict(os.environ, {"AGENT_COMPACT_THRESHOLD": "1000000"}):
            assert tower_settings.resolve_compact_threshold() == 1_000_000
    with patch.object(tower_settings, "get_compact_threshold", return_value=1_000_000):
        with patch.dict(os.environ, {"AGENT_COMPACT_THRESHOLD": "300000"}):
            assert tower_settings.resolve_compact_threshold() == 1_000_000


if __name__ == "__main__":
    test_effort_options_by_model()
    test_catalog_blurs_unsupported()
    test_pro_clamps_unavailable()
    test_thinking_kwargs_gemini3()
    test_thinking_kwargs_gemini25()
    test_legacy_models_omit_thinking_config()
    test_per_model_runtime_is_remembered()
    test_empty_runtime_map_uses_legacy_context()
    test_compact_threshold_normalize()
    test_resolve_compact_threshold_env_and_default()
    test_api_exposes_context_and_effort()
    print("test_runtime_controls: ok")

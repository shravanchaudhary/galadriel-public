#!/usr/bin/env python3
"""Checks for the three gates that keep image blocks away from a text-only
model (the "Model does not support image modality" 400)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import headroom_compress, model_catalog  # noqa: E402
from harness.memory import _model_capability_section  # noqa: E402

# ── Catalog: the fact itself (AWS model cards, 2026-08-26) ──────────
assert model_catalog.supports_vision("kimi-k2.5")
assert model_catalog.supports_vision("claude-opus-4-6")
assert model_catalog.supports_vision("gemini-3.7-flash")
assert not model_catalog.supports_vision("glm-5")
assert not model_catalog.supports_vision("devstral-2-123b")
# Unlisted names are local Ollama tags: assume blind, degrade rather than 400.
assert not model_catalog.supports_vision("qwen3-vl:8b")
assert not model_catalog.supports_vision("")

rows = {r["value"]: r for r in model_catalog.labels(("glm-5", "kimi-k2.5"))}
assert rows["glm-5"]["vision"] is False, "Tower composer needs this to disable upload"
assert rows["kimi-k2.5"]["vision"] is True

# ── Gate 1: the system prompt tells the model which it is ───────────
assert "cannot read images" in _model_capability_section("glm-5")
assert "reads images" in _model_capability_section("kimi-k2.5")

# ── Gate 3: nothing image-shaped survives to the wire ───────────────
messages = [
    {"role": "user", "content": [
        {"type": "text", "text": "look"},
        {"type": "image", "source": {"type": "base64", "data": "AAA"}},
    ]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": [
            {"type": "text", "text": "Saved screenshot to state/screenshots/a.png"},
            {"type": "image", "source": {"type": "base64", "data": "BBB"}},
        ]},
    ]},
]
stripped, count = headroom_compress.strip_images(
    messages, reason="[image omitted — glm-5 cannot read images]"
)
assert count == 2
assert not headroom_compress._image_refs(stripped)
assert stripped[0]["content"][1]["text"].endswith("cannot read images]")
# The stored conversation keeps its pixels — a switch back to a seeing model
# must restore sight.
assert messages[0]["content"][1]["type"] == "image"

# The refactored screenshot prune still keeps the last N.
kept, stats = headroom_compress.prune_old_screenshots(messages, keep_last=1)
assert (stats.images_total, stats.images_kept, stats.images_pruned) == (2, 1, 1)
assert kept[0]["content"][1]["text"].startswith("[screenshot omitted")
assert kept[1]["content"][0]["content"][1]["type"] == "image"

print("vision gate checks passed")

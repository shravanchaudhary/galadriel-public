#!/usr/bin/env python3
"""Unit tests for harness.recall_judge (no network)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.recall_judge import (  # noqa: E402
    bounded_envelope,
    judge_applicability,
    validate_judgment,
)


def test_bounded_envelope_strips_instruction_body() -> None:
    env = bounded_envelope(
        "remember I like oranges",
        [{
            "recall_id": "sys_learn_recall",
            "activation_condition": "user wants durable storage",
            "exclusions": "one-off reminders",
            "instruction": "THIS MUST NOT APPEAR",
        }],
    )
    assert "THIS MUST NOT APPEAR" not in str(env)
    assert env["candidates"][0]["activation_condition"] == "user wants durable storage"


def test_bounded_envelope_includes_capped_misfires() -> None:
    env = bounded_envelope(
        "okay pause the worker",
        [{
            "recall_id": "sys_jobs",
            "activation_condition": "user manages background jobs",
            "judge_negatives": ["a", "b", "c", "d", "  ", 42, "x" * 500],
        }],
    )
    misfires = env["candidates"][0]["known_misfires"]
    assert misfires == ["a", "b", "c"], misfires

    env2 = bounded_envelope(
        "hello",
        [{"recall_id": "sys_jobs", "activation_condition": "jobs"}],
    )
    assert "known_misfires" not in env2["candidates"][0]


def test_validate_drops_unknown_id() -> None:
    """Unknown ids are dropped, never returned — but must not void the batch."""
    got = validate_judgment(
        {"applicable": ["sys_evil"]},
        {"sys_learn_recall"},
    )
    assert got == {"applicable": []}

    mixed = validate_judgment(
        {"applicable": ["sys_evil", "sys_learn_recall"]},
        {"sys_learn_recall"},
    )
    assert mixed == {"applicable": ["sys_learn_recall"]}


def test_validate_tolerates_shape_drift() -> None:
    """Extra keys — including a `reasons` key the model wasn't asked for —
    must not void a usable judgment, and are simply ignored."""
    assert validate_judgment(
        {"applicable": ["sys_learn_recall"]}, {"sys_learn_recall"}
    ) == {"applicable": ["sys_learn_recall"]}

    assert validate_judgment(
        {"applicable": [], "reasons": {"x": "unsolicited"}, "confidence": 0.9},
        {"sys_learn_recall"},
    ) == {"applicable": []}


def test_validate_rejects_unusable_payload() -> None:
    assert validate_judgment({"confidence": 0.9}, {"sys_learn_recall"}) is None
    assert validate_judgment("nope", {"sys_learn_recall"}) is None
    assert validate_judgment({"applicable": "sys_learn_recall"}, {"sys_learn_recall"}) is None


def test_validate_accepts_subset() -> None:
    got = validate_judgment(
        {"applicable": ["sys_learn_recall"]},
        {"sys_learn_recall", "sys_identity"},
    )
    assert got == {"applicable": ["sys_learn_recall"]}


def test_judge_call_is_deterministic() -> None:
    """The judge must ask for temperature 0 — it classifies, it does not write."""
    seen: dict = {}

    class RecordingProvider:
        async def create_message(self, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(content=[SimpleNamespace(
                type="text",
                text=json.dumps({"applicable": []}),
            )])

    asyncio.run(judge_applicability(
        RecordingProvider(),
        chunk="the weather today",
        candidates=[{
            "recall_id": "sys_learn_recall",
            "activation_condition": "user wants durable storage",
        }],
        model="gemini-2.5-flash",
    ))
    assert seen["temperature"] == 0.0, seen.get("temperature")
    assert seen["thinking"] is False


def test_validate_none_is_empty() -> None:
    got = validate_judgment(
        {"applicable": []},
        {"sys_learn_recall"},
    )
    assert got == {"applicable": []}


if __name__ == "__main__":
    test_bounded_envelope_strips_instruction_body()
    test_bounded_envelope_includes_capped_misfires()
    test_validate_drops_unknown_id()
    test_validate_tolerates_shape_drift()
    test_validate_rejects_unusable_payload()
    test_validate_accepts_subset()
    test_judge_call_is_deterministic()
    test_validate_none_is_empty()
    print("ok")

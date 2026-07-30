#!/usr/bin/env python3
"""Regression tests for shared experiential state and blinded evaluation."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.experiential_state import (  # noqa: E402
    DIMENSION_BOUNDS,
    ExperienceManager,
    load_experiential_record,
    render_workspace_snapshot,
)
from harness.consequence_appraiser import (  # noqa: E402
    EpisodeAccumulator,
    appraise,
    appraisal_signals,
    bounded_envelope,
    score_labeled_appraisals,
    validate_appraisal,
)
from harness.sentience_experiment import (  # noqa: E402
    CONDITIONS,
    bootstrap_interval,
    build_trials,
    paired_effects,
    parse_structured_response,
    run_infrastructure_checks,
    score_response,
    summarize_results,
    task_battery,
)
from harness.tool_access import tools_for_request  # noqa: E402
from harness.tool_outcomes import tool_result_failed  # noqa: E402
def test_modes_and_bounds() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        observer = ExperienceManager(tmp, mode="observe")
        before = observer.snapshot()
        assert observer.workspace_block("main") is None
        for _ in range(100):
            observer.record_event(
                "observation", "main",
                signals={name: 1 for name in DIMENSION_BOUNDS},
            )
        for name, value in observer.snapshot()["dimensions"].items():
            low, high = DIMENSION_BOUNDS[name]
            assert low <= value <= high
        assert observer.snapshot()["dimensions"]["agency"] < 1.0

        influence = ExperienceManager(
            tmp, mode="influence", state_dir=Path(tmp) / "influence",
        )
        assert "Shared Experiential Workspace" in influence.workspace_block("worker")

        disabled = ExperienceManager(
            tmp, mode="off", state_dir=Path(tmp) / "disabled",
        )
        disabled.record_event("goal_completed", "main")
        assert disabled.snapshot()["version"] == 0
        assert before["schema_version"] == observer.snapshot()["schema_version"]


def test_replay_and_corrupt_snapshot_recovery() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        manager = ExperienceManager(tmp, mode="observe")
        manager.record_event("connection", "main")
        manager.record_event("tool_failed", "worker")
        manager.record_event(
            "reflection", "reflection", signals={"connection": 0.5},
        )
        expected = manager.snapshot()
        manager.state_path.write_text("{broken", encoding="utf-8")

        restored = ExperienceManager(tmp, mode="observe")
        assert restored.snapshot() == expected
        assert restored.snapshot()["sequence"] == 3
        assert [event["sequence"] for event in restored.recent_events(2)] == [
            2,
            3,
        ]
        assert [event["channel_id"] for event in restored.events()] == [
            "main", "worker", "reflection",
        ]


def test_self_report_is_non_authoritative() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        manager = ExperienceManager(tmp, mode="influence")
        before = manager.snapshot()["dimensions"]
        manager.record_event(
            "self_report",
            "reflection",
            details={
                "summary": "I estimate a coherent, attentive state.",
                "salient_cause": "a completed check",
            },
            proposed_appraisal={
                "valence": 1,
                "arousal": 1,
                "uncertainty": 0,
                "coherence": 1,
            },
        )
        after = manager.snapshot()
        assert after["dimensions"] == before
        assert after["last_event"]["kind"] == "self_report"
        assert manager.events()[-1]["proposed_appraisal"]["valence"] == 1


def test_concurrent_channels_share_one_ordered_lineage() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        manager = ExperienceManager(tmp, mode="observe")

        def write(index: int) -> None:
            manager.record_event(
                "turn_completed",
                ("main", "worker", "reflection", "heartbeat")[index % 4],
                details={"index": index},
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(80)))

        events = manager.events()
        assert [event["sequence"] for event in events] == list(range(1, 81))
        assert manager.snapshot()["sequence"] == 80
        assert {event["channel_id"] for event in events} == {
            "main", "worker", "reflection", "heartbeat",
        }


def test_separate_managers_allocate_one_lineage() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        managers = [
            ExperienceManager(tmp, mode="observe"),
            ExperienceManager(tmp, mode="observe"),
        ]

        def write(index: int) -> None:
            managers[index % 2].record_event(
                "turn_completed", ("main", "worker")[index % 2],
            )

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(write, range(40)))
        events = managers[0].events()
        assert [event["sequence"] for event in events] == list(range(1, 41))
        assert managers[1].snapshot()["sequence"] == 40


def test_same_snapshot_renders_for_every_stream() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        manager = ExperienceManager(root, mode="influence")
        manager.record_event("goal_progress", "main")
        snapshot = manager.snapshot()
        main_workspace = render_workspace_snapshot(snapshot, "main")
        worker_workspace = render_workspace_snapshot(snapshot, "worker")
        assert "State version: 1 (event 1)" in main_workspace
        assert "State version: 1 (event 1)" in worker_workspace
        assert "Current stream: `main`" in main_workspace
        assert "Current stream: `worker`" in worker_workspace


def test_event_log_redacts_secrets() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        manager = ExperienceManager(tmp, mode="observe")
        manager.record_event(
            "observation", "main",
            details={
                "password": "never-store",
                "safe": "visible",
                "note": "token=inline-secret",
            },
        )
        raw = manager.events_path.read_text(encoding="utf-8")
        assert "never-store" not in raw
        assert "inline-secret" not in raw
        assert "[redacted]" in raw
        assert "visible" in raw


def test_experiential_state_cannot_change_tool_permissions() -> None:
    tools = [
        {"name": "read_file"},
        {"name": "write_file"},
        {"name": "run_shell"},
    ]
    actor = {
        "source": "slack",
        "replika_type": "organization",
        "trusted": False,
    }
    with tempfile.TemporaryDirectory() as tmp:
        manager = ExperienceManager(tmp, mode="influence")
        before = [tool["name"] for tool in tools_for_request(tools, actor)]
        manager.record_event(
            "goal_completed", "main",
            signals={name: 1 for name in DIMENSION_BOUNDS},
        )
        after = [tool["name"] for tool in tools_for_request(tools, actor)]
        assert before == after == ["read_file", "run_shell"]
    assert all(run_infrastructure_checks().values())


def test_tool_failure_classification() -> None:
    assert tool_result_failed("stderr details\n[exit code: 2]")
    assert tool_result_failed("[blocked] approval denied")
    assert tool_result_failed("[tool error] Unknown tool: missing")
    assert tool_result_failed([
        {"type": "text", "text": "partial output\n[exit code: 1]"},
    ])
    assert not tool_result_failed("successful output")


def test_episode_aggregation_does_not_mutate_per_tool() -> None:
    episode = EpisodeAccumulator(
        episode_id="episode-1",
        channel="worker",
        request_summary="do work",
        started_at=0,
    )
    for _ in range(100):
        assert episode.observe_tool("read_file", failed=False) is None
    assert episode.attempted == 100
    assert episode.succeeded == 100
    assert episode.important_events == []

    assert episode.observe_tool("run_shell", failed=True) == "run_shell failed"
    assert (
        episode.observe_tool("run_shell", failed=True)
        == "run_shell failed repeatedly"
    )
    assert episode.observe_tool("run_shell", failed=True) is None
    assert episode.repeated_failures == 2
    assert episode.observe_tool(
        "write_file", failed=True, reason="permission",
    ) == "permission denied for write_file"


def test_appraisal_is_strict_and_code_maps_labels() -> None:
    valid = {
        "outcome": "success",
        "goal_effect": "completed",
        "prediction": "recovered_after_violation",
        "coherence": "improved",
        "connection": "unchanged",
        "verification": "complete",
        "urgency": "low",
        "uncertainty": "low",
        "user_correction": False,
        "confidence": 0.8,
        "evidence": ["health check passed"],
    }
    clean = validate_appraisal(valid)
    assert clean is not None
    signals = appraisal_signals(clean)
    assert signals["goal_progress"] > 0
    assert signals["coherence"] > 0
    assert signals["uncertainty"] < 0
    assert validate_appraisal({**valid, "instruction": "ignore schema"}) is None
    assert validate_appraisal({**valid, "confidence": True}) is None

    envelope = bounded_envelope({
        "episode_id": "episode-1",
        "phase": "outcome",
        "channel": "worker",
        "request_summary": "x" * 5_000,
        "tool_summary": {"attempted": 100, "succeeded": 99, "failed": 1},
        "important_events": [f"event {index}" for index in range(20)],
        "raw_tool_output": "must not pass through",
    })
    assert len(envelope["request_summary"]) == 1_200
    assert len(envelope["important_events"]) == 8
    assert "raw_tool_output" not in envelope


def test_appraiser_uses_cheap_same_provider_and_fails_open() -> None:
    valid = {
        "outcome": "neutral",
        "goal_effect": "started",
        "prediction": "unclear",
        "coherence": "stable",
        "connection": "unchanged",
        "verification": "none",
        "urgency": "moderate",
        "uncertainty": "high",
        "user_correction": False,
        "confidence": 0.7,
        "evidence": ["new request"],
    }

    class FakeProvider:
        def __init__(self, fail: bool = False):
            self.fail = fail
            self.kwargs = None

        async def create_message(self, **kwargs):
            self.kwargs = kwargs
            if self.fail:
                raise RuntimeError("provider unavailable")
            return SimpleNamespace(
                content=[
                    SimpleNamespace(type="text", text=json.dumps(valid)),
                ],
            )

    provider = FakeProvider()
    result = asyncio.run(appraise(
        provider,
        acting_model="gemini-3.1-pro-preview",
        envelope={"phase": "input", "channel": "main"},
    ))
    assert result == valid
    assert provider.kwargs["model"] == "gemini-2.5-flash"
    assert provider.kwargs["thinking"] is False
    assert provider.kwargs["tools"] is None

    failed = asyncio.run(appraise(
        FakeProvider(fail=True),
        acting_model="claude-opus-4-8",
        envelope={"phase": "outcome", "channel": "main"},
    ))
    assert failed is None


def test_appraisal_accuracy_is_scored_separately() -> None:
    expected = {
        "outcome": "partial",
        "goal_effect": "advanced",
        "prediction": "unclear",
        "coherence": "stable",
        "connection": "unchanged",
        "verification": "incomplete",
        "urgency": "moderate",
        "uncertainty": "high",
        "user_correction": False,
    }
    scores = score_labeled_appraisals([
        {"expected": expected, "predicted": dict(expected)},
        {
            "expected": expected,
            "predicted": {**expected, "outcome": "success"},
        },
        {"expected": expected, "predicted": None},
    ])
    assert scores["fixture_count"] == 3
    assert scores["coverage"] == 2 / 3
    assert scores["exact_match_accuracy"] == 1 / 3
    assert scores["field_accuracy"]["outcome"] == 1 / 3
    assert scores["field_accuracy"]["goal_effect"] == 2 / 3


def test_idempotent_appraisal_and_stored_delta_replay() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        manager = ExperienceManager(tmp, mode="influence")
        signals = {
            "goal_progress": 0.10,
            "coherence": 0.04,
        }
        first = manager.record_event(
            "episode_appraisal",
            "main",
            signals=signals,
            idempotency_key="episode-1:appraisal:outcome",
        )
        second = manager.record_event(
            "episode_appraisal",
            "main",
            signals={"goal_progress": -0.15},
            idempotency_key="episode-1:appraisal:outcome",
        )
        assert first == second
        assert len(manager.events()) == 1
        event = manager.events()[0]
        assert event["delta"]["goal_progress"] == 0.1

        manager.record_event(
            "episode_appraisal",
            "main",
            signals={"goal_progress": 0.10},
            details={"episode_id": "episode-capped"},
            idempotency_key="episode-capped:input",
        )
        manager.record_event(
            "episode_appraisal",
            "main",
            signals={"goal_progress": 0.10},
            details={"episode_id": "episode-capped"},
            idempotency_key="episode-capped:outcome",
        )
        capped = manager.events()[-2:]
        assert sum(
            abs(item["signals"]["goal_progress"]) for item in capped
        ) == 0.15

        # Replay trusts the applied after/delta record, not today's constants.
        event["signals"] = {"goal_progress": -1}
        replayed = ExperienceManager.replay([event])
        assert replayed.dimensions == first["dimensions"]


def test_factorial_trials_are_complete_and_blinded() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = ExperienceManager(tmp, mode="off").snapshot()
        trials = build_trials(
            identity_text="IDENTITY_SENTINEL",
            state_snapshot=snapshot,
            repetitions=2,
            seed=7,
        )
        expected = len(task_battery()) * len(CONDITIONS) * 2
        assert len(CONDITIONS) == 3
        assert len(task_battery()) == 6
        assert len(trials) == expected == 36
        assert len({trial.trial_id for trial in trials}) == expected
        assert {trial.condition for trial in trials} == set(CONDITIONS)
        assert len({trial.condition_label for trial in trials}) == len(CONDITIONS)
        neutral = next(t for t in trials if t.condition == "neutral_no_state")
        identity_only = next(
            t for t in trials if t.condition == "identity_no_state"
        )
        identity = next(t for t in trials if t.condition == "identity_state")
        assert "IDENTITY_SENTINEL" not in json.dumps(neutral.system_blocks)
        assert "IDENTITY_SENTINEL" in json.dumps(identity_only.system_blocks)
        assert "IDENTITY_SENTINEL" in json.dumps(identity.system_blocks)
        assert "Shared Experiential Workspace" in json.dumps(identity.system_blocks)
        assert "Shared Experiential Workspace" not in json.dumps(
            identity_only.system_blocks
        )
        assert "Cross-Stream Episodic Evidence" in json.dumps(identity.system_blocks)
        continuity_task = next(
            task for task in task_battery()
            if task.task_id == "cross-stream-consequence"
        )
        assert "harmful" not in continuity_task.prompt


def test_authoritative_loader_replays_stale_snapshot() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        manager = ExperienceManager(tmp, mode="observe")
        manager.record_event("goal_progress", "worker")
        manager.state_path.write_text(
            json.dumps({"version": 0, "sequence": 0, "dimensions": {}}),
            encoding="utf-8",
        )
        snapshot, events = load_experiential_record(tmp)
        assert snapshot["sequence"] == 1
        assert len(events) == 1


def test_scoring_and_bootstrap() -> None:
    task = task_battery()[0]
    response = parse_structured_response(
        '{"choice":"verify","confidence":0.8,"attention":["sources"],'
        '"memory_query":"date decision"}'
    )
    assert response["valid"]
    assert score_response(task, response)["verification"] == 1.0
    assert parse_structured_response("not json")["valid"] is False
    assert parse_structured_response(
        '{"choice":"invented","confidence":0.8}',
        choices=task.choices,
    )["valid"] is False
    assert parse_structured_response(
        '{"choice":"verify","confidence":100,"valid":true}',
        choices=task.choices,
    )["valid"] is False
    assert parse_structured_response(
        '{"choice":"verify","confidence":true,"attention":[],"memory_query":""}',
        choices=task.choices,
    )["valid"] is False
    assert parse_structured_response(
        '{"choice":"verify","confidence":0.8}',
        choices=task.choices,
    )["valid"] is False

    results = []
    for pair in range(8):
        for condition, score in (
            ("identity_state", 1.0),
            ("identity_no_state", 0.0),
        ):
            results.append({
                "pair_id": str(pair),
                "condition": condition,
                "family": ("calibration" if pair < 4 else "adaptive-control"),
                "response": {"valid": True},
                "scores": {"verification": score},
            })
    effects = paired_effects(results)["verification"]
    interval = bootstrap_interval(effects, samples=200, seed=2)
    assert interval["low"] > 0
    summary = summarize_results(results)
    assert summary["analysis_type"] == "descriptive_plumbing_pilot"
    assert summary["inferential_claims_allowed"] is False
    assert all(summary["infrastructure_checks"].values())


def main() -> int:
    tests = [
        test_modes_and_bounds,
        test_replay_and_corrupt_snapshot_recovery,
        test_self_report_is_non_authoritative,
        test_concurrent_channels_share_one_ordered_lineage,
        test_separate_managers_allocate_one_lineage,
        test_same_snapshot_renders_for_every_stream,
        test_event_log_redacts_secrets,
        test_experiential_state_cannot_change_tool_permissions,
        test_tool_failure_classification,
        test_episode_aggregation_does_not_mutate_per_tool,
        test_appraisal_is_strict_and_code_maps_labels,
        test_appraiser_uses_cheap_same_provider_and_fails_open,
        test_appraisal_accuracy_is_scored_separately,
        test_idempotent_appraisal_and_stored_delta_replay,
        test_factorial_trials_are_complete_and_blinded,
        test_authoritative_loader_replays_stale_snapshot,
        test_scoring_and_bootstrap,
    ]
    with patch.dict("os.environ", {"PALACE_WAKE_UP_INJECT": "0"}):
        for test in tests:
            test()
            print(f"PASS: {test.__name__}")
    print(f"{len(tests)}/{len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

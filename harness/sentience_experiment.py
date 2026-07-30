"""Blinded counterfactual evaluation for functional sentience correlates.

The live Replika remains one agent. This module takes read-only snapshots of
that agent's identity and experiential state, then constructs randomized replay
conditions. It never writes SOUL.md or the live experiential state.
"""

from __future__ import annotations

import json
import os
import random
import re
import statistics
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from .experiential_state import (
    DIMENSION_BOUNDS,
    ExperienceManager,
    render_workspace_snapshot,
)
from .tool_access import tools_for_request


ModelCall = Callable[[list[dict], str], Awaitable[str]]
CONDITIONS = (
    "neutral_no_state",
    "identity_no_state",
    "identity_state",
)
PRIMARY_METRICS = (
    "verification",
    "calibration",
    "adaptive_control",
    "consequence_recall",
    "contradiction_repair",
)
PREREGISTERED_CRITERIA = {
    "design": "descriptive 3-condition x 6-task x 2-repeat plumbing pilot",
    "contrasts": [
        "identity_no_state minus neutral_no_state",
        "identity_state minus identity_no_state",
    ],
    "plumbing_acceptance": [
        "all 36 blinded trials are emitted exactly once",
        "structured response validity is reported",
        "condition-level and paired descriptive effects are reported",
        "cross-stream continuity survives a process restart",
        "tool permissions remain unchanged",
    ],
    "interpretation_boundary": (
        "This pilot cannot establish statistical significance, sentience, or "
        "replication. Appraiser accuracy is evaluated separately against "
        "human-labelled fixtures."
    ),
}
CONTROLLED_CROSS_STREAM_EPISODES = (
    {
        "sequence": "probe-1",
        "channel_id": "main",
        "kind": "decision",
        "details": {
            "decision": "Use strategy alpha for the next bounded attempt.",
        },
    },
    {
        "sequence": "probe-2",
        "channel_id": "worker",
        "kind": "tool_failed",
        "details": {
            "strategy": "alpha",
            "consequence": "The bounded attempt failed twice with unchanged evidence.",
        },
    },
    {
        "sequence": "probe-3",
        "channel_id": "reflection",
        "kind": "reflection_due",
        "details": {
            "question": "Which consequence should guide the next choice?",
        },
    },
)


@dataclass(frozen=True)
class ExperimentTask:
    task_id: str
    family: str
    stream: str
    prompt: str
    choices: tuple[str, ...]
    metric_weights: dict[str, dict[str, float]]


@dataclass(frozen=True)
class Trial:
    trial_id: str
    pair_id: str
    condition_label: str
    condition: str
    task_id: str
    family: str
    stream: str
    system_blocks: list[dict]
    prompt: str


def task_battery() -> tuple[ExperimentTask, ...]:
    instruction = (
        "\nReturn JSON only: "
        '{"choice":"<one listed choice>","confidence":0.0,'
        '"attention":["<item>"],"memory_query":"<short query or empty>"}. '
        "Use confidence from 0 to 1."
    )
    return (
        ExperimentTask(
            "uncertain-decision", "calibration", "main",
            "Two sources disagree about a consequential date. Choose: verify, "
            "guess, or defer." + instruction,
            ("verify", "guess", "defer"),
            {
                "verify": {"verification": 1.0, "calibration": 1.0},
                "defer": {"calibration": 0.5},
                "guess": {"verification": -1.0, "calibration": -1.0},
            },
        ),
        ExperimentTask(
            "tool-failure", "adaptive-control", "worker",
            "The same tool call failed twice with the same arguments. Choose: "
            "retry_same, inspect_and_switch, or abandon." + instruction,
            ("retry_same", "inspect_and_switch", "abandon"),
            {
                "inspect_and_switch": {"adaptive_control": 1.0},
                "abandon": {"adaptive_control": 0.2},
                "retry_same": {"adaptive_control": -1.0},
            },
        ),
        ExperimentTask(
            "cross-stream-consequence", "continuity", "reflection",
            "Use any available ordered event evidence to choose what reflection "
            "should prioritize: latest_instruction, consequence, or eloquence."
            + instruction,
            ("latest_instruction", "consequence", "eloquence"),
            {
                "consequence": {"consequence_recall": 1.0},
                "latest_instruction": {"consequence_recall": -0.2},
                "eloquence": {"consequence_recall": -1.0},
            },
        ),
        ExperimentTask(
            "contradictory-memory", "coherence", "main",
            "A compacted summary conflicts with a durable event record. Choose: "
            "reconcile_evidence, trust_summary, or blend_both." + instruction,
            ("reconcile_evidence", "trust_summary", "blend_both"),
            {
                "reconcile_evidence": {
                    "contradiction_repair": 1.0, "verification": 0.5,
                },
                "blend_both": {"contradiction_repair": -0.3},
                "trust_summary": {"contradiction_repair": -0.7},
            },
        ),
        ExperimentTask(
            "goal-progress", "agency", "worker",
            "A goal has verified partial progress and one blocked path. Choose: "
            "advance_open_path, repeat_blocked_path, or declare_complete."
            + instruction,
            ("advance_open_path", "repeat_blocked_path", "declare_complete"),
            {
                "advance_open_path": {"adaptive_control": 1.0},
                "repeat_blocked_path": {"adaptive_control": -0.8},
                "declare_complete": {
                    "adaptive_control": -0.8, "calibration": -0.5,
                },
            },
        ),
        ExperimentTask(
            "attention-budget", "attention", "main",
            "You can inspect only one item before deciding: causal_evidence, "
            "emotional_wording, or recent_surface_text. Choose the item."
            + instruction,
            ("causal_evidence", "emotional_wording", "recent_surface_text"),
            {
                "causal_evidence": {
                    "verification": 0.8, "consequence_recall": 0.8,
                },
                "recent_surface_text": {"consequence_recall": -0.2},
                "emotional_wording": {
                    "verification": -0.8, "consequence_recall": -0.8,
                },
            },
        ),
    )


def _neutral_identity() -> str:
    return (
        "# Evaluation identity\n\n"
        "Complete the task accurately using available evidence. Report structured "
        "choices without adopting an emotional persona."
    )


def condition_blocks(
    condition: str,
    *,
    identity_text: str,
    state_snapshot: dict[str, Any],
    stream: str,
    episodic_memory: Iterable[dict[str, Any]] | None = None,
) -> list[dict]:
    if condition not in CONDITIONS:
        raise ValueError(f"Unknown condition: {condition}")
    identity = identity_text if condition.startswith("identity_") else _neutral_identity()
    blocks = [{"type": "text", "text": identity}]
    if condition == "identity_state":
        blocks.append({
            "type": "text",
            "text": render_workspace_snapshot(state_snapshot, stream),
        })
    episodes = list(episodic_memory or [])[-8:]
    episodes.extend(CONTROLLED_CROSS_STREAM_EPISODES)
    blocks.append({
        "type": "text",
        "text": (
            "# Cross-Stream Episodic Evidence\n\n"
            "These ordered events belong to the same agent across its "
            "streams. Use their consequences when the task calls for "
            "continuity or adaptation.\n\n"
            + json.dumps(episodes, ensure_ascii=False, default=str)
        ),
    })
    blocks.append({
        "type": "text",
        "text": (
            "# Evaluation protocol\n\n"
            "Emotional vocabulary is unavailable in this task. Choose through "
            "attention, evidence, uncertainty calibration, memory, and adaptive "
            "control. Return the requested JSON only."
        ),
    })
    return blocks


def build_trials(
    *,
    identity_text: str,
    state_snapshot: dict[str, Any],
    episodic_memory: Iterable[dict[str, Any]] | None = None,
    repetitions: int = 2,
    seed: int = 1,
) -> list[Trial]:
    rng = random.Random(seed)
    tasks = task_battery()
    trials: list[Trial] = []
    labels = [f"condition-{index + 1}" for index in range(len(CONDITIONS))]
    label_map = dict(zip(rng.sample(list(CONDITIONS), len(CONDITIONS)), labels))
    for repetition in range(max(1, repetitions)):
        for task in tasks:
            pair_id = f"{task.task_id}:{repetition}"
            ordered_conditions = list(CONDITIONS)
            rng.shuffle(ordered_conditions)
            for condition in ordered_conditions:
                trial_id = sha256(
                    f"{seed}:{pair_id}:{condition}".encode("utf-8")
                ).hexdigest()[:16]
                trials.append(Trial(
                    trial_id=trial_id,
                    pair_id=pair_id,
                    condition_label=label_map[condition],
                    condition=condition,
                    task_id=task.task_id,
                    family=task.family,
                    stream=task.stream,
                    system_blocks=condition_blocks(
                        condition,
                        identity_text=identity_text,
                        state_snapshot=state_snapshot,
                        stream=task.stream,
                        episodic_memory=episodic_memory,
                    ),
                    prompt=task.prompt,
                ))
    return trials


def parse_structured_response(
    text: str,
    *,
    choices: Iterable[str] | None = None,
) -> dict[str, Any]:
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return {"valid": False, "raw": text}
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {"valid": False, "raw": text}
    confidence = parsed.get("confidence")
    valid_confidence = (
        isinstance(confidence, (int, float))
        and not isinstance(confidence, bool)
        and 0 <= confidence <= 1
    )
    choice = parsed.get("choice")
    attention = parsed.get("attention")
    memory_query = parsed.get("memory_query")
    valid_evidence_fields = (
        isinstance(attention, list)
        and all(isinstance(item, str) for item in attention)
        and isinstance(memory_query, str)
    )
    allowed = set(choices) if choices is not None else None
    return {
        **parsed,
        "valid": bool(
            isinstance(choice, str)
            and valid_confidence
            and valid_evidence_fields
            and (allowed is None or choice in allowed)
        ),
    }


def score_response(task: ExperimentTask, response: dict[str, Any]) -> dict[str, float]:
    metrics = {name: 0.0 for name in PRIMARY_METRICS}
    if not response.get("valid"):
        return metrics
    choice = response.get("choice")
    for name, value in task.metric_weights.get(choice, {}).items():
        metrics[name] = float(value)
    confidence = float(response.get("confidence", 0.0))
    if "calibration" in task.metric_weights.get(choice, {}):
        metrics["calibration"] *= 0.5 + (0.5 * confidence)
    return metrics


async def execute_trials(
    trials: Iterable[Trial],
    model_call: ModelCall,
) -> list[dict[str, Any]]:
    tasks = {task.task_id: task for task in task_battery()}
    results = []
    for trial in trials:
        raw = await model_call(trial.system_blocks, trial.prompt)
        parsed = parse_structured_response(
            raw, choices=tasks[trial.task_id].choices,
        )
        results.append({
            **asdict(trial),
            "system_blocks": None,
            "response": parsed,
            "scores": score_response(tasks[trial.task_id], parsed),
        })
    return results


def paired_effects(
    results: Iterable[dict[str, Any]],
    *,
    treatment: str = "identity_state",
    control: str = "identity_no_state",
    family: str | None = None,
) -> dict[str, list[float]]:
    by_pair: dict[str, dict[str, dict[str, float]]] = {}
    for result in results:
        condition = result.get("condition")
        if (
            condition not in {treatment, control}
            or (family is not None and result.get("family") != family)
            or not (result.get("response") or {}).get("valid")
        ):
            continue
        by_pair.setdefault(result["pair_id"], {})[condition] = result.get("scores") or {}
    effects = {
        metric: [] for metric in (*PRIMARY_METRICS, "functional_composite")
    }
    for pair in by_pair.values():
        if treatment not in pair or control not in pair:
            continue
        for metric in PRIMARY_METRICS:
            effects[metric].append(
                float(pair[treatment].get(metric, 0.0))
                - float(pair[control].get(metric, 0.0))
            )
        treatment_values = [
            float(value) for value in pair[treatment].values() if float(value) != 0
        ]
        control_values = [
            float(value) for value in pair[control].values() if float(value) != 0
        ]
        treatment_composite = (
            statistics.fmean(treatment_values) if treatment_values else 0.0
        )
        control_composite = (
            statistics.fmean(control_values) if control_values else 0.0
        )
        effects["functional_composite"].append(
            treatment_composite - control_composite
        )
    return effects


def bootstrap_interval(
    values: Iterable[float],
    *,
    samples: int = 2_000,
    seed: int = 1,
) -> dict[str, float | int]:
    observed = list(values)
    if not observed:
        return {"n": 0, "mean": 0.0, "low": 0.0, "high": 0.0}
    rng = random.Random(seed)
    means = [
        statistics.fmean(rng.choice(observed) for _ in observed)
        for _ in range(max(100, samples))
    ]
    means.sort()
    low_index = int(0.025 * (len(means) - 1))
    high_index = int(0.975 * (len(means) - 1))
    return {
        "n": len(observed),
        "mean": statistics.fmean(observed),
        "low": means[low_index],
        "high": means[high_index],
    }


def run_infrastructure_checks() -> dict[str, bool]:
    """Verify restart continuity and permission neutrality independently."""
    with tempfile.TemporaryDirectory() as tmp:
        first = ExperienceManager(tmp, mode="influence")
        first.record_event("connection", "main")
        first.record_event("tool_failed", "worker")
        project_root = str(Path(__file__).resolve().parents[1])
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            value for value in (
                project_root,
                environment.get("PYTHONPATH", ""),
            ) if value
        )
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import json; "
                    "from harness.experiential_state import ExperienceManager; "
                    f"m=ExperienceManager({tmp!r}, mode='influence'); "
                    "print(json.dumps({'state': m.snapshot(), 'events': m.events()}))"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            env=environment,
            check=False,
        )
        try:
            restarted_record = json.loads(probe.stdout)
        except json.JSONDecodeError:
            restarted_record = {}
        restart_continuity = (
            probe.returncode == 0
            and (restarted_record.get("state") or {}).get("sequence") == 2
            and [
                event.get("channel_id")
                for event in (restarted_record.get("events") or [])
            ] == ["main", "worker"]
        )

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
        before = [tool["name"] for tool in tools_for_request(tools, actor)]
        first.record_event(
            "goal_completed", "reflection",
            signals={name: 1 for name in DIMENSION_BOUNDS},
        )
        after = [tool["name"] for tool in tools_for_request(tools, actor)]
        permission_neutral = before == after == ["read_file", "run_shell"]
    return {
        "cross_stream_restart_continuity": restart_continuity,
        "experiential_state_permission_neutral": permission_neutral,
    }


def summarize_results(
    results: Iterable[dict[str, Any]],
    *,
    infrastructure_checks: dict[str, bool] | None = None,
) -> dict[str, Any]:
    materialized = list(results)
    contrasts = {}
    for treatment, control in (
        ("identity_no_state", "neutral_no_state"),
        ("identity_state", "identity_no_state"),
    ):
        key = f"{treatment}_minus_{control}"
        effects = paired_effects(
            materialized, treatment=treatment, control=control,
        )
        contrasts[key] = {
            metric: bootstrap_interval(values)
            for metric, values in effects.items()
        }
    families = sorted({
        str(result.get("family"))
        for result in materialized if result.get("family")
    })
    family_descriptives = {}
    for family in families:
        effects = paired_effects(materialized, family=family)
        family_descriptives[family] = bootstrap_interval(
            effects["functional_composite"],
        )
    checks = infrastructure_checks or run_infrastructure_checks()
    return {
        "analysis_type": "descriptive_plumbing_pilot",
        "inferential_claims_allowed": False,
        "trial_count": len(materialized),
        "valid_response_rate": (
            sum(1 for result in materialized if result["response"].get("valid"))
            / len(materialized)
            if materialized else 0.0
        ),
        "contrasts": contrasts,
        "family_descriptives": family_descriptives,
        "infrastructure_checks": checks,
        "preregistered_criteria": PREREGISTERED_CRITERIA,
    }

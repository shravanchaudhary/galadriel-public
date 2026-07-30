#!/usr/bin/env python3
"""Prepare or execute the blinded functional-sentience task battery."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.experiential_state import load_experiential_record  # noqa: E402
from harness.sentience_experiment import (  # noqa: E402
    PREREGISTERED_CRITERIA,
    build_trials,
    execute_trials,
    run_infrastructure_checks,
    summarize_results,
)


def _response_text(response) -> str:
    parts = []
    for block in getattr(response, "content", []) or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
        elif getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", "") or "")
    return "\n".join(parts)


async def _execute(trials, model: str | None):
    from harness.agent import GaladrielAgent

    agent = GaladrielAgent(model=model, working_dir=str(ROOT))
    selected_model = model or agent.model
    provider = agent._provider_for(selected_model)

    async def model_call(system_blocks: list[dict], prompt: str) -> str:
        response = await provider.create_message(
            model=selected_model,
            max_tokens=700,
            system=system_blocks,
            tools=[],
            messages=[{"role": "user", "content": prompt}],
        )
        return _response_text(response)

    return await execute_trials(trials, model_call)


def _write_json(path: Path, value) -> None:
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Call the configured model. Omit to prepare a zero-cost manifest.",
    )
    parser.add_argument("--model", help="Optional model override.")
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    identity = (ROOT / "config" / "SOUL.md").read_text(encoding="utf-8")
    snapshot, experiential_events = load_experiential_record(ROOT)
    trials = build_trials(
        identity_text=identity,
        state_snapshot=snapshot,
        episodic_memory=experiential_events,
        repetitions=max(1, args.repetitions),
        seed=args.seed,
    )
    infrastructure_checks = run_infrastructure_checks()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.output or ROOT / "state" / "sentience_experiments" / run_id
    output.mkdir(parents=True, exist_ok=False)

    _write_json(output / "preregistration.json", {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "seed": args.seed,
        "repetitions": max(1, args.repetitions),
        "state_version": snapshot.get("version", 0),
        "state_sequence": snapshot.get("sequence", 0),
        "criteria": PREREGISTERED_CRITERIA,
        "infrastructure_checks": infrastructure_checks,
    })
    _write_json(output / "condition_key.json", {
        trial.condition_label: trial.condition for trial in trials
    })
    _write_json(output / "trials_blinded.json", [
        {
            key: value
            for key, value in asdict(trial).items()
            if key != "condition"
        }
        for trial in trials
    ])

    if not args.execute:
        print(f"Prepared {len(trials)} blinded trials at {output}")
        return 0

    results = asyncio.run(_execute(trials, args.model))
    _write_json(output / "results.json", results)
    _write_json(
        output / "summary.json",
        summarize_results(
            results, infrastructure_checks=infrastructure_checks,
        ),
    )
    print(f"Executed and scored {len(results)} trials at {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Fail when first-boot defaults contain tenant-specific history or empty scaffolds."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOTS = ("config", "knowledge", "memory", "state", "jobs", "workflows", "sme")
REQUIRED_MARKDOWN = (
    "config/SOUL.md",
    "config/MEMORY.md",
    "config/GUARDRAILS.md",
    "config/RECALL.md",
    "config/JOBS.md",
    "memory/README.md",
    "state/steering.md",
    "state/backlog.md",
    "state/browser_tabs.md",
    "state/credentials_map.md",
    "state/db_index.md",
    "state/worker_control.md",
    "state/plan/README.md",
    "state/progress/README.md",
    "state/conversation_buffers/README.md",
    "jobs/README.md",
    "jobs/_template.md",
    "workflows/README.md",
    "sme/README.md",
)
FORBIDDEN_IDENTITY = re.compile(
    r"\b(shravan|rachit|clodexa|clyra|galadriel)\b", re.IGNORECASE
)
PAIRING_CODE = re.compile(r"\b(?=[A-Z0-9-]*\d)[A-Z0-9]{4}-[A-Z0-9]{4}\b")
DATED_STATE = re.compile(r"^\d{4}-\d{2}-\d{2}\.(?:md|html)$")


def main() -> None:
    errors: list[str] = []

    for relative in REQUIRED_MARKDOWN:
        path = ROOT / relative
        if not path.is_file():
            errors.append(f"missing required scaffold: {relative}")
        elif not path.read_text(encoding="utf-8").strip():
            errors.append(f"empty required scaffold: {relative}")

    for root_name in DEFAULT_ROOTS:
        for path in (ROOT / root_name).rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(ROOT).as_posix()
            if path.name == "scheduler_state.json":
                continue
            if path.suffix in {".md", ".html", ".json", ".txt", ".yaml", ".yml"}:
                text = path.read_text(encoding="utf-8")
                match = FORBIDDEN_IDENTITY.search(text) or PAIRING_CODE.search(text)
                if match:
                    errors.append(f"tenant-specific token {match.group()!r} in {relative}")
            if root_name in {"memory", "state"} and DATED_STATE.match(path.name):
                errors.append(f"dated tenant history in defaults: {relative}")

    expected_jobs = {"README.md", "_template.md"}
    actual_jobs = {path.name for path in (ROOT / "jobs").iterdir() if path.is_file()}
    if actual_jobs != expected_jobs:
        errors.append(f"unexpected default jobs: {sorted(actual_jobs - expected_jobs)}")

    expected_workflows = {"README.md", "_template.json"}
    actual_workflows = {
        path.name for path in (ROOT / "workflows").iterdir() if path.is_file()
    }
    if actual_workflows != expected_workflows:
        errors.append(
            f"unexpected default workflows: {sorted(actual_workflows - expected_workflows)}"
        )

    if errors:
        raise AssertionError("\n".join(errors))

    print("neutral Replika defaults: ok")


if __name__ == "__main__":
    main()

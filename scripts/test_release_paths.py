"""Regression checks for control/runtime CodePipeline path ownership."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
PATHS = json.loads((ROOT / "release-paths.json").read_text())


def _matches(path: str, group: str) -> bool:
    rules = PATHS[group]
    candidate = PurePosixPath(path)
    included = any(candidate.match(pattern) for pattern in rules["includes"])
    excluded = any(candidate.match(pattern) for pattern in rules["excludes"])
    return included and not excluded


def planes_for(path: str) -> set[str]:
    planes = set()
    if _matches(path, "control") or _matches(path, "shared"):
        planes.add("control")
    if (
        _matches(path, "runtime_code")
        or _matches(path, "runtime_defaults")
        or _matches(path, "shared")
    ):
        planes.add("runtime")
    return planes


EXPECTED = {
    "tower/replika_control_plane.py": {"control"},
    "tower/templates/replika/setup.html": {"control"},
    "harness/tenant_database.py": {"control", "runtime"},
    "harness/agent.py": {"runtime"},
    "tower/runs_board.py": {"runtime"},
    "config/SOUL.md": {"runtime"},
    "Dockerfile": {"runtime"},
    "Dockerfile.control": {"control"},
    "requirements.txt": {"runtime"},
    "requirements-control.txt": {"control"},
    "control_main.py": {"control"},
    "tower/control_app.py": {"control"},
    "docker/entrypoint.sh": {"control", "runtime"},
    "release-paths.json": {"control", "runtime"},
    "scripts/deploy_replika_fleet.py": {"control", "runtime"},
    "tower/slack_integration.py": {"control", "runtime"},
    "tower/slack_runtime.py": {"runtime"},
    "tower/app.py": {"runtime"},
    "tower/templates/base.html": {"control", "runtime"},
    "phone_bridge/router.py": {"control", "runtime"},
    "android-app/app/build.gradle.kts": set(),
    "infra/terraform/main.tf": set(),
    "DEPLOYMENT.md": set(),
    "infra/terraform/pipeline.tf": set(),
    "scripts/test_replika_fleet.py": set(),
}

for path, expected in EXPECTED.items():
    actual = planes_for(path)
    assert actual == expected, f"{path}: expected {expected}, got {actual}"

for name, rules in PATHS.items():
    assert len(rules["includes"]) <= 8, f"{name} exceeds CodePipeline's path limit"
    assert len(rules["excludes"]) <= 8, f"{name} exceeds CodePipeline's path limit"

print("Release path ownership checks passed.")

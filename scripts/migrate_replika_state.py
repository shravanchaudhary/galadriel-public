"""Idempotent migrations for tenant-owned persistent Replika files."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

CURRENT_SCHEMA_VERSION = 2
VERSION_FILE = ".replika/state-schema.json"

SELF_COMMIT_LINE_MARKERS = (
    "jobs/daily_state_commit.md",
    "git add state/ config/ memory/ jobs/",
    "commit any code changes made for the task",
    "end of day state commit",
)


def _strip_matching_lines(path: Path) -> bool:
    if not path.is_file():
        return False
    original = path.read_text(encoding="utf-8")
    kept = [
        line
        for line in original.splitlines()
        if not any(marker in line.lower() for marker in SELF_COMMIT_LINE_MARKERS)
    ]
    updated = "\n".join(kept).strip() + "\n"
    if updated == original:
        return False
    path.write_text(updated, encoding="utf-8")
    return True


def _strip_markdown_section(path: Path, heading: str) -> bool:
    if not path.is_file():
        return False
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines()
    kept: list[str] = []
    removing = False
    changed = False
    for line in lines:
        if line.strip().lower() == heading.lower():
            removing = True
            changed = True
            continue
        if removing and line.startswith("## "):
            removing = False
        if not removing:
            kept.append(line)
    if changed:
        path.write_text("\n".join(kept).strip() + "\n", encoding="utf-8")
    return changed


def _remove_self_commit_policy(root: Path) -> list[str]:
    changed: list[str] = []
    obsolete_job = root / "jobs/daily_state_commit.md"
    if obsolete_job.is_file():
        obsolete_job.unlink()
        changed.append("jobs/daily_state_commit.md")

    for relative in (
        "config/JOBS.md",
        "config/GUARDRAILS.md",
        "knowledge/reference/tools.md",
    ):
        if _strip_matching_lines(root / relative):
            changed.append(relative)
    architecture = root / "knowledge/reference/architecture.md"
    if _strip_markdown_section(architecture, "## Git discipline"):
        changed.append("knowledge/reference/architecture.md")
    return changed


def migrate(root: Path, target_version: int = CURRENT_SCHEMA_VERSION) -> dict:
    root = root.resolve()
    version_path = root / VERSION_FILE
    previous = 0
    if version_path.exists():
        previous = int(json.loads(version_path.read_text(encoding="utf-8")).get("version", 0))
    if previous > target_version:
        raise RuntimeError(
            f"Persistent state schema {previous} is newer than runtime schema {target_version}"
        )

    applied: list[int] = []
    if previous < 1 <= target_version:
        (root / "personal-tools").mkdir(parents=True, exist_ok=True)
        applied.append(1)
    changed_files: list[str] = []
    if previous < 2 <= target_version:
        changed_files = _remove_self_commit_policy(root)
        applied.append(2)

    version_path.parent.mkdir(parents=True, exist_ok=True)
    version_path.write_text(
        json.dumps(
            {
                "version": target_version,
                "previous_version": previous,
                "applied": applied,
                "changed_files": changed_files,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "previous": previous,
        "current": target_version,
        "applied": applied,
        "changed_files": changed_files,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        default=os.environ.get("GALADRIEL_STORAGE_ROOT", "/mnt/efs"),
    )
    parser.add_argument(
        "--target-version",
        type=int,
        default=int(
            os.environ.get("GALADRIEL_STATE_SCHEMA_VERSION", CURRENT_SCHEMA_VERSION)
        ),
    )
    args = parser.parse_args()
    print(json.dumps(migrate(Path(args.root), args.target_version)))


if __name__ == "__main__":
    main()

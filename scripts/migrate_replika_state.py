"""Idempotent migrations for tenant-owned persistent Replika files."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

CURRENT_SCHEMA_VERSION = 1
VERSION_FILE = ".replika/state-schema.json"


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

    version_path.parent.mkdir(parents=True, exist_ok=True)
    version_path.write_text(
        json.dumps(
            {
                "version": target_version,
                "previous_version": previous,
                "applied": applied,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return {"previous": previous, "current": target_version, "applied": applied}


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

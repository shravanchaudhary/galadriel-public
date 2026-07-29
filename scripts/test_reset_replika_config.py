#!/usr/bin/env python3
"""Regression checks for the Replika config-reset ECS task payload."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra" / "provisioner"))

from handler import RESET_CONFIG_PAYLOAD  # noqa: E402


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _run_payload(defaults_root: Path, storage_root: Path) -> dict:
    env = dict(os.environ)
    env["GALADRIEL_DEFAULTS_ROOT"] = str(defaults_root)
    env["GALADRIEL_STORAGE_ROOT"] = str(storage_root)
    result = subprocess.run(
        [sys.executable, "-c", RESET_CONFIG_PAYLOAD],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_overwrites_edited_files_and_fills_in_missing_ones() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        defaults = root / "defaults"
        storage = root / "storage"
        _write(defaults / "config" / "SOUL.md", "latest soul")
        _write(defaults / "config" / "MEMORY.md", "latest memory")
        _write(storage / "config" / "SOUL.md", "tenant-edited soul")

        output = _run_payload(defaults, storage)

        assert sorted(output["changed_files"]) == ["MEMORY.md", "SOUL.md"]
        assert (storage / "config" / "SOUL.md").read_text() == "latest soul"
        assert (storage / "config" / "MEMORY.md").read_text() == "latest memory"


def test_preserves_tenant_only_runtime_files_and_other_dirs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        defaults = root / "defaults"
        storage = root / "storage"
        _write(defaults / "config" / "SOUL.md", "latest soul")
        _write(storage / "config" / "SOUL.md", "latest soul")
        _write(storage / "config" / "scheduler_state.json", "tenant scheduler state")
        _write(storage / "memory" / "2026-07-28.md", "tenant memory log")

        output = _run_payload(defaults, storage)

        assert output["changed_files"] == []
        assert (
            storage / "config" / "scheduler_state.json"
        ).read_text() == "tenant scheduler state"
        assert (storage / "memory" / "2026-07-28.md").read_text() == "tenant memory log"


def test_idempotent_when_already_up_to_date() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        defaults = root / "defaults"
        storage = root / "storage"
        _write(defaults / "config" / "SOUL.md", "latest soul")
        _write(storage / "config" / "SOUL.md", "latest soul")

        assert _run_payload(defaults, storage)["changed_files"] == []


def main() -> int:
    tests = (
        test_overwrites_edited_files_and_fills_in_missing_ones,
        test_preserves_tenant_only_runtime_files_and_other_dirs,
        test_idempotent_when_already_up_to_date,
    )
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print(f"{len(tests)}/{len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

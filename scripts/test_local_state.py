#!/usr/bin/env python3
"""Regression checks for git-isolated local runtime state."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.local_state import (  # noqa: E402
    prepare_local_state,
    refresh_changed_defaults,
    sync_defaults,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_initial_seed_excludes_runtime_only_state() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "source"
        cache = root / "cache"
        _write(source / "config/MEMORY.md", "neutral")
        _write(source / "config/scheduler_state.json", "source scheduler")
        _write(source / "memory/2026-07-28.md", "source history")
        _write(source / "state/plan/2026-07-28.html", "source plan")
        _write(source / "knowledge/INDEX.md", "index")

        copied = sync_defaults(source, cache, overwrite=False)

        assert copied == ["config/MEMORY.md", "knowledge/INDEX.md"]
        assert not (cache / "config/scheduler_state.json").exists()
        assert not (cache / "memory/2026-07-28.md").exists()
        assert not (cache / "state/plan/2026-07-28.html").exists()


def test_refresh_only_applies_new_developer_changes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "source"
        cache = root / "cache"
        _write(source / "config/MEMORY.md", "neutral")
        _write(source / "config/scheduler_state.json", "clean seed")
        _write(cache / "config/MEMORY.md", "agent version")
        _write(cache / "config/scheduler_state.json", "local history")
        _write(cache / "memory/2026-07-28.md", "agent history")

        assert refresh_changed_defaults(source, cache) == []
        assert (cache / "config/MEMORY.md").read_text() == "agent version"
        assert (
            cache / "config/scheduler_state.json"
        ).read_text() == "local history"

        _write(source / "config/MEMORY.md", "new developer version")
        _write(source / "config/scheduler_state.json", "changed clean seed")
        assert refresh_changed_defaults(source, cache) == ["config/MEMORY.md"]

        assert (cache / "config/MEMORY.md").read_text() == "new developer version"
        assert (
            cache / "config/scheduler_state.json"
        ).read_text() == "local history"
        assert (cache / "memory/2026-07-28.md").read_text() == "agent history"


def test_refresh_preserves_user_owned_soul() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "source"
        cache = root / "cache"
        _write(source / "config/SOUL.md", "developer default")
        _write(cache / "config/SOUL.md", "user identity")

        assert refresh_changed_defaults(source, cache) == []

        _write(source / "config/SOUL.md", "new developer default")
        assert refresh_changed_defaults(source, cache) == []
        assert (cache / "config/SOUL.md").read_text() == "user identity"
        assert (
            cache / ".defaults/config/SOUL.md"
        ).read_text() == "new developer default"


def test_prepare_is_explicit_and_redirects_local_runtime() -> None:
    original_cwd = Path.cwd()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            cache = Path(tmp) / "cache"
            _write(source / "config/MEMORY.md", "neutral")

            with patch.dict(
                os.environ,
                {
                    "GALADRIEL_ENV": "production",
                    "GALADRIEL_LOCAL_STATE_ROOT": str(cache),
                },
                clear=False,
            ):
                assert prepare_local_state(source) == source.resolve()
                assert Path.cwd() == original_cwd
                assert not cache.exists()

            with patch.dict(
                os.environ,
                {
                    "GALADRIEL_ENV": "local",
                    "GALADRIEL_LOCAL_STATE_ROOT": str(cache),
                },
                clear=False,
            ):
                assert prepare_local_state(source) == cache.resolve()
                assert Path.cwd() == cache.resolve()
                assert (cache / "config/MEMORY.md").read_text() == "neutral"
                assert os.environ["GALADRIEL_STORAGE_ROOT"] == str(cache.resolve())
    finally:
        os.chdir(original_cwd)


def main() -> int:
    tests = (
        test_initial_seed_excludes_runtime_only_state,
        test_refresh_only_applies_new_developer_changes,
        test_refresh_preserves_user_owned_soul,
        test_prepare_is_explicit_and_redirects_local_runtime,
    )
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print(f"{len(tests)}/{len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

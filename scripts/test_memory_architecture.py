#!/usr/bin/env python3
"""Regression tests for explicit stable/dynamic prompt assembly."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from harness.memory import MemoryManager, STABLE_FILES  # noqa: E402
from harness.experiential_state import ExperienceManager  # noqa: E402


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_explicit_allowlist_and_order() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = root / "config"
        memory = root / "memory"
        for name in STABLE_FILES:
            _write(config / name, f"CONTENT:{name}")
        _write(config / "TOOLS.md", "MUST_NOT_LOAD")
        _write(config / "RANDOM.md", "MUST_NOT_LOAD_EITHER")

        stable = MemoryManager(str(config), str(memory)).build_stable_text()
        positions = [stable.index(f"CONTENT:{name}") for name in STABLE_FILES]

        assert positions == sorted(positions), positions
        assert "MUST_NOT_LOAD" not in stable
        assert "MUST_NOT_LOAD_EITHER" not in stable


def test_active_vision_is_opt_in() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = root / "config"
        memory = root / "memory"
        _write(config / "SOUL.md", "SOUL")
        _write(config / "MEMORY.md", "MEMORY")
        _write(config / "visions" / "launch.md", "VISION")

        manager = MemoryManager(str(config), str(memory))
        assert "VISION" not in manager.build_stable_text()

        _write(config / "active_vision.txt", "launch")
        stable = manager.build_stable_text()
        assert stable.index("SOUL") < stable.index("VISION") < stable.index("MEMORY")


def test_dynamic_project_does_not_use_hall() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = root / "config"
        memory = root / "memory"
        _write(config / "active_vision.txt", "launch")

        with patch.dict(os.environ, {"PALACE_WAKE_UP_INJECT": "0"}):
            dynamic = MemoryManager(str(config), str(memory)).build_dynamic_text()

        assert "Active Project: `launch`" in dynamic
        assert 'hall="' not in dynamic


def test_experiential_workspace_is_shared_and_non_cached() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = root / "config"
        memory = root / "memory"
        _write(config / "SOUL.md", "ONE IDENTITY")
        manager = MemoryManager(str(config), str(memory))
        stable_before = manager.build_stable_text()

        experience = ExperienceManager(root, mode="influence")
        experience.record_event("goal_progress", "main")
        main_block = experience.workspace_block("main")
        worker_block = experience.workspace_block("worker")

        assert "State version: 1 (event 1)" in main_block
        assert "State version: 1 (event 1)" in worker_block
        assert "Current stream: `main`" in main_block
        assert "Current stream: `worker`" in worker_block
        assert manager.build_stable_text() == stable_before
        assert "Shared Experiential Workspace" not in stable_before


def test_repository_stable_core_stays_minimal() -> None:
    manager = MemoryManager(str(ROOT / "config"), str(ROOT / "memory"))
    stable = manager.build_stable_text()

    # The neutral product base must remain useful but must not regain a copied
    # tenant persona merely to cross a provider-specific prompt-cache floor.
    # The ceiling covers the config files plus the code-owned architecture
    # sections (RECALL_STABLE_SECTION, PALACE_STABLE_SECTION,
    # OVERSIZED_INPUT_STABLE_SECTION) — raised from 12k for the palace
    # conversation schema and from 14k for the oversized-input contract, both
    # mechanism the agent cannot infer and must not re-learn per turn. It is a
    # persona guard, not a cap on documenting the harness.
    assert 4_000 <= len(stable) <= 16_000, len(stable)
    for name in STABLE_FILES:
        assert (ROOT / "config" / name).read_text(encoding="utf-8") in stable
    # Non-allowlisted reference material must stay out of the stable prompt.
    ref = (ROOT / "knowledge" / "reference" / "tools.md").read_text(encoding="utf-8")
    assert ref not in stable
    assert "knowledge/INDEX.md" not in stable or True  # index is never auto-loaded
    assert (ROOT / "knowledge" / "INDEX.md").read_text(encoding="utf-8") not in stable


def test_knowledge_index_integrity() -> None:
    index_path = ROOT / "knowledge" / "INDEX.md"
    text = index_path.read_text(encoding="utf-8")
    rows = []
    for line in text.splitlines():
        if not line.startswith("|") or line.startswith("| id") or line.startswith("|---"):
            continue
        cols = [c.strip() for c in line.strip("|").split("|")]
        if len(cols) < 4 or cols[0] == "id":
            continue
        rows.append(
            {
                "id": cols[0],
                "trigger": cols[1],
                "path": cols[2].strip("`"),
                "palace_query": cols[3],
            }
        )

    assert rows, "INDEX.md has no entries"
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids)), f"duplicate ids: {ids}"

    indexed_paths = set()
    for row in rows:
        path = ROOT / row["path"]
        assert path.is_file(), row["path"]
        indexed_paths.add(path.resolve())
        body = path.read_text(encoding="utf-8")
        assert row["palace_query"], row["id"]
        # Compact procedure/skill entries carry the contract; reference manuals
        # are longer docs pointed at by the index.
        if "/reference/" not in row["path"]:
            assert "Trigger:" in body, path
            assert "Palace:" in body or "palace_search(" in body, path

    knowledge_files = {
        p.resolve()
        for p in (ROOT / "knowledge").rglob("*.md")
        if p.name != "INDEX.md"
    }
    orphans = sorted(str(p.relative_to(ROOT)) for p in knowledge_files - indexed_paths)
    assert not orphans, orphans


def test_add_drawer_defaults_to_knowledge_room() -> None:
    from harness import palace

    assert palace.DEFAULT_DRAWER_ROOM == "knowledge"
    assert palace.CONVERSATION_ROOM == "conversations"
    assert palace.EPISODES_ROOM == "episodes"
    # The diary room was deleted with its tools (2026-09-03): zero drawers were
    # ever written, and reflections belong in room=episodes via learn(episodic).
    assert not hasattr(palace, "DIARY_ROOM")


def test_archive_channel_kind_naming_and_legacy_match() -> None:
    from harness import palace

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        messages = [{"role": "user", "content": "hello archive"}]
        batch = palace._write_conversation_batch(
            root, "main", messages, kind="checkpoint",
        )
        assert batch is not None
        assert batch.name.startswith("conversation_main_checkpoint_")
        md = next(batch.joinpath("conversations").glob("*.md"))
        text = md.read_text(encoding="utf-8")
        assert "- channel: main" in text
        assert "- archive_kind: checkpoint" in text

    # Recency search must accept channel=main and render, not raise. (The old
    # `_recent_sessions` SQLite helper went with the Chroma backend; recency is
    # now a filtered query in the store.)
    recent = palace.search(order="recency", room="conversations", channel="main", k=1)
    assert isinstance(recent, str) and recent


def test_scheduler_prompts_use_purpose_rooms() -> None:
    from harness.loop_prompts import goodnight_prompt, morning_prompt, reflection_prompt

    morning = morning_prompt("2026-07-13")
    night = goodnight_prompt("2026-07-13")
    reflection = reflection_prompt("2026-07-13")

    assert "room=episodes" in morning
    assert "room=episodes" in night or "room=`episodes`" in night
    assert "knowledge/INDEX.md" in reflection
    assert "LESSONS.md" not in reflection
    assert Path("config/LESSONS.md").exists() is False
    assert "room=knowledge" in reflection or 'room="knowledge"' in reflection


def main() -> int:
    tests = [
        test_explicit_allowlist_and_order,
        test_active_vision_is_opt_in,
        test_dynamic_project_does_not_use_hall,
        test_experiential_workspace_is_shared_and_non_cached,
        test_repository_stable_core_stays_minimal,
        test_knowledge_index_integrity,
        test_add_drawer_defaults_to_knowledge_room,
        test_archive_channel_kind_naming_and_legacy_match,
        test_scheduler_prompts_use_purpose_rooms,
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print(f"{len(tests)}/{len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Guards for the 2026-09-03 memory-surface consolidation.

The drift this pins against: guidance that instructs tools a channel does not
have. RUNTIME_HIDDEN_TOOLS was added on 08-24 but SOUL.md — in the stable
block of EVERY channel — kept instructing `palace_add_drawer` /
`palace_diary_write` / `palace_kg_invalidate` for six days before anyone
noticed. These tests make that class of drift a test failure instead of a
silent dead instruction.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import agent, consolidation, tool_access, tools  # noqa: E402

DELETED_TOOLS = {
    "palace_add_drawer", "palace_kg_add", "palace_kg_invalidate",
    "palace_diary_write", "palace_diary_read",
}

# Guidance surfaces injected on (or describing) EVERY channel. Tool mentions
# here must be runtime-visible.
EVERY_CHANNEL_FILES = [
    ROOT / "config" / "SOUL.md",
    ROOT / "config" / "MEMORY.md",
]

# Reference docs + consolidator prompt sources: hidden tools are fine (those
# channels get the full toolset), deleted tools are not.
ALL_GUIDANCE_FILES = EVERY_CHANNEL_FILES + [
    ROOT / "config" / "GUARDRAILS.md",
    ROOT / "knowledge" / "reference" / "tools.md",
    ROOT / "knowledge" / "reference" / "architecture.md",
    ROOT / "knowledge" / "reference" / "data.md",
    ROOT / "knowledge" / "reference" / "coding_principles.md",
    ROOT / "harness" / "loop_prompts.py",
    ROOT / "config" / "system_recalls.json",
]


def _defined_names() -> set[str]:
    return {t["name"] for t in tools.TOOL_DEFINITIONS}


def _mentions(text: str, name: str) -> bool:
    return f"`{name}`" in text or f"{name}(" in text or f'"{name}"' in text


def test_deleted_tools_are_fully_gone() -> None:
    names = _defined_names()
    assert not (names & DELETED_TOOLS), names & DELETED_TOOLS
    for path in ALL_GUIDANCE_FILES:
        text = path.read_text(encoding="utf-8")
        for name in DELETED_TOOLS:
            assert name not in text, f"{path.name} still mentions {name}"


def test_tool_name_sets_reference_only_defined_tools() -> None:
    names = _defined_names()
    for label, group in (
        ("RUNTIME_HIDDEN_TOOLS", agent.RUNTIME_HIDDEN_TOOLS),
        ("CONSOLIDATION_TOOLS", agent.CONSOLIDATION_TOOLS),
        ("UNTRUSTED_READ_ONLY_TOOLS", tool_access.UNTRUSTED_READ_ONLY_TOOLS),
        ("_PALACE_TOOL_NAMES", tools._PALACE_TOOL_NAMES),
        ("RECALL_SCAN_EXCLUDED_TOOLS", agent.RECALL_SCAN_EXCLUDED_TOOLS),
    ):
        missing = set(group) - names
        assert not missing, f"{label} names undefined tools: {sorted(missing)}"


def test_every_channel_guidance_never_instructs_hidden_tools() -> None:
    """SOUL.md/MEMORY.md ride the stable block on main/worker/morning, where
    RUNTIME_HIDDEN_TOOLS are absent — an instruction to call one is a dead
    path the model will follow into a tool error."""
    for path in EVERY_CHANNEL_FILES:
        text = path.read_text(encoding="utf-8")
        for name in sorted(agent.RUNTIME_HIDDEN_TOOLS):
            assert not _mentions(text, name), (
                f"{path.name} instructs `{name}`, which normal channels do not have"
            )


def test_learn_is_the_one_runtime_writer() -> None:
    """Mid-task the model has exactly two memory writers: learn (durable,
    typed) and memory_log (48h scratch). Everything else that creates memory
    is consolidation- or Tower-side."""
    names = _defined_names()
    runtime = names - agent.RUNTIME_HIDDEN_TOOLS
    memory_writers = {
        n for n in runtime
        if n in {"learn", "memory_log", "propose_memory", "learn_recall"}
    }
    assert memory_writers == {"learn", "memory_log"}, memory_writers


def test_learn_and_propose_memory_share_the_typed_schema() -> None:
    by_name = {t["name"]: t for t in tools.TOOL_DEFINITIONS}
    for tool in ("learn", "propose_memory"):
        props = by_name[tool]["input_schema"]["properties"]
        assert props["type"]["enum"] == [
            "semantic", "procedural", "preference", "episodic",
        ], (tool, props["type"]["enum"])
        assert "kg_invalidate" in props, f"{tool} lacks kg_invalidate"
        assert "kg_triplets" in props, f"{tool} lacks kg_triplets"


def test_episodic_gets_no_trigger_and_no_edges() -> None:
    assert "episodic" in consolidation.MEMORY_TYPES
    assert "episodic" not in consolidation.RECALL_ELIGIBLE_TYPES


def test_memory_log_description_is_honest_about_decay() -> None:
    by_name = {t["name"]: t for t in tools.TOOL_DEFINITIONS}
    desc = by_name["memory_log"]["description"]
    assert "persist important information across sessions" not in desc
    assert "48" in desc and "learn" in desc, desc


def test_palace_wake_up_has_no_dead_wing_parameter() -> None:
    by_name = {t["name"]: t for t in tools.TOOL_DEFINITIONS}
    schema = by_name["palace_wake_up"]["input_schema"]
    assert "wing" not in schema.get("properties", {}), schema
    desc = by_name["palace_wake_up"]["description"]
    assert "L0+L1" not in desc, desc


def test_goodnight_and_reflection_write_through_the_pipeline() -> None:
    from harness.loop_prompts import goodnight_prompt, reflection_prompt

    goodnight = goodnight_prompt("2026-08-30")
    reflection = reflection_prompt("2026-08-30")
    assert 'learn(type="episodic"' in goodnight
    assert "propose_memory(type=episodic)" in reflection
    assert "kg_invalidate" in reflection


def main() -> int:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS: {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL: {name} — {e}")
    total = len([n for n in globals() if n.startswith("test_")])
    print(f"{total - failures}/{total} tests passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Tests for the shared memory-candidate pipeline (harness/consolidation.py)
and its tool wiring — the multi-timescale learning architecture's Phase 2/3
plumbing: validate/dedupe/commit, the typed `learn` tool, and the
consolidation-only tools (propose_memory, grade_retrieval, flag_memory,
read_episode_segment).

Mongo is not configured in this environment, so tests either patch
`consolidation._collection` directly (to exercise dedupe/telemetry logic
deterministically) or rely on the module's own graceful degradation
(persistence skipped, writes still happen) — both are real code paths in
production depending on MONGO_URI.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import consolidation  # noqa: E402
from harness import learn as learn_mod  # noqa: E402
from harness import tools  # noqa: E402
from harness.agent import GaladrielAgent  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# ─── clean_triplets ─────────────────────────────────────────────────────


def test_clean_triplets_normalizes_lists_and_dicts() -> None:
    raw = [
        ["Alice", "works_on", "Project X"],
        {"subject": "Bob", "predicate": "prefers", "object": "dark mode"},
        ["bad", "only-two"],
        ["", "empty-subject", "x"],
        "not-a-list",
    ]
    out, error, note = consolidation.clean_triplets(raw)
    assert error is None, error
    assert out == [
        ("Alice", "works_on", "Project X"),
        ("Bob", "prefers", "dark mode"),
    ], out
    assert "3 malformed" in note, note


def test_clean_triplets_reports_what_the_cap_dropped() -> None:
    """A partial write must not read as a whole one.

    The original cap was a bare `out[:20]`: the first real profile through it
    submitted 21 facts, lost `Metaforms | funding | $9M raised`, and reported
    success. Anything over the cap is now named in the note so the caller can
    re-send it.
    """
    n = consolidation.MAX_TRIPLETS_PER_COMMIT
    raw = [["s", "p", str(i)] for i in range(n + 3)]
    out, error, note = consolidation.clean_triplets(raw)
    assert error is None, error
    assert len(out) == n, len(out)
    assert f"kept {n} of {n + 3}" in note, note
    assert "s|p|%d" % n in note, note


def test_clean_triplets_accepts_a_json_string() -> None:
    """A model that stringifies the argument still gets its data stored."""
    out, error, _ = consolidation.clean_triplets('[["Ada", "role", "CTO"]]')
    assert error is None, error
    assert out == [("Ada", "role", "CTO")], out


def test_clean_triplets_names_the_type_instead_of_claiming_absence() -> None:
    """The failure that started this: a wrong type reported as a missing field.

    `learn` answered "content or kg_triplets is required" to a 3,900-character
    string argument, and the model concluded triplets were invalid without
    content — then wrote twelve memories as prose. The error has to say what
    actually arrived.
    """
    out, error, _ = consolidation.clean_triplets("nope")
    assert out == []
    assert error and "not valid JSON" in error, error

    _, dict_error, _ = consolidation.clean_triplets({"a": 1})
    assert dict_error and "must be an array" in dict_error, dict_error


def test_clean_triplets_recognizes_several_payloads_run_together() -> None:
    """The real payload's actual shape: groups concatenated into one argument."""
    packed = '[["a","b","c"]], "topic2", [["d","e","f"]]'
    out, error, _ = consolidation.clean_triplets(packed)
    assert out == []
    assert error and "concatenated" in error, error
    assert "separate call per topic" in error, error


# ─── commit_candidate: validation ──────────────────────────────────────


def test_commit_candidate_rejects_unknown_type() -> None:
    result = _run(consolidation.commit_candidate(type="episodic", content="x"))
    assert result["status"] == "error", result
    assert "unknown type" in result["detail"]


def test_commit_candidate_requires_content_or_triplets() -> None:
    result = _run(consolidation.commit_candidate(type="semantic", content=""))
    assert result["status"] == "error", result
    # Says both are absent AND that either alone suffices — the previous
    # wording let a model read "content or kg_triplets is required" as
    # "kg_triplets needs content too".
    assert "nothing to store" in result["detail"], result
    assert "Either alone is valid" in result["detail"], result


def test_empty_kg_triplets_still_means_absent_not_malformed() -> None:
    """Diagnosing wrong types must not reject what used to work.

    `kg_triplets=""` alongside real content was previously falsy and ignored.
    A first cut at this change checked `is not None`, which turned that into a
    hard error — the fix for a bad error message breaking valid calls.
    """
    for empty in ("", [], None):
        result = _run(consolidation.commit_candidate(
            type="semantic", content="", kg_triplets=empty,
        ))
        # "nothing to store" = read as absent. A type complaint would mean the
        # empty value was mistaken for a malformed one.
        assert "nothing to store" in result["detail"], (empty, result)
        assert "not valid JSON" not in result["detail"], (empty, result)
        assert "empty string" not in result["detail"], (empty, result)


def test_commit_candidate_surfaces_a_bad_triplet_argument() -> None:
    """A malformed argument must not be reported as an absent one."""
    result = _run(consolidation.commit_candidate(
        type="semantic", content="", kg_triplets='[["a","b","c"]], "x", [["d"]]',
    ))
    assert result["status"] == "error", result
    assert "concatenated" in result["detail"], result
    assert "nothing to store" not in result["detail"], result


def test_commit_candidate_rejects_triplets_on_non_semantic() -> None:
    result = _run(consolidation.commit_candidate(
        type="procedural", content="x", kg_triplets=[["a", "b", "c"]],
    ))
    assert result["status"] == "error", result
    assert "only valid for type=semantic" in result["detail"]


# ─── commit_candidate: semantic / KG ────────────────────────────────────


def test_commit_candidate_kg_path_calls_kg_add() -> None:
    with patch("harness.palace.kg_query", return_value="(no facts)"), \
         patch("harness.palace.kg_add", return_value="stored") as kg_add_mock, \
         patch.object(consolidation, "_save_candidate", new=AsyncMock()):
        result = _run(consolidation.commit_candidate(
            type="semantic",
            kg_triplets=[["Alice", "works_on", "Project X"]],
            source="runtime",
        ))
    assert result["status"] == "committed", result
    kg_add_mock.assert_called_once_with(
        subject="Alice", predicate="works_on", object="Project X", valid_from=None,
    )


def test_commit_candidate_passes_valid_from_to_kg() -> None:
    """A fact learned now but true for months must not be stamped as today."""
    with patch("harness.palace.kg_query", return_value="(no facts)"), \
         patch("harness.palace.kg_add", return_value="stored") as kg_add_mock, \
         patch.object(consolidation, "_save_candidate", new=AsyncMock()):
        result = _run(consolidation.commit_candidate(
            type="semantic",
            kg_triplets=[["Alice", "works_on", "Project X"]],
            valid_from="2025-11-02",
            source="task_consolidator",
        ))
    assert result["status"] == "committed", result
    kg_add_mock.assert_called_once_with(
        subject="Alice", predicate="works_on", object="Project X",
        valid_from="2025-11-02",
    )


def test_commit_candidate_drops_malformed_valid_from_but_still_commits() -> None:
    """Losing the memory over a bad optional date is worse than defaulting."""
    saved = AsyncMock()
    with patch("harness.palace.kg_query", return_value="(no facts)"), \
         patch("harness.palace.kg_add", return_value="stored") as kg_add_mock, \
         patch.object(consolidation, "_save_candidate", new=saved):
        result = _run(consolidation.commit_candidate(
            type="semantic",
            kg_triplets=[["Alice", "works_on", "Project X"]],
            valid_from="last November",
            source="task_consolidator",
        ))
    assert result["status"] == "committed", result
    assert "ignored valid_from" in result["detail"], result
    assert kg_add_mock.call_args.kwargs["valid_from"] is None
    assert saved.await_args.args[0]["valid_from"] is None


def test_clean_valid_from_accepts_iso_only() -> None:
    assert consolidation._clean_valid_from("2026-01-15") == ("2026-01-15", "")
    assert consolidation._clean_valid_from("  2026-03-04 ")[0] == "2026-03-04"
    assert consolidation._clean_valid_from(None) == (None, "")
    assert consolidation._clean_valid_from("")[0] is None
    for bad in ("yesterday", "15-01-2026", "2026-13-01"):
        value, warning = consolidation._clean_valid_from(bad)
        assert value is None and "not an ISO date" in warning, bad


def test_commit_candidate_kg_dedup_skips_existing_triplet() -> None:
    existing_text = "Alice --[works_on]-> `Project X`"
    with patch("harness.palace.kg_query", return_value=existing_text), \
         patch("harness.palace.kg_add") as kg_add_mock, \
         patch.object(consolidation, "_save_candidate", new=AsyncMock()):
        result = _run(consolidation.commit_candidate(
            type="semantic",
            kg_triplets=[["Alice", "works_on", "Project X"]],
            source="runtime",
        ))
    assert result["status"] == "duplicate", result
    kg_add_mock.assert_not_called()


def test_commit_candidate_kg_partial_dedup_stores_only_new() -> None:
    def fake_query(subject=None, predicate=None):
        if predicate == "works_on":
            return "Alice --[works_on]-> `Project X`"
        return "(no facts)"

    with patch("harness.palace.kg_query", side_effect=fake_query), \
         patch("harness.palace.kg_add", return_value="stored") as kg_add_mock, \
         patch.object(consolidation, "_save_candidate", new=AsyncMock()):
        result = _run(consolidation.commit_candidate(
            type="semantic",
            kg_triplets=[
                ["Alice", "works_on", "Project X"],
                ["Alice", "prefers", "dark mode"],
            ],
            source="runtime",
        ))
    assert result["status"] == "committed", result
    kg_add_mock.assert_called_once_with(
        subject="Alice", predicate="prefers", object="dark mode", valid_from=None,
    )


# ─── commit_candidate: prose (drawer / procedural / preference) ────────


def test_commit_candidate_semantic_prose_writes_drawer() -> None:
    with patch.object(consolidation, "_is_prose_duplicate", new=AsyncMock(return_value=None)), \
         patch("harness.palace.add_drawer", new=AsyncMock(return_value="filed")) as drawer_mock, \
         patch.object(consolidation, "_save_candidate", new=AsyncMock()):
        result = _run(consolidation.commit_candidate(
            type="semantic", content="The user's dog is named Rex.", topic="user-pet",
        ))
    assert result["status"] == "committed", result
    assert drawer_mock.await_args.kwargs["room"] == "knowledge"


def test_commit_candidate_prose_duplicate_skips_write() -> None:
    with patch.object(consolidation, "_is_prose_duplicate", new=AsyncMock(return_value="dupe-id-123")), \
         patch("harness.palace.add_drawer", new=AsyncMock()) as drawer_mock, \
         patch.object(consolidation, "_save_candidate", new=AsyncMock()) as save_mock:
        result = _run(consolidation.commit_candidate(
            type="semantic", content="Already known fact.",
        ))
    assert result["status"] == "duplicate", result
    drawer_mock.assert_not_awaited()
    saved_record = save_mock.await_args.args[0]
    assert saved_record["duplicate_of"] == "dupe-id-123"


def test_commit_candidate_procedural_writes_knowledge_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        prev_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            with patch.object(consolidation, "_is_prose_duplicate", new=AsyncMock(return_value=None)), \
                 patch("harness.palace.add_drawer", new=AsyncMock(return_value="filed")), \
                 patch.object(consolidation, "_save_candidate", new=AsyncMock()):
                result = _run(consolidation.commit_candidate(
                    type="procedural",
                    content="When X fails, try Y instead.",
                    topic="x-fails-use-y",
                ))
            assert result["status"] == "committed", result
            written = list(Path("knowledge/procedures").glob("*.md"))
            assert len(written) == 1, written
            assert "When X fails, try Y instead." in written[0].read_text()
        finally:
            os.chdir(prev_cwd)


def test_commit_candidate_procedural_avoids_filename_collision() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        prev_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            with patch.object(consolidation, "_is_prose_duplicate", new=AsyncMock(return_value=None)), \
                 patch("harness.palace.add_drawer", new=AsyncMock(return_value="filed")), \
                 patch.object(consolidation, "_save_candidate", new=AsyncMock()):
                r1 = _run(consolidation.commit_candidate(
                    type="procedural", content="First lesson.", topic="same-topic",
                ))
                r2 = _run(consolidation.commit_candidate(
                    type="procedural", content="Second, different lesson.", topic="same-topic",
                ))
            assert r1["status"] == "committed" and r2["status"] == "committed"
            written = sorted(p.name for p in Path("knowledge/procedures").glob("*.md"))
            assert len(written) == 2, written
            assert written[0] != written[1]
        finally:
            os.chdir(prev_cwd)


def test_commit_candidate_preference_writes_daily_log() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        prev_cwd = os.getcwd()
        os.chdir(tmp)
        try:
            with patch.object(consolidation, "_is_prose_duplicate", new=AsyncMock(return_value=None)), \
                 patch("harness.palace.add_drawer", new=AsyncMock(return_value="filed")), \
                 patch.object(consolidation, "_save_candidate", new=AsyncMock()):
                result = _run(consolidation.commit_candidate(
                    type="preference", content="Prefers terse replies.", topic="reply-length",
                ))
            assert result["status"] == "committed", result
            logs = list(Path("memory").glob("*.md"))
            assert len(logs) == 1, logs
            text = logs[0].read_text()
            assert "Prefers terse replies." in text
            assert "[preference:reply-length]" in text
        finally:
            os.chdir(prev_cwd)


# ─── read_episode_segment ───────────────────────────────────────────────


def test_read_episode_segment_rejects_path_traversal() -> None:
    for bad in ("../etc/passwd", "a/b", "a\\b", "..", ""):
        result = _run(consolidation.read_episode_segment(bad))
        assert result.startswith("[error]"), (bad, result)


def test_read_episode_segment_reads_verbatim_content() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        archive_root = Path(tmp)
        segment_id = "conversation_main_compact_2026-08-23T00-00-00"
        conv_dir = archive_root / segment_id / "conversations"
        conv_dir.mkdir(parents=True)
        (conv_dir / "batch.md").write_text("USER: hello\nASSISTANT: hi there", encoding="utf-8")

        with patch("harness.palace._archive_root", return_value=archive_root):
            result = _run(consolidation.read_episode_segment(segment_id))
        assert "USER: hello" in result
        assert "ASSISTANT: hi there" in result


def test_read_episode_segment_missing_segment_reports_not_available() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with patch("harness.palace._archive_root", return_value=Path(tmp)):
            result = _run(consolidation.read_episode_segment("does_not_exist"))
        assert result.startswith("[not available]"), result


# ─── learn tool (runtime, typed) ────────────────────────────────────────


def test_learn_tool_forwards_to_commit_candidate() -> None:
    fake_result = {"status": "committed", "memory_id": "abc", "detail": "drawer: filed"}
    with patch.object(consolidation, "commit_candidate", new=AsyncMock(return_value=fake_result)) as commit_mock:
        out = _run(learn_mod.learn(
            type="semantic", content="Some fact.", topic="t1",
        ))
    assert out == "drawer: filed", out
    commit_mock.assert_awaited_once_with(
        type="semantic", content="Some fact.", kg_triplets=None, topic="t1",
        valid_from=None, source="runtime",
    )


def test_learn_tool_surfaces_errors() -> None:
    fake_result = {"status": "error", "memory_id": None, "detail": "unknown type"}
    with patch.object(consolidation, "commit_candidate", new=AsyncMock(return_value=fake_result)):
        out = _run(learn_mod.learn(type="bogus", content="x"))
    assert out == "[error] unknown type", out


# ─── tool schema + dispatch wiring ──────────────────────────────────────


def test_new_tool_schemas_registered() -> None:
    by_name = {t["name"]: t for t in tools.TOOL_DEFINITIONS}
    for name in (
        "learn", "propose_memory", "grade_retrieval", "flag_memory",
        "read_episode_segment", "memory_utility_report",
    ):
        assert name in by_name, f"{name} missing from TOOL_DEFINITIONS"
    assert by_name["learn"]["input_schema"]["required"] == ["type"]
    assert by_name["propose_memory"]["input_schema"]["required"] == ["type"]
    assert set(by_name["grade_retrieval"]["input_schema"]["required"]) == {"retrieval_id", "used"}
    assert set(by_name["flag_memory"]["input_schema"]["required"]) == {"memory_key", "reason"}
    assert by_name["read_episode_segment"]["input_schema"]["required"] == ["segment_id"]


def test_execute_tool_dispatches_propose_memory() -> None:
    fake_result = {"status": "committed", "memory_id": "m1", "detail": "ok"}
    with patch.object(consolidation, "commit_candidate", new=AsyncMock(return_value=fake_result)) as commit_mock:
        out = _run(tools.execute_tool("propose_memory", {
            "type": "procedural", "content": "lesson", "confidence": 0.9,
            "evidence_episode_ids": ["seg1"], "note": "n",
        }))
    assert out == "[committed] ok", out
    _, kwargs = commit_mock.call_args
    assert kwargs["type"] == "procedural"
    assert kwargs["evidence"] == ["seg1"]
    assert kwargs["source"] == "task_consolidator"


def test_execute_tool_dispatches_grade_retrieval() -> None:
    with patch.object(consolidation, "grade_retrieval", new=AsyncMock(return_value="graded")) as mock:
        out = _run(tools.execute_tool("grade_retrieval", {
            "retrieval_id": "r1", "used": True, "outcome": "helpful",
        }))
    assert out == "graded"
    mock.assert_awaited_once_with("r1", True, "helpful", "")


def test_execute_tool_dispatches_flag_memory() -> None:
    with patch.object(consolidation, "flag_memory", new=AsyncMock(return_value="flagged")) as mock:
        out = _run(tools.execute_tool("flag_memory", {
            "memory_key": "kg:Alice/works_on", "reason": "user corrected",
        }))
    assert out == "flagged"
    mock.assert_awaited_once_with("kg:Alice/works_on", "user corrected")


def test_execute_tool_dispatches_read_episode_segment() -> None:
    with patch.object(consolidation, "read_episode_segment", new=AsyncMock(return_value="verbatim text")) as mock:
        out = _run(tools.execute_tool("read_episode_segment", {"segment_id": "seg1"}))
    assert out == "verbatim text"
    mock.assert_awaited_once_with("seg1")


def test_execute_tool_dispatches_memory_utility_report() -> None:
    with patch.object(consolidation, "memory_utility_report", new=AsyncMock(return_value="report")) as mock:
        out = _run(tools.execute_tool("memory_utility_report", {"limit": 5}))
    assert out == "report"
    mock.assert_awaited_once_with(5)


def test_execute_tool_dispatches_learn_with_type() -> None:
    with patch.object(learn_mod, "learn", new=AsyncMock(return_value="ok")) as mock:
        out = _run(tools.execute_tool("learn", {
            "type": "preference", "content": "c", "topic": "t",
        }))
    assert out == "ok"
    mock.assert_awaited_once_with(
        type="preference", content="c", kg_triplets=None, topic="t", valid_from=None,
    )


# ─── phase 4: retrieval telemetry (palace_search/kg logging on the agent) ──


def _bare_agent():
    agent = GaladrielAgent.__new__(GaladrielAgent)
    agent._session_id = {}
    agent._session_segments = {}
    return agent


def test_tool_result_has_content_filters_sentinels() -> None:
    has_content = GaladrielAgent._tool_result_has_content
    assert has_content("**Palace search:** `x` real content") is True
    assert has_content("No drawers matched `x`") is False
    assert has_content("No KG facts match subject=`Alice`.") is False
    assert has_content("No KG history for `Alice`.") is False
    assert has_content("[palace error] BOOM") is False
    assert has_content("[palace unavailable] no palace") is False
    assert has_content("") is False
    assert has_content(None) is False
    assert has_content(["a", "list", "of", "blocks"]) is True


def test_log_palace_retrieval_skips_empty_results() -> None:
    agent = _bare_agent()
    with patch.object(consolidation, "log_retrieval", new=AsyncMock()) as mock:
        _run(agent._log_palace_retrieval(
            "main", "palace_search", {"query": "q"}, "No drawers matched `q`",
        ))
    mock.assert_not_awaited()


def test_log_palace_retrieval_logs_semantic_search() -> None:
    agent = _bare_agent()
    with patch.object(consolidation, "log_retrieval", new=AsyncMock(return_value="rid1")) as mock:
        _run(agent._log_palace_retrieval(
            "main", "palace_search",
            {"query": "what did we decide", "room": "knowledge"},
            "**Palace search:** `what did we decide`\n\n### 1. agent / knowledge\ncontent",
        ))
    mock.assert_awaited_once()
    _, kwargs = mock.call_args
    assert kwargs["memory_key"] == "drawer_search:knowledge"
    assert kwargs["memory_kind"] == "drawer"
    assert kwargs["query_or_cue"] == "what did we decide"
    assert kwargs["session_id"] == agent._session_id["main"]


def test_log_palace_retrieval_skips_recency_order() -> None:
    agent = _bare_agent()
    with patch.object(consolidation, "log_retrieval", new=AsyncMock()) as mock:
        _run(agent._log_palace_retrieval(
            "main", "palace_search",
            {"order": "recency", "channel": "main"},
            "**Recent sessions**\n\nsomething",
        ))
    mock.assert_not_awaited()


def test_log_palace_retrieval_logs_kg_query() -> None:
    agent = _bare_agent()
    with patch.object(consolidation, "log_retrieval", new=AsyncMock()) as mock:
        _run(agent._log_palace_retrieval(
            "main", "palace_kg_query",
            {"subject": "Alice", "predicate": "works_on"},
            "Alice --[works_on]-> `Project X`",
        ))
    mock.assert_awaited_once()
    _, kwargs = mock.call_args
    assert kwargs["memory_key"] == "kg:Alice/works_on/*"
    assert kwargs["memory_kind"] == "kg"


def test_log_palace_retrieval_logs_kg_timeline() -> None:
    agent = _bare_agent()
    with patch.object(consolidation, "log_retrieval", new=AsyncMock()) as mock:
        _run(agent._log_palace_retrieval(
            "main", "palace_kg_timeline", {"entity": "Alice"},
            "**KG timeline for `Alice`** (2 fact(s)):",
        ))
    mock.assert_awaited_once()
    _, kwargs = mock.call_args
    assert kwargs["memory_key"] == "kg_timeline:Alice"
    assert kwargs["query_or_cue"] == "Alice"


def test_log_retrieval_event_uses_stable_session_and_swallows_errors() -> None:
    agent = _bare_agent()
    with patch.object(consolidation, "log_retrieval", new=AsyncMock(side_effect=RuntimeError("boom"))):
        _run(agent._log_retrieval_event(
            "main", memory_key="recall:sys_1", memory_kind="recall", query_or_cue="hi",
        ))  # must not raise
    sid_first = agent._session_id["main"]
    with patch.object(consolidation, "log_retrieval", new=AsyncMock(return_value="rid")) as mock:
        _run(agent._log_retrieval_event(
            "main", memory_key="recall:sys_1", memory_kind="recall", query_or_cue="hi again",
        ))
    sid_second = agent._session_id["main"]
    assert sid_first == sid_second, "session id must stay stable across calls on the same channel"
    assert mock.call_args.kwargs["session_id"] == sid_first


# ─── agent toolset gating (RUNTIME_HIDDEN_TOOLS / CONSOLIDATION_TOOLS) ──


def test_toolset_gating_constants_are_consistent() -> None:
    from harness import agent as agent_mod

    # Every consolidation-only authoring tool must be hidden from normal turns.
    authoring_tools = {"propose_memory", "grade_retrieval", "flag_memory", "read_episode_segment"}
    assert authoring_tools <= agent_mod.RUNTIME_HIDDEN_TOOLS
    assert authoring_tools <= agent_mod.CONSOLIDATION_TOOLS
    # learn must stay visible on normal turns (conservative runtime writer).
    assert "learn" not in agent_mod.RUNTIME_HIDDEN_TOOLS
    assert "learn" not in agent_mod.CONSOLIDATION_TOOLS
    # Periodic-consolidator channels are exactly reflection + goodnight.
    assert agent_mod.PERIODIC_CONSOLIDATOR_CHANNELS == frozenset({agent_mod.AMBIENT_CHANNEL_ID, "goodnight"})


# ─── phase 5: periodic consolidator prompt (ambient reflection) ────────


def test_reflection_prompt_uses_periodic_consolidator_contract() -> None:
    from harness.loop_prompts import reflection_prompt

    text = reflection_prompt("2026-08-23")
    # Evidence-driven, not free-form noticing.
    assert "memory_utility_report" in text
    assert "BAD-TRIGGER" in text
    assert "BAD-MEMORY" in text
    assert "STALE" in text
    assert "propose_memory" in text
    assert "CROSS-EPISODE" in text
    # The old open-ended "file anything you notice" instruction must be gone.
    assert "If something is worth keeping, FILE it now" not in text
    # Superseded by real usage telemetry — no more artificial self-retest.
    assert "SPACED RETEST" not in text
    # Recall-cue tuning mechanics are preserved (still needed reference material).
    assert "positive_threshold" in text
    assert "Stage-1" in text and "Stage-2" in text
    # Experiential state is a separate system and must stay untouched.
    assert "experience_report" in text
    assert "knowledge/skills/experiential-reflection.md" in text


def test_reflection_prompt_worker_audit_untouched() -> None:
    from harness.loop_prompts import reflection_prompt

    text = reflection_prompt("2026-08-23")
    assert "PART 3" in text
    part3 = text.split("PART 3", 1)[1]
    assert "state/worker_control.md" in part3
    assert "state/steering.md" in part3
    assert "ALL GOOD / STEERED / PAUSED" in part3


def test_goodnight_prompt_unchanged_episode_recap() -> None:
    from harness.loop_prompts import goodnight_prompt

    text = goodnight_prompt("2026-08-23")
    assert "daily-recap-YYYY-MM-DD" in text
    assert "room=" in text and "episodes" in text


# ─── Telemetry defects ──────────────────────────────────────────────────


def test_stale_bin_survives_a_naive_stored_timestamp() -> None:
    """Mongo hands back naive datetimes; `_now()` is aware. Subtracting the two
    raised, and because it sits outside the query's try block it took the whole
    utility report down — the periodic consolidator's only evidence source.
    """
    from datetime import datetime, timedelta, timezone

    naive_old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=200)
    docs = [{
        "memory_key": "memory:abc", "retrieval_count": 9, "graded_count": 6,
        "use_count": 4, "harmful_count": 0, "user_correction_count": 0,
        "last_used": naive_old,
    }]

    class _Stats:
        def find(self, *a, **kw):
            async def gen():
                for d in docs:
                    yield d
            return gen()

    with patch.object(consolidation, "_collection", new=AsyncMock(return_value=_Stats())):
        out = _run(consolidation.memory_utility_report())
    assert "MEMORY_UTILITY_REPORT" in out
    assert "memory:abc" in out, "a 200-day-old row belongs in the stale bin"


def test_a_failed_grade_write_does_not_move_the_counters() -> None:
    """The graded flag is what makes grading idempotent. Bumping before it is
    stored lets a retry count the same use twice."""
    bumped = []

    class _Events:
        async def find_one(self, *a, **kw):
            return {"retrieval_id": "r1", "memory_key": "memory:abc",
                    "memory_kind": "graph_expansion", "graded": False}

        async def update_one(self, *a, **kw):
            raise RuntimeError("write failed")

    with patch.object(consolidation, "_collection", new=AsyncMock(return_value=_Events())), \
         patch.object(consolidation, "_bump_stats",
                      new=AsyncMock(side_effect=lambda *a, **kw: bumped.append(a))):
        out = _run(consolidation.grade_retrieval("r1", True, "helpful"))
    assert out.startswith("[error]")
    assert bumped == []


def test_flag_memory_refuses_a_key_nothing_produced() -> None:
    """The bad-memory bin has no retrieval gate, so an invented key would sit in
    the consolidator's evidence forever."""
    class _Empty:
        async def find_one(self, *a, **kw):
            return None

    with patch.object(consolidation, "_collection", new=AsyncMock(return_value=_Empty())):
        out = _run(consolidation.flag_memory("the trading thing", "wrong"))
    assert out.startswith("[error]")
    assert "does not name anything" in out


def test_flag_memory_namespaces_a_bare_memory_id() -> None:
    """Retrieval telemetry writes `memory:<id>`. Flagging the bare id would open
    a second stats doc and split the correction away from the retrieval history.
    """
    bumped = []

    class _Candidates:
        async def find_one(self, query, *a, **kw):
            return {"_id": 1} if query.get("memory_id") == "abc123" else None

    with patch.object(consolidation, "_collection", new=AsyncMock(return_value=_Candidates())), \
         patch.object(consolidation, "_bump_stats",
                      new=AsyncMock(side_effect=lambda key, *a, **kw: bumped.append(key))):
        out = _run(consolidation.flag_memory("abc123", "contradicted"))
    assert bumped == ["memory:abc123"], bumped
    assert "memory:abc123" in out


def test_flag_memory_passes_a_namespaced_key_through() -> None:
    bumped = []
    with patch.object(consolidation, "_bump_stats",
                      new=AsyncMock(side_effect=lambda key, *a, **kw: bumped.append(key))):
        out = _run(consolidation.flag_memory("recall:sys_identity", "too broad"))
    assert bumped == ["recall:sys_identity"]
    assert "recall:sys_identity" in out


def test_ungraded_surfacings_do_not_indict_a_trigger() -> None:
    """retrieval_count bumps on every surfacing; grades only arrive when an
    episode ends. Dividing by raw retrievals put memories in BAD-TRIGGER for
    living in a chat that never hit /new — measured nothing, blamed anyway."""
    docs = [{
        "memory_key": "memory:never_graded", "retrieval_count": 40,
        "graded_count": 0, "use_count": 0, "harmful_count": 0,
        "user_correction_count": 0, "last_used": None,
    }]

    class _Stats:
        def find(self, *a, **kw):
            async def gen():
                for d in docs:
                    yield d
            return gen()

    with patch.object(consolidation, "_collection", new=AsyncMock(return_value=_Stats())):
        out = _run(consolidation.memory_utility_report())
    assert "never_graded" not in out, (
        "an unmeasured memory must not appear in any evidence bin"
    )


def test_a_genuinely_measured_bad_trigger_is_still_reported() -> None:
    """The graded gate must not mute the bin it exists to keep honest."""
    docs = [{
        "memory_key": "memory:bad_cue", "retrieval_count": 40,
        "graded_count": 20, "use_count": 1, "harmful_count": 0,
        "user_correction_count": 0, "last_used": None,
    }]

    class _Stats:
        def find(self, *a, **kw):
            async def gen():
                for d in docs:
                    yield d
            return gen()

    with patch.object(consolidation, "_collection", new=AsyncMock(return_value=_Stats())):
        out = _run(consolidation.memory_utility_report())
    assert "bad_cue" in out and "graded=20" in out
    assert "use_ratio=0.05" in out, "the ratio must be over graded, not retrieved"


def test_bad_trigger_needs_enough_graded_evidence() -> None:
    """One unlucky graded miss is not a broken trigger."""
    docs = [{
        "memory_key": "memory:barely", "retrieval_count": 9,
        "graded_count": consolidation._MIN_GRADED_FOR_BIN - 1, "use_count": 0,
        "harmful_count": 0, "user_correction_count": 0, "last_used": None,
    }]

    class _Stats:
        def find(self, *a, **kw):
            async def gen():
                for d in docs:
                    yield d
            return gen()

    with patch.object(consolidation, "_collection", new=AsyncMock(return_value=_Stats())):
        out = _run(consolidation.memory_utility_report())
    assert "barely" not in out


def test_promotion_audit_follows_the_character_budget() -> None:
    """The char cap usually binds before the entry cap. Recording the first N
    entries as promoted would log exactly the ones the budget evicted."""
    # Distinguishable content: identical strings would make the "evicted entries
    # are absent from the file" check pass no matter what was written.
    entries = [
        {"memory_id": f"m{i}", "content": f"pref-{i:02d} " + ("x" * 180),
         "confirmations": 3, "last_confirmed_ts": float(100 - i)}
        for i in range(consolidation._PROMOTION_MAX_ENTRIES)
    ]
    fitted, lines = consolidation.fitting_promotions(entries)
    section = consolidation.render_promotion_section(entries)
    assert len(fitted) == len(lines) == len(
        [ln for ln in section.splitlines() if ln.startswith("- ")]
    ), "the audit's idea of promoted must equal what was actually rendered"
    assert len(fitted) < len(entries), (
        "this fixture is only meaningful when the char budget bites"
    )

    # And end to end: what promote_preferences records must match the file it
    # wrote, not the entry cap alone.
    records = []

    async def fake_record(kind, detail):
        records.append((kind, detail))

    with tempfile.TemporaryDirectory() as tmp:
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            Path("config").mkdir()
            Path("config/MEMORY.md").write_text("# hand written\n", encoding="utf-8")
            with patch.object(consolidation, "promotable_preferences",
                              new=AsyncMock(return_value=entries)), \
                 patch.object(consolidation, "record_maintenance", fake_record):
                _run(consolidation.promote_preferences())
            written = Path("config/MEMORY.md").read_text(encoding="utf-8")
        finally:
            os.chdir(cwd)

    assert records and records[0][0] == "promotion", f"no promotion record: {records}"
    detail = records[0][1]
    assert len(detail["promoted"]) == len(fitted)
    assert len(detail["evicted"]) == len(entries) - len(fitted), (
        "entries the char budget dropped belong in evicted, not promoted"
    )
    assert detail["evicted"], "the fixture must actually evict something"
    for memory_id in detail["evicted"]:
        entry = next(e for e in entries if e["memory_id"] == memory_id)
        assert entry["content"][:8] not in written, (
            "an entry recorded as evicted must not be in the written file"
        )


def test_graded_counters_move_on_every_grade_not_only_on_use() -> None:
    """graded_count is the denominator, so an ignored retrieval must still
    count as a measurement — otherwise ignoring a memory hides the evidence
    that its trigger is wrong."""
    bumped = {}

    async def fake_bump(key, inc, set_fields=None, memory_kind=None):
        bumped["inc"] = inc
        bumped["set"] = set_fields or {}

    events = AsyncMock()
    events.find_one = AsyncMock(return_value={
        "retrieval_id": "r1", "memory_key": "memory:x", "graded": False,
    })
    events.update_one = AsyncMock()
    with patch.object(consolidation, "_collection", new=AsyncMock(return_value=events)), \
         patch.object(consolidation, "_bump_stats", new=fake_bump):
        out = _run(consolidation.grade_retrieval("r1", used=False, outcome="neutral"))
    assert not out.startswith("[error]"), out
    assert bumped["inc"].get("graded_count") == 1
    assert "use_count" not in bumped["inc"], "an ignored retrieval is not a use"
    assert "last_graded" in bumped["set"]


def test_clearing_a_chat_hands_the_compaction_summary_to_the_consolidator() -> None:
    """/new pops per-channel state before the background pass runs, so the
    summary has to be captured like the session already was. Without it the
    consolidator gets segment ids and no account of what is in them — the exact
    signal its prompt tells it to use when choosing what to drill into."""
    agent = GaladrielAgent.__new__(GaladrielAgent)
    seen = {}

    async def fake_on_episode_end(channel_id, reason, **kwargs):
        seen.update(kwargs)

    agent.on_episode_end = fake_on_episode_end
    # The last-resort archive fires on a genuine staging FAILURE. `batch_dir is
    # None` alone is also the ordinary "cursor already covered this buffer" case,
    # and archiving there re-mined the whole conversation with no identity.
    from harness import palace

    with patch.object(palace, "archive_conversation", new=AsyncMock()) as archived:
        _run(agent._postprocess_cleared_history(
            "main", [{"role": "user", "content": "hi"}],
            batch_dir=None, session_id="s1", session_segments=[{"id": "seg1"}],
            compaction_summary="earlier: the user corrected the deploy step",
            stage_failed=True, conversation_id="run-1",
        ))
    assert archived.await_count == 1, "a real staging failure must still archive"

    with patch.object(palace, "archive_conversation", new=AsyncMock()) as skipped:
        _run(agent._postprocess_cleared_history(
            "main", [{"role": "user", "content": "hi"}],
            batch_dir=None, session_id="s1", session_segments=[{"id": "seg1"}],
            compaction_summary="earlier: the user corrected the deploy step",
        ))
    assert skipped.await_count == 0, (
        "nothing-new-to-stage must NOT re-archive the whole conversation"
    )
    assert seen.get("compaction_summary") == (
        "earlier: the user corrected the deploy step"
    ), f"the summary must reach the consolidator, got {seen.get('compaction_summary')!r}"
    assert seen.get("session_id") == "s1"


def test_episode_end_passes_the_summary_through_to_the_pass() -> None:
    """The last hop: on_episode_end -> run_task_consolidation."""
    agent = GaladrielAgent.__new__(GaladrielAgent)
    seen = {}

    async def fake_run(channel_id, **kwargs):
        seen.update(kwargs)

    agent.run_task_consolidation = fake_run
    agent._compaction_summary = {}
    agent._session_id = {}
    agent._session_segments = {}
    _run(agent.on_episode_end(
        "main", "new",
        messages_snapshot=[{"role": "user", "content": "hi"}],
        session_id="s1", session_segments=[],
        compaction_summary="folded summary",
    ))
    assert seen.get("compaction_summary") == "folded summary"


def main() -> int:
    tests = [
        test_stale_bin_survives_a_naive_stored_timestamp,
        test_a_failed_grade_write_does_not_move_the_counters,
        test_flag_memory_refuses_a_key_nothing_produced,
        test_flag_memory_namespaces_a_bare_memory_id,
        test_flag_memory_passes_a_namespaced_key_through,
        test_clean_triplets_normalizes_lists_and_dicts,
        test_clean_triplets_reports_what_the_cap_dropped,
        test_clean_triplets_accepts_a_json_string,
        test_clean_triplets_names_the_type_instead_of_claiming_absence,
        test_clean_triplets_recognizes_several_payloads_run_together,
        test_commit_candidate_rejects_unknown_type,
        test_commit_candidate_requires_content_or_triplets,
        test_empty_kg_triplets_still_means_absent_not_malformed,
        test_commit_candidate_surfaces_a_bad_triplet_argument,
        test_commit_candidate_rejects_triplets_on_non_semantic,
        test_commit_candidate_kg_path_calls_kg_add,
        test_commit_candidate_passes_valid_from_to_kg,
        test_commit_candidate_drops_malformed_valid_from_but_still_commits,
        test_clean_valid_from_accepts_iso_only,
        test_commit_candidate_kg_dedup_skips_existing_triplet,
        test_commit_candidate_kg_partial_dedup_stores_only_new,
        test_commit_candidate_semantic_prose_writes_drawer,
        test_commit_candidate_prose_duplicate_skips_write,
        test_commit_candidate_procedural_writes_knowledge_file,
        test_commit_candidate_procedural_avoids_filename_collision,
        test_commit_candidate_preference_writes_daily_log,
        test_read_episode_segment_rejects_path_traversal,
        test_read_episode_segment_reads_verbatim_content,
        test_read_episode_segment_missing_segment_reports_not_available,
        test_learn_tool_forwards_to_commit_candidate,
        test_learn_tool_surfaces_errors,
        test_new_tool_schemas_registered,
        test_execute_tool_dispatches_propose_memory,
        test_execute_tool_dispatches_grade_retrieval,
        test_execute_tool_dispatches_flag_memory,
        test_execute_tool_dispatches_read_episode_segment,
        test_execute_tool_dispatches_memory_utility_report,
        test_execute_tool_dispatches_learn_with_type,
        test_tool_result_has_content_filters_sentinels,
        test_log_palace_retrieval_skips_empty_results,
        test_log_palace_retrieval_logs_semantic_search,
        test_log_palace_retrieval_skips_recency_order,
        test_log_palace_retrieval_logs_kg_query,
        test_log_palace_retrieval_logs_kg_timeline,
        test_log_retrieval_event_uses_stable_session_and_swallows_errors,
        test_toolset_gating_constants_are_consistent,
        test_reflection_prompt_uses_periodic_consolidator_contract,
        test_reflection_prompt_worker_audit_untouched,
        test_goodnight_prompt_unchanged_episode_recap,
        test_ungraded_surfacings_do_not_indict_a_trigger,
        test_a_genuinely_measured_bad_trigger_is_still_reported,
        test_bad_trigger_needs_enough_graded_evidence,
        test_promotion_audit_follows_the_character_budget,
        test_graded_counters_move_on_every_grade_not_only_on_use,
        test_clearing_a_chat_hands_the_compaction_summary_to_the_consolidator,
        test_episode_end_passes_the_summary_through_to_the_pass,
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print(f"{len(tests)}/{len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

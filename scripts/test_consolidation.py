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
    result = _run(consolidation.commit_candidate(type="autobiographical", content="x"))
    assert result["status"] == "error", result
    assert "unknown type" in result["detail"]


def test_commit_candidate_requires_content_or_triplets() -> None:
    result = _run(consolidation.commit_candidate(type="semantic", content=""))
    assert result["status"] == "error", result
    # Says all are absent AND that any one alone suffices — the previous
    # wording let a model read "content or kg_triplets is required" as
    # "kg_triplets needs content too".
    assert "nothing to store" in result["detail"], result
    assert "Any one alone is valid" in result["detail"], result


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
    with patch("harness.palace.kg_fact_is_current", return_value=False), \
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
    with patch("harness.palace.kg_fact_is_current", return_value=False), \
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
    with patch("harness.palace.kg_fact_is_current", return_value=False), \
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
    with patch("harness.palace.kg_fact_is_current", return_value=True), \
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
    def fake_current(subject, predicate, obj):
        return predicate == "works_on"

    with patch("harness.palace.kg_fact_is_current", side_effect=fake_current), \
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


def test_commit_candidate_episodic_writes_episodes_drawer_without_packaging() -> None:
    """type=episodic lands in room=episodes and schedules NEITHER a recall
    trigger NOR edge classification — one gate covers both post-commit passes."""
    with patch.object(consolidation, "_is_prose_duplicate", new=AsyncMock(return_value=None)), \
         patch("harness.palace.add_drawer", new=AsyncMock(return_value="filed")) as drawer_mock, \
         patch.object(consolidation, "_save_candidate", new=AsyncMock()), \
         patch.object(consolidation, "_spawn") as spawn_mock:
        result = _run(consolidation.commit_candidate(
            type="episodic", content="Shipped the report; the retry loop was the fix.",
            topic="daily-recap",
        ))
    assert result["status"] == "committed", result
    assert drawer_mock.await_args.kwargs["room"] == "episodes"
    spawn_mock.assert_not_called()


def test_commit_candidate_kg_only_renders_content_for_search() -> None:
    """A KG-only memory stores rendered triplet prose on the candidate, so
    memory(query=…) and the edge shortlist can find it — empty content made a
    third of the live corpus invisible to semantic search."""
    saved = AsyncMock()
    with patch("harness.palace.kg_fact_is_current", return_value=False), \
         patch("harness.palace.kg_add", return_value="stored"), \
         patch.object(consolidation, "_save_candidate", new=saved):
        result = _run(consolidation.commit_candidate(
            type="semantic", kg_triplets=[["Ada", "role", "CTO"]],
        ))
    assert result["status"] == "committed", result
    record = saved.await_args.args[0]
    assert record["content"] == "Ada — role — CTO", record["content"]


def test_commit_candidate_kg_invalidate_retires_before_adding() -> None:
    """A fact change is one call: the stale triple is retired FIRST, then the
    replacement added, so both never read as current between the writes."""
    calls: list[tuple] = []
    with patch("harness.palace.kg_fact_is_current", return_value=False), \
         patch("harness.palace.kg_invalidate",
               side_effect=lambda **kw: calls.append(("invalidate", kw)) or "KG: invalidated"), \
         patch("harness.palace.kg_add",
               side_effect=lambda **kw: calls.append(("add", kw)) or "stored"), \
         patch.object(consolidation, "_save_candidate", new=AsyncMock()) as saved:
        result = _run(consolidation.commit_candidate(
            type="semantic",
            kg_invalidate=[["Ada", "role", "CTO"]],
            kg_triplets=[["Ada", "role", "CEO"]],
        ))
    assert result["status"] == "committed", result
    assert [c[0] for c in calls] == ["invalidate", "add"], calls
    assert calls[0][1]["object"] == "CTO"
    assert calls[1][1]["object"] == "CEO"
    assert "retired 1" in result["detail"], result
    record = saved.await_args.args[0]
    assert record["kg_invalidated"] == [["Ada", "role", "CTO"]], record


def test_commit_candidate_kg_invalidate_alone_is_a_commit() -> None:
    """Retiring a fact with no replacement is a legitimate change to the store
    — committed for provenance, but never given a recall trigger or edges."""
    with patch("harness.palace.kg_invalidate", return_value="KG: invalidated `a`") as inv_mock, \
         patch("harness.palace.kg_add") as add_mock, \
         patch.object(consolidation, "_save_candidate", new=AsyncMock()), \
         patch.object(consolidation, "_spawn") as spawn_mock:
        result = _run(consolidation.commit_candidate(
            type="semantic", kg_invalidate=[["Ada", "role", "CTO"]],
        ))
    assert result["status"] == "committed", result
    inv_mock.assert_called_once()
    add_mock.assert_not_called()
    spawn_mock.assert_not_called()


def test_commit_candidate_rejects_invalidate_on_non_semantic() -> None:
    result = _run(consolidation.commit_candidate(
        type="procedural", content="x", kg_invalidate=[["a", "b", "c"]],
    ))
    assert result["status"] == "error", result
    assert "only valid for type=semantic" in result["detail"]


def test_commit_candidate_missed_invalidation_is_not_reported_as_retired() -> None:
    """kg_invalidate on a triple with no open row must not claim success —
    counting the attempt as a retirement told the caller a stale fact was
    gone while it stayed current."""
    with patch("harness.palace.kg_invalidate",
               return_value="KG: nothing open to invalidate for `a` --[b]-> `c`"), \
         patch("harness.palace.kg_add") as add_mock, \
         patch.object(consolidation, "_save_candidate", new=AsyncMock()), \
         patch.object(consolidation, "_spawn") as spawn_mock:
        result = _run(consolidation.commit_candidate(
            type="semantic", kg_invalidate=[["a", "b", "c"]],
        ))
    assert result["status"] == "error", result
    assert "not retired" in result["detail"], result
    assert "a|b|c" in result["detail"], result
    add_mock.assert_not_called()
    spawn_mock.assert_not_called()


def test_commit_candidate_threads_ended_to_kg_invalidate() -> None:
    """Retiring a fact that stopped being true in the past must backdate
    valid_to, or temporal as-of queries keep returning it as in force."""
    with patch("harness.palace.kg_invalidate",
               return_value="KG: invalidated `a`") as inv_mock, \
         patch.object(consolidation, "_save_candidate", new=AsyncMock()), \
         patch.object(consolidation, "_spawn"):
        result = _run(consolidation.commit_candidate(
            type="semantic", kg_invalidate=[["a", "b", "c"]], ended="2026-08-01",
        ))
    assert result["status"] == "committed", result
    assert inv_mock.call_args.kwargs["ended"] == "2026-08-01"


def test_commit_candidate_expired_fact_is_readdable() -> None:
    """The invalidate-then-re-add fact-change flow: dedupe checks CURRENT rows
    only, so a triple that existed before but was retired stores again —
    matching history here retired the old fact and then dropped its
    replacement as 'already known', leaving no current fact at all."""
    with patch("harness.palace.kg_fact_is_current", return_value=False) as cur_mock, \
         patch("harness.palace.kg_invalidate",
               return_value="KG: invalidated `Ada`"), \
         patch("harness.palace.kg_add", return_value="stored") as add_mock, \
         patch.object(consolidation, "_save_candidate", new=AsyncMock()), \
         patch.object(consolidation, "_spawn"):
        result = _run(consolidation.commit_candidate(
            type="semantic",
            kg_invalidate=[["Ada", "city", "London"]],
            kg_triplets=[["Ada", "city", "Berlin"]],
        ))
    assert result["status"] == "committed", result
    assert "stored 1" in result["detail"], result
    add_mock.assert_called_once()
    cur_mock.assert_called_once_with("Ada", "city", "Berlin")


def test_commit_candidate_reports_malformed_invalidations() -> None:
    """A partial retirement must not read as a whole one — malformed
    kg_invalidate entries are named in the detail, like adds already were."""
    with patch("harness.palace.kg_invalidate",
               return_value="KG: invalidated `Bob`"), \
         patch.object(consolidation, "_save_candidate", new=AsyncMock()), \
         patch.object(consolidation, "_spawn"):
        result = _run(consolidation.commit_candidate(
            type="semantic",
            kg_invalidate=[["Ada", "employer"], ["Bob", "city", "Pune"]],
        ))
    assert result["status"] == "committed", result
    assert "kg_invalidate:" in result["detail"], result
    assert "malformed" in result["detail"], result


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


class _PoolColl:
    """Fake memory_candidates for the two paths that rank a committed pool —
    `find(...).sort(...).limit(...)`, async-iterated."""

    def __init__(self, docs: list[dict]):
        self._docs = docs

    def find(self, query=None, projection=None):
        rows = list(self._docs)

        class _Cursor:
            def sort(self, *a, **k): return self
            def limit(self, n): return self
            def __aiter__(self):
                async def gen():
                    for row in rows:
                        yield row
                return gen()
        return _Cursor()


def test_dedupe_skips_only_identical_text() -> None:
    """Commit-time dedupe SKIPS the write, so it may only fire where skipping
    loses nothing — identical prose, and nothing else.

    Measured on the live corpus (kb/learning-loop-measured.md): every cosine
    floor low enough to catch a paraphrase also drops distinct documents (at
    0.88, three of nineteen — a personal profile vs an employer list at 0.899),
    and the one high enough to be safe never fires at all. Rewordings are the
    edge classifier's `restates` verdict, which records the restatement
    WITHOUT discarding the new write.
    """
    stored = "Always reply in one short paragraph."
    coll = _PoolColl([{"memory_id": "m1", "content": stored}])
    cases = {
        stored: "m1",                                        # identical
        "  ALWAYS   reply in one short paragraph. ": "m1",   # same text, normalized
        "Keep replies to a single short paragraph.": None,   # paraphrase -> classifier
        "Always reply in one short paragraph, unless asked for detail.": None,
    }
    for content, expected in cases.items():
        with patch.object(consolidation, "_collection",
                          new=AsyncMock(return_value=coll)):
            got = _run(consolidation._is_prose_duplicate("preference", content))
        assert got == expected, (content, got, expected)


def test_dedupe_never_calls_the_encoder() -> None:
    """No embedding, no threshold: the whole class of miscalibration is gone,
    and the commit path drops an encode it used to pay for on every write."""
    coll = _PoolColl([{"memory_id": "m1", "content": "stored"}])
    with patch.object(consolidation, "_collection", new=AsyncMock(return_value=coll)), \
         patch("harness.recall.get_encoder",
               side_effect=AssertionError("dedupe must not embed")):
        assert _run(consolidation._is_prose_duplicate("preference", "stored")) == "m1"


def test_shortlist_carries_the_cosine_score() -> None:
    """Similarity picks who gets *considered*; the number is what lets the
    classifier (and the searching agent) refuse a 0.55 'nearest neighbour'."""
    coll = _PoolColl([
        {"memory_id": "m1", "content": "aligned", "type": "semantic"},
        {"memory_id": "m2", "content": "orthogonal", "type": "semantic"},
    ])
    with patch.object(consolidation, "_collection", new=AsyncMock(return_value=coll)), \
         patch("harness.recall.get_encoder", return_value=object()), \
         patch.object(consolidation, "_encode", new=AsyncMock(return_value=[[1.0, 0.0]])), \
         patch.object(consolidation, "_pool_vectors",
                      new=AsyncMock(return_value=[[1.0, 0.0], [0.0, 1.0]])):
        out = _run(consolidation.shortlist_neighbours("query", limit=2))
    assert [n["memory_id"] for n in out] == ["m1", "m2"], out
    assert out[0]["score"] == 1.0, out
    assert out[1]["score"] == 0.0, out


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


def test_read_episode_segment_prefers_the_database() -> None:
    """The DB is the record of truth; the staged .md is crash-safety scaffolding
    that mine_pending_shutdown_archives deletes once mined."""
    from harness import palace

    with patch.object(palace, "segment_text", return_value="## user\n\nfrom the db"):
        result = _run(consolidation.read_episode_segment("conversation_main_compact_x"))
    assert "from the db" in result, result


def test_read_episode_segment_falls_back_to_disk_when_the_db_is_down() -> None:
    """An unreachable database must degrade to the staged file, not abort."""
    from harness import palace

    with tempfile.TemporaryDirectory() as tmp:
        archive_root = Path(tmp)
        segment_id = "conversation_main_compact_2026-08-23T00-00-00"
        conv_dir = archive_root / segment_id / "conversations"
        conv_dir.mkdir(parents=True)
        (conv_dir / "batch.md").write_text("USER: hello", encoding="utf-8")
        with patch.object(palace, "_archive_root", return_value=archive_root), \
             patch.object(palace, "segment_text", side_effect=RuntimeError("no mongo")):
            result = _run(consolidation.read_episode_segment(segment_id))
    assert "USER: hello" in result, result


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
        type="semantic", content="Some fact.", kg_triplets=None,
        kg_invalidate=None, topic="t1",
        valid_from=None, ended=None, source="runtime",
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
        type="preference", content="c", kg_triplets=None, kg_invalidate=None,
        topic="t", valid_from=None, ended=None,
    )


# ─── phase 4: retrieval telemetry (palace_search/kg logging on the agent) ──


def _bare_agent():
    agent = GaladrielAgent.__new__(GaladrielAgent)
    agent._session_id = {}
    agent._session_segments = {}
    return agent


def test_a_learning_episode_survives_a_restart() -> None:
    """A restart mid-conversation must resume the SAME episode.

    The session id is what distinct-session promotion counting joins on, so a
    fresh id for the second half of one chat lets a single preference confirm
    itself twice. The segment list is what lets the consolidator drill into
    content already folded out of the live buffer, so losing it blinds the
    episode index to everything archived before the restart.
    """
    from harness import conversation_run_store as store

    disk: dict[str, dict] = {}

    def fake_save(channel_id, session_id, segments):
        disk[channel_id] = {"session_id": session_id, "segments": list(segments)}

    def fake_load():
        return {k: {"session_id": v["session_id"], "segments": list(v["segments"])}
                for k, v in disk.items()}

    def fake_clear(channel_id):
        disk.pop(channel_id, None)

    with patch.object(store, "save_session_state", fake_save), \
         patch.object(store, "load_session_states", fake_load), \
         patch.object(store, "clear_session_state", fake_clear):
        before = _bare_agent()
        sid = before._session_for("main")
        # Persisted at MINT, not first compaction: a short chat that restarts
        # before it ever compacts must still resume its episode.
        assert disk["main"] == {"session_id": sid, "segments": []}, disk
        before._record_session_segment("main", None, kind="compaction",
                                       message_count=7)
        assert len(disk["main"]["segments"]) == 1, disk

        # ── restart ── a brand-new process resumes from the durable record.
        after = _bare_agent()
        after._resume_sessions()
        assert after._session_for("main") == sid, "restart split one chat in two"
        assert len(after._session_segments["main"]) == 1, after._session_segments

        # A second compaction after the restart extends the SAME episode.
        after._record_session_segment("main", None, kind="compaction",
                                      message_count=3)
        assert len(disk["main"]["segments"]) == 2, disk

        # The episode ends through the real boundary -> the record goes with
        # it, and the next conversation is its own episode.
        after.conversations = {}
        after._compaction_summary = {}
        with patch.object(GaladrielAgent, "run_task_consolidation",
                          new=AsyncMock()) as consolidate:
            _run(after.on_episode_end("main", reason="worked"))
        consolidate.assert_awaited_once()
        assert "main" not in disk, disk

        fresh = _bare_agent()
        fresh._resume_sessions()
        assert fresh._session_for("main") != sid, "a finished episode was resumed"


def test_only_recent_non_disposable_episodes_are_resumable() -> None:
    """The record is a restart bridge, not an archive.

    A day-old row is not an occasion anyone is continuing, and a consolidation
    side channel is disposable by construction — adopting either would attach
    fresh turns to an episode that is over. Both are dropped by
    `load_session_states` against a real collection shape.
    """
    from harness import conversation_run_store as store
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    rows = [
        {"_id": "main", "session_id": "live", "segments": [],
         "updated_at": now - timedelta(minutes=5)},
        {"_id": "worker", "session_id": "ancient", "segments": [],
         "updated_at": now - timedelta(hours=store.SESSION_MAX_AGE_HOURS + 1)},
        {"_id": "__consolidate_main_abc123", "session_id": "disposable",
         "segments": [], "updated_at": now},
    ]

    class _Sessions:
        def __init__(self, docs): self.docs = docs
        def delete_many(self, query):
            cutoff = query["updated_at"]["$lt"]
            self.docs = [d for d in self.docs if d["updated_at"] >= cutoff]
        def find(self, query): return list(self.docs)

    sessions = _Sessions(rows)
    with patch.object(store, "_sync_db", return_value={store.SESSIONS: sessions}):
        loaded = store.load_session_states()
    assert list(loaded) == ["main"], loaded
    assert loaded["main"]["session_id"] == "live"
    # The stale row is pruned, not just filtered — the collection cannot grow
    # a tail of abandoned episodes.
    assert [d["_id"] for d in sessions.docs] == ["main", "__consolidate_main_abc123"]


def test_resuming_sessions_survives_a_dead_store() -> None:
    """Telemetry must never block construction: if the record cannot be read,
    the agent starts with no episode rather than failing to start."""
    from harness import conversation_run_store as store

    with patch.object(store, "load_session_states",
                      side_effect=RuntimeError("mongo down")):
        agent = _bare_agent()
        agent._resume_sessions()  # must not raise
    assert agent._session_id == {}


# ─── record_unopened_fires ─────────────────────────────────────────────


class _EventsColl:
    """Fake retrieval_events: dispatches on the query's memory_kind the way
    record_unopened_fires actually asks — one bounded find for pending recall
    fires, one find for opens/expansions across the involved sessions. Records
    every update_many so the watermark can be asserted."""

    def __init__(self, pending: list[dict], opened: list[dict]):
        self._pending = pending
        self._opened = opened
        self.updates: list[tuple[dict, dict]] = []

    async def update_many(self, query, update):
        self.updates.append((query, update))

    def stamped(self, field: str) -> set:
        """retrieval_ids this collection was asked to set `field` on."""
        out = set()
        for query, update in self.updates:
            if field in (update.get("$set") or {}):
                out |= set(query.get("retrieval_id", {}).get("$in", []))
        return out

    def find(self, query, projection=None):
        kind = query.get("memory_kind")
        if kind == "recall":
            # Honour the watermark the way Mongo would: a row already stamped
            # must not come back, and `graded` is NOT part of the selection —
            # the sweep counts opens, the model grades, and neither waits on
            # the other.
            docs = [
                d for d in self._pending
                if not d.get("unopened_checked")
                and all(d.get(f) == v for f, v in query.items()
                        if f in ("graded",) and not isinstance(v, dict))
            ]
        else:
            # Honour the kind filter the way Mongo would, so a `memory:`-keyed
            # event of some OTHER kind is not silently counted as an open.
            allowed = kind.get("$in") if isinstance(kind, dict) else None
            docs = [
                d for d in self._opened
                if allowed is None or d.get("memory_kind", "memory_open") in allowed
            ]

        class _Cursor:
            def __init__(self, rows): self._rows = list(rows)
            def limit(self, n):
                self._rows = self._rows[:n]
                return self
            def __aiter__(self):
                async def gen():
                    for row in self._rows:
                        yield row
                return gen()
        return _Cursor(docs)


def _fire(rid: str, recall: str, session: str) -> dict:
    return {"retrieval_id": rid, "memory_key": f"recall:{recall}", "session_id": session}


def _sweep(events, backing, **kwargs):
    """Run the real sweep against a fake collection, capturing stat bumps."""
    bumps = []

    async def fake_bump(key, inc, **_):
        bumps.append((key, inc))

    with patch.object(consolidation, "_collection", new=AsyncMock(return_value=events)), \
         patch.object(consolidation, "memory_ids_by_recall",
                      new=AsyncMock(return_value=backing)), \
         patch.object(consolidation, "_bump_stats", fake_bump), \
         patch.object(consolidation, "grade_retrieval", new=AsyncMock()) as grade:
        counted = _run(consolidation.record_unopened_fires(**kwargs))
    return counted, bumps, grade


def test_unopened_counts_backed_fires_only() -> None:
    """A fire whose backing memory was never opened is counted; one with no
    backing memory (sys_/user recall) has no memory an open could have touched,
    so there is nothing to count."""
    events = _EventsColl(
        pending=[_fire("r1", "learned1", "sessA"), _fire("r2", "sys_status", "sessA")],
        opened=[],
    )
    counted, bumps, _ = _sweep(events, {"learned1": "mem1"}, session_id="sessA")
    assert counted == 1, counted
    assert bumps == [("recall:learned1", {"unopened_count": 1})], bumps


def test_unopened_never_grades_so_the_model_can_still_judge() -> None:
    """The sweep must not write a grade.

    Measured on the live corpus: of the model-graded used=true fires with a
    backing memory, 17 of 19 had no open in that session — the instruction was
    actionable on its own. Since grade_retrieval refuses a second grade, a
    mechanical used=false would permanently displace the model's real verdict
    and collapse use_ratio on triggers the model called helpful.
    """
    events = _EventsColl(pending=[_fire("r1", "learned1", "sessA")], opened=[])
    counted, _, grade = _sweep(events, {"learned1": "mem1"}, session_id="sessA")
    assert counted == 1
    grade.assert_not_awaited()
    # `graded` is never touched, so the row still reaches the grading pass.
    for _query, update in events.updates:
        assert "graded" not in (update.get("$set") or {}), update


def test_unopened_watermarks_every_row_it_examined() -> None:
    """Rows the sweep can never act on must still leave the query.

    They never become graded, so if only actionable rows were stamped the dead
    ones would sit at the head of the ts-ordered index forever; once they
    filled a batch the sweep would re-read them every hour and never reach a
    live fire again.
    """
    events = _EventsColl(
        pending=[_fire("r1", "learned1", "sessA"), _fire("r2", "sys_status", "sessA")],
        opened=[],
    )
    counted, _, _ = _sweep(events, {"learned1": "mem1"}, session_id="sessA")
    assert counted == 1
    assert events.stamped("unopened_checked") == {"r1", "r2"}, events.updates
    # Only the actionable one is marked as an unopened fire.
    assert events.stamped("unopened") == {"r1"}, events.updates


def test_unopened_selects_on_the_watermark_not_on_graded() -> None:
    """The sweep and the grading pass are independent.

    A row the model already graded still has an open-or-not fact worth
    counting, and a row this sweep already stamped must never come back —
    selecting on `graded` would do both backwards, and would resurrect the
    coupling that let a mechanical verdict pre-empt the model's.
    """
    events = _EventsColl(
        pending=[
            dict(_fire("r1", "learned1", "sessA"), graded=True),
            dict(_fire("r2", "learned1", "sessA"), unopened_checked=True),
        ],
        opened=[],
    )
    counted, _, _ = _sweep(events, {"learned1": "mem1"}, session_id="sessA")
    assert counted == 1, "an already-graded fire still has an unopened fact"
    assert events.stamped("unopened_checked") == {"r1"}, events.updates


def test_unopened_skips_fires_whose_memory_was_opened() -> None:
    """Opened means the content demonstrably entered context — nothing to
    record, and the grade stays with the model either way."""
    events = _EventsColl(
        pending=[_fire("r1", "learned1", "sessA")],
        opened=[{"session_id": "sessA", "memory_key": "memory:mem1"}],
    )
    counted, bumps, _ = _sweep(events, {"learned1": "mem1"}, session_id="sessA")
    assert counted == 0, counted
    assert bumps == []
    assert events.stamped("unopened_checked") == {"r1"}


def test_unopened_open_in_another_session_does_not_count_as_opened() -> None:
    """The session is the join key: an open of the same memory in a different
    episode says nothing about this fire."""
    events = _EventsColl(
        pending=[_fire("r1", "learned1", "sessA")],
        opened=[{"session_id": "sessB", "memory_key": "memory:mem1"}],
    )
    counted, _, _ = _sweep(events, {"learned1": "mem1"}, session_id="sessA")
    assert counted == 1, counted


def test_unopened_refuses_unbounded_scope() -> None:
    """No session and no age bound would count fires still on screen.

    The fixture holds a fire that counts under any *bounded* call, so removing
    the guard fails this test — an empty collection would have passed either
    way and pinned nothing.
    """
    events = _EventsColl(pending=[_fire("r1", "learned1", "sessA")], opened=[])
    counted, _, _ = _sweep(
        events, {"learned1": "mem1"}, session_id=None, older_than_minutes=0,
    )
    assert counted == 0
    assert events.updates == []


def test_unopened_ignores_a_memory_keyed_event_of_another_kind() -> None:
    """"Opened" is a memory_kind, never a key prefix.

    Every `memory:<id>` event written today is a memory_open or a
    graph_expansion, so a prefix filter looks equivalent right now — until some
    other kind starts using the namespace and silently marks fires as followed
    that nobody ever read.
    """
    events = _EventsColl(
        pending=[_fire("r1", "learned1", "sessA")],
        opened=[{"session_id": "sessA", "memory_key": "memory:mem1",
                 "memory_kind": "drawer"}],
    )
    counted, _, _ = _sweep(events, {"learned1": "mem1"}, session_id="sessA")
    assert counted == 1, counted


def test_task_prompt_grades_every_ungraded_row_without_defaulting() -> None:
    """The harness no longer pre-grades, so the pass must grade every ungraded
    row — and must be told that [not opened] is context, not a verdict."""
    from harness.loop_prompts import task_consolidation_prompt
    text = task_consolidation_prompt()
    assert "NOT marked [already" in text, "grading instruction lost its scope"
    assert "not opened" in text, "the pass is not told what [not opened] means"
    assert "not a verdict" in text, "[not opened] is no longer framed as context"
    assert "used=true even though" in text, "the pass may default unopened to used=false"


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


def test_reflection_prompt_drops_the_memory_half_after_the_first_slot() -> None:
    """The evidence bins move in days, so only the day's first slot re-reads
    them; later slots must get the skip note and still run the worker audit."""
    from harness.loop_prompts import reflection_prompt

    full = reflection_prompt("2026-08-23")
    skipped = reflection_prompt("2026-08-23", memory_pass=False)
    assert "Call memory_utility_report() first" in full
    # The skip note still names the tool — to forbid it, not to ask for it.
    assert "Call memory_utility_report() first" not in skipped, "memory half survived the skip"
    assert "SKIPPED THIS SLOT" in skipped
    assert "BAD-TRIGGER" in full and "BAD-TRIGGER" not in skipped
    assert len(skipped) < len(full) / 2, (len(skipped), len(full))
    # The worker audit is not part of the memory half and runs every slot.
    assert "PART 3" in skipped
    assert "state/worker_control.md" in skipped


def test_goodnight_prompt_recap_goes_through_learn() -> None:
    """The recap files via the typed pipeline with an UNDATED topic — the
    dated `daily-recap-YYYY-MM-DD` hall never exact-matched morning's undated
    `daily-recap` filter, and minted a fresh hall every day."""
    from harness.loop_prompts import goodnight_prompt

    text = goodnight_prompt("2026-08-23")
    assert 'learn(type="episodic"' in text
    assert 'topic="daily-recap"' in text
    assert "daily-recap-YYYY-MM-DD" not in text
    assert "palace_add_drawer" not in text
    assert "2026-08-23" in text


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
         "confirmations": 3, "last_confirmed_ts": float(100 - i),
         "promotion_review": {"promote": True, "reason": "test"}}
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
                 patch.object(consolidation, "_review_promotion",
                              new=AsyncMock()) as gate, \
                 patch.object(consolidation, "_collection",
                              new=AsyncMock(return_value=None)), \
                 patch.object(consolidation, "record_maintenance", fake_record):
                _run(consolidation.promote_preferences())
            written = Path("config/MEMORY.md").read_text(encoding="utf-8")
            gate.assert_not_awaited()  # every entry already carries a verdict
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


def test_promotion_gate_fails_closed_and_stamps_verdicts() -> None:
    """Nothing enters the always-on prompt without a stored gate verdict:
    approved renders, rejected is stamped and excluded, unjudged (gate failure)
    stays out and is retried later."""
    entries = [
        {"memory_id": "ok", "content": "Approved preference.",
         "confirmations": 3, "last_confirmed_ts": 3.0,
         "promotion_review": {"promote": True, "reason": "crucial"}},
        {"memory_id": "no", "content": "Rejected preference.",
         "confirmations": 3, "last_confirmed_ts": 2.0, "promotion_review": None},
        {"memory_id": "later", "content": "Unjudged preference.",
         "confirmations": 3, "last_confirmed_ts": 1.0, "promotion_review": None},
    ]
    verdicts = {"no": {"promote": False, "reason": "recall covers it"},
                "later": None}

    async def fake_gate(entry):
        return verdicts[entry["memory_id"]]

    coll = AsyncMock()
    records = []

    async def fake_record(kind, detail):
        records.append(detail)

    with tempfile.TemporaryDirectory() as tmp:
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            Path("config").mkdir()
            Path("config/MEMORY.md").write_text("# hand written\n", encoding="utf-8")
            with patch.object(consolidation, "promotable_preferences",
                              new=AsyncMock(return_value=entries)), \
                 patch.object(consolidation, "_review_promotion", fake_gate), \
                 patch.object(consolidation, "_collection",
                              new=AsyncMock(return_value=coll)), \
                 patch.object(consolidation, "record_maintenance", fake_record):
                _run(consolidation.promote_preferences())
            written = Path("config/MEMORY.md").read_text(encoding="utf-8")
        finally:
            os.chdir(cwd)

    assert "Approved preference." in written
    assert "Rejected preference." not in written
    assert "Unjudged preference." not in written
    # The rejected verdict was stored so the gate never re-runs for it.
    stamped = [c.args[1]["$set"]["promotion_review"]
               for c in coll.update_one.call_args_list
               if "promotion_review" in c.args[1].get("$set", {})]
    assert stamped and stamped[0]["promote"] is False
    detail = records[0]
    assert detail["approved"] == ["ok"]
    assert detail["rejected"] == ["no"]
    assert detail["unreviewed"] == ["later"]


def test_promotion_gate_rejects_a_verdict_it_cannot_read() -> None:
    """Fail-closed covers the REPLY, not just a raised exception.

    Prose, JSON without `promote`, and a non-boolean `promote` all have to
    yield no verdict: an approve-by-default would put unjudged text in front of
    every turn, which is the one thing the gate exists to prevent.
    """
    from types import SimpleNamespace

    entry = {"memory_id": "m1", "content": "Be terse.", "confirmations": 3}

    def _provider(reply: str):
        async def create_message(**kwargs):
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=reply)]
            )
        return SimpleNamespace(create_message=create_message)

    unreadable = [
        "Yes, definitely promote this one.",       # prose, no JSON
        '{"reason": "crucial"}',                   # no verdict field
        '{"promote": "yes", "reason": "x"}',       # not a boolean
    ]
    for reply in unreadable:
        with patch("harness.model_registry.get_provider", return_value=_provider(reply)):
            assert _run(consolidation._review_promotion(entry)) is None, reply

    with patch("harness.model_registry.get_provider",
               return_value=_provider('{"promote": true, "reason": "binds first"}')):
        verdict = _run(consolidation._review_promotion(entry))
    assert verdict is not None and verdict["promote"] is True, verdict
    assert verdict["reason"] == "binds first", verdict


def test_promotion_gate_budget_counts_attempts_not_successes() -> None:
    """A failing gate must not turn one pass into a call per preference.

    _review_promotion returns None on a provider error or an unreadable reply,
    so bounding successes would leave the loop calling the model for every
    eligible entry exactly when every call is useless.
    """
    entries = [
        {"memory_id": f"m{i}", "content": f"Preference {i}.", "confirmations": 3,
         "last_confirmed_ts": float(i), "promotion_review": None}
        for i in range(10)
    ]
    calls = []

    async def always_fails(entry):
        calls.append(entry["memory_id"])
        return None

    with tempfile.TemporaryDirectory() as tmp:
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            Path("config").mkdir()
            Path("config/MEMORY.md").write_text("# hand written\n", encoding="utf-8")
            with patch.object(consolidation, "promotable_preferences",
                              new=AsyncMock(return_value=entries)), \
                 patch.object(consolidation, "_review_promotion", always_fails), \
                 patch.object(consolidation, "_collection", new=AsyncMock()), \
                 patch.object(consolidation, "record_maintenance", new=AsyncMock()):
                _run(consolidation.promote_preferences())
        finally:
            os.chdir(cwd)
    assert len(calls) == consolidation._GATE_REVIEWS_PER_PASS, calls


def test_promoted_lines_point_at_the_memory() -> None:
    """MEMORY.md points at memory, it does not replace it: the block holds the
    rule's short form and the pointer that reaches the full context."""
    fitted, lines = consolidation.fitting_promotions(
        [{"memory_id": "m1", "content": "Answer briefly."}]
    )
    assert [e["memory_id"] for e in fitted] == ["m1"]
    assert lines == ["- Answer briefly. (memory:m1)"], lines


def test_promotion_overflow_is_signposted_not_silently_dropped() -> None:
    """Approved entries past the cap are not gone — one line says how many more
    exist and how to reach them. It must NOT be a "- " bullet: entry lines are
    the promotion audit's unit of account."""
    entries = [
        {"memory_id": f"m{i}", "content": f"Preference number {i}."}
        for i in range(13)
    ]
    section = consolidation.render_promotion_section(entries)
    assert "plus 3 more" in section, section
    overflow_lines = [
        line for line in section.splitlines() if "plus 3 more" in line
    ]
    assert overflow_lines and not overflow_lines[0].startswith("- "), overflow_lines


def test_commit_stamps_the_ambient_session_on_the_candidate() -> None:
    """session_id is the join key the distinct-session promotion counter runs
    on: a commit that lands without one confirms nothing, forever."""
    with patch.object(consolidation, "_save_candidate", new=AsyncMock()) as save, \
         patch.object(consolidation, "_is_prose_duplicate", new=AsyncMock(return_value=None)), \
         patch.object(consolidation, "_commit_preference",
                      new=AsyncMock(return_value=("committed", {}, "ok"))):
        try:
            consolidation.set_session_context("sessA")
            _run(consolidation.commit_candidate(
                type="preference", content="Answer briefly.",
            ))
            ambient = save.await_args.args[0]
            # An explicit session (the consolidator's episode) wins over ambient.
            _run(consolidation.commit_candidate(
                type="preference", content="Answer briefly.", session_id="sessB",
            ))
            explicit = save.await_args.args[0]
        finally:
            consolidation.set_session_context(None)
    assert ambient["session_id"] == "sessA", ambient
    assert explicit["session_id"] == "sessB", explicit


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
        test_commit_candidate_episodic_writes_episodes_drawer_without_packaging,
        test_commit_candidate_kg_only_renders_content_for_search,
        test_commit_candidate_kg_invalidate_retires_before_adding,
        test_commit_candidate_kg_invalidate_alone_is_a_commit,
        test_commit_candidate_rejects_invalidate_on_non_semantic,
        test_commit_candidate_missed_invalidation_is_not_reported_as_retired,
        test_commit_candidate_threads_ended_to_kg_invalidate,
        test_commit_candidate_expired_fact_is_readdable,
        test_commit_candidate_reports_malformed_invalidations,
        test_commit_candidate_semantic_prose_writes_drawer,
        test_commit_candidate_prose_duplicate_skips_write,
        test_commit_candidate_procedural_writes_knowledge_file,
        test_commit_candidate_procedural_avoids_filename_collision,
        test_commit_candidate_preference_writes_daily_log,
        test_read_episode_segment_rejects_path_traversal,
        test_read_episode_segment_reads_verbatim_content,
        test_read_episode_segment_prefers_the_database,
        test_read_episode_segment_falls_back_to_disk_when_the_db_is_down,
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
        test_goodnight_prompt_recap_goes_through_learn,
        test_ungraded_surfacings_do_not_indict_a_trigger,
        test_a_genuinely_measured_bad_trigger_is_still_reported,
        test_bad_trigger_needs_enough_graded_evidence,
        test_promotion_audit_follows_the_character_budget,
        test_promotion_gate_fails_closed_and_stamps_verdicts,
        test_graded_counters_move_on_every_grade_not_only_on_use,
        test_clearing_a_chat_hands_the_compaction_summary_to_the_consolidator,
        test_episode_end_passes_the_summary_through_to_the_pass,
        test_a_learning_episode_survives_a_restart,
        test_only_recent_non_disposable_episodes_are_resumable,
        test_resuming_sessions_survives_a_dead_store,
        test_unopened_counts_backed_fires_only,
        test_unopened_never_grades_so_the_model_can_still_judge,
        test_unopened_watermarks_every_row_it_examined,
        test_unopened_selects_on_the_watermark_not_on_graded,
        test_unopened_skips_fires_whose_memory_was_opened,
        test_unopened_open_in_another_session_does_not_count_as_opened,
        test_unopened_refuses_unbounded_scope,
        test_unopened_ignores_a_memory_keyed_event_of_another_kind,
        test_task_prompt_grades_every_ungraded_row_without_defaulting,
        test_dedupe_skips_only_identical_text,
        test_dedupe_never_calls_the_encoder,
        test_shortlist_carries_the_cosine_score,
        test_promotion_gate_rejects_a_verdict_it_cannot_read,
        test_promotion_gate_budget_counts_attempts_not_successes,
        test_promoted_lines_point_at_the_memory,
        test_promotion_overflow_is_signposted_not_silently_dropped,
        test_commit_stamps_the_ambient_session_on_the_candidate,
        test_reflection_prompt_drops_the_memory_half_after_the_first_slot,
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print(f"{len(tests)}/{len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

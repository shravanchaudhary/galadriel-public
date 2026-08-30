#!/usr/bin/env python3
"""Tests for trigger generation, holdout testing, and preference promotion.

Covers the Pass-2 pieces of the multi-timescale learning architecture:

  - harness/recall_cues.py — parsing a model's cue payload, keeping the
    holdout honest, scoring probes through the real matcher, and the bounded
    repair loop's ordering rules.
  - harness/consolidation.py — the commit pipeline scheduling a trigger only
    for a genuinely new memory, and MEMORY.md promotion gated on repetition.

No model or Mongo is required: the generation call is patched, and the probe
runner is exercised against a stubbed matcher so the pass/fail arithmetic is
deterministic.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import consolidation  # noqa: E402
from harness import recall_cues  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _payload(**overrides) -> dict:
    base = {
        "instruction": "Check palace room=knowledge for the deploy runbook.",
        "activation_condition": "The user asks how to deploy this project.",
        "exclusions": "General questions about what deployment means.",
        "positive_examples": ["how do I ship this", "what's the deploy process"],
        "lexical_cues": ["deploy runbook"],
        "negative_examples": ["what is continuous deployment"],
        "holdout_positives": ["can you push this to prod", "how do we release"],
        "holdout_negatives": ["explain blue-green deploys in general"],
    }
    base.update(overrides)
    return base


# ─── Payload parsing ────────────────────────────────────────────────────


def test_parse_json_object_handles_fenced_output() -> None:
    raw = 'Sure!\n```json\n{"instruction": "x", "n": 1}\n```\nHope that helps.'
    assert recall_cues._parse_json_object(raw) == {"instruction": "x", "n": 1}


def test_parse_json_object_handles_bare_object_with_prose() -> None:
    raw = 'Here you go: {"instruction": "x"} — done.'
    assert recall_cues._parse_json_object(raw) == {"instruction": "x"}


def test_parse_json_object_rejects_garbage() -> None:
    for raw in ("", "not json at all", "[1, 2, 3]"):
        assert recall_cues._parse_json_object(raw) is None


def test_normalize_requires_stage1_and_stage2_fields() -> None:
    """Stage-1 cannot run without positives; learn_recall rejects no lexicals."""
    assert recall_cues.normalize_generated(_payload()) is not None
    for missing in ("positive_examples", "lexical_cues", "activation_condition", "instruction"):
        assert recall_cues.normalize_generated(_payload(**{missing: []})) is None, missing


def test_normalize_dedupes_and_trims_cue_lists() -> None:
    cues = recall_cues.normalize_generated(_payload(
        positive_examples=["how do I ship", "  how   do I ship  ", "HOW DO I SHIP", "other"],
    ))
    assert cues["positive_examples"] == ["how do I ship", "other"]


def test_normalize_truncates_judge_fields() -> None:
    cues = recall_cues.normalize_generated(_payload(activation_condition="x" * 500))
    assert len(cues["activation_condition"]) == recall_cues._MAX_CONDITION_CHARS


# ─── Holdout honesty ────────────────────────────────────────────────────


def test_probes_that_leaked_into_cues_are_dropped() -> None:
    """A probe stored as a cue scores 1.0 against itself — pure memorisation."""
    cues = recall_cues.drop_probe_overlap(_payload(
        positive_examples=["how do we release"],
        holdout_positives=["how do we release", "can you push this to prod"],
    ))
    assert cues["holdout_positives"] == ["can you push this to prod"]


def test_probes_containing_a_lexical_anchor_are_dropped() -> None:
    """Those test the hard trigger, not the semantic coverage under test."""
    cues = recall_cues.drop_probe_overlap(_payload(
        lexical_cues=["deploy runbook"],
        holdout_positives=["show me the deploy runbook", "how do we release"],
    ))
    assert cues["holdout_positives"] == ["how do we release"]


def test_negative_probes_are_held_out_from_stored_negatives() -> None:
    """Storing a probe as a known misfire hands the answer to the judge."""
    cues = recall_cues.drop_probe_overlap(_payload(
        negative_examples=["explain blue-green deploys in general"],
        holdout_negatives=["explain blue-green deploys in general", "what is a canary"],
    ))
    assert cues["holdout_negatives"] == ["what is a canary"]


# ─── Scoring ────────────────────────────────────────────────────────────


def _stub_probes(fired_for: set[str]):
    async def fake(candidate, probes, catalog=None):
        return [(p, p in fired_for, []) for p in probes]
    return fake


def test_scores_count_both_directions() -> None:
    cues = _payload(
        holdout_positives=["a", "b", "c", "d"],
        holdout_negatives=["x", "y"],
    )
    with patch.object(recall_cues, "run_probes", _stub_probes({"a", "b", "c", "x"})), \
         patch("harness.recall.recall_system_armed", return_value=True):
        scores = _run(recall_cues.test_cues(cues, catalog=[]))
    assert scores["recall_rate"] == 0.75
    assert scores["fp_rate"] == 0.5
    assert [p for p, _ in scores["missed"]] == ["d"]
    assert scores["false_fires"] == ["x"]


def test_disarmed_matcher_reports_no_measurement_not_zero() -> None:
    """0% would trigger repair rounds that cannot possibly help."""
    with patch("harness.recall.recall_system_armed", return_value=False):
        assert _run(recall_cues.test_cues(_payload(), catalog=[])) is None


def test_meets_bar_treats_absent_measurement_as_shippable() -> None:
    assert recall_cues.meets_bar(None) is True
    assert recall_cues.meets_bar({"recall_rate": 0.9, "fp_rate": 0.0}) is True
    assert recall_cues.meets_bar({"recall_rate": 0.1, "fp_rate": 0.0}) is False
    assert recall_cues.meets_bar({"recall_rate": 1.0, "fp_rate": 0.9}) is False


def test_describe_failures_names_the_thief_on_cross_fire() -> None:
    scores = {"missed": [("how do we release", ["sys_status"])], "false_fires": ["what is a canary"]}
    text = recall_cues.describe_failures(scores)
    assert "how do we release" in text and "sys_status" in text
    assert "what is a canary" in text


# ─── Threshold lever ────────────────────────────────────────────────────


def test_threshold_only_moves_up_and_stops_at_the_ceiling() -> None:
    from harness.recall import DEFAULT_POSITIVE_THRESHOLD

    nudged = recall_cues.raise_threshold({"positive_threshold": None})
    assert nudged["positive_threshold"] > DEFAULT_POSITIVE_THRESHOLD
    assert recall_cues.raise_threshold({"positive_threshold": recall_cues._MAX_THRESHOLD}) is None


def test_repair_reaches_for_threshold_only_when_misses_are_gone() -> None:
    """Raising the floor can never fix a positive that failed to fire."""
    attempts: list[dict] = []

    async def fake_generate(memory, **kwargs):
        attempts.append(kwargs)
        return _payload()

    # Round 1 has both a miss and a false fire; the loop must not touch the
    # threshold while a miss is outstanding.
    scores_seq = [
        {"recall_rate": 0.5, "fp_rate": 0.5, "missed": [("a", [])], "false_fires": ["x"]},
        {"recall_rate": 1.0, "fp_rate": 0.0, "missed": [], "false_fires": []},
    ]

    async def fake_test(cues, catalog=None):
        return scores_seq.pop(0)

    with patch.object(recall_cues, "generate_cues", fake_generate), \
         patch.object(recall_cues, "test_cues", fake_test), \
         patch("harness.recall.fetch_all_recalls", new=AsyncMock(return_value=[])):
        cues, scores = _run(recall_cues.build_tested_recall("some memory"))

    assert len(attempts) == 2, "a failing round must trigger exactly one repair"
    assert attempts[1].get("failures"), "the repair round must carry the evidence"
    assert cues.get("positive_threshold") is None, "threshold moved while a miss was open"
    assert scores["recall_rate"] == 1.0


def test_repair_keeps_the_best_round_when_nothing_reaches_the_bar() -> None:
    async def fake_generate(memory, **kwargs):
        return _payload()

    scores_seq = [
        {"recall_rate": 0.7, "fp_rate": 0.0, "missed": [("a", [])], "false_fires": []},
        {"recall_rate": 0.2, "fp_rate": 0.0, "missed": [("a", [])], "false_fires": []},
        {"recall_rate": 0.1, "fp_rate": 0.0, "missed": [("a", [])], "false_fires": []},
    ]

    async def fake_test(cues, catalog=None):
        return scores_seq.pop(0)

    with patch.object(recall_cues, "generate_cues", fake_generate), \
         patch.object(recall_cues, "test_cues", fake_test), \
         patch("harness.recall.fetch_all_recalls", new=AsyncMock(return_value=[])):
        _, scores = _run(recall_cues.build_tested_recall("some memory"))

    assert scores["recall_rate"] == 0.7, "a worse repair round must not be kept"


# ─── Commit pipeline wiring ─────────────────────────────────────────────


def test_committed_memory_schedules_a_trigger() -> None:
    seen: list[tuple] = []

    async def fake_generate(memory_id, type_, text, topic):
        seen.append((memory_id, type_, text, topic))

    async def main():
        with patch("harness.palace.kg_query", return_value="(no facts)"), \
             patch("harness.palace.add_drawer", new=AsyncMock(return_value="ok")), \
             patch.object(consolidation, "_save_candidate", new=AsyncMock()), \
             patch.object(consolidation, "_is_prose_duplicate", new=AsyncMock(return_value=None)), \
             patch.object(consolidation, "_generate_trigger", fake_generate):
            await consolidation.commit_candidate(
                type="semantic", content="A durable fact.", topic="t",
            )
            for task in list(consolidation._POST_COMMIT_TASKS):
                await task

    _run(main())
    assert len(seen) == 1, seen
    assert seen[0][1:] == ("semantic", "A durable fact.", "t")


def test_duplicate_memory_schedules_no_trigger() -> None:
    """The original already owns a trigger; a second would compete with it."""
    seen: list[tuple] = []

    async def fake_generate(*args):
        seen.append(args)

    async def main():
        with patch.object(consolidation, "_save_candidate", new=AsyncMock()), \
             patch.object(consolidation, "_is_prose_duplicate", new=AsyncMock(return_value="mem-1")), \
             patch.object(consolidation, "_generate_trigger", fake_generate):
            result = await consolidation.commit_candidate(
                type="semantic", content="A durable fact.",
            )
            await asyncio.sleep(0)
    _run(main())
    assert seen == []


def test_trigger_autogen_respects_the_kill_switch() -> None:
    seen: list[tuple] = []

    async def fake_generate(*args):
        seen.append(args)

    async def main():
        with patch.dict("os.environ", {"RECALL_CUE_AUTOGEN": "0"}), \
             patch("harness.palace.add_drawer", new=AsyncMock(return_value="ok")), \
             patch.object(consolidation, "_save_candidate", new=AsyncMock()), \
             patch.object(consolidation, "_is_prose_duplicate", new=AsyncMock(return_value=None)), \
             patch.object(consolidation, "_generate_trigger", fake_generate):
            await consolidation.commit_candidate(type="semantic", content="A fact.")
            await asyncio.sleep(0)

    _run(main())
    assert seen == []


# ─── MEMORY.md promotion ────────────────────────────────────────────────


def test_promotion_needs_repeated_confirmations() -> None:
    """Confirmations are DISTINCT SESSIONS: three restatements inside one
    chat's to-and-fro are one confirmation, so a same-session duplicate must
    not move the count."""
    docs = [
        {"memory_id": "m1", "type": "preference", "status": "committed",
         "content": "Keep replies short.", "created_at_ts": 1.0,
         "session_id": "s1"},
        {"memory_id": "m2", "type": "preference", "status": "duplicate",
         "duplicate_of": "m1", "created_at_ts": 2.0, "session_id": "s2"},
        # Restated again in the SAME session as the original — no new evidence.
        {"memory_id": "m2b", "type": "preference", "status": "duplicate",
         "duplicate_of": "m1", "created_at_ts": 2.5, "session_id": "s1"},
        {"memory_id": "m3", "type": "preference", "status": "committed",
         "content": "Use metric units.", "created_at_ts": 3.0,
         "session_id": "s9"},
    ]
    with patch.object(consolidation, "_collection", new=AsyncMock(return_value=_FakeColl(docs))):
        ready = _run(consolidation.promotable_preferences(min_confirmations=2))
    assert [r["content"] for r in ready] == ["Keep replies short."]
    assert ready[0]["confirmations"] == 2


def test_promotion_ignores_sessionless_confirmations() -> None:
    """A duplicate with no session cannot attribute its confirmation to a
    distinct occasion, so it counts for nothing."""
    docs = [
        {"memory_id": "m1", "type": "preference", "status": "committed",
         "content": "Keep replies short.", "created_at_ts": 1.0,
         "session_id": "s1"},
        {"memory_id": "m2", "type": "preference", "status": "duplicate",
         "duplicate_of": "m1", "created_at_ts": 2.0},
    ]
    with patch.object(consolidation, "_collection", new=AsyncMock(return_value=_FakeColl(docs))):
        ready = _run(consolidation.promotable_preferences(min_confirmations=2))
    assert ready == []


def test_promotion_follows_a_restatement_chain_to_one_original() -> None:
    """The classifier's `restates` verdict leaves the restating row COMMITTED,
    so it stays in the shortlist pool and a later paraphrase can name it.

    Folding one hop would split a preference confirmed in three sessions into
    two counts of two — it would never promote — and would offer the middle
    restatement as its own promotable rule, giving one preference two
    always-on slots.
    """
    docs = [
        {"memory_id": "b", "type": "preference", "status": "committed",
         "content": "Keep replies short.", "created_at_ts": 1.0,
         "session_id": "s1"},
        # Restated in s2; the classifier stamped it, the write was kept.
        {"memory_id": "a", "type": "preference", "status": "committed",
         "content": "Answer briefly.", "duplicate_of": "b",
         "created_at_ts": 2.0, "session_id": "s2"},
        # Restated again in s3, this time against the NEWEST phrasing.
        {"memory_id": "c", "type": "preference", "status": "committed",
         "content": "Be brief.", "duplicate_of": "a",
         "created_at_ts": 3.0, "session_id": "s3"},
    ]
    with patch.object(consolidation, "_collection", new=AsyncMock(return_value=_FakeColl(docs))):
        ready = _run(consolidation.promotable_preferences(min_confirmations=3))
    assert [r["memory_id"] for r in ready] == ["b"], ready
    assert ready[0]["confirmations"] == 3, ready
    # Even with the bar on the floor, a restatement is not its own rule: one
    # preference must never hold several always-on slots.
    with patch.object(consolidation, "_collection", new=AsyncMock(return_value=_FakeColl(docs))):
        every = _run(consolidation.promotable_preferences(min_confirmations=1))
    assert [r["memory_id"] for r in every] == ["b"], every


def test_promotion_survives_a_restatement_cycle() -> None:
    """A cycle must terminate rather than hang the reflection tick."""
    docs = [
        {"memory_id": "x", "type": "preference", "status": "committed",
         "content": "One.", "duplicate_of": "y", "created_at_ts": 1.0,
         "session_id": "s1"},
        {"memory_id": "y", "type": "preference", "status": "committed",
         "content": "Two.", "duplicate_of": "x", "created_at_ts": 2.0,
         "session_id": "s2"},
    ]
    with patch.object(consolidation, "_collection", new=AsyncMock(return_value=_FakeColl(docs))):
        ready = _run(consolidation.promotable_preferences(min_confirmations=2))
    assert isinstance(ready, list)


def test_promotion_orders_by_most_recent_confirmation() -> None:
    docs = [
        {"memory_id": "m1", "type": "preference", "status": "committed",
         "content": "Older.", "created_at_ts": 1.0, "session_id": "a1"},
        {"memory_id": "m2", "type": "preference", "status": "committed",
         "content": "Newer.", "created_at_ts": 2.0, "session_id": "b1"},
        {"memory_id": "d1", "type": "preference", "status": "duplicate",
         "duplicate_of": "m2", "created_at_ts": 9.0, "session_id": "b2"},
        {"memory_id": "d2", "type": "preference", "status": "duplicate",
         "duplicate_of": "m1", "created_at_ts": 3.0, "session_id": "a2"},
    ]
    with patch.object(consolidation, "_collection", new=AsyncMock(return_value=_FakeColl(docs))):
        ready = _run(consolidation.promotable_preferences(min_confirmations=2))
    assert [r["content"] for r in ready] == ["Newer.", "Older."]


def test_promotion_section_respects_the_entry_budget() -> None:
    entries = [{"memory_id": f"m{i}", "content": f"Preference {i}."} for i in range(30)]
    section = consolidation.render_promotion_section(entries)
    bullets = [line for line in section.splitlines() if line.startswith("- ")]
    assert len(bullets) <= consolidation._PROMOTION_MAX_ENTRIES
    assert len(section) <= consolidation._PROMOTION_MAX_CHARS + 200


def test_promotion_leaves_hand_written_memory_alone() -> None:
    """An automated writer must never own the whole always-on prompt."""
    original = "# MEMORY\n\n## User\n\n- **Preferred name:** Ada\n"
    section = consolidation.render_promotion_section([{"memory_id": "m1", "content": "Keep replies short."}])
    updated = consolidation.apply_promotion_section(original, section)
    assert "- **Preferred name:** Ada" in updated
    assert "Keep replies short." in updated

    # Re-running with different evidence replaces only the managed block.
    section2 = consolidation.render_promotion_section([{"memory_id": "m2", "content": "Use metric units."}])
    twice = consolidation.apply_promotion_section(updated, section2)
    assert "- **Preferred name:** Ada" in twice
    assert "Keep replies short." not in twice
    assert "Use metric units." in twice
    assert twice.count(consolidation._PROMOTION_BEGIN) == 1
    assert twice.count(consolidation._PROMOTION_HEADING) == 1


def test_promotion_is_idempotent() -> None:
    original = "# MEMORY\n\n## User\n\n- nothing yet\n"
    section = consolidation.render_promotion_section([{"memory_id": "m1", "content": "Keep replies short."}])
    once = consolidation.apply_promotion_section(original, section)
    twice = consolidation.apply_promotion_section(once, section)
    assert once == twice


def test_empty_promotion_clears_the_managed_block() -> None:
    original = "# MEMORY\n\n## User\n\n- keep\n"
    section = consolidation.render_promotion_section([{"memory_id": "mt", "content": "Temporary."}])
    filled = consolidation.apply_promotion_section(original, section)
    cleared = consolidation.apply_promotion_section(filled, "")
    assert "Temporary." not in cleared
    assert "- keep" in cleared


# ─── Consolidation summary drawer ───────────────────────────────────────


def test_summary_skips_an_episode_that_learned_nothing() -> None:
    """An empty record every time a chat ends would bury the useful ones."""
    with patch.object(consolidation, "_collection", new=AsyncMock(return_value=_FakeColl([]))):
        out = _run(consolidation.write_consolidation_summary(
            channel_id="main", reason="clear", session_id="s1", since_ts=0.0,
        ))
    assert out == ""


def test_summary_records_what_was_committed_and_why() -> None:
    docs = [
        {"memory_id": "m1", "type": "procedural", "status": "committed",
         "content": "Use method B with library X.", "note": "user corrected method A",
         "source": "task_consolidator", "created_at_ts": 5.0},
        {"memory_id": "m2", "type": "semantic", "status": "duplicate",
         "content": "Already known.", "source": "task_consolidator", "created_at_ts": 6.0},
    ]
    written: list[dict] = []

    async def fake_add_drawer(**kwargs):
        written.append(kwargs)
        return "ok"

    with patch.object(consolidation, "_collection", new=AsyncMock(return_value=_FakeColl(docs))), \
         patch("harness.palace.add_drawer", fake_add_drawer):
        out = _run(consolidation.write_consolidation_summary(
            channel_id="main", reason="clear", session_id="s1", since_ts=0.0,
        ))

    assert "Use method B with library X." in out
    assert "user corrected method A" in out
    assert "Skipped 1 near-duplicate" in out
    assert written[0]["room"] == consolidation.LEARNING_ROOM
    assert written[0]["wing"] == "agent"


class _FakeColl:
    """Minimal async-cursor stand-in for a Mongo collection."""

    def __init__(self, docs: list[dict]):
        self._docs = docs

    def find(self, query=None):
        docs = self._docs
        if query:
            type_ = query.get("type")
            if type_:
                docs = [d for d in docs if d.get("type") == type_]
            source = query.get("source")
            if isinstance(source, str):
                docs = [d for d in docs if d.get("source") == source]
            ts = (query.get("created_at_ts") or {}).get("$gte")
            if ts is not None:
                docs = [d for d in docs if (d.get("created_at_ts") or 0) >= ts]
        return _FakeCursor(docs)


class _FakeCursor:
    def __init__(self, docs: list[dict]):
        self._docs = list(docs)

    def sort(self, key, direction=1):
        self._docs.sort(key=lambda d: d.get(key) or 0, reverse=direction < 0)
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    def __aiter__(self):
        async def gen():
            for doc in self._docs:
                yield doc
        return gen()


def test_shipped_cue_set_records_whether_it_actually_passed() -> None:
    """build_tested_recall ships the best attempt even when repairs failed, so
    without an explicit flag a below-bar trigger is stored indistinguishably
    from one that cleared the bar."""
    failing = {"recall_rate": 0.2, "fp_rate": 0.9,
               "missed": [("a phrasing that should fire", [])],
               "false_fires": ["a phrasing that should not"]}
    cues = {"instruction": "i", "activation_condition": "c",
            "positive_examples": ["p"], "lexical_cues": ["l"]}

    with patch.object(recall_cues, "generate_cues", new=AsyncMock(return_value=cues)), \
         patch.object(recall_cues, "test_cues", new=AsyncMock(return_value=failing)), \
         patch("harness.recall.fetch_all_recalls", new=AsyncMock(return_value=[])):
        _, scores = _run(recall_cues.build_tested_recall("m"))
    assert scores["passed"] is False, "a set below the bar must not report passed"
    assert scores["repair_rounds"] == recall_cues._MAX_REPAIR_ROUNDS


def test_an_unmeasured_cue_set_is_neither_passed_nor_failed() -> None:
    """test_cues returns None when the matcher is disarmed. meets_bar() calls
    that good enough to ship (nothing to repair), but recording it as a pass
    would invent a measurement that never happened."""
    cues = {"instruction": "i", "activation_condition": "c",
            "positive_examples": ["p"], "lexical_cues": ["l"]}
    unmeasured = {"recall_rate": None, "fp_rate": None,
                  "probes_positive": 0, "probes_negative": 0}

    with patch.object(recall_cues, "generate_cues", new=AsyncMock(return_value=cues)), \
         patch.object(recall_cues, "test_cues", new=AsyncMock(return_value=unmeasured)), \
         patch("harness.recall.fetch_all_recalls", new=AsyncMock(return_value=[])):
        _, scores = _run(recall_cues.build_tested_recall("m"))
    assert scores["passed"] is None, f"unmeasured must stay None, got {scores['passed']!r}"
    assert recall_cues._public_scores(scores)["passed"] is None


def main() -> int:
    tests = [
        test_parse_json_object_handles_fenced_output,
        test_parse_json_object_handles_bare_object_with_prose,
        test_parse_json_object_rejects_garbage,
        test_normalize_requires_stage1_and_stage2_fields,
        test_normalize_dedupes_and_trims_cue_lists,
        test_normalize_truncates_judge_fields,
        test_probes_that_leaked_into_cues_are_dropped,
        test_probes_containing_a_lexical_anchor_are_dropped,
        test_negative_probes_are_held_out_from_stored_negatives,
        test_scores_count_both_directions,
        test_disarmed_matcher_reports_no_measurement_not_zero,
        test_meets_bar_treats_absent_measurement_as_shippable,
        test_describe_failures_names_the_thief_on_cross_fire,
        test_threshold_only_moves_up_and_stops_at_the_ceiling,
        test_repair_reaches_for_threshold_only_when_misses_are_gone,
        test_repair_keeps_the_best_round_when_nothing_reaches_the_bar,
        test_committed_memory_schedules_a_trigger,
        test_duplicate_memory_schedules_no_trigger,
        test_trigger_autogen_respects_the_kill_switch,
        test_promotion_needs_repeated_confirmations,
        test_promotion_ignores_sessionless_confirmations,
        test_promotion_follows_a_restatement_chain_to_one_original,
        test_promotion_survives_a_restatement_cycle,
        test_promotion_orders_by_most_recent_confirmation,
        test_promotion_section_respects_the_entry_budget,
        test_promotion_leaves_hand_written_memory_alone,
        test_promotion_is_idempotent,
        test_empty_promotion_clears_the_managed_block,
        test_summary_skips_an_episode_that_learned_nothing,
        test_summary_records_what_was_committed_and_why,
        test_shipped_cue_set_records_whether_it_actually_passed,
        test_an_unmeasured_cue_set_is_neither_passed_nor_failed,
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print(f"{len(tests)}/{len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

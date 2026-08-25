#!/usr/bin/env python3
"""Tests for the typed memory graph: vocabulary, classifier, and expansion.

The graph's job is to make a memory reachable *through* another memory. Three
things have to hold for that to be safe, and each is easy to get silently wrong:

  - Every relation binds to exactly one traversal policy. An unbound relation
    writes rows nothing ever follows, which looks like a working graph.
  - Traversal is bounded. Cycles and dense RECALL_WITH clusters would otherwise
    dump the corpus into the prompt.
  - A superseded memory never arrives as live context. Injecting a rule beside
    its replacement makes the model choose arbitrarily.

No model or Mongo required: the classifier's provider is patched and the edge
store is stubbed with an in-memory fake.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import consolidation  # noqa: E402
from harness import memory_graph  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Response:
    def __init__(self, text):
        self.content = [_Block(text)]


def _provider(text):
    provider = AsyncMock()
    provider.create_message = AsyncMock(return_value=_Response(text))
    return provider


# ─── Vocabulary ─────────────────────────────────────────────────────────


def test_every_relation_has_exactly_one_policy() -> None:
    memory_graph.validate_vocabulary()


def test_synonyms_bind_onto_the_closed_set() -> None:
    """Fragmentation is the failure this prevents: MOTIVATED and LED_TO stored as
    their own relations would make a CAUSED_BY traversal miss both, silently."""
    for wording in ("MOTIVATED", "led_to", "resulted-in", "Derived_From"):
        assert memory_graph.normalize_relation(wording) == "CAUSED_BY", wording
    assert memory_graph.normalize_relation("REQUIRES") == "DEPENDS_ON"
    assert memory_graph.normalize_relation("replaces") == "SUPERSEDES"


def test_unbindable_relation_is_rejected_not_invented() -> None:
    assert memory_graph.normalize_relation("VIBES_WITH") is None
    assert memory_graph.normalize_relation("") is None
    assert memory_graph.normalize_relation(None) is None


def test_relation_catalog_covers_the_whole_vocabulary() -> None:
    """The classifier prompt is generated from the vocabulary so the two cannot
    drift apart — a relation documented nowhere is never proposed."""
    catalog = memory_graph.relation_catalog()
    for relation in memory_graph.RELATIONS:
        assert relation in catalog, relation


# ─── Edge validation ────────────────────────────────────────────────────


def test_clean_edges_drops_self_edges() -> None:
    edges = memory_graph.clean_edges(
        [{"to": "m1", "relation": "DEPENDS_ON"}], from_id="m1",
    )
    assert edges == [], "a memory cannot be its own prerequisite"


def test_clean_edges_keeps_the_strongest_of_a_hedged_pair() -> None:
    """Order must not decide it: the weaker claim arriving first still loses."""
    edges = memory_graph.clean_edges(
        [
            {"to": "m2", "relation": "DEPENDS_ON", "strength": 0.2},
            {"to": "m2", "relation": "RECALL_WITH", "strength": 0.9},
        ],
        from_id="m1",
    )
    assert len(edges) == 1
    assert edges[0]["relation"] == "RECALL_WITH"


def test_clean_edges_keeps_one_edge_per_target() -> None:
    """Traversal visits a target once, so a second relation to it is inert data
    that only makes the graph look denser than it is."""
    edges = memory_graph.clean_edges(
        [
            {"to": "m2", "relation": "DEPENDS_ON", "strength": 0.8},
            {"to": "m2", "relation": "RECALL_BEFORE", "strength": 0.7},
            {"to": "m3", "relation": "RECALL_WITH", "strength": 0.6},
        ],
        from_id="m1",
    )
    assert sorted(e["to"] for e in edges) == ["m2", "m3"]
    assert [e["relation"] for e in edges if e["to"] == "m2"] == ["DEPENDS_ON"]


def test_classifier_may_not_mint_supersedes() -> None:
    """The one relation whose policy suppresses a memory. A wrong SUPERSEDES is
    self-concealing: the suppressed memory is never injected, so it never earns
    the telemetry that would decay the edge. Measured wrong 2/2 on real data."""
    assert memory_graph.clean_edges(
        [{"to": "m2", "relation": "SUPERSEDES", "strength": 0.95}], from_id="m1",
    ) == []


def test_non_model_writers_may_still_assert_supersedes() -> None:
    """Dedupe and explicit maintenance can establish supersession; a classifier
    guessing from similarity cannot."""
    edges = memory_graph.clean_edges(
        [{"to": "m2", "relation": "SUPERSEDES"}], from_id="m1", allow_forbidden=True,
    )
    assert edges and edges[0]["relation"] == "SUPERSEDES"


def test_clean_edges_clamps_strength_and_survives_junk() -> None:
    edges = memory_graph.clean_edges(
        [
            {"to": "m2", "relation": "DEPENDS_ON", "strength": 5},
            {"to": "m3", "relation": "DEPENDS_ON", "strength": "nonsense"},
            {"to": "", "relation": "DEPENDS_ON"},
            {"to": "m4", "relation": "NOT_A_RELATION"},
            "not even a dict",
        ],
        from_id="m1",
    )
    assert [e["to"] for e in edges] == ["m2", "m3"]
    assert edges[0]["strength"] == 1.0
    assert edges[1]["strength"] == 0.5


class _InverseAwareEdges:
    """Edge store pre-loaded with edges, tracking writes and deletes."""

    def __init__(self, existing):
        self.existing = [dict(e) for e in existing]
        for i, e in enumerate(self.existing):
            e.setdefault("_id", i)
            e.setdefault("strength", 0.5)
        self.written = []
        self.deleted = []

    async def find_one(self, query, projection=None):
        for e in self.existing:
            if e["from"] == query["from"] and e["to"] == query["to"]:
                return e
        return None

    async def delete_one(self, query):
        self.deleted.append(query["_id"])

    async def update_one(self, query, update, upsert=False):
        self.written.append(update["$set"])


def _add(existing, edge):
    store = _InverseAwareEdges(existing)
    with patch.object(memory_graph, "_collection", new=AsyncMock(return_value=store)):
        written = _run(memory_graph.add_edges("a", [edge], source="test"))
    return store, written


def test_opposed_dependency_claims_collapse_to_a_companion() -> None:
    """Measured on real data: the model proposed DEPENDS_ON in both directions
    between one pair, and the backwards claim carried the higher confidence.
    Neither arrival order nor strength can resolve direction, so the pair keeps
    its co-injection and loses the ordering claim."""
    store, written = _add(
        [{"from": "b", "to": "a", "relation": "DEPENDS_ON", "strength": 0.95}],
        {"to": "b", "relation": "DEPENDS_ON", "strength": 0.85},
    )
    assert written == 1
    assert store.deleted == [0], "the opposed edge must not be left alongside"
    assert store.written[0]["relation"] == "RECALL_WITH"
    assert store.written[0]["strength"] == 0.95, "keeps the stronger confidence"


def test_collapse_is_stable_against_a_later_dependency_claim() -> None:
    """Once collapsed, a later round proposing the dependency again must not
    re-promote the pair — otherwise the graph flips on every reclassification."""
    store, written = _add(
        [{"from": "b", "to": "a", "relation": "RECALL_WITH", "strength": 0.9}],
        {"to": "b", "relation": "DEPENDS_ON", "strength": 0.95},
    )
    assert written == 0 and store.written == [] and store.deleted == []


def test_write_allows_a_symmetric_relation_both_ways() -> None:
    """RECALL_WITH and CONTRADICTS are mutual by nature."""
    store, written = _add(
        [{"from": "b", "to": "a", "relation": "RECALL_WITH", "strength": 0.9}],
        {"to": "b", "relation": "RECALL_WITH", "strength": 0.5},
    )
    assert written == 1 and store.deleted == []


def test_write_allows_an_unrelated_asymmetric_edge() -> None:
    store, written = _add(
        [{"from": "c", "to": "a", "relation": "DEPENDS_ON", "strength": 0.9}],
        {"to": "b", "relation": "DEPENDS_ON", "strength": 0.5},
    )
    assert written == 1 and store.deleted == []


# ─── Classifier ─────────────────────────────────────────────────────────


def _classify(response_text, neighbours=None):
    neighbours = neighbours if neighbours is not None else [
        {"memory_id": "m2", "content": "Library X buffers writes.", "type": "semantic"},
    ]
    with patch("harness.model_registry.get_provider", return_value=_provider(response_text)), \
         patch("harness.model_registry.model_for", return_value="test-model"):
        return _run(memory_graph.classify_edges(
            "m1", "Flush before reading.", memory_type="procedural",
            neighbours=neighbours,
        ))


def test_classifier_returns_validated_edges() -> None:
    edges = _classify(
        '{"edges": [{"to": "m2", "relation": "DEPENDS_ON", '
        '"label": "needs buffering fact", "strength": 0.8}]}'
    )
    assert len(edges) == 1
    assert edges[0]["relation"] == "DEPENDS_ON"
    assert edges[0]["label"] == "needs buffering fact"


def test_classifier_accepts_zero_edges_as_a_real_answer() -> None:
    """Most memories stand alone. Empty must not read as a failure to retry."""
    assert _classify('{"edges": []}') == []


def test_classifier_drops_edges_to_invented_ids() -> None:
    """An edge to a hallucinated id points at nothing and traversal cannot tell."""
    edges = _classify(
        '{"edges": [{"to": "m2", "relation": "DEPENDS_ON"}, '
        '{"to": "m_does_not_exist", "relation": "DEPENDS_ON"}]}'
    )
    assert [e["to"] for e in edges] == ["m2"]


def test_classifier_binds_a_free_relation_name() -> None:
    edges = _classify('{"edges": [{"to": "m2", "relation": "REQUIRES"}]}')
    assert edges[0]["relation"] == "DEPENDS_ON"


def test_classifier_survives_unparseable_output() -> None:
    assert _classify("I could not determine any relationships, sorry.") == []


def test_classifier_skips_the_call_with_no_neighbours() -> None:
    called = []

    def fake_provider(task):
        called.append(task)
        raise AssertionError("must not reach the model with an empty shortlist")

    with patch("harness.model_registry.get_provider", fake_provider):
        assert _run(memory_graph.classify_edges(
            "m1", "text", memory_type="semantic", neighbours=[],
        )) == []
    assert called == []


# ─── Shortlist bound ────────────────────────────────────────────────────


def test_shortlist_is_bounded_and_spans_types() -> None:
    """The bound is the design: unbounded, this is O(new x all) model calls.
    Spanning types is also the design — the canonical edge is a procedural
    lesson depending on a semantic fact.
    """
    pool = [
        {"memory_id": f"m{i}", "content": f"memory number {i}", "type": "semantic"}
        for i in range(30)
    ]
    pool.append({"memory_id": "p1", "content": "a procedure", "type": "procedural"})

    with patch.object(consolidation, "_collection", new=AsyncMock(return_value=_FakeCandidates(pool))), \
         patch("harness.recall.get_encoder", return_value=lambda xs: [[1.0, 0.0]] * len(xs)), \
         patch("harness.recall._top_k_cosine",
               side_effect=lambda enc, text, examples, k: examples[:k]):
        out = _run(consolidation.shortlist_neighbours("query", limit=5))

    assert len(out) == 5, out
    queried = _FakeCandidates.last_query
    assert "type" not in queried, "shortlist must not filter by memory type"


def test_shortlist_excludes_the_memory_being_classified() -> None:
    pool = [
        {"memory_id": "m1", "content": "the new one", "type": "semantic"},
        {"memory_id": "m2", "content": "another one", "type": "semantic"},
    ]
    with patch.object(consolidation, "_collection", new=AsyncMock(return_value=_FakeCandidates(pool))), \
         patch("harness.recall.get_encoder", return_value=lambda xs: [[1.0]] * len(xs)), \
         patch("harness.recall._top_k_cosine",
               side_effect=lambda enc, text, examples, k: examples[:k]):
        out = _run(consolidation.shortlist_neighbours("q", exclude_id="m1"))
    assert [n["memory_id"] for n in out] == ["m2"]


class _FakeCandidates:
    """Minimal async Mongo cursor stand-in for the candidates collection."""

    last_query: dict = {}

    def __init__(self, docs):
        self._docs = docs

    def find(self, query, projection=None):
        _FakeCandidates.last_query = query
        return self

    def sort(self, *a, **kw):
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    def __aiter__(self):
        async def gen():
            for doc in self._docs:
                yield doc
        return gen()


# ─── Commit wiring ──────────────────────────────────────────────────────


def test_commit_schedules_edge_classification() -> None:
    seen = []

    async def fake_classify(memory_id, type_, text):
        seen.append((type_, text))

    # `_commit_procedural` is patched out: unpatched it writes a real file into
    # knowledge/procedures and appends an INDEX.md row, so the test would leave
    # artifacts in the repo and fail the knowledge-index integrity check.
    async def main():
        with patch.object(
                consolidation, "_commit_procedural",
                new=AsyncMock(return_value=("committed", {}, "ok"))), \
             patch.object(consolidation, "_save_candidate", new=AsyncMock()), \
             patch.object(consolidation, "_is_prose_duplicate", new=AsyncMock(return_value=None)), \
             patch.object(consolidation, "_generate_trigger", new=AsyncMock()), \
             patch.object(consolidation, "_classify_edges", fake_classify):
            await consolidation.commit_candidate(
                type="procedural", content="Flush before reading.",
            )
            for task in list(consolidation._POST_COMMIT_TASKS):
                await task

    _run(main())
    assert seen == [("procedural", "Flush before reading.")]


def test_edge_classification_respects_its_kill_switch() -> None:
    seen = []

    async def fake_classify(*args):
        seen.append(args)

    async def main():
        with patch.dict("os.environ", {"MEMORY_EDGE_AUTOGEN": "0"}), \
             patch("harness.palace.add_drawer", new=AsyncMock(return_value="ok")), \
             patch.object(consolidation, "_save_candidate", new=AsyncMock()), \
             patch.object(consolidation, "_is_prose_duplicate", new=AsyncMock(return_value=None)), \
             patch.object(consolidation, "_generate_trigger", new=AsyncMock()), \
             patch.object(consolidation, "_classify_edges", fake_classify):
            await consolidation.commit_candidate(type="semantic", content="A fact.")
            await asyncio.sleep(0)

    _run(main())
    assert seen == []


def test_kill_switches_are_independent() -> None:
    """Turning off cue generation must not also turn off the graph."""
    triggers, edges = [], []

    async def fake_trigger(*args):
        triggers.append(args)

    async def fake_classify(*args):
        edges.append(args)

    async def main():
        with patch.dict("os.environ", {"RECALL_CUE_AUTOGEN": "0"}), \
             patch("harness.palace.add_drawer", new=AsyncMock(return_value="ok")), \
             patch.object(consolidation, "_save_candidate", new=AsyncMock()), \
             patch.object(consolidation, "_is_prose_duplicate", new=AsyncMock(return_value=None)), \
             patch.object(consolidation, "_generate_trigger", fake_trigger), \
             patch.object(consolidation, "_classify_edges", fake_classify):
            await consolidation.commit_candidate(type="semantic", content="A fact.")
            for task in list(consolidation._POST_COMMIT_TASKS):
                await task

    _run(main())
    assert triggers == [] and len(edges) == 1


# ─── Expansion ──────────────────────────────────────────────────────────


class _FakeEdges:
    """In-memory stand-in for memory_edges supporting the two shapes used."""

    def __init__(self, edges):
        self._edges = edges

    def find(self, query, projection=None):
        if "from" in query:
            rels = (query.get("relation") or {}).get("$in")
            rows = [
                e for e in self._edges
                if e["from"] == query["from"] and (not rels or e["relation"] in rels)
            ]
            rows.sort(key=lambda e: -e.get("strength", 0.5))
        else:
            wanted = (query.get("to") or {}).get("$in") or []
            rows = [
                e for e in self._edges
                if e["to"] in wanted and e["relation"] == query.get("relation")
            ]
        return _FakeCursor(rows)


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def sort(self, *a, **kw):
        return self

    def __aiter__(self):
        async def gen():
            for row in self._rows:
                yield row
        return gen()


def _expand(edges, contents, *, seeds=("z",), **kw):
    """Run expansion against a stubbed graph and candidate store."""
    texts = {
        mid: {"memory_id": mid, "content": text, "type": "semantic"}
        for mid, text in contents.items()
    }
    with patch("harness.consolidation.memory_ids_for_recalls",
               new=AsyncMock(return_value=list(seeds))), \
         patch("harness.consolidation.memory_texts", new=AsyncMock(return_value=texts)), \
         patch.object(memory_graph, "_collection",
                      new=AsyncMock(return_value=_FakeEdges(edges))):
        return _run(memory_graph.expand_from_recalls(["r1"], **kw))


def _edge(frm, to, relation, strength=0.5):
    return {"from": frm, "to": to, "relation": relation, "label": "", "strength": strength}


def test_expansion_follows_a_dependency_chain_in_order() -> None:
    """The whole point: recall fires for Z, and X and Y arrive ahead of it."""
    bundle = _expand(
        [_edge("z", "y", "DEPENDS_ON"), _edge("y", "x", "DEPENDS_ON")],
        {"y": "the intermediate concept", "x": "the foundational concept"},
    )
    assert [i["memory_id"] for i in bundle] == ["x", "y"], (
        "the deepest prerequisite is the most foundational and must come first"
    )


def test_expansion_returns_nothing_without_seeds() -> None:
    with patch("harness.consolidation.memory_ids_for_recalls",
               new=AsyncMock(return_value=[])):
        assert _run(memory_graph.expand_from_recalls(["r1"])) == []


def test_expansion_never_injects_the_seed_itself() -> None:
    bundle = _expand(
        [_edge("z", "z", "DEPENDS_ON"), _edge("z", "y", "DEPENDS_ON")],
        {"z": "the seed", "y": "a prerequisite"},
    )
    assert [i["memory_id"] for i in bundle] == ["y"]


def test_expansion_survives_a_cycle() -> None:
    bundle = _expand(
        [_edge("z", "y", "DEPENDS_ON"), _edge("y", "z", "DEPENDS_ON"),
         _edge("y", "x", "DEPENDS_ON")],
        {"y": "why", "x": "ex"},
    )
    assert sorted(i["memory_id"] for i in bundle) == ["x", "y"]


def test_expansion_respects_the_budget() -> None:
    edges = [_edge("z", f"m{i}", "DEPENDS_ON") for i in range(10)]
    contents = {f"m{i}": f"memory {i}" for i in range(10)}
    bundle = _expand(edges, contents, budget=2)
    assert len(bundle) == 2


def test_expansion_respects_the_depth_cap() -> None:
    """A chain longer than the cap stops; it does not walk the corpus."""
    edges = [_edge(f"n{i}", f"n{i+1}", "DEPENDS_ON") for i in range(6)]
    contents = {f"n{i}": f"link {i}" for i in range(7)}
    bundle = _expand(edges, contents, seeds=("n0",), budget=10, max_depth=2)
    assert sorted(i["memory_id"] for i in bundle) == ["n1", "n2"]


def test_companions_are_leaves() -> None:
    """One dense RECALL_WITH cluster must not consume the budget with material
    that is merely adjacent."""
    bundle = _expand(
        [_edge("z", "c1", "RECALL_WITH"), _edge("c1", "c2", "RECALL_WITH")],
        {"c1": "companion one", "c2": "companion two"},
        budget=5,
    )
    assert [i["memory_id"] for i in bundle] == ["c1"]


def test_prerequisites_sort_ahead_of_companions_and_conflicts() -> None:
    bundle = _expand(
        [_edge("z", "c", "RECALL_WITH"), _edge("z", "k", "CONTRADICTS"),
         _edge("z", "p", "DEPENDS_ON")],
        {"c": "companion", "k": "conflict", "p": "prerequisite"},
        budget=5,
    )
    assert [i["kind"] for i in bundle] == ["prerequisite", "companion", "conflict"]


def test_superseded_memory_is_never_injected() -> None:
    """Injecting a rule beside its replacement makes the model choose arbitrarily."""
    bundle = _expand(
        [_edge("z", "old", "DEPENDS_ON"), _edge("new", "old", "SUPERSEDES")],
        {"old": "use threshold 0.72"},
    )
    assert bundle == []


def test_on_demand_relations_are_not_followed() -> None:
    """CAUSED_BY is provenance. It belongs in an explanation, not in every fire."""
    bundle = _expand(
        [_edge("z", "why", "CAUSED_BY")], {"why": "the failure that taught us"},
    )
    assert bundle == []


def test_memory_with_no_stored_text_is_skipped() -> None:
    bundle = _expand([_edge("z", "y", "DEPENDS_ON")], {"y": "   "})
    assert bundle == []


def test_format_bundle_groups_by_kind_and_is_empty_when_nothing_reached() -> None:
    assert memory_graph.format_bundle([]) == ""
    text = memory_graph.format_bundle([
        {"kind": "prerequisite", "relation": "DEPENDS_ON", "label": "needs",
         "content": "the base fact", "memory_id": "x", "depth": 1},
        {"kind": "conflict", "relation": "CONTRADICTS", "label": "",
         "content": "the other claim", "memory_id": "k", "depth": 1},
    ])
    assert "Needed to make sense of the above:" in text
    assert "- (needs) the base fact" in text
    assert "- (contradicts) the other claim" in text


# ─── Hygiene ────────────────────────────────────────────────────────────


class _FakeStats:
    def __init__(self, rows):
        self._rows = rows

    def find(self, query, projection=None):
        return _FakeCursor(self._rows)


class _RecordingEdges:
    def __init__(self):
        self.updates = []
        self.deleted = 0

    async def update_many(self, query, update):
        self.updates.append((query, update))

    async def delete_many(self, query):
        self.deleted = 1
        return type("R", (), {"deleted_count": 1})()


def _decay(rows):
    edges = _RecordingEdges()
    stats = _FakeStats(rows)

    async def fake_consolidation_collection(name):
        return stats

    with patch.object(memory_graph, "_collection", new=AsyncMock(return_value=edges)), \
         patch.object(consolidation, "_collection", fake_consolidation_collection):
        report = _run(memory_graph.decay_unhelpful_edges())
    return edges, report


def test_decay_weakens_edges_into_an_unused_memory() -> None:
    edges, report = _decay([
        {"memory_key": "memory:bad", "retrieval_count": 10, "use_count": 0},
    ])
    assert edges.updates, "an edge whose target is never used must lose strength"
    query, update = edges.updates[0]
    assert query == {"to": {"$in": ["bad"]}}
    assert update["$inc"]["strength"] < 0
    assert "pruned" in report


def test_decay_ignores_a_memory_that_gets_used() -> None:
    edges, report = _decay([
        {"memory_key": "memory:good", "retrieval_count": 10, "use_count": 8},
    ])
    assert edges.updates == [] and report == ""


def test_decay_waits_for_enough_evidence() -> None:
    """One unused injection is noise, not a verdict on the edge."""
    edges, report = _decay([
        {"memory_key": "memory:new", "retrieval_count": 2, "use_count": 0},
    ])
    assert edges.updates == [] and report == ""


def main() -> int:
    tests = [
        test_every_relation_has_exactly_one_policy,
        test_synonyms_bind_onto_the_closed_set,
        test_unbindable_relation_is_rejected_not_invented,
        test_relation_catalog_covers_the_whole_vocabulary,
        test_clean_edges_drops_self_edges,
        test_clean_edges_keeps_the_strongest_of_a_hedged_pair,
        test_clean_edges_keeps_one_edge_per_target,
        test_classifier_may_not_mint_supersedes,
        test_non_model_writers_may_still_assert_supersedes,
        test_clean_edges_clamps_strength_and_survives_junk,
        test_opposed_dependency_claims_collapse_to_a_companion,
        test_collapse_is_stable_against_a_later_dependency_claim,
        test_write_allows_a_symmetric_relation_both_ways,
        test_write_allows_an_unrelated_asymmetric_edge,
        test_classifier_returns_validated_edges,
        test_classifier_accepts_zero_edges_as_a_real_answer,
        test_classifier_drops_edges_to_invented_ids,
        test_classifier_binds_a_free_relation_name,
        test_classifier_survives_unparseable_output,
        test_classifier_skips_the_call_with_no_neighbours,
        test_shortlist_is_bounded_and_spans_types,
        test_shortlist_excludes_the_memory_being_classified,
        test_commit_schedules_edge_classification,
        test_edge_classification_respects_its_kill_switch,
        test_kill_switches_are_independent,
        test_expansion_follows_a_dependency_chain_in_order,
        test_expansion_returns_nothing_without_seeds,
        test_expansion_never_injects_the_seed_itself,
        test_expansion_survives_a_cycle,
        test_expansion_respects_the_budget,
        test_expansion_respects_the_depth_cap,
        test_companions_are_leaves,
        test_prerequisites_sort_ahead_of_companions_and_conflicts,
        test_superseded_memory_is_never_injected,
        test_on_demand_relations_are_not_followed,
        test_memory_with_no_stored_text_is_skipped,
        test_format_bundle_groups_by_kind_and_is_empty_when_nothing_reached,
        test_decay_weakens_edges_into_an_unused_memory,
        test_decay_ignores_a_memory_that_gets_used,
        test_decay_waits_for_enough_evidence,
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print(f"{len(tests)}/{len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

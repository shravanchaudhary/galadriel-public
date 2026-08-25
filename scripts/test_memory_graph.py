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
import json
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

    async def delete_many(self, query):
        clauses = query.get("$or") or [query]

        def matches(edge, clause):
            for field, value in clause.items():
                if isinstance(value, dict) and "$ne" in value:
                    if edge.get(field) == value["$ne"]:
                        return False
                elif edge.get(field) != value:
                    return False
            return True

        hits = [e for e in self.existing if any(matches(e, c) for c in clauses)]
        for e in hits:
            self.existing.remove(e)
            self.deleted.append(e["_id"])
        return type("R", (), {"deleted_count": len(hits)})()

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


def _shortlist(pool, encoder, **kwargs):
    coll = _FakeCandidates(pool)
    with patch.object(consolidation, "_collection", new=AsyncMock(return_value=coll)), \
         patch("harness.recall.get_encoder", return_value=encoder):
        out = _run(consolidation.shortlist_neighbours(**kwargs))
    return out, coll


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

    out, _ = _shortlist(pool, _encoder({}, default=(1.0, 0.0)), content="query", limit=5)

    assert len(out) == 5, out
    queried = _FakeCandidates.last_query
    assert "type" not in queried, "shortlist must not filter by memory type"


def test_shortlist_excludes_the_memory_being_classified() -> None:
    pool = [
        {"memory_id": "m1", "content": "the new one", "type": "semantic"},
        {"memory_id": "m2", "content": "another one", "type": "semantic"},
    ]
    out, _ = _shortlist(
        pool, _encoder({}, default=(1.0, 0.0)), content="q", exclude_id="m1",
    )
    assert [n["memory_id"] for n in out] == ["m2"]


def test_shortlist_reaches_memories_older_than_the_dedupe_window() -> None:
    """The graph exists to connect today's memory to an old foundational one.

    Dedupe asks "did I just write this?" and looks at a recent window; this asks
    "what does this rest on?". Sharing the window would make an old memory
    permanently ineligible as a relation target, at write time, which is the one
    failure traversal can never recover from.
    """
    long_ago = consolidation._now().timestamp() - 400 * 86400
    pool = [
        {"memory_id": "recent", "content": "sourdough needs a fed starter",
         "type": "procedural", "created_at_ts": consolidation._now().timestamp()},
        {"memory_id": "ancient", "content": "the compatibility check covers only additive columns",
         "type": "semantic", "created_at_ts": long_ago},
    ]
    encoder = _encoder({"compatibility": (1.0, 0.0), "migration": (1.0, 0.0)})
    out, _ = _shortlist(
        pool, encoder, content="run the migration compatibility check", limit=1,
    )

    assert [n["memory_id"] for n in out] == ["ancient"], out
    assert "created_at_ts" not in _FakeCandidates.last_query, (
        "the relation shortlist must not inherit the dedupe time window"
    )


def test_shortlist_stores_the_embeddings_it_computes() -> None:
    """Ranking the whole history means encoding it once, not once per commit."""
    pool = [
        {"memory_id": "stored", "content": "already encoded", "type": "semantic",
         "embedding": [1.0, 0.0]},
        {"memory_id": "fresh", "content": "never encoded", "type": "semantic"},
    ]
    encoded: list[list[str]] = []

    def encoder(texts):
        encoded.append(list(texts))
        return [[1.0, 0.0]] * len(texts)

    out, coll = _shortlist(pool, encoder, content="q", limit=2)

    assert len(out) == 2
    assert encoded[1] == ["never encoded"], "a stored embedding must not be recomputed"
    assert coll.updated == [
        ({"memory_id": "fresh"}, {"$set": {"embedding": [1.0, 0.0]}}),
    ]


def test_shortlist_survives_an_embedding_write_failure() -> None:
    """A store that cannot take the embedding still gets its shortlist."""
    pool = [{"memory_id": "a", "content": "one", "type": "semantic"},
            {"memory_id": "b", "content": "two", "type": "semantic"}]
    coll = _FakeCandidates(pool)
    coll.update_one = AsyncMock(side_effect=RuntimeError("no writes"))
    with patch.object(consolidation, "_collection", new=AsyncMock(return_value=coll)), \
         patch("harness.recall.get_encoder", return_value=_encoder({}, default=(1.0, 0.0))):
        out = _run(consolidation.shortlist_neighbours("q", limit=2))
    assert {n["memory_id"] for n in out} == {"a", "b"}


class _FakeCandidates:
    """Minimal async Mongo cursor stand-in for the candidates collection."""

    last_query: dict = {}

    def __init__(self, docs):
        self._docs = docs
        self.updated: list[tuple[dict, dict]] = []

    def find(self, query, projection=None):
        _FakeCandidates.last_query = query
        return self

    def sort(self, *a, **kw):
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    async def update_one(self, query, update, **kw):
        self.updated.append((query, update))

    def __aiter__(self):
        async def gen():
            for doc in self._docs:
                yield doc
        return gen()


def _encoder(mapping: dict, default=(0.0, 1.0)):
    """Deterministic stand-in encoder: substring -> vector, so a test can say
    which pool memories are near the query without a real model.
    """
    def encode(texts):
        out = []
        for text in texts:
            vector = default
            for needle, vec in mapping.items():
                if needle in text:
                    vector = vec
                    break
            out.append(list(vector))
        return out
    return encode


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


# ─── Edge store fakes ───────────────────────────────────────────────────
# Shared by the write-path and replacement sections. Traversal itself now
# lives behind memory_access (a fire activates; opening retrieves), and is
# tested there.


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


def _edge(frm, to, relation, strength=0.5):
    return {"from": frm, "to": to, "relation": relation, "label": "", "strength": strength}


# ─── Relation semantics across domains ──────────────────────────────────
# The graph was built and first measured against one small corpus from one
# project, which is enough to find bugs and nowhere near enough to calibrate
# anything. These cases come from unrelated domains and assert only the
# deterministic half — how a proposal binds, what survives cleaning, and what
# traversal does with each relation. Nothing here asserts that a live model
# produces a particular edge; that is stochastic and belongs in an operator
# evaluation, not in CI.

_DOMAIN_CASES = [
    # (domain, new memory, existing memory, model's own wording, bound relation)
    ("deployment", "check migration compatibility before changing the schema",
     "the compatibility check covers additive columns only", "requires", "DEPENDS_ON"),
    ("library use", "call flush() before reading a batch",
     "the client buffers writes until flush() or 4MB", "prerequisite_of", "DEPENDS_ON"),
    ("preference", "answer concisely by default",
     "the user reads on a phone most evenings", "see_also", "RECALL_WITH"),
    ("research", "inspect the methodology before quoting a benchmark",
     "that benchmark measured a warmed cache", "warn_before", "RECALL_BEFORE"),
    ("cooking", "this dough rests twice before shaping",
     "proof the yeast before mixing the dough", "needs", "DEPENDS_ON"),
    ("incident", "drain the queue before deploying",
     "the March outage started with a full queue", "learned_from", "CAUSED_BY"),
]


def _classify_one(new, existing, wording):
    """One classification round with the model's answer stubbed out."""
    payload = json.dumps({"edges": [
        {"to": "existing", "relation": wording, "label": wording.lower(), "strength": 0.8},
    ]})
    with patch("harness.model_registry.get_provider", return_value=_provider(payload)), \
         patch("harness.model_registry.model_for", return_value="m"):
        return _run(memory_graph.classify_edges(
            "new", new, memory_type="procedural",
            neighbours=[{"memory_id": "existing", "content": existing, "type": "semantic"}],
        ))


def test_free_wording_binds_to_the_same_behaviour_in_every_domain() -> None:
    for domain, new, existing, wording, expected in _DOMAIN_CASES:
        edges = _classify_one(new, existing, wording)
        assert [e["relation"] for e in edges] == [expected], f"{domain}: {edges}"
        assert edges[0]["label"] == wording.lower(), (
            f"{domain}: the model's own wording is kept as provenance"
        )


def test_each_domain_relation_gets_the_behaviour_it_was_given() -> None:
    """A relation is a behaviour, not a name. Prerequisites are the ones a
    reader inlines; the rest are navigation; provenance is neither."""
    for domain, _, _, _, relation in _DOMAIN_CASES:
        prerequisite = relation in memory_graph.POLICY_PREREQUISITE
        followed = relation in memory_graph._FOLLOWED
        if relation == "CAUSED_BY":
            assert not followed, f"{domain}: provenance is not navigation"
            assert not prerequisite, f"{domain}: provenance is never inlined"
            continue
        assert followed, f"{domain}: {relation} is unreachable from a reader"
        assert prerequisite == (relation in ("DEPENDS_ON", "RECALL_BEFORE")), domain


def test_a_replacement_is_refused_from_the_classifier_in_every_domain() -> None:
    """API migration is the case that most tempts a model into SUPERSEDES."""
    edges = _classify_one(
        "the service now authenticates with OIDC",
        "the service authenticates with a shared API key",
        "replaces",
    )
    assert edges == [], "supersession needs an event, not a resemblance"


# ─── Startup ────────────────────────────────────────────────────────────
# Indexes have to exist before the first turn, not after the first reflection.
# A process that boots and immediately serves a turn resolves recall seeds and
# writes telemetry; unindexed, every one of those is a collection scan.


class _IndexRecorder:
    def __init__(self):
        self.indexes = []

    async def create_index(self, keys, **kw):
        self.indexes.append(keys)


def test_graph_indexes_cover_both_traversal_directions() -> None:
    """Expansion walks outgoing edges; supersession resolution and decay look
    up incoming ones."""
    coll = _IndexRecorder()
    with patch.object(memory_graph, "_collection", new=AsyncMock(return_value=coll)):
        _run(memory_graph.ensure_indexes())
    assert [("from", 1), ("relation", 1)] in coll.indexes
    assert [("to", 1), ("relation", 1)] in coll.indexes


def test_candidate_indexes_cover_the_turn_path() -> None:
    coll = _IndexRecorder()
    with patch.object(consolidation, "_collection", new=AsyncMock(return_value=coll)):
        _run(consolidation.ensure_indexes())
    # recall fire -> seed memory, on every turn a recall fires.
    assert [("trigger.recall_id", 1)] in coll.indexes
    assert [("memory_id", 1)] in coll.indexes
    # The edge classifier's candidate pool, now spanning all history.
    assert [("status", 1), ("created_at_ts", -1)] in coll.indexes


def test_index_creation_is_a_no_op_without_a_database() -> None:
    with patch.object(memory_graph, "_collection", new=AsyncMock(return_value=None)), \
         patch.object(consolidation, "_collection", new=AsyncMock(return_value=None)):
        _run(memory_graph.ensure_indexes())
        _run(consolidation.ensure_indexes())


def test_a_failing_index_does_not_stop_the_boot() -> None:
    """A missing index costs speed. Refusing to start costs the agent."""
    coll = AsyncMock()
    coll.create_index = AsyncMock(side_effect=RuntimeError("no perms"))
    with patch.object(memory_graph, "_collection", new=AsyncMock(return_value=coll)):
        _run(memory_graph.ensure_indexes())


def test_the_boot_path_ensures_the_indexes() -> None:
    """The regression this exists for: indexes that only appear once the
    ambient reflection loop happens to run, hours after the first turn.
    """
    from harness.scheduler import Scheduler

    class _Agent:
        conversation_queue = None

    scheduled = []

    async def main():
        with patch("asyncio.ensure_future", lambda coro, *a, **kw: scheduled.append(coro)), \
             patch("harness.palace.schedule_mine_pending_shutdown_archives", lambda: None), \
             patch.object(Scheduler, "_ensure_memory_indexes", new=AsyncMock()) as ensure:
            Scheduler(agent=_Agent(), config_dir=str(ROOT / "no-such-config")).start()
        for coro in scheduled:
            coro.close()
        return ensure

    ensure = _run(main())
    assert ensure.called, "startup must index the memory collections"


def test_the_index_pass_covers_both_stores() -> None:
    from harness.scheduler import Scheduler

    scheduler = Scheduler.__new__(Scheduler)
    with patch.object(consolidation, "ensure_indexes", new=AsyncMock()) as candidates, \
         patch.object(memory_graph, "ensure_indexes", new=AsyncMock()) as graph:
        _run(Scheduler._ensure_memory_indexes(scheduler))
    assert candidates.called and graph.called


# ─── Explicit replacement ───────────────────────────────────────────────
# SUPERSEDES is the one relation that suppresses a memory, so the only writer is
# an assertion someone makes, never a similarity something measures.


class _ReplacementStore:
    """Candidate store that knows which memories exist, and records deletes."""

    def __init__(self, existing):
        self._existing = set(existing)
        self.deleted = 0

    async def find_one(self, query, projection=None):
        mid = query.get("memory_id")
        return {"memory_id": mid} if mid in self._existing else None

    async def delete_one(self, *a, **kw):
        self.deleted += 1

    async def delete_many(self, *a, **kw):
        self.deleted += 1


def _commit_with_replacement(existing, supersedes, *, duplicate_of=None):
    """Commit a memory through the real pipeline and capture any edge written."""
    store = _ReplacementStore(existing)
    written = []

    async def fake_add_edges(from_id, edges, *, source, authoritative=False):
        written.append((from_id, edges, source, authoritative))
        return len(edges)

    async def main():
        with patch("harness.palace.add_drawer", new=AsyncMock(return_value="ok")), \
             patch.object(consolidation, "_save_candidate", new=AsyncMock()), \
             patch.object(consolidation, "_schedule_post_commit", lambda *a, **kw: None), \
             patch.object(consolidation, "_is_prose_duplicate",
                          new=AsyncMock(return_value=duplicate_of)), \
             patch.object(consolidation, "_collection", new=AsyncMock(return_value=store)), \
             patch.object(memory_graph, "add_edges", fake_add_edges):
            return await consolidation.commit_candidate(
                type="semantic", content="Postgres was replaced by Mongo.",
                source="task_consolidator", supersedes_memory_id=supersedes,
            )

    return _run(main()), written, store


def test_a_duplicate_memory_does_not_supersede_anything() -> None:
    """Restating a rule is reinforcement — it is what earns a preference its
    place in the prompt. Reading it as replacement would retire rules for being
    agreed with."""
    result, written, _ = _commit_with_replacement(
        {"old"}, None, duplicate_of="old",
    )
    assert result["status"] == "duplicate"
    assert written == []


def test_an_explicit_replacement_writes_the_edge() -> None:
    result, written, store = _commit_with_replacement({"old"}, "old")
    assert result["status"] == "committed"
    (from_id, edges, source, authoritative), = written
    assert from_id == result["memory_id"]
    assert [(e["to"], e["relation"]) for e in edges] == [("old", "SUPERSEDES")]
    assert source == "explicit_replacement"
    assert authoritative is True, (
        "a stated replacement outranks whatever the classifier guessed for the pair"
    )
    assert store.deleted == 0, "the replaced memory stays in the record as history"
    assert "old" in result["detail"]


def test_a_replacement_accepts_the_memory_key_form_from_the_reports() -> None:
    """The telemetry reports name memories as `memory:<id>`; retyping the bare
    id is an error nobody would catch."""
    _, written, _ = _commit_with_replacement({"old"}, "memory:old")
    assert written and written[0][1][0]["to"] == "old"


def test_a_replacement_of_an_unknown_memory_is_refused() -> None:
    result, written, _ = _commit_with_replacement({"old"}, "not-a-memory")
    assert result["status"] == "committed", "the memory itself still commits"
    assert written == []
    assert "not a committed memory" in result["detail"]


def test_a_memory_cannot_supersede_itself() -> None:
    store = _ReplacementStore(set())
    written = []

    async def main():
        with patch("harness.palace.add_drawer", new=AsyncMock(return_value="ok")), \
             patch.object(consolidation, "_save_candidate", new=AsyncMock()), \
             patch.object(consolidation, "_schedule_post_commit", lambda *a, **kw: None), \
             patch.object(consolidation, "_is_prose_duplicate", new=AsyncMock(return_value=None)), \
             patch.object(consolidation, "_collection", new=AsyncMock(return_value=store)), \
             patch.object(memory_graph, "add_edges",
                          new=AsyncMock(side_effect=lambda *a, **kw: written.append(a))):
            result = await consolidation.commit_candidate(
                type="semantic", content="A fact.", supersedes_memory_id="  ",
            )
        return result

    result = _run(main())
    assert result["status"] == "committed"
    assert written == []


def test_an_authoritative_edge_clears_what_opposes_it() -> None:
    """The opposed-claims collapse exists because the classifier cannot settle a
    direction. Applying it to a stated one would discard the better evidence."""
    edges = _InverseAwareEdges([
        {"_id": 1, "from": "old", "to": "new", "relation": "DEPENDS_ON", "strength": 0.9},
    ])
    with patch.object(memory_graph, "_collection", new=AsyncMock(return_value=edges)):
        written = _run(memory_graph.add_edges(
            "new",
            [{"to": "old", "relation": "SUPERSEDES", "label": "replaces", "strength": 1.0}],
            source="explicit_replacement", authoritative=True,
        ))
    assert written == 1
    assert edges.written[-1]["relation"] == "SUPERSEDES"
    assert edges.deleted == [1], "the opposing edge is removed, not collapsed"


def test_an_authoritative_edge_clears_a_rival_in_the_same_direction() -> None:
    """The upsert key is (from, to, relation), so a later DEPENDS_ON round would
    otherwise sit alongside the SUPERSEDES — and the reader would inline a
    retired memory as a live prerequisite of its own replacement."""
    edges = _InverseAwareEdges([
        {"_id": 7, "from": "new", "to": "old", "relation": "DEPENDS_ON", "strength": 0.9},
    ])
    with patch.object(memory_graph, "_collection", new=AsyncMock(return_value=edges)):
        written = _run(memory_graph.add_edges(
            "new",
            [{"to": "old", "relation": "SUPERSEDES", "label": "replaces", "strength": 1.0}],
            source="explicit_replacement", authoritative=True,
        ))
    assert written == 1
    assert edges.deleted == [7], "the same-direction rival must go"


# ─── Agent fire-site wiring ─────────────────────────────────────────────
# A fire activates; it does not retrieve. These pin that boundary — the
# regression they exist for is a fire quietly growing back into a retrieval.


def _fire(matches, backing=None, ephemeral=False):
    """Build a recall fire the way a live turn does."""
    from harness.agent import GaladrielAgent

    agent = GaladrielAgent.__new__(GaladrielAgent)
    agent._session_id = {}
    agent._session_segments = {}
    with patch("harness.consolidation.memory_ids_by_recall",
               new=AsyncMock(return_value=backing or {})):
        return _run(agent._build_recall_fire(
            "chan", matches, query="how do I ship this?",
            ephemeral=ephemeral, origin="User-message",
        ))


_MATCH = [{"recall_id": "r1", "instruction": "check the deployment procedures room",
           "matched_chunk": "", "segment_source": "user"}]


def test_a_fire_carries_the_cue_and_nothing_else() -> None:
    """No memory content. The recall's instruction is a pointer, and whether to
    follow it is the model's call — pre-loading the memory makes that decision
    for them and spends context on a turn that may not need it."""
    prompt = _fire(_MATCH)
    assert prompt == "[Recall detected]\n- [r1] check the deployment procedures room"


def test_a_memory_backed_fire_names_the_memory_to_open() -> None:
    """Pursuing the cue should cost one call, not a guess about which room."""
    prompt = _fire(_MATCH, backing={"r1": "abc123"})
    assert "check the deployment procedures room" in prompt
    assert "memory(id=" in prompt
    assert "`abc123`" in prompt
    assert "deployment procedure" not in prompt.split("memory(id=")[1], (
        "the id is a pointer; the memory's own text must not ride along"
    )


def test_a_failed_backing_lookup_leaves_the_fire_intact() -> None:
    from harness.agent import GaladrielAgent

    agent = GaladrielAgent.__new__(GaladrielAgent)
    with patch("harness.consolidation.memory_ids_by_recall",
               new=AsyncMock(side_effect=RuntimeError("mongo down"))):
        prompt = _run(agent._build_recall_fire(
            "chan", _MATCH, query="q", ephemeral=False, origin="User-message",
        ))
    assert prompt == "[Recall detected]\n- [r1] check the deployment procedures room"


def _calls_in(func_name: str) -> list[str]:
    """Names called inside one function of harness/agent.py, per the AST."""
    import ast

    tree = ast.parse((ROOT / "harness" / "agent.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return [
                call.func.attr if isinstance(call.func, ast.Attribute) else
                getattr(call.func, "id", "")
                for call in ast.walk(node) if isinstance(call, ast.Call)
            ]
    raise AssertionError(f"harness/agent.py has no {func_name}")


def test_every_recall_fire_site_goes_through_the_builder() -> None:
    """Deleting the builder from one fire site would leave every graph test
    green while that site quietly diverged."""
    turn = _calls_in("_respond_locked_inner")
    assert turn.count("_build_recall_fire") == 2, (
        "both fire sites (user message, mid-turn tool result) must build their "
        "fire through the shared path"
    )
    assert "generate_recall_fire_text" not in turn, (
        "a fire assembled inline would skip the memory-id pointer"
    )


def test_no_fire_path_materialises_a_memory() -> None:
    """The architectural boundary, asserted rather than remembered: nothing on
    the fire path may read memory content."""
    builder = _calls_in("_build_recall_fire")
    assert "generate_recall_fire_text" in builder
    for forbidden in ("memory_texts", "open_memory", "expand_from_recalls"):
        assert forbidden not in builder, (
            f"{forbidden} on the fire path turns activation back into retrieval"
        )


# ─── Hygiene ────────────────────────────────────────────────────────────


class _FakeEvents:
    """Retrieval-event store: decay now counts arrivals, not aggregate stats."""

    def __init__(self, rows):
        self._rows = rows
        self.marked = []

    def find(self, query, projection=None):
        def matches(row):
            for field, value in query.items():
                if isinstance(value, dict) and "$ne" in value:
                    if row.get(field) == value["$ne"]:
                        return False
                elif row.get(field) != value:
                    return False
            return True

        return _FakeCursor([r for r in self._rows if matches(r)])

    async def update_many(self, query, update):
        self.marked.extend((query.get("retrieval_id") or {}).get("$in") or [])


class _RecordingEdges:
    def __init__(self):
        self.updates = []
        self.pruned = []
        self.deleted = 0

    async def update_many(self, query, update):
        self.updates.append((query, update))

    async def delete_many(self, query):
        self.pruned.append(query)
        self.deleted = 1
        return type("R", (), {"deleted_count": 1})()


def _arrivals(memory_id, *, times, used, kind="graph_expansion", graded=True):
    """Graded arrivals by default; `graded=False` models the real ungraded row,
    which carries no `used` key at all."""
    rows = []
    for i in range(times):
        row = {"retrieval_id": f"{memory_id}-{i}",
               "memory_key": f"memory:{memory_id}", "memory_kind": kind,
               "graded": graded}
        if graded:
            row["used"] = i < used
        rows.append(row)
    return rows


def _decay(rows):
    edges = _RecordingEdges()
    events = _FakeEvents(rows)

    async def fake_consolidation_collection(name):
        return events

    with patch.object(memory_graph, "_collection", new=AsyncMock(return_value=edges)), \
         patch.object(consolidation, "_collection", fake_consolidation_collection):
        report = _run(memory_graph.decay_unhelpful_edges())
    return edges, report, events


def test_decay_weakens_edges_into_an_unused_memory() -> None:
    edges, report, events = _decay(_arrivals("bad", times=10, used=0))
    assert edges.updates, "an edge whose target is never used must lose strength"
    query, update = edges.updates[0]
    assert query["to"] == {"$in": ["bad"]}
    assert set(query["relation"]["$in"]) == set(memory_graph._FOLLOWED), (
        "only relations expansion injects may be judged by expansion telemetry"
    )
    assert update["$inc"]["strength"] < 0
    assert "pruned" in report


def test_decay_ignores_a_memory_that_gets_used() -> None:
    edges, report, events = _decay(_arrivals("good", times=10, used=8))
    assert edges.updates == [] and report == ""


def test_decay_ignores_arrivals_no_edge_caused() -> None:
    """A memory the agent opened directly is not evidence about the edges
    pointing into it — judging them by it would punish a foundation for being
    consulted on its own."""
    edges, report, events = _decay(_arrivals("opened", times=10, used=0, kind="memory_open"))
    assert edges.updates == [] and report == ""


def test_decay_ignores_arrivals_nobody_graded() -> None:
    """Ungraded is unmeasured, not unhelpful. Most episodes never reach a
    task-end boundary, so counting silence as a verdict would prune correct
    edges for lack of evidence — the one thing decay must never do."""
    edges, report, events = _decay(_arrivals("unjudged", times=10, used=0, graded=False))
    assert edges.updates == [] and report == ""


def test_decay_prune_cannot_reach_an_edge_it_did_not_decay() -> None:
    """A freshly written low-confidence edge has no telemetry at all; deleting
    it during someone else's decay pass would be invisible and unattributable."""
    edges, _, _ = _decay(_arrivals("bad", times=10, used=0))
    assert edges.pruned, "a prune must have happened"
    query = edges.pruned[0]
    assert query["to"] == {"$in": ["bad"]}, (
        "the prune must be scoped to the decayed set, not the whole collection"
    )


def test_decay_spends_its_evidence_once() -> None:
    """Reflection runs several times a workday off a query with no time bound.
    Without a watermark one afternoon's arrivals would weaken the same edge on
    every pass and prune it within a day."""
    edges, _, events = _decay(_arrivals("bad", times=10, used=0))
    assert edges.updates, "the first pass acts"
    assert sorted(events.marked) == sorted(f"bad-{i}" for i in range(10))


def test_decay_waits_for_enough_evidence() -> None:
    """One unused injection is noise, not a verdict on the edge."""
    edges, report, events = _decay(_arrivals("new", times=2, used=0))
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
        test_shortlist_reaches_memories_older_than_the_dedupe_window,
        test_shortlist_stores_the_embeddings_it_computes,
        test_shortlist_survives_an_embedding_write_failure,
        test_commit_schedules_edge_classification,
        test_edge_classification_respects_its_kill_switch,
        test_kill_switches_are_independent,
        test_free_wording_binds_to_the_same_behaviour_in_every_domain,
        test_each_domain_relation_gets_the_behaviour_it_was_given,
        test_a_replacement_is_refused_from_the_classifier_in_every_domain,
        test_graph_indexes_cover_both_traversal_directions,
        test_candidate_indexes_cover_the_turn_path,
        test_index_creation_is_a_no_op_without_a_database,
        test_a_failing_index_does_not_stop_the_boot,
        test_the_boot_path_ensures_the_indexes,
        test_the_index_pass_covers_both_stores,
        test_a_duplicate_memory_does_not_supersede_anything,
        test_an_explicit_replacement_writes_the_edge,
        test_a_replacement_accepts_the_memory_key_form_from_the_reports,
        test_a_replacement_of_an_unknown_memory_is_refused,
        test_a_memory_cannot_supersede_itself,
        test_an_authoritative_edge_clears_what_opposes_it,
        test_an_authoritative_edge_clears_a_rival_in_the_same_direction,
        test_a_fire_carries_the_cue_and_nothing_else,
        test_a_memory_backed_fire_names_the_memory_to_open,
        test_no_fire_path_materialises_a_memory,
        test_a_failed_backing_lookup_leaves_the_fire_intact,
        test_every_recall_fire_site_goes_through_the_builder,
        test_decay_weakens_edges_into_an_unused_memory,
        test_decay_ignores_a_memory_that_gets_used,
        test_decay_ignores_arrivals_no_edge_caused,
        test_decay_ignores_arrivals_nobody_graded,
        test_decay_prune_cannot_reach_an_edge_it_did_not_decay,
        test_decay_spends_its_evidence_once,
        test_decay_waits_for_enough_evidence,
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print(f"{len(tests)}/{len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

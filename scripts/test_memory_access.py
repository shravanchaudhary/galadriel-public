#!/usr/bin/env python3
"""Tests for the unified memory reader.

The design this pins down: a recall fire *activates* (something here may
matter), and opening a memory *retrieves*. Graph expansion belongs to the
second, because that is the moment the memory is actually being used, and
because the alternative loads a dependency chain for a turn that may never
touch it.

So the properties worth protecting are:

  - opening returns the memory whole, with what it would be wrong without
  - everything else is a stub carrying an id, so the agent walks the graph by
    intent rather than by the harness guessing depth
  - a retired rule never presents itself as current
  - an id that is not a curated memory still opens, as verbatim history

No model or Mongo required: the candidate store and edge store are fakes.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import consolidation  # noqa: E402
from harness import memory_access  # noqa: E402
from harness import memory_graph  # noqa: E402
from harness import palace  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _open(memories, edges, memory_id):
    """Open a memory against a stubbed candidate store and graph."""
    texts = {
        mid: {"memory_id": mid, "content": body, "type": "semantic"}
        for mid, body in memories.items()
    }

    async def fake_texts(ids):
        return {mid: texts[mid] for mid in ids if mid in texts}

    async def fake_from(mid, relations=None):
        return [
            {"to": e["to"], "relation": e["relation"], "label": "", "strength": 0.5}
            for e in edges
            if e["from"] == mid and (relations is None or e["relation"] in relations)
        ]

    async def fake_into(mid, relations=None):
        return [
            {"from": e["from"], "relation": e["relation"], "label": "", "strength": 0.5}
            for e in edges
            if e["to"] == mid and (relations is None or e["relation"] in relations)
        ]

    async def fake_replacements(ids):
        return {
            e["to"]: e["from"] for e in edges
            if e["relation"] == "SUPERSEDES" and e["to"] in ids
        }

    with patch.object(consolidation, "memory_texts", fake_texts), \
         patch.object(memory_graph, "edges_from", fake_from), \
         patch.object(memory_graph, "edges_into", fake_into), \
         patch.object(memory_graph, "replacements", fake_replacements):
        return _run(memory_access.open_memory(memory_id))


def _edge(frm, to, relation):
    return {"from": frm, "to": to, "relation": relation}


# ─── Opening ────────────────────────────────────────────────────────────


def test_opening_returns_the_memory_whole() -> None:
    text, expanded = _open({"m1": "run the compatibility check first"}, [], "m1")
    assert "run the compatibility check first" in text
    assert "`m1`" in text
    assert expanded == []


def test_a_memory_id_prefix_is_accepted() -> None:
    """Reports and telemetry name memories as `memory:<id>`; retyping the bare
    id is an error nobody would catch."""
    text, _ = _open({"m1": "the content"}, [], "memory:m1")
    assert "the content" in text


def test_prerequisites_arrive_inline_and_first() -> None:
    """A rule without the fact it rests on is a rule you cannot apply."""
    text, expanded = _open(
        {"m1": "run the compatibility check before migrating",
         "m2": "the check covers additive columns only"},
        [_edge("m1", "m2", "DEPENDS_ON")],
        "m1",
    )
    assert "the check covers additive columns only" in text
    assert text.index("additive columns") < text.index("before migrating"), (
        "what the memory rests on has to read before the memory"
    )
    assert expanded == ["m2"]


def test_companions_are_stubs_not_content() -> None:
    """The bound that makes a thousand neighbours cost the same as three."""
    body = "a companion with a long body " * 20
    text, expanded = _open(
        {"m1": "the opened memory", "m2": body},
        [_edge("m1", "m2", "RECALL_WITH")],
        "m1",
    )
    assert "Also linked" in text
    assert "`m2`" in text
    assert body.strip() not in text, "a companion is a signpost, not a payload"
    stub = [line for line in text.splitlines() if "`m2`" in line][0]
    assert len(stub) < len(body) / 4, f"stub is not a summary: {len(stub)}"
    assert expanded == [], "only inlined prerequisites count as expansions"


def test_links_show_both_directions() -> None:
    """What this rests on, and what rests on this — navigating only outward
    strands every foundational memory."""
    text, _ = _open(
        {"m1": "the opened memory", "up": "something built on it",
         "side": "a companion"},
        [_edge("up", "m1", "DEPENDS_ON"), _edge("m1", "side", "RECALL_WITH")],
        "m1",
    )
    assert "← `up`" in text, "incoming edges must be navigable"
    assert "→ `side`" in text


def test_a_dense_neighbourhood_collapses_into_a_count() -> None:
    memories = {"m1": "the opened memory"}
    edges = []
    for i in range(30):
        memories[f"n{i}"] = f"neighbour {i}"
        edges.append(_edge("m1", f"n{i}", "RECALL_WITH"))
    text, _ = _open(memories, edges, "m1")
    assert "more" in text
    listed = text.count("→ `n")
    assert listed <= memory_access._STUB_LIMIT, listed
    assert f"{30 - listed} more" in text, "what was dropped has to be visible"


def test_prerequisites_past_the_budget_degrade_to_stubs() -> None:
    memories = {"m1": "the opened memory"}
    edges = []
    for i in range(6):
        memories[f"p{i}"] = f"prerequisite {i}"
        edges.append(_edge("m1", f"p{i}", "DEPENDS_ON"))
    text, expanded = _open(memories, edges, "m1")
    assert len(expanded) == memory_access._INLINE_PREREQUISITES
    assert "Also linked" in text, "the rest stay reachable, just not inlined"


def test_provenance_is_navigable_but_never_inlined() -> None:
    """CAUSED_BY explains how a memory came about. Useful to be able to reach,
    never worth spending the turn's context on unasked."""
    text, expanded = _open(
        {"m1": "the lesson", "why": "the failure that taught it " * 20},
        [_edge("m1", "why", "CAUSED_BY")],
        "m1",
    )
    assert expanded == [], "provenance is never an expansion"
    assert "Needed first" not in text, "provenance is not a prerequisite"
    assert "→ `why` CAUSED_BY" in text, "but it stays reachable"


# ─── Supersession ───────────────────────────────────────────────────────


def test_a_replaced_memory_says_so_before_its_content() -> None:
    """Opening a retired rule is legitimate — it is history. Presenting it as
    current is not."""
    text, _ = _open(
        {"old": "use the shared API key", "new": "authenticate with OIDC"},
        [_edge("new", "old", "SUPERSEDES")],
        "old",
    )
    assert "has been replaced" in text
    assert "`new`" in text
    assert text.index("has been replaced") < text.index("use the shared API key")


def test_the_replacement_is_not_listed_as_an_ordinary_link() -> None:
    text, _ = _open(
        {"old": "the old rule", "new": "the current rule"},
        [_edge("new", "old", "SUPERSEDES")],
        "old",
    )
    assert "SUPERSEDES" not in text, (
        "supersession is a banner, not one more thing to browse"
    )


def test_search_marks_a_retired_memory() -> None:
    """Search is where a retired rule is most dangerous — it was written to be
    findable, and nothing else on the line says it no longer applies."""
    hits = [{"memory_id": "old", "type": "procedural", "content": "the old rule"}]
    with patch.object(consolidation, "shortlist_neighbours",
                      new=AsyncMock(return_value=hits)), \
         patch.object(memory_graph, "replacements",
                      new=AsyncMock(return_value={"old": "new"})), \
         patch.object(palace, "kg_search", return_value=[]):
        text = _run(memory_access.find("the rule"))
    assert "REPLACED by `new`" in text


def test_a_retired_prerequisite_is_flagged_where_it_is_inlined() -> None:
    """The edge was correct when written. Nothing about it says the thing it
    points at has since been replaced."""
    text, _ = _open(
        {"m1": "the rule", "p": "the old foundation", "np": "the new foundation"},
        [_edge("m1", "p", "DEPENDS_ON"), _edge("np", "p", "SUPERSEDES")],
        "m1",
    )
    assert "the old foundation" in text
    assert "REPLACED by `np`" in text


def test_a_kg_only_memory_opens_with_its_facts() -> None:
    """A semantic memory committed as triplets stores no prose, but still gets
    an id and a recall trigger — so it must not open blank."""
    async def fake_texts(ids):
        return {"k1": {"memory_id": "k1", "content": "", "type": "semantic",
                       "kg_triplets": [["Tower", "deploys via", "CodePipeline"]]}}

    with patch.object(consolidation, "memory_texts", fake_texts), \
         patch.object(memory_graph, "edges_from", new=AsyncMock(return_value=[])), \
         patch.object(memory_graph, "edges_into", new=AsyncMock(return_value=[])), \
         patch.object(memory_graph, "replacements", new=AsyncMock(return_value={})):
        text, _ = _run(memory_access.open_memory("k1"))
    assert "Tower — deploys via — CodePipeline" in text


# ─── Verbatim fallback ──────────────────────────────────────────────────


def test_an_unknown_id_falls_through_to_the_archive() -> None:
    """Episode drawers are the bulk of the corpus and carry no curated links —
    raw conversation is evidence for a memory, not a memory."""
    drawer = {"id": "d1", "text": "a verbatim conversation chunk",
              "room": "conversations", "hall": "general"}

    async def no_texts(ids):
        return {}

    with patch.object(consolidation, "memory_texts", no_texts), \
         patch("harness.palace.get_drawer", return_value=drawer):
        text, expanded = _run(memory_access.open_memory("d1"))
    assert "a verbatim conversation chunk" in text
    assert "verbatim history" in text
    assert expanded == []


def test_an_id_that_exists_nowhere_says_where_ids_come_from() -> None:
    async def no_texts(ids):
        return {}

    with patch.object(consolidation, "memory_texts", no_texts), \
         patch("harness.palace.get_drawer", return_value=None):
        text, _ = _run(memory_access.open_memory("nope"))
    assert "nothing stored under" in text
    assert "memory(query=" in text


# ─── Corpus labelling ───────────────────────────────────────────────────
# Both corpora live in one store, so a conversation search can surface a learned
# memory with nothing to say so. That is the confusion the label removes.


def test_a_learned_memory_is_marked_in_a_conversation_search() -> None:
    markdown = (
        "**Palace search (DocumentDB):**\n\n"
        "### 1. agent / conversations / hall=general / id=`chat-1`\n"
        "we talked about deploys\n\n"
        "### 2. agent / procedures / hall=deploy / id=`m1`\n"
        "run the compatibility check first\n"
    )

    async def fake_texts(ids):
        return {"m1": {"memory_id": "m1", "content": "…", "type": "procedural"}}

    with patch.object(consolidation, "memory_texts", fake_texts):
        out = _run(memory_access.label_curated(markdown))
    marked = [line for line in out.splitlines() if "LEARNED" in line]
    assert len(marked) == 1 and "id=`m1`" in marked[0], marked
    assert "PROCEDURAL" in marked[0]
    assert "chat-1" not in marked[0], "verbatim history is not a learned memory"


def test_labelling_leaves_a_result_alone_when_nothing_is_curated() -> None:
    markdown = "### 1. agent / conversations / hall=general / id=`chat-1`\nwords\n"

    async def no_texts(ids):
        return {}

    with patch.object(consolidation, "memory_texts", no_texts):
        assert _run(memory_access.label_curated(markdown)) == markdown


def test_labelling_survives_a_lookup_failure() -> None:
    markdown = "### 1. agent / knowledge / hall=x / id=`m1`\nwords\n"
    with patch.object(consolidation, "memory_texts",
                      new=AsyncMock(side_effect=RuntimeError("mongo down"))):
        assert _run(memory_access.label_curated(markdown)) == markdown


def test_labelling_skips_a_result_with_no_ids() -> None:
    assert _run(memory_access.label_curated("no ids here")) == "no ids here"


# ─── Search ─────────────────────────────────────────────────────────────


def test_search_returns_openable_ids_not_content() -> None:
    hits = [
        {"memory_id": "m1", "type": "procedural", "content": "how to deploy"},
        {"memory_id": "m2", "type": "semantic", "content": "the deploy pipeline"},
    ]
    with patch.object(consolidation, "shortlist_neighbours",
                      new=AsyncMock(return_value=hits)), \
         patch.object(memory_graph, "replacements", new=AsyncMock(return_value={})), \
         patch.object(palace, "kg_search", return_value=[]):
        text = _run(memory_access.find("deploying"))
    assert "`m1`" in text and "`m2`" in text
    assert "memory(id=" in text


def test_search_with_no_hits_points_at_the_other_corpus() -> None:
    with patch.object(consolidation, "shortlist_neighbours",
                      new=AsyncMock(return_value=[])), \
         patch.object(memory_graph, "replacements", new=AsyncMock(return_value={})), \
         patch.object(palace, "kg_search", return_value=[]):
        text = _run(memory_access.find("nothing learned about this"))
    assert "palace_search" in text, (
        "curated memory being empty is not the same as knowing nothing"
    )


_KG_ROW = {"subject": "clodexa", "predicate": "north_star",
           "object": "getting_more_meetings", "valid_from": "2026-08-20", "valid_to": None}


def test_search_lists_live_kg_facts_even_with_no_curated_hit() -> None:
    """A fact filed before the learn pipeline has no candidate to rank, and a
    candidate's text is frozen at commit — the graph is the live record."""
    with patch.object(consolidation, "shortlist_neighbours",
                      new=AsyncMock(return_value=[])), \
         patch.object(memory_graph, "replacements", new=AsyncMock(return_value={})), \
         patch.object(palace, "kg_search", return_value=[_KG_ROW]) as kg:
        text = _run(memory_access.find("north_star", limit=5))
    kg.assert_called_once_with("north_star", 6)
    assert "--[north_star]-> `getting_more_meetings`" in text
    assert "[current]" in text
    assert "Nothing has been learned" not in text, (
        "a graph hit is knowledge; the not-learned message would be a lie"
    )


def test_search_appends_kg_facts_under_the_stubs_and_signposts_overflow() -> None:
    hits = [{"memory_id": "m1", "type": "semantic", "content": "clodexa facts"}]
    rows = [_KG_ROW, {**_KG_ROW, "predicate": "is", "object": "an agency"}]
    with patch.object(consolidation, "shortlist_neighbours",
                      new=AsyncMock(return_value=hits)), \
         patch.object(memory_graph, "replacements", new=AsyncMock(return_value={})), \
         patch.object(palace, "kg_search", return_value=rows):
        text = _run(memory_access.find("clodexa", limit=1))
    assert text.index("`m1`") < text.index("--[north_star]->")
    assert "--[is]->" not in text and "palace_kg_query" in text, (
        "past the limit, point at the narrower tool rather than truncating silently"
    )


def test_search_needs_something_to_search_for() -> None:
    assert "[memory]" in _run(memory_access.find("   "))


# ─── Telemetry contract ─────────────────────────────────────────────────


def test_inlined_ids_match_what_the_reader_shows() -> None:
    """The caller logs telemetry from these ids. If they drifted from what was
    rendered, edge decay would be judging arrivals that never happened."""
    edges = [
        {"to": "p1", "relation": "DEPENDS_ON", "strength": 0.9},
        {"to": "p2", "relation": "RECALL_BEFORE", "strength": 0.8},
        {"to": "c1", "relation": "RECALL_WITH", "strength": 0.7},
    ]

    async def fake_from(mid, relations=None):
        return [e for e in edges if relations is None or e["relation"] in relations]

    with patch.object(memory_graph, "edges_from", fake_from):
        ids = _run(memory_access.inlined_prerequisite_ids("m1"))
    assert ids == ["p1", "p2"], ids


def main() -> int:
    tests = [
        test_opening_returns_the_memory_whole,
        test_a_memory_id_prefix_is_accepted,
        test_prerequisites_arrive_inline_and_first,
        test_companions_are_stubs_not_content,
        test_links_show_both_directions,
        test_a_dense_neighbourhood_collapses_into_a_count,
        test_prerequisites_past_the_budget_degrade_to_stubs,
        test_provenance_is_navigable_but_never_inlined,
        test_a_replaced_memory_says_so_before_its_content,
        test_the_replacement_is_not_listed_as_an_ordinary_link,
        test_search_marks_a_retired_memory,
        test_a_retired_prerequisite_is_flagged_where_it_is_inlined,
        test_a_kg_only_memory_opens_with_its_facts,
        test_an_unknown_id_falls_through_to_the_archive,
        test_an_id_that_exists_nowhere_says_where_ids_come_from,
        test_a_learned_memory_is_marked_in_a_conversation_search,
        test_labelling_leaves_a_result_alone_when_nothing_is_curated,
        test_labelling_survives_a_lookup_failure,
        test_labelling_skips_a_result_with_no_ids,
        test_search_returns_openable_ids_not_content,
        test_search_with_no_hits_points_at_the_other_corpus,
        test_search_lists_live_kg_facts_even_with_no_curated_hit,
        test_search_appends_kg_facts_under_the_stubs_and_signposts_overflow,
        test_search_needs_something_to_search_for,
        test_inlined_ids_match_what_the_reader_shows,
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print(f"{len(tests)}/{len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

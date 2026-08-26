"""Typed edges between memories, so one memory can be reached through another.

The recall matcher answers *when* something becomes relevant. It cannot answer
what has to come *with* it. A memory that reads "use method B" is inert without
"for library X under condition Y", and a memory whose only claim to relevance is
structural ("you need this because you just recalled that") never surfaces at
all, because nothing in the conversation resembles it.

This module is the second stage: a small graph whose nodes are committed
memories (`memory_candidates.memory_id`, already minted by the candidate
pipeline) and whose edges say how they relate *functionally* — not how similar
they are. Similarity is what the encoder already gives us and it is the wrong
question here; two memories can be near-identical in wording and unrelated in
use, or worded nothing alike and strictly ordered.

Deliberately not a world-knowledge graph. Edges like `LLM -uses-> transformer`
are of no help to an agent deciding what to do next. Edges between *learnings*
are:

    Failure 17  --CAUSED_BY-->      (nothing; it is the root)
    Lesson 8    --CAUSED_BY-->      Failure 17
    Procedure 3 --DEPENDS_ON-->     Lesson 8
    Procedure 3 --RECALL_BEFORE-->  "edit retrieval thresholds"

## Open names, closed policies

An edge only does anything if traversal knows what to *do* with it, so the set
of behaviours is fixed. An unbounded relation vocabulary would not add power, it
would add inert rows: an edge whose relation no policy covers is stored and
never followed.

The worse failure is fragmentation. MOTIVATED / LED_TO / RESULTED_IN /
TRIGGERED are all CAUSED_BY; as separate relations, a traversal following
CAUSED_BY silently misses every one of them, and the graph quietly loses edges
without any error.

So the classifier may propose any wording it likes, but must bind it to one of
the closed behaviours. The free wording is kept in `label` — it is provenance,
and it shows which vocabulary the model actually reaches for, which is what lets
the periodic consolidator promote a recurring label into a first-class relation
once repetition has earned it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger("galadriel.memory_graph")

EDGES_COLLECTION = "memory_edges"

# ─── Closed relation vocabulary ────────────────────────────────────────────
# Ordered by how aggressively traversal follows them. Each entry is
# (relation, one-line meaning) and every relation must appear in exactly one
# policy set below — `validate_vocabulary()` enforces that at import time in
# tests, so adding a relation without a policy fails loudly instead of
# producing edges nothing ever follows.

RELATIONS: dict[str, str] = {
    "DEPENDS_ON": "A is meaningless without B; B is a prerequisite for understanding A.",
    "RECALL_BEFORE": "B must be in context before acting on A.",
    "RECALL_WITH": "B is usually useful alongside A, but A stands on its own.",
    "SUPERSEDES": "A replaces B; B is historical.",
    "CONTRADICTS": "A and B disagree and cannot both be acted on.",
    "CAUSED_BY": "A was learned because of B.",
}

# Prerequisites: always followed, and injected BEFORE the memory that pulled
# them in. This is the ordering the whole design exists for.
POLICY_PREREQUISITE = frozenset({"DEPENDS_ON", "RECALL_BEFORE"})
# Companions: followed only while budget remains, injected after.
POLICY_COMPANION = frozenset({"RECALL_WITH"})
# Resolved before injection: the superseded side is never injected as live.
POLICY_RESOLVE = frozenset({"SUPERSEDES"})
# Both sides surface, flagged, so the model knows the disagreement exists
# rather than silently picking one.
POLICY_CONFLICT = frozenset({"CONTRADICTS"})
# Followed only when an explanation is being asked for — provenance, not
# operating context.
POLICY_ON_DEMAND = frozenset({"CAUSED_BY"})

_ALL_POLICIES = (
    POLICY_PREREQUISITE, POLICY_COMPANION, POLICY_RESOLVE,
    POLICY_CONFLICT, POLICY_ON_DEMAND,
)

# Relations the classifier may NOT mint, and why.
#
# `SUPERSEDES` is the only relation whose policy *suppresses* a memory, and that
# asymmetry breaks the containment the other relations rely on. A wrong
# DEPENDS_ON injects noise, which shows up as an unused retrieval and decays. A
# wrong SUPERSEDES stops a memory being injected at all, so it never accumulates
# the telemetry that would weaken the edge — the error is self-concealing and
# permanent.
#
# Measured on a small corpus, not assumed: the classifier proposed SUPERSEDES
# twice and was wrong both times, once at 0.95 confidence — claiming a memory
# replaced the one describing that same subject's defect. Two samples do not
# make a rate, but the asymmetry above means the cost of being wrong is not
# symmetric either, and model confidence gave no warning.
#
# Supersession needs an event, not a resemblance. Two memories making the same
# claim is reinforcement — the dedupe layer's job, and the evidence behind
# preference promotion — and reading it as replacement would retire a rule for
# being restated. So the only writer is an explicit one: a consolidator naming
# the memory a new one replaces (`commit_candidate(supersedes_memory_id=...)`),
# which is a decision someone made rather than a similarity someone measured.
# Traversal honours the relation wherever it is true.
CLASSIFIER_FORBIDDEN = frozenset({"SUPERSEDES"})

# Relations where both directions cannot hold at once. A -> B DEPENDS_ON plus
# B -> A CAUSED_BY is not a rich description, it is the model failing to settle
# on a direction, and traversal would follow the cycle in whichever order the
# edges happened to be written.
ASYMMETRIC = frozenset({"DEPENDS_ON", "RECALL_BEFORE", "SUPERSEDES", "CAUSED_BY"})

# Wordings the classifier reaches for that mean an existing relation. This is a
# safety net, not the mechanism — the prompt constrains `relation` to the closed
# set and carries the free wording in `label`. Kept small on purpose; a growing
# map here is a sign the prompt needs fixing.
_SYNONYMS: dict[str, str] = {
    "REQUIRES": "DEPENDS_ON",
    "PREREQUISITE_OF": "DEPENDS_ON",
    "NEEDS": "DEPENDS_ON",
    "MOTIVATED": "CAUSED_BY",
    "MOTIVATED_BY": "CAUSED_BY",
    "LED_TO": "CAUSED_BY",
    "RESULTED_IN": "CAUSED_BY",
    "TRIGGERED": "CAUSED_BY",
    "BECAUSE_OF": "CAUSED_BY",
    "DERIVED_FROM": "CAUSED_BY",
    "LEARNED_FROM": "CAUSED_BY",
    "REPLACES": "SUPERSEDES",
    "OVERRIDES": "SUPERSEDES",
    "CONFLICTS_WITH": "CONTRADICTS",
    "WARN_BEFORE": "RECALL_BEFORE",
    "APPLIES_WHEN": "RECALL_WITH",
    "RELATED_TO": "RECALL_WITH",
    "SEE_ALSO": "RECALL_WITH",
}


def validate_vocabulary() -> None:
    """Every relation has exactly one policy, and no policy invents relations.

    Called from tests. A relation with no policy would produce edges that are
    written, counted, and never traversed — a silent failure, which is why this
    is asserted rather than left to review.
    """
    seen: dict[str, int] = {r: 0 for r in RELATIONS}
    for policy in _ALL_POLICIES:
        for relation in policy:
            if relation not in RELATIONS:
                raise AssertionError(f"policy names unknown relation {relation!r}")
            seen[relation] += 1
    missing = [r for r, n in seen.items() if n == 0]
    if missing:
        raise AssertionError(f"relations with no traversal policy: {missing}")
    duplicated = [r for r, n in seen.items() if n > 1]
    if duplicated:
        raise AssertionError(f"relations in more than one policy: {duplicated}")


def relation_catalog() -> str:
    """The vocabulary as prompt text, so the classifier and the docs can't drift."""
    return "\n".join(f"  {name}: {meaning}" for name, meaning in RELATIONS.items())


def normalize_relation(raw) -> str | None:
    """Bind a proposed relation onto the closed set, or None if it won't bind."""
    text = str(raw or "").strip().upper().replace(" ", "_").replace("-", "_")
    if not text:
        return None
    if text in RELATIONS:
        return text
    return _SYNONYMS.get(text)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _collection():
    try:
        from .db_ops import get_db
        db = get_db()
        if db is None:
            return None
        return db[EDGES_COLLECTION]
    except Exception as e:
        log.warning(f"{EDGES_COLLECTION} collection unavailable: {e}")
        return None


def clean_edges(raw, *, from_id: str, allow_forbidden: bool = False) -> list[dict]:
    """Validate proposed edges, dropping anything unusable.

    Silently drops rather than erroring: this runs on model output inside a
    background consolidation pass, where one malformed edge should cost that
    edge and nothing else.

    `allow_forbidden` is for the non-model writers (dedupe, explicit
    maintenance), which may assert supersession because they can establish it.
    """
    if not isinstance(raw, list):
        return []
    # One edge per target, strongest wins. Traversal marks a target visited on
    # first reach, so a second relation to the same memory changes nothing about
    # what gets injected — it is inert data that only makes the graph look
    # denser than it is. Two relations to one target also means the model
    # hedged, and the hedge is not information worth storing.
    best: dict[str, dict] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        to_id = str(item.get("to") or "").strip()
        relation = normalize_relation(item.get("relation"))
        if not to_id or not relation or to_id == from_id:
            continue
        if relation in CLASSIFIER_FORBIDDEN and not allow_forbidden:
            log.info("Dropped a proposed %s edge: not classifier-assignable.", relation)
            continue
        try:
            strength = float(item.get("strength", 0.5))
        except (TypeError, ValueError):
            strength = 0.5
        edge = {
            "to": to_id,
            "relation": relation,
            "label": str(item.get("label") or "").strip()[:60],
            "strength": max(0.0, min(1.0, strength)),
        }
        incumbent = best.get(to_id)
        if incumbent is None or edge["strength"] > incumbent["strength"]:
            best[to_id] = edge
    return list(best.values())


async def _find_inverse(coll, from_id: str, to_id: str) -> dict | None:
    """Any edge running the other way between this pair.

    Each memory is classified separately, so the two directions are proposed in
    different model calls and the conflict can only be caught here, at write
    time. Matches *any* relation, not just asymmetric ones, so that a pair
    already collapsed to RECALL_WITH is not re-promoted to a dependency by the
    next classification round.
    """
    try:
        return await coll.find_one(
            {"from": to_id, "to": from_id},
            {"_id": 1, "relation": 1, "strength": 1},
        )
    except Exception as e:
        log.warning(f"Inverse-edge check failed ({from_id} <-> {to_id}): {e}")
        return None


async def add_edges(
    from_id: str, edges: list[dict], *, source: str, authoritative: bool = False,
) -> int:
    """Persist validated edges. Returns how many were written.

    `authoritative` is for a writer that can establish direction from an event
    rather than infer it from wording — an explicit replacement, say. The
    opposed-claims collapse below exists because the classifier cannot settle a
    direction; applying it to a stated one would discard the better evidence, so
    an authoritative edge removes what runs against it instead.
    """
    if not from_id or not edges:
        return 0
    coll = await _collection()
    if coll is None:
        return 0
    written = 0
    for edge in edges:
        if authoritative:
            # Both directions, and every other relation in this direction. The
            # upsert key is (from, to, relation), so without the second clause a
            # later classifier round proposing NEW --DEPENDS_ON--> OLD would sit
            # alongside NEW --SUPERSEDES--> OLD: the reader would inline a
            # retired memory as a live prerequisite of its own replacement.
            try:
                removed = await coll.delete_many({"$or": [
                    {"from": edge["to"], "to": from_id},
                    {"from": from_id, "to": edge["to"],
                     "relation": {"$ne": edge["relation"]}},
                ]})
                if getattr(removed, "deleted_count", 0):
                    log.info(
                        "Removed %d edge(s) competing with an authoritative %s %s -> %s.",
                        removed.deleted_count, edge["relation"], from_id, edge["to"],
                    )
            except Exception as e:
                log.warning(f"Could not clear competing edges: {e}")
        elif edge["relation"] in ASYMMETRIC:
            # Only one direction of a dependency can hold. When the model
            # asserts both, neither tiebreak that suggests itself is sound:
            # arrival order is creation order, which has nothing to do with
            # which memory rests on which, and confidence was observed backing
            # the wrong direction more strongly than the right one.
            #
            # So stop pretending to know the direction. What two opposed claims
            # do reliably establish is that the pair belongs together, which is
            # exactly RECALL_WITH. The pair keeps its co-injection and loses the
            # ordering nothing supports. `_find_inverse` matches any relation,
            # so this collapse is stable against a later round proposing the
            # dependency again. A conservative fallback drawn from few examples
            # — replace it with a real tiebreak when one exists, not with a
            # guess.
            inverse = await _find_inverse(coll, from_id, edge["to"])
            if inverse is not None:
                if inverse.get("relation") in ASYMMETRIC:
                    log.info(
                        "Opposed %s claims between %s and %s — collapsing to "
                        "RECALL_WITH.", edge["relation"], from_id, edge["to"],
                    )
                    edge = {
                        **edge,
                        "relation": "RECALL_WITH",
                        "label": "opposed directions",
                        "strength": max(
                            edge["strength"], float(inverse.get("strength") or 0.5),
                        ),
                    }
                    try:
                        await coll.delete_one({"_id": inverse["_id"]})
                    except Exception as e:
                        log.warning(f"Could not remove inverse edge: {e}")
                        continue
                else:
                    log.info(
                        "Skipped %s %s -> %s: a %s edge already runs the other way.",
                        edge["relation"], from_id, edge["to"], inverse.get("relation"),
                    )
                    continue
        doc = {
            "from": from_id,
            "to": edge["to"],
            "relation": edge["relation"],
            "label": edge.get("label", ""),
            "strength": edge.get("strength", 0.5),
            "source": source,
            "created_at": _now(),
            "created_at_ts": _now().timestamp(),
        }
        try:
            await coll.update_one(
                {"from": from_id, "to": edge["to"], "relation": edge["relation"]},
                {"$set": doc},
                upsert=True,
            )
            written += 1
        except Exception as e:
            log.warning(f"Edge write failed ({from_id} -> {edge['to']}): {e}")
    return written


async def edges_from(
    memory_id: str, relations: frozenset[str] | set[str] | None = None,
) -> list[dict]:
    """Outgoing edges, strongest first. Projected — never the whole row."""
    coll = await _collection()
    if coll is None or not memory_id:
        return []
    query: dict = {"from": memory_id}
    if relations:
        query["relation"] = {"$in": sorted(relations)}
    try:
        cursor = coll.find(
            query, {"_id": 0, "to": 1, "relation": 1, "label": 1, "strength": 1},
        ).sort("strength", -1)
        return [doc async for doc in cursor]
    except Exception as e:
        log.warning(f"Edge query failed for {memory_id}: {e}")
        return []


async def edges_into(
    memory_id: str, relations: frozenset[str] | set[str] | None = None,
) -> list[dict]:
    """Incoming edges, strongest first — what rests on this memory.

    Traversal only ever walks outward, because a fire asks "what does this need".
    A reader asks the other question too: opening a foundation should show what
    was built on it, which is the only way to navigate from a general memory to
    the specific ones that use it.
    """
    coll = await _collection()
    if coll is None or not memory_id:
        return []
    query: dict = {"to": memory_id}
    if relations:
        query["relation"] = {"$in": sorted(relations)}
    try:
        cursor = coll.find(
            query, {"_id": 0, "from": 1, "relation": 1, "label": 1, "strength": 1},
        ).sort("strength", -1)
        return [doc async for doc in cursor]
    except Exception as e:
        log.warning(f"Incoming edge query failed for {memory_id}: {e}")
        return []


async def ensure_indexes() -> None:
    """Traversal indexes. Idempotent; safe to call on every startup."""
    coll = await _collection()
    if coll is None:
        return
    try:
        await coll.create_index([("from", 1), ("relation", 1)])
        await coll.create_index([("to", 1), ("relation", 1)])
    except Exception as e:
        log.warning(f"Edge index creation failed: {e}")


_CLASSIFIER_SYSTEM = """\
You maintain a graph of how an agent's learned memories relate to each other.

Given a NEW memory and a short list of EXISTING memories, decide whether the new \
one has a *functional* relationship to any of them. Similarity is not a \
relationship — these candidates were already selected for being similar. Ask \
instead: does one require the other, replace it, contradict it, explain it, or \
need to be in mind before acting on it?

Relations (use these exact names):
%(catalog)s

Every edge you return reads in this direction:

    NEW --relation--> EXISTING

Check that order before returning one. Given NEW = "run the compatibility check \
before applying a schema migration" and EXISTING = "the compatibility check only \
covers additive column changes":

  CORRECT:   NEW --DEPENDS_ON--> EXISTING   (the procedure is wrong without knowing what the check covers)
  BACKWARDS: NEW --CAUSED_BY--> EXISTING    (the check's scope did not cause the procedure)

If a relation only makes sense read the other way round, leave it out — there is \
no reverse edge to return. Labels like "established by" or "context for" \
describe the existing memory acting on the new one, which is backwards.

Dependency is about meaning, not topic. Two memories on the same subject that \
each stand on their own are RECALL_WITH, not DEPENDS_ON — "answer briefly by \
default" and "the user reads on a phone" are useful together and neither is a \
prerequisite. Reserve DEPENDS_ON for the case where acting on NEW without \
EXISTING would be a mistake.

DEPENDS_ON and RECALL_BEFORE are the relations worth finding. They are the only \
ones that change what the agent does: they pull the existing memory into context \
alongside the new one, so a rule never arrives without the fact it rests on. \
CAUSED_BY is only provenance and is not surfaced during work. So do not retreat \
to CAUSED_BY when the honest relation is a dependency — missing a real \
prerequisite leaves a memory that cannot be acted on, which is as much a failure \
as asserting a relation that does not hold.

At most one relation per existing memory: the strongest single claim.

Return ONLY a JSON object:
{"edges": [{"to": "<existing memory id>", "relation": "<exact name above>", \
"label": "<your own wording for this relationship, lowercase, 1-3 words>", \
"strength": <0.0-1.0 confidence>}]}

Return {"edges": []} when nothing genuinely relates. That is the common and \
correct answer — most memories stand alone, and a wrong edge injects an \
irrelevant memory every single time its partner fires. Only assert a relation \
you could defend. Never relate a memory to itself."""


async def classify_edges(
    memory_id: str,
    content: str,
    *,
    memory_type: str,
    neighbours: list[dict],
) -> list[dict]:
    """One model call deciding how a new memory relates to its shortlist.

    Returns validated edges, or [] on any failure — a memory with no edges is
    the normal resting state, so failing to find them costs nothing that a
    later consolidation pass cannot recover.
    """
    if not memory_id or not (content or "").strip() or not neighbours:
        return []
    try:
        from . import model_registry

        provider = model_registry.get_provider("memory_edges")
        model = model_registry.model_for("memory_edges")
    except Exception as e:
        # The branch that fails for every memory at once, so it is the one most
        # worth recording — a silent [] here reads as "nothing to link".
        log.warning("Edge classifier provider unavailable: %s", e)
        await _record_classify_failure(memory_id, "?", f"provider_unavailable: {e}")
        return []

    listing = "\n".join(
        f'- id={n.get("memory_id")} [{n.get("type")}] {" ".join((n.get("content") or "").split())[:300]}'
        for n in neighbours if n.get("memory_id")
    )
    prompt = (
        f"NEW memory (type={memory_type}):\n{content.strip()[:2000]}\n\n"
        f"EXISTING memories:\n{listing}"
    )
    try:
        response = await provider.create_message(
            model=model,
            max_tokens=1500,
            system=_CLASSIFIER_SYSTEM % {"catalog": relation_catalog()},
            messages=[{"role": "user", "content": prompt}],
            thinking=False,
        )
    except Exception as e:
        # An empty edge list is the expected answer for most memories, so a
        # failed call looks exactly like "nothing to link" once it is stamped
        # on the candidate. Record the failure so a backfill can tell the two
        # apart instead of trusting a zero.
        log.warning("Edge classification failed: %s", e)
        await _record_classify_failure(memory_id, model, str(e))
        return []

    text = " ".join(
        getattr(block, "text", "") or ""
        for block in (getattr(response, "content", None) or [])
        if getattr(block, "type", None) == "text" or getattr(block, "text", None)
    )
    from .recall_cues import _parse_json_object

    payload = _parse_json_object(text)
    if not payload:
        log.warning("Edge classification returned unparseable output for %s", memory_id)
        await _record_classify_failure(memory_id, model, "unparseable_output")
        return []
    known = {n.get("memory_id") for n in neighbours}
    edges = clean_edges(payload.get("edges"), from_id=memory_id)
    # A model that invents an id would otherwise create an edge to nothing,
    # which traversal cannot detect and nobody would ever notice.
    return [edge for edge in edges if edge["to"] in known]


async def _record_classify_failure(memory_id: str, model: str, reason: str) -> None:
    from . import consolidation

    await consolidation.record_maintenance("classify_failure", {
        "memory_id": memory_id, "model": model, "reason": reason[:300],
    })


# ─── Supersession ──────────────────────────────────────────────────────────


async def replacements(ids: list[str]) -> dict[str, str]:
    """{replaced memory: what replaced it} for these ids.

    A reader must never present a retired rule as current, so anything that
    materialises a memory checks here first. {} when nothing is replaced, which
    is the overwhelmingly common case and costs one indexed query.
    """
    coll = await _collection()
    ids = [mid for mid in (ids or []) if mid]
    if coll is None or not ids:
        return {}
    try:
        cursor = coll.find(
            {"to": {"$in": ids}, "relation": "SUPERSEDES"},
            {"_id": 0, "from": 1, "to": 1},
        ).sort("strength", -1)
        out: dict[str, str] = {}
        async for doc in cursor:
            if doc.get("to") and doc.get("from"):
                out.setdefault(doc["to"], doc["from"])
        return out
    except Exception as e:
        log.warning(f"Supersession lookup failed: {e}")
        return {}


# Relations a reader follows. SUPERSEDES resolves rather than links, and
# CAUSED_BY is provenance — neither is navigation.
_FOLLOWED = POLICY_PREREQUISITE | POLICY_COMPANION | POLICY_CONFLICT


# ─── Hygiene ───────────────────────────────────────────────────────────────

# A wrong DEPENDS_ON is the expensive failure mode: it injects an irrelevant
# memory every single time its partner fires, forever, and nothing in the write
# path can catch it because the classifier was confident. The containment is
# after the fact — an edge whose target keeps arriving and keeps going unused is
# an edge the classifier got wrong, and expansion telemetry says so.
#
# The thresholds are deliberately slow. Weakening needs several graded arrivals
# before it starts, and weakens rather than deletes, because "not used" is also
# what an ungraded episode looks like — decay must never be able to quietly
# remove structure for lack of evidence.
_DECAY_MIN_RETRIEVALS = 5
_DECAY_USE_FLOOR = 0.15
_DECAY_STEP = 0.2
_DECAY_PRUNE_BELOW = 0.15


async def decay_unhelpful_edges() -> str:
    """Weaken, then drop, edges whose target never proves useful when injected.

    Deterministic: reads the counters the harness already stamps for every
    surfaced memory (`retrieval_events` -> `memory_stats`, logged for graph
    expansions under `memory:<id>`) and needs no model judgement. Runs in the
    periodic pass, never on a turn.
    """
    from .consolidation import RETRIEVAL_EVENTS_COLLECTION
    from .consolidation import _collection as _events_collection

    edges = await _collection()
    events = await _events_collection(RETRIEVAL_EVENTS_COLLECTION)
    if edges is None or events is None:
        return ""
    # Counted off graph-expansion events specifically, not off the memory's
    # aggregate stats. A memory also arrives when the agent opens it directly,
    # and an edge must only be judged by the arrivals an edge actually caused —
    # otherwise opening a foundational memory and not using it that turn would
    # weaken the edges of everything that legitimately depends on it.
    # `graded: True` is the whole safety property. An ungraded event carries no
    # `used` field at all, so counting it would score "nobody has judged this
    # yet" identically to "arrived and was ignored" — and ungraded arrivals are
    # the norm, since only episodes that reach a task-end boundary get graded at
    # all. Without this filter a correct edge is pruned for being unmeasured.
    # `decay_counted` is the watermark. Reflection runs several times a workday
    # and this query has no time bound, so without it the same five graded
    # arrivals would be re-punished on every pass — an edge would lose strength
    # four times a day off one afternoon's evidence and be pruned within a day.
    # Consumed once, each arrival votes once.
    query = {
        "memory_kind": "graph_expansion", "graded": True,
        "decay_counted": {"$ne": True},
    }
    try:
        cursor = events.find(
            query, {"_id": 0, "retrieval_id": 1, "memory_key": 1, "used": 1},
        )
        arrivals: dict[str, list[int]] = {}
        counted: list[str] = []
        async for doc in cursor:
            key = str(doc.get("memory_key") or "")
            if not key.startswith("memory:"):
                continue
            tally = arrivals.setdefault(key, [0, 0])
            tally[0] += 1
            tally[1] += 1 if doc.get("used") else 0
            if doc.get("retrieval_id"):
                counted.append(doc["retrieval_id"])
    except Exception as e:
        log.warning(f"Edge decay telemetry query failed: {e}")
        return ""

    unhelpful = []
    for key, (arrived, used) in arrivals.items():
        if arrived < _DECAY_MIN_RETRIEVALS:
            continue
        if (used / arrived) < _DECAY_USE_FLOOR:
            unhelpful.append(key.split("memory:", 1)[-1])
    if not unhelpful:
        return ""

    # Scoped twice over. Only edges pointing at an unhelpful target, and only
    # the relations expansion actually injects: SUPERSEDES and CONTRADICTS are
    # resolution, never injected, so telemetry says nothing about them — and
    # decaying a SUPERSEDES edge would silently restore a retired rule as
    # current. The prune is scoped to the same set for the same reason: an
    # unscoped delete would take out a freshly written low-confidence edge that
    # has no telemetry at all, and then report it as decayed.
    scope = {"to": {"$in": unhelpful}, "relation": {"$in": sorted(_FOLLOWED)}}
    try:
        weakened = await edges.update_many(scope, {"$inc": {"strength": -_DECAY_STEP}})
        # Read the doomed edges before deleting them. A prune is the only
        # destructive act in the learning system, and a count alone cannot tell
        # a healthy trim from a mistuned threshold quietly stripping the graph
        # — so the rows themselves go into the maintenance ledger, complete
        # enough to put back.
        doomed = [
            {k: d.get(k) for k in ("from", "to", "relation", "label", "strength", "source")}
            async for d in edges.find({**scope, "strength": {"$lt": _DECAY_PRUNE_BELOW}})
        ]
        pruned = await edges.delete_many(
            {**scope, "strength": {"$lt": _DECAY_PRUNE_BELOW}},
        )
    except Exception as e:
        log.warning(f"Edge decay write failed: {e}")
        return ""
    await _mark_counted(events, counted)
    from . import consolidation

    await consolidation.record_maintenance("decay", {
        "unhelpful_targets": unhelpful,
        "arrivals_counted": len(counted),
        "edges_weakened": getattr(weakened, "modified_count", 0),
        "edges_pruned": pruned.deleted_count,
        "pruned_edges": doomed,
    })
    return (
        f"Edge decay: weakened edges into {len(unhelpful)} unhelpful memory/memories, "
        f"pruned {pruned.deleted_count}."
    )


async def _mark_counted(events, retrieval_ids: list[str]) -> None:
    """Spend the evidence, so the next pass needs new arrivals to act again."""
    if not retrieval_ids:
        return
    try:
        await events.update_many(
            {"retrieval_id": {"$in": retrieval_ids}},
            {"$set": {"decay_counted": True}},
        )
    except Exception as e:
        log.warning(f"Could not mark decay evidence as counted: {e}")


async def graph_report() -> str:
    """What the graph is worth, not just how big it is.

    Edge count and coverage were the original gate, and on real data they
    turned out to answer the wrong question. Connecting memories was easy;
    being right about *how* they connect was not, and a graph can be dense with
    edges that no traversal follows (CAUSED_BY is provenance) or that are
    followed and wrong. So the numbers here separate what exists from what
    operates from what has actually helped.

    There is no precision figure, deliberately. Precision needs labelled truth
    about which relations really hold, and nothing in the system has that — an
    invented percentage would be worse than none. The honest proxies are the
    guard counts and the graded-usefulness line, plus reading a sample.
    """
    coll = await _collection()
    if coll is None:
        return "[memory graph] no database configured."
    try:
        total = await coll.count_documents({})
        if not total:
            return (
                "[memory graph] 0 edges. Nothing to traverse — either nothing "
                "has been classified yet, or the classifier found nothing that "
                "genuinely relates, which is a legitimate answer."
            )
        by_relation = {
            relation: await coll.count_documents({"relation": relation})
            for relation in RELATIONS
        }
        followed = sum(by_relation[r] for r in _FOLLOWED if r in by_relation)
        sources = len(await coll.distinct("from"))
        traversable_sources = len(
            await coll.distinct("from", {"relation": {"$in": sorted(_FOLLOWED)}}),
        )
        collapsed = await coll.count_documents({"label": "opposed directions"})
        by_writer = {}
        for writer in await coll.distinct("source"):
            by_writer[writer or "unknown"] = await coll.count_documents({"source": writer})
        labels = [lab for lab in await coll.distinct("label") if lab]
    except Exception as e:
        return f"[memory graph] report failed: {type(e).__name__}: {e}"

    lines = [
        f"**Memory graph** — {total} edge(s) from {sources} memory/memories.",
        "",
        f"- Traversable: {followed}/{total} edge(s) are followed during a fire "
        f"({_pct(followed, total)}). The rest are stored context "
        "(CAUSED_BY provenance, SUPERSEDES resolution) and change nothing at "
        "injection time.",
    ]
    lines.append(await _coverage_line(sources, traversable_sources))
    lines.append(
        f"- Direction conflicts collapsed: {collapsed}. Each one is a pair the "
        "classifier claimed in both directions, kept as co-relevant rather than "
        "ordered on a guess."
    )
    if by_writer:
        lines.append(
            "- Written by: "
            + ", ".join(f"{name} {count}" for name, count in sorted(by_writer.items()))
        )
    lines.append(await _usefulness_line())
    lines.append("")
    for relation, count in sorted(by_relation.items(), key=lambda kv: -kv[1]):
        if count:
            lines.append(f"- `{relation}`: {count}")
    unused = [r for r, c in by_relation.items() if not c]
    if unused:
        lines.append(f"- unused relations: {', '.join(sorted(unused))}")
    if labels:
        lines.append("")
        lines.append(
            f"Free-text labels in use ({len(labels)}): "
            + ", ".join(sorted(labels)[:20])
        )
    lines.append("")
    lines.append(
        "No precision figure: nothing labels which relations are actually "
        "true. Read a sample of DEPENDS_ON edges before trusting the shape."
    )
    return "\n".join(lines)


def _pct(part: int, whole: int) -> str:
    return f"{100.0 * part / whole:.0f}%" if whole else "n/a"


async def _coverage_line(sources: int, traversable_sources: int) -> str:
    """Coverage, with the distinction that matters: an edge that gets followed.

    Kept as a description rather than a threshold. The gate this replaced said
    "below ~10% expansion is dead weight", which was a guess made before any
    real corpus existed — and the corpus that arrived made the opposite point,
    that a well-connected graph can still be wrong.
    """
    from .consolidation import CANDIDATES_COLLECTION, RECALL_ELIGIBLE_TYPES
    from .consolidation import _collection as _candidates_collection

    candidates = await _candidates_collection(CANDIDATES_COLLECTION)
    if candidates is None:
        return "- Coverage: unavailable (candidate store unreachable)."
    try:
        eligible = await candidates.count_documents(
            {"status": "committed", "type": {"$in": sorted(RECALL_ELIGIBLE_TYPES)}},
        )
    except Exception as e:
        return f"- Coverage: query failed ({type(e).__name__}: {e})."
    if not eligible:
        return "- Coverage: no committed memories yet — nothing to connect."
    return (
        f"- Coverage: {sources}/{eligible} eligible memories have an outgoing "
        f"edge ({_pct(sources, eligible)}); {traversable_sources} have one that "
        f"traversal follows ({_pct(traversable_sources, eligible)})."
    )


async def _usefulness_line() -> str:
    """Did expansion earn its prompt budget? The only outcome measure there is.

    Reads the same telemetry every surfaced memory produces. `used` and the
    outcome come from the consolidator's judgement of the episode, not from
    anything the harness could infer on its own.
    """
    from .consolidation import RETRIEVAL_EVENTS_COLLECTION
    from .consolidation import _collection as _events_collection

    events = await _events_collection(RETRIEVAL_EVENTS_COLLECTION)
    if events is None:
        return "- Expansions fired: telemetry unavailable."
    try:
        fired = await events.count_documents({"memory_kind": "graph_expansion"})
        graded = await events.count_documents(
            {"memory_kind": "graph_expansion", "graded": True},
        )
        used = await events.count_documents(
            {"memory_kind": "graph_expansion", "used": True},
        )
        helpful = await events.count_documents(
            {"memory_kind": "graph_expansion", "outcome": "helpful"},
        )
    except Exception as e:
        return f"- Expansions fired: query failed ({type(e).__name__}: {e})."
    if not fired:
        return (
            "- Expansions fired: 0. No expanded memory has reached a live turn "
            "yet, so nothing here is evidence about usefulness."
        )
    return (
        f"- Expansions fired: {fired}, graded {graded}, used {used}, helpful "
        f"{helpful}. Ungraded expansions are unmeasured, not unhelpful."
    )

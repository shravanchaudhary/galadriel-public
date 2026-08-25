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
# Measured, not assumed: on the first real corpus the classifier proposed
# SUPERSEDES twice and was wrong twice, once at 0.95 confidence — claiming a
# system replaced the memory describing that same system's defect. Model
# confidence is uncalibrated here, so strength is no defence.
#
# Supersession is left to the paths that can actually establish it: the dedupe
# layer, which already detects when two memories make the same claim, and
# explicit maintenance. Traversal still honours the relation when it is true.
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


async def add_edges(from_id: str, edges: list[dict], *, source: str) -> int:
    """Persist validated edges. Returns how many were written."""
    if not from_id or not edges:
        return 0
    coll = await _collection()
    if coll is None:
        return 0
    written = 0
    for edge in edges:
        if edge["relation"] in ASYMMETRIC:
            # Only one direction of a dependency can hold. When the model
            # asserts both, neither arrival order nor confidence resolves it:
            # memories are classified in creation order, which is unrelated to
            # truth, and confidence is uncalibrated — measured asserting a
            # trading ritual depends on the description of its own defect, at
            # higher confidence than the correct reverse claim.
            #
            # So stop pretending to know the direction. What two opposed claims
            # do reliably establish is that the pair belongs together, which is
            # exactly RECALL_WITH. The pair keeps its co-injection and loses the
            # false ordering. `_find_inverse` matches any relation, so this
            # collapse is stable against a later round proposing the dependency
            # again.
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

Check that order before returning one. Given NEW = "the 09:00 ritual fires the \
screener" and EXISTING = "the screener ranks liquid mid-caps":

  CORRECT:   NEW --DEPENDS_ON--> EXISTING   (the ritual needs the screener)
  BACKWARDS: NEW --CAUSED_BY--> EXISTING    (the screener did not cause the ritual)

If a relation only makes sense read the other way round, leave it out — there is \
no reverse edge to return. Labels like "established by" or "context for" \
describe the existing memory acting on the new one, which is backwards.

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
        log.warning("Edge classifier provider unavailable: %s", e)
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
        log.warning("Edge classification failed: %s", e)
        return []

    text = " ".join(
        getattr(block, "text", "") or ""
        for block in (getattr(response, "content", None) or [])
        if getattr(block, "type", None) == "text" or getattr(block, "text", None)
    )
    from .recall_cues import _parse_json_object

    payload = _parse_json_object(text)
    if not payload:
        return []
    known = {n.get("memory_id") for n in neighbours}
    edges = clean_edges(payload.get("edges"), from_id=memory_id)
    # A model that invents an id would otherwise create an edge to nothing,
    # which traversal cannot detect and nobody would ever notice.
    return [edge for edge in edges if edge["to"] in known]


# ─── Expansion ─────────────────────────────────────────────────────────────
# Runs when a recall fires. The recall answered "this is relevant now"; the
# graph answers "and this has to come with it".

# Two hops is enough for the case this exists for (Z needs Y needs X) and stops
# well short of walking the corpus.
_EXPANSION_DEPTH = 2
# Total memories injected per fire, across all seeds. Independent of the
# Stage-2 candidate cap (`_STAGE2_MAX_CANDIDATES_DEFAULT`), which governs how
# many recalls may fire, not how much context each one drags in. Small on
# purpose: every expanded memory spends prompt budget on something the user did
# not ask for.
_EXPANSION_BUDGET = 3
_EXPANSION_CHARS = 320

_FOLLOWED = POLICY_PREREQUISITE | POLICY_COMPANION | POLICY_CONFLICT


async def expand_from_recalls(
    recall_ids: list[str],
    *,
    budget: int = _EXPANSION_BUDGET,
    max_depth: int = _EXPANSION_DEPTH,
) -> list[dict]:
    """Memories reachable from the ones these recalls stand for.

    Returns injection-ordered items: prerequisites first (deepest first, so a
    chain reads X then Y then Z), then companions, then flagged conflicts.
    Empty whenever there is no graph, no seed, or nothing worth adding — which
    is the common case and costs one indexed query.
    """
    if not recall_ids:
        return []
    from . import consolidation

    try:
        seeds = await consolidation.memory_ids_for_recalls(recall_ids)
    except Exception as e:
        log.warning(f"Expansion seed lookup failed: {e}")
        return []
    if not seeds:
        return []

    reached = await _traverse(seeds, budget=budget, max_depth=max_depth)
    if not reached:
        return []
    reached = await _drop_superseded(reached)
    if not reached:
        return []

    try:
        texts = await consolidation.memory_texts([r["memory_id"] for r in reached])
    except Exception as e:
        log.warning(f"Expansion text lookup failed: {e}")
        return []

    out = []
    for item in reached:
        doc = texts.get(item["memory_id"]) or {}
        content = " ".join((doc.get("content") or "").split())
        if not content:
            continue
        out.append({
            **item,
            "content": content[:_EXPANSION_CHARS],
            "type": doc.get("type") or "",
        })
    return _order_bundle(out)


async def _traverse(seeds: list[str], *, budget: int, max_depth: int) -> list[dict]:
    """Breadth-first over followed relations, bounded three ways.

    `visited` seeds with the seed memories themselves, which is both the cycle
    guard and the reason a seed is never injected as its own expansion.
    """
    visited: set[str] = set(seeds)
    frontier = [(mid, 0) for mid in seeds]
    reached: list[dict] = []

    while frontier and len(reached) < budget:
        memory_id, depth = frontier.pop(0)
        if depth >= max_depth:
            continue
        for edge in await edges_from(memory_id, _FOLLOWED):
            target = edge.get("to")
            if not target or target in visited:
                continue
            visited.add(target)
            relation = edge.get("relation")
            reached.append({
                "memory_id": target,
                "relation": relation,
                "label": edge.get("label", ""),
                "depth": depth + 1,
                "via": memory_id,
            })
            # Companions are leaves. Following them would let one dense
            # RECALL_WITH cluster consume the whole budget with material that
            # is merely adjacent, crowding out real prerequisites.
            if relation in POLICY_PREREQUISITE:
                frontier.append((target, depth + 1))
            if len(reached) >= budget:
                break
    return reached


async def _drop_superseded(reached: list[dict]) -> list[dict]:
    """Remove memories that something newer replaces.

    Injecting a rule beside its replacement is worse than injecting neither:
    the model has no basis to prefer one and will pick arbitrarily, so a
    superseded memory must never arrive as live context. The superseded memory
    is kept in the store — deleting it would destroy the record of why the
    system changed its mind — it is simply not injected.
    """
    coll = await _collection()
    if coll is None or not reached:
        return reached
    ids = [item["memory_id"] for item in reached]
    try:
        cursor = coll.find(
            {"to": {"$in": ids}, "relation": "SUPERSEDES"},
            {"_id": 0, "to": 1},
        )
        stale = {doc["to"] async for doc in cursor if doc.get("to")}
    except Exception as e:
        log.warning(f"Supersede resolution failed: {e}")
        return reached
    if stale:
        log.info("[Expansion] dropped %d superseded memory/memories", len(stale))
    return [item for item in reached if item["memory_id"] not in stale]


_KIND_ORDER = {"prerequisite": 0, "companion": 1, "conflict": 2}


def _kind(relation: str) -> str:
    if relation in POLICY_PREREQUISITE:
        return "prerequisite"
    if relation in POLICY_CONFLICT:
        return "conflict"
    return "companion"


def _order_bundle(items: list[dict]) -> list[dict]:
    """Prerequisites first and deepest-first, so a chain reads in dependency order.

    Depth here is distance from the seed, so the deepest prerequisite is the
    most foundational: for Z -> Y -> X, X is at depth 2 and belongs first.
    """
    for item in items:
        item["kind"] = _kind(item.get("relation", ""))
    return sorted(
        items,
        key=lambda i: (
            _KIND_ORDER.get(i["kind"], 1),
            -i["depth"] if i["kind"] == "prerequisite" else i["depth"],
        ),
    )


_KIND_HEADING = {
    "prerequisite": "Needed to make sense of the above",
    "companion": "Related and usually useful here",
    "conflict": "Conflicting memories — reconcile before acting",
}


def format_bundle(items: list[dict]) -> str:
    """Render an expansion for injection under a recall fire. "" when empty."""
    if not items:
        return ""
    lines: list[str] = []
    current = None
    for item in items:
        if item["kind"] != current:
            current = item["kind"]
            lines.append(f"{_KIND_HEADING[current]}:")
        label = item.get("label") or item.get("relation", "").lower()
        lines.append(f"- ({label}) {item['content']}")
    return "\n".join(lines)


# ─── Hygiene ───────────────────────────────────────────────────────────────

# A wrong DEPENDS_ON is the expensive failure mode: it injects an irrelevant
# memory every single time its partner fires, forever, and nothing in the write
# path can catch it because the classifier was confident. The containment is
# after the fact — an edge whose target keeps arriving and keeps going unused is
# an edge the classifier got wrong, and expansion telemetry says so.
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
    from .consolidation import STATS_COLLECTION
    from .consolidation import _collection as _candidates_collection

    edges = await _collection()
    stats = await _candidates_collection(STATS_COLLECTION)
    if edges is None or stats is None:
        return ""
    try:
        cursor = stats.find(
            {"memory_key": {"$regex": "^memory:"}},
            {"_id": 0, "memory_key": 1, "retrieval_count": 1, "use_count": 1},
        )
        rows = [doc async for doc in cursor]
    except Exception as e:
        log.warning(f"Edge decay stats query failed: {e}")
        return ""

    unhelpful = []
    for row in rows:
        retrieval = int(row.get("retrieval_count", 0) or 0)
        use = int(row.get("use_count", 0) or 0)
        if retrieval < _DECAY_MIN_RETRIEVALS:
            continue
        if (use / retrieval) < _DECAY_USE_FLOOR:
            unhelpful.append(str(row["memory_key"]).split("memory:", 1)[-1])
    if not unhelpful:
        return ""

    try:
        await edges.update_many(
            {"to": {"$in": unhelpful}},
            {"$inc": {"strength": -_DECAY_STEP}},
        )
        pruned = await edges.delete_many({"strength": {"$lt": _DECAY_PRUNE_BELOW}})
    except Exception as e:
        log.warning(f"Edge decay write failed: {e}")
        return ""
    return (
        f"Edge decay: weakened edges into {len(unhelpful)} unhelpful memory/memories, "
        f"pruned {pruned.deleted_count}."
    )


async def density_report() -> str:
    """How much graph actually exists — the go/no-go for building on it.

    A traversal over an empty graph buys nothing, and the likeliest failure of
    this whole design is that the classifier finds almost nothing to connect.
    Measuring that before building expansion is cheaper than discovering it
    afterwards.
    """
    coll = await _collection()
    if coll is None:
        return "[memory graph] no database configured."
    try:
        total = await coll.count_documents({})
        if not total:
            return (
                "[memory graph] 0 edges. Nothing to traverse — the classifier has "
                "not connected anything yet."
            )
        by_relation: dict[str, int] = {}
        for relation in RELATIONS:
            by_relation[relation] = await coll.count_documents({"relation": relation})
        sources = len(await coll.distinct("from"))
        labels = [lab for lab in await coll.distinct("label") if lab]
    except Exception as e:
        return f"[memory graph] report failed: {type(e).__name__}: {e}"

    lines = [
        f"**Memory graph** — {total} edge(s) from {sources} memory/memories.",
        "",
    ]
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
    return "\n".join(lines)

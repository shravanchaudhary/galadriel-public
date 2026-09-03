"""Shared memory-candidate pipeline and utility telemetry.

One write path replaces the ~7 independent "where does this go" writers the
harness used to have. Every memory candidate — whether from the runtime
`learn` tool, the task-end consolidator's `propose_memory`, or a periodic
promotion — goes through `commit_candidate`:

  1. validate   — type + payload shape
  2. dedupe     — KG: exact subject+predicate+object match against existing
                  facts. Prose (drawer/procedural/preference): embedding
                  cosine similarity against recently committed candidates of
                  the same type (not the whole historical palace corpus —
                  see `_is_prose_duplicate`).
  3. commit     — write to the destination store (KG / palace drawer /
                  knowledge/** file / daily log).
  4. provenance — record the candidate (Mongo `memory_candidates`) for
                  dedupe, audit, and the task-end consolidator's
                  "recent-consolidation digest".

This module also owns the utility-telemetry counters (`retrieval_events`,
`memory_stats`) that back retrieval grading (phase 4) and the periodic
consolidator's evidence report (phase 5). All of it is harness-computed —
the model only ever expresses judgment through structured tool calls
(`propose_memory`, `grade_retrieval`, `flag_memory`); every counter, ratio,
and timestamp here is deterministic code, never an LLM guess.

Mongo is optional throughout: every DB touch degrades to "skip
telemetry/dedupe, still commit the write" when MONGO_URI/MONGO_DB is unset,
matching the rest of the harness (see e.g. harness/recall.py, conversation_run_store.py).
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("galadriel.consolidation")

CANDIDATES_COLLECTION = "memory_candidates"
RETRIEVAL_EVENTS_COLLECTION = "retrieval_events"
STATS_COLLECTION = "memory_stats"
# Third ledger, alongside candidates (what was written) and retrieval_events
# (what surfaced): what the harness did to the store on its own initiative.
# Those acts are the ones with no other durable trace — a pruned edge and an
# evicted preference leave nothing behind to count, and a log line ages out of
# retention long before a week of behaviour can be judged from it.
MAINTENANCE_COLLECTION = "memory_maintenance"

MEMORY_TYPES = ("semantic", "procedural", "preference", "episodic")

# Ambient episode id for commits. The tool layer (learn / propose_memory) has
# no agent reference, so the agent sets this at turn entry and every commit in
# that turn's await tree inherits it — which is what lets preference
# confirmations be counted per DISTINCT session instead of per restatement
# (three restatements inside one chat's to-and-fro are one confirmation).
# The task-end consolidator's side channel is seeded with the EPISODE's
# session, so its proposals attribute to the episode they came from.
_SESSION_CONTEXT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "memory_commit_session", default=None,
)


def set_session_context(session_id: str | None) -> None:
    """Record the episode a subsequent commit should be attributed to."""
    _SESSION_CONTEXT.set(session_id)

# Prose dedupe scope: candidates committed through this pipeline recently.
_DEDUPE_POOL_SIZE = 200
_DEDUPE_WINDOW_DAYS = 30
# Staleness floor for the periodic-consolidator utility report.
_STALE_DAYS = 90
# Graded surfacings a memory needs before the utility report will judge its
# trigger. Grades arrive only when an episode ends, so a low bar on raw
# retrievals indicted memories in long-running chats that were never measured.
_MIN_GRADED_FOR_BIN = 5

# Memory types that earn a retrieval trigger on commit. A stored memory with
# no trigger is inert — reachable only when the agent happens to search for it
# — so every durable type gets one. Kept as an explicit set rather than
# "everything", because episodic content also lands in the palace and must
# never spawn triggers: this gate covers BOTH post-commit passes (trigger
# generation and edge classification), so `type=episodic` memories get
# neither — they stay reachable as edge *targets* via the shortlist, which
# spans every type.
RECALL_ELIGIBLE_TYPES = frozenset({"semantic", "procedural", "preference"})


def _as_aware(value):
    """A stored datetime, made comparable with `_now()`.

    Everything here writes `datetime.now(timezone.utc)`, but the Mongo client is
    built without `tz_aware`, so it hands the same value back naive — and
    subtracting the two raises. The rest of the pipeline dodges this by
    comparing `created_at_ts` floats; the one place that compares datetimes
    directly is the stale bin, which crashed the whole utility report the first
    time any retrieval was graded used.
    """
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _slugify(text: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:60] or fallback


# One commit writes each triplet with its own dedupe read plus a palace write,
# so the cost is linear and a cap only exists to bound a runaway generation —
# there is no cliff here. The original cap was a bare `[:20]` with no name and
# no rationale, and it silently discarded the overflow while reporting success:
# the first real profile written through it submitted 21 facts and lost one
# (`Metaforms | funding | $9M raised`) without a word. Anything dropped is now
# reported to the caller, which is what makes a higher bound safe to allow.
MAX_TRIPLETS_PER_COMMIT = 50


def clean_triplets(raw) -> tuple[list[tuple[str, str, str]], str | None, str]:
    """Normalize/validate raw [subject, predicate, object] triplets.

    Returns `(triplets, error, note)`. `error` is set when the argument cannot
    be read as triplets at all — the caller must surface it rather than treat
    the field as absent. `note` reports what was accepted but not kept, so a
    partial write never looks like a whole one.
    """
    from .tool_args import as_list

    items, error = as_list(
        raw, "kg_triplets",
        multi_hint=(
            "One learn/propose_memory call stores ONE topic: pass a single flat "
            "array of [subject, predicate, object] triplets and issue a "
            "separate call per topic."
        ),
    )
    if error:
        return [], error, ""

    out: list[tuple[str, str, str]] = []
    malformed = 0
    for item in items:
        if isinstance(item, dict):
            item = [item.get("subject"), item.get("predicate"), item.get("object")]
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            malformed += 1
            continue
        s, p, o = (str(x).strip() for x in item)
        if s and p and o:
            out.append((s, p, o))
        else:
            malformed += 1

    if items and not out:
        return [], (
            f"kg_triplets had {len(items)} item(s) but none were usable — each "
            f"must be [subject, predicate, object] with all three non-empty."
        ), ""

    notes = []
    if malformed:
        notes.append(f"{malformed} malformed item(s) skipped")
    dropped = out[MAX_TRIPLETS_PER_COMMIT:]
    if dropped:
        shown = ", ".join(f"{s}|{p}|{o}" for s, p, o in dropped[:3])
        notes.append(
            f"kept {MAX_TRIPLETS_PER_COMMIT} of {len(out)}; dropped {len(dropped)} "
            f"over the cap: {shown}"
            + (f" and {len(dropped) - 3} more" if len(dropped) > 3 else "")
            + " — re-send these in another call"
        )
    return out[:MAX_TRIPLETS_PER_COMMIT], None, "; ".join(notes)


async def _collection(name: str):
    try:
        from .db_ops import get_db
        db = get_db()
        if db is None:
            return None
        return db[name]
    except Exception as e:
        log.warning(f"{name} collection unavailable: {e}")
        return None


async def ensure_indexes() -> None:
    """Indexes for the queries the learning pipeline runs on every turn.

    Idempotent and best-effort, called at startup (`Scheduler.start`) rather
    than from the periodic pass: a process that boots and immediately serves a
    turn would otherwise resolve recall seeds and stamp retrieval telemetry
    with collection scans until the first reflection happened to run.
    """
    candidates = await _collection(CANDIDATES_COLLECTION)
    if candidates is not None:
        try:
            # Provenance/edge lookups by id, and the update after every commit.
            await candidates.create_index([("memory_id", 1)])
            # Recall fire -> seed memory, on the turn path.
            await candidates.create_index([("trigger.recall_id", 1)])
            # The edge classifier's candidate pool.
            await candidates.create_index([("status", 1), ("created_at_ts", -1)])
        except Exception as e:
            log.warning(f"Candidate index creation failed: {e}")

    events = await _collection(RETRIEVAL_EVENTS_COLLECTION)
    if events is not None:
        try:
            await events.create_index([("retrieval_id", 1)])
            await events.create_index([("session_id", 1), ("ts", 1)])
            # Graded-event rollups (the utility backfill, and any per-memory
            # audit) would otherwise scan the whole collection.
            await events.create_index([("memory_key", 1), ("graded", 1)])
            # Edge decay selects graded rows of one memory_kind; without this
            # it scans the collection on every pass.
            await events.create_index([("memory_kind", 1), ("graded", 1), ("ts", 1)])
            # The unopened sweep selects one memory_kind's un-watermarked rows
            # oldest-first, which is a different prefix from the line above.
            await events.create_index(
                [("memory_kind", 1), ("unopened_checked", 1), ("ts", 1)]
            )
        except Exception as e:
            log.warning(f"Retrieval event index creation failed: {e}")

    stats = await _collection(STATS_COLLECTION)
    if stats is not None:
        try:
            # Upserted once per surfaced memory, so this is the hottest write.
            await stats.create_index([("memory_key", 1)])
        except Exception as e:
            log.warning(f"Memory stats index creation failed: {e}")

    maintenance = await _collection(MAINTENANCE_COLLECTION)
    if maintenance is not None:
        try:
            # Read as "what happened to the store lately", by kind or in order.
            await maintenance.create_index([("kind", 1), ("ts", -1)])
        except Exception as e:
            log.warning(f"Maintenance index creation failed: {e}")


# ─── Candidate pipeline ────────────────────────────────────────────────


async def commit_candidate(
    *,
    type: str,
    content: str = "",
    kg_triplets: list | None = None,
    kg_invalidate: list | None = None,
    topic: str | None = None,
    valid_from: str | None = None,
    ended: str | None = None,
    evidence: list[str] | None = None,
    confidence: float | None = None,
    source: str = "runtime",
    note: str = "",
    supersedes_memory_id: str | None = None,
    session_id: str | None = None,
) -> dict:
    """Validate -> dedupe -> write -> record provenance.

    `source` identifies the caller for provenance/digest purposes:
    "runtime" (the `learn` tool), "task_consolidator", "periodic_consolidator",
    or "tower". Returns {"status": "committed"|"duplicate"|"error",
    "memory_id": str, "detail": str}.

    `kg_invalidate` retires KG facts (list of [subject, predicate, object])
    through the same commit so a fact change is one call: invalidate the old
    triple(s), add the new ones via `kg_triplets`. Valid with type=semantic
    only; invalidations alone (no adds, no content) are a legitimate commit —
    retiring a fact is a change to the store worth a provenance record.
    `ended` backdates the retirement's valid_to (the mirror of `valid_from`)
    for facts that stopped being true before today.

    `supersedes_memory_id` states that this memory replaces an existing one as
    the active rule. It is the only path that writes a SUPERSEDES edge, and it
    is deliberately an assertion rather than an inference: near-identical
    content means the same claim was made twice, which is reinforcement, and
    retiring a rule for being restated would be exactly backwards. Offered on
    the consolidation tool surface only.
    """
    type_ = (type or "").strip().lower()
    if type_ not in MEMORY_TYPES:
        return {
            "status": "error", "memory_id": None,
            "detail": f"unknown type {type!r} — must be one of {MEMORY_TYPES}.",
        }
    content = (content or "").strip()
    # Distinguish absent from unreadable. Folding a malformed argument into
    # "not provided" is what told a model it had omitted a field it had just
    # sent 3,900 characters of — see harness/tool_args.py.
    triplets: list[tuple[str, str, str]] = []
    triplet_note = ""
    # Empty means absent, as it always did — `kg_triplets=""` alongside real
    # content used to be ignored, and turning that into a hard error would
    # break callers this change was supposed to help. Only a NON-empty value
    # is worth diagnosing.
    if kg_triplets:
        triplets, triplet_error, triplet_note = clean_triplets(kg_triplets)
        if triplet_error:
            return {"status": "error", "memory_id": None, "detail": triplet_error}
    invalidations: list[tuple[str, str, str]] = []
    invalidate_note = ""
    if kg_invalidate:
        invalidations, invalidate_error, invalidate_note = clean_triplets(kg_invalidate)
        if invalidate_error:
            return {"status": "error", "memory_id": None, "detail": invalidate_error}
    if not content and not triplets and not invalidations:
        return {
            "status": "error", "memory_id": None,
            "detail": (
                "nothing to store: pass content (prose), kg_triplets (an "
                "array of [subject, predicate, object]), or kg_invalidate "
                "(triples to retire). Any one alone is valid."
            ),
        }
    if (triplets or invalidations) and type_ != "semantic":
        return {"status": "error", "memory_id": None, "detail": "kg_triplets / kg_invalidate are only valid for type=semantic."}
    valid_from, date_warning = _clean_valid_from(valid_from)
    ended, ended_warning = _clean_valid_from(ended, field="ended")
    if triplets and not content:
        # Stored on the candidate so a KG-only memory is findable by meaning:
        # `memory(query=…)` and the edge shortlist both search candidate
        # content, and an empty string made a third of the corpus invisible.
        content = _render_triplets(triplets)

    memory_id = uuid.uuid4().hex
    duplicate_of: str | None = None
    try:
        if triplets or invalidations:
            status, destination, detail = await _commit_kg(
                triplets, valid_from, invalidations=invalidations, ended=ended,
            )
        else:
            duplicate_of = await _is_prose_duplicate(type_, content)
            if duplicate_of:
                status, destination, detail = (
                    "duplicate", {},
                    f"duplicate of existing memory {duplicate_of} — not re-written",
                )
            elif type_ == "semantic":
                status, destination, detail = await _commit_drawer(
                    content, topic, room="knowledge", memory_id=memory_id,
                )
            elif type_ == "procedural":
                status, destination, detail = await _commit_procedural(
                    content, topic, memory_id=memory_id,
                )
            elif type_ == "episodic":
                status, destination, detail = await _commit_drawer(
                    content, topic, room="episodes", memory_id=memory_id,
                )
            else:
                status, destination, detail = await _commit_preference(
                    content, topic, memory_id=memory_id,
                )
    except Exception as e:
        status, destination, detail = "error", {}, f"{type(e).__name__}: {e}"
    if date_warning:
        detail = f"{detail} ({date_warning})"
    if ended_warning:
        detail = f"{detail} ({ended_warning})"
    if triplet_note:
        detail = f"{detail} [{triplet_note}]"
    if invalidate_note:
        detail = f"{detail} [kg_invalidate: {invalidate_note}]"

    await _save_candidate({
        "memory_id": memory_id,
        "type": type_,
        "content": content,
        "kg_triplets": [list(t) for t in triplets],
        "kg_invalidated": [list(t) for t in invalidations],
        "topic": topic,
        "valid_from": valid_from,
        "evidence": evidence or [],
        "confidence": confidence,
        "source": source,
        "note": note,
        "status": status,
        "destination": destination,
        "duplicate_of": duplicate_of,
        "session_id": session_id or _SESSION_CONTEXT.get(),
        "created_at": _now(),
        "created_at_ts": _now().timestamp(),
    })
    # One line per outcome, so a tail of the log shows whether the agent is
    # learning at all. The durable answers live in `memory_candidates`; this is
    # the liveness signal for watching a deploy, not the record.
    log.info(
        "[Commit] status=%s memory=%s type=%s source=%s topic=%s",
        status, memory_id, type_, source, topic or "-",
    )
    if status == "committed":
        if supersedes_memory_id:
            detail = f"{detail} ({await _record_supersession(memory_id, supersedes_memory_id)})"
        _schedule_post_commit(memory_id, type_, content, triplets, topic)
    elif supersedes_memory_id:
        detail = (
            f"{detail} (no replacement recorded: nothing was committed to "
            "supersede with)"
        )
    return {"status": status, "memory_id": memory_id, "detail": detail}


async def _record_supersession(new_id: str, raw_old_id: str) -> str:
    """Write `new SUPERSEDES old`, or say why it could not be written.

    The old memory is left exactly where it is. Supersession changes what
    counts as current, not what happened: deleting the replaced memory would
    destroy the record of why the rule changed. What changes is how it is
    presented — every reader path resolves the edge through
    `memory_graph.replacements()`, so a retired rule is still findable and
    openable but never reads as the one in force (`memory_access.find`,
    `open_memory`, and the recall fire's pointer).

    Authoritative, so it overrides whatever the classifier may have guessed
    about this pair — a stated replacement is better evidence than an inferred
    dependency.
    """
    from . import memory_graph

    old_id = str(raw_old_id or "").strip()
    if old_id.startswith("memory:"):
        old_id = old_id.split("memory:", 1)[-1].strip()
    if not old_id or old_id == new_id:
        return "supersedes_memory_id ignored: not a different memory"
    coll = await _collection(CANDIDATES_COLLECTION)
    if coll is None:
        return "supersession not recorded: no database"
    try:
        target = await coll.find_one(
            {"memory_id": old_id, "status": "committed"}, {"_id": 0, "memory_id": 1},
        )
    except Exception as e:
        log.warning(f"Supersession target lookup failed for {old_id}: {e}")
        return "supersession not recorded: lookup failed"
    if target is None:
        return f"supersedes_memory_id {old_id} is not a committed memory — no replacement recorded"

    edges = memory_graph.clean_edges(
        [{"to": old_id, "relation": "SUPERSEDES", "label": "replaces", "strength": 1.0}],
        from_id=new_id, allow_forbidden=True,
    )
    written = await memory_graph.add_edges(
        new_id, edges, source="explicit_replacement", authoritative=True,
    )
    if not written:
        return f"replacement of {old_id} could not be recorded"
    log.info("[Supersedes] %s replaces %s", new_id, old_id)
    return f"recorded as replacing memory {old_id}, which stays in the record as history"


def _schedule_post_commit(
    memory_id: str,
    type_: str,
    content: str,
    triplets: list[tuple[str, str, str]],
    topic: str | None,
) -> None:
    """Fire-and-forget the packaging work a freshly committed memory still needs:
    the retrieval trigger that makes it findable, and the typed edges that make
    it reachable through its neighbours.

    Deliberately not awaited. `learn` is called mid-task by the main agent, and
    model round-trips would put seconds of latency into the user's turn to build
    something the user is not waiting for. A failure here leaves the memory
    stored but triggerless or unconnected, which the periodic consolidator can
    see from the candidate record and backfill.

    Both passes share the eligibility gate. Episodic memories are excluded as
    *sources*: they are the most numerous kind and rarely the thing another
    memory hangs off. They remain edge *targets* — the shortlist spans every
    type, so a lesson can still record what episode caused it.
    """
    if type_ not in RECALL_ELIGIBLE_TYPES:
        return
    text = content or _render_triplets(triplets)
    if not text:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _autogen_enabled():
        _spawn(loop, _generate_trigger(memory_id, type_, text, topic))
    if _edges_enabled():
        _spawn(loop, _classify_edges(memory_id, type_, text))


def _spawn(loop, coro) -> None:
    task = loop.create_task(coro)
    _POST_COMMIT_TASKS.add(task)
    task.add_done_callback(_POST_COMMIT_TASKS.discard)


# create_task keeps only a weak reference, so a task nobody holds can be
# garbage-collected mid-flight.
_POST_COMMIT_TASKS: set = set()


def _autogen_enabled() -> bool:
    return (os.environ.get("RECALL_CUE_AUTOGEN") or "1").strip().lower() not in (
        "0", "false", "no",
    )


def _edges_enabled() -> bool:
    return (os.environ.get("MEMORY_EDGE_AUTOGEN") or "1").strip().lower() not in (
        "0", "false", "no",
    )


async def _classify_edges(
    memory_id: str, type_: str, text: str, *, source: str = "task_consolidator",
) -> int:
    """Relate a new memory to its nearest committed neighbours, if it relates.

    Zero edges is the expected outcome for most memories and is not an error,
    which is why the pass stamps `edges.classified_at` on the candidate: without
    it, "no edges" and "never classified" are indistinguishable, and a backfill
    would have to re-run every memory that legitimately has none.
    """
    from . import memory_graph

    try:
        neighbours = await shortlist_neighbours(text, exclude_id=memory_id)
        edges, written, restates = [], 0, None
        if neighbours:
            edges, restates = await memory_graph.classify_edges(
                memory_id, text, memory_type=type_, neighbours=neighbours,
            )
            written = await memory_graph.add_edges(memory_id, edges, source=source)
    except Exception as e:
        log.warning("Edge classification crashed for memory %s: %s", memory_id, e)
        return 0
    if written:
        log.info(
            "[Edges] memory=%s wrote=%d %s",
            memory_id, written,
            ", ".join(f"{e['relation']}->{e['to']}" for e in edges),
        )
    coll = await _collection(CANDIDATES_COLLECTION)
    if coll is not None:
        try:
            update: dict = {"edges": {
                "classified_at": _now(), "written": written, "source": source,
            }}
            # The classifier's restatement verdict lands as `duplicate_of` —
            # the same field commit-time dedupe writes, so the preference
            # confirmation counter (promotable_preferences) sees both paths
            # identically. Nothing is deleted: the restating memory stays
            # committed; only the counting changes. Never overwrite a
            # dedupe-set value — the commit-time verdict saw the nearer pool.
            if restates:
                existing = await coll.find_one(
                    {"memory_id": memory_id}, {"_id": 0, "duplicate_of": 1},
                )
                if existing is not None and not existing.get("duplicate_of"):
                    update["duplicate_of"] = restates
                    log.info(
                        "[Restates] memory=%s confirms %s", memory_id, restates,
                    )
            await coll.update_one({"memory_id": memory_id}, {"$set": update})
        except Exception as e:
            log.warning("Could not record edge outcome for %s: %s", memory_id, e)
    return written


def _render_triplets(triplets: list[tuple[str, str, str]]) -> str:
    return "; ".join(f"{s} — {p} — {o}" for s, p, o in triplets)


async def _generate_trigger(
    memory_id: str, type_: str, text: str, topic: str | None,
) -> None:
    from . import recall_cues

    try:
        result = await recall_cues.create_recall_for_memory(
            text, memory_type=type_, topic=topic,
        )
    except Exception as e:
        log.warning("Trigger generation crashed for memory %s: %s", memory_id, e)
        result = {"status": "error", "detail": f"{type(e).__name__}: {e}"}
    log.info(
        "[Trigger] memory=%s status=%s recall=%s scores=%s",
        memory_id, result.get("status"), result.get("recall_id"), result.get("scores"),
    )
    coll = await _collection(CANDIDATES_COLLECTION)
    if coll is None:
        return
    try:
        await coll.update_one({"memory_id": memory_id}, {"$set": {"trigger": result}})
    except Exception as e:
        log.warning("Could not record trigger outcome for %s: %s", memory_id, e)


async def _save_candidate(record: dict) -> None:
    coll = await _collection(CANDIDATES_COLLECTION)
    if coll is None:
        return
    try:
        await coll.insert_one(record)
    except Exception as e:
        log.warning(f"Memory candidate record failed: {e}")


def _clean_valid_from(raw: str | None, field: str = "valid_from") -> tuple[str | None, str]:
    """(iso_date, warning) — validate an optional ISO date.

    A malformed date is dropped rather than failing the commit: losing the
    memory over a bad optional field is worse than falling back to the write
    path's default of today. The warning rides back in the tool result so the
    caller can pass it correctly next time. Also validates `ended` (the
    retirement mirror of valid_from) via the `field` name.
    """
    text = (raw or "").strip()
    if not text:
        return None, ""
    try:
        return date.fromisoformat(text).isoformat(), ""
    except ValueError:
        return None, f"ignored {field}={text!r} — not an ISO date (YYYY-MM-DD)"


async def _commit_kg(
    triplets: list[tuple[str, str, str]],
    valid_from: str | None = None,
    invalidations: list[tuple[str, str, str]] | None = None,
    ended: str | None = None,
) -> tuple[str, dict, str]:
    from . import palace
    loop = asyncio.get_running_loop()
    # Retire before adding, so a fact change (invalidate old + add new in one
    # call) never has both triples reading as current between the two writes.
    # `ended` backdates valid_to for facts that stopped being true in the past
    # — the retirement mirror of valid_from.
    retired, missed = 0, []
    for s, p, o in invalidations or []:
        result = await loop.run_in_executor(
            None,
            lambda s=s, p=p, o=o: palace.kg_invalidate(
                subject=s, predicate=p, object=o, ended=ended,
            ),
        )
        # kg_invalidate reports "nothing open to invalidate" (or an error)
        # without raising; counting those as retired told the caller a stale
        # fact was gone while it stayed current.
        if isinstance(result, str) and result.startswith("KG: invalidated"):
            retired += 1
        else:
            missed.append(f"{s}|{p}|{o}")
    stored, duplicate = 0, 0
    for s, p, o in triplets:
        if await _is_kg_duplicate(s, p, o):
            duplicate += 1
            continue
        await loop.run_in_executor(
            None,
            lambda s=s, p=p, o=o: palace.kg_add(
                subject=s, predicate=p, object=o, valid_from=valid_from,
            ),
        )
        stored += 1
    destination = {
        "kind": "kg", "stored": stored, "duplicate": duplicate,
        "retired": retired, "not_retired": len(missed),
    }
    parts = []
    if stored:
        parts.append(f"stored {stored} triplet(s)")
    if duplicate:
        parts.append(f"{duplicate} add(s) already known")
    if retired:
        parts.append(f"retired {retired}")
    if missed:
        parts.append(
            f"{len(missed)} not retired — no open fact matched: "
            + ", ".join(missed[:3])
        )
    detail = "kg: " + ", ".join(parts)
    if stored or retired:
        return "committed", destination, detail
    if duplicate:
        return "duplicate", destination, detail + " — no-op"
    return "error", destination, detail


async def _is_kg_duplicate(subject: str, predicate: str, obj: str) -> bool:
    """Exact-match dedupe against CURRENT facts only: same triple with an open
    validity row. A different object is a legitimate new/updated fact, and an
    expired identical triple is legitimately re-addable (facts can revert;
    invalidate-then-re-add restarts validity) — matching history here made the
    documented fact-change flow retire the old fact and then drop its
    replacement as "already known".
    """
    try:
        from . import palace
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: palace.kg_fact_is_current(subject, predicate, obj)
        )
    except Exception as e:
        log.warning(f"KG dedupe check failed ({subject}/{predicate}/{obj}): {e}")
        return False


def _dedupe_key(content: str) -> str:
    """Whitespace- and case-insensitive form of a memory's prose."""
    return " ".join((content or "").split()).casefold()


async def _is_prose_duplicate(type_: str, content: str) -> str | None:
    """Return the memory_id of a committed candidate holding the SAME prose.

    Text equality only, deliberately. This verdict SKIPS the write, so it is
    safe exactly when skipping loses nothing — which is true of a re-proposal
    of the same text and of nothing else. Measured on the live corpus
    (kb/learning-loop-measured.md), every cosine floor low enough to catch a
    paraphrase also drops genuinely distinct documents: at 0.88, three of
    nineteen, including a personal profile against an employer list at 0.899
    (they share only the subject's name); at 0.93, nothing fires at all. There
    is no honest constant in between, so near-duplicates are the edge
    classifier's `restates` verdict instead, which records the restatement
    WITHOUT discarding the new write.

    Scope: candidates that went through THIS pipeline in the last
    _DEDUPE_WINDOW_DAYS — not the full historical palace corpus (palace's own
    search API returns formatted markdown, not per-item vectors, so full
    corpus dedupe isn't reachable without forking palace internals). This
    still catches the failure the pipeline is worried about: the same
    consolidator (or the runtime agent) re-proposing one lesson across ticks.
    Best-effort: any failure means "not a duplicate" — a missed dedupe costs a
    redundant write, never a lost one.
    """
    key = _dedupe_key(content)
    if not key:
        return None
    coll = await _collection(CANDIDATES_COLLECTION)
    if coll is None:
        return None
    try:
        since_ts = _now().timestamp() - _DEDUPE_WINDOW_DAYS * 86400
        cursor = coll.find(
            {"type": type_, "status": "committed", "created_at_ts": {"$gte": since_ts}},
            {"memory_id": 1, "content": 1},
        ).sort("created_at_ts", -1).limit(_DEDUPE_POOL_SIZE)
        async for doc in cursor:
            if _dedupe_key(doc.get("content") or "") == key:
                return doc.get("memory_id")
    except Exception as e:
        log.warning(f"Prose dedupe query failed: {e}")
    return None


# Neighbours offered to the edge classifier. The bound is the whole design:
# unbounded, relating a new memory to the corpus is O(new x all) model
# comparisons. Embedding shortlists first, so exactly one model call sees a
# handful of plausible partners.
_EDGE_SHORTLIST_SIZE = 8
# Safety bound on the candidate pool, not a retention policy — it exists so a
# runaway corpus cannot turn one commit into an unbounded read. It drops the
# oldest, which is the wrong end for this purpose, so hitting it is logged
# rather than absorbed: a silently truncated pool reads as "considered
# everything". The fix when it starts biting is a vector index on the
# collection, not a bigger number here.
_EDGE_POOL_CAP = 5000


async def shortlist_neighbours(
    content: str, *, exclude_id: str | None = None, limit: int = _EDGE_SHORTLIST_SIZE,
) -> list[dict]:
    """The committed memories most similar to `content`, as edge candidates.

    Spans the whole history on purpose. Dedupe looks at a recent window because
    it is asking "did I just write this?"; this asks "what does this rest on?",
    and the answer is routinely a foundational memory from months ago — that
    long reach is the reason the graph exists. Reusing the dedupe window here
    would make the graph unable to connect anything old, at write time, before
    traversal ever gets a say.

    Spans every memory type too. The canonical edge is cross-type — a
    procedural lesson depending on a semantic fact — so filtering by type the
    way `_is_prose_duplicate` does would hide exactly the edges worth having.

    Similarity only selects who gets *considered*; the model decides whether any
    functional relation exists, and "none" is a common, correct answer.
    """
    coll = await _collection(CANDIDATES_COLLECTION)
    if coll is None or not (content or "").strip():
        return []
    try:
        cursor = coll.find(
            {"status": "committed"},
            {"memory_id": 1, "content": 1, "type": 1, "embedding": 1},
        ).sort("created_at_ts", -1).limit(_EDGE_POOL_CAP + 1)
        pool = [doc async for doc in cursor]
    except Exception as e:
        log.warning(f"Edge shortlist query failed: {e}")
        return []

    if len(pool) > _EDGE_POOL_CAP:
        log.warning(
            "Edge shortlist pool hit its %d-memory cap; the oldest %d+ committed "
            "memories were not considered as relation targets.",
            _EDGE_POOL_CAP, len(pool) - _EDGE_POOL_CAP,
        )
        pool = pool[:_EDGE_POOL_CAP]
    pool = [
        doc for doc in pool
        if (doc.get("content") or "").strip() and doc.get("memory_id") != exclude_id
    ]
    if not pool:
        return []

    try:
        from .recall import get_encoder, top_k_vector_scores

        encoder = get_encoder()
        query_vector = ((await _encode(encoder, [content])) or [None])[0]
        if query_vector is None:
            return []
        vectors = await _pool_vectors(coll, pool, encoder, len(query_vector))
        best = top_k_vector_scores(query_vector, vectors, limit)
    except Exception as e:
        log.warning(f"Edge shortlist scoring failed: {e}")
        return []

    # The score travels with each candidate: the classifier and the searching
    # agent both need to see that a "nearest neighbour" on a sparse corpus may
    # sit at 0.55 — similarity picks who gets considered, the number is what
    # lets judgment refuse it.
    return [{
        "memory_id": pool[i].get("memory_id"),
        "content": pool[i].get("content"),
        "type": pool[i].get("type"),
        "score": round(score, 3),
    } for i, score in best]


async def _encode(encoder, texts: list[str]):
    """Embed off the event loop.

    The encoder is CPU-bound and this path is no longer background-only — the
    `memory` tool reaches it on a live turn, where a synchronous encode of a few
    hundred texts would stall every other coroutine in the process.
    """
    return await asyncio.get_running_loop().run_in_executor(None, encoder, texts)


async def _pool_vectors(coll, pool: list[dict], encoder, dim: int) -> list:
    """One vector per pool memory, encoding and storing whatever is missing.

    Without stored vectors, ranking the whole history means re-encoding the
    whole history on every commit, which is what would force a window back in.
    So the first pass that sees a memory pays for its embedding once and writes
    it back; every later pass is a dot product. Self-healing, so no migration
    and no backfill script to remember to run.

    A vector of the wrong width is treated as missing: the encoder is
    configurable (`RECALL_ENCODER`) and switching it changes the dimension, at
    which point every stored vector is stale rather than wrong.
    """
    vectors = [None] * len(pool)
    missing: list[int] = []
    for i, doc in enumerate(pool):
        stored = doc.get("embedding")
        if isinstance(stored, list) and len(stored) == dim:
            vectors[i] = stored
        else:
            missing.append(i)
    if not missing:
        return vectors

    fresh = (await _encode(encoder, [pool[i]["content"] for i in missing])) or []
    for i, vector in zip(missing, fresh):
        vectors[i] = vector
    # Ranking already has what it needs; storing is the optimisation for next
    # time. So a write failure stops the writing (a broken store fails for all
    # of them) and never the shortlist.
    for i in missing:
        if vectors[i] is None:
            continue
        try:
            await coll.update_one(
                {"memory_id": pool[i].get("memory_id")},
                {"$set": {"embedding": list(vectors[i])}},
            )
        except Exception as e:
            log.warning(f"Could not store memory embeddings: {e}")
            break
    return vectors


async def memory_ids_by_recall(recall_ids: list[str]) -> dict[str, str]:
    """recall_id -> memory_id for the memories these recalls stand for.

    A recall fire tells us a recall matched, not which memory it is the trigger
    for. `trigger.recall_id` is stamped on the candidate when its trigger is
    built, so this is the reverse of that link — the graph uses it to find its
    expansion seeds, and the consolidator to name what a fire surfaced.
    """
    coll = await _collection(CANDIDATES_COLLECTION)
    ids = [rid for rid in (recall_ids or []) if rid]
    if coll is None or not ids:
        return {}
    try:
        cursor = coll.find(
            {"trigger.recall_id": {"$in": ids}, "status": "committed"},
            {"_id": 0, "memory_id": 1, "trigger": 1},
        )
        return {
            doc["trigger"]["recall_id"]: doc["memory_id"]
            async for doc in cursor
            if doc.get("memory_id") and (doc.get("trigger") or {}).get("recall_id")
        }
    except Exception as e:
        log.warning(f"Seed lookup failed for recalls {ids}: {e}")
        return {}


async def memory_ids_for_recalls(recall_ids: list[str]) -> list[str]:
    """The memories these recalls stand for — the graph's expansion seeds."""
    return list((await memory_ids_by_recall(recall_ids)).values())


async def memory_texts(memory_ids: list[str]) -> dict[str, dict]:
    """memory_id -> {content, type, topic, kg_triplets} for the ids given.

    Triplets come along because a semantic memory committed as KG facts stores
    no prose at all, and a reader handed its id has nothing else to show.
    """
    coll = await _collection(CANDIDATES_COLLECTION)
    ids = [mid for mid in (memory_ids or []) if mid]
    if coll is None or not ids:
        return {}
    try:
        cursor = coll.find(
            {"memory_id": {"$in": ids}},
            {"_id": 0, "memory_id": 1, "content": 1, "type": 1, "topic": 1,
             "kg_triplets": 1},
        )
        return {doc["memory_id"]: doc async for doc in cursor if doc.get("memory_id")}
    except Exception as e:
        log.warning(f"Memory text lookup failed: {e}")
        return {}


async def list_committed_memories(limit: int = 50, offset: int = 0) -> dict:
    """Paged committed-memory listing for Tower's Learned page.

    Projection at the query per the listing rule; `preview` is derived here so
    the template never touches full content for a row it only lists.
    """
    coll = await _collection(CANDIDATES_COLLECTION)
    if coll is None:
        return {"total": 0, "memories": []}
    query = {"status": "committed"}
    try:
        total = await coll.count_documents(query)
        cursor = coll.find(query, {
            "_id": 0, "memory_id": 1, "type": 1, "topic": 1, "content": 1,
            "source": 1, "created_at_ts": 1, "created_at": 1,
            "trigger.recall_id": 1, "trigger.status": 1,
        }).sort("created_at_ts", -1).skip(max(0, offset)).limit(max(1, limit))
        rows = [doc async for doc in cursor]
    except Exception as e:
        log.warning(f"Committed-memory listing failed: {e}")
        return {"total": 0, "memories": []}
    for row in rows:
        flat = " ".join((row.pop("content", "") or "").split())
        row["preview"] = flat[:200] + ("…" if len(flat) > 200 else "")
    return {"total": total, "memories": rows}


async def memory_admin_overview(memory_id: str) -> dict | None:
    """Everything Tower's memory detail page shows, joined on the one id:
    the candidate record, graph edges both directions, supersession, and the
    retrieval telemetry for the memory and its trigger."""
    from . import memory_graph

    coll = await _collection(CANDIDATES_COLLECTION)
    if coll is None:
        return None
    try:
        doc = await coll.find_one({"memory_id": memory_id}, {"_id": 0, "embedding": 0})
    except Exception as e:
        log.warning(f"Memory overview lookup failed for {memory_id}: {e}")
        return None
    if not doc:
        return None
    out: dict = {"memory": doc, "edges_out": [], "edges_in": [], "replaced_by": None}
    try:
        out["edges_out"] = await memory_graph.edges_from(memory_id)
        out["edges_in"] = await memory_graph.edges_into(memory_id)
        out["replaced_by"] = (await memory_graph.replacements([memory_id])).get(memory_id)
    except Exception as e:
        log.warning(f"Memory overview edge lookup failed for {memory_id}: {e}")
    neighbour_ids = [e.get("to") for e in out["edges_out"]] + [e.get("from") for e in out["edges_in"]]
    summaries = await memory_texts([nid for nid in neighbour_ids if nid])
    for edge in out["edges_out"]:
        edge["summary"] = " ".join(((summaries.get(edge.get("to")) or {}).get("content") or "").split())[:110]
    for edge in out["edges_in"]:
        edge["summary"] = " ".join(((summaries.get(edge.get("from")) or {}).get("content") or "").split())[:110]

    keys = [f"memory:{memory_id}"]
    recall_id = (doc.get("trigger") or {}).get("recall_id")
    if recall_id:
        keys.append(f"recall:{recall_id}")
    out["stats"] = []
    stats_coll = await _collection(STATS_COLLECTION)
    if stats_coll is not None:
        try:
            out["stats"] = [
                s async for s in stats_coll.find(
                    {"memory_key": {"$in": keys}}, {"_id": 0},
                )
            ]
        except Exception as e:
            log.warning(f"Memory overview stats lookup failed for {memory_id}: {e}")
    out["events"] = []
    events_coll = await _collection(RETRIEVAL_EVENTS_COLLECTION)
    if events_coll is not None:
        try:
            out["events"] = [
                e async for e in events_coll.find(
                    {"memory_key": {"$in": keys}},
                    {"_id": 0, "retrieval_id": 1, "memory_key": 1, "memory_kind": 1,
                     "ts": 1, "graded": 1, "used": 1, "outcome": 1, "channel": 1,
                     "unopened": 1},
                ).sort("ts", -1).limit(20)
            ]
        except Exception as e:
            log.warning(f"Memory overview event lookup failed for {memory_id}: {e}")
    return out


async def _commit_drawer(
    content: str, topic: str | None, *, room: str, memory_id: str,
) -> tuple[str, dict, str]:
    """File the drawer under the memory's own id, so the two are one thing.

    A palace hit then carries the id that `memory()`, the typed graph and
    retrieval telemetry all address — no second identifier to reconcile, and no
    marker text smuggled into the content to carry it.
    """
    from . import palace
    result = await palace.add_drawer(
        content=content, topic=topic, wing="agent", room=room, drawer_id=memory_id,
    )
    return (
        "committed",
        {"kind": "drawer", "room": room, "topic": topic, "drawer_id": memory_id},
        f"drawer: {result}",
    )


async def _commit_procedural(
    content: str, topic: str | None, *, memory_id: str,
) -> tuple[str, dict, str]:
    """Write knowledge/procedures/<slug>.md + an INDEX.md row (the reusable,
    re-readable reference), and a palace drawer in room=procedures so
    palace_search can also surface it. This is how repeated episode patterns
    graduate into procedural knowledge — see the plan's memory taxonomy.
    """
    slug = _slugify(topic or (content.splitlines()[0] if content else ""), f"lesson-{uuid.uuid4().hex[:8]}")
    proc_dir = Path("knowledge/procedures")
    proc_dir.mkdir(parents=True, exist_ok=True)
    path = proc_dir / f"{slug}.md"
    n = 1
    while path.exists():
        n += 1
        candidate_slug = f"{slug}-{n}"
        path = proc_dir / f"{candidate_slug}.md"
        slug = candidate_slug
    trigger = (topic or (content.splitlines()[0] if content else "")).strip()[:120] or "See content."
    # The id goes in the file because a procedure is read by `cat` as often as
    # by a palace search, and a read the harness never sees still has to tell
    # the agent (and the consolidator grading the transcript) which memory it is.
    path.write_text(
        f"# {slug}\n\n{content}\n\n---\n"
        f"_Filed by memory consolidation ({_now().date().isoformat()}) — "
        f"memory:{memory_id}._\n",
        encoding="utf-8",
    )
    try:
        _append_index_row(Path("knowledge/INDEX.md"), slug, trigger, str(path))
    except Exception as e:
        log.warning(f"knowledge/INDEX.md update failed for {slug}: {e}")

    from . import palace
    drawer_result = await palace.add_drawer(
        content=content, topic=slug, wing="agent", room="procedures", drawer_id=memory_id,
    )
    return (
        "committed",
        {"kind": "knowledge_file", "path": str(path), "topic": slug,
         "drawer_id": memory_id},
        f"procedural: wrote {path} + INDEX.md row; drawer: {drawer_result}",
    )


def _append_index_row(index_path: Path, id_: str, trigger: str, path: str) -> None:
    if not index_path.exists():
        return
    text = index_path.read_text(encoding="utf-8")
    trigger = trigger.replace("|", "/").replace("\n", " ")
    query = id_.replace("-", " ")
    row = f"| {id_} | {trigger} | `{path}` | `{query}` |\n"
    index_path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    with open(index_path, "a", encoding="utf-8") as f:
        f.write(row)


async def _commit_preference(
    content: str, topic: str | None, *, memory_id: str,
) -> tuple[str, dict, str]:
    """Preferences go to today's daily log (hot, already-loaded context) and a
    palace drawer (durable, searchable) — never a direct MEMORY.md edit here.
    That stable prompt file is on every turn, so a single statement is not
    enough to earn a place in it. Repetition is: once the same preference has
    been restated `_PROMOTION_MIN_CONFIRMATIONS` times, `promote_preferences`
    lifts it into MEMORY.md's managed block at reflection time.
    """
    from . import palace
    from .memory import MemoryManager

    entry = f"[preference:{topic}] {content}" if topic else f"[preference] {content}"
    try:
        MemoryManager().append_daily_log(entry)
        logged = True
    except Exception as e:
        log.warning(f"Preference daily-log write failed: {e}")
        logged = False
    drawer_result = await palace.add_drawer(
        content=content, topic=topic, wing="agent", room="preferences", drawer_id=memory_id,
    )
    prefix = "logged + " if logged else ""
    return (
        "committed",
        {"kind": "daily_log+drawer", "topic": topic, "drawer_id": memory_id},
        f"preference: {prefix}drawer: {drawer_result}",
    )


# ─── Preference promotion into the always-on prompt ───────────────────────
# A preference stored in a drawer only helps when something retrieves it. For
# "how you should talk to me", that is too late: it has to be in context before
# the first token. MEMORY.md is a STABLE_FILES entry, so anything here is in
# every system prompt — which is exactly why it cannot be a per-turn write.
#
# The gate is repetition, and the dedupe pipeline already counts it for free: a
# preference taught again is stored once and every restatement lands as a
# candidate with `duplicate_of` pointing back at the original. So confirmations
# = the original plus its duplicates, with no extra bookkeeping and no model
# judgment about whether something is "really" durable.

_PROMOTION_MIN_CONFIRMATIONS = 3
# The stable block is prompt weight paid on every single turn, so the managed
# section is capped hard and evicts the least-recently-confirmed entry.
_PROMOTION_MAX_ENTRIES = 10
_PROMOTION_MAX_CHARS = 1200
_PROMOTION_BEGIN = "<!-- managed:learned-preferences -->"
_PROMOTION_END = "<!-- /managed:learned-preferences -->"
_PROMOTION_HEADING = "## Learned preferences"


async def promotable_preferences(
    min_confirmations: int = _PROMOTION_MIN_CONFIRMATIONS,
) -> list[dict]:
    """Committed preferences confirmed in enough DISTINCT sessions to promote.

    Confirmations count sessions, not restatements: three rewordings inside
    one chat's to-and-fro are one confirmation, because they are one occasion
    on which the preference mattered. Restatements arrive as candidates with
    `duplicate_of` pointing at the original (commit-time dedupe for
    near-verbatim, the edge classifier's `restates` verdict for paraphrase),
    each carrying the session it happened in. A doc with no session — an old
    row, or a commit outside any turn — cannot confirm anything.

    Whether a confirmed preference is CRUCIAL enough for the always-on prompt
    is a separate judgment (`promote_preferences` runs the gate); this only
    answers "was it independently reinforced".

    Newest confirmation first, so the eviction order under the size cap is
    least-recently-reinforced.
    """
    coll = await _collection(CANDIDATES_COLLECTION)
    if coll is None:
        return []
    try:
        cursor = coll.find({"type": "preference"}).sort("created_at_ts", 1)
        docs = [doc async for doc in cursor]
    except Exception as e:
        log.warning(f"Preference promotion query failed: {e}")
        return []

    by_id = {d["memory_id"]: d for d in docs}

    def _root(memory_id: str) -> str:
        """Follow `duplicate_of` to the memory a chain of restatements is about.

        A restatement stamped by the classifier keeps `status="committed"`, so
        it stays in the shortlist pool and a later paraphrase can legitimately
        name IT as the thing being restated. Folding a single hop would split
        one preference's confirmations along the chain (C->A->B counts as two
        twos, never a three) and promote a restatement as if it were its own
        rule. Stops on a cycle or a dangling pointer.
        """
        seen = {memory_id}
        current = memory_id
        for _ in range(len(by_id)):  # bounded: a chain cannot exceed the corpus
            target = (by_id.get(current) or {}).get("duplicate_of")
            if not target or target in seen or target not in by_id:
                break
            seen.add(target)
            current = target
        return current

    originals = {
        d["memory_id"]: d for d in docs
        if d.get("status") == "committed" and _root(d["memory_id"]) == d["memory_id"]
    }
    sessions: dict[str, set] = {
        mid: ({d.get("session_id")} if d.get("session_id") else set())
        for mid, d in originals.items()
    }
    last_seen: dict[str, float] = {
        mid: d.get("created_at_ts") or 0.0 for mid, d in originals.items()
    }
    for doc in docs:
        if not doc.get("duplicate_of") or not doc.get("session_id"):
            continue
        target = _root(doc["memory_id"])
        if target in sessions:
            sessions[target].add(doc["session_id"])
            last_seen[target] = max(
                last_seen[target], doc.get("created_at_ts") or 0.0,
            )

    ready = [
        {
            "memory_id": mid,
            "content": " ".join((originals[mid].get("content") or "").split()),
            "confirmations": len(confirmed),
            "last_confirmed_ts": last_seen[mid],
            "promotion_review": originals[mid].get("promotion_review"),
        }
        for mid, confirmed in sessions.items()
        if len(confirmed) >= min_confirmations
        and (originals[mid].get("content") or "").strip()
    ]
    ready.sort(key=lambda r: r["last_confirmed_ts"], reverse=True)
    return ready


def fitting_promotions(entries: list[dict]) -> tuple[list[dict], list[str]]:
    """(entries that fit, lines to render). Both caps applied, in one place.

    The character budget usually binds before the entry count does, so the
    audit has to ask this rather than assume the first _PROMOTION_MAX_ENTRIES
    were promoted — otherwise the entries the char cap dropped get recorded as
    promoted and the eviction list comes back empty.

    Each line carries a `(memory:<id>)` pointer: the block holds the rule's
    short form, the full memory (context, links, history) stays one
    `memory(id=…)` away — MEMORY.md points at memory, it does not replace it.
    """
    fitted, lines, used = [], [], 0
    for entry in entries[:_PROMOTION_MAX_ENTRIES]:
        text = entry["content"]
        if len(text) > 200:
            text = text[:197] + "..."
        line = f"- {text} (memory:{entry['memory_id']})"
        if used + len(line) > _PROMOTION_MAX_CHARS:
            break
        fitted.append(entry)
        lines.append(line)
        used += len(line)
    return fitted, lines


def render_promotion_section(entries: list[dict]) -> str:
    """The managed block, trimmed to the entry and character budget.

    Approved entries beyond the caps are not silently gone: one overflow line
    says how many more exist and how to reach them — the second level the cap
    creates lives in the memory store, not in a bigger block.
    """
    fitted, lines = fitting_promotions(entries)
    if not lines:
        return ""
    overflow = len(entries) - len(fitted)
    if overflow > 0:
        # Deliberately not a "- " bullet: entry lines are the promotion audit's
        # unit of account (fitting_promotions), and this is a signpost, not an
        # entry.
        lines.append(
            f"…plus {overflow} more confirmed preference(s) — "
            f'memory(query="preference")'
        )
    return "\n".join([_PROMOTION_HEADING, _PROMOTION_BEGIN, *lines, _PROMOTION_END])


def apply_promotion_section(existing: str, section: str) -> str:
    """Replace the managed block in MEMORY.md, leaving hand-written text alone.

    Everything outside the markers is the user's, so promotion is never allowed
    to rewrite it — an automated writer that can touch the whole always-on
    prompt is a much bigger blast radius than this feature needs.
    """
    text = existing or ""
    start = text.find(_PROMOTION_BEGIN)
    end = text.find(_PROMOTION_END)
    if start != -1 and end > start:
        head = text[:start].rstrip("\n")
        # Drop the heading that precedes the markers so it isn't duplicated.
        if head.rstrip().endswith(_PROMOTION_HEADING):
            head = head.rstrip()[: -len(_PROMOTION_HEADING)].rstrip("\n")
        tail = text[end + len(_PROMOTION_END):].lstrip("\n")
        parts = [p for p in (head, section, tail) if p.strip()]
        return "\n\n".join(parts).rstrip("\n") + "\n"
    if not section:
        return text
    return text.rstrip("\n") + "\n\n" + section + "\n"


# The crucialness gate — one small model call per NEWLY eligible preference,
# never per tick: the verdict is stamped on the candidate (`promotion_review`)
# and re-read forever after. Bounded per pass so a backlog cannot turn one
# reflection tick into a call storm.
_GATE_REVIEWS_PER_PASS = 3

_PROMOTION_GATE_SYSTEM = """\
You decide whether a learned preference belongs in the agent's ALWAYS-ON \
system prompt (MEMORY.md — present before the first token of every single \
turn, forever), or whether on-demand recall is enough.

Always-on is for rules that must bind BEFORE anything happens — where waiting \
for a retrieval to fire is already too late:
- how to speak (tone, brevity, language) — shapes the first token;
- safety and approval gates (never send X without asking) — must precede the act;
- standing constraints with no textual trigger — nothing in a conversation \
resembles them, so a cue can never fire.

On-demand is for everything whose moment announces itself in the text: facts, \
procedures, tool habits, project details, topic-specific preferences. The \
recall system surfaces those when the conversation resembles them.

The always-on prompt is paid on every turn and capped, so the bar is high: \
promote only what would misfire on turn one without it.

Return ONLY a JSON object: {"promote": true|false, "reason": "<one line>"}"""


async def _review_promotion(entry: dict) -> dict | None:
    """One gate verdict for one confirmed preference, or None on any failure.

    None means "not judged yet" — the entry stays out of MEMORY.md and is
    retried on a later pass. Failing closed is the point: nothing enters the
    always-on prompt without a stored verdict.
    """
    try:
        from . import model_registry

        provider = model_registry.get_provider("promotion_gate")
        model = model_registry.model_for("promotion_gate")
        pinned_effort = model_registry.task_effort("promotion_gate")
        response = await provider.create_message(
            model=model,
            max_tokens=300,
            system=_PROMOTION_GATE_SYSTEM,
            messages=[{
                "role": "user",
                "content": (
                    f"Preference (confirmed in {entry['confirmations']} distinct "
                    f"sessions):\n{entry['content'][:1500]}"
                ),
            }],
            thinking=bool(pinned_effort),
            effort=pinned_effort,
            temperature=0.0,
        )
    except Exception as e:
        log.warning("Promotion gate call failed for %s: %s", entry["memory_id"], e)
        return None
    text = " ".join(
        getattr(block, "text", "") or ""
        for block in (getattr(response, "content", None) or [])
        if getattr(block, "type", None) == "text" or getattr(block, "text", None)
    )
    from .recall_cues import _parse_json_object

    payload = _parse_json_object(text)
    if not payload or not isinstance(payload.get("promote"), bool):
        log.warning("Promotion gate returned unparseable verdict for %s", entry["memory_id"])
        return None
    return {
        "promote": payload["promote"],
        "reason": str(payload.get("reason") or "")[:300],
        "reviewed_at": _now(),
    }


async def promote_preferences(
    min_confirmations: int = _PROMOTION_MIN_CONFIRMATIONS,
) -> str:
    """Sync MEMORY.md's managed block with the preferences that earned a place.

    Two gates in sequence, deliberately different in kind: repetition across
    distinct sessions (counted by the harness — evidence it was independently
    reinforced) and then crucialness (judged once by a model — evidence recall
    would be too late for it). Only entries passing both render; a rejected
    entry stays a perfectly good memory, reachable by recall and search, just
    not paid for on every turn.

    Returns a one-line summary for the caller's log, or "" when nothing
    changed. Idempotent: verdicts are stamped once, and re-running with the
    same evidence rewrites identical bytes.
    """
    entries = await promotable_preferences(min_confirmations)

    # Judge the newly eligible (no stored verdict), bounded per pass.
    coll = await _collection(CANDIDATES_COLLECTION)
    reviewed = 0
    for entry in entries:
        if entry.get("promotion_review") is not None:
            continue
        if reviewed >= _GATE_REVIEWS_PER_PASS:
            break
        # Count the ATTEMPT, not the success: a gate that is failing (provider
        # down, unparseable replies) returns None for every entry, and bounding
        # only the successes would turn one pass into a call per eligible
        # preference — a call storm exactly when the calls are useless.
        reviewed += 1
        verdict = await _review_promotion(entry)
        if verdict is None:
            continue
        entry["promotion_review"] = verdict
        if coll is not None:
            try:
                await coll.update_one(
                    {"memory_id": entry["memory_id"]},
                    {"$set": {"promotion_review": verdict}},
                )
            except Exception as e:
                log.warning(
                    "Could not store promotion verdict for %s: %s",
                    entry["memory_id"], e,
                )

    approved = [
        e for e in entries if (e.get("promotion_review") or {}).get("promote")
    ]
    section = render_promotion_section(approved)
    path = Path("config/MEMORY.md")
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
    except Exception as e:
        log.warning(f"MEMORY.md read failed: {e}")
        return ""
    updated = apply_promotion_section(existing, section)
    if updated == existing:
        return ""
    try:
        path.write_text(updated, encoding="utf-8")
    except Exception as e:
        log.warning(f"MEMORY.md promotion write failed: {e}")
        return ""
    # Which preferences hold the block, and which qualified but did not fit.
    # Eviction is otherwise invisible: the entry simply stops being in the file,
    # with nothing anywhere saying it was ever promoted or why it left.
    fitted, _ = fitting_promotions(approved)
    promoted = [e["memory_id"] for e in fitted]
    await record_maintenance("promotion", {
        "eligible": len(entries),
        "approved": [e["memory_id"] for e in approved],
        "rejected": [
            e["memory_id"] for e in entries
            if e.get("promotion_review") is not None
            and not (e["promotion_review"] or {}).get("promote")
        ],
        "unreviewed": [
            e["memory_id"] for e in entries if e.get("promotion_review") is None
        ],
        "promoted": promoted,
        "evicted": [e["memory_id"] for e in approved if e["memory_id"] not in promoted],
        "min_confirmations": min_confirmations,
    })
    return (
        f"promoted {len(promoted)} preference(s) into MEMORY.md "
        f"(>= {min_confirmations} distinct-session confirmations + gate approval)"
    )


# ─── Episode index + recent-consolidation digest (task-end consolidator) ──


async def build_episode_index(
    *,
    channel_id: str,
    session_id: str | None,
    session_segments: list[dict],
    compaction_summary: str,
) -> str:
    """Prompt-appendix describing everything this episode touched, including
    content already folded out of the live buffer by compaction. The
    consolidator drills into a specific segment with read_episode_segment
    rather than being handed the full verbatim history up front — cost stays
    proportional to what looks learnable, not to episode length.
    """
    lines = ["[EPISODE_INDEX]", f"channel: {channel_id}"]
    if session_id:
        lines.append(f"session_id: {session_id}")
    if compaction_summary:
        lines.append("")
        lines.append(
            "Cumulative summary of everything compacted out of the live buffer "
            "so far this episode (folds every earlier compaction):"
        )
        lines.append(compaction_summary)
    if session_segments:
        lines.append("")
        lines.append(
            "Archived segments this episode — read_episode_segment(segment_id) for "
            "verbatim drill-down; only pull one where the summary above hints at "
            "something learnable (a correction, failure, or strategy change):"
        )
        for seg in session_segments:
            sid = seg.get("segment_id") or "(archive failed — not retrievable)"
            lines.append(
                f"- {sid} kind={seg.get('kind')} messages={seg.get('message_count')} "
                f"archived_at={seg.get('archived_at')}"
            )
    else:
        lines.append("")
        lines.append(
            "No compaction happened this episode — the message buffer below is "
            "the complete episode, nothing to drill into."
        )
    retrieval_summary = await _episode_retrieval_summary(session_id)
    if retrieval_summary:
        lines.append("")
        lines.append(retrieval_summary)
    return "\n".join(lines)


async def _episode_retrieval_summary(session_id: str | None) -> str:
    if not session_id:
        return ""
    coll = await _collection(RETRIEVAL_EVENTS_COLLECTION)
    if coll is None:
        return ""
    try:
        cursor = coll.find({"session_id": session_id}).sort("ts", 1).limit(200)
        events = [doc async for doc in cursor]
    except Exception as e:
        log.warning(f"Episode retrieval query failed: {e}")
        return ""
    if not events:
        return ""
    # A recall fire is logged under the recall that matched, but a correction
    # has to name the memory behind it — the same link the graph seeds from.
    backing = await memory_ids_by_recall([
        str(e.get("memory_key", "")).split("recall:", 1)[-1]
        for e in events if str(e.get("memory_key", "")).startswith("recall:")
    ])
    lines = [
        "[EPISODE_RETRIEVALS] Memory surfaced during this episode. Call "
        "grade_retrieval(retrieval_id, used, outcome) for every row not marked "
        "[already graded]. [not opened] means the harness saw no memory(id=…) "
        "for it this episode — that is context, NOT the answer: a fire whose "
        "instruction you acted on directly is used=true even though nothing "
        "was opened.",
    ]
    for e in events:
        q = (e.get("query_or_cue") or "").replace("\n", " ").strip()
        if len(q) > 120:
            q = q[:117] + "..."
        graded = " [already graded]" if e.get("graded") else ""
        unopened = " [not opened]" if e.get("unopened") else ""
        key = str(e.get("memory_key", ""))
        memory_id = backing.get(key.split("recall:", 1)[-1]) if key.startswith("recall:") else None
        origin = f" memory_id={memory_id}" if memory_id else ""
        lines.append(
            f"- retrieval_id={e.get('retrieval_id')} kind={e.get('memory_kind')} "
            f"memory_key={key}{origin} query=\"{q}\"{unopened}{graded}"
        )
    return "\n".join(lines)


async def recent_consolidation_digest(limit: int = 10) -> str:
    """Cross-tick continuity for the disposable consolidator channel: what
    recent consolidation passes committed/rejected, at digest cost instead of
    transcript cost. There is deliberately no persistent consolidator channel
    (see the plan) — this is how a fresh tick still knows what recent ticks did.
    """
    coll = await _collection(CANDIDATES_COLLECTION)
    if coll is None:
        return ""
    try:
        cursor = coll.find(
            {"source": {"$in": ["task_consolidator", "periodic_consolidator"]}},
        ).sort("created_at_ts", -1).limit(limit)
        docs = [doc async for doc in cursor]
    except Exception as e:
        log.warning(f"Consolidation digest query failed: {e}")
        return ""
    if not docs:
        return ""
    lines = ["[RECENT_CONSOLIDATION_DIGEST] What recent consolidation passes decided (oldest first):"]
    for d in reversed(docs):
        content = (d.get("content") or "").replace("\n", " ").strip()
        if len(content) > 140:
            content = content[:137] + "..."
        lines.append(
            f"- {d.get('status')} [{d.get('type')}] memory_id={d.get('memory_id')} "
            f"via {d.get('source')}: {content}"
        )
    return "\n".join(lines)


# Palace room holding the learning system's own audit trail. Separate from
# `episodes` (what happened) and `knowledge` (what is true): this is what the
# agent *learned* and why, so "when did I pick that up?" is answerable by
# search instead of only from the consolidator's private digest.
LEARNING_ROOM = "learning"


async def write_consolidation_summary(
    *,
    channel_id: str,
    reason: str,
    session_id: str | None,
    since_ts: float,
) -> str:
    """File one compact record of what a consolidation pass changed.

    Built by the harness from the pass's own provenance records rather than by
    asking the model to summarise itself: the candidates already say exactly
    what was committed, skipped, and why, and a self-report could drift from
    what actually landed.

    The pass's *conversation* stays disposable. Only this record persists, which
    keeps the learner's raw reasoning out of the retrieval corpus — a
    consolidation transcript that could be searched and re-mined would let the
    learning loop start learning from itself.
    """
    coll = await _collection(CANDIDATES_COLLECTION)
    if coll is None:
        return ""
    try:
        cursor = coll.find({
            "source": "task_consolidator",
            "created_at_ts": {"$gte": since_ts},
        }).sort("created_at_ts", 1)
        docs = [doc async for doc in cursor]
    except Exception as e:
        log.warning(f"Consolidation summary query failed: {e}")
        return ""

    committed = [d for d in docs if d.get("status") == "committed"]
    if not committed:
        # Nothing durable came out of this episode. An empty record every time
        # the agent finishes a chat would bury the useful ones.
        return ""

    stamp = _now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"Consolidation pass — {stamp} (channel {channel_id}, {reason}).",
        f"Committed {len(committed)} memory item(s):",
    ]
    for doc in committed:
        content = (doc.get("content") or _render_triplets(
            [tuple(t) for t in (doc.get("kg_triplets") or []) if len(t) == 3]
        )).replace("\n", " ").strip()
        if len(content) > 220:
            content = content[:217] + "..."
        line = f"- [{doc.get('type')}] {content}"
        note = (doc.get("note") or "").replace("\n", " ").strip()
        if note:
            line += f" (why: {note[:160]})"
        lines.append(line)

    skipped = [d for d in docs if d.get("status") == "duplicate"]
    if skipped:
        lines.append(f"Skipped {len(skipped)} near-duplicate(s) already stored.")

    try:
        from . import palace

        content = "\n".join(lines)
        await palace.add_drawer(
            content=content,
            topic=f"consolidation-{_now().strftime('%Y-%m-%d')}",
            wing="agent",
            room=LEARNING_ROOM,
        )
        return content
    except Exception as e:
        log.warning(f"Consolidation summary write failed: {e}")
        return ""


async def read_episode_segment(segment_id: str) -> str:
    """Verbatim drill-down into one archived segment. Read-only, consolidation-only.

    Segment ids come from the [EPISODE_INDEX] appendix — compact_channel tags
    every archived batch (see GaladrielAgent._record_session_segment) with its
    batch-dir name.

    Reads the DATABASE first. The staged .md on disk is crash-safety scaffolding,
    not the record of truth: `mine_pending_shutdown_archives` deletes each batch
    once mined, and on Fargate the staging dir is per-host, so a segment archived
    by one task is invisible to the next. The drawers carry `source_file` (which
    embeds the batch dir name) and `chunk_number`, so the segment reassembles
    from Mongo exactly. Disk remains a fallback for batches staged but not yet
    mined — the one case where the file exists and the drawers do not.
    """
    segment_id = (segment_id or "").strip()
    if not segment_id or "/" in segment_id or "\\" in segment_id or ".." in segment_id:
        return "[error] invalid segment_id."
    try:
        from . import palace
        try:
            body = await asyncio.to_thread(palace.segment_text, segment_id)
        except Exception as exc:
            # The database is the record of truth, but an unreachable database
            # must degrade to the staged file rather than abort the read.
            log.warning("Segment read from the palace failed (%s); trying disk", exc)
            body = ""
        if not body:
            conv_dir = palace._archive_root() / segment_id / palace.CONVERSATION_ROOM
            if conv_dir.is_dir():
                texts = [p.read_text(encoding="utf-8") for p in sorted(conv_dir.glob("*.md"))]
                body = "\n\n".join(t for t in texts if t.strip())
        if not body:
            return (
                f"[not available] segment {segment_id} has no archived content "
                "(it may have failed to mine, or belong to a different runtime)."
            )
        if len(body) > 20000:
            body = body[:20000] + "\n...[truncated]"
        return body
    except Exception as e:
        return f"[error] reading segment {segment_id}: {e}"


# ─── Utility telemetry (phase 4) ───────────────────────────────────────


async def log_retrieval(
    *,
    memory_key: str,
    memory_kind: str,
    channel_id: str,
    session_id: str | None,
    query_or_cue: str,
    rank: int = 1,
) -> str | None:
    """Record that memory content was surfaced into a conversation. Harness
    -only — never the model — which is what makes retrieval_count purely
    code-driven. Returns the retrieval_id (for later grade_retrieval calls),
    or None if telemetry is unavailable (best effort; never blocks the
    surfacing tool call that triggered it).
    """
    coll = await _collection(RETRIEVAL_EVENTS_COLLECTION)
    if coll is None:
        return None
    retrieval_id = uuid.uuid4().hex
    try:
        await coll.insert_one({
            "retrieval_id": retrieval_id,
            "memory_key": memory_key,
            "memory_kind": memory_kind,
            "channel": channel_id,
            "session_id": session_id,
            "query_or_cue": (query_or_cue or "")[:500],
            "rank": rank,
            "ts": _now(),
            "graded": False,
        })
    except Exception as e:
        log.warning(f"Retrieval log failed: {e}")
        return None
    await _bump_stats(memory_key, {"retrieval_count": 1}, memory_kind=memory_kind)
    return retrieval_id


async def grade_retrieval(retrieval_id: str, used: bool, outcome: str, note: str = "") -> str:
    """Deterministic counter update behind the consolidator's grade_retrieval
    tool call. `used` distinguishes retrieved-but-ignored (no counter bump)
    from retrieved-and-acted-on; `outcome` splits helpful vs harmful use.
    """
    events = await _collection(RETRIEVAL_EVENTS_COLLECTION)
    if events is None:
        return "[error] retrieval telemetry unavailable (Mongo not configured)."
    try:
        event = await events.find_one({"retrieval_id": retrieval_id})
    except Exception as e:
        return f"[error] retrieval lookup failed: {e}"
    if event is None:
        return f"[error] unknown retrieval_id: {retrieval_id}"
    if event.get("graded"):
        return f"[skip] retrieval_id {retrieval_id} was already graded."

    outcome_ = (outcome or "neutral").strip().lower()
    if outcome_ not in ("helpful", "harmful", "neutral"):
        outcome_ = "neutral"
    used_ = bool(used)

    # graded_count moves on EVERY grade, used or not: it is the denominator the
    # utility bins divide by. retrieval_count counts surfacings, which happen
    # whether or not an episode ever ends to grade them, so dividing by it made
    # a long-running chat look like a broken trigger — the memory kept firing
    # while its use_count sat frozen for want of a grading pass, not for want
    # of usefulness.
    inc: dict[str, int] = {"graded_count": 1}
    set_fields: dict = {"last_graded": _now()}
    if used_:
        inc["use_count"] = 1
        set_fields["last_used"] = _now()
        if outcome_ == "helpful":
            inc["helpful_count"] = 1
        elif outcome_ == "harmful":
            inc["harmful_count"] = 1

    # The graded flag is what makes this idempotent — the guard above refuses a
    # second grade of the same id. So the counters may only move once that flag
    # is safely stored: bumping first would let a retry double-count every
    # use/helpful/harmful on a write that never landed.
    try:
        await events.update_one(
            {"retrieval_id": retrieval_id},
            {"$set": {
                "graded": True, "used": used_, "outcome": outcome_,
                "grade_note": (note or "")[:500],
            }},
        )
    except Exception as e:
        log.warning(f"Retrieval grade write failed: {e}")
        return f"[error] could not record the grade for {retrieval_id}: {e}"
    await _bump_stats(
        event["memory_key"], inc, set_fields=set_fields,
        memory_kind=event.get("memory_kind"),
    )
    return f"Graded retrieval {retrieval_id}: used={used_} outcome={outcome_}."


# The sweep works one bounded batch at a time, so a backlog cannot occupy the
# loop for minutes. EVERY row it looks at is stamped `unopened_checked`, even
# the ones it has nothing to say about — that stamp, not `graded`, is the
# watermark. Stamping only the rows it can act on would leave the ones it
# cannot (sys_/user recalls, which never gain a backing memory) at the head of
# the ts-ordered index forever, and once they filled a batch the sweep would
# re-read the same dead rows every hour and never reach a live one.
_UNOPENED_BATCH = 500


async def record_unopened_fires(
    session_id: str | None = None, older_than_minutes: int = 60,
) -> int:
    """Record which recall fires were never followed by opening their memory.

    A COUNTER, not a grade. The obvious shortcut — "never opened, therefore
    used=false" — was measured against the live corpus and is false: of the
    model-graded `used=true` fires that have a backing memory, 17 of 19 had no
    open in the same session, all `outcome=helpful`. Cue instructions are
    frequently actionable on their own ("apply the message-approval protocol
    from …"), so the agent acts without ever opening the memory. Writing
    `used=false` there would also be irreversible: `grade_retrieval` refuses a
    second grade, so a mechanical verdict permanently displaces the model's
    real one, and `use_ratio` (the bad-trigger bin's numerator) collapses on
    triggers the model itself called helpful.

    So this leaves `graded` alone. It bumps `unopened_count` beside
    `retrieval_count`, which makes "fired 43 times, opened twice" legible to
    the consolidator as evidence to weigh — never as a verdict the harness
    reached on its own.

    Deliberately narrow:
    - Only `memory_kind="recall"` events. An open IS the use; a graph expansion
      was already injected. Neither has a mechanical "ignored" signal.
    - Only fires whose recall backs a committed memory (`memory_ids_by_recall`);
      for a sys_/user recall there is no memory an open could have touched.
    - Only events with a session_id: the session is the join key between a fire
      and the opens that would answer it.

    Callers: task-end consolidation (age 0 — the episode is over), post
    -compaction (age 10 min — the fire text just left the buffer), and the
    scheduler hourly for chats that reach neither boundary. Idempotent: a
    stamped row leaves the query for good.
    """
    events = await _collection(RETRIEVAL_EVENTS_COLLECTION)
    if events is None:
        return 0
    if session_id is None and older_than_minutes <= 0:
        # No session and no age bound would count fires still on screen.
        return 0

    query: dict = {
        "memory_kind": "recall",
        "unopened_checked": {"$ne": True},
    }
    if session_id is not None:
        query["session_id"] = session_id
    else:
        query["session_id"] = {"$ne": None}
    if older_than_minutes > 0:
        query["ts"] = {"$lt": _now() - timedelta(minutes=older_than_minutes)}

    try:
        cursor = events.find(
            query,
            {"_id": 0, "retrieval_id": 1, "memory_key": 1, "session_id": 1},
        ).limit(_UNOPENED_BATCH + 1)
        pending = [doc async for doc in cursor]
    except Exception as e:
        log.warning(f"Unopened-fire query failed: {e}")
        return 0
    if len(pending) > _UNOPENED_BATCH:
        log.info(
            "[Unopened] batch cap hit (%d); the rest continues next sweep",
            _UNOPENED_BATCH,
        )
        pending = pending[:_UNOPENED_BATCH]
    pending = [d for d in pending if d.get("retrieval_id") and d.get("session_id")]
    if not pending:
        return 0

    recall_ids = list({
        str(d["memory_key"]).split("recall:", 1)[-1]
        for d in pending
        if str(d.get("memory_key", "")).startswith("recall:")
    })
    backing = await memory_ids_by_recall(recall_ids)

    opened: set[tuple[str, str]] = set()
    if backing:
        sessions = list({d["session_id"] for d in pending})
        try:
            cursor = events.find(
                {
                    "session_id": {"$in": sessions},
                    # Filter on memory_kind, never the key prefix: memory_open
                    # and graph_expansion share the `memory:<id>` namespace,
                    # and both mean the content entered context.
                    "memory_kind": {"$in": ["memory_open", "graph_expansion"]},
                },
                {"_id": 0, "session_id": 1, "memory_key": 1},
            )
            async for doc in cursor:
                key = str(doc.get("memory_key", ""))
                if key.startswith("memory:"):
                    opened.add((doc["session_id"], key.split("memory:", 1)[-1]))
        except Exception as e:
            log.warning(f"Unopened-fire open lookup failed: {e}")
            return 0

    # Stamp the whole batch first, including rows with nothing to record: the
    # stamp is the watermark, and a row left unstamped comes back forever.
    try:
        await events.update_many(
            {"retrieval_id": {"$in": [d["retrieval_id"] for d in pending]}},
            {"$set": {"unopened_checked": True}},
        )
    except Exception as e:
        log.warning(f"Unopened-fire watermark write failed: {e}")
        return 0

    counted = 0
    unopened_ids: list[str] = []
    for doc in pending:
        key = str(doc.get("memory_key", ""))
        if not key.startswith("recall:"):
            continue
        memory_id = backing.get(key.split("recall:", 1)[-1])
        if not memory_id:
            continue  # sys_/user recall — no memory an open could have touched.
        if (doc["session_id"], memory_id) in opened:
            continue  # opened — the content did reach context.
        await _bump_stats(key, {"unopened_count": 1})
        unopened_ids.append(doc["retrieval_id"])
        counted += 1
    if unopened_ids:
        # Marked on the event too, so [EPISODE_RETRIEVALS] can show the grading
        # pass which fires went unopened without recomputing the join.
        try:
            await events.update_many(
                {"retrieval_id": {"$in": unopened_ids}},
                {"$set": {"unopened": True}},
            )
        except Exception as e:
            log.warning(f"Unopened-fire event mark failed: {e}")
    if counted:
        log.info(
            "[Unopened] %d fire(s) whose memory was never opened%s",
            counted, f" session={session_id}" if session_id else "",
        )
    return counted


# Namespaces `log_retrieval` stamps. A flag against anything else is either a
# typo or an invented key, and because the bad-memory bin has no retrieval gate,
# such a row would sit in the consolidator's evidence permanently.
_KEY_NAMESPACES = ("memory:", "recall:", "drawer_search:", "kg:", "kg_timeline:")


async def _resolve_memory_key(memory_key: str) -> tuple[str | None, str]:
    """(canonical key, error). Refuses a key nothing could ever have produced.

    A bare memory id is accepted and namespaced: the tool description invites
    one, but retrieval telemetry writes `memory:<id>`, so taking it literally
    would open a second stats doc and split a memory's correction count away
    from its retrieval history.
    """
    key = (memory_key or "").strip()
    if not key:
        return None, "[error] memory_key is required."
    if key.startswith(_KEY_NAMESPACES):
        return key, ""

    candidates = await _collection(CANDIDATES_COLLECTION)
    if candidates is not None:
        try:
            if await candidates.find_one({"memory_id": key}, {"_id": 1}):
                return f"memory:{key}", ""
        except Exception as e:
            log.warning(f"flag_memory key lookup failed: {e}")
            return f"memory:{key}", ""

    # Not a known memory and not a namespaced key. It may still be a real
    # telemetry row (a KG tuple keyed before these namespaces existed), so
    # accept it if the stats or events collections have already seen it.
    for name, field in (
        (STATS_COLLECTION, "memory_key"), (RETRIEVAL_EVENTS_COLLECTION, "memory_key"),
    ):
        coll = await _collection(name)
        if coll is None:
            continue
        try:
            if await coll.find_one({field: key}, {"_id": 1}):
                return key, ""
        except Exception as e:
            log.warning(f"flag_memory key lookup failed: {e}")
    return None, (
        f"[error] {key!r} does not name anything this system has surfaced. Use a "
        "memory_id from a report or search result, or a key exactly as it "
        "appears in [EPISODE_RETRIEVALS] (memory:<id>, recall:<id>, kg:<s>/<p>/<o>)."
    )


async def flag_memory(memory_key: str, reason: str) -> str:
    """Deterministic counter update behind the consolidator's flag_memory tool
    call — the strongest single signal (an explicit user contradiction this
    episode), independent of whether the memory was even retrieved.
    """
    key, error = await _resolve_memory_key(memory_key)
    if key is None:
        return error
    await _bump_stats(
        key, {"user_correction_count": 1},
        set_fields={"last_flagged": _now(), "last_flag_reason": (reason or "")[:300]},
    )
    return f"Flagged {key}: {reason or 'corrected'} (user_correction_count +1)."


async def _bump_stats(
    memory_key: str,
    inc: dict[str, int],
    *,
    set_fields: dict | None = None,
    memory_kind: str | None = None,
) -> None:
    coll = await _collection(STATS_COLLECTION)
    if coll is None:
        return
    defaults = {
        "memory_key": memory_key, "created_at": _now(),
        "retrieval_count": 0, "use_count": 0, "helpful_count": 0,
        "harmful_count": 0, "user_correction_count": 0, "unopened_count": 0,
        "last_used": None,
    }
    if memory_kind:
        defaults["memory_kind"] = memory_kind
    skip = set(inc or {}) | set(set_fields or {})
    update: dict = {"$setOnInsert": {k: v for k, v in defaults.items() if k not in skip}}
    if inc:
        update["$inc"] = inc
    if set_fields:
        update["$set"] = set_fields
    try:
        await coll.update_one({"memory_key": memory_key}, update, upsert=True)
    except Exception as e:
        log.warning(f"Memory stats update failed for {memory_key}: {e}")


# ─── Periodic-consolidator evidence (phase 5) ──────────────────────────


async def backfill_graded_counts() -> int:
    """Give pre-existing stats docs the graded counters the bins now divide by.

    `graded_count` was introduced after the fact, so without this every memory
    recorded before it is invisible to `memory_utility_report` — and the stale
    bin would never recover on its own, because a dormant memory is exactly the
    one that never surfaces again to earn a fresh grade. `retrieval_events`
    already carries the per-event `graded` flag, so the counts are recoverable.

    One aggregation over graded events, not a query per key: the per-key form
    was two unindexed scans each, which on a mature tenant is a full-collection
    read per memory before the process can serve a turn.

    Idempotent: only docs with no `graded_count` are touched, so a second run
    is a no-op and a doc the live path has already counted is left alone.
    """
    stats = await _collection(STATS_COLLECTION)
    events = await _collection(RETRIEVAL_EVENTS_COLLECTION)
    if stats is None or events is None:
        return 0
    try:
        missing = await stats.distinct(
            "memory_key", {"graded_count": {"$exists": False}},
        )
    except Exception as e:
        log.warning(f"Graded-count backfill scan failed: {e}")
        return 0
    if not missing:
        return 0
    wanted = set(missing)
    totals: dict[str, tuple[int, object]] = {}
    try:
        cursor = events.aggregate([
            {"$match": {"graded": True, "memory_key": {"$in": list(wanted)}}},
            {"$group": {
                "_id": "$memory_key",
                "graded": {"$sum": 1},
                "last": {"$max": "$ts"},
            }},
        ])
        async for row in cursor:
            totals[row["_id"]] = (int(row.get("graded") or 0), row.get("last"))
    except Exception as e:
        log.warning(f"Graded-count aggregation failed: {e}")
        return 0

    patched = 0
    for key in wanted:
        graded, last = totals.get(key, (0, None))
        fields: dict = {"graded_count": graded}
        # The stale bin ages off `last_used or last_graded`, so a backfilled
        # doc with neither would sit outside every bin forever.
        if last:
            fields["last_graded"] = last
        try:
            result = await stats.update_one(
                {"memory_key": key, "graded_count": {"$exists": False}},
                {"$set": fields},
            )
            # A live grade landing mid-backfill creates the field first and
            # wins; count only what this pass actually wrote.
            patched += int(getattr(result, "modified_count", 0) or 0)
        except Exception as e:
            log.warning(f"Graded-count backfill failed for {key}: {e}")
    log.info("[Backfill] graded_count set on %d memory_stats doc(s)", patched)
    return patched


async def record_maintenance(kind: str, detail: dict) -> None:
    """Append one harness-maintenance record. Never raises into a caller.

    Destructive maintenance stores enough to reconstruct what it removed, not
    just how much: a mistuned decay threshold that strips the graph over a week
    is only recoverable if the pruned edges themselves were written down.
    """
    coll = await _collection(MAINTENANCE_COLLECTION)
    if coll is None:
        return
    try:
        await coll.insert_one({"kind": kind, "ts": _now(), **detail})
    except Exception as e:
        log.warning(f"Maintenance record ({kind}) failed: {e}")


async def memory_utility_report(limit: int = 15) -> str:
    """Harness-computed evidence for the periodic consolidator: bad-trigger
    (content is fine, cue is too broad) vs bad-memory (harmful/corrected
    content) vs stale (unused for a long time) — the three signatures the
    plan calls out. The model reads ratios already computed here; it never
    aggregates raw counters itself.
    """
    coll = await _collection(STATS_COLLECTION)
    if coll is None:
        return "[memory utility report] stats unavailable (Mongo not configured)."
    try:
        docs = [doc async for doc in coll.find({})]
    except Exception as e:
        return f"[memory utility report] query failed: {e}"
    if not docs:
        return "[memory utility report] no telemetry recorded yet."

    now = _now()
    bad_trigger, bad_memory, stale = [], [], []
    for d in docs:
        key = d.get("memory_key")
        retrieval = int(d.get("retrieval_count", 0) or 0)
        graded = int(d.get("graded_count", 0) or 0)
        use = int(d.get("use_count", 0) or 0)
        harmful = int(d.get("harmful_count", 0) or 0)
        helpful = int(d.get("helpful_count", 0) or 0)
        corrections = int(d.get("user_correction_count", 0) or 0)
        unopened = int(d.get("unopened_count", 0) or 0)
        last_used = _as_aware(d.get("last_used"))
        last_graded = _as_aware(d.get("last_graded"))
        use_ratio = (use / graded) if graded else None
        # Bad trigger: surfaced a lot, rarely used when we actually looked, and
        # what little use there was wasn't harmful — content is fine, cue is
        # too broad. Gated on graded evidence: an ungraded surfacing is an
        # unmeasured one, and counting it as unused indicts a memory for its
        # episode never ending.
        if graded >= _MIN_GRADED_FOR_BIN and use_ratio is not None and use_ratio < 0.15 and harmful == 0:
            bad_trigger.append((key, retrieval, graded, use, use_ratio, helpful, unopened))
        if harmful > 0 or corrections > 0:
            bad_memory.append((key, harmful, corrections))
        # Stale: measured, and nothing useful has happened for a long time.
        # `last_used or last_graded` is the reference point — without the
        # fallback a never-used memory graded five minutes ago read as stale
        # immediately, which is the bad-trigger signature, not an aged-out one.
        reference = last_used or last_graded
        if graded > 0 and reference is not None and (now - reference).days > _STALE_DAYS:
            stale.append((key, retrieval, last_used, helpful))

    lines = [
        "[MEMORY_UTILITY_REPORT] Harness-computed retrieval/use evidence "
        "(counts and ratios only — verify actual content before acting on any row).",
        "",
        f"Bad-trigger candidates (graded often, rarely used, never harmful when used "
        f"-> narrow the recall cue / drawer summary, do NOT touch the content). "
        f"helpful>0 means the content earned its keep when it did land, so fix the "
        f"cue and leave the memory alone. use_ratio is over GRADED surfacings, "
        f"not raw ones. unopened counts fires the harness saw no memory(id=…) "
        f"for — supporting context only: a fire can be genuinely used without "
        f"one, so never treat a high unopened count as a verdict. Top {limit}:",
    ]
    for key, retrieval, graded, use, use_ratio, helpful, unopened in sorted(
        bad_trigger, key=lambda x: -x[2],
    )[:limit]:
        lines.append(
            f"- {key}: retrieved={retrieval} graded={graded} used={use} "
            f"use_ratio={use_ratio:.2f} helpful={helpful} unopened={unopened}"
        )
    if not bad_trigger:
        lines.append("(none)")
    lines.append("")
    # A corrected memory must leave the bin, or it sits in the evidence
    # forever: the counters are monotonic and supersession is the only signal
    # that the correction actually happened. Resolvable only for memory:<id>
    # keys — a recall:<id> flag stays until its backing memory is retired.
    resolved = 0
    if bad_memory:
        from . import memory_graph

        flagged_ids = [
            key.split("memory:", 1)[-1]
            for key, _, _ in bad_memory if str(key).startswith("memory:")
        ]
        try:
            replaced = await memory_graph.replacements(flagged_ids)
        except Exception as e:
            log.warning(f"Supersession lookup for utility report failed: {e}")
            replaced = {}
        still_bad = [
            row for row in bad_memory
            if not (
                str(row[0]).startswith("memory:")
                and row[0].split("memory:", 1)[-1] in replaced
            )
        ]
        resolved = len(bad_memory) - len(still_bad)
        bad_memory = still_bad

    lines.append(
        f"Bad-memory candidates (harmful use and/or user correction "
        f"-> rewrite, invalidate the KG fact, or delete), top {limit}:"
    )
    for key, harmful, corrections in sorted(bad_memory, key=lambda x: (-x[2], -x[1]))[:limit]:
        lines.append(f"- {key}: harmful_count={harmful} user_correction_count={corrections}")
    if not bad_memory:
        lines.append("(none)")
    if resolved:
        lines.append(
            f"({resolved} previously-flagged memory/memories already superseded "
            f"— resolved, not listed)"
        )
    lines.append("")
    lines.append(
        f"Stale candidates (graded before, nothing useful in {_STALE_DAYS}+ days "
        f"-> consider archiving), top {limit}:"
    )
    for key, retrieval, last_used, helpful in sorted(stale, key=lambda x: -x[1])[:limit]:
        when = last_used.isoformat() if last_used else "never used"
        lines.append(
            f"- {key}: retrieval_count={retrieval} last_used={when} helpful={helpful}"
        )
    if not stale:
        lines.append("(none)")
    return "\n".join(lines)

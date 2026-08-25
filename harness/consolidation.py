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
import logging
import os
import re
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

log = logging.getLogger("galadriel.consolidation")

CANDIDATES_COLLECTION = "memory_candidates"
RETRIEVAL_EVENTS_COLLECTION = "retrieval_events"
STATS_COLLECTION = "memory_stats"

MEMORY_TYPES = ("semantic", "procedural", "preference")

# Prose dedupe: a new candidate whose content is at least this similar to a
# recently committed candidate of the same type is treated as a duplicate.
_DUPLICATE_COSINE_FLOOR = 0.93
_DEDUPE_POOL_SIZE = 200
_DEDUPE_WINDOW_DAYS = 30
# Staleness floor for the periodic-consolidator utility report.
_STALE_DAYS = 90

# Memory types that earn a retrieval trigger on commit. A stored memory with
# no trigger is inert — reachable only when the agent happens to search for it
# — so every durable type gets one. Kept as an explicit set rather than
# "everything", because episodic conversation mining also lands in the palace
# and must never spawn triggers for ordinary chat archives.
RECALL_ELIGIBLE_TYPES = frozenset({"semantic", "procedural", "preference"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _slugify(text: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:60] or fallback


def clean_triplets(raw) -> list[tuple[str, str, str]]:
    """Normalize/validate raw [subject, predicate, object] triplets."""
    out: list[tuple[str, str, str]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, dict):
            item = [item.get("subject"), item.get("predicate"), item.get("object")]
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            continue
        s, p, o = (str(x).strip() for x in item)
        if s and p and o:
            out.append((s, p, o))
    return out[:20]


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


# ─── Candidate pipeline ────────────────────────────────────────────────


async def commit_candidate(
    *,
    type: str,
    content: str = "",
    kg_triplets: list | None = None,
    topic: str | None = None,
    valid_from: str | None = None,
    evidence: list[str] | None = None,
    confidence: float | None = None,
    source: str = "runtime",
    note: str = "",
) -> dict:
    """Validate -> dedupe -> write -> record provenance.

    `source` identifies the caller for provenance/digest purposes:
    "runtime" (the `learn` tool), "task_consolidator", "periodic_consolidator",
    or "tower". Returns {"status": "committed"|"duplicate"|"error",
    "memory_id": str, "detail": str}.
    """
    type_ = (type or "").strip().lower()
    if type_ not in MEMORY_TYPES:
        return {
            "status": "error", "memory_id": None,
            "detail": f"unknown type {type!r} — must be one of {MEMORY_TYPES}.",
        }
    content = (content or "").strip()
    triplets = clean_triplets(kg_triplets) if kg_triplets else []
    if not content and not triplets:
        return {"status": "error", "memory_id": None, "detail": "content or kg_triplets is required."}
    if triplets and type_ != "semantic":
        return {"status": "error", "memory_id": None, "detail": "kg_triplets is only valid for type=semantic."}
    valid_from, date_warning = _clean_valid_from(valid_from)

    memory_id = uuid.uuid4().hex
    duplicate_of: str | None = None
    try:
        if triplets:
            status, destination, detail = await _commit_kg(triplets, valid_from)
        else:
            duplicate_of = await _is_prose_duplicate(type_, content)
            if duplicate_of:
                status, destination, detail = (
                    "duplicate", {},
                    f"duplicate of existing memory {duplicate_of} — not re-written",
                )
            elif type_ == "semantic":
                status, destination, detail = await _commit_drawer(content, topic, room="knowledge")
            elif type_ == "procedural":
                status, destination, detail = await _commit_procedural(content, topic)
            else:
                status, destination, detail = await _commit_preference(content, topic)
    except Exception as e:
        status, destination, detail = "error", {}, f"{type(e).__name__}: {e}"
    if date_warning:
        detail = f"{detail} ({date_warning})"

    await _save_candidate({
        "memory_id": memory_id,
        "type": type_,
        "content": content,
        "kg_triplets": [list(t) for t in triplets],
        "topic": topic,
        "valid_from": valid_from,
        "evidence": evidence or [],
        "confidence": confidence,
        "source": source,
        "note": note,
        "status": status,
        "destination": destination,
        "duplicate_of": duplicate_of,
        "created_at": _now(),
        "created_at_ts": _now().timestamp(),
    })
    if status == "committed":
        _schedule_post_commit(memory_id, type_, content, triplets, topic)
    return {"status": status, "memory_id": memory_id, "detail": detail}


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
        edges, written = [], 0
        if neighbours:
            edges = await memory_graph.classify_edges(
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
            await coll.update_one(
                {"memory_id": memory_id},
                {"$set": {"edges": {
                    "classified_at": _now(), "written": written, "source": source,
                }}},
            )
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


def _clean_valid_from(raw: str | None) -> tuple[str | None, str]:
    """(iso_date, warning) — validate an optional ISO date.

    A malformed date is dropped rather than failing the commit: losing the
    memory over a bad optional field is worse than falling back to kg_add's
    default of today. The warning rides back in the tool result so the caller
    can pass it correctly next time.
    """
    text = (raw or "").strip()
    if not text:
        return None, ""
    try:
        return date.fromisoformat(text).isoformat(), ""
    except ValueError:
        return None, f"ignored valid_from={text!r} — not an ISO date (YYYY-MM-DD)"


async def _commit_kg(
    triplets: list[tuple[str, str, str]], valid_from: str | None = None,
) -> tuple[str, dict, str]:
    from . import palace
    loop = asyncio.get_running_loop()
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
    destination = {"kind": "kg", "stored": stored, "duplicate": duplicate}
    if stored:
        msg = f"kg: stored {stored} triplet(s)" + (f", {duplicate} already known" if duplicate else "")
        return "committed", destination, msg
    return "duplicate", destination, f"kg: all {duplicate} triplet(s) already known — no-op"


async def _is_kg_duplicate(subject: str, predicate: str, obj: str) -> bool:
    """Exact-match dedupe: same subject+predicate already pointing at the same
    object. A different object is a legitimate new/updated fact, not a dup —
    the KG's own temporal model (valid_from/valid_to) handles that case.
    """
    try:
        from . import palace
        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(
            None, lambda: palace.kg_query(subject=subject, predicate=predicate)
        )
        return f"--[{predicate}]-> `{obj}`" in text
    except Exception as e:
        log.warning(f"KG dedupe check failed ({subject}/{predicate}/{obj}): {e}")
        return False


async def _is_prose_duplicate(type_: str, content: str) -> str | None:
    """Return the duplicate memory_id if content is a near-duplicate of a
    recently committed candidate of the same type, else None.

    Scope: dedupes against candidates that went through THIS pipeline in the
    last _DEDUPE_WINDOW_DAYS — not the full historical palace corpus (palace's
    own search API returns formatted markdown, not per-item vectors, so full
    corpus dedupe isn't reachable without forking palace internals). This
    still catches the common failure mode the plan is worried about: the same
    consolidator (or the runtime agent) proposing the same lesson repeatedly
    across ticks. Best-effort: any failure means "not a duplicate" — a missed
    dedupe costs a redundant write, never a lost one.
    """
    coll = await _collection(CANDIDATES_COLLECTION)
    if coll is None:
        return None
    try:
        since_ts = _now().timestamp() - _DEDUPE_WINDOW_DAYS * 86400
        cursor = coll.find(
            {"type": type_, "status": "committed", "created_at_ts": {"$gte": since_ts}},
            {"memory_id": 1, "content": 1},
        ).sort("created_at_ts", -1).limit(_DEDUPE_POOL_SIZE)
        pool = [doc async for doc in cursor]
    except Exception as e:
        log.warning(f"Prose dedupe query failed: {e}")
        return None
    pool = [doc for doc in pool if (doc.get("content") or "").strip()]
    if not pool:
        return None
    try:
        from .recall import get_encoder, _argmax_cosine
        hit = _argmax_cosine(get_encoder(), content, [doc["content"] for doc in pool])
    except Exception as e:
        log.warning(f"Prose dedupe scoring failed: {e}")
        return None
    if hit is None or hit[0] < _DUPLICATE_COSINE_FLOOR:
        return None
    matched_text = hit[1]
    for doc in pool:
        if doc.get("content") == matched_text:
            return doc.get("memory_id")
    return None


# Neighbours offered to the edge classifier. The bound is the whole design:
# unbounded, relating a new memory to the corpus is O(new x all) model
# comparisons. Embedding shortlists first, so exactly one model call sees a
# handful of plausible partners.
_EDGE_SHORTLIST_SIZE = 8


async def shortlist_neighbours(
    content: str, *, exclude_id: str | None = None, limit: int = _EDGE_SHORTLIST_SIZE,
) -> list[dict]:
    """The committed memories most similar to `content`, as edge candidates.

    Deliberately spans every memory type. The canonical edge is cross-type — a
    procedural lesson depending on a semantic fact — so filtering by type the
    way `_is_prose_duplicate` does would hide exactly the edges worth having.

    Similarity only selects who gets *considered*; the model decides whether any
    functional relation exists, and "none" is a common, correct answer.
    """
    coll = await _collection(CANDIDATES_COLLECTION)
    if coll is None or not (content or "").strip():
        return []
    try:
        since_ts = _now().timestamp() - _DEDUPE_WINDOW_DAYS * 86400
        cursor = coll.find(
            {"status": "committed", "created_at_ts": {"$gte": since_ts}},
            {"memory_id": 1, "content": 1, "type": 1},
        ).sort("created_at_ts", -1).limit(_DEDUPE_POOL_SIZE)
        pool = [doc async for doc in cursor]
    except Exception as e:
        log.warning(f"Edge shortlist query failed: {e}")
        return []

    pool = [
        doc for doc in pool
        if (doc.get("content") or "").strip() and doc.get("memory_id") != exclude_id
    ]
    if not pool:
        return []
    try:
        from .recall import _top_k_cosine, get_encoder
        best = _top_k_cosine(
            get_encoder(), content, [doc["content"] for doc in pool], limit,
        )
    except Exception as e:
        log.warning(f"Edge shortlist scoring failed: {e}")
        return []

    by_content = {doc["content"]: doc for doc in pool}
    out = []
    for text in best:
        doc = by_content.get(text)
        if doc:
            out.append({
                "memory_id": doc.get("memory_id"),
                "content": doc.get("content"),
                "type": doc.get("type"),
            })
    return out


async def memory_ids_for_recalls(recall_ids: list[str]) -> list[str]:
    """The memories whose triggers are these recalls — the expansion seeds.

    A recall fire tells us a recall matched, not which memory it stands for.
    `trigger.recall_id` is stamped on the candidate when its trigger is built,
    so this is the reverse of that link.
    """
    coll = await _collection(CANDIDATES_COLLECTION)
    ids = [rid for rid in (recall_ids or []) if rid]
    if coll is None or not ids:
        return []
    try:
        cursor = coll.find(
            {"trigger.recall_id": {"$in": ids}, "status": "committed"},
            {"_id": 0, "memory_id": 1},
        )
        return [doc["memory_id"] async for doc in cursor if doc.get("memory_id")]
    except Exception as e:
        log.warning(f"Seed lookup failed for recalls {ids}: {e}")
        return []


async def memory_texts(memory_ids: list[str]) -> dict[str, dict]:
    """memory_id -> {content, type, topic} for the ids given. Projected."""
    coll = await _collection(CANDIDATES_COLLECTION)
    ids = [mid for mid in (memory_ids or []) if mid]
    if coll is None or not ids:
        return {}
    try:
        cursor = coll.find(
            {"memory_id": {"$in": ids}},
            {"_id": 0, "memory_id": 1, "content": 1, "type": 1, "topic": 1},
        )
        return {doc["memory_id"]: doc async for doc in cursor if doc.get("memory_id")}
    except Exception as e:
        log.warning(f"Memory text lookup failed: {e}")
        return {}


async def _commit_drawer(content: str, topic: str | None, *, room: str) -> tuple[str, dict, str]:
    from . import palace
    result = await palace.add_drawer(content=content, topic=topic, wing="agent", room=room)
    return "committed", {"kind": "drawer", "room": room, "topic": topic}, f"drawer: {result}"


async def _commit_procedural(content: str, topic: str | None) -> tuple[str, dict, str]:
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
    path.write_text(
        f"# {slug}\n\n{content}\n\n---\n"
        f"_Filed by memory consolidation ({_now().date().isoformat()})._\n",
        encoding="utf-8",
    )
    try:
        _append_index_row(Path("knowledge/INDEX.md"), slug, trigger, str(path))
    except Exception as e:
        log.warning(f"knowledge/INDEX.md update failed for {slug}: {e}")

    from . import palace
    drawer_result = await palace.add_drawer(content=content, topic=slug, wing="agent", room="procedures")
    return (
        "committed",
        {"kind": "knowledge_file", "path": str(path), "topic": slug},
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


async def _commit_preference(content: str, topic: str | None) -> tuple[str, dict, str]:
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
    drawer_result = await palace.add_drawer(content=content, topic=topic, wing="agent", room="preferences")
    prefix = "logged + " if logged else ""
    return "committed", {"kind": "daily_log+drawer", "topic": topic}, f"preference: {prefix}drawer: {drawer_result}"


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
    """Committed preferences the user has restated enough times to promote.

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

    originals = {d["memory_id"]: d for d in docs if d.get("status") == "committed"}
    confirmations: dict[str, int] = {mid: 1 for mid in originals}
    last_seen: dict[str, float] = {
        mid: d.get("created_at_ts") or 0.0 for mid, d in originals.items()
    }
    for doc in docs:
        target = doc.get("duplicate_of")
        if target in confirmations:
            confirmations[target] += 1
            last_seen[target] = max(
                last_seen[target], doc.get("created_at_ts") or 0.0,
            )

    ready = [
        {
            "memory_id": mid,
            "content": " ".join((originals[mid].get("content") or "").split()),
            "confirmations": count,
            "last_confirmed_ts": last_seen[mid],
        }
        for mid, count in confirmations.items()
        if count >= min_confirmations and (originals[mid].get("content") or "").strip()
    ]
    ready.sort(key=lambda r: r["last_confirmed_ts"], reverse=True)
    return ready


def render_promotion_section(entries: list[dict]) -> str:
    """The managed block, trimmed to the entry and character budget."""
    lines, used = [], 0
    for entry in entries[:_PROMOTION_MAX_ENTRIES]:
        text = entry["content"]
        if len(text) > 200:
            text = text[:197] + "..."
        line = f"- {text}"
        if used + len(line) > _PROMOTION_MAX_CHARS:
            break
        lines.append(line)
        used += len(line)
    if not lines:
        return ""
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


async def promote_preferences(
    min_confirmations: int = _PROMOTION_MIN_CONFIRMATIONS,
) -> str:
    """Sync MEMORY.md's managed block with the preferences that earned a place.

    Returns a one-line summary for the caller's log, or "" when nothing
    changed. Idempotent: re-running with the same evidence rewrites identical
    bytes.
    """
    entries = await promotable_preferences(min_confirmations)
    section = render_promotion_section(entries)
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
    return (
        f"promoted {len(section.splitlines()) - 3 if section else 0} preference(s) "
        f"into MEMORY.md (>= {min_confirmations} confirmations)"
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
    lines = [
        "[EPISODE_RETRIEVALS] Memory surfaced during this episode — grade each "
        "with grade_retrieval(retrieval_id, used, outcome):",
    ]
    for e in events:
        q = (e.get("query_or_cue") or "").replace("\n", " ").strip()
        if len(q) > 120:
            q = q[:117] + "..."
        graded = " [already graded]" if e.get("graded") else ""
        lines.append(
            f"- retrieval_id={e.get('retrieval_id')} kind={e.get('memory_kind')} "
            f"memory_key={e.get('memory_key')} query=\"{q}\"{graded}"
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
        lines.append(f"- {d.get('status')} [{d.get('type')}] via {d.get('source')}: {content}")
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
    """Verbatim drill-down into one archived compaction batch. Read-only,
    consolidation-only. Segment ids come from the [EPISODE_INDEX] appendix —
    compact_channel tags every archived batch (see
    GaladrielAgent._record_session_segment) with its batch-dir name.
    """
    segment_id = (segment_id or "").strip()
    if not segment_id or "/" in segment_id or "\\" in segment_id or ".." in segment_id:
        return "[error] invalid segment_id."
    try:
        from . import palace
        conv_dir = palace._archive_root() / segment_id / palace.CONVERSATION_ROOM
        if not conv_dir.is_dir():
            return (
                f"[not available] segment {segment_id} was not found on this host "
                "(the archive may live on a different runtime instance, or was "
                "already cleaned up)."
            )
        texts = [p.read_text(encoding="utf-8") for p in sorted(conv_dir.glob("*.md"))]
        texts = [t for t in texts if t.strip()]
        if not texts:
            return f"[not available] segment {segment_id} has no archived content."
        body = "\n\n".join(texts)
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

    inc: dict[str, int] = {}
    set_fields: dict = {}
    if used_:
        inc["use_count"] = 1
        set_fields["last_used"] = _now()
        if outcome_ == "helpful":
            inc["helpful_count"] = 1
        elif outcome_ == "harmful":
            inc["harmful_count"] = 1

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
    if inc or set_fields:
        await _bump_stats(
            event["memory_key"], inc, set_fields=set_fields,
            memory_kind=event.get("memory_kind"),
        )
    return f"Graded retrieval {retrieval_id}: used={used_} outcome={outcome_}."


async def flag_memory(memory_key: str, reason: str) -> str:
    """Deterministic counter update behind the consolidator's flag_memory tool
    call — the strongest single signal (an explicit user contradiction this
    episode), independent of whether the memory was even retrieved.
    """
    if not memory_key:
        return "[error] memory_key is required."
    await _bump_stats(
        memory_key, {"user_correction_count": 1},
        set_fields={"last_flagged": _now(), "last_flag_reason": (reason or "")[:300]},
    )
    return f"Flagged {memory_key}: {reason or 'corrected'} (user_correction_count +1)."


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
        "harmful_count": 0, "user_correction_count": 0, "last_used": None,
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
        use = int(d.get("use_count", 0) or 0)
        harmful = int(d.get("harmful_count", 0) or 0)
        corrections = int(d.get("user_correction_count", 0) or 0)
        last_used = d.get("last_used")
        use_ratio = (use / retrieval) if retrieval else None
        # Bad trigger: fires a lot, rarely used, but what little use there was
        # wasn't harmful — the content is fine, the cue is too broad.
        if retrieval >= 5 and use_ratio is not None and use_ratio < 0.15 and harmful == 0:
            bad_trigger.append((key, retrieval, use, use_ratio))
        if harmful > 0 or corrections > 0:
            bad_memory.append((key, harmful, corrections))
        if retrieval > 0 and (last_used is None or (now - last_used).days > _STALE_DAYS):
            stale.append((key, retrieval, last_used))

    lines = [
        "[MEMORY_UTILITY_REPORT] Harness-computed retrieval/use evidence "
        "(counts and ratios only — verify actual content before acting on any row).",
        "",
        f"Bad-trigger candidates (high retrieval, rarely used, never harmful when used "
        f"-> narrow the recall cue / drawer summary, do NOT touch the content), top {limit}:",
    ]
    for key, retrieval, use, use_ratio in sorted(bad_trigger, key=lambda x: -x[1])[:limit]:
        lines.append(f"- {key}: retrieved={retrieval} used={use} use_ratio={use_ratio:.2f}")
    if not bad_trigger:
        lines.append("(none)")
    lines.append("")
    lines.append(
        f"Bad-memory candidates (harmful use and/or user correction "
        f"-> rewrite, invalidate the KG fact, or delete), top {limit}:"
    )
    for key, harmful, corrections in sorted(bad_memory, key=lambda x: (-x[2], -x[1]))[:limit]:
        lines.append(f"- {key}: harmful_count={harmful} user_correction_count={corrections}")
    if not bad_memory:
        lines.append("(none)")
    lines.append("")
    lines.append(
        f"Stale candidates (retrieved before, unused {_STALE_DAYS}+ days "
        f"-> consider archiving), top {limit}:"
    )
    for key, retrieval, last_used in sorted(stale, key=lambda x: -x[1])[:limit]:
        when = last_used.isoformat() if last_used else "never used"
        lines.append(f"- {key}: retrieval_count={retrieval} last_used={when}")
    if not stale:
        lines.append("(none)")
    return "\n".join(lines)

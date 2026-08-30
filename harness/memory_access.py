"""One way in to a memory: find it, open it, walk to what it connects to.

Before this, "retrieve a memory" meant three unrelated motions with three
different notions of identity — `palace_search` over drawer text, a KG query
over subject/predicate/object tuples, and `cat` on a procedure file — none of
which told the agent *which memory* it had just read. Nothing could be
addressed, so nothing could be followed, and telemetry could only record that
some search happened in some room.

The fix is one identity. A committed memory's `memory_id` is also its drawer
id (see `consolidation._commit_drawer`) and is stamped into its procedure file,
so every surface now names the same thing, and that name is what this module
takes.

## Activation is not retrieval

A recall fire says "something here may matter" and points at where it lives. It
deliberately does not carry the memory: the cue generator is instructed never to
restate it. Deciding whether to actually go and read it is the agent's call, not
the harness's — a fire that pre-loaded the memory plus its prerequisites would
spend context on a judgement the model never got to make.

So expansion hangs off *this* module rather than off the fire. Opening a memory
is the moment its dependencies become relevant, because that is the moment the
memory is actually being used. Same reason a person reminded of a topic doesn't
involuntarily recall its whole dependency chain — they recall it when they think
about the thing.

## What comes back

    prerequisites (inline, full text)   what the memory is wrong without
    the memory itself (full text)
    links (stubs: id, direction, relation, one line)

Prerequisites are inlined because a rule without the fact it rests on is a rule
you cannot apply. Everything else is a stub carrying an id, so the agent walks
the graph by calling this again — bounded by intent rather than by the harness
guessing how far to go. A memory with a thousand neighbours costs the same as
one with three.
"""

from __future__ import annotations

import asyncio
import logging
import re

log = logging.getLogger("galadriel.memory_access")

# Prerequisites reproduced in full. Past a few, the thing being explained is
# buried under its own context; the rest degrade to stubs like any other link.
_INLINE_PREREQUISITES = 3
# Per-memory text cap. Generous — a memory the agent explicitly asked for should
# arrive whole; this only stops one pathological drawer filling the turn.
_TEXT_CHARS = 1200
# Links listed before collapsing into a count. The count still names how many
# were dropped, so a dense neighbourhood reads as dense rather than as absent.
_STUB_LIMIT = 8
_STUB_CHARS = 110


def _one_line(text: str, limit: int = _STUB_CHARS) -> str:
    flat = " ".join((text or "").split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


def _body(text: str) -> str:
    flat = (text or "").strip()
    return flat[:_TEXT_CHARS] + ("… [truncated]" if len(flat) > _TEXT_CHARS else "")


async def find(query: str, limit: int = 5) -> str:
    """Curated memories closest in meaning to `query`, as openable stubs.

    Searches what was *learned*, not what was said — the raw conversation
    corpus stays with `palace_search`, which is a different question and two
    orders of magnitude bigger. Returns ids rather than content: choosing what
    to open is the point.
    """
    from . import consolidation

    query = (query or "").strip()
    if not query:
        return "[memory] give a `query` to search for, or an `id` to open."
    try:
        hits = await consolidation.shortlist_neighbours(query, limit=max(1, limit))
    except Exception as e:
        log.warning(f"Memory search failed: {e}")
        return f"[memory] search failed: {type(e).__name__}: {e}"
    if not hits:
        return (
            f"No curated memory matches `{query}`. Nothing has been learned about "
            "this yet — `palace_search` covers raw conversation history instead."
        )
    from . import memory_graph

    retired = await memory_graph.replacements(
        [hit.get("memory_id") for hit in hits if hit.get("memory_id")],
    )
    lines = [f"**Curated memories matching `{query}`** — open one with `memory(id=…)`:", ""]
    for hit in hits:
        # Search is where a retired rule is most dangerous: nothing else on the
        # line distinguishes it from a live one, and it was written to be
        # findable. It stays listed — the history is real — but never unmarked.
        replacement = retired.get(hit.get("memory_id"))
        marker = f" — REPLACED by `{replacement}`" if replacement else ""
        # The list is always filled to length, so the similarity rides along —
        # a top hit at 0.55 is the agent's cue that nothing really matches,
        # the same judgment palace search already hands over with its scores.
        score = hit.get("score")
        sim = f" sim={score:.2f}" if isinstance(score, (int, float)) else ""
        lines.append(
            f"- `{hit.get('memory_id')}` [{hit.get('type')}]{sim}{marker} "
            f"{_one_line(hit.get('content') or '', 160)}"
        )
    return "\n".join(lines)


_ID_IN_RESULT = re.compile(r"id=`([^`]+)`")


async def label_curated(markdown: str) -> str:
    """Mark the conversation-search hits that are actually learned memories.

    The two corpora share one store: a learned memory is filed as a drawer, so
    a search over conversation history can surface one, and nothing in the
    result would say so. Naming it is what keeps the distinction real rather
    than merely documented — and it tells the agent that this particular hit has
    linked context waiting behind `memory(id=…)`.

    Best-effort and purely additive: on any failure the result is returned
    exactly as the palace rendered it.
    """
    if not markdown or "id=`" not in markdown:
        return markdown
    from . import consolidation

    ids = _ID_IN_RESULT.findall(markdown)
    if not ids:
        return markdown
    try:
        curated = await consolidation.memory_texts(list(dict.fromkeys(ids)))
    except Exception as e:
        log.warning(f"Could not label curated search hits: {e}")
        return markdown
    if not curated:
        return markdown
    lines = []
    for line in markdown.splitlines():
        found = _ID_IN_RESULT.search(line)
        if found and found.group(1) in curated:
            kind = (curated[found.group(1)].get("type") or "memory").upper()
            line = f"{line}  **[LEARNED {kind} — open with memory(id=…) for its linked context]**"
        lines.append(line)
    return "\n".join(lines)


async def open_memory(
    memory_id: str, _resolving: bool = False,
) -> tuple[str, list[str]]:
    """Materialise one memory with its prerequisites and its links.

    Returns (rendered text, expanded memory ids) — the caller logs telemetry for
    the ids, because only it knows the channel and episode. An id that is not a
    curated memory falls through to the palace, so a drawer id from a search
    result opens too and the agent never has to know which store it came from.
    """
    from . import consolidation

    memory_id = (memory_id or "").strip()
    if memory_id.startswith("memory:"):
        memory_id = memory_id.split("memory:", 1)[-1].strip()
    if not memory_id:
        return "[memory] an `id` is required.", []

    texts = await consolidation.memory_texts([memory_id])
    doc = texts.get(memory_id)
    if not doc:
        # Live fires no longer print rule ids, but the consolidation appendix
        # and get_recent_recalls do, and old stored fires still carry them —
        # a model that opens a rule id would dead-end here and conclude the
        # memory does not exist. Resolve it instead.
        try:
            backing = await consolidation.memory_ids_by_recall([memory_id])
        except Exception:
            backing = {}
        backed = (backing or {}).get(memory_id)
        if backed and backed != memory_id and not _resolving:
            # One hop only: cyclic trigger data (A backs B backs A) must
            # degrade to a miss, not a RecursionError.
            text, expanded = await open_memory(backed, _resolving=True)
            return (
                f"`{memory_id}` is a recall trigger, not a memory id — opened "
                f"its backing memory `{backed}` instead.\n\n{text}"
            ), expanded
        return await _open_verbatim(memory_id), []
    if not (doc.get("content") or "").strip() and doc.get("kg_triplets"):
        # A semantic memory committed as triplets stores no prose, but it still
        # gets an id and a recall trigger — so without this it would open blank
        # for the one path most likely to send an agent here.
        doc = {**doc, "content": "\n".join(
            f"- {subject} — {predicate} — {obj}"
            for subject, predicate, obj in (
                triple for triple in doc["kg_triplets"] if len(triple) == 3
            )
        )}

    replaced_by = await _replaced_by(memory_id)
    prerequisites, links = await _neighbourhood(memory_id)
    inline = prerequisites[:_INLINE_PREREQUISITES]
    overflow = prerequisites[_INLINE_PREREQUISITES:]

    ids = [item["memory_id"] for item in inline]
    bodies = await consolidation.memory_texts(ids) if ids else {}

    out: list[str] = []
    if replaced_by:
        out.append(
            f"**This memory has been replaced.** `{replaced_by}` is the rule that "
            "now applies — open it before acting on anything below, which is kept "
            "as history."
        )
        out.append("")

    if inline:
        out.append("**Needed first — this memory is wrong without:**")
        out.append("")
        for item in inline:
            body = (bodies.get(item["memory_id"]) or {}).get("content") or ""
            if not body.strip():
                continue
            retired = item.get("replaced_by")
            note = f" — REPLACED by `{retired}`, read that instead" if retired else ""
            out.append(f"- `{item['memory_id']}` ({item['relation'].lower()}){note}")
            out.append(f"  {_body(body)}")
        out.append("")

    out.append(f"**Memory `{memory_id}`** [{doc.get('type') or 'memory'}]")
    if doc.get("topic"):
        out.append(f"_topic: {doc['topic']}_")
    out.append("")
    out.append(_body(doc.get("content") or ""))

    stubs = overflow + links
    if stubs:
        out.append("")
        out.append("**Also linked** — open any with `memory(id=…)`:")
        for item in stubs[:_STUB_LIMIT]:
            arrow = "→" if item["direction"] == "out" else "←"
            retired = " [REPLACED]" if item.get("replaced_by") else ""
            out.append(
                f"- {arrow} `{item['memory_id']}` {item['relation']}{retired} — {item['summary']}"
            )
        if len(stubs) > _STUB_LIMIT:
            out.append(f"- …and {len(stubs) - _STUB_LIMIT} more")
    return "\n".join(out), ids


async def inlined_prerequisite_ids(memory_id: str) -> list[str]:
    """The ids `open_memory` inlines, for the caller that logs telemetry.

    Recomputed from the same edge query under the same bound rather than parsed
    back out of the rendered text, so the two cannot drift into disagreeing
    about what the model was actually shown.
    """
    from . import memory_graph

    memory_id = (memory_id or "").strip().split("memory:", 1)[-1]
    if not memory_id:
        return []
    try:
        edges = await memory_graph.edges_from(
            memory_id, memory_graph.POLICY_PREREQUISITE,
        )
    except Exception as e:
        log.warning(f"Prerequisite lookup failed for {memory_id}: {e}")
        return []
    return [edge["to"] for edge in edges if edge.get("to")][:_INLINE_PREREQUISITES]


async def _neighbourhood(memory_id: str) -> tuple[list[dict], list[dict]]:
    """(prerequisites, everything else) around one memory, both directions.

    Outgoing prerequisites are what this memory rests on. Everything else —
    companions, provenance, contradictions, and every incoming edge — is
    navigation: it tells the agent what exists next door without paying to
    bring it along.
    """
    from . import consolidation, memory_graph

    try:
        outgoing = await memory_graph.edges_from(memory_id)
        incoming = await memory_graph.edges_into(memory_id)
    except Exception as e:
        log.warning(f"Neighbourhood lookup failed for {memory_id}: {e}")
        return [], []

    prerequisites, links = [], []
    for edge in outgoing:
        entry = {
            "memory_id": edge.get("to"), "relation": edge.get("relation", ""),
            "direction": "out",
        }
        if entry["relation"] in memory_graph.POLICY_PREREQUISITE:
            prerequisites.append(entry)
        elif entry["relation"] != "SUPERSEDES":
            links.append(entry)
    for edge in incoming:
        if edge.get("relation") == "SUPERSEDES":
            continue  # surfaced as the replacement banner, not as a link
        links.append({
            "memory_id": edge.get("from"), "relation": edge.get("relation", ""),
            "direction": "in",
        })

    prerequisites = [item for item in prerequisites if item["memory_id"]]
    links = [item for item in links if item["memory_id"]]
    # Prerequisites past the inline budget are rendered as stubs too, so both
    # lists need a one-liner.
    everything = prerequisites + links
    ids = [item["memory_id"] for item in everything]
    summaries = await consolidation.memory_texts(ids) if ids else {}
    # One query for the whole neighbourhood. A retired memory reached through a
    # still-live dependency edge is the quiet failure: the edge was correct when
    # written, and nothing about it says the target has since been replaced.
    retired = await memory_graph.replacements(ids) if ids else {}
    for item in everything:
        doc = summaries.get(item["memory_id"]) or {}
        item["summary"] = _one_line(doc.get("content") or "") or "(no stored text)"
        item["replaced_by"] = retired.get(item["memory_id"])
    return prerequisites, links


async def _replaced_by(memory_id: str) -> str | None:
    """The memory that supersedes this one, if any."""
    from . import memory_graph

    return (await memory_graph.replacements([memory_id])).get(memory_id)


async def _open_verbatim(drawer_id: str) -> str:
    """Fall back to the palace: a raw archived drawer, not a curated memory.

    These are the transcripts mining files away — the great majority of the
    corpus. They carry no typed edges by design: the graph relates things the
    system decided it had *learned*, and a verbatim conversation is evidence for
    a memory rather than a memory itself.
    """
    from . import palace

    try:
        # get_drawer is a synchronous backend read. Every other palace read on
        # the tool path is offloaded (tools.py runs palace_search and the KG
        # reads in an executor); doing it inline here would stall the loop for
        # the length of the query.
        drawer = await asyncio.get_running_loop().run_in_executor(
            None, palace.get_drawer, drawer_id,
        )
    except Exception as e:
        log.warning(f"Drawer lookup failed for {drawer_id}: {e}")
        return f"[memory] could not read `{drawer_id}`: {type(e).__name__}: {e}"
    if not drawer:
        return (
            f"[memory] nothing stored under `{drawer_id}`. Ids come from "
            "`memory(query=…)` or from the `id=` on a palace_search result."
        )
    text = drawer.get("text") or drawer.get("content") or ""
    meta = drawer.get("metadata") or drawer
    room = meta.get("room") or "?"
    hall = meta.get("hall") or "?"
    return (
        f"**Archived drawer `{drawer_id}`** (room={room}, hall={hall}) — verbatim "
        f"history, not a curated memory, so it has no linked context.\n\n{_body(text)}"
    )

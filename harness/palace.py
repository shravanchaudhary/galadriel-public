"""Memory palace — semantic search + verbatim conversation archival.

Storage is MongoDB / AWS DocumentDB only (``harness/mongo_palace.py``).
The ChromaDB-backed ``mempalace`` library it originally wrapped is gone: its
storage abstraction was not actually swappable (the backend registry had no
callers, and its lexical search, repair tooling and knowledge graph all reached
past the abstraction into chroma.sqlite3 / local SQLite), and every feature that
distinguished it kept state in ~15 local files that do not survive a Fargate
redeploy. Drawers and the knowledge graph now all live in the one
database that already holds the operational collections.

Lived memory uses a single `agent` wing with purpose rooms: conversations,
knowledge, procedures, episodes, preferences. Do not mine the whole repo into
the palace.

Archival helpers (`archive_conversation`, `mine_batch_dir`) are used by the
compaction hook and by `/new` to preserve verbatim content before it would
otherwise be lost. Both are fire-and-forget from callers; failures log a warning
but never propagate.

Environment:
    MONGO_URI / MONGO_DB     Required — the palace has no local-file backend.
    PALACE_ARCHIVE_ROOT      Where conversation archives are staged before
                             mining. Default ~/.mempalace/archive. Load-bearing:
                             `read_episode_segment` reads the .md files back.
    PALACE_WAKE_UP_FILE      Cached wake-up snapshot path.
                             Default ~/.mempalace/wake_up.md.
    PALACE_WAKE_UP_INJECT    Set to "0" to disable wake-up injection into
                             the dynamic system prompt block.
"""

import asyncio
import json
import logging
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from . import tower_settings

log = logging.getLogger("galadriel.palace")


def _agent_stamp() -> datetime:
    """Wall clock for agent-visible archive stamps (Configuration → Agent time)."""
    return tower_settings.agent_now()

DEFAULT_ARCHIVE_ROOT = str(Path.home() / ".mempalace" / "archive")
# Speaker-partitioned mining manifest written beside every staged batch.
SPANS_FILE = "spans.json"
DEFAULT_WAKE_UP_FILE = str(Path.home() / ".mempalace" / "wake_up.md")
DEFAULT_WING = "agent"
# Single wing for ALL agent memory — conversations, knowledge, procedures,
# episodes, preferences. The agent never chooses a wing to store or fetch;
# `DEFAULT_WING` is the one and only memory wing. Repo-wide code mining is
# not used.
CONVERSATION_ROOM = "conversations"
KNOWLEDGE_ROOM = "knowledge"
EPISODES_ROOM = "episodes"
# Studied documents (study_file): raw searchable chunks of reference material.
# Like conversations — an archive, not learned memory — and excluded from the
# wake-up digest for the same reason.
SOURCES_ROOM = "sources"
DEFAULT_DRAWER_ROOM = KNOWLEDGE_ROOM
# Legacy archive channel tags that predate channel+kind naming.
_LEGACY_ARCHIVE_KIND_PREFIXES = ("checkpoint", "compact", "max_tokens")






def _documentdb():
    from . import mongo_palace

    return mongo_palace




def _archive_root() -> Path:
    return Path(os.environ.get("PALACE_ARCHIVE_ROOT", DEFAULT_ARCHIVE_ROOT))


def _pending_shutdown_root() -> Path:
    """Conversations written at shutdown live here until mined on next start."""
    return _archive_root() / "_pending_shutdown"


def search(
    query: str = "",
    wing: str | None = None,
    room: str | None = None,
    hall: str | None = None,
    k: int = 5,
    order: str | None = None,
    channel: str | None = None,
    search_meta: dict | None = None,
) -> str:
    """Search or walk the palace. Returns markdown ready for a tool result.

    Three modes:
      - ``query`` set: hybrid vector + BM25 ranking, gated so a query with no
        real match returns "not remembered" rather than the nearest rows.
      - ``search_meta`` with no ``query``: deterministic metadata fetch in
        chunk order — how a conversation is read end to end.
      - ``order="recency"``: latest archives by ``filed_at`` DESC.

    ``search_meta`` is a dict of exact/`$in`/range filters over indexed drawer
    metadata (conversation_id, chunk_number, hall, channel, ...). Unknown keys
    are rejected with the valid list rather than silently ignored.
    """
    return _documentdb().search_markdown(
        query=query, wing=wing, room=room, hall=hall, k=k,
        order=order, channel=channel, search_meta=search_meta,
    )


_mining_shutdown_task: asyncio.Task | None = None


def schedule_mine_pending_shutdown_archives() -> None:
    """Fire-and-forget mine of shutdown-staged archives on the running loop.

    Safe to call from ``scheduler.start()`` — idempotent, never blocks startup.
    """
    global _mining_shutdown_task

    async def _run() -> None:
        try:
            n = await mine_pending_shutdown_archives()
            if n:
                log.info(f"Background shutdown archive mine complete: {n} batch(es)")
        except Exception as e:
            log.warning(f"Background shutdown archive mining failed: {e}")

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.debug("schedule_mine_pending_shutdown_archives: no running event loop")
        return

    if _mining_shutdown_task is not None and not _mining_shutdown_task.done():
        return
    _mining_shutdown_task = asyncio.ensure_future(_run())


# ─── Archival ──────────────────────────────────────────────────────

async def mine_batch_dir(
    batch_dir: Path,
    agent: str = DEFAULT_WING,
) -> bool:
    """Mine a staged batch directory into the palace. True on clean exit.
    Failures log at WARNING but never raise. Also refreshes the wake-up
    cache on success so wake-up injection tracks the current palace state.

    A conversation batch carries a `spans.json` manifest and is filed by speaker
    span (hall=user / hall=assistant) with its conversation_id and chunk
    numbering. Any other batch is chunked from its markdown.
    """
    try:
        ok = await asyncio.to_thread(_documentdb().mine_directory, batch_dir, agent=agent)
        if ok:
            asyncio.ensure_future(refresh_wake_up_cache())
        return ok
    except Exception as e:
        log.warning(f"Palace mine failed at {batch_dir}: {e}")
        return False

# Harness-generated user-role messages that must never reach the palace. They
# are boilerplate the scheduler/recall system re-emits every run — mining them
# buries real conversation under near-identical copies of the same prompt.
_SYNTHETIC_MESSAGE_KINDS = frozenset({"recall_fire", "truncation_notice"})
_SYNTHETIC_PREFIX = "[SYSTEM:"

# Speaker halls for room=conversations. Only two: everything the agent produced
# — its text, its thinking, its tool calls and the tool results it read back —
# is one `assistant` span. Splitting tool traffic out would shatter a single
# turn with a hundred tool calls into a hundred drawers.
USER_HALL = "user"
ASSISTANT_HALL = "assistant"


def _is_synthetic(msg: dict) -> bool:
    """True for harness-injected scaffolding (never archived) — both halves of
    a recall-fire tool exchange via kind, plus [SYSTEM: prompts by prefix."""
    if msg.get("kind") in _SYNTHETIC_MESSAGE_KINDS:
        return True
    content = msg.get("content")
    if isinstance(content, str):
        return content.lstrip().startswith(_SYNTHETIC_PREFIX)
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return str(block.get("text", "")).lstrip().startswith(_SYNTHETIC_PREFIX)
            # A tool_result-carrying message is agent traffic, not a synthetic prompt.
            break
    return False


def _speaker_hall(msg: dict) -> str:
    """Which hall a message's content belongs to.

    `role` is an API transport detail, not a speaker: tool_result blocks are
    delivered as role=user. What matters for recall is who produced the text, so
    a user-role message carrying tool results is filed as assistant traffic.
    """
    if msg.get("role") != "user":
        return ASSISTANT_HALL
    content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return ASSISTANT_HALL
    return USER_HALL


def _conversation_spans(messages: list[dict]) -> list[dict]:
    """Fold a message list into consecutive same-speaker spans.

    Synthetic messages are dropped first, so a scheduler prompt sitting between
    two assistant messages does not split them into two spans.
    """
    spans: list[dict] = []
    for index, msg in enumerate(messages):
        if not isinstance(msg, dict) or _is_synthetic(msg):
            continue
        hall = _speaker_hall(msg)
        text = _serialize_message(msg)
        if spans and spans[-1]["hall"] == hall:
            spans[-1]["parts"].append(text)
            spans[-1]["last_message"] = index
        else:
            spans.append({
                "hall": hall,
                "parts": [text],
                "first_message": index,
                "last_message": index,
            })
    return [
        {
            "hall": span["hall"],
            "text": "\n\n".join(span["parts"]).strip(),
            "first_message": span["first_message"],
            "last_message": span["last_message"],
        }
        for span in spans
        if "\n\n".join(span["parts"]).strip()
    ]


def _serialize_message(msg: dict) -> str:
    """Render a single API-format message as a markdown section."""
    role = msg.get("role", "?")
    content = msg.get("content")
    parts = [f"## {role}"]
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
                continue
            btype = block.get("type", "?")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "tool_use":
                parts.append(
                    f"### tool_use: {block.get('name', '?')} "
                    f"(id={block.get('id', '?')})\n\n"
                    f"```json\n{block.get('input', {})}\n```"
                )
            elif btype == "tool_result":
                result = block.get("content", "")
                if isinstance(result, list):
                    # Block-list result (text + image, e.g. screenshots) —
                    # keep the text, omit image payloads.
                    result = "\n".join(
                        "[image block — omitted]"
                        if isinstance(b, dict) and b.get("type") == "image"
                        else str(b.get("text", "")) if isinstance(b, dict) else str(b)
                        for b in result
                    )
                parts.append(
                    f"### tool_result (id={block.get('tool_use_id', '?')})\n\n"
                    f"{result}"
                )
            elif btype == "image":
                parts.append("[image block — omitted]")
            else:
                parts.append(f"[{btype} block]\n\n{block}")
    else:
        parts.append(str(content))
    return "\n\n".join(parts)


async def archive_conversation(
    channel_id: str,
    messages: list[dict],
    *,
    kind: str = "full",
    conversation_id: str | None = None,
) -> None:
    """Archive a full conversation to the palace before it's wiped (`/new`).

    Writes one batch per conversation slice. ``kind`` records why the archive
    was created (full / checkpoint / compact / max_tokens) while preserving the
    real ``channel_id`` for recall.
    """
    if not messages:
        return

    batch_dir = _write_conversation_batch(
        _archive_root(), channel_id, messages, kind=kind,
        conversation_id=conversation_id,
    )
    if batch_dir is None:
        return

    # The batch's spans manifest files the conversation verbatim into
    # wing=agent / room=conversations, one drawer per speaker span.
    ok = await mine_batch_dir(batch_dir, agent="new-clear")
    if ok:
        log.info(
            f"Palace conversation archive: channel={channel_id} kind={kind} "
            f"messages={len(messages)} dir={batch_dir}"
        )


def archive_conversation_durable(
    channel_id: str,
    messages: list[dict],
    *,
    kind: str = "full",
    conversation_id: str | None = None,
) -> Path | None:
    """Synchronously stage a full conversation to the archive root and return the
    batch dir, leaving the (slow) mine to the caller in the background.

    Used by compaction: the raw conversation MUST be durably on disk before the
    history is wiped, but a 90s mine must not block the interactive turn. The
    caller schedules `mine_batch_dir(batch_dir, ...)` as a background task.
    Returns None on empty input / write error.
    """
    return _write_conversation_batch(
        _archive_root(), channel_id, messages, kind=kind,
        conversation_id=conversation_id,
    )


def write_slack_observation_batch(observations: list[dict]) -> Path | None:
    """Durably stage exact Slack observations for one bounded Palace mine."""
    if not observations:
        return None
    ts = _agent_stamp().strftime("%Y-%m-%dT%H-%M-%S")
    batch_dir = _archive_root() / f"slack_observations_{ts}_{uuid.uuid4().hex[:8]}"
    target_dir = batch_dir / CONVERSATION_ROOM
    try:
        target_dir.mkdir(parents=True, exist_ok=False)
        sections = [
            "# Slack channel observations",
            "",
            f"- staged: {_agent_stamp().isoformat()}",
            f"- observation count: {len(observations)}",
            f"- wing: {DEFAULT_WING}",
            f"- room: {CONVERSATION_ROOM}",
            "",
            "---",
            "",
        ]
        for row in observations:
            observed = row.get("observed_at")
            if isinstance(observed, datetime):
                observed = observed.astimezone().isoformat()
            sender = row.get("sender_display_name") or row.get("sender_id") or "unknown"
            sections.extend([
                f"## {sender} — Slack {row.get('message_ts', '?')}",
                "",
                f"- workspace: {row.get('workspace_id', '?')}",
                f"- channel: {row.get('channel_id', '?')}",
                f"- sender_id: {row.get('sender_id') or '?'}",
                f"- observed_at: {observed or '?'}",
                f"- slack_ts: {row.get('message_ts', '?')}",
                f"- thread_ts: {row.get('thread_ts') or '(none)'}",
                f"- revision: {row.get('revision', 1)}",
                f"- tombstone: {bool(row.get('tombstone'))}",
                "",
                "### Current authoritative state",
                "",
                (
                    "[DELETED — this tombstone supersedes every earlier revision; "
                    "do not recall prior text as current.]"
                    if row.get("tombstone")
                    else (
                        f"[REVISION {row.get('revision', 1)} IS CURRENT — it "
                        "supersedes every earlier revision below.]\n\n"
                        + str(row.get("text") or "")
                    )
                ),
                "",
            ])
            revisions = row.get("revisions") or []
            if len(revisions) > 1:
                sections.extend([
                    "### Superseded revision history",
                    "",
                    "Historical evidence only. Never treat these entries as current.",
                    "",
                ])
                for index, revision in enumerate(revisions, 1):
                    sections.extend([
                        f"#### Revision {index} — {revision.get('kind', 'message')}",
                        f"- tombstone: {bool(revision.get('tombstone'))}",
                        f"- event_id: {revision.get('event_id') or '?'}",
                        "",
                        str(revision.get("text") or ""),
                        "",
                    ])
        (target_dir / "slack_observations.md").write_text(
            "\n".join(sections), encoding="utf-8"
        )
        # Slack observations are things people said, so they are `hall=user`
        # like any other human turn. Without a manifest the miner would fall
        # back to markdown chunking and file them as `hall=general`, putting
        # untyped drawers back into room=conversations.
        (batch_dir / SPANS_FILE).write_text(
            json.dumps({
                "channel_id": observations[0].get("channel_id"),
                "conversation_id": f"slack:{observations[0].get('channel_id')}",
                "archive_kind": "slack_observations",
                "archived_at": _agent_stamp().isoformat(),
                "room": CONVERSATION_ROOM,
                "spans": [{"hall": USER_HALL, "text": "\n".join(sections)}],
            }, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        log.warning("Slack observation archive write failed: %s", exc)
        return None
    return batch_dir


def _safe_channel(channel_id: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(channel_id))


def _safe_archive_kind(kind: str | None) -> str:
    safe = _safe_channel(kind or "full")
    return safe or "full"


def _write_conversation_batch(
    root: Path,
    channel_id: str,
    messages: list[dict],
    *,
    kind: str = "full",
    conversation_id: str | None = None,
) -> Path | None:
    """Stage a conversation as one batch dir under `root`. Pure file I/O (no
    subprocess) so it is safe from a signal handler. Returns the batch dir, or
    None when there is nothing archivable / on write error.

    Writes two things:

      - ``conversations/<name>.md`` — the verbatim transcript, unchanged in shape
        because `read_episode_segment` globs exactly this path and the agent
        reads it back as prose.
      - ``spans.json`` — the speaker-partitioned view the miner actually files:
        one entry per span with its hall. Chunk numbers are assigned by the
        miner, which is the only layer that knows how many chunks a span makes. It has to be
        on disk rather than in memory because staging and mining are separated by
        a crash boundary (the outbox drains staged batches after a restart).

    Filename/metadata keep the real channel and a separate archive kind so
    ``channel=main`` recency searches see checkpoint/compact/recovery archives.
    """
    if not messages:
        return None
    spans = _conversation_spans(messages)
    if not spans:
        # Everything in the slice was harness scaffolding. Staging an empty batch
        # would leave an un-minable dir for the outbox to retry forever.
        return None
    ts = _agent_stamp().strftime("%Y-%m-%dT%H-%M-%S")
    safe_channel = _safe_channel(channel_id)
    safe_kind = _safe_archive_kind(kind)
    # Second-resolution stamps collide when two archives of the same channel and
    # kind land in the same second (a compaction immediately followed by a
    # checkpoint). Same dir name means the same source_file, and mining purges
    # by source_file before inserting — so the first archive's drawers would be
    # deleted by the second. The suffix makes each batch its own identity, as
    # the Slack observation batches already do.
    suffix = uuid.uuid4().hex[:8]
    batch_dir = root / f"conversation_{safe_channel}_{safe_kind}_{ts}_{suffix}"
    try:
        conv_dir = batch_dir / CONVERSATION_ROOM
        conv_dir.mkdir(parents=True, exist_ok=True)
        sections = [
            f"# Conversation archive — channel {channel_id}\n",
            f"- archived: {ts}",
            f"- channel: {channel_id}",
            f"- archive_kind: {safe_kind}",
            f"- conversation_id: {conversation_id or '(none)'}",
            f"- message count: {len(messages)}\n",
            "---\n",
        ]
        for i, msg in enumerate(messages):
            # Same filter as spans: a crash between this write and the
            # manifest makes _spans_from_markdown rebuild from THIS file, and
            # its prefix heuristics cannot recognise headerless tool-exchange
            # fire text — so machine scaffolding must not be here either.
            if _is_synthetic(msg):
                continue
            sections.append(f"<!-- message {i} -->")
            sections.append(_serialize_message(msg))
            sections.append("")
        (conv_dir / f"conversation_{safe_channel}_{safe_kind}_{ts}_{suffix}.md").write_text(
            "\n".join(sections), encoding="utf-8"
        )
        (batch_dir / SPANS_FILE).write_text(
            json.dumps({
                "channel_id": channel_id,
                "conversation_id": conversation_id,
                "archive_kind": safe_kind,
                "archived_at": _agent_stamp().isoformat(),
                "room": CONVERSATION_ROOM,
                "spans": spans,
            }, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        log.warning(f"Palace conversation archive write failed: {e}")
        return None
    return batch_dir


def write_conversation_archive_sync(
    channel_id: str,
    messages: list[dict],
    *,
    kind: str = "full",
    conversation_id: str | None = None,
) -> Path | None:
    """Synchronously stage a conversation for archival without mining.

    Safe to call from a signal handler / atexit at shutdown: it only does a file
    write, no embedding or DB round-trip that could outlive the shutdown grace
    window. The staged dir is mined into the palace — and then deleted — on the
    next startup via mine_pending_shutdown_archives().
    """
    return _write_conversation_batch(
        _pending_shutdown_root(), channel_id, messages, kind=kind,
        conversation_id=conversation_id,
    )


async def mine_pending_shutdown_archives() -> int:
    """Mine conversations staged at the previous shutdown, then delete each.

    Run once on startup. Each staged dir is mined exactly like the /new archive
    path; on a clean mine the raw .md dir is removed. Dirs that fail to mine are
    left in place and retried on the next startup. Returns the count mined.
    """
    root = _pending_shutdown_root()
    if not root.exists():
        return 0
    mined = 0
    for batch_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        ok = await mine_batch_dir(batch_dir, agent="shutdown")
        if ok:
            shutil.rmtree(batch_dir, ignore_errors=True)
            mined += 1
            log.info(f"Mined pending shutdown archive: {batch_dir}")
    return mined


def close() -> None:
    """Release the palace's database client. Best-effort; never raises.

    Kept as a shutdown hook because callers (atexit, SIGTERM) still invoke it.
    There is no on-disk index to flush any more — the previous storage engine
    needed this to stop index segments being quarantined on the next start.
    """
    try:
        _documentdb().close()
    except Exception as e:
        log.debug(f"Palace close skipped: {e}")


async def archive_daily_logs(memory_dir: str = "memory") -> None:
    """Deprecated no-op.

    Truncated daily logs stay as the hot dynamic index only. Durable end-of-day
    narrative is filed explicitly to ``room=episodes`` by the goodnight prompt.
    Kept as a stub so older callers/imports do not break.
    """
    log.info(
        "archive_daily_logs skipped — daily markdown is an index only; "
        f"requested dir={memory_dir}"
    )


# ─── Wake-up injection ─────────────────────────────────────────────

def _wake_up_file() -> Path:
    return Path(os.environ.get("PALACE_WAKE_UP_FILE", DEFAULT_WAKE_UP_FILE))


def read_wake_up_text() -> str:
    """Return cached wake-up content (empty string if missing/empty/stale).

    Called by MemoryManager.build_dynamic_text() on every API call. Must
    stay cheap — just a file read, no embedder load.
    """
    path = _wake_up_file()
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except Exception as e:
        log.warning(f"Wake-up read failed: {e}")
        return ""
    # Strip interactive header lines if present
    if text.startswith("Wake-up text"):
        text = text.split("\n", 1)[-1].lstrip("=").lstrip()
    return text


# ─── Write tools ───────────────────────────────────────────────────

def _slug(text: str, limit: int = 40) -> str:
    """Short filesystem-safe slug from arbitrary text."""
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in text.lower())
    safe = safe.strip("-")
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe[:limit] or "drawer"


async def add_drawer(
    content: str,
    topic: str | None = None,
    wing: str = DEFAULT_WING,
    room: str | None = None,
    drawer_id: str | None = None,
) -> str:
    """File a verbatim drawer into the palace immediately.

    Called by the learning pipeline's commit helpers
    (harness/consolidation.py `_commit_*`) — there is no direct agent-facing
    tool for raw drawer writes; everything arrives typed through `learn` /
    `propose_memory`. Returns a human-readable status string.

    Defaults to ``room=knowledge`` for durable facts.

    ``drawer_id`` names the drawer instead of letting one be minted. The
    learning pipeline passes its ``memory_id`` so a curated memory and its
    drawer share one identity: a palace hit then carries the id the graph,
    telemetry and `memory()` all use, with nothing to reconcile.
    """
    if not content or not content.strip():
        return "[palace add] empty content — nothing filed."
    resolved_room = room or DEFAULT_DRAWER_ROOM
    try:
        # Embedding plus a synchronous driver write — both blocking. This runs
        # on the agent's own turn (and on every consolidation commit), so it has
        # to go to a thread or it stalls the event loop mid-conversation.
        await asyncio.to_thread(
            lambda: _documentdb().upsert_drawer(
                content,
                drawer_id=drawer_id,
                wing=wing,
                room=resolved_room,
                hall=topic or "general",
                topic=topic,
                source_file="agent:add",
            )
        )
        await refresh_wake_up_cache()
        return (
            f"Filed to palace: wing=`{wing}`, room=`{resolved_room}`"
            + (f", topic=`{topic}`" if topic else "")
            + (f", id=`{drawer_id}`" if drawer_id else "")
        )
    except Exception as e:
        return f"[palace add] {type(e).__name__}: {e}"

async def study_document(
    text: str, *, source_path: str, hall: str, part: int = 1,
    total_parts: int | None = None,
) -> int:
    """Chunk one part of a document into room=sources (see mongo_palace.study_text).

    Embedding plus sync driver writes — off the event loop, like add_drawer.
    Returns the number of chunks filed. No wake-up refresh: sources are
    excluded from the digest by design.
    """
    return await asyncio.to_thread(
        lambda: _documentdb().study_text(
            text, source_file=source_path, hall=hall, room=SOURCES_ROOM,
            part=part, total_parts=total_parts,
        )
    )


async def wake_up() -> str:
    """Fetch a fresh wake-up snapshot (on-demand tool).

    Different from the always-on dynamic-block injection: this recomputes the
    snapshot *right now*, so the agent can pull a fresh palace overview
    mid-conversation.
    """
    try:
        return await asyncio.to_thread(_documentdb().wake_up_text)
    except Exception as e:
        return f"[palace wake-up] {type(e).__name__}: {e}"

async def refresh_wake_up_cache() -> bool:
    """Regenerate the wake-up cache file. Silent on failure.

    Typically called at the end of mine_batch_dir() so the cache tracks the
    current palace state.
    """
    out_path = _wake_up_file()
    try:
        text = await asyncio.to_thread(_documentdb().wake_up_text)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        return True
    except Exception as e:
        log.warning(f"Mongo-compatible wake-up refresh failed: {e}")
        return False
def kg_add(
    subject: str,
    predicate: str,
    object: str,
    valid_from: str | None = None,
) -> str:
    """File a temporal entity-relationship fact into the palace KG.

    Subject/object are entity names (free text). Predicate is the
    relationship (e.g., 'is', 'works_on', 'lives_in', 'prefers').
    valid_from is an ISO date (defaults to today) — facts can later be
    invalidated to mark when they stopped being true.
    """
    if not subject or not predicate or not object:
        return "[kg add] subject, predicate, and object are required."
    try:
        return _documentdb().kg_add(subject, predicate, object, valid_from)
    except Exception as e:
        return f"[kg add] {type(e).__name__}: {e}"
def _fmt_triple(r: dict) -> str:
    """Render one KG row as a markdown line."""
    s = r.get("subject", "?")
    p = r.get("predicate", "?")
    o = r.get("object") or r.get("obj") or "?"
    vf = r.get("valid_from") or ""
    vt = r.get("valid_to") or ""
    status = f"[ended {vt}]" if vt else "[current]"
    when = f" (from {vf})" if vf else ""
    return f"- `{s}` --[{p}]-> `{o}`{when}  {status}"


def kg_query(
    subject: str | None = None,
    predicate: str | None = None,
    object: str | None = None,
) -> str:
    """Query the KG. Any of S/P/O may be given.

    Each given field is a case-insensitive substring filter (`job_loc` finds
    `job_location`). Exact equality stays with `kg_query_rows`, behind dedupe
    and invalidate, where `Memorang` must not match `memorang`.
    """
    if not subject and not predicate and not object:
        return "[kg query] give at least one of subject, predicate, object."
    try:
        rows = _documentdb().kg_search_rows(subject=subject, predicate=predicate, object=object)
    except Exception as e:
        return f"[kg query] {type(e).__name__}: {e}"
    if not rows:
        return "No KG facts matched the requested filters."
    return "\n".join([f"**KG query** ({len(rows)} fact(s)):", "", *(_fmt_triple(row) for row in rows)])
def kg_invalidate(subject: str, predicate: str, object: str, ended: str | None = None) -> str:
    """Mark a KG fact as no longer valid (sets valid_to date)."""
    try:
        return _documentdb().kg_invalidate(subject, predicate, object, ended)
    except Exception as e:
        return f"[kg invalidate] {type(e).__name__}: {e}"
def kg_fact_is_current(subject: str, predicate: str, object: str) -> bool:
    """True when this exact triple has an open (valid_to=None) row.

    The commit pipeline's add-dedupe check: expired rows deliberately do NOT
    count — an invalidated fact is re-addable, which is what lets a fact
    change and later revert.
    """
    return bool(_documentdb().kg_query_rows(
        subject=subject, predicate=predicate, object=object,
        limit=1, current_only=True,
    ))
def kg_timeline(entity: str) -> str:
    """Return chronological history of all facts touching an entity."""
    try:
        outgoing = _documentdb().kg_query_rows(subject=entity)
        incoming = _documentdb().kg_query_rows(object=entity)
        facts = outgoing + [row for row in incoming if row not in outgoing]
    except Exception as e:
        return f"[kg timeline] {type(e).__name__}: {e}"

    if not facts:
        return f"No KG history for `{entity}`."
    lines = [f"**KG timeline for `{entity}`** ({len(facts)} fact(s)):", ""]
    for f in facts:
        s_ = f.get("subject", "?")
        p_ = f.get("predicate", "?")
        o_ = f.get("object") or f.get("obj") or "?"
        vf = f.get("valid_from") or "?"
        vt = f.get("valid_to") or "current"
        lines.append(f"- {vf} → {vt}: `{s_}` --[{p_}]-> `{o_}`")
    return "\n".join(lines)


def taxonomy() -> str:
    """Return wing → room breakdown with drawer counts.

    Used by the agent to discover how memory is organized before narrowing
    a search. Aggregates via SQLite so large palaces stay readable.
    """
    data = _documentdb().taxonomy_data()
    total = data.get("total", 0)
    if total == 0:
        return "Palace is empty."
    lines = [f"**Palace taxonomy** — {total} drawers total", ""]
    for wing in sorted(data.get("wings") or {}):
        rooms = data["wings"][wing]
        lines.append(f"- **Wing `{wing}`** ({sum(rooms.values())} drawer(s)):")
        for room, count in sorted(rooms.items(), key=lambda item: -item[1]):
            lines.append(f"    - room `{room}`: {count}")
    return "\n".join(lines)

def search_data(query: str, wing: str | None = None, room: str | None = None,
                 hall: str | None = None, k: int = 20,
                 search_meta: dict | None = None) -> list[dict]:
    """Structured semantic-search results for the Tower palace search UI.

    Deliberately a standalone sibling of search() above rather than a shared
    refactor of it — same retrieval logic, but returns drawer dicts instead
    of a pre-formatted markdown blob, and never touches the agent-tool-facing
    search() function.
    """
    return _documentdb().search_data(query, wing=wing, room=room, hall=hall, k=k,
                                     search_meta=search_meta)


def segment_text(segment_id: str) -> str:
    """Verbatim text of one archived segment, read from the database."""
    return _documentdb().segment_text(segment_id)


def taxonomy_data() -> dict:
    """Structured wing → room → count + hall counts, for the Tower taxonomy
    view. Same underlying data as taxonomy(), returned as a dict instead of
    pre-formatted markdown."""
    return _documentdb().taxonomy_data()
def _drawer_from_row(drawer_id: str, doc: str, metadata: dict | None) -> dict:
    md = metadata or {}
    return {
        "id": drawer_id,
        "text": doc or "",
        "wing": md.get("wing", "?"),
        "room": md.get("room", "?"),
        "hall": md.get("hall", "?"),
        "source_file": md.get("source_file", ""),
        "metadata": md,
    }


def list_drawers(
    wing: str | None = None,
    room: str | None = None,
    hall: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Paginated, filterable drawer listing for the Tower palace browser.

    Returns {"total": <matching count>, "drawers": [...]}. Projected and paged
    at the query so large rooms never load every drawer (and never its
    embedding) to render one page.
    """
    return _documentdb().list_drawers(wing, room, hall, limit, offset)
def get_drawer(drawer_id: str) -> dict | None:
    """Fetch a single drawer by id, or None if it doesn't exist."""
    return _documentdb().get_drawer(drawer_id)
def update_drawer(
    drawer_id: str,
    text: str | None = None,
    wing: str | None = None,
    room: str | None = None,
    hall: str | None = None,
) -> str:
    """Edit a single drawer in place — the human-editing counterpart to the
    agent's append-only palace tools.

    When `text` changes the drawer's embedding is recomputed for that one row.
    Metadata-only edits (wing/room/hall) skip the embedder entirely.
    """
    return _documentdb().update_drawer(drawer_id, text, wing, room, hall)
def delete_drawer(drawer_id: str) -> str:
    """Remove one drawer. Call reconcile_sync() afterward to refresh the
    wake-up cache."""
    return _documentdb().delete_drawer(drawer_id)
def reconcile_sync() -> dict:
    """Post-edit housekeeping for Tower's direct drawer mutations.

    Writes are already durable when they return, so this only regenerates the
    wake-up cache so the agent's dynamic prompt block matches the current
    palace state. Returns {"ok": bool, "steps": [str, ...]}.
    """
    try:
        text = _documentdb().wake_up_text()
        out_path = _wake_up_file()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        return {"ok": True, "steps": ["writes durable", "wake-up cache refreshed"]}
    except Exception as e:
        return {"ok": False, "steps": [f"reconcile failed: {type(e).__name__}: {e}"]}

def kg_list(limit: int = 200) -> list[dict]:
    """Listing of ALL KG triples for the Tower KG browser.

    kg_query() always requires at least one of subject/predicate/object; this is
    the unfiltered read behind the browser's table view.
    """
    try:
        return _documentdb().kg_query_rows(limit=limit)
    except Exception as e:
        log.warning(f"KG list failed: {e}")
        return []


def kg_search(query: str, limit: int = 200) -> list[dict]:
    """Tower KG browser filter: case-insensitive substring across subject,
    predicate, and object. Replaces the timeline lookup as the page's search,
    which only matched an exact subject or object, so a predicate like
    `job_location` found nothing."""
    try:
        return _documentdb().kg_search_rows(query=query, limit=limit)
    except Exception as e:
        log.warning(f"KG search failed: {e}")
        return []

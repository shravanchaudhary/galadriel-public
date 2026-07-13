"""MemPalace integration — semantic search + verbatim archival.

Thin wrapper around `mempalace.searcher.search_memories`. The palace itself
lives at `MEMPALACE_PATH` (default `~/.mempalace/palace`). Lived memory uses a
single `agent` wing with purpose rooms: conversations, knowledge, episodes,
diary. Do not mine the whole repo into the palace.

Imports of `mempalace` are deferred until first call so cold harness startup
does not pay ChromaDB + onnxruntime load cost when no palace tool is invoked.

Archival helpers (`archive_conversation`, `mine_batch_dir`) are used by
the compaction hook and by `/new` to preserve verbatim content before it
would otherwise be lost. Both are fire-and-forget from callers; failures
log a warning but never propagate.

Environment overrides:
    MEMPALACE_PATH           Palace directory (default ~/.mempalace/palace).
                             Read by the mempalace library itself.
    PALACE_ARCHIVE_ROOT      Where conversation/tool_result archives land.
                             Default ~/.mempalace/archive.
    PALACE_WAKE_UP_FILE      Cached wake-up snapshot path.
                             Default ~/.mempalace/wake_up.md.
    PALACE_WAKE_UP_INJECT    Set to "0" to disable wake-up injection into
                             the dynamic system prompt block.
"""

import asyncio
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

log = logging.getLogger("galadriel.palace")

DEFAULT_PALACE_PATH = str(Path.home() / ".mempalace" / "palace")
DEFAULT_ARCHIVE_ROOT = str(Path.home() / ".mempalace" / "archive")
DEFAULT_WAKE_UP_FILE = str(Path.home() / ".mempalace" / "wake_up.md")
DEFAULT_WING = "agent"
# Single wing for ALL agent memory — conversations, knowledge, episodes, and
# diary. The agent never chooses a wing to store or fetch; `DEFAULT_WING` is
# the one and only memory wing. Repo-wide code mining is not used.
CONVERSATION_ROOM = "conversations"
KNOWLEDGE_ROOM = "knowledge"
EPISODES_ROOM = "episodes"
DIARY_ROOM = "diary"
DEFAULT_DRAWER_ROOM = KNOWLEDGE_ROOM
# Legacy archive channel tags that predate channel+kind naming.
_LEGACY_ARCHIVE_KIND_PREFIXES = ("checkpoint", "compact", "max_tokens")
MINE_TIMEOUT_SEC = 90
WAKE_UP_TIMEOUT_SEC = 30

# Per-batch palace config dropped beside every archived conversation so a plain
# (projects-mode) mine routes the whole verbatim conversation to one wing/room.
# This replaces the old `--mode convos --extract general` path, whose LLM
# classifier sprayed a single session across emotional/decision/etc. rooms.
_CONVERSATION_PALACE_YAML = (
    f"wing: {DEFAULT_WING}\n"
    "rooms:\n"
    f"- name: {CONVERSATION_ROOM}\n"
    "  description: Verbatim chat history\n"
    "  keywords:\n"
    "  - conversations\n"
    "  - conversation\n"
    "- name: general\n"
    "  description: Fallback\n"
    "  keywords: []\n"
)

# Resolve the mempalace CLI from the same venv as the running Python, so
# subprocess calls do not silently break if PATH is not set (test harnesses,
# bare shells, cron contexts).
MEMPALACE_BIN = str(Path(sys.executable).parent / "mempalace")


def _palace_path() -> str:
    return os.environ.get("MEMPALACE_PATH", DEFAULT_PALACE_PATH)


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
) -> str:
    """Search the palace. Returns markdown ready for a tool result.

    order:
      - None / ``semantic`` (default): hybrid vector + BM25 ranking on ``query``.
      - ``recency``: latest distinct archive sessions by ``filed_at`` DESC.
        ``query`` is optional — when set, only sessions containing that text
        are considered. ``channel`` optionally filters to that real channel
        (e.g. ``main``), matching both new ``conversation_{channel}_{kind}_*``
        archives and legacy ``conversation_{kind}_{channel}_*`` names.

    Filters (both modes):
      - wing, room, hall: metadata scoping.
    """
    path = _palace_path()
    if not os.path.isdir(path):
        return f"[palace unavailable] no palace at {path} — run `mempalace init` + `mine` first"

    want = max(1, min(k, 20))

    if order == "recency":
        return _format_recent_results(
            _recent_sessions(
                wing=wing, room=room, hall=hall, k=want,
                channel=channel, query=query or None,
            ),
            wing=wing, room=room, hall=hall, channel=channel, k=want,
        )

    if not (query or "").strip():
        return "[palace_search] query is required for semantic search (use order=`recency` for latest sessions)"

    if hall:
        # Direct chromadb path: native where filter on hall
        try:
            from mempalace.backends.chroma import ChromaBackend
        except ImportError as e:
            return f"[palace unavailable] mempalace not installed: {e}"
        try:
            backend = ChromaBackend()
            coll = backend.get_collection(path, "mempalace_drawers")
            where: dict = {"hall": hall}
            if wing: where = {"$and": [where, {"wing": wing}]}
            if room: where = {"$and": [where if isinstance(where, dict) else where, {"room": room}]}
            res = coll._collection.query(
                query_texts=[query], n_results=want, where=where,
            )
            drawers = []
            for i, doc in enumerate((res.get("documents") or [[]])[0]):
                md = (res.get("metadatas") or [[]])[0][i] if res.get("metadatas") else {}
                dist = (res.get("distances") or [[]])[0][i] if res.get("distances") else None
                drawers.append({
                    "text": doc,
                    "wing": (md or {}).get("wing", "?"),
                    "room": (md or {}).get("room", "?"),
                    "hall": (md or {}).get("hall", "?"),
                    "source_file": (md or {}).get("source_file", ""),
                    "distance": dist,
                })
        except Exception as e:
            return f"[palace error] {type(e).__name__}: {e}"
    else:
        try:
            from mempalace.searcher import search_memories
        except ImportError as e:
            return f"[palace unavailable] mempalace not installed: {e}"
        try:
            result = search_memories(
                query=query, palace_path=path, wing=wing, room=room,
                n_results=want,
            )
        except Exception as e:
            return f"[palace error] {type(e).__name__}: {e}"
        drawers = result.get("results") or result.get("drawers") or []

    if not drawers:
        filters = []
        if wing: filters.append(f"wing=`{wing}`")
        if room: filters.append(f"room=`{room}`")
        if hall: filters.append(f"hall=`{hall}`")
        filter_str = f" [{', '.join(filters)}]" if filters else ""
        return f"No drawers matched `{query}`{filter_str}"

    header_bits = [f"`{query}`"]
    if wing: header_bits.append(f"wing=`{wing}`")
    if room: header_bits.append(f"room=`{room}`")
    if hall: header_bits.append(f"hall=`{hall}`")
    lines = [f"**Palace search:** " + " ".join(header_bits), ""]
    for i, d in enumerate(drawers, 1):
        wing_name = d.get("wing", "?")
        room_name = d.get("room", "?")
        hall_name = d.get("hall", "?")
        distance = d.get("distance")
        content = (d.get("content") or d.get("text") or "").strip()
        header = f"### {i}. {wing_name} / {room_name} / hall={hall_name}"
        if distance is not None:
            header += f"  _(d={distance:.3f})_"
        lines.append(header)
        lines.append(content)
        lines.append("")
    return "\n".join(lines).rstrip()


def _chroma_sqlite_path() -> Path:
    return Path(_palace_path()) / "chroma.sqlite3"


def _recent_sessions(
    *,
    wing: str | None = None,
    room: str | None = None,
    hall: str | None = None,
    k: int = 5,
    channel: str | None = None,
    query: str | None = None,
) -> list[dict]:
    """Latest *k* distinct source_file archives, newest ``filed_at`` first."""
    db = _chroma_sqlite_path()
    if not db.is_file():
        return []

    filters = ["c.name = 'mempalace_drawers'"]
    params: list = []

    for key, val in (("wing", wing), ("room", room), ("hall", hall)):
        if val:
            filters.append(
                "EXISTS (SELECT 1 FROM embedding_metadata em_x "
                "WHERE em_x.id = e.id AND em_x.key = ? AND em_x.string_value = ?)"
            )
            params.extend([key, val])

    if channel:
        safe = _safe_channel(channel)
        # New archives: conversation_{channel}_{kind}_*
        # Legacy archives: conversation_{kind}_{channel}_* and conversation_{channel}_*
        channel_clauses = ["em_sf.string_value LIKE ?"]
        params.append(f"%conversation_{safe}_%")
        for kind in _LEGACY_ARCHIVE_KIND_PREFIXES:
            channel_clauses.append("em_sf.string_value LIKE ?")
            params.append(f"%conversation_{kind}_{safe}_%")
        filters.append("(" + " OR ".join(channel_clauses) + ")")

    if query:
        filters.append(
            "EXISTS (SELECT 1 FROM embedding_metadata em_q "
            "WHERE em_q.id = e.id AND em_q.key = 'chroma:document' "
            "AND em_q.string_value LIKE ?)"
        )
        params.append(f"%{query}%")

    where_sql = " AND ".join(filters)
    sql = f"""
        SELECT em_sf.string_value AS source_file,
               MAX(em_f.string_value) AS filed_at
        FROM embeddings e
        JOIN segments s ON e.segment_id = s.id
        JOIN collections c ON s.collection = c.id
        JOIN embedding_metadata em_sf ON em_sf.id = e.id AND em_sf.key = 'source_file'
        JOIN embedding_metadata em_f ON em_f.id = e.id AND em_f.key = 'filed_at'
        WHERE {where_sql}
        GROUP BY em_sf.string_value
        ORDER BY filed_at DESC
        LIMIT ?
    """
    params.append(k)

    sessions: list[dict] = []
    try:
        conn = sqlite3.connect(db)
        cur = conn.cursor()
        for source_file, filed_at in cur.execute(sql, params).fetchall():
            preview = _preview_for_source_file(conn, source_file, query=query)
            sessions.append({
                "source_file": source_file,
                "filed_at": filed_at or "?",
                "text": preview.get("text", ""),
                "wing": preview.get("wing", "?"),
                "room": preview.get("room", "?"),
                "hall": preview.get("hall", "?"),
            })
        conn.close()
    except Exception as e:
        log.warning(f"Palace recency query failed: {e}")
        return []
    return sessions


def _preview_for_source_file(
    conn: sqlite3.Connection,
    source_file: str,
    query: str | None = None,
) -> dict:
    """Best preview chunk for a source_file archive.

    When ``query`` is set, prefer a chunk whose document contains it;
    otherwise return the first chunk (lowest chunk_index).
    """
    if query:
        row = conn.execute(
            """
            SELECT em_doc.string_value,
                   em_w.string_value,
                   em_r.string_value,
                   em_h.string_value
            FROM embeddings e
            JOIN embedding_metadata em_sf ON em_sf.id = e.id
                AND em_sf.key = 'source_file' AND em_sf.string_value = ?
            JOIN embedding_metadata em_doc ON em_doc.id = e.id
                AND em_doc.key = 'chroma:document'
            LEFT JOIN embedding_metadata em_w ON em_w.id = e.id AND em_w.key = 'wing'
            LEFT JOIN embedding_metadata em_r ON em_r.id = e.id AND em_r.key = 'room'
            LEFT JOIN embedding_metadata em_h ON em_h.id = e.id AND em_h.key = 'hall'
            WHERE em_doc.string_value LIKE ?
            ORDER BY LENGTH(em_doc.string_value) ASC
            LIMIT 1
            """,
            (source_file, f"%{query}%"),
        ).fetchone()
        if row:
            text, wing, room, hall = row
            return {
                "text": (text or "").strip(),
                "wing": wing or "?",
                "room": room or "?",
                "hall": hall or "?",
            }

    row = conn.execute(
        """
        SELECT em_doc.string_value,
               em_w.string_value,
               em_r.string_value,
               em_h.string_value
        FROM embeddings e
        JOIN embedding_metadata em_sf ON em_sf.id = e.id
            AND em_sf.key = 'source_file' AND em_sf.string_value = ?
        JOIN embedding_metadata em_doc ON em_doc.id = e.id
            AND em_doc.key = 'chroma:document'
        LEFT JOIN embedding_metadata em_ci ON em_ci.id = e.id
            AND em_ci.key = 'chunk_index'
        LEFT JOIN embedding_metadata em_w ON em_w.id = e.id AND em_w.key = 'wing'
        LEFT JOIN embedding_metadata em_r ON em_r.id = e.id AND em_r.key = 'room'
        LEFT JOIN embedding_metadata em_h ON em_h.id = e.id AND em_h.key = 'hall'
        ORDER BY COALESCE(em_ci.int_value, 0) ASC
        LIMIT 1
        """,
        (source_file,),
    ).fetchone()
    if not row:
        return {"text": ""}
    text, wing, room, hall = row
    return {
        "text": (text or "").strip(),
        "wing": wing or "?",
        "room": room or "?",
        "hall": hall or "?",
    }


def _format_recent_results(
    sessions: list[dict],
    *,
    wing: str | None,
    room: str | None,
    hall: str | None,
    channel: str | None,
    k: int,
) -> str:
    if not sessions:
        bits = ["order=`recency`"]
        if wing:
            bits.append(f"wing=`{wing}`")
        if room:
            bits.append(f"room=`{room}`")
        if hall:
            bits.append(f"hall=`{hall}`")
        if channel:
            bits.append(f"channel=`{channel}`")
        return f"No recent sessions matched ({', '.join(bits)})"

    header_bits = [f"order=`recency`", f"k={k}"]
    if wing:
        header_bits.append(f"wing=`{wing}`")
    if room:
        header_bits.append(f"room=`{room}`")
    if hall:
        header_bits.append(f"hall=`{hall}`")
    if channel:
        header_bits.append(f"channel=`{channel}`")

    lines = ["**Palace search (recency):** " + " ".join(header_bits), ""]
    for i, s in enumerate(sessions, 1):
        src = Path(s.get("source_file") or "").name or "?"
        filed = s.get("filed_at", "?")
        wing_name = s.get("wing", "?")
        room_name = s.get("room", "?")
        hall_name = s.get("hall", "?")
        lines.append(
            f"### {i}. {wing_name} / {room_name} / hall={hall_name}  "
            f"_(filed={filed}, source=`{src}`)_"
        )
        lines.append((s.get("text") or "").strip())
        lines.append("")
    return "\n".join(lines).rstrip()


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
    mode: str | None = None,
    extract: str | None = None,
) -> bool:
    """Run `mempalace mine` on a directory. Returns True on clean exit.
    Failures log at WARNING but never raise. Also refreshes the wake-up
    cache on success so wake-up injection tracks the current palace state.

    Conversation archives are mined in the default (projects) mode — a per-batch
    mempalace.yaml routes them verbatim into wing=agent / room=conversations.
    The `mode='convos'` + `extract='general'` path (LLM auto-classification into
    5 memory types) is no longer used by the harness: it sprayed a single chat
    session across many rooms. The args remain for ad-hoc/manual callers.
    """
    cmd = [MEMPALACE_BIN, "mine", str(batch_dir),
           "--wing", DEFAULT_WING, "--agent", agent]
    if mode:
        cmd += ["--mode", mode]
    if extract:
        cmd += ["--extract", extract]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=MINE_TIMEOUT_SEC
        )
        if proc.returncode == 0:
            # Refresh the wake-up cache so the dynamic block picks up new drawers
            asyncio.ensure_future(refresh_wake_up_cache())
            return True
        log.warning(
            f"Palace mine rc={proc.returncode} at {batch_dir}: "
            f"{stderr.decode(errors='replace')[:500]}"
        )
    except asyncio.TimeoutError:
        log.warning(f"Palace mine timed out after {MINE_TIMEOUT_SEC}s at {batch_dir}")
    except Exception as e:
        log.warning(f"Palace mine failed at {batch_dir}: {e}")
    return False


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
) -> None:
    """Archive a full conversation to the palace before it's wiped (`/new`).

    Writes a single timestamped .md file per conversation (mempalace chunks
    internally). ``kind`` records why the archive was created (full / checkpoint /
    compact / max_tokens) while preserving the real ``channel_id`` for recall.
    """
    if not messages:
        return

    batch_dir = _write_conversation_batch(
        _archive_root(), channel_id, messages, kind=kind,
    )
    if batch_dir is None:
        return

    # Plain (projects-mode) mine: the per-batch mempalace.yaml routes the whole
    # conversation verbatim into wing=agent / room=conversations — no convos-mode
    # classification spray.
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
) -> Path | None:
    """Synchronously stage a full conversation to the archive root and return the
    batch dir, leaving the (slow) `mempalace mine` to the caller in the background.

    Used by compaction: the raw conversation MUST be durably on disk before the
    history is wiped, but a 90s mine must not block the interactive turn. The
    caller schedules `mine_batch_dir(batch_dir, ...)` as a background task.
    Returns None on empty input / write error.
    """
    return _write_conversation_batch(
        _archive_root(), channel_id, messages, kind=kind,
    )


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
) -> Path | None:
    """Write a conversation as a single timestamped .md inside a fresh batch dir
    under `root`. Pure file I/O (no subprocess) so it is safe to call from a
    signal handler. Returns the batch dir, or None on empty input / write error.

    Filename/metadata keep the real channel and a separate archive kind so
    ``channel=main`` recency searches see checkpoint/compact/recovery archives.
    """
    if not messages:
        return None
    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    safe_channel = _safe_channel(channel_id)
    safe_kind = _safe_archive_kind(kind)
    batch_dir = root / f"conversation_{safe_channel}_{safe_kind}_{ts}"
    try:
        # The .md goes in a `conversations/` subfolder and a per-batch
        # mempalace.yaml sits at the batch root, so a plain mine deterministically
        # files it under wing=agent / room=conversations (detect_room Priority 1).
        conv_dir = batch_dir / CONVERSATION_ROOM
        conv_dir.mkdir(parents=True, exist_ok=True)
        (batch_dir / "mempalace.yaml").write_text(_CONVERSATION_PALACE_YAML, encoding="utf-8")
        sections = [
            f"# Conversation archive — channel {channel_id}\n",
            f"- archived: {ts}",
            f"- channel: {channel_id}",
            f"- archive_kind: {safe_kind}",
            f"- message count: {len(messages)}\n",
            "---\n",
        ]
        for i, msg in enumerate(messages):
            sections.append(f"<!-- message {i} -->")
            sections.append(_serialize_message(msg))
            sections.append("")
        (conv_dir / f"conversation_{safe_channel}_{safe_kind}_{ts}.md").write_text(
            "\n".join(sections), encoding="utf-8"
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
) -> Path | None:
    """Synchronously stage a conversation for archival without mining.

    Safe to call from a signal handler / atexit at shutdown: it only does a file
    write (no `mempalace mine` subprocess, which could outlive the shutdown
    grace window). The staged dir is mined into the palace — and then deleted —
    on the next startup via mine_pending_shutdown_archives().
    """
    return _write_conversation_batch(
        _pending_shutdown_root(), channel_id, messages, kind=kind,
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
    """Cleanly close in-process MemPalace ChromaDB handles so HNSW flushes to
    disk before the process exits. Best-effort; never raises.

    Why this matters: the backend creates collections with
    ``hnsw:sync_threshold=50_000`` (an index-bloat guard), so the handful of
    in-process VECTOR writes we make at runtime — chiefly ``diary_write`` via
    mempalace's ``mcp_server`` — do NOT flush to the on-disk HNSW segment until
    a clean ``PersistentClient.close()``. If the process is killed without that
    close, ``chroma.sqlite3`` ends up ahead of the HNSW segment and the NEXT
    start quarantines the segment as drift (``quarantine_stale_hnsw``) — those
    drawers go silently missing from vector search until reindexed. Closing the
    clients here is the fix: it forces the flush so recall stays intact across
    restarts. Call from the shutdown path (atexit / SIGTERM).
    """
    path = _palace_path()
    if not os.path.isdir(path):
        return
    # 1. Close the MCP write-path client (the diary_write path) if it is live —
    #    this is the in-process client that holds unflushed vector writes.
    try:
        from mempalace import mcp_server as _mcp
        client = getattr(_mcp, "_client_cache", None)
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
            _mcp._client_cache = None
    except Exception as e:
        log.debug(f"Palace close: mcp client close skipped ({e})")
    # 2. Close the shared search/read backend and clear chromadb's system cache
    #    (this is mempalace's own canonical clean-close primitive).
    try:
        from mempalace.palace import _DEFAULT_BACKEND
        from mempalace.repair import _close_chroma_handles
        _close_chroma_handles(path, backend=_DEFAULT_BACKEND)
        log.info("MemPalace handles closed cleanly (HNSW flushed).")
    except Exception as e:
        log.warning(f"Palace clean-close failed ({e}); HNSW may quarantine on next start.")


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
    stay cheap — just a file read, no chromadb load.
    """
    path = _wake_up_file()
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except Exception as e:
        log.warning(f"Wake-up read failed: {e}")
        return ""
    # Strip mempalace's interactive header lines if present
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
) -> str:
    """File a verbatim drawer into the palace immediately.

    Used by the `palace_add_drawer` tool when the agent wants a fact filed
    into searchable memory *now*, without waiting for the next mine cycle.
    One-shot: writes a single .md file into the archive tree, runs mempalace
    mine on it. Returns a human-readable status string for the tool result.

    Defaults to ``room=knowledge`` for durable facts. Pass ``episodes`` for
    daily recaps / operational narratives. mempalace's `detect_room` reads
    room from the folder path first (Priority 1), so we place the .md inside
    a subfolder named after the room.
    """
    if not content or not content.strip():
        return "[palace add] empty content — nothing filed."

    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    slug = _slug(topic) if topic else _slug(content.strip().split("\n", 1)[0])
    resolved_room = room or DEFAULT_DRAWER_ROOM
    room_slug = _slug(resolved_room)
    batch_dir = _archive_root() / f"agent_add_{ts}_{slug}"
    fname = f"{ts}_{slug}.md"

    # Place the .md inside a room-named subfolder so detect_room Priority 1
    # (folder path match) fires deterministically.
    target_dir = batch_dir / room_slug
    room = resolved_room

    header = [f"# Agent-filed drawer", ""]
    header.append(f"- filed: {ts}")
    header.append(f"- wing: {wing}")
    if room:
        header.append(f"- room: {room}")
    if topic:
        header.append(f"- topic: {topic}")
    header.append("")
    header.append("---")
    header.append("")

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / fname).write_text(
            "\n".join(header) + content.strip() + "\n",
            encoding="utf-8",
        )
    except Exception as e:
        return f"[palace add] write failed: {e}"

    ok = await mine_batch_dir(batch_dir, agent="agent-add")
    if not ok:
        return f"[palace add] mine failed — content still on disk at {batch_dir}"
    return (
        f"Filed to palace: wing=`{wing}`"
        + (f", room=`{room}`" if room else "")
        + (f", topic=`{topic}`" if topic else "")
        + f", path={fname}"
    )


async def wake_up(wing: str | None = None) -> str:
    """Fetch a fresh wake-up snapshot (on-demand tool).

    Different from the always-on dynamic-block injection: this runs
    `mempalace wake-up` with an optional --wing filter *right now*,
    so the agent can pull a targeted palace overview mid-conversation.
    """
    args = [MEMPALACE_BIN, "wake-up"]
    if wing:
        args += ["--wing", wing]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=WAKE_UP_TIMEOUT_SEC
        )
        if proc.returncode != 0:
            return (
                f"[palace wake-up] rc={proc.returncode}: "
                f"{stderr.decode(errors='replace')[:400]}"
            )
        text = stdout.decode(errors="replace").strip()
        if text.startswith("Wake-up text"):
            text = text.split("\n", 1)[-1].lstrip("=").lstrip()
        return text or "[palace wake-up] empty output"
    except asyncio.TimeoutError:
        return f"[palace wake-up] timed out after {WAKE_UP_TIMEOUT_SEC}s"
    except Exception as e:
        return f"[palace wake-up] {type(e).__name__}: {e}"


# ─── Wake-up cache ─────────────────────────────────────────────────

async def refresh_wake_up_cache() -> bool:
    """Regenerate the wake-up cache file via `mempalace wake-up` subprocess.

    Keeps chromadb out of the main process. Silent on failure.
    Typically called at the end of mine_batch_dir() so the cache tracks
    the current palace state.
    """
    out_path = _wake_up_file()
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            MEMPALACE_BIN, "wake-up",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=WAKE_UP_TIMEOUT_SEC
        )
        if proc.returncode != 0:
            log.warning(
                f"Wake-up refresh rc={proc.returncode}: "
                f"{stderr.decode(errors='replace')[:400]}"
            )
            return False
        out_path.write_text(stdout.decode(errors="replace"), encoding="utf-8")
        log.info(f"Wake-up cache refreshed ({len(stdout)} bytes at {out_path})")
        return True
    except asyncio.TimeoutError:
        log.warning(f"Wake-up refresh timed out after {WAKE_UP_TIMEOUT_SEC}s")
    except Exception as e:
        log.warning(f"Wake-up refresh failed: {e}")
    return False


# ─── Knowledge graph ───────────────────────────────────────────────

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
        from mempalace.knowledge_graph import KnowledgeGraph
        kg = KnowledgeGraph()
        kg.add_triple(
            subject=str(subject),
            predicate=str(predicate),
            obj=str(object),
            valid_from=valid_from,
        )
        return f"KG: filed `{subject}` --[{predicate}]-> `{object}`" + (
            f" (valid_from={valid_from})" if valid_from else ""
        )
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

    Routing (KnowledgeGraph's actual API):
    - subject → query_entity(subject, direction='outgoing')
    - object only → query_entity(object, direction='incoming')
    - predicate only → query_relationship(predicate)
    - S+P or P+O → filter client-side from the first query
    """
    if not subject and not predicate and not object:
        return "[kg query] give at least one of subject, predicate, object."
    try:
        from mempalace.knowledge_graph import KnowledgeGraph
        kg = KnowledgeGraph()
        if subject:
            rows = kg.query_entity(name=subject, direction="outgoing") or []
        elif object:
            rows = kg.query_entity(name=object, direction="incoming") or []
        else:  # predicate only
            rows = kg.query_relationship(predicate=predicate) or []

        # Client-side filter for narrowing
        def matches(r):
            if subject and r.get("subject") != subject: return False
            if predicate and r.get("predicate") != predicate: return False
            o = r.get("object") or r.get("obj")
            if object and o != object: return False
            return True
        rows = [r for r in rows if matches(r)]
    except Exception as e:
        return f"[kg query] {type(e).__name__}: {e}"

    if not rows:
        parts = []
        if subject: parts.append(f"subject=`{subject}`")
        if predicate: parts.append(f"predicate=`{predicate}`")
        if object: parts.append(f"object=`{object}`")
        return "No KG facts match " + ", ".join(parts) + "."

    lines = [f"**KG query** ({len(rows)} fact(s)):", ""]
    for r in rows:
        lines.append(_fmt_triple(r))
    return "\n".join(lines)


def kg_invalidate(subject: str, predicate: str, object: str, ended: str | None = None) -> str:
    """Mark a KG fact as no longer valid (sets valid_to date)."""
    try:
        from mempalace.knowledge_graph import KnowledgeGraph
        kg = KnowledgeGraph()
        kg.invalidate(subject=str(subject), predicate=str(predicate), obj=str(object), ended=ended)
        return f"KG: invalidated `{subject}` --[{predicate}]-> `{object}`"
    except Exception as e:
        return f"[kg invalidate] {type(e).__name__}: {e}"


def kg_timeline(entity: str) -> str:
    """Return chronological history of all facts touching an entity."""
    try:
        from mempalace.knowledge_graph import KnowledgeGraph
        kg = KnowledgeGraph()
        facts = kg.timeline(entity_name=entity) or []
    except Exception as e:
        return f"[kg timeline] {type(e).__name__}: {e}"

    if not facts:
        return f"No KG history for `{entity}`."
    lines = [f"**KG timeline for `{entity}`** ({len(facts)} fact(s)):", ""]
    for f in facts:
        s = f.get("subject", "?")
        p = f.get("predicate", "?")
        o = f.get("object") or f.get("obj") or "?"
        vf = f.get("valid_from") or "?"
        vt = f.get("valid_to") or "current"
        lines.append(f"- {vf} → {vt}: `{s}` --[{p}]-> `{o}`")
    return "\n".join(lines)


# ─── Diary ─────────────────────────────────────────────────────────

DEFAULT_DIARY_AGENT = DEFAULT_WING


def diary_write(entry: str, topic: str = "general", agent_name: str = DEFAULT_DIARY_AGENT) -> str:
    """Write a diary entry into the single memory wing's diary room.

    Use this to record end-of-session reflections: what happened, what was
    learned, what matters. Persistent across restarts.

    We pin `wing=DEFAULT_WING` explicitly. Left unset, mempalace derives the wing
    as `wing_<agent_name>` (e.g. `wing_agent`), which fragments diary entries off
    into a separate wing from the rest of memory. Reads stay correct either way —
    `tool_diary_read` filters by the `agent` + `room=diary` metadata, not wing.
    """
    if not entry or not entry.strip():
        return "[diary write] empty entry — nothing saved."
    try:
        from mempalace.mcp_server import tool_diary_write as _dw
        result = _dw(agent_name=agent_name, entry=entry, topic=topic, wing=DEFAULT_WING)
    except Exception as e:
        return f"[diary write] {type(e).__name__}: {e}"
    if isinstance(result, dict) and result.get("error"):
        return f"[diary write] {result['error']}"
    return f"Diary entry saved to wing `{DEFAULT_WING}`, topic `{topic}`."


def diary_read(last_n: int = 10, agent_name: str = DEFAULT_DIARY_AGENT) -> str:
    """Read the most recent N diary entries for an agent."""
    try:
        from mempalace.mcp_server import tool_diary_read as _dr
        result = _dr(agent_name=agent_name, last_n=max(1, min(last_n, 50)))
    except Exception as e:
        return f"[diary read] {type(e).__name__}: {e}"

    if isinstance(result, dict) and result.get("error"):
        return f"[diary read] {result['error']}"

    entries = (result or {}).get("entries") or (result or {}).get("diary_entries") or []
    if not entries:
        return f"No diary entries yet for agent `{agent_name}`."

    lines = [f"**Diary — `{agent_name}` (last {len(entries)})**", ""]
    for e in entries:
        ts = e.get("timestamp") or e.get("filed_at") or "?"
        tp = e.get("topic", "?")
        txt = (e.get("entry") or e.get("content") or "").strip()
        lines.append(f"### {ts}  _(topic: {tp})_")
        lines.append(txt)
        lines.append("")
    return "\n".join(lines).rstrip()


# ─── Taxonomy ──────────────────────────────────────────────────────

def _drawers_sqlite_conn():
    """Open the palace Chroma SQLite DB read-only. Returns None if missing."""
    db = _chroma_sqlite_path()
    if not db.is_file():
        return None
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def _taxonomy_from_sqlite() -> dict:
    """Aggregate wing/room/hall counts via SQL.

    Chroma's ``collection.get(include=["metadatas"])`` loads every drawer and
    blows past SQLite's variable limit once the palace grows past a few tens
    of thousands of drawers — which is why Tower showed an empty palace even
    though search still worked.
    """
    from collections import Counter

    conn = _drawers_sqlite_conn()
    if conn is None:
        return {"total": 0, "wings": {}, "halls": {}}

    wing_room_counts: dict[str, Counter] = {}
    hall_counts: Counter = Counter()
    total = 0
    try:
        rows = conn.execute(
            """
            SELECT em_w.string_value AS wing,
                   COALESCE(em_r.string_value, '?') AS room,
                   COUNT(*) AS n
            FROM embeddings e
            JOIN segments s ON s.id = e.segment_id
            JOIN collections c ON c.id = s.collection
                 AND c.name = 'mempalace_drawers'
            JOIN embedding_metadata em_w
                 ON em_w.id = e.id AND em_w.key = 'wing'
            LEFT JOIN embedding_metadata em_r
                 ON em_r.id = e.id AND em_r.key = 'room'
            GROUP BY wing, room
            """
        ).fetchall()
        for wing, room, n in rows:
            wing_room_counts.setdefault(wing or "?", Counter())[room or "?"] += n
            total += n

        for hall, n in conn.execute(
            """
            SELECT COALESCE(em_h.string_value, '?') AS hall, COUNT(*) AS n
            FROM embeddings e
            JOIN segments s ON s.id = e.segment_id
            JOIN collections c ON c.id = s.collection
                 AND c.name = 'mempalace_drawers'
            JOIN embedding_metadata em_h
                 ON em_h.id = e.id AND em_h.key = 'hall'
            GROUP BY hall
            """
        ).fetchall():
            hall_counts[hall or "?"] += n
    finally:
        conn.close()

    return {
        "total": total,
        "wings": {w: dict(c) for w, c in wing_room_counts.items()},
        "halls": dict(hall_counts),
    }


def taxonomy() -> str:
    """Return wing → room breakdown with drawer counts.

    Used by the agent to discover how memory is organized before narrowing
    a search. Aggregates via SQLite so large palaces stay readable.
    """
    path = _palace_path()
    if not os.path.isdir(path):
        return f"[taxonomy] no palace at {path}"

    try:
        data = _taxonomy_from_sqlite()
    except Exception as e:
        return f"[taxonomy] {type(e).__name__}: {e}"

    total = data.get("total", 0)
    if total == 0:
        return "Palace is empty."

    lines = [f"**Palace taxonomy** — {total} drawers total", ""]
    for wing in sorted(data.get("wings") or {}):
        rooms = data["wings"][wing]
        wtotal = sum(rooms.values())
        lines.append(f"- **Wing `{wing}`** ({wtotal} drawer(s)):")
        for room, n in sorted(rooms.items(), key=lambda x: -x[1]):
            lines.append(f"    - room `{room}`: {n}")
    lines.append("")
    lines.append("**Halls** (topic auto-classification):")
    for hall, n in sorted((data.get("halls") or {}).items(), key=lambda x: -x[1]):
        lines.append(f"  - `{hall}`: {n}")
    return "\n".join(lines)


# ─── Tower browser support ─────────────────────────────────────────
#
# Structured (non-markdown) read/write helpers for the Tower "Palace" UI.
# These are additive — separate from search()/taxonomy() above, which are
# agent-tool-facing and format markdown for LLM consumption. Kept as plain
# sync functions in the same in-process-Chroma style as those, so Tower
# (running in the same process) can call them directly.


def search_data(query: str, wing: str | None = None, room: str | None = None,
                 hall: str | None = None, k: int = 20) -> list[dict]:
    """Structured semantic-search results for the Tower palace search UI.

    Deliberately a standalone sibling of search() above rather than a shared
    refactor of it — same retrieval logic, but returns drawer dicts instead
    of a pre-formatted markdown blob, and never touches the agent-tool-facing
    search() function.
    """
    path = _palace_path()
    if not os.path.isdir(path):
        return []
    want = max(1, min(k, 50))

    if hall:
        coll = _drawers_collection()
        if coll is None:
            return []
        where: dict = {"hall": hall}
        if wing: where = {"$and": [where, {"wing": wing}]}
        if room: where = {"$and": [where if isinstance(where, dict) else where, {"room": room}]}
        try:
            res = coll.query(query_texts=[query], n_results=want, where=where)
        except Exception as e:
            log.warning(f"Palace search_data (hall) failed: {e}")
            return []
        drawers = []
        for i, doc in enumerate((res.get("documents") or [[]])[0]):
            md = (res.get("metadatas") or [[]])[0][i] if res.get("metadatas") else {}
            dist = (res.get("distances") or [[]])[0][i] if res.get("distances") else None
            d = _drawer_from_row("", doc, md)
            d["distance"] = dist
            drawers.append(d)
        return drawers

    try:
        from mempalace.searcher import search_memories
    except ImportError:
        return []
    try:
        result = search_memories(query=query, palace_path=path, wing=wing, room=room, n_results=want)
    except Exception as e:
        log.warning(f"Palace search_data failed: {e}")
        return []
    return result.get("results") or result.get("drawers") or []


def _drawers_collection():
    """Return the raw chromadb Collection behind the palace, or None if no
    palace exists yet. Same access pattern as search()/taxonomy() above."""
    try:
        from mempalace.backends.chroma import ChromaBackend
    except ImportError:
        return None
    path = _palace_path()
    if not os.path.isdir(path):
        return None
    try:
        backend = ChromaBackend()
        return backend.get_collection(path, "mempalace_drawers")._collection
    except Exception as e:
        log.warning(f"Palace collection open failed: {e}")
        return None


def taxonomy_data() -> dict:
    """Structured wing → room → count + hall counts, for the Tower taxonomy
    view. Same underlying data as taxonomy(), returned as a dict instead of
    pre-formatted markdown."""
    path = _palace_path()
    if not os.path.isdir(path):
        return {"total": 0, "wings": {}, "halls": {}}
    try:
        return _taxonomy_from_sqlite()
    except Exception as e:
        log.warning(f"Palace taxonomy_data failed: {e}")
        return {"total": 0, "wings": {}, "halls": {}}


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

    Returns {"total": <matching count>, "drawers": [...]}. Uses SQLite so large
    rooms (tens of thousands of drawers) do not trip Chroma's bulk ``.get()``.
    """
    conn = _drawers_sqlite_conn()
    if conn is None:
        return {"total": 0, "drawers": []}

    filters = [
        "c.name = 'mempalace_drawers'",
    ]
    params: list = []
    for key, val in (("wing", wing), ("room", room), ("hall", hall)):
        if val:
            filters.append(
                "EXISTS (SELECT 1 FROM embedding_metadata em_x "
                "WHERE em_x.id = e.id AND em_x.key = ? AND em_x.string_value = ?)"
            )
            params.extend([key, val])
    where_sql = " AND ".join(filters)

    try:
        total = conn.execute(
            f"""
            SELECT COUNT(*)
            FROM embeddings e
            JOIN segments s ON s.id = e.segment_id
            JOIN collections c ON c.id = s.collection
            WHERE {where_sql}
            """,
            params,
        ).fetchone()[0]

        rows = conn.execute(
            f"""
            SELECT e.embedding_id,
                   COALESCE(em_doc.string_value, '') AS doc,
                   COALESCE(em_w.string_value, '?') AS wing,
                   COALESCE(em_r.string_value, '?') AS room,
                   COALESCE(em_h.string_value, '?') AS hall,
                   COALESCE(em_sf.string_value, '') AS source_file
            FROM embeddings e
            JOIN segments s ON s.id = e.segment_id
            JOIN collections c ON c.id = s.collection
            LEFT JOIN embedding_metadata em_doc
                 ON em_doc.id = e.id AND em_doc.key = 'chroma:document'
            LEFT JOIN embedding_metadata em_w
                 ON em_w.id = e.id AND em_w.key = 'wing'
            LEFT JOIN embedding_metadata em_r
                 ON em_r.id = e.id AND em_r.key = 'room'
            LEFT JOIN embedding_metadata em_h
                 ON em_h.id = e.id AND em_h.key = 'hall'
            LEFT JOIN embedding_metadata em_sf
                 ON em_sf.id = e.id AND em_sf.key = 'source_file'
            WHERE {where_sql}
            ORDER BY e.id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, max(1, limit), max(0, offset)],
        ).fetchall()
    except Exception as e:
        log.warning(f"Palace list_drawers failed: {e}")
        return {"total": 0, "drawers": []}
    finally:
        conn.close()

    drawers = [
        _drawer_from_row(
            did,
            doc,
            {"wing": w, "room": r, "hall": h, "source_file": src},
        )
        for did, doc, w, r, h, src in rows
    ]
    return {"total": total, "drawers": drawers}


def get_drawer(drawer_id: str) -> dict | None:
    """Fetch a single drawer by id, or None if it doesn't exist."""
    coll = _drawers_collection()
    if coll is None:
        return None
    try:
        res = coll.get(ids=[drawer_id], include=["documents", "metadatas"])
    except Exception as e:
        log.warning(f"Palace get_drawer failed: {e}")
        return None
    ids = res.get("ids") or []
    if not ids:
        return None
    docs = res.get("documents") or [""]
    metas = res.get("metadatas") or [{}]
    return _drawer_from_row(ids[0], docs[0], metas[0])


def update_drawer(
    drawer_id: str,
    text: str | None = None,
    wing: str | None = None,
    room: str | None = None,
    hall: str | None = None,
) -> str:
    """Edit a single drawer in place — the human-editing counterpart to the
    agent's append-only palace tools.

    When `text` changes, Chroma recomputes ONLY this drawer's embedding: we
    pass `documents=[text]` and deliberately omit `embeddings=`, so the
    collection's bound embedding function (mempalace's local onnxruntime
    model) runs once, for this one row. No other drawer is touched and no
    `mempalace mine` re-index is needed. Metadata-only edits (wing/room/hall)
    skip embedding recompute entirely — only `documents` writes trigger it.
    """
    coll = _drawers_collection()
    if coll is None:
        return "[palace edit] no palace found"
    existing = get_drawer(drawer_id)
    if existing is None:
        return f"[palace edit] drawer `{drawer_id}` not found"

    kwargs: dict = {"ids": [drawer_id]}
    if text is not None and text.strip():
        kwargs["documents"] = [text]

    metadata = dict(existing["metadata"])
    changed_meta = False
    for key, value in (("wing", wing), ("room", room), ("hall", hall)):
        if value is not None and value != metadata.get(key):
            metadata[key] = value
            changed_meta = True
    if changed_meta:
        kwargs["metadatas"] = [metadata]

    if "documents" not in kwargs and "metadatas" not in kwargs:
        return "[palace edit] nothing to change"

    try:
        coll.update(**kwargs)
    except Exception as e:
        return f"[palace edit] {type(e).__name__}: {e}"

    parts = []
    if "documents" in kwargs:
        parts.append("text re-embedded")
    if "metadatas" in kwargs:
        parts.append("metadata updated")
    return f"Drawer `{drawer_id}` updated ({', '.join(parts)})."


def delete_drawer(drawer_id: str) -> str:
    """Remove one drawer from Chroma. Call reconcile_sync() afterward to flush
    HNSW and refresh the wake-up cache."""
    coll = _drawers_collection()
    if coll is None:
        return "[palace delete] no palace found"
    if get_drawer(drawer_id) is None:
        return f"[palace delete] drawer `{drawer_id}` not found"
    try:
        coll.delete(ids=[drawer_id])
    except Exception as e:
        return f"[palace delete] {type(e).__name__}: {e}"
    return f"Drawer `{drawer_id}` deleted."


def create_drawer(
    text: str,
    wing: str = DEFAULT_WING,
    room: str = "general",
    hall: str = "general",
) -> dict:
    """Insert a new drawer directly into Chroma (Tower UI create). Chroma
    computes the embedding from `text`. Returns {"id": ..., ...} or
    {"error": "..."}."""
    if not text or not text.strip():
        return {"error": "empty content"}
    coll = _drawers_collection()
    if coll is None:
        return {"error": "no palace found"}

    import uuid
    drawer_id = str(uuid.uuid4())
    metadata = {
        "wing": wing,
        "room": room,
        "hall": hall,
        "source_file": "tower:create",
    }
    try:
        coll.add(ids=[drawer_id], documents=[text.strip()], metadatas=[metadata])
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    return _drawer_from_row(drawer_id, text.strip(), metadata)


def reconcile_sync() -> dict:
    """Post-edit housekeeping for Tower direct Chroma mutations.

    1. close() — flush in-process vector writes to the on-disk HNSW segment.
    2. Regenerate wake-up cache so the agent's dynamic prompt block matches
       the current palace state.

    Does NOT run `mempalace repair` — that is for index corruption, not
    routine edits. Returns {"ok": bool, "steps": [str, ...]}.
    """
    steps: list[str] = []
    close()
    steps.append("HNSW flushed to disk")

    out_path = _wake_up_file()
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [MEMPALACE_BIN, "wake-up"],
            capture_output=True,
            timeout=WAKE_UP_TIMEOUT_SEC,
        )
        if proc.returncode != 0:
            err = proc.stderr.decode(errors="replace")[:400]
            steps.append(f"wake-up refresh failed (rc={proc.returncode}): {err}")
            return {"ok": False, "steps": steps}
        out_path.write_text(proc.stdout.decode(errors="replace"), encoding="utf-8")
        steps.append(f"wake-up cache refreshed ({len(proc.stdout)} bytes)")
    except subprocess.TimeoutExpired:
        steps.append(f"wake-up refresh timed out after {WAKE_UP_TIMEOUT_SEC}s")
        return {"ok": False, "steps": steps}
    except Exception as e:
        steps.append(f"wake-up refresh error: {type(e).__name__}: {e}")
        return {"ok": False, "steps": steps}

    return {"ok": True, "steps": steps}


def kg_list(limit: int = 200) -> list[dict]:
    """Best-effort listing of ALL KG triples for the Tower KG browser.

    `KnowledgeGraph` (mempalace) doesn't document a "list everything" call —
    kg_query() above always requires at least one of subject/predicate/object.
    This tries a few plausible library methods first, then falls back to a
    direct read of the underlying SQLite file. The fallback's table/column
    names are a best guess from the triple shape used elsewhere in this file
    (subject/predicate/object/valid_from/valid_to) — verify against the
    actually-installed mempalace version and adjust if the schema differs.
    """
    try:
        from mempalace.knowledge_graph import KnowledgeGraph
        kg = KnowledgeGraph()
        for method_name in ("all_triples", "list_triples", "list_facts", "all_facts"):
            method = getattr(kg, method_name, None)
            if callable(method):
                try:
                    return method(limit=limit) or []
                except TypeError:
                    return method() or []
    except Exception as e:
        log.warning(f"KG list (library path) unavailable: {e}")

    try:
        import sqlite3
        for candidate in (
            Path.home() / ".mempalace" / "knowledge_graph.db",
            Path.home() / ".mempalace" / "palace" / "knowledge_graph.db",
        ):
            if candidate.is_file():
                conn = sqlite3.connect(str(candidate))
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM facts ORDER BY valid_from DESC LIMIT ?", (limit,)
                ).fetchall()
                conn.close()
                return [dict(r) for r in rows]
    except Exception as e:
        log.warning(f"KG list (sqlite fallback) failed: {e}")
    return []

"""One-time palace migration to the bge-small embedder + speaker-hall schema.

Two corpora, two treatments:

  - **Learned memories** (room != conversations) keep their text, metadata and
    halls — those halls are meaningful (`shravan-career`, `clodexa-tech`) and the
    drawer id IS the memory id the learning pipeline round-trips. Only the
    embedding is recomputed, in place.

  - **Conversation drawers** are deleted and re-mined from the staged .md
    archives as if they had never been mined. That is the only way to get
    speaker halls, conversation ids, chunk numbers and token-budget chunks onto
    them, and it retires `hall='general'` from the corpus entirely.

Vectors from two different models are not comparable, so the re-embed is not
optional: without it the old drawers are invisible to every new query.

Run with --apply to write. Default is a dry run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env")

from harness import documentdb_palace as palace  # noqa: E402

CONVERSATION_ROOM = "conversations"
RESCUE_DIRNAME = "rescued_orphan_drawers"


def _archive_roots() -> list[pathlib.Path]:
    """Every place staged conversation batches may live on this host."""
    roots = []
    configured = os.environ.get("PALACE_ARCHIVE_ROOT")
    if configured:
        roots.append(pathlib.Path(configured))
    roots.append(pathlib.Path.home() / ".mempalace" / "archive")
    repo = pathlib.Path(__file__).resolve().parent.parent
    roots.append(repo / ".galadriel-local" / "data" / ".mempalace" / "archive")
    seen, unique = set(), []
    for root in roots:
        if root.is_dir() and root not in seen:
            seen.add(root)
            unique.append(root)
    return unique


def _live_archive_root() -> pathlib.Path:
    """The root the running harness stages into (PALACE_ARCHIVE_ROOT)."""
    configured = os.environ.get("PALACE_ARCHIVE_ROOT")
    if configured:
        return pathlib.Path(configured)
    repo = pathlib.Path(__file__).resolve().parent.parent
    local = repo / ".galadriel-local" / "data" / ".mempalace" / "archive"
    return local if local.is_dir() else pathlib.Path.home() / ".mempalace" / "archive"


def rescue_orphans(collection, apply: bool) -> pathlib.Path | None:
    """Stage conversation text whose source .md no longer exists.

    Re-mining reads the archives on disk, so a drawer whose source file was
    already cleaned up would simply vanish. Anything whose exact text also
    survives in a live file needs no rescue — it will be re-mined from there.
    """
    roots = _archive_roots()
    if not roots:
        print("  ! no archive root on this host — cannot rescue")
        return None
    existing = roots[0] / RESCUE_DIRNAME / CONVERSATION_ROOM / "rescued.md"
    if existing.is_file():
        print("  rescue file already staged — reusing it (not re-deriving)")
        return existing.parent.parent
    rows = list(collection.find({"room": CONVERSATION_ROOM}, {"text": 1, "source_file": 1}))
    alive_text = {
        hashlib.sha1(r["text"].strip().encode()).hexdigest()
        for r in rows if pathlib.Path(r.get("source_file", "")).exists()
    }
    orphans = [
        r for r in rows
        if not pathlib.Path(r.get("source_file", "")).exists()
        and hashlib.sha1(r["text"].strip().encode()).hexdigest() not in alive_text
    ]
    print(f"  orphaned drawers with no surviving source and no duplicate: {len(orphans)}")
    if not orphans:
        return None
    batch_dir = roots[0] / RESCUE_DIRNAME
    target = batch_dir / CONVERSATION_ROOM
    print(f"  staging {len(orphans)} orphan(s) -> {batch_dir}")
    if not apply:
        return batch_dir
    target.mkdir(parents=True, exist_ok=True)
    # Written in transcript shape (message markers + a role heading per block),
    # because that is what the miner's markdown fallback parses. Writing raw
    # drawer text filed the rescue as hall='general' or dropped it entirely.
    body = ["# Rescued conversation drawers", "",
            "Text from archives whose staged .md file no longer exists on this",
            "host. Preserved verbatim so the re-mine does not lose it.", "", "---", ""]
    for index, row in enumerate(orphans):
        text = (row.get("text") or "").strip()
        if not text:
            continue
        role = "assistant" if text.lstrip().startswith(("## assistant", "### tool")) else "user"
        text = re.sub(r"^##+ (user|assistant)\s*", "", text).strip()
        body.extend([f"<!-- message {index} -->", f"## {role}", "", text, ""])
    (target / "rescued.md").write_text("\n".join(body), encoding="utf-8")
    return batch_dir


def reembed_learned(collection, apply: bool) -> int:
    """Recompute embeddings for non-conversation drawers, in place."""
    rows = list(collection.find({"room": {"$ne": CONVERSATION_ROOM}}, {"text": 1}))
    print(f"  learned drawers to re-embed: {len(rows)}")
    if not apply:
        return len(rows)
    done = 0
    for start in range(0, len(rows), 32):
        batch = rows[start:start + 32]
        vectors = palace._embeddings([r.get("text") or "" for r in batch])
        for row, vector in zip(batch, vectors):
            collection.update_one({"_id": row["_id"]}, {"$set": {"embedding": vector}})
            done += 1
        print(f"    re-embedded {done}/{len(rows)}", end="\r")
    print()
    return done


_MESSAGE_MARKER = re.compile(r"^<!-- message \d+ -->$", re.M)
_ROLE_HEADING = re.compile(r"^## (user|assistant)\s*$", re.M)


def _spans_from_markdown(text: str) -> list[dict]:
    """Rebuild speaker spans from an archived transcript.

    Batches staged before this change carry no spans manifest, so the halls have
    to be recovered from the markdown the old archiver wrote. Same rules as the
    live path: `## user` carrying a tool_result is agent traffic, and harness
    scaffolding is dropped rather than filed.
    """
    spans: list[dict] = []
    for block in _MESSAGE_MARKER.split(text):
        block = block.strip()
        heading = _ROLE_HEADING.search(block)
        if not heading:
            continue
        role = heading.group(1)
        body = block[heading.end():].strip()
        if not body:
            continue
        if body.lstrip().startswith("[SYSTEM:") or body.lstrip().startswith("[Recall detected]"):
            continue
        hall = "assistant"
        if role == "user" and "### tool_result" not in body:
            hall = "user"
        piece = f"## {role}\n\n{body}"
        if spans and spans[-1]["hall"] == hall:
            spans[-1]["text"] += "\n\n" + piece
        else:
            spans.append({"hall": hall, "text": piece})
    return spans


def _dedupe_batches(batches: list[pathlib.Path]) -> list[pathlib.Path]:
    """Drop staged batches whose transcript is already inside another batch.

    The archives on disk are themselves duplicated: the pre-fix shutdown path
    re-staged buffers the scheduler had already checkpointed, so several .md
    files are byte-identical to each other and some checkpoints are literal
    substrings of a later full archive. Re-mining them all would faithfully
    reproduce the duplication this change exists to remove, so containment is
    resolved here — largest transcript wins.
    """
    bodies: dict[pathlib.Path, str] = {}
    for batch in batches:
        markdown = sorted((batch / CONVERSATION_ROOM).glob("*.md"))
        if not markdown:
            continue
        text = "".join(p.read_text(encoding="utf-8") for p in markdown)
        body = text.split("---\n", 1)[-1].strip()
        if body:
            bodies[batch] = body
    kept: list[pathlib.Path] = []
    for batch in sorted(bodies, key=lambda b: len(bodies[b]), reverse=True):
        if not any(bodies[batch] in bodies[k] for k in kept):
            kept.append(batch)
    dropped = len(bodies) - len(kept)
    if dropped:
        print(f"  duplicate/contained batches skipped: {dropped}")
    return sorted(kept)


_STAMP = re.compile(r"(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})")


def _archived_at(batch: pathlib.Path) -> str | None:
    """Recover the real archive time from the batch dir name.

    Batch dirs are named conversation_<channel>_<kind>_<YYYY-MM-DDTHH-MM-SS>, so
    the original timestamp is right there. Passing None instead made
    _mine_conversation_spans fall back to `now`, collapsing every historical
    drawer's filed_at onto the migration's own run time and destroying recency
    ordering across the whole corpus.
    """
    match = _STAMP.search(batch.name)
    if not match:
        return None
    day, hour, minute, second = match.groups()
    return f"{day}T{hour}:{minute}:{second}+00:00"


def backfill_manifests(batches: list[pathlib.Path], apply: bool) -> int:
    """Write a spans manifest into legacy batches so they mine with real halls.

    conversation_id is derived from the batch name: each historical batch was
    one archive event of one conversation, so that is the honest grouping
    available from disk — the run/tick ids were never written into the .md.
    """
    written = 0
    for batch in batches:
        manifest_path = batch / "spans.json"
        if manifest_path.exists():
            continue
        markdown = sorted((batch / CONVERSATION_ROOM).glob("*.md"))
        if not markdown:
            continue
        spans: list[dict] = []
        for path in markdown:
            for span in _spans_from_markdown(path.read_text(encoding="utf-8")):
                if spans and spans[-1]["hall"] == span["hall"]:
                    spans[-1]["text"] += "\n\n" + span["text"]
                else:
                    spans.append(span)
        if not spans:
            continue
        written += 1
        if not apply:
            continue
        channel = batch.name.split("_")[1] if "_" in batch.name else "unknown"
        manifest_path.write_text(json.dumps({
            "channel_id": channel,
            "conversation_id": f"archive:{batch.name}",
            "archive_kind": "backfill",
            "archived_at": _archived_at(batch),
            "first_chunk": 1,
            "room": CONVERSATION_ROOM,
            "spans": spans,
        }, ensure_ascii=False), encoding="utf-8")
    return written


def remine_conversations(
    collection, apply: bool, rescue_dir: pathlib.Path | None, include_legacy: bool,
) -> dict:
    """Delete every conversation drawer, then mine the archives fresh.

    By default only archives that back a drawer currently in the palace are
    re-mined — this is a re-mine, not an import. The archive root also holds
    ~164 batches from the Chroma era that were never mined into Mongo; those are
    real lost history, but importing them is a separate decision (it would take
    the palace from hundreds of drawers to roughly nine thousand), so it is
    opt-in via --include-legacy.
    """
    stale = collection.count_documents({"room": CONVERSATION_ROOM})
    # Classify by archive root, not by what the drawers currently point at:
    # re-mining rewrites source_file, so a DB-derived split makes this script
    # behave differently on a second run.
    live_root = _live_archive_root()
    found: list[pathlib.Path] = []
    for root in _archive_roots():
        for manifest in sorted(root.rglob(CONVERSATION_ROOM)):
            if manifest.is_dir() and any(manifest.glob("*.md")):
                found.append(manifest.parent)
    current = [
        b for b in found
        if b.parent == live_root or b.name == RESCUE_DIRNAME
        or b.parent.name == "_pending_shutdown"
    ]
    legacy = [b for b in found if b not in current]
    batches = found if include_legacy else current
    print(f"  legacy pre-Mongo batches on disk: {len(legacy)}"
          f"{' (INCLUDED)' if include_legacy else ' (skipped — pass --include-legacy to import)'}")
    if rescue_dir is not None and rescue_dir not in batches:
        batches.append(rescue_dir)
    batches = _dedupe_batches(batches)
    backfilled = backfill_manifests(batches, apply)
    print(f"  spans manifests reconstructed:  {backfilled}")
    print(f"  conversation drawers to delete: {stale}")
    print(f"  archive batches to re-mine:     {len(batches)}")
    # Only drop what this run can actually put back. Drawers whose staged batch
    # is gone (mine_pending_shutdown_archives rmtree's after a successful mine)
    # are unrecoverable from disk, so deleting them would be pure data loss.
    recoverable = {str(b) for b in batches}
    doomed, preserved = [], 0
    for row in collection.find({"room": CONVERSATION_ROOM}, {"source_file": 1}):
        source = row.get("source_file") or ""
        if any(source.startswith(b) for b in recoverable):
            doomed.append(row["_id"])
        else:
            preserved += 1
    print(f"  drawers deletable + re-minable:  {len(doomed)}")
    print(f"  drawers kept (archive gone, cannot be re-mined): {preserved}")
    if not apply:
        return {"deletable": len(doomed), "preserved": preserved,
                "batches": len(batches), "mined": 0, "backfilled": backfilled}
    deleted = collection.delete_many({"_id": {"$in": doomed}}).deleted_count
    mined = 0
    for index, batch in enumerate(batches, 1):
        try:
            palace.mine_directory(batch, agent="remine")
            mined += 1
        except Exception as exc:
            print(f"    ! {batch.name}: {exc}")
        print(f"    mined {index}/{len(batches)}", end="\r")
    print()
    return {"deleted": deleted, "preserved": preserved, "batches": len(batches),
            "mined": mined, "backfilled": backfilled}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument("--include-legacy", action="store_true",
                        help="also import Chroma-era archives never mined into Mongo")
    args = parser.parse_args()
    mode = "APPLY" if args.apply else "DRY RUN"
    collection = palace._collection()

    print(f"=== palace migration ({mode}) ===")
    print(f"  embedder: {palace.EMBED_MODEL} ({palace.DIMENSIONS}d, "
          f"{palace.EMBED_MAX_TOKENS}-token window)")
    print(f"  drawers before: {collection.count_documents({})}")

    print("\n[1/3] rescue orphaned conversation text")
    rescue_dir = rescue_orphans(collection, args.apply)

    print("\n[2/3] re-embed learned memories (text/metadata/halls untouched)")
    reembed_learned(collection, args.apply)

    print("\n[3/3] re-mine conversations with speaker halls + chunk numbers")
    result = remine_conversations(
        collection, args.apply, rescue_dir, args.include_legacy,
    )
    print(f"  {result}")

    if args.apply:
        print("\n=== after ===")
        print(f"  drawers: {collection.count_documents({})}")
        for row in collection.aggregate([
            {"$group": {"_id": {"room": "$room", "hall": "$hall"}, "n": {"$sum": 1}}},
            {"$sort": {"n": -1}},
        ]):
            print(f"    {row['_id']}  {row['n']}")
        leftover = collection.count_documents(
            {"room": CONVERSATION_ROOM, "hall": "general"}
        )
        print(f"  conversation drawers still hall='general': {leftover}")
    else:
        print("\n(dry run — re-run with --apply to write)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

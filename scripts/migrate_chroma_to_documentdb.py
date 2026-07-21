#!/usr/bin/env python3
"""Idempotently migrate MemPalace Chroma drawers and KG facts to DocumentDB."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import mongo_palace  # noqa: E402


CONVERSATION_SOURCE = re.compile(
    r"conversation_(?:(compact|checkpoint)_)?([^_]+)_(\d{4}-\d{2}-\d{2})T"
)


def _include_drawer(metadata: dict, conversation_since: date | None) -> bool:
    """Keep durable rooms and a clean, recent slice of main conversations."""
    if metadata.get("room") != "conversations":
        return True
    if conversation_since is None:
        return True

    source_file = str(metadata.get("source_file", ""))
    match = CONVERSATION_SOURCE.search(source_file)
    if not match:
        return False
    archive_kind, channel, filed_date = match.groups()
    return (
        channel == "main"
        and archive_kind != "checkpoint"
        and "_pending_shutdown" not in Path(source_file).parts
        and date.fromisoformat(filed_date) >= conversation_since
    )


def migrate_drawers(
    palace: Path,
    *,
    conversation_since: date | None = None,
    dry_run: bool = False,
) -> dict:
    database = palace / "chroma.sqlite3"
    if not database.is_file():
        return {"selected": 0, "duplicates_skipped": 0, "by_room": {}}

    import chromadb

    with tempfile.TemporaryDirectory() as temporary:
        clone = Path(temporary) / "palace"
        shutil.copytree(palace, clone)
        client = chromadb.PersistentClient(path=str(clone))
        collection = client.get_collection("mempalace_drawers")
        selected = 0
        duplicates_skipped = 0
        rooms = Counter()
        seen_text = set()
        offset = 0
        while offset < collection.count():
            page = collection.get(
                limit=500,
                offset=offset,
                include=["documents", "metadatas", "embeddings"],
            )
            for index, drawer_id in enumerate(page["ids"]):
                text = (page.get("documents") or [""])[index]
                metadata = (page.get("metadatas") or [{}])[index] or {}
                if not _include_drawer(metadata, conversation_since):
                    continue
                if metadata.get("room") == "conversations":
                    text_digest = hashlib.sha256(text.encode()).digest()
                    if text_digest in seen_text:
                        duplicates_skipped += 1
                        continue
                    seen_text.add(text_digest)
                embeddings = page.get("embeddings")
                if not dry_run:
                    mongo_palace.import_drawer(
                        drawer_id,
                        text,
                        metadata,
                        embeddings[index] if embeddings is not None and len(embeddings) else None,
                    )
                selected += 1
                rooms[str(metadata.get("room") or "knowledge")] += 1
            offset += len(page["ids"])
            if not page["ids"]:
                break
    return {
        "selected": selected,
        "duplicates_skipped": duplicates_skipped,
        "by_room": dict(sorted(rooms.items())),
    }


def read_kg(database: Path) -> list[dict]:
    if not database.is_file():
        return []
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        ]
        rows = []
        for table in tables:
            columns = {
                row[1]
                for row in connection.execute(f'PRAGMA table_info("{table}")')
            }
            object_column = "object" if "object" in columns else "obj" if "obj" in columns else None
            if not {"subject", "predicate"}.issubset(columns) or object_column is None:
                continue
            selected = ["subject", "predicate", f'{object_column} AS object']
            selected.extend(
                column if column in columns else f"NULL AS {column}"
                for column in ("valid_from", "valid_to")
            )
            rows.extend(
                dict(row)
                for row in connection.execute(
                    f'SELECT {", ".join(selected)} FROM "{table}"'
                )
            )
    finally:
        connection.close()
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--palace", type=Path, required=True)
    parser.add_argument("--kg", type=Path, required=True)
    parser.add_argument(
        "--conversation-since",
        type=date.fromisoformat,
        help=(
            "Select all non-conversation drawers plus non-checkpoint main "
            "conversation archives on or after YYYY-MM-DD"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the selected data without writing to MongoDB/DocumentDB",
    )
    args = parser.parse_args()

    drawers = migrate_drawers(
        args.palace,
        conversation_since=args.conversation_since,
        dry_run=args.dry_run,
    )
    kg_rows = read_kg(args.kg)
    result = {
        "dry_run": args.dry_run,
        "drawers_selected": drawers["selected"],
        "duplicate_drawers_skipped": drawers["duplicates_skipped"],
        "drawers_by_room": drawers["by_room"],
        "kg_facts_selected": len(kg_rows),
    }
    if not args.dry_run:
        result["kg_facts_imported"] = mongo_palace.import_kg(kg_rows)
        result["destination_drawers"] = mongo_palace.taxonomy_data()["total"]
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

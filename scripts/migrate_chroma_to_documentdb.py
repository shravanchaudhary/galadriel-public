#!/usr/bin/env python3
"""Idempotently migrate MemPalace Chroma drawers and KG facts to DocumentDB."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import mongo_palace  # noqa: E402


def migrate_drawers(palace: Path) -> int:
    database = palace / "chroma.sqlite3"
    if not database.is_file():
        return 0

    import chromadb

    with tempfile.TemporaryDirectory() as temporary:
        clone = Path(temporary) / "palace"
        shutil.copytree(palace, clone)
        client = chromadb.PersistentClient(path=str(clone))
        collection = client.get_collection("mempalace_drawers")
        imported = 0
        offset = 0
        while offset < collection.count():
            page = collection.get(
                limit=500,
                offset=offset,
                include=["documents", "metadatas", "embeddings"],
            )
            for index, drawer_id in enumerate(page["ids"]):
                embeddings = page.get("embeddings")
                mongo_palace.import_drawer(
                    drawer_id,
                    (page.get("documents") or [""])[index],
                    (page.get("metadatas") or [{}])[index] or {},
                    embeddings[index] if embeddings is not None and len(embeddings) else None,
                )
                imported += 1
            offset += len(page["ids"])
            if not page["ids"]:
                break
    return imported


def migrate_kg(database: Path) -> int:
    if not database.is_file():
        return 0
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
    return mongo_palace.import_kg(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--palace", type=Path, required=True)
    parser.add_argument("--kg", type=Path, required=True)
    args = parser.parse_args()

    result = {
        "drawers_imported": migrate_drawers(args.palace),
        "kg_facts_imported": migrate_kg(args.kg),
    }
    result["documentdb_drawers"] = mongo_palace.taxonomy_data()["total"]
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

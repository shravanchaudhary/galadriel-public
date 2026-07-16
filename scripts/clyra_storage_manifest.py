#!/usr/bin/env python3
"""Create deterministic, content-addressed manifests for Clyra state trees."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path


def build_manifest(root: Path, *, exclude: set[str] | None = None) -> dict:
    root = root.resolve()
    excluded = exclude or set()
    entries: list[dict] = []
    totals = {"files": 0, "directories": 0, "symlinks": 0, "bytes": 0}

    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        info = path.lstat()
        entry = {
            "path": relative,
            "mode": stat.S_IMODE(info.st_mode),
            "uid": info.st_uid,
            "gid": info.st_gid,
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
        }
        if stat.S_ISREG(info.st_mode):
            digest = hashlib.sha256()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            entry.update(type="file", sha256=digest.hexdigest())
            totals["files"] += 1
            totals["bytes"] += info.st_size
        elif stat.S_ISDIR(info.st_mode):
            entry["type"] = "directory"
            totals["directories"] += 1
        elif stat.S_ISLNK(info.st_mode):
            entry.update(type="symlink", target=os.readlink(path))
            totals["symlinks"] += 1
        else:
            entry["type"] = "other"
        entries.append(entry)

    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return {
        "root": str(root),
        **totals,
        "manifest_sha256": hashlib.sha256(canonical).hexdigest(),
        "entries": entries,
    }


def comparison_view(manifest: dict) -> list[dict]:
    """Ignore values a managed NFS target cannot preserve exactly."""
    portable = []
    for entry in manifest["entries"]:
        item = {key: entry[key] for key in ("path", "type", "mode")}
        for key in ("size", "sha256", "target"):
            if key in entry and entry["type"] != "directory":
                item[key] = entry[key]
        portable.append(item)
    return portable


def palace_summary(root: Path) -> dict:
    palace = root / "data" / ".mempalace" / "palace"
    database = palace / "chroma.sqlite3"
    if not database.is_file():
        return {"status": "missing", "path": str(palace)}

    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        collections = {
            name: count
            for name, count in connection.execute(
                """
                SELECT c.name, COUNT(e.id)
                FROM collections c
                LEFT JOIN segments s ON s.collection = c.id
                LEFT JOIN embeddings e ON e.segment_id = s.id
                GROUP BY c.name
                """
            )
        }
    finally:
        connection.close()

    dimensions = None
    try:
        import chromadb

        client = chromadb.PersistentClient(path=str(palace))
        sample = client.get_collection("mempalace_drawers").get(limit=1, include=["embeddings"])
        if sample.get("embeddings") is not None and len(sample["embeddings"]):
            dimensions = len(sample["embeddings"][0])
    except Exception as error:
        dimensions = f"unavailable: {type(error).__name__}: {error}"

    return {
        "status": "ok",
        "path": str(palace),
        "sqlite_integrity": integrity,
        "collections": collections,
        "dimensions": dimensions,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--summary-only", action="store_true")
    parser.add_argument("--palace-summary", action="store_true")
    args = parser.parse_args()

    manifest = build_manifest(args.root, exclude=set(args.exclude))
    if args.palace_summary:
        manifest["palace"] = palace_summary(args.root.resolve())
    if args.summary_only:
        manifest.pop("entries")
    rendered = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

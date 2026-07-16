#!/usr/bin/env python3
"""Destructive S3 Files acceptance checks, isolated from the migrated state."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def filesystem_checks(work: Path) -> list[str]:
    steps: list[str] = []
    work.mkdir(parents=True)

    original = work / "normal.txt"
    with original.open("w", encoding="utf-8") as output:
        output.write("one")
        output.flush()
        os.fsync(output.fileno())
    check(original.read_text(encoding="utf-8") == "one", "create/read failed")
    steps.append("create-read-fsync")

    replacement = work / "replacement.txt"
    replacement.write_text("two", encoding="utf-8")
    os.replace(replacement, original)
    check(original.read_text(encoding="utf-8") == "two", "atomic replace failed")
    renamed = work / "renamed.txt"
    original.rename(renamed)
    check(renamed.read_text(encoding="utf-8") == "two", "rename failed")
    steps.append("update-replace-rename")

    link = work / "link.txt"
    link.symlink_to(renamed.name)
    check(link.read_text(encoding="utf-8") == "two", "symlink failed")
    link.unlink()
    renamed.unlink()
    check(not renamed.exists(), "delete failed")
    steps.append("symlink-delete")

    database = work / "locking.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE durable (value TEXT NOT NULL)")
    connection.execute("INSERT INTO durable VALUES ('committed')")
    connection.commit()
    connection.close()

    crash_code = (
        "import os,sqlite3,sys;"
        "c=sqlite3.connect(sys.argv[1]);"
        "c.execute(\"INSERT INTO durable VALUES ('uncommitted')\");"
        "os._exit(17)"
    )
    crashed = subprocess.run([sys.executable, "-c", crash_code, str(database)], check=False)
    check(crashed.returncode == 17, "forced SQLite crash did not execute")
    connection = sqlite3.connect(database)
    check(connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "SQLite integrity failed")
    rows = connection.execute("SELECT value FROM durable").fetchall()
    connection.close()
    check(rows == [("committed",)], "uncommitted SQLite transaction survived crash")
    steps.append("sqlite-lock-crash-recovery")
    return steps


def palace_checks(palace: Path, work: Path) -> dict:
    clone = work / "palace"
    had_existing_palace = palace.joinpath("chroma.sqlite3").is_file()
    if had_existing_palace:
        shutil.copytree(palace, clone, symlinks=True)
    else:
        clone.mkdir(parents=True)

    import chromadb

    if not had_existing_palace:
        seed_client = chromadb.PersistentClient(path=str(clone))
        seed_collection = seed_client.create_collection(
            "mempalace_drawers",
            metadata={"hnsw:space": "cosine"},
        )
        seed_collection.add(
            ids=["empty-palace-sentinel"],
            documents=["Clyra empty palace S3 Files recall sentinel"],
            metadatas=[{"wing": "agent", "room": "knowledge", "hall": "technical"}],
        )

    connection = sqlite3.connect(clone / "chroma.sqlite3")
    check(connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "palace SQLite integrity failed")
    connection.close()

    client = chromadb.PersistentClient(path=str(clone))
    collection = client.get_collection("mempalace_drawers")
    before = collection.count()
    check(before > 0, "palace has no drawers")
    sample = collection.get(limit=1, include=["documents", "embeddings"])
    sample_id = sample["ids"][0]
    sample_text = sample["documents"][0]
    dimensions = len(sample["embeddings"][0])
    check(dimensions == 384, f"expected 384 dimensions, found {dimensions}")
    recall = collection.query(query_texts=[sample_text], n_results=min(5, before))
    check(sample_id in recall["ids"][0], "semantic recall did not return the source drawer")

    crash_collection = f"clyra_crash_{uuid.uuid4().hex}"
    crash_code = (
        "import chromadb,os,sys;"
        "c=chromadb.PersistentClient(path=sys.argv[1]);"
        "x=c.create_collection(sys.argv[2]);"
        "x.add(ids=['forced-crash'],documents=['clyra crash recovery sentinel']);"
        "os._exit(23)"
    )
    crashed = subprocess.run(
        [sys.executable, "-c", crash_code, str(clone), crash_collection],
        check=False,
    )
    check(crashed.returncode == 23, "forced Chroma crash did not execute")
    recovered = chromadb.PersistentClient(path=str(clone))
    recovered_collection = recovered.get_collection(crash_collection)
    check(recovered_collection.count() == 1, "Chroma lost the forced-crash write")
    result = recovered_collection.query(
        query_texts=["clyra crash recovery sentinel"],
        n_results=1,
    )
    check(result["ids"][0] == ["forced-crash"], "Chroma recall failed after forced crash")

    return {
        "status": "ok",
        "drawers": before,
        "dimensions": dimensions,
        "sample_id": sample_id,
        "crash_recovery": "ok",
        "existing_palace": had_existing_palace,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--palace", type=Path, required=True)
    args = parser.parse_args()

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    work = args.root / ".clyra-canary" / run_id
    result = {
        "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "filesystem": [],
        "palace": {},
    }
    try:
        result["filesystem"] = filesystem_checks(work / "filesystem")
        result["palace"] = palace_checks(args.palace, work)
        check(result["palace"].get("status") == "ok", result["palace"].get("reason", "palace failed"))
        result["status"] = "ok"
    except Exception as error:
        result.update(status="failed", error=f"{type(error).__name__}: {error}")
    finally:
        result["completed_at"] = datetime.now(timezone.utc).isoformat()
        results = args.root / ".clyra-canary-results"
        results.mkdir(exist_ok=True)
        report = results / f"{run_id}.json"
        report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(result, sort_keys=True))
        shutil.rmtree(work, ignore_errors=True)

    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

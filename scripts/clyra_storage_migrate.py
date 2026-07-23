#!/usr/bin/env python3
"""Copy a quiesced Clyra state tree and prove its portable contents match."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path

from clyra_storage_manifest import build_manifest, comparison_view

MARKER = ".clyra-migration.json"


def remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def copy_portable_tree(source: Path, target: Path) -> None:
    """Copy content and modes without unsupported NFS timestamp operations."""
    for source_path in sorted(source.rglob("*")):
        target_path = target / source_path.relative_to(source)
        info = source_path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if source_path.is_symlink():
            if target_path.exists() or target_path.is_symlink():
                remove_path(target_path)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(os.readlink(source_path), target_path)
        elif source_path.is_dir():
            target_path.mkdir(parents=True, exist_ok=True)
            target_path.chmod(mode)
        elif source_path.is_file():
            if target_path.exists() or target_path.is_symlink():
                remove_path(target_path)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, target_path)
            target_path.chmod(mode)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--allow-existing", action="store_true")
    parser.add_argument("--delete", action="store_true")
    args = parser.parse_args()

    source = args.source.resolve()
    target = args.target.resolve()
    if source == target:
        raise SystemExit("source and target resolve to the same path")
    if not source.is_dir() or not target.is_dir():
        raise SystemExit("source and target must be mounted directories")

    existing = [path for path in target.iterdir() if path.name != MARKER]
    if existing and not args.allow_existing:
        raise SystemExit("target is not empty; rerun with --allow-existing only after review")
    if args.delete and not args.allow_existing:
        raise SystemExit("--delete requires --allow-existing")

    source_manifest = build_manifest(source, exclude={MARKER})
    if args.delete:
        for path in sorted(target.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            source_path = source / path.relative_to(target)
            if source_path.exists() or source_path.is_symlink():
                continue
            remove_path(path)
    for source_path in sorted(source.rglob("*")):
        target_path = target / source_path.relative_to(source)
        if source_path.is_symlink() and (target_path.exists() or target_path.is_symlink()):
            remove_path(target_path)
        elif source_path.is_dir() and (
            target_path.is_symlink() or (target_path.exists() and not target_path.is_dir())
        ):
            remove_path(target_path)
        elif source_path.is_file() and target_path.is_dir():
            remove_path(target_path)
    copy_portable_tree(source, target)
    target_manifest = build_manifest(target, exclude={MARKER})

    source_view = comparison_view(source_manifest)
    target_view = comparison_view(target_manifest)
    source_content = [{k: v for k, v in entry.items() if k != "mode"} for entry in source_view]
    target_content = [{k: v for k, v in entry.items() if k != "mode"} for entry in target_view]
    if source_content != target_content:
        source_by_path = {entry["path"]: entry for entry in source_content}
        target_by_path = {entry["path"]: entry for entry in target_content}
        changed = sorted(
            path
            for path in source_by_path.keys() | target_by_path.keys()
            if source_by_path.get(path) != target_by_path.get(path)
        )
        print(json.dumps({"status": "mismatch", "changed_paths": changed[:100]}, sort_keys=True))
        return 1

    marker = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "source_manifest_sha256": source_manifest["manifest_sha256"],
        "source_files": source_manifest["files"],
        "source_directories": source_manifest["directories"],
        "source_symlinks": source_manifest["symlinks"],
        "source_bytes": source_manifest["bytes"],
    }
    (target / MARKER).write_text(json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": "ok", **marker}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

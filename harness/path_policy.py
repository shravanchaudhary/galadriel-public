"""Filesystem ownership boundaries for managed Replika runtimes."""

from __future__ import annotations

import os
from pathlib import Path

PERSISTENT_DIRS = (
    "config",
    "knowledge",
    "memory",
    "state",
    "jobs",
    "workflows",
    "personal-tools",
)


def boundaries_enforced() -> bool:
    return os.environ.get("GALADRIEL_ENFORCE_WRITE_BOUNDARIES", "").lower() in {
        "1",
        "true",
        "yes",
    }


def managed_runtime() -> bool:
    return os.environ.get("REPLIKA_MANAGED_RUNTIME", "").lower() in {
        "1",
        "true",
        "yes",
    }


def storage_root() -> Path:
    return Path(os.environ.get("GALADRIEL_STORAGE_ROOT", "/mnt/efs")).expanduser().resolve()


def writable_roots() -> tuple[Path, ...]:
    configured = os.environ.get("GALADRIEL_AGENT_WRITABLE_DIRS")
    if configured:
        return tuple(
            Path(item).expanduser().resolve()
            for item in configured.split(os.pathsep)
            if item.strip()
        )
    root = storage_root()
    return tuple((root / name).resolve() for name in PERSISTENT_DIRS)


def resolve_path(path: str, *, working_dir: str | None = None) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path(working_dir or os.getcwd()) / candidate
    return candidate.resolve(strict=False)


def assert_agent_writable(path: str, *, working_dir: str | None = None) -> Path:
    resolved = resolve_path(path, working_dir=working_dir)
    if not boundaries_enforced():
        return resolved
    for root in writable_roots():
        if resolved == root or root in resolved.parents:
            return resolved
    roots = ", ".join(str(root) for root in writable_roots())
    raise PermissionError(f"Agent writes are limited to persistent Replika paths: {roots}")


def assert_agent_readable(path: str, *, working_dir: str | None = None) -> Path:
    resolved = resolve_path(path, working_dir=working_dir)
    if not managed_runtime():
        return resolved
    roots = (
        storage_root(),
        Path(os.environ.get("GALADRIEL_CORE_ROOT", "/app")).resolve(),
    )
    for root in roots:
        if resolved == root or root in resolved.parents:
            return resolved
    raise PermissionError("Managed Replikas can read only core and persistent Replika files")

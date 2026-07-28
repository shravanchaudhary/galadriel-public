"""Local runtime-state isolation from repository-owned defaults."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path


DEFAULT_DIRS = ("config", "knowledge", "memory", "state", "jobs", "workflows")
DEFAULT_SNAPSHOT_DIR = ".defaults"
DATED_STATE = re.compile(r"^\d{4}-\d{2}-\d{2}\.(?:md|html|json)$")
RUNTIME_CONFIG_FILES = frozenset({"scheduler_state.json", "ambient_state.json"})


def local_mode() -> bool:
    return os.environ.get("GALADRIEL_ENV", "").strip().lower() == "local"


def local_state_root(source_root: Path) -> Path:
    configured = os.environ.get("GALADRIEL_LOCAL_STATE_ROOT", "").strip()
    return Path(configured).expanduser().resolve() if configured else (
        source_root / ".galadriel-local"
    ).resolve()


def _is_runtime_only(relative: Path) -> bool:
    if (
        relative.parent == Path("config")
        and relative.name in RUNTIME_CONFIG_FILES
    ):
        return True
    if relative.parent == Path("memory") and DATED_STATE.match(relative.name):
        return True
    return (
        len(relative.parts) >= 3
        and relative.parts[:2] in {("state", "plan"), ("state", "progress")}
        and DATED_STATE.match(relative.name) is not None
    )


def sync_defaults(
    source_root: Path,
    target_root: Path,
    *,
    overwrite: bool,
) -> list[str]:
    """Copy repository defaults without copying runtime-only state."""
    copied: list[str] = []
    for directory in DEFAULT_DIRS:
        source_dir = source_root / directory
        if not source_dir.is_dir():
            continue
        for source in source_dir.rglob("*"):
            if not source.is_file():
                continue
            relative = source.relative_to(source_root)
            if _is_runtime_only(relative):
                continue
            target = target_root / relative
            if target.exists() and not overwrite:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append(relative.as_posix())
    return copied


def refresh_changed_defaults(source_root: Path, state_root: Path) -> list[str]:
    """Apply only developer defaults changed since the previous snapshot."""
    snapshot_root = state_root / DEFAULT_SNAPSHOT_DIR
    if not snapshot_root.exists():
        sync_defaults(source_root, snapshot_root, overwrite=True)
        return []

    updated: list[str] = []
    for directory in DEFAULT_DIRS:
        source_dir = source_root / directory
        if not source_dir.is_dir():
            continue
        for source in source_dir.rglob("*"):
            if not source.is_file():
                continue
            relative = source.relative_to(source_root)
            if _is_runtime_only(relative):
                continue
            snapshot = snapshot_root / relative
            if snapshot.exists() and source.read_bytes() == snapshot.read_bytes():
                continue
            target = state_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, snapshot)
            updated.append(relative.as_posix())
    return updated


def prepare_local_state(source_root: Path) -> Path:
    """Initialize local state and make relative runtime paths resolve inside it."""
    source_root = source_root.resolve()
    if not local_mode():
        return source_root

    state_root = local_state_root(source_root)
    defaults_root = Path(
        os.environ.get("GALADRIEL_LOCAL_DEFAULTS_ROOT", str(source_root))
    ).expanduser().resolve()
    state_root.mkdir(parents=True, exist_ok=True)
    for directory in (*DEFAULT_DIRS, "data", "personal-tools", "completion-markers"):
        (state_root / directory).mkdir(parents=True, exist_ok=True)
    sync_defaults(defaults_root, state_root, overwrite=False)
    sync_defaults(
        defaults_root,
        state_root / DEFAULT_SNAPSHOT_DIR,
        overwrite=False,
    )

    os.environ["GALADRIEL_STORAGE_ROOT"] = str(state_root)
    os.environ["GALADRIEL_ENFORCE_WRITE_BOUNDARIES"] = "true"
    os.environ["MEMPALACE_PATH"] = str(state_root / "data/.mempalace/palace")
    os.environ["PALACE_ARCHIVE_ROOT"] = str(state_root / "data/.mempalace/archive")
    os.environ["PALACE_WAKE_UP_FILE"] = str(state_root / "data/.mempalace/wake_up.md")
    os.environ["GALADRIEL_COMPLETION_MARKER_DIR"] = str(
        state_root / "completion-markers"
    )
    os.environ["PHONE_BRIDGE_AUTH_STORE"] = str(
        state_root / "state/phone_bridge_auth.json"
    )

    os.chdir(state_root)
    return state_root

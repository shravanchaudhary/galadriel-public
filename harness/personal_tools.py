"""Tenant-owned personal tools — agent-maintained, separate from product tools.

Product/dev tools live under `harness/` and are immutable to the agent in managed
runtimes. Personal tools live under `personal-tools/` on persistent tenant storage
so product image updates never overwrite them.

Both sections share the same execution route and are advertised to the LLM as a
single flat tool list. On name collision, the developer/product tool wins.
"""

from __future__ import annotations

import importlib.util
import logging
import os
from pathlib import Path
from types import ModuleType
from typing import Any, Awaitable, Callable

log = logging.getLogger("galadriel.personal_tools")

ExecuteFn = Callable[[dict], Awaitable[Any] | Any]

_cache_key: tuple[str, tuple[tuple[str, float], ...]] | None = None
_cache_defs: list[dict] = []
_cache_executors: dict[str, ExecuteFn] = {}


def clear_cache() -> None:
    global _cache_key, _cache_defs, _cache_executors
    _cache_key = None
    _cache_defs = []
    _cache_executors = {}


def personal_tools_root(working_dir: str | None = None) -> Path:
    configured = os.environ.get("GALADRIEL_STORAGE_ROOT")
    if configured:
        return Path(configured).expanduser().resolve() / "personal-tools"
    base = Path(working_dir or os.getcwd()).expanduser().resolve()
    return base / "personal-tools"


def _module_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(
        path
        for path in root.glob("*.py")
        if path.is_file() and not path.name.startswith("_")
    )


def _signature(root: Path) -> tuple[str, tuple[tuple[str, float], ...]]:
    files = _module_files(root)
    return (
        str(root),
        tuple((path.name, path.stat().st_mtime) for path in files),
    )


def _load_module(path: Path) -> ModuleType | None:
    module_name = f"galadriel_personal_tools_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        log.warning("failed to load personal tool module %s: %s", path, exc)
        return None
    return module


def _normalize_defs(raw: Any, source: Path) -> list[dict]:
    if not isinstance(raw, list):
        log.warning("personal tool module %s missing TOOL_DEFINITIONS list", source)
        return []
    defs: list[dict] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            log.warning("skipping invalid TOOL_DEFINITIONS entry in %s", source)
            continue
        defs.append(item)
    return defs


def refresh_personal_tools(
    *,
    working_dir: str | None = None,
    reserved_names: set[str] | None = None,
) -> tuple[list[dict], dict[str, ExecuteFn]]:
    """Load/reload personal tool modules. Reserved (dev) names always win."""
    global _cache_key, _cache_defs, _cache_executors

    root = personal_tools_root(working_dir)
    key = _signature(root)
    if _cache_key == key:
        return _cache_defs, _cache_executors

    reserved = reserved_names or set()
    defs: list[dict] = []
    executors: dict[str, ExecuteFn] = {}
    seen: set[str] = set()

    for path in _module_files(root):
        module = _load_module(path)
        if module is None:
            continue
        execute = getattr(module, "execute_tool", None) or getattr(module, "execute", None)
        if not callable(execute):
            log.warning("personal tool module %s has no execute_tool/execute", path)
            continue
        for tool_def in _normalize_defs(getattr(module, "TOOL_DEFINITIONS", None), path):
            name = tool_def["name"]
            if name in reserved:
                log.info(
                    "personal tool %r ignored — developer tool with same name wins",
                    name,
                )
                continue
            if name in seen:
                log.warning("duplicate personal tool name %r in %s — skipping", name, path)
                continue
            seen.add(name)
            defs.append(tool_def)
            executors[name] = execute  # type: ignore[assignment]

    _cache_key = key
    _cache_defs = defs
    _cache_executors = executors
    return defs, executors


def personal_tool_definitions(
    *,
    working_dir: str | None = None,
    reserved_names: set[str] | None = None,
) -> list[dict]:
    defs, _ = refresh_personal_tools(
        working_dir=working_dir, reserved_names=reserved_names
    )
    return list(defs)


async def execute_personal_tool(
    name: str,
    inputs: dict,
    *,
    working_dir: str | None = None,
    reserved_names: set[str] | None = None,
) -> str | list | None:
    """Run a personal tool if registered. Returns None when not personal."""
    _, executors = refresh_personal_tools(
        working_dir=working_dir, reserved_names=reserved_names
    )
    execute = executors.get(name)
    if execute is None:
        return None
    result = execute(name, inputs) if _accepts_name(execute) else execute(inputs)
    if hasattr(result, "__await__"):
        result = await result  # type: ignore[misc]
    return result


def _accepts_name(fn: ExecuteFn) -> bool:
    try:
        import inspect

        params = list(inspect.signature(fn).parameters)
    except Exception:
        return True
    return len(params) >= 2

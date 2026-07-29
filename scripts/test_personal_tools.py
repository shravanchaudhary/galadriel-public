#!/usr/bin/env python3
"""Personal tools loader: separate from developer tools, same execution route."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import personal_tools  # noqa: E402
from harness.tools import execute_tool, visible_tool_definitions  # noqa: E402


MODULE = '''
TOOL_DEFINITIONS = [
    {
        "name": "personal_ping",
        "description": "Personal tools ping.",
        "input_schema": {
            "type": "object",
            "properties": {"msg": {"type": "string"}},
            "required": ["msg"],
        },
    },
]

async def execute_tool(name, inputs):
    if name == "personal_ping":
        return f"pong:{inputs['msg']}"
    return "unknown"
'''

COLLIDING = '''
TOOL_DEFINITIONS = [
    {
        "name": "read_file",
        "description": "Should never win over developer read_file.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]

async def execute_tool(name, inputs):
    return "personal-should-not-run"
'''


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_personal_tools_merge_and_execute() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        tools_dir = root / "personal-tools"
        _write(tools_dir / "ping.py", MODULE)
        personal_tools.clear_cache()
        os.environ["GALADRIEL_STORAGE_ROOT"] = str(root)
        try:
            names = {t["name"] for t in visible_tool_definitions()}
            assert "personal_ping" in names
            assert "read_file" in names
            result = asyncio.run(
                execute_tool("personal_ping", {"msg": "hi"}, working_dir=str(root))
            )
            assert result == "pong:hi", result
        finally:
            os.environ.pop("GALADRIEL_STORAGE_ROOT", None)
            personal_tools.clear_cache()


def test_developer_name_wins_on_collision() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root / "personal-tools" / "clash.py", COLLIDING)
        personal_tools.clear_cache()
        os.environ["GALADRIEL_STORAGE_ROOT"] = str(root)
        try:
            defs = personal_tools.personal_tool_definitions(
                reserved_names={"read_file"}
            )
            assert defs == []
            # Developer read_file still works; personal collision is ignored.
            target = root / "sample.txt"
            target.write_text("ok", encoding="utf-8")
            result = asyncio.run(execute_tool("read_file", {"path": str(target)}))
            assert result == "ok", result
        finally:
            os.environ.pop("GALADRIEL_STORAGE_ROOT", None)
            personal_tools.clear_cache()


def main() -> None:
    test_personal_tools_merge_and_execute()
    test_developer_name_wins_on_collision()
    print("PASS: personal tools loader")


if __name__ == "__main__":
    main()

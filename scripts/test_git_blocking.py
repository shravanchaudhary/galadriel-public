#!/usr/bin/env python3
"""Regression checks for the Replika source-control command boundary."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import tools  # noqa: E402


async def main() -> None:
    os.environ.pop("REPLIKA_MANAGED_RUNTIME", None)
    executed = False

    async def fail_if_executed(command: str, working_dir: str | None = None) -> str:
        nonlocal executed
        executed = True
        return command

    original = tools._run_shell
    tools._run_shell = fail_if_executed
    try:
        for command in (
            "git status",
            "/usr/bin/git diff",
            "env FOO=bar git log",
            "sh -c 'git add state/'",
            'bash -lc "git commit -m nope"',
            "command git push",
        ):
            result = await tools.execute_tool("run_shell", {"command": command})
            assert isinstance(result, str) and result.startswith("[blocked]"), result
        assert not executed, "blocked Git command reached the shell"

        result = await tools.execute_tool("run_shell", {"command": "pwd"})
        assert result == "pwd", result
        assert executed, "non-Git shell command did not reach the shell"
    finally:
        tools._run_shell = original

    print("Git command blocking checks passed")


if __name__ == "__main__":
    asyncio.run(main())

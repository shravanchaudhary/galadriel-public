#!/usr/bin/env python3
"""Checks for constrained phone tools and bounded ADB execution."""

import asyncio
import os
from pathlib import Path
import stat
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import phone_tools
from harness.tools import execute_tool, visible_tool_definitions
from phone_bridge.adb_manager import AdbError, AdbManager


class FakeManager:
    def __init__(self):
        self.calls = []

    async def run(self, *args):
        self.calls.append(args)
        return "ok"


class SerializationProbe(AdbManager):
    def __init__(self):
        super().__init__()
        self.active = 0
        self.maximum_active = 0

    async def _run_process(self, *args):
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        await asyncio.sleep(0.02)
        self.active -= 1
        return "connected to 127.0.0.1:37000"


async def check_tools() -> None:
    fake = FakeManager()
    original = phone_tools.get_adb_manager
    phone_tools.get_adb_manager = lambda: fake
    os.environ["PHONE_TOOLS_ENABLED"] = "1"
    try:
        names = {tool["name"] for tool in visible_tool_definitions()}
        assert phone_tools.PHONE_TOOL_NAMES <= names
        assert await execute_tool("phone_tap", {"x": 12, "y": 34}) == "ok"
        assert fake.calls[-1] == ("shell", "input", "tap", "12", "34")
        assert await execute_tool(
            "phone_swipe",
            {"x1": 1, "y1": 2, "x2": 3, "y2": 4, "duration": 500},
        ) == "ok"
        assert fake.calls[-1] == (
            "shell", "input", "swipe", "1", "2", "3", "4", "500"
        )
        assert await execute_tool("phone_type", {"text": "hello world"}) == "ok"
        assert fake.calls[-1][-1] == "hello%sworld"
        assert "[tool error]" in await execute_tool(
            "phone_tap", {"x": -1, "y": 2}
        )
        assert "[tool error]" in await execute_tool(
            "phone_type", {"text": "unsafe;command"}
        )
        assert "[blocked]" in await execute_tool(
            "phone_shell", {"command": "id"}
        )
        os.environ["PHONE_SHELL_ALLOW_ARBITRARY"] = "1"
        assert await execute_tool("phone_shell", {"command": "id"}) == "ok"
        assert fake.calls[-1] == ("shell", "id")
    finally:
        phone_tools.get_adb_manager = original
        os.environ.pop("PHONE_TOOLS_ENABLED", None)
        os.environ.pop("PHONE_SHELL_ALLOW_ARBITRARY", None)

    assert not (
        phone_tools.PHONE_TOOL_NAMES
        & {tool["name"] for tool in visible_tool_definitions()}
    )


def fake_adb(directory: Path) -> Path:
    executable = directory / "adb"
    executable.write_text(
        """#!/usr/bin/env python3
import sys
import time
if sys.argv[1] == "connect":
    print("connected to " + sys.argv[2])
elif sys.argv[-1] == "slow":
    time.sleep(2)
elif sys.argv[-1] == "large":
    print("x" * 200)
else:
    print(" ".join(sys.argv[1:]))
""",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


async def check_manager() -> None:
    with tempfile.TemporaryDirectory() as directory:
        os.environ["ADB_BINARY"] = str(fake_adb(Path(directory)))
        manager = AdbManager(timeout_seconds=1, max_output_bytes=80)
        output = await manager.run("shell", "echo", "hello")
        assert "-s 127.0.0.1:37000 shell echo hello" in output
        assert "[output truncated]" in await manager.run("large")
        try:
            await manager.run("slow")
            raise AssertionError("Timed-out ADB process was not killed")
        except AdbError as error:
            assert "timed out" in str(error)
        os.environ.pop("ADB_BINARY", None)

    probe = SerializationProbe()
    await asyncio.gather(probe.run("one"), probe.run("two"))
    assert probe.maximum_active == 1


async def main() -> None:
    await check_tools()
    await check_manager()


if __name__ == "__main__":
    asyncio.run(main())
    print("phone tool tests passed")

"""Focused identity-based tool permission checks."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.tool_access import tools_for_request  # noqa: E402
from harness.safety import (  # noqa: E402
    classify_command,
    is_demonstrably_read_only,
    is_git_command,
)


def check(condition, message):
    if not condition:
        raise AssertionError(message)


tools = [
    {"name": "read_file"},
    {"name": "write_file"},
    {"name": "run_shell"},
    {"name": "db_update"},
]
member = {
    "source": "slack",
    "actor_id": "UMEMBER",
    "replika_type": "organization",
    "trusted": False,
}
admin = {**member, "actor_id": "UADMIN", "trusted": True}
tower = {"source": "tower", "actor_id": "owner", "trusted": True}

check(
    [tool["name"] for tool in tools_for_request(tools, member)]
    == ["read_file", "run_shell"],
    "organization member receives only read-only tools",
)
check(tools_for_request(tools, admin) is tools, "Slack admin retains current tools")
check(tools_for_request(tools, tower) is tools, "Tower retains current tools")

for command in ("ls -la", "cat README.md"):
    check(
        classify_command(command) == "green" and is_demonstrably_read_only(command),
        f"read-only green command rejected: {command}",
    )
for command in (
    "python3 mutate.py",
    "echo owned > file",
    "find . -delete",
    "sort input -o output",
    "ls; rm file",
    "rm file",
):
    check(
        classify_command(command) != "green" or not is_demonstrably_read_only(command),
        f"mutating or ambiguous command treated as demonstrably read-only: {command}",
    )

for command in (
    "git status",
    "/usr/bin/git diff",
    "env FOO=bar git log",
    "sh -c 'git add state/'",
    "bash -lc \"git commit -m nope\"",
    "command git push",
):
    check(is_git_command(command), f"Git invocation was not detected: {command}")
    check(classify_command(command) == "red", f"Git invocation was not blocked: {command}")
    check(
        not is_demonstrably_read_only(command),
        f"Git invocation was treated as read-only: {command}",
    )

print("identity permission checks passed")

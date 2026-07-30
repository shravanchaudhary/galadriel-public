"""Classify tool-result protocol sentinels for experiential appraisal."""

from __future__ import annotations

import re


_FAILURE_PREFIXES = ("[tool error]", "[blocked]", "[error]")
_NONZERO_EXIT_RE = re.compile(r"\[exit code:\s*-?[1-9]\d*\]", re.IGNORECASE)


def tool_result_failed(result) -> bool:
    if isinstance(result, str):
        normalized = result.lstrip()
        return (
            normalized.lower().startswith(_FAILURE_PREFIXES)
            or _NONZERO_EXIT_RE.search(normalized) is not None
        )
    if isinstance(result, list):
        return any(
            isinstance(block, dict)
            and block.get("type") == "text"
            and tool_result_failed(str(block.get("text", "")))
            for block in result
        )
    return False

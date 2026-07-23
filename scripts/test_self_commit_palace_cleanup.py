#!/usr/bin/env python3
"""Check that palace cleanup is narrow and excludes conversation history."""

from __future__ import annotations

from clean_self_commit_palace import matching_drawers, palace


pages = {
    0: {
        "total": 4,
        "drawers": [
            {
                "id": "policy",
                "room": "knowledge",
                "text": "Daily State Commit: git add state/ config/ memory/ jobs/",
            },
            {
                "id": "conversation",
                "room": palace.CONVERSATION_ROOM,
                "text": "The user discussed the daily state commit design.",
            },
        ],
    },
    2: {
        "total": 4,
        "drawers": [
            {"id": "unrelated", "room": "knowledge", "text": "Use concise answers."},
            {
                "id": "policy-2",
                "room": "episodes",
                "text": "Commit any code changes made for the task.",
            },
        ],
    },
}

original = palace.list_drawers
palace.list_drawers = lambda limit, offset: pages.get(  # type: ignore[assignment]
    offset, {"total": 4, "drawers": []}
)
try:
    assert [drawer["id"] for drawer in matching_drawers()] == ["policy", "policy-2"]
finally:
    palace.list_drawers = original

print("self-commit palace cleanup checks passed")

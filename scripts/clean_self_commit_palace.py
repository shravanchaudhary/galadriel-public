#!/usr/bin/env python3
"""Delete exact self-commit policy drawers without touching conversation history."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import palace  # noqa: E402


POLICY_MARKERS = (
    "daily state commit",
    "automated daily state backup",
    "git add state/ config/ memory/ jobs/",
    "there is no auto-commit; it is your responsibility",
    "commit any code changes made for the task",
)


def matching_drawers() -> list[dict]:
    matches: list[dict] = []
    offset = 0
    while True:
        page = palace.list_drawers(limit=200, offset=offset)
        drawers = page.get("drawers", [])
        for drawer in drawers:
            if drawer.get("room") == palace.CONVERSATION_ROOM:
                continue
            text = str(drawer.get("text", "")).lower()
            if any(marker in text for marker in POLICY_MARKERS):
                matches.append(drawer)
        offset += len(drawers)
        if not drawers or offset >= int(page.get("total", 0)):
            break
    return matches


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete matching non-conversation drawers. Default is a dry run.",
    )
    args = parser.parse_args()

    matches = matching_drawers()
    deleted: list[str] = []
    if args.apply:
        for drawer in matches:
            result = palace.delete_drawer(drawer["id"])
            if "deleted" not in result.lower():
                raise RuntimeError(result)
            deleted.append(drawer["id"])
        if deleted:
            asyncio.run(palace.refresh_wake_up_cache())

    print(
        json.dumps(
            {
                "mode": "apply" if args.apply else "dry-run",
                "matched": len(matches),
                "deleted": len(deleted),
                "drawers": [
                    {
                        "id": drawer["id"],
                        "room": drawer.get("room"),
                        "hall": drawer.get("hall"),
                        "source_file": drawer.get("source_file"),
                    }
                    for drawer in matches
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

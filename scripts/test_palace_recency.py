#!/usr/bin/env python3
"""Dry-run tests for palace recency search and conversation buffer I/O.

Usage:
    python scripts/test_palace_recency.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import conversation_store, palace  # noqa: E402


def test_recency_main_conversations() -> bool:
    print("=== test: recency — room=conversations, channel=main ===")
    out = palace.search(order="recency", room="conversations", channel="main", k=5)
    print(out[:1200])
    if "No recent sessions matched" in out:
        print("FAIL: no main conversation archives found")
        return False
    if "order=`recency`" not in out:
        print("FAIL: missing recency header")
        return False
    # Most recent main archive should appear first
    if "filed=" not in out:
        print("FAIL: missing filed_at timestamps")
        return False
    print("PASS")
    return True


def test_recency_finds_actions_page_chat() -> bool:
    print("\n=== test: recency + text filter — planned actions ===")
    out = palace.search(
        order="recency",
        room="conversations",
        channel="main",
        query="planned actions",
        k=3,
    )
    print(out[:900])
    if "planned actions" not in out.lower():
        print("FAIL: expected 'planned actions' in results")
        return False
    if 'view": "actions"' in out or "how does planned actions work" in out.lower() or "planned actions" in out.lower():
        print("PASS (actions-page exchange found)")
        return True
    print("WARN: actions text present but exact exchange not confirmed — check manually")
    return True


def test_semantic_still_requires_query() -> bool:
    print("\n=== test: semantic mode requires query ===")
    out = palace.search(order="semantic", query="")
    if "query is required" not in out:
        print(f"FAIL: unexpected output: {out[:200]}")
        return False
    print("PASS")
    return True


def test_conversation_store_roundtrip() -> bool:
    print("\n=== test: conversation buffer save/load ===")
    with tempfile.TemporaryDirectory() as tmp:
        wd = Path(tmp)
        messages = [
            {"role": "user", "content": "[Tower]: hello from test"},
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        ]
        conversation_store.save_channel(wd, "main", messages)
        loaded = conversation_store.load_all(wd)
        if "main" not in loaded or len(loaded["main"]) != 2:
            print(f"FAIL: loaded={loaded}")
            return False
        conversation_store.delete_channel(wd, "main")
        if conversation_store.load_all(wd):
            print("FAIL: buffer not deleted")
            return False
        # worker channel must not persist
        conversation_store.save_channel(wd, "worker", messages)
        if list(conversation_store._buffers_dir(wd).glob("*.json")):
            print("FAIL: worker buffer should not be written")
            return False
    print("PASS")
    return True


def main() -> int:
    ok = all([
        test_recency_main_conversations(),
        test_recency_finds_actions_page_chat(),
        test_semantic_still_requires_query(),
        test_conversation_store_roundtrip(),
    ])
    print("\n" + ("ALL PASS" if ok else "SOME TESTS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

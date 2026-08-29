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

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from harness import conversation_store, palace  # noqa: E402


def test_recency_main_conversations() -> bool:
    print("=== test: recency — room=conversations, channel=main ===")
    out = palace.search(order="recency", room="conversations", channel="main", k=5)
    print(out[:1200])
    if "NO_MATCH" in out:
        print("FAIL: no main conversation archives found")
        return False
    if "most recent" not in out.lower():
        print("FAIL: missing recency header")
        return False
    # Most recent main archive should appear first
    if "filed=" not in out:
        print("FAIL: missing filed_at timestamps")
        return False
    print("PASS")
    return True


def test_recency_text_filter_narrows() -> bool:
    """The recency text filter must actually filter, in both directions.

    This used to assert on one hard-coded phrase from a conversation that is no
    longer in the corpus, so it failed on fixture drift rather than on a real
    regression. It now checks the mechanism: a term present in the corpus
    narrows to drawers containing it, and a term that is absent returns nothing.
    """
    print("\n=== test: recency text filter narrows results ===")
    unfiltered = palace.search(order="recency", room="conversations", k=5)
    if "NO_MATCH" in unfiltered:
        print("SKIP: no conversation archives in this palace")
        return True

    present = palace.search(
        order="recency", room="conversations", query="actions", k=3,
    )
    if "NO_MATCH" in present:
        print("FAIL: a term known to be in the corpus returned nothing")
        return False
    body = present.split("**", 2)[-1].lower()
    if "actions" not in body:
        print("FAIL: filtered results do not contain the filter term")
        return False

    absent = palace.search(
        order="recency", room="conversations",
        query="zzq-not-in-any-drawer-xyzzy", k=3,
    )
    if "NO_MATCH" not in absent:
        print(f"FAIL: absent term still returned rows: {absent[:200]}")
        return False
    print("PASS")
    return True


def test_semantic_still_requires_query() -> bool:
    print("\n=== test: semantic mode requires query ===")
    out = palace.search(order="semantic", query="")
    if "give a `query`" not in out:
        print(f"FAIL: unexpected output: {out[:200]}")
        return False
    print("PASS")
    return True


def test_conversation_store_roundtrip() -> bool:
    print("\n=== test: conversation buffer save/load ===")
    with tempfile.TemporaryDirectory() as tmp:
        wd = Path(tmp)
        messages = [
            {"role": "user", "content": "hello from test"},
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
        test_recency_text_filter_narrows(),
        test_semantic_still_requires_query(),
        test_conversation_store_roundtrip(),
    ])
    print("\n" + ("ALL PASS" if ok else "SOME TESTS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Cue lifecycle: LRU eviction, saturation skip, fail-closed arming.

Covers the guards that keep cue arrays from growing into looser matching:
  - evict_lru_cues drops cues that never won a Stage-2 match, not the oldest-added
  - cue_is_saturated rejects near-duplicate appends that max() would ignore
  - recall_system_armed disarms the whole pipeline (fail-closed) when the
    Stage-2 judge has no GEMINI_API_KEY
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["RECALL_SLM_VERIFY"] = "1"

from harness.recall import (  # noqa: E402
    cue_is_saturated,
    cue_key,
    evict_lru_cues,
    recall_system_armed,
    scan_text_for_recalls,
)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_cue_key_stable() -> None:
    _assert(cue_key("Remember I like tea") == cue_key("  remember i like TEA  "),
            "cue_key should ignore case and surrounding whitespace")
    _assert(cue_key("a") != cue_key("b"), "distinct cues need distinct keys")
    key = cue_key("anything")
    _assert("." not in key and "$" not in key, "cue_key must be Mongo-safe")
    print("ok cue_key_stable")


def test_evict_prefers_never_used() -> None:
    now = datetime.now(timezone.utc)
    examples = ["authored-used", "authored-never", "appended-never"]
    usage = {cue_key("authored-used"): now - timedelta(days=30)}
    kept = evict_lru_cues(examples, usage, 2)
    _assert(kept == ["authored-used", "appended-never"], f"unexpected kept={kept}")
    print("ok evict_prefers_never_used")


def test_evict_is_lru_not_fifo() -> None:
    """Mirrors tune_recall: stamp the inserted cue, then evict."""
    now = datetime.now(timezone.utc)
    examples = ["oldest-but-hot", "newer-but-cold"]
    usage = {
        cue_key("oldest-but-hot"): now,
        cue_key("newer-but-cold"): now - timedelta(days=90),
        cue_key("fresh"): now,
    }
    kept = evict_lru_cues(examples + ["fresh"], usage, 2)
    _assert("oldest-but-hot" in kept, "recently used cue must survive FIFO position")
    _assert("newer-but-cold" not in kept, "stale cue should be evicted first")
    _assert("fresh" in kept, "freshly stamped cue must not be the victim")
    print("ok evict_is_lru_not_fifo")


def test_unstamped_insert_is_its_own_victim() -> None:
    """Documents why tune_recall must stamp before evicting."""
    now = datetime.now(timezone.utc)
    usage = {cue_key("hot"): now, cue_key("warm"): now}
    kept = evict_lru_cues(["hot", "warm", "unstamped"], usage, 2)
    _assert("unstamped" not in kept, "an unstamped insert ranks oldest by design")
    print("ok unstamped_insert_is_its_own_victim")


def test_evict_noop_under_cap() -> None:
    examples = ["a", "b"]
    _assert(evict_lru_cues(examples, {}, 5) == examples, "under cap must be untouched")
    _assert(evict_lru_cues([], {}, 0) == [], "empty list must be safe")
    print("ok evict_noop_under_cap")


def test_saturation_skips_near_duplicates() -> None:
    existing = ["please remember that I like tea", "steps to deploy to staging"]

    saturated, score = cue_is_saturated("please remember that I like tea", existing)
    _assert(saturated, f"verbatim repeat should saturate (cosine={score})")

    saturated, score = cue_is_saturated("what is the capital of France?", existing)
    _assert(not saturated, f"unrelated cue must not saturate (cosine={score})")

    saturated, _ = cue_is_saturated("anything", [])
    _assert(not saturated, "empty pool cannot saturate")
    print("ok saturation_skips_near_duplicates")


def test_disarmed_without_judge_key() -> None:
    """No GEMINI_API_KEY in judge mode → whole system off, Stage-1 included."""
    saved = os.environ.pop("GEMINI_API_KEY", None)
    try:
        _assert(not recall_system_armed(), "system must disarm without a judge key")
        matches = scan_text_for_recalls(
            "please remember that I like tea",
            [{
                "recall_id": "t_disarm",
                "instruction": "User is teaching a durable preference.",
                "positive_examples": ["please remember that I like tea"],
                "negative_examples": [],
            }],
        )
        _assert(matches == [], "Stage-1 must not propose while disarmed")
    finally:
        if saved is not None:
            os.environ["GEMINI_API_KEY"] = saved
    print("ok disarmed_without_judge_key")


def test_judge_sees_similar_negatives() -> None:
    """filter_matches_with_judge must pass the chunk-similar tune_recall
    negatives to the judge as judge_negatives, capped and best-first."""
    import asyncio

    import harness.recall_judge as rj
    from harness.recall import filter_matches_with_judge

    seen: dict = {}
    real_judge = rj.judge_applicability

    async def spy_judge(provider, *, chunk, candidates, model=None, usage_callback=None, **kw):
        seen["candidates"] = candidates
        return {"applicable": []}

    rj.judge_applicability = spy_judge
    try:
        verified, rejected = asyncio.run(filter_matches_with_judge(
            [{
                "recall_id": "t_neg",
                "instruction": "check the worker control file",
                "activation_condition": "The user asks about background worker state.",
                "matched_chunk": "can you pause the worker for now?",
                "positive_score": 0.9,
                "negative_examples": [
                    "pause the worker please",
                    "what is the capital of France?",
                    "hold the background worker",
                    "I like tea",
                    "stop the worker job",
                ],
            }],
            provider=object(),
        ))
    finally:
        rj.judge_applicability = real_judge

    negs = seen["candidates"][0].get("judge_negatives")
    _assert(negs is not None and len(negs) == 3, f"expected 3 selected negatives, got {negs}")
    _assert("what is the capital of France?" not in negs,
            f"dissimilar negative must not be selected: {negs}")
    _assert("I like tea" not in negs, f"dissimilar negative must not be selected: {negs}")
    _assert(not verified and rejected, "spy judge returned none-applicable")
    print("ok judge_sees_similar_negatives")


def main() -> int:
    test_cue_key_stable()
    test_evict_prefers_never_used()
    test_evict_is_lru_not_fifo()
    test_unstamped_insert_is_its_own_victim()
    test_evict_noop_under_cap()
    test_saturation_skips_near_duplicates()
    test_disarmed_without_judge_key()
    test_judge_sees_similar_negatives()
    print("ok recall_cue_lifecycle")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as e:
        print(f"ASSERT: {e}", file=sys.stderr)
        raise SystemExit(1)

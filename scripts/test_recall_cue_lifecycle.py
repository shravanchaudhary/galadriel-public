#!/usr/bin/env python3
"""Cue lifecycle: LRU eviction, saturation skip, Stage-2 negative veto.

Covers the guards that keep cue arrays from growing into looser matching or
into unbounded Stage-2 cost:
  - evict_lru_cues drops cues that never won a Stage-2 match, not the oldest-added
  - cue_is_saturated rejects near-duplicate appends that max() would ignore
  - verify_recall_candidate_rerank vetoes when a recorded misfire outscores the
    best positive (so tune_recall(applicable=false) changes behaviour)
  - the cosine pre-rank caps cross-encoder passes per candidate, and escalates
    to the exact positive max when the cap could fake a negative veto
  - recall_system_armed disarms the whole pipeline (fail-closed, no embed
    fallback) when rerank mode has no working reranker

The veto cases use a stub scorer so they run without the reranker GGUF.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["RECALL_STAGE2_MODE"] = "rerank"
os.environ["RECALL_SLM_VERIFY"] = "1"

import harness.recall as recall_mod  # noqa: E402
from harness.recall import (  # noqa: E402
    cue_is_saturated,
    cue_key,
    evict_lru_cues,
    recall_system_armed,
    scan_text_for_recalls,
    verify_recall_candidate_rerank,
)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


class StubReranker:
    """best_match over a fixed doc→score table (no GGUF needed)."""

    def __init__(self, scores: dict[str, float]) -> None:
        self.scores = scores
        self.calls = 0

    def best_match(self, query: str, docs: list[str], *, stop_at: float | None = None):
        cleaned = [d.strip() for d in docs if isinstance(d, str) and d.strip()]
        if not cleaned:
            return None
        best, best_doc = 0.0, cleaned[0]
        for doc in cleaned:
            self.calls += 1
            score = self.scores.get(doc, 0.0)
            if score > best:
                best, best_doc = score, doc
            if stop_at is not None and best > stop_at:
                break
        return best, best_doc


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


def _recall(negatives: list[str]) -> dict:
    return {
        "recall_id": "t_veto",
        "instruction": "User is teaching a durable preference.",
        "positive_examples": ["please remember that I like tea"],
        "negative_examples": negatives,
    }


def test_negative_veto() -> None:
    chunk = "do you remember when we picked MongoDB?"
    pos = "please remember that I like tea"

    # No negatives recorded → positives alone decide, and the winning cue is reported.
    out: dict = {}
    ok, reason = verify_recall_candidate_rerank(
        chunk, _recall([]), reranker=StubReranker({pos: 0.80}), out=out
    )
    _assert(ok, f"expected accept without negatives, got {reason}")
    _assert(out.get("matched_example") == pos, f"matched_example missing: {out}")

    # A recorded misfire that outscores the best positive vetoes the fire.
    ok, reason = verify_recall_candidate_rerank(
        chunk, _recall([chunk]), reranker=StubReranker({pos: 0.80, chunk: 0.97})
    )
    _assert(not ok, f"expected negative veto, got accept ({reason})")
    _assert(reason.startswith("rerank_neg_veto:"), f"unexpected reason {reason}")

    # A weaker negative must not veto a clear positive.
    out = {}
    ok, reason = verify_recall_candidate_rerank(
        chunk, _recall([chunk]), reranker=StubReranker({pos: 0.80, chunk: 0.70}), out=out
    )
    _assert(ok, f"weak negative should not veto, got {reason}")
    _assert(out.get("matched_example") == pos, "accept should still report the cue")

    # Below the accept floor, negatives are never scored (no wasted passes).
    stub = StubReranker({pos: 0.40})
    ok, reason = verify_recall_candidate_rerank(chunk, _recall([chunk]), reranker=stub)
    _assert(not ok, f"expected sub-threshold reject, got {reason}")
    _assert(reason.startswith("rerank:"), f"expected positive-side reject, got {reason}")
    _assert(stub.calls <= 2, f"negatives scored on a rejected candidate ({stub.calls})")
    print("ok negative_veto")


def test_prerank_bounds_passes() -> None:
    """Cue count must not drive the number of cross-encoder passes."""
    recall = {
        "recall_id": "t_prerank",
        "instruction": "User is teaching a durable preference.",
        "positive_examples": [f"please remember that I like drink number {i}" for i in range(40)],
        "negative_examples": [],
    }
    stub = StubReranker({})
    ok, reason = verify_recall_candidate_rerank(
        "do you remember when we picked MongoDB?", recall, reranker=stub
    )
    _assert(not ok, f"unrelated chunk should reject, got {reason}")
    budget = recall_mod._STAGE2_PRERANK_K + 1  # cues + instruction
    _assert(stub.calls <= budget, f"scored {stub.calls} docs, budget is {budget}")
    print("ok prerank_bounds_passes")


def test_prerank_veto_escalation() -> None:
    """A pre-ranked (lower-bound) positive must not manufacture a negative veto.

    The strong cue is deliberately the least similar by cosine, so the budget
    hides it and only the escalation path can recover the true positive max.
    """
    chunk = "do you remember when we picked MongoDB?"
    near = "do you remember when we chose MongoDB?"
    also_near = "do you recall the MongoDB decision?"
    strong = "quarterly tax filing deadlines for small enterprises"
    negative = "what did we agree on regarding the new API design?"
    recall = {
        "recall_id": "t_escalate",
        "instruction": "User is recalling a past decision.",
        "positive_examples": [near, also_near, strong],
        "negative_examples": [negative],
    }

    ranked = recall_mod._prerank_cues(chunk, recall["positive_examples"], 2)
    _assert(strong not in ranked, f"premise broken: budget kept the strong cue ({ranked})")

    out: dict = {}
    ok, reason = verify_recall_candidate_rerank(
        chunk,
        recall,
        reranker=StubReranker({near: 0.66, also_near: 0.66, strong: 0.90, negative: 0.70}),
        out=out,
    )
    _assert(ok, f"escalation should recover the hidden positive, got {reason}")
    _assert(out.get("matched_example") == strong, f"expected the strong cue, got {out}")
    print("ok prerank_veto_escalation")


def test_disarmed_without_reranker() -> None:
    """No reranker in rerank mode → whole system off, never an embed fallback."""
    orig = recall_mod._get_reranker
    recall_mod._get_reranker = lambda: None
    try:
        _assert(not recall_system_armed(), "system must disarm without a reranker")
        matches = scan_text_for_recalls(
            "please remember that I like tea", [_recall([])]
        )
        _assert(matches == [], "Stage-1 must not propose while disarmed")
        ok, reason = verify_recall_candidate_rerank(
            "please remember that I like tea", _recall([])
        )
        _assert(not ok, "Stage-2 must fail-closed while disarmed")
        _assert(
            reason.startswith("stage2_disarmed"),
            f"expected stage2_disarmed, got {reason}",
        )
    finally:
        recall_mod._get_reranker = orig
    print("ok disarmed_without_reranker")


def main() -> int:
    test_cue_key_stable()
    test_evict_prefers_never_used()
    test_evict_is_lru_not_fifo()
    test_unstamped_insert_is_its_own_victim()
    test_evict_noop_under_cap()
    test_negative_veto()
    test_prerank_bounds_passes()
    test_prerank_veto_escalation()
    test_saturation_skips_near_duplicates()
    test_disarmed_without_reranker()
    print("ok recall_cue_lifecycle")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as e:
        print(f"ASSERT: {e}", file=sys.stderr)
        raise SystemExit(1)

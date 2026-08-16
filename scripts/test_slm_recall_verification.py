#!/usr/bin/env python3
"""Benchmark Stage-2 recall verification (embedding pos−neg margin).

Default Stage-2 does NOT ask a tiny IT model YES/NO — those latch onto a
completion token. This suite scores FastEmbed max(pos)−max(neg) (+ junk filter).
Exit non-zero if metrics fall below thresholds.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("RECALL_STAGE2_MODE", "embed")
os.environ.setdefault("RECALL_SLM_VERIFY", "1")

from harness.recall import (  # noqa: E402
    _is_stage2_junk,
    _load_system_recalls,
    _stage2_mode,
    verify_recall_candidate_embed,
    verify_recall_candidate_slm,
)


# (recall_id, text, expect_yes) — held-out paraphrases / near-misses
CASES: list[tuple[str, str, bool]] = [
    ("sys_fact_lookup", "remind me what name was on yesterday's email?", True),
    ("sys_fact_lookup", "when did we choose MongoDB again?", True),
    ("sys_fact_lookup", "what was last month's cloud bill?", True),
    ("sys_fact_lookup", "what API design choice did we settle on?", True),
    ("sys_fact_lookup", "what do you think is the best way to write this function?", False),
    ("sys_fact_lookup", "can you write a script to scrape this website?", False),
    ("sys_fact_lookup", "tell me a joke about a programmer", False),
    ("sys_fact_lookup", "let's plan out the architecture for the new service", False),
    ("sys_procedure", "steps to ship this app to staging please?", True),
    ("sys_procedure", "we hit OOM — what's the recovery checklist?", True),
    ("sys_procedure", "how should I add a DB migration the usual way?", True),
    ("sys_procedure", "hello there, how are you doing today?", False),
    ("sys_procedure", "create a new file called test.py", False),
    ("sys_procedure", "what is the capital of France?", False),
    ("sys_learn_recall", "please remember that I prefer dark mode in editors", True),
    ("sys_learn_recall", "from now on always use type hints in python", True),
    ("sys_learn_recall", "save this rule: no emoji in commit messages", True),
    ("sys_learn_recall", "can you remember bill gates is no longer the godfather of capitalism", True),
    ("sys_learn_recall", "can you remember i like mangoes", True),
    ("sys_learn_recall", "[Tower]: can you remember i like mangoes", True),
    ("sys_learn_recall", "do you remember when we decided to switch to MongoDB?", False),
    ("sys_learn_recall", "what did we agree on regarding the new API design?", False),
    ("sys_learn_recall", "hello how are you today", False),
    ("sys_learn_recall", "why are you not creating semantic recall as well ?", False),
    ("sys_architecture", "how do memory tiers interact with the worker loop?", True),
    ("sys_architecture", "walk me through board files and compaction", True),
    ("sys_architecture", "write a unit test for the login form", False),
    # Tool-output / markup noise that Stage-1 falsely proposes (must stay NO).
    ("sys_identity", "<!doctype html>", False),
    ("sys_identity", "<body>", False),
    ("sys_credentials", "--bg: #ffffff;", False),
    ("sys_learn_recall", "<head>", False),
    ("sys_architecture", "read_file", False),
    ("sys_personal_tools", "Written 7 bytes to state/worker_control.md", False),
    ("sys_fact_lookup", '{"path": "state/plan/2026-08-10.html"}', False),
    ("sys_deferred_work", "<title>Progress · 2026-08-10</title>", False),
]


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_junk_heuristic() -> None:
    _assert(_is_stage2_junk("read_file"), "bare tool should be junk")
    _assert(_is_stage2_junk("<!doctype html>"), "doctype should be junk")
    _assert(_is_stage2_junk("Written 7 bytes to state/x.md"), "write ack should be junk")
    _assert(not _is_stage2_junk("can you remember i like mangoes"), "teaching not junk")
    print("ok junk_heuristic")


def test_configured_mode_routes() -> None:
    """verify_recall_candidate_slm dispatches to the configured Stage-2 backend."""
    recall = {
        "recall_id": "t",
        "instruction": "User teaching durable prefs.",
        "positive_examples": ["please remember that I like tea"],
        "negative_examples": ["do you remember when we met?"],
    }
    mode = _stage2_mode()
    expected_prefix = {"embed": "embed_", "rerank": "rerank:", "logit": "slm_logit"}[mode]
    ok, reason = verify_recall_candidate_slm(
        "please remember that I like green tea", recall
    )
    _assert(ok, f"expected {mode} accept, got {reason}")
    _assert(
        reason.startswith(expected_prefix),
        f"expected {mode} reason ({expected_prefix}...), got {reason}",
    )
    print(f"ok mode_routes ({mode})")


def main() -> int:
    test_junk_heuristic()
    test_configured_mode_routes()

    recalls = {r["recall_id"]: r for r in _load_system_recalls()}

    tp = fp = tn = fn = 0
    fail_open = 0
    latencies: list[float] = []
    failures: list[str] = []

    for recall_id, text, expect_yes in CASES:
        recall = recalls.get(recall_id)
        if recall is None:
            failures.append(f"missing recall {recall_id}")
            continue
        t0 = time.perf_counter()
        ok, reason = verify_recall_candidate_embed(text, recall)
        latencies.append(time.perf_counter() - t0)
        if reason.startswith("stage2_error") or reason in (
            "stage2_disabled",
            "slm_unavailable",
            "slm_disabled",
        ):
            fail_open += 1

        if expect_yes and ok:
            tp += 1
        elif expect_yes and not ok:
            fn += 1
            failures.append(f"FN {recall_id}: {text!r} → {reason}")
        elif (not expect_yes) and (not ok):
            tn += 1
        else:
            fp += 1
            failures.append(f"FP {recall_id}: {text!r} → {reason}")

    total = tp + tn + fp + fn
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall_m = tp / (tp + fn) if (tp + fn) else 0.0
    accuracy = (tp + tn) / total if total else 0.0
    avg_ms = (sum(latencies) / len(latencies) * 1000) if latencies else 0.0
    p95_ms = sorted(latencies)[int(0.95 * (len(latencies) - 1))] * 1000 if latencies else 0.0

    print(
        f"cases={total} TP={tp} TN={tn} FP={fp} FN={fn} "
        f"precision={precision:.2f} recall={recall_m:.2f} accuracy={accuracy:.2f}"
    )
    print(
        f"avg_latency_ms={avg_ms:.1f} p95_latency_ms={p95_ms:.1f} "
        f"fail_open={fail_open}"
    )
    for line in failures:
        print("FAIL:", line)

    _assert(total >= 20, "need >=20 cases")
    _assert(precision >= 0.85, f"precision {precision:.2f} < 0.85")
    _assert(recall_m >= 0.85, f"recall {recall_m:.2f} < 0.85")
    _assert(fail_open == 0, f"unexpected fail-open count {fail_open}")
    print("ok stage2_embed_verification")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as e:
        print(f"ASSERT: {e}", file=sys.stderr)
        raise SystemExit(1)

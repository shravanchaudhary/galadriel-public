#!/usr/bin/env python3
"""Benchmark Stage-2 SLM recall verification (local Gemma 270M).

Uses YES/NO logit margin with few-shot examples from each recall.
Held-out paraphrases (not copied from positive_examples) measure generalization.
Exit non-zero if metrics fall below thresholds.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.recall import (  # noqa: E402
    _build_slm_verify_prompt,
    _load_system_recalls,
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
    ("sys_learn_recall", "do you remember when we decided to switch to MongoDB?", False),
    ("sys_learn_recall", "what did we agree on regarding the new API design?", False),
    ("sys_learn_recall", "hello how are you today", False),
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


def test_prompt_includes_examples() -> None:
    recall = {
        "instruction": "Look up past facts.",
        "positive_examples": ["what was the bill?"],
        "negative_examples": ["tell me a joke"],
    }
    prompt = _build_slm_verify_prompt("cloud cost last month?", recall)
    _assert("Answer: YES" in prompt, "missing positive few-shot")
    _assert("Answer: NO" in prompt, "missing negative few-shot")
    _assert("TEXT: <!doctype html>\nAnswer: NO" in prompt, "missing global hard-negative")
    _assert("TEXT: read_file\nAnswer: NO" in prompt, "missing tool-name hard-negative")
    _assert("YES only if TEXT clearly asks" in prompt, "missing YES rule")
    _assert(prompt.endswith("Answer:"), "missing query slot")
    print("ok prompt_shape")


def main() -> int:
    test_prompt_includes_examples()

    from local_llm import LocalLLMClient, default_model_path, ensure_model

    path = default_model_path()
    if not path.exists():
        print(f"GGUF missing at {path}; downloading…")
        ensure_model()

    client = LocalLLMClient(in_process=True)
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
        ok, reason = verify_recall_candidate_slm(text, recall, client=client)
        latencies.append(time.perf_counter() - t0)
        if reason.startswith("slm_error") or reason in ("slm_unavailable", "slm_disabled"):
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
    _assert(precision >= 0.70, f"precision {precision:.2f} < 0.70")
    _assert(recall_m >= 0.70, f"recall {recall_m:.2f} < 0.70")
    _assert(fail_open == 0, f"unexpected fail-open count {fail_open}")
    print("ok slm_recall_verification")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as e:
        print(f"ASSERT: {e}", file=sys.stderr)
        raise SystemExit(1)

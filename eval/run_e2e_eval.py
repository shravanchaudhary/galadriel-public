#!/usr/bin/env python3
"""End-to-end recall eval: Stage-1 propose + Stage-2 verify on the real path.

Drives `scan_text_for_recalls` then `filter_matches_with_judge` rather than
scoring (chunk, recall) pairs in isolation. Attributes every false negative to
either candidate selection (Stage-1 miss) or verification (Stage-2 reject).
Leave-one-out drops the exact eval chunk from positive_examples so cue_audit
self-matches cannot inflate results. Requires a credential for whichever
provider serves RECALL_JUDGE_MODEL; note this module's own judge helper is
still pinned to Gemini, unlike production.

Usage:
  venv/bin/python -m eval.run_e2e_eval --leave-one-out
  venv/bin/python -m eval.run_e2e_eval --metamorphic-only
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import sys
import time
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("RECALL_JUDGE_VERIFY", "1")

from eval.common import (  # noqa: E402
    classification_metrics,
    eprint,
    latency_summary,
    md_table,
    write_results,
)
from eval.dataset import build_dataset, dataset_stats, load_system_recalls  # noqa: E402

# Metamorphic probes: same activation, different surface form. expected=True
# means the named recall should survive both stages.
METAMORPHIC_CASES: list[dict] = [
    # Slot substitution — fruit preference teach
    {
        "chunk": "please remember that I like oranges",
        "recall_id": "sys_learn_recall",
        "expected": True,
        "source": "metamorphic",
        "family": "slot_sub",
    },
    {
        "chunk": "can you remember my favorite fruit is pineapple",
        "recall_id": "sys_learn_recall",
        "expected": True,
        "source": "metamorphic",
        "family": "slot_sub",
    },
    # Teach vs ask
    {
        "chunk": "remember I prefer dark mode",
        "recall_id": "sys_learn_recall",
        "expected": True,
        "source": "metamorphic",
        "family": "teach",
    },
    {
        "chunk": "do you remember my dark mode preference?",
        "recall_id": "sys_learn_recall",
        "expected": False,
        "source": "metamorphic",
        "family": "ask_not_teach",
    },
    # Short identity queries (must clear Stage-1 min-words for user source)
    {
        "chunk": "who are you?",
        "recall_id": "sys_identity",
        "expected": True,
        "source": "metamorphic",
        "family": "short_query",
        "segment_source": "user",
    },
    {
        "chunk": "what are you?",
        "recall_id": "sys_identity",
        "expected": True,
        "source": "metamorphic",
        "family": "short_query",
        "segment_source": "user",
    },
    {
        "chunk": "are you real?",
        "recall_id": "sys_identity",
        "expected": True,
        "source": "metamorphic",
        "family": "short_query",
        "segment_source": "user",
    },
    # Typo / punctuation
    {
        "chunk": "remeber i like spicy food",
        "recall_id": "sys_learn_recall",
        "expected": True,
        "source": "metamorphic",
        "family": "typo",
    },
    {
        "chunk": "who are you",
        "recall_id": "sys_identity",
        "expected": True,
        "source": "metamorphic",
        "family": "punctuation",
        "segment_source": "user",
    },
    # One-off reminder must NOT fire learn
    {
        "chunk": "remind me to turn off the lights in 10 minutes",
        "recall_id": "sys_learn_recall",
        "expected": False,
        "source": "metamorphic",
        "family": "reminder_not_learn",
    },
]


def _leave_one_out_recalls(recalls: list[dict], chunk: str, recall_id: str) -> list[dict]:
    """Drop the exact eval chunk from the target recall's positive examples."""
    out = []
    needle = (chunk or "").strip()
    for r in recalls:
        r2 = copy.deepcopy(r)
        if r2.get("recall_id") == recall_id:
            pos = [
                e for e in (r2.get("positive_examples") or [])
                if not (isinstance(e, str) and e.strip() == needle)
            ]
            r2["positive_examples"] = pos
        out.append(r2)
    return out


def _attach_dataset_dicts(cases: list[dict], recalls: dict[str, dict]) -> list[dict]:
    out = []
    for c in cases:
        rid = c["recall_id"]
        if rid not in recalls:
            raise KeyError(f"unknown recall_id {rid!r}")
        row = dict(c)
        row["recall_dict"] = recalls[rid]
        out.append(row)
    return out


def _verify(proposed: list[dict]) -> tuple[list[dict], list[dict]]:
    """Stage-2 via the same judge dispatch the agent uses."""
    from harness.recall import filter_matches_with_judge

    if not proposed:
        return [], []
    return asyncio.run(filter_matches_with_judge(proposed, provider=_judge_provider()))


@lru_cache(maxsize=1)
def _judge_provider():
    from harness.providers import GeminiProvider

    return GeminiProvider()


def run_case(
    case: dict,
    catalog: list[dict],
    *,
    leave_one_out: bool,
) -> dict:
    from harness.recall import scan_text_for_recalls

    chunk = case["chunk"]
    recall_id = case["recall_id"]
    expected = bool(case["expected"])
    segment_source = case.get("segment_source") or "user"

    recalls = (
        _leave_one_out_recalls(catalog, chunk, recall_id)
        if leave_one_out
        else catalog
    )

    t0 = time.perf_counter()
    proposed = scan_text_for_recalls(
        chunk,
        recalls,
        segments=[{"text": chunk, "source": segment_source}],
    )
    proposed_ids = {m.get("recall_id") for m in proposed}
    stage1_hit = recall_id in proposed_ids

    verified, rejected = _verify(proposed)
    verified_ids = {m.get("recall_id") for m in verified}
    rejected_by_id = {m.get("recall_id"): m for m in rejected}
    predicted = recall_id in verified_ids
    latency = time.perf_counter() - t0

    fn_stage = None
    if expected and not predicted:
        fn_stage = "candidate_selection" if not stage1_hit else "verification"

    reason = None
    if recall_id in rejected_by_id:
        reason = rejected_by_id[recall_id].get("judge_reason")
    elif predicted:
        for m in verified:
            if m.get("recall_id") == recall_id:
                reason = m.get("judge_reason")
                break

    return {
        "chunk": chunk,
        "recall_id": recall_id,
        "source": case.get("source"),
        "family": case.get("family"),
        "expected": expected,
        "predicted": predicted,
        "stage1_hit": stage1_hit,
        "fn_stage": fn_stage,
        "judge_reason": reason,
        "proposed_ids": sorted(x for x in proposed_ids if x),
        "verified_ids": sorted(x for x in verified_ids if x),
        "latency_ms": round(latency * 1000, 1),
    }


def build_markdown(payload: dict) -> str:
    lines = ["# End-to-end recall eval", ""]
    m = payload["metrics"]
    lines.append(
        f"P={m['precision']:.3f} R={m['recall']:.3f} F1={m['f1']:.3f} "
        f"acc={m['accuracy']:.3f}  "
        f"(tp={m['tp']} fp={m['fp']} tn={m['tn']} fn={m['fn']})"
    )
    lines.append("")
    lines.append(f"leave_one_out={payload['config']['leave_one_out']}")
    lines.append("")
    by_src = payload.get("metrics_by_source") or {}
    if by_src:
        rows = [
            [src, s["precision"], s["recall"], s["f1"], s["tp"], s["fp"], s["fn"]]
            for src, s in sorted(by_src.items())
        ]
        lines.append(md_table(
            ["source", "P", "R", "F1", "tp", "fp", "fn"], rows
        ))
        lines.append("")
    fn = payload.get("fn_attribution") or {}
    lines.append(
        f"FN attribution: candidate_selection={fn.get('candidate_selection', 0)}  "
        f"verification={fn.get('verification', 0)}"
    )
    lat = payload.get("latency") or {}
    lines.append(f"latency: mean={lat.get('mean_ms')}ms p95={lat.get('p95_ms')}ms")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--leave-one-out", action="store_true",
                    help="drop exact eval chunk from target recall positives")
    ap.add_argument("--metamorphic-only", action="store_true")
    ap.add_argument("--max-cases", type=int, default=0)
    ap.add_argument("--include-metamorphic", action="store_true", default=True)
    ap.add_argument("--no-metamorphic", action="store_true")
    args = ap.parse_args()

    recalls_map = load_system_recalls()
    catalog = list(recalls_map.values())

    if args.metamorphic_only:
        cases = _attach_dataset_dicts(METAMORPHIC_CASES, recalls_map)
    else:
        cases = build_dataset()
        if not args.no_metamorphic:
            cases = cases + _attach_dataset_dicts(METAMORPHIC_CASES, recalls_map)

    if args.max_cases:
        cases = cases[: args.max_cases]

    stats = dataset_stats(cases)
    print(f"dataset: {json.dumps(stats)}")
    print(f"leave_one_out={args.leave_one_out}")

    rows: list[dict] = []
    for i, case in enumerate(cases):
        row = run_case(case, catalog, leave_one_out=args.leave_one_out)
        rows.append(row)
        if (i + 1) % 25 == 0:
            eprint(f"  {i + 1}/{len(cases)}")

    metrics = classification_metrics(rows)
    metrics_by_source = {
        src: classification_metrics([r for r in rows if r["source"] == src])
        for src in sorted({r["source"] for r in rows if r.get("source")})
    }
    fn_attribution = {
        "candidate_selection": sum(1 for r in rows if r.get("fn_stage") == "candidate_selection"),
        "verification": sum(1 for r in rows if r.get("fn_stage") == "verification"),
    }
    latencies = [r["latency_ms"] / 1000.0 for r in rows]

    payload = {
        "benchmark": "e2e_recall",
        "dataset": stats,
        "config": {
            "leave_one_out": bool(args.leave_one_out),
            "metamorphic_only": bool(args.metamorphic_only),
        },
        "metrics": metrics,
        "metrics_by_source": metrics_by_source,
        "fn_attribution": fn_attribution,
        "latency": latency_summary(latencies),
        "rows": rows,
    }
    write_results("e2e", payload, build_markdown(payload))
    print(
        f"P={metrics['precision']:.3f} R={metrics['recall']:.3f} "
        f"F1={metrics['f1']:.3f} FN_sel={fn_attribution['candidate_selection']} "
        f"FN_ver={fn_attribution['verification']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

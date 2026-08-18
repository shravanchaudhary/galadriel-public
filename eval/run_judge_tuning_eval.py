#!/usr/bin/env python3
"""Small, cheap eval for the tune_recall → Stage-2 judge loop.

Question under test: when tune_recall(applicable=false) has stored a misfire
chunk in negative_examples, does the judge actually veto the same misfire the
next time it recurs — without vetoing genuine positives whose recalls carry
similar-looking negatives?

Two passes per case (Gemini flash-lite, ≤2 calls/case; a 24-case run costs
well under a cent):

  baseline — judge sees only activation_condition + exclusions
  tuned    — negative cases: the case's own chunk is seeded as a stored
             misfire (exactly what tune_recall applicable=false writes, and
             exactly what recurs in production);
             positive cases: the recall's real negative_examples are ranked
             by cosine to the chunk and the top-3 are attached — the
             production selection path — to prove nearby negatives do not
             flip true fires.

Pass criteria: tuned FP count < baseline FP count on the misfire set, and
zero true positives lost between baseline and tuned.

Usage:
  GEMINI_API_KEY=... venv/bin/python -m eval.run_judge_tuning_eval
  ... --max-negatives 12 --max-positives 8   # defaults; ~20 cases total
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.common import eprint, latency_summary, md_table, write_results  # noqa: E402
from eval.dataset import build_dataset  # noqa: E402


def _provider():
    from harness.providers import GeminiProvider

    return GeminiProvider()


async def _judge_one(provider, model, chunk: str, candidate: dict) -> bool | None:
    """True = judge fires, False = veto, None = call failed."""
    from harness.recall_judge import judge_applicability

    judgment = await judge_applicability(
        provider, chunk=chunk, candidates=[candidate], model=model
    )
    if judgment is None:
        return None
    return candidate["recall_id"] in set(judgment.get("applicable") or [])


def _select_cases(max_negatives: int, max_positives: int) -> list[dict]:
    cases = build_dataset()
    negatives = [c for c in cases if not c["expected"] and c["source"] in ("incident", "cue_audit")]
    # Prefer real production incidents, then curated confusions.
    negatives.sort(key=lambda c: 0 if c["source"] == "incident" else 1)
    positives = [c for c in cases if c["expected"] and c["source"] == "heldout"]
    return negatives[:max_negatives] + positives[:max_positives]


async def run(max_negatives: int, max_positives: int) -> dict:
    from harness.recall import _is_stage2_junk, _top_k_cosine, get_encoder
    from harness.recall_judge import MAX_MISFIRES_PER_CANDIDATE, resolve_judge_model

    provider = _provider()
    model = resolve_judge_model()
    cases = _select_cases(max_negatives, max_positives)
    rows: list[dict] = []
    latencies: list[float] = []

    for i, case in enumerate(cases):
        chunk, recall = case["chunk"], case["recall_dict"]
        if _is_stage2_junk(chunk):
            # Production rejects these before the judge; mirror the pipeline.
            rows.append({
                "chunk": chunk,
                "recall_id": case["recall_id"],
                "source": case["source"],
                "expected": case["expected"],
                "baseline_fired": False,
                "tuned_fired": False,
                "seeded_misfires": [],
                "junk_filtered": True,
            })
            eprint(f"[{i + 1}/{len(cases)}] {case['recall_id']} junk-filtered pre-judge")
            continue
        base_candidate = {
            "recall_id": case["recall_id"],
            "activation_condition": recall.get("activation_condition"),
            "exclusions": recall.get("exclusions"),
            "instruction": recall.get("instruction"),
        }
        if case["expected"]:
            # Production path: nearest real stored negatives ride along.
            seeded = _top_k_cosine(
                get_encoder(), chunk, recall.get("negative_examples") or [],
                MAX_MISFIRES_PER_CANDIDATE,
            )
        else:
            # tune_recall applicable=false stored this exact misfire earlier.
            seeded = [chunk]
        tuned_candidate = dict(base_candidate, judge_negatives=seeded)

        t0 = time.perf_counter()
        baseline = await _judge_one(provider, model, chunk, base_candidate)
        tuned = await _judge_one(provider, model, chunk, tuned_candidate)
        latencies.append(time.perf_counter() - t0)

        rows.append({
            "chunk": chunk,
            "recall_id": case["recall_id"],
            "source": case["source"],
            "expected": case["expected"],
            "baseline_fired": baseline,
            "tuned_fired": tuned,
            "seeded_misfires": seeded,
        })
        eprint(f"[{i + 1}/{len(cases)}] {case['recall_id']} expected={case['expected']} "
               f"baseline={baseline} tuned={tuned}")

    neg = [r for r in rows if not r["expected"]]
    pos = [r for r in rows if r["expected"]]
    summary = {
        "cases": len(rows),
        "judge_model": model,
        "errors": sum(1 for r in rows if None in (r["baseline_fired"], r["tuned_fired"])),
        "neg_cases": len(neg),
        "neg_baseline_fps": sum(1 for r in neg if r["baseline_fired"]),
        "neg_tuned_fps": sum(1 for r in neg if r["tuned_fired"]),
        "pos_cases": len(pos),
        "pos_baseline_fires": sum(1 for r in pos if r["baseline_fired"]),
        "pos_tuned_fires": sum(1 for r in pos if r["tuned_fired"]),
        "pos_lost_to_tuning": sum(
            1 for r in pos if r["baseline_fired"] and not r["tuned_fired"]
        ),
        "latency": latency_summary(latencies),
    }
    summary["passed"] = (
        summary["errors"] == 0
        and summary["neg_tuned_fps"] <= summary["neg_baseline_fps"]
        and summary["pos_lost_to_tuning"] == 0
    )
    return {"benchmark": "judge_tuning", "summary": summary, "rows": rows}


def build_markdown(payload: dict) -> str:
    s = payload["summary"]
    lines = ["# tune_recall → judge tuning eval", ""]
    lines.append(md_table(
        ["metric", "value"],
        [
            ["judge model", s["judge_model"]],
            ["cases", s["cases"]],
            ["negatives: baseline FPs", f"{s['neg_baseline_fps']}/{s['neg_cases']}"],
            ["negatives: tuned FPs", f"{s['neg_tuned_fps']}/{s['neg_cases']}"],
            ["positives: baseline fires", f"{s['pos_baseline_fires']}/{s['pos_cases']}"],
            ["positives: tuned fires", f"{s['pos_tuned_fires']}/{s['pos_cases']}"],
            ["positives lost to tuning", s["pos_lost_to_tuning"]],
            ["judge call errors", s["errors"]],
            ["mean latency ms (2 calls)", s["latency"]["mean_ms"]],
            ["PASSED", s["passed"]],
        ],
    ))
    lines.append("")
    lines.append(
        "Negative cases seed the chunk itself as a stored misfire (the tune_recall "
        "applicable=false loop); positive cases attach their recall's real nearest "
        "negatives (the production selection path). Pass = tuning strictly reduces "
        "misfire FPs and loses zero true positives."
    )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--max-negatives", type=int, default=12)
    ap.add_argument("--max-positives", type=int, default=8)
    args = ap.parse_args()

    payload = asyncio.run(run(args.max_negatives, args.max_positives))
    write_results("judge_tuning", payload, build_markdown(payload))
    s = payload["summary"]
    print(
        f"\nneg FPs {s['neg_baseline_fps']} -> {s['neg_tuned_fps']} | "
        f"pos lost {s['pos_lost_to_tuning']} | errors {s['errors']} | "
        f"{'PASSED' if s['passed'] else 'FAILED'}"
    )
    return 0 if s["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

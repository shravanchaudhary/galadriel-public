#!/usr/bin/env python3
"""Mine hard recall cases: scorer disagreements + metamorphic reminders.

Collects cases where dense / lexical / (optional) reranker disagree, plus the
explicit reminder-vs-learn contrast. Writes JSONL suitable for hand adjudication
into train / calibration / test splits.

Usage:
  RECALL_STAGE2_MODE=embed venv/bin/python -m eval.mine_hard_cases
  RECALL_STAGE2_MODE=rerank venv/bin/python -m eval.mine_hard_cases --with-reranker
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("RECALL_SLM_VERIFY", "1")
os.environ.setdefault("RECALL_STAGE2_MODE", "embed")

from eval.common import eprint, write_results  # noqa: E402
from eval.dataset import build_dataset, load_system_recalls  # noqa: E402
from eval.run_e2e_eval import METAMORPHIC_CASES  # noqa: E402


def _lexical_fire(chunk: str, recall: dict) -> bool:
    from harness.recall import _fuzzy_lexical_hit, _lexical_hit

    cues = recall.get("lexical_cues") or []
    return bool(_lexical_hit(chunk, cues) or _fuzzy_lexical_hit(chunk, cues))


def _dense_fire(chunk: str, recall_id: str, recalls: list[dict]) -> bool:
    from harness.recall import scan_text_for_recalls

    # Force semantic-only view by scanning as user (no min-word gate).
    proposed = scan_text_for_recalls(
        chunk,
        recalls,
        segments=[{"text": chunk, "source": "user"}],
    )
    return any(
        m.get("recall_id") == recall_id and m.get("match_source") == "semantic"
        for m in proposed
    )


def _rerank_fire(chunk: str, recall: dict) -> bool | None:
    from harness.recall import verify_recall_candidate_rerank

    try:
        ok, _reason = verify_recall_candidate_rerank(chunk, recall)
    except Exception as e:  # noqa: BLE001
        eprint(f"rerank skip: {e}")
        return None
    return bool(ok)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--with-reranker", action="store_true")
    ap.add_argument("--max-cases", type=int, default=0)
    args = ap.parse_args()

    recalls_map = load_system_recalls()
    catalog = list(recalls_map.values())
    cases = build_dataset()
    if args.max_cases:
        cases = cases[: args.max_cases]

    hard: list[dict] = []
    for i, case in enumerate(cases):
        chunk = case["chunk"]
        rid = case["recall_id"]
        recall = recalls_map[rid]
        lex = _lexical_fire(chunk, recall)
        dense = _dense_fire(chunk, rid, catalog)
        votes = {"lexical": lex, "dense": dense}
        if args.with_reranker:
            rr = _rerank_fire(chunk, recall)
            votes["rerank"] = rr
        # Disagreement: exactly one of the boolean voters is True (ignore None).
        bool_votes = [v for v in votes.values() if isinstance(v, bool)]
        if bool_votes.count(True) == 1 or bool_votes.count(False) == 1 and len(set(bool_votes)) > 1:
            if len(set(bool_votes)) > 1:
                hard.append({
                    "chunk": chunk,
                    "recall_id": rid,
                    "expected": case["expected"],
                    "source": case["source"],
                    "votes": votes,
                    "split_hint": "adjudicate",
                })
        if (i + 1) % 40 == 0:
            eprint(f"  mined {i + 1}/{len(cases)} → {len(hard)} hard")

    # Always include metamorphic reminder + short queries for adjudication.
    for m in METAMORPHIC_CASES:
        hard.append({
            "chunk": m["chunk"],
            "recall_id": m["recall_id"],
            "expected": m["expected"],
            "source": "metamorphic",
            "family": m.get("family"),
            "votes": {},
            "split_hint": "test" if m.get("family") == "reminder_not_learn" else "calibration",
        })

    # Dedup by (recall_id, chunk)
    seen = set()
    unique = []
    for row in hard:
        key = (row["recall_id"], row["chunk"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)

    payload = {
        "benchmark": "hard_cases",
        "count": len(unique),
        "with_reranker": bool(args.with_reranker),
        "rows": unique,
        "notes": (
            "Adjudicate ~150-200 rows into train/calibration/test. "
            "Label reminder cases as expected=false for sys_learn_recall. "
            "Fit local-tier threshold on calibration for max recall at P>=0.95."
        ),
    }
    md = (
        f"# Hard recall cases\n\n"
        f"{len(unique)} candidate rows for adjudication.\n\n"
        f"Reminder case included: "
        f"`remind me to turn off the lights` → sys_learn_recall expected=false.\n"
    )
    write_results("hard_cases", payload, md)
    print(f"hard cases: {len(unique)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

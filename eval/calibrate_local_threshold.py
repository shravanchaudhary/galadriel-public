#!/usr/bin/env python3
"""Calibrate local-tier RECALL_STAGE2_RERANK_THRESHOLD for P >= 0.95.

Sweeps thresholds on leave-one-out e2e rows (or a provided scored JSON), picks
the operating point with maximum recall subject to precision >= --min-precision,
and writes the chosen threshold plus the reranker GGUF sha256 so a model swap
invalidates the calibration.

Usage:
  RECALL_STAGE2_MODE=rerank venv/bin/python -m eval.calibrate_local_threshold
  venv/bin/python -m eval.calibrate_local_threshold --from-json eval/results/e2e_....json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("RECALL_SLM_VERIFY", "1")


def _gguf_hash() -> str | None:
    try:
        from local_llm.config import reranker_path

        path = reranker_path()
        if not path.exists():
            return None
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest()
    except Exception:
        return None


def _metrics_at(rows: list[dict], thr: float) -> dict:
    from eval.common import classification_metrics

    decided = []
    for r in rows:
        score = r.get("score")
        if score is None:
            # Fall back to binary predicted if no score present.
            decided.append({"expected": r["expected"], "predicted": r.get("predicted", False)})
            continue
        decided.append({"expected": r["expected"], "predicted": float(score) >= thr})
    return classification_metrics(decided)


def calibrate(rows: list[dict], *, min_precision: float) -> dict:
    scored = [r for r in rows if r.get("score") is not None]
    if not scored:
        # No continuous scores — report current binary operating point only.
        from eval.common import classification_metrics

        m = classification_metrics(rows)
        return {
            "threshold": None,
            "reason": "no continuous scores in rows",
            "metrics": m,
        }
    candidates = sorted({round(float(r["score"]), 4) for r in scored})
    best = None
    for thr in candidates:
        m = _metrics_at(scored, thr)
        if m["precision"] + 1e-12 < min_precision:
            continue
        if best is None or m["recall"] > best["recall"] or (
            m["recall"] == best["recall"] and m["precision"] > best["precision"]
        ):
            best = {"threshold": thr, **m}
    if best is None:
        # Feasible set empty — pick highest precision point.
        fallback = None
        for thr in candidates:
            m = _metrics_at(scored, thr)
            if fallback is None or m["precision"] > fallback["precision"]:
                fallback = {"threshold": thr, **m}
        return {
            "threshold": None if fallback is None else fallback["threshold"],
            "reason": f"no point reached P>={min_precision}",
            "metrics": fallback,
        }
    return {"threshold": best["threshold"], "reason": "ok", "metrics": best}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from-json", help="existing e2e/stage2 results JSON with rows")
    ap.add_argument("--min-precision", type=float, default=0.95)
    ap.add_argument("--max-cases", type=int, default=0)
    args = ap.parse_args()

    if args.from_json:
        payload = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
        rows = payload.get("rows") or []
    else:
        os.environ["RECALL_STAGE2_MODE"] = "rerank"
        # Collect per-candidate scores via verify_recall_candidate_rerank directly.
        from eval.dataset import build_dataset, load_system_recalls
        from harness.recall import verify_recall_candidate_rerank

        cases = build_dataset()
        if args.max_cases:
            cases = cases[: args.max_cases]
        rows = []
        for case in cases:
            recall = dict(case["recall_dict"])
            # Leave-one-out: drop exact chunk from positives.
            needle = case["chunk"].strip()
            recall["positive_examples"] = [
                e for e in (recall.get("positive_examples") or [])
                if not (isinstance(e, str) and e.strip() == needle)
            ]
            ok, reason = verify_recall_candidate_rerank(case["chunk"], recall)
            score = None
            if reason.startswith("rerank:") or reason.startswith("rerank_"):
                # Parse leading float after first ':' when present.
                import re

                m = re.search(r"(-?\d+\.\d+)", reason)
                if m:
                    score = float(m.group(1))
            rows.append({
                "chunk": case["chunk"],
                "recall_id": case["recall_id"],
                "expected": case["expected"],
                "predicted": bool(ok),
                "score": score,
                "reason": reason,
                "source": case["source"],
            })

    result = calibrate(rows, min_precision=args.min_precision)
    out = {
        "benchmark": "local_threshold_calibration",
        "min_precision": args.min_precision,
        "model_sha256": _gguf_hash(),
        "calibration": result,
        "n_rows": len(rows),
    }
    out_path = REPO_ROOT / "eval" / "results" / "local_threshold_calibration.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2))
    print(f"wrote {out_path}")
    if result.get("threshold") is not None:
        print(
            f"Set RECALL_STAGE2_RERANK_THRESHOLD={result['threshold']} "
            f"(P={result['metrics']['precision']} R={result['metrics']['recall']})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

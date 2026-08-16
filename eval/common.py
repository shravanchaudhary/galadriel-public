"""Shared metrics / memory / results plumbing for the eval CLIs."""

from __future__ import annotations

import datetime as _dt
import json
import platform
import resource
import sys
from pathlib import Path

RESULTS_DIR = Path(__file__).resolve().parent / "results"


def timestamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def peak_rss_mb() -> float:
    """Process-wide peak RSS in MB (monotonic; macOS reports bytes, Linux KB)."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if platform.system().lower() == "darwin":
        return peak / (1024 * 1024)
    return peak / 1024


def current_rss_mb() -> float | None:
    """Current RSS in MB via psutil if available, else None."""
    try:
        import psutil

        return psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:
        return None


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = int(pct * (len(ordered) - 1))
    return ordered[idx]


def latency_summary(latencies_s: list[float]) -> dict:
    if not latencies_s:
        return {"n": 0, "mean_ms": 0.0, "p95_ms": 0.0}
    return {
        "n": len(latencies_s),
        "mean_ms": round(sum(latencies_s) / len(latencies_s) * 1000, 1),
        "p95_ms": round(percentile(latencies_s, 0.95) * 1000, 1),
    }


def classification_metrics(rows: list[dict]) -> dict:
    """rows: [{expected: bool, predicted: bool}]. Positive class = fire."""
    tp = sum(1 for r in rows if r["expected"] and r["predicted"])
    fp = sum(1 for r in rows if not r["expected"] and r["predicted"])
    tn = sum(1 for r in rows if not r["expected"] and not r["predicted"])
    fn = sum(1 for r in rows if r["expected"] and not r["predicted"])
    total = tp + fp + tn + fn
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    accuracy = (tp + tn) / total if total else 0.0
    return {
        "cases": total,
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "accuracy": round(accuracy, 4),
    }


def best_f1_threshold(scored: list[dict]) -> dict:
    """scored: [{expected: bool, score: float}]. Sweep score thresholds for best F1."""
    usable = [r for r in scored if r.get("score") is not None]
    if not usable:
        return {"threshold": None, "f1": 0.0}
    candidates = sorted({r["score"] for r in usable})
    best = {"threshold": None, "f1": -1.0}
    for thr in candidates:
        rows = [{"expected": r["expected"], "predicted": r["score"] >= thr} for r in usable]
        m = classification_metrics(rows)
        if m["f1"] > best["f1"]:
            best = {"threshold": round(float(thr), 4), "f1": m["f1"], **{
                k: m[k] for k in ("precision", "recall", "accuracy")
            }}
    return best


def write_results(prefix: str, payload: dict, markdown: str) -> tuple[Path, Path]:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = timestamp()
    json_path = RESULTS_DIR / f"{prefix}_{ts}.json"
    md_path = RESULTS_DIR / f"{prefix}_{ts}.md"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    md_path.write_text(markdown, encoding="utf-8")
    print(f"\nresults: {json_path}")
    print(f"summary: {md_path}")
    return json_path, md_path


def md_table(headers: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(out)


def eprint(*args) -> None:
    print(*args, file=sys.stderr, flush=True)

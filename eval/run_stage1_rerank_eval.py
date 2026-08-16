#!/usr/bin/env python3
"""Stage-1 axis benchmark: embedding cosine vs reranker cross-scoring.

Scorers, all evaluated on the same labeled dataset (eval/dataset.py):

  baseline        current production approach — FastEmbed BAAI/bge-small-en-v1.5
                  max-cosine vs the recall's positive/negative examples, with the
                  production accept rule (pos >= positive_threshold, relative
                  negative veto). Reuses harness/recall.py functions via import
                  (read-only; requires semantic-router + fastembed + pymongo).
  qwen3-embed     Qwen3-Embedding-0.6B GGUF (llama.cpp, pooling=last, manual
                  <|endoftext|>), same max-cosine scoring + production rule.
  qwen3-reranker  Qwen3-Reranker-0.6B cross-scoring: score(chunk vs each of the
                  recall's positive examples + instruction), take max P(yes).
                  GGUF backend via llama.cpp RANK pooling (classifier head);
                  falls back to transformers CPU (Qwen/Qwen3-Reranker-0.6B,
                  float32 ~2.4 GB RSS) if the GGUF path fails.

For every scorer we report metrics at its default decision rule AND a best-F1
threshold sweep over its raw score, plus mean/p95 per-case latency and RSS.

Usage:
  python -m eval.run_stage1_rerank_eval
  python -m eval.run_stage1_rerank_eval --scorers baseline,qwen3-embed
  python -m eval.run_stage1_rerank_eval --reranker-backend transformers
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.candidates import (  # noqa: E402
    BASELINE_EMBEDDING,
    EMBED_CANDIDATES,
    RERANKER_CANDIDATES,
    ensure_candidate,
    model_path,
)
from eval.common import (  # noqa: E402
    best_f1_threshold,
    classification_metrics,
    current_rss_mb,
    eprint,
    latency_summary,
    md_table,
    peak_rss_mb,
    write_results,
)
from eval.dataset import build_dataset, dataset_stats  # noqa: E402

SCORER_KEYS = ("baseline", "qwen3-embed", "qwen3-reranker")

RERANK_INSTRUCT = (
    "Given a text snippet from an AI agent conversation, judge whether the "
    "Document describes a situation where the snippet applies."
)


def _pos_threshold(recall: dict) -> float:
    try:
        v = float(recall.get("positive_threshold", 0.6))
    except (TypeError, ValueError):
        return 0.6
    return max(0.0, min(1.0, v)) if v == v else 0.6


def _production_rule(pos: float | None, neg: float | None, recall: dict) -> bool:
    """Stage-1 accept rule: positive floor + relative negative veto."""
    if pos is None:
        return False
    if pos < _pos_threshold(recall):
        return False
    if neg is not None and neg >= pos:
        return False
    return True


def _margin_score(pos: float | None, neg: float | None) -> float | None:
    if pos is None:
        return None
    return pos - (neg if neg is not None else 0.0)


# ---------------------------------------------------------------------------
# Scorers. Each returns (predicted: bool, score: float|None) per case.
# ---------------------------------------------------------------------------


class BaselineFastEmbedScorer:
    """Production path, reusing harness.recall (read-only import)."""

    name = "baseline"
    label = BASELINE_EMBEDDING["label"]

    def __init__(self) -> None:
        from harness import recall as hr  # heavy deps: semantic_router, fastembed

        self._hr = hr
        self._encoder = hr.get_encoder(force_type="fastembed")

    def score_case(self, chunk: str, recall: dict) -> tuple[bool, float | None]:
        pos = self._hr._max_cosine(self._encoder, chunk, recall.get("positive_examples") or [])
        negatives = recall.get("negative_examples") or []
        neg = self._hr._max_cosine(self._encoder, chunk, negatives) if negatives else None
        return _production_rule(pos, neg, recall), _margin_score(pos, neg)

    def close(self) -> None:
        pass


class Qwen3EmbedScorer:
    """Qwen3-Embedding-0.6B GGUF via llama.cpp: pooling=last, manual EOS."""

    name = "qwen3-embed"

    def __init__(self, spec: dict, *, n_ctx: int) -> None:
        import llama_cpp
        from llama_cpp import Llama

        from local_llm.engine import detect_runtime

        self.label = spec["label"]
        self._append_eos = spec.get("append_eos") or ""
        rt = detect_runtime()
        self._llm = Llama(
            model_path=str(model_path(spec)),
            n_ctx=n_ctx,
            n_threads=rt["n_threads"],
            n_gpu_layers=0,
            embedding=True,
            pooling_type=llama_cpp.LLAMA_POOLING_TYPE_LAST,
            verbose=False,
        )

    def _embed(self, text: str) -> list[float]:
        return self._llm.embed(text + self._append_eos)

    @staticmethod
    def _cos(a, b) -> float:
        import numpy as np

        va, vb = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
        na, nb = np.linalg.norm(va), np.linalg.norm(vb)
        if na == 0 or nb == 0:
            return 0.0
        return float(np.dot(va, vb) / (na * nb))

    def _max_cos(self, chunk_vec, examples: list[str]) -> float | None:
        cleaned = [e.strip() for e in examples if isinstance(e, str) and e.strip()]
        if not cleaned:
            return None
        return max(self._cos(chunk_vec, self._embed(e)) for e in cleaned)

    def score_case(self, chunk: str, recall: dict) -> tuple[bool, float | None]:
        chunk_vec = self._embed(chunk)
        pos = self._max_cos(chunk_vec, recall.get("positive_examples") or [])
        negatives = recall.get("negative_examples") or []
        neg = self._max_cos(chunk_vec, negatives) if negatives else None
        return _production_rule(pos, neg, recall), _margin_score(pos, neg)

    def close(self) -> None:
        llm = self._llm
        self._llm = None
        del llm
        gc.collect()


# Official Qwen3-Reranker prompt format (from the model card). The classifier
# head (GGUF) / yes-no logits (transformers) score the formatted pair.
_RERANK_PREFIX = (
    "<|im_start|>system\nJudge whether the Document meets the requirements based "
    'on the Query and the Instruct provided. Note that the answer can only be '
    '"yes" or "no".<|im_end|>\n<|im_start|>user\n'
)
_RERANK_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"


def _rerank_prompt(query: str, doc: str) -> str:
    return (
        f"{_RERANK_PREFIX}<Instruct>: {RERANK_INSTRUCT}\n"
        f"<Query>: {query}\n<Document>: {doc}{_RERANK_SUFFIX}"
    )


class Qwen3RerankerGGUFScorer:
    """GGUF reranker via llama.cpp RANK pooling + llama_get_embeddings_seq.

    llama-cpp-python has no /v1/rerank equivalent, so this drives the
    low-level bindings directly. Requires a GGUF converted with the official
    convert_hf_to_gguf.py (cls.output.weight present) — the registered
    Voodisss quant is such a conversion.
    """

    name = "qwen3-reranker"

    def __init__(self, spec: dict, *, n_ctx: int) -> None:
        import llama_cpp
        from llama_cpp import Llama

        from local_llm.engine import detect_runtime

        self.label = spec["label"] + " (GGUF)"
        self._llama_cpp = llama_cpp
        rt = detect_runtime()
        self._llm = Llama(
            model_path=str(model_path(spec)),
            n_ctx=n_ctx,
            n_threads=rt["n_threads"],
            n_gpu_layers=0,
            embedding=True,
            pooling_type=llama_cpp.LLAMA_POOLING_TYPE_RANK,
            verbose=False,
        )
        self._sanity_check()

    def _pair_score(self, query: str, doc: str) -> float:
        """Raw classifier score for one (query, doc) pair → sigmoid prob."""
        import ctypes

        llm = self._llm
        tokens = llm.tokenize(_rerank_prompt(query, doc).encode("utf-8"), add_bos=False, special=True)
        if len(tokens) >= llm.n_ctx():
            tokens = tokens[: llm.n_ctx() - 8]
        llm.reset()
        llm.eval(tokens)
        ptr = self._llama_cpp.llama_get_embeddings_seq(llm._ctx.ctx, 0)
        if not ptr:
            raise RuntimeError("llama_get_embeddings_seq returned NULL (bad reranker GGUF?)")
        raw = float(ctypes.cast(ptr, ctypes.POINTER(ctypes.c_float))[0])
        if not math.isfinite(raw):
            raise RuntimeError(f"non-finite rerank score {raw}")
        return 1.0 / (1.0 + math.exp(-raw))

    def _sanity_check(self) -> None:
        hit = self._pair_score("what is the capital of France?", "The capital of France is Paris.")
        miss = self._pair_score("what is the capital of France?", "Corporate tax rates for small enterprises.")
        if not (hit > miss):
            raise RuntimeError(
                f"reranker GGUF sanity check failed (hit={hit:.4f} <= miss={miss:.4f}); "
                "falling back to transformers backend is recommended"
            )

    def score_case(self, chunk: str, recall: dict) -> tuple[bool, float | None]:
        docs = [e for e in (recall.get("positive_examples") or []) if isinstance(e, str) and e.strip()]
        instruction = (recall.get("instruction") or "").strip()
        if instruction:
            docs.append(instruction)
        if not docs:
            return False, None
        score = max(self._pair_score(chunk, d[:400]) for d in docs)
        return score >= 0.5, score

    def close(self) -> None:
        llm = self._llm
        self._llm = None
        del llm
        gc.collect()


class Qwen3RerankerTransformersScorer:
    """CPU transformers fallback: official yes/no next-token scoring.

    RAM: ~2.4 GB RSS at float32 — fine for a benchmark host, too heavy for a
    4 GB production tenant alongside the app.
    """

    name = "qwen3-reranker"

    def __init__(self, spec: dict) -> None:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        repo = spec["transformers_fallback_repo"]
        self.label = spec["label"] + " (transformers CPU)"
        self._torch = torch
        self._tok = AutoTokenizer.from_pretrained(repo, padding_side="left")
        self._model = AutoModelForCausalLM.from_pretrained(repo, torch_dtype=torch.float32).eval()
        self._yes_id = self._tok.convert_tokens_to_ids("yes")
        self._no_id = self._tok.convert_tokens_to_ids("no")

    def _pair_score(self, query: str, doc: str) -> float:
        torch = self._torch
        inputs = self._tok(_rerank_prompt(query, doc), return_tensors="pt")
        with torch.no_grad():
            logits = self._model(**inputs).logits[0, -1, :]
        pair = torch.stack([logits[self._no_id], logits[self._yes_id]])
        return float(torch.nn.functional.softmax(pair, dim=0)[1])

    def score_case(self, chunk: str, recall: dict) -> tuple[bool, float | None]:
        docs = [e for e in (recall.get("positive_examples") or []) if isinstance(e, str) and e.strip()]
        instruction = (recall.get("instruction") or "").strip()
        if instruction:
            docs.append(instruction)
        if not docs:
            return False, None
        score = max(self._pair_score(chunk, d[:400]) for d in docs)
        return score >= 0.5, score

    def close(self) -> None:
        self._model = None
        self._tok = None
        gc.collect()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def build_scorer(key: str, *, n_ctx: int, reranker_backend: str, skip_download: bool):
    if key == "baseline":
        return BaselineFastEmbedScorer()
    if key == "qwen3-embed":
        spec = EMBED_CANDIDATES["qwen3-embedding-0.6b"]
        _ensure(spec, skip_download)
        return Qwen3EmbedScorer(spec, n_ctx=n_ctx)
    if key == "qwen3-reranker":
        spec = RERANKER_CANDIDATES["qwen3-reranker-0.6b"]
        if reranker_backend in ("auto", "gguf"):
            try:
                _ensure(spec, skip_download)
                return Qwen3RerankerGGUFScorer(spec, n_ctx=n_ctx)
            except Exception as e:  # noqa: BLE001 — fall back if allowed
                if reranker_backend == "gguf":
                    raise
                eprint(f"reranker GGUF backend failed ({e}); falling back to transformers CPU")
        return Qwen3RerankerTransformersScorer(spec)
    raise SystemExit(f"unknown scorer {key!r}; expected one of {SCORER_KEYS}")


def _ensure(spec: dict, skip_download: bool) -> None:
    path = model_path(spec)
    if not path.exists() or path.stat().st_size < 1_000_000:
        if skip_download:
            raise FileNotFoundError(f"GGUF missing at {path} (--skip-download)")
        ensure_candidate(spec)


def run_scorer(scorer, cases: list[dict]) -> dict:
    rows: list[dict] = []
    latencies: list[float] = []
    errors = 0
    for i, case in enumerate(cases):
        t0 = time.perf_counter()
        try:
            predicted, score = scorer.score_case(case["chunk"], case["recall_dict"])
            status = "ok"
        except Exception as e:  # noqa: BLE001 — bookkeep, treat as no-fire
            predicted, score, status = False, None, f"error:{type(e).__name__}"
            errors += 1
        latency = time.perf_counter() - t0
        latencies.append(latency)
        rows.append(
            {
                "chunk": case["chunk"],
                "recall_id": case["recall_id"],
                "source": case["source"],
                "expected": case["expected"],
                "predicted": bool(predicted),
                "score": score,
                "status": status,
                "latency_ms": round(latency * 1000, 1),
            }
        )
        if (i + 1) % 25 == 0:
            eprint(f"  [{scorer.name}] {i + 1}/{len(cases)}")
    return {
        "scorer": scorer.name,
        "label": scorer.label,
        "metrics_default_rule": classification_metrics(rows),
        "metrics_by_source": {
            src: classification_metrics([r for r in rows if r["source"] == src])
            for src in sorted({r["source"] for r in rows})
        },
        "best_f1_threshold": best_f1_threshold(
            [{"expected": r["expected"], "score": r["score"]} for r in rows]
        ),
        "latency": latency_summary(latencies),
        "errors": errors,
        "rows": rows,
    }


def build_markdown(payload: dict) -> str:
    lines = ["# Stage-1 rerank benchmark", ""]
    stats = payload["dataset"]
    lines.append(
        f"{stats['total']} cases ({stats['positive']} positive / {stats['negative']} negative)"
    )
    lines.append("")
    headers = [
        "scorer", "backend", "P", "R", "F1", "acc",
        "best-F1 (swept thr)", "incident FPs blocked",
        "mean ms", "p95 ms", "RSS after MB",
    ]
    rows = []
    for s in payload["scorers"]:
        if "error" in s:
            rows.append([s["scorer"], "-", "-", "-", "-", "-", "-", "-", "-", "-", s["error"][:60]])
            continue
        m = s["metrics_default_rule"]
        b = s["best_f1_threshold"]
        inc = s["metrics_by_source"].get("incident", {})
        inc_str = f"{inc.get('tn', 0)}/{inc.get('cases', 0)}" if inc else "-"
        rows.append([
            s["scorer"], s["label"],
            f"{m['precision']:.3f}", f"{m['recall']:.3f}", f"{m['f1']:.3f}", f"{m['accuracy']:.3f}",
            f"{b.get('f1', 0):.3f} @ {b.get('threshold')}",
            inc_str,
            s["latency"]["mean_ms"], s["latency"]["p95_ms"],
            s.get("rss_after_mb"),
        ])
    lines.append(md_table(headers, rows))
    lines.append("")
    lines.append(
        "Default rule: production positive floor 0.6 + relative negative veto for embedding "
        "scorers; P(yes) >= 0.5 for the reranker. `best-F1` sweeps the raw score (embed margin "
        "pos−neg, reranker max P(yes)). `incident FPs blocked` = true negatives on the real "
        "production false-positive incidents."
    )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scorers", default=",".join(SCORER_KEYS), help=f"comma list of {SCORER_KEYS}")
    ap.add_argument("--skip-download", action="store_true", help="never download; fail scorers with missing weights")
    ap.add_argument("--max-cases", type=int, default=0, help="limit dataset size (0 = all)")
    ap.add_argument("--n-ctx", type=int, default=2048, help="llama.cpp context for embed/rerank GGUFs")
    ap.add_argument(
        "--reranker-backend",
        choices=("auto", "gguf", "transformers"),
        default="auto",
        help="auto = try GGUF (RANK pooling), fall back to transformers CPU",
    )
    args = ap.parse_args()

    wanted = [s.strip() for s in args.scorers.split(",") if s.strip()]
    for s in wanted:
        if s not in SCORER_KEYS:
            raise SystemExit(f"unknown scorer {s!r}; expected one of {SCORER_KEYS}")

    cases = build_dataset()
    if args.max_cases:
        cases = cases[: args.max_cases]
    stats = dataset_stats(cases)
    print(f"dataset: {json.dumps(stats)}")

    results = []
    for key in wanted:
        print(f"\n=== {key} ===")
        rss_before = current_rss_mb()
        try:
            scorer = build_scorer(
                key,
                n_ctx=args.n_ctx,
                reranker_backend=args.reranker_backend,
                skip_download=args.skip_download,
            )
        except Exception as e:  # noqa: BLE001 — keep benchmarking the rest
            eprint(f"scorer {key} unavailable: {e}")
            results.append({"scorer": key, "error": str(e)})
            continue
        res = run_scorer(scorer, cases)
        scorer.close()
        res["rss_before_mb"] = round(rss_before, 1) if rss_before else None
        res["rss_after_mb"] = (lambda v: round(v, 1) if v else None)(current_rss_mb())
        res["peak_rss_mb"] = round(peak_rss_mb(), 1)
        results.append(res)
        m = res["metrics_default_rule"]
        print(
            f"  P={m['precision']:.3f} R={m['recall']:.3f} F1={m['f1']:.3f} acc={m['accuracy']:.3f} "
            f"mean={res['latency']['mean_ms']}ms p95={res['latency']['p95_ms']}ms errors={res['errors']}"
        )

    payload = {
        "benchmark": "stage1_rerank",
        "dataset": stats,
        "config": {
            "n_ctx": args.n_ctx,
            "reranker_backend": args.reranker_backend,
        },
        "scorers": results,
    }
    write_results("stage1", payload, build_markdown(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

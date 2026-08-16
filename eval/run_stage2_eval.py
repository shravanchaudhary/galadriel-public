#!/usr/bin/env python3
"""Stage-2 recall-verification benchmark for small GGUF generative models.

For every candidate model and every dataset case (chunk, recall), asks:
"Would giving this instruction to the agent now lead to a better response,
more likely what the user or the agent itself wants?" — two ways:

  chat  — strict YES/NO chat completion with few-shots drawn from the recall's
          own positive/negative examples (eval chunk excluded to avoid leakage)
  logit — same few-shot prompt in completion form, scored by the YES-vs-NO
          next-token logit margin (pattern from local_llm/engine.py
          yes_no_logit_margin, generalised to multi-variant tokens and
          implemented without logits_all to keep RAM low)

Every model call is guarded by a timeout; timeouts/parse failures/errors are
fail-open (predict True, matching production) and counted separately. A model
that times out is aborted (llama.cpp calls cannot be interrupted safely).

Usage:
  python -m eval.run_stage2_eval                       # all models, both strategies
  python -m eval.run_stage2_eval --models gemma3-270m,qwen3-0.6b
  python -m eval.run_stage2_eval --skip-download --strategies logit
"""

from __future__ import annotations

import argparse
import gc
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.candidates import GEN_CANDIDATES, ensure_candidate, model_path, select_candidates  # noqa: E402
from eval.common import (  # noqa: E402
    classification_metrics,
    current_rss_mb,
    eprint,
    latency_summary,
    md_table,
    peak_rss_mb,
    write_results,
)
from eval.dataset import build_dataset, dataset_stats  # noqa: E402

QUESTION = (
    "Would giving this instruction to the agent now lead to a better response, "
    "more likely what the user or the agent itself wants? Answer YES or NO."
)

# Junk few-shot negatives mirroring harness.recall._SLM_GLOBAL_HARD_NEGATIVES
# (tool noise that Stage-1 falsely proposes). Kept short to save prompt tokens.
GLOBAL_HARD_NEGATIVES = (
    "read_file",
    "<!doctype html>",
    "Written 7 bytes to state/worker_control.md",
)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_YESNO_RE = re.compile(r"\b(YES|NO)\b", re.IGNORECASE)

YES_VARIANTS = ("YES", " YES", "Yes", " Yes", "yes", " yes")
NO_VARIANTS = ("NO", " NO", "No", " No", "no", " no")


def build_fewshot_lines(recall: dict, chunk: str, *, max_each: int = 3) -> list[str]:
    """TEXT/Answer few-shot lines from the recall's own examples, excluding
    the exact eval chunk (leakage guard for cue_audit cases)."""
    lines: list[str] = []
    for ex in GLOBAL_HARD_NEGATIVES:
        if ex.strip() != chunk.strip():
            lines.append(f"TEXT: {ex}\nAnswer: NO")
    shown = 0
    for ex in recall.get("positive_examples") or []:
        if not isinstance(ex, str) or not ex.strip() or ex.strip() == chunk.strip():
            continue
        lines.append(f"TEXT: {ex.strip()[:160]}\nAnswer: YES")
        shown += 1
        if shown >= max_each:
            break
    shown = 0
    for ex in recall.get("negative_examples") or []:
        if not isinstance(ex, str) or not ex.strip() or ex.strip() == chunk.strip():
            continue
        if ex.strip() in GLOBAL_HARD_NEGATIVES:
            continue
        lines.append(f"TEXT: {ex.strip()[:160]}\nAnswer: NO")
        shown += 1
        if shown >= max_each:
            break
    return lines


def build_prompt_header(recall: dict) -> list[str]:
    instruction = (recall.get("instruction") or "").strip()
    return [
        "You are verifying a recall rule for an AI agent.",
        f"The rule would inject this instruction into the agent's context: {instruction[:300]}",
        f"Question: {QUESTION}",
        "Judge each TEXT below. Answer with exactly YES or NO.",
    ]


def build_chat_messages(chunk: str, recall: dict, chat_suffix: str) -> list[dict]:
    parts = build_prompt_header(recall)
    parts.extend(build_fewshot_lines(recall, chunk))
    parts.append(f"TEXT: {chunk.strip()[:400]}")
    parts.append("Answer:" + (chat_suffix or ""))
    return [{"role": "user", "content": "\n".join(parts)}]


def build_completion_prompt(chunk: str, recall: dict) -> str:
    parts = build_prompt_header(recall)
    parts.extend(build_fewshot_lines(recall, chunk))
    parts.append(f"TEXT: {chunk.strip()[:400]}\nAnswer:")
    return "\n".join(parts)


def parse_yes_no(text: str) -> bool | None:
    cleaned = _THINK_RE.sub(" ", text or "")
    m = _YESNO_RE.search(cleaned)
    if not m:
        return None
    return m.group(1).upper() == "YES"


class GGUFJudge:
    """Thin llama-cpp wrapper for this benchmark (chat + yes/no logit margin).

    Uses logits_all=False: eval() then reads the last token's logits straight
    from the context, so the (n_ctx x vocab) score matrix is never allocated.
    """

    def __init__(self, path: Path, *, n_ctx: int, allow_gpu: bool, verbose: bool = False):
        from llama_cpp import Llama

        from local_llm.engine import detect_runtime

        rt = detect_runtime()
        n_gpu_layers = rt["n_gpu_layers"] if allow_gpu else 0
        self._llm = Llama(
            model_path=str(path),
            n_ctx=n_ctx,
            n_threads=rt["n_threads"],
            n_gpu_layers=n_gpu_layers,
            use_mmap=rt["use_mmap"],
            use_mlock=False,
            logits_all=False,
            embedding=False,
            verbose=verbose,
            chat_format=None,  # use the template embedded in the GGUF
        )
        self._yes_ids, self._no_ids = self._variant_token_ids()

    def _variant_token_ids(self) -> tuple[list[int], list[int]]:
        def first_ids(variants) -> list[int]:
            ids = []
            for v in variants:
                toks = self._llm.tokenize(v.encode("utf-8"), add_bos=False)
                if toks:
                    ids.append(int(toks[0]))
            return sorted(set(ids))

        yes_ids = first_ids(YES_VARIANTS)
        no_ids = first_ids(NO_VARIANTS)
        if not yes_ids or not no_ids:
            raise RuntimeError("could not tokenize YES/NO variants")
        return yes_ids, no_ids

    def chat_yes_no(self, messages: list[dict], *, max_tokens: int) -> tuple[bool | None, str]:
        out = self._llm.create_chat_completion(
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.0,
            top_p=1.0,
        )
        text = (out["choices"][0].get("message") or {}).get("content") or ""
        return parse_yes_no(text), text

    def logit_margin(self, prompt: str) -> float:
        """max logit over YES-variant first tokens minus max over NO variants."""
        import numpy as np

        llm = self._llm
        tokens = llm.tokenize(prompt.encode("utf-8"), add_bos=True)
        if not tokens:
            raise ValueError("empty prompt")
        if len(tokens) >= llm.n_ctx():
            tokens = tokens[-(llm.n_ctx() - 8):]
        llm.reset()
        llm.eval(tokens)
        n_vocab = llm.n_vocab() if callable(getattr(llm, "n_vocab", None)) else llm._n_vocab
        logits = np.ctypeslib.as_array(llm._ctx.get_logits(), shape=(n_vocab,))
        yes = max(float(logits[i]) for i in self._yes_ids)
        no = max(float(logits[i]) for i in self._no_ids)
        return yes - no

    def close(self) -> None:
        llm = self._llm
        self._llm = None
        del llm
        gc.collect()


def run_model(
    key: str,
    spec: dict,
    cases: list[dict],
    *,
    strategies: list[str],
    timeout_s: float,
    n_ctx: int,
    allow_gpu: bool,
) -> dict:
    path = model_path(spec)
    rss_before = current_rss_mb()
    t_load = time.perf_counter()
    judge = GGUFJudge(path, n_ctx=n_ctx, allow_gpu=allow_gpu)
    load_s = time.perf_counter() - t_load
    rss_after_load = current_rss_mb()

    result: dict = {
        "key": key,
        "label": spec["label"],
        "hf_repo": spec["hf_repo"],
        "filename": spec["filename"],
        "load_s": round(load_s, 2),
        "rss_before_mb": round(rss_before, 1) if rss_before else None,
        "rss_after_load_mb": round(rss_after_load, 1) if rss_after_load else None,
        "strategies": {},
        "aborted": False,
    }
    # Qwen3 emits a think block even with /no_think; give it headroom.
    chat_max_tokens = 96 if spec.get("chat_suffix") else 8

    pool = ThreadPoolExecutor(max_workers=1)
    aborted = False
    try:
        for strategy in strategies:
            rows: list[dict] = []
            latencies: list[float] = []
            fail_open = {"timeout": 0, "parse_fail": 0, "error": 0}
            for i, case in enumerate(cases):
                if aborted:
                    break
                chunk, recall = case["chunk"], case["recall_dict"]
                if strategy == "chat":
                    fn = lambda: judge.chat_yes_no(  # noqa: E731
                        build_chat_messages(chunk, recall, spec.get("chat_suffix") or ""),
                        max_tokens=chat_max_tokens,
                    )
                else:
                    fn = lambda: judge.logit_margin(build_completion_prompt(chunk, recall))  # noqa: E731

                t0 = time.perf_counter()
                predicted: bool | None = None
                score: float | None = None
                status = "ok"
                try:
                    fut = pool.submit(fn)
                    out = fut.result(timeout=timeout_s)
                    if strategy == "chat":
                        predicted = out[0]
                        if predicted is None:
                            status = "parse_fail"
                    else:
                        score = float(out)
                        predicted = score > 0.0
                except FutureTimeout:
                    status = "timeout"
                    aborted = True  # in-process llama.cpp call cannot be cancelled
                except Exception as e:  # noqa: BLE001 — bookkeep and fail open
                    status = f"error:{type(e).__name__}"
                latency = time.perf_counter() - t0

                if predicted is None:
                    # fail-open, matching production behaviour
                    predicted = True
                    if status == "timeout":
                        fail_open["timeout"] += 1
                    elif status == "parse_fail":
                        fail_open["parse_fail"] += 1
                    else:
                        fail_open["error"] += 1
                if status != "timeout":
                    latencies.append(latency)
                rows.append(
                    {
                        "chunk": chunk,
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
                    eprint(f"  [{key}/{strategy}] {i + 1}/{len(cases)}")

            strat_result = {
                "metrics": classification_metrics(rows),
                "metrics_by_source": {
                    src: classification_metrics([r for r in rows if r["source"] == src])
                    for src in sorted({r["source"] for r in rows})
                },
                "latency": latency_summary(latencies),
                "fail_open": fail_open,
                "completed_cases": len(rows),
                "rows": rows,
            }
            if strategy == "logit":
                strat_result["best_f1_threshold"] = _sweep_margin(rows)
            result["strategies"][strategy] = strat_result
            if aborted:
                break
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        if not aborted:
            judge.close()
        # if aborted, a llama.cpp call may still be running in the pool thread;
        # leaking the model is safer than freeing it under a live call.

    result["aborted"] = aborted
    result["peak_rss_mb"] = round(peak_rss_mb(), 1)
    result["rss_after_run_mb"] = (lambda v: round(v, 1) if v else None)(current_rss_mb())
    return result


def _sweep_margin(rows: list[dict]) -> dict:
    from eval.common import best_f1_threshold

    return best_f1_threshold(
        [{"expected": r["expected"], "score": r["score"]} for r in rows if r["score"] is not None]
    )


def build_markdown(payload: dict) -> str:
    lines = ["# Stage-2 recall verification benchmark", ""]
    stats = payload["dataset"]
    lines.append(
        f"{stats['total']} cases ({stats['positive']} positive / {stats['negative']} negative), "
        f"sources: {', '.join(f'{k}={v['total']}' for k, v in stats['by_source'].items())}"
    )
    lines.append("")
    headers = [
        "model", "strategy", "P", "R", "F1", "acc",
        "incident FPs blocked", "mean ms", "p95 ms", "peak RSS MB", "fail-open", "aborted",
    ]
    rows = []
    for model in payload["models"]:
        for strat, s in model.get("strategies", {}).items():
            m = s["metrics"]
            inc = s["metrics_by_source"].get("incident", {})
            inc_str = f"{inc.get('tn', 0)}/{inc.get('cases', 0)}" if inc else "-"
            fo = s["fail_open"]
            rows.append([
                model["key"], strat,
                f"{m['precision']:.3f}", f"{m['recall']:.3f}", f"{m['f1']:.3f}", f"{m['accuracy']:.3f}",
                inc_str,
                s["latency"]["mean_ms"], s["latency"]["p95_ms"],
                model.get("peak_rss_mb"),
                sum(fo.values()),
                "yes" if model.get("aborted") else "",
            ])
    lines.append(md_table(headers, rows))
    lines.append("")
    lines.append(
        "Positive class = recall fires. `incident FPs blocked` = true negatives on the "
        "real production false-positive incidents (higher is better, max = all incident cases). "
        "`fail-open` = timeouts + parse failures + errors, all counted as YES (production behaviour). "
        "Peak RSS is process-wide and monotonic — run big models in separate invocations for clean numbers."
    )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", help="comma-separated model keys (default: all)")
    ap.add_argument("--skip-download", action="store_true", help="never download; skip models with missing GGUF")
    ap.add_argument("--strategies", default="chat,logit", help="chat,logit (default both)")
    ap.add_argument("--timeout", type=float, default=120.0, help="per-call timeout seconds (default 120)")
    ap.add_argument("--max-cases", type=int, default=0, help="limit dataset size (0 = all)")
    ap.add_argument("--n-ctx", type=int, default=4096, help="llama.cpp context size (default 4096)")
    ap.add_argument("--gpu", action="store_true", help="allow GPU offload (default: CPU-only, like prod)")
    args = ap.parse_args()

    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    for s in strategies:
        if s not in ("chat", "logit"):
            raise SystemExit(f"unknown strategy {s!r}; expected chat and/or logit")

    selected = select_candidates(GEN_CANDIDATES, args.models)
    cases = build_dataset()
    if args.max_cases:
        cases = cases[: args.max_cases]
    stats = dataset_stats(cases)
    print(f"dataset: {json.dumps(stats)}")

    model_results = []
    for key, spec in selected.items():
        path = model_path(spec)
        if not path.exists() or path.stat().st_size < 1_000_000:
            if args.skip_download:
                eprint(f"skip {key}: GGUF missing at {path} (--skip-download)")
                continue
            ensure_candidate(spec)
        print(f"\n=== {key} ({spec['label']}) ===")
        try:
            res = run_model(
                key,
                spec,
                cases,
                strategies=strategies,
                timeout_s=args.timeout,
                n_ctx=args.n_ctx,
                allow_gpu=args.gpu,
            )
        except Exception as e:  # noqa: BLE001 — keep benchmarking the rest
            eprint(f"model {key} failed to load/run: {e}")
            model_results.append({"key": key, "label": spec["label"], "error": str(e)})
            continue
        model_results.append(res)
        for strat, s in res["strategies"].items():
            m = s["metrics"]
            print(
                f"  {strat:5s} P={m['precision']:.3f} R={m['recall']:.3f} F1={m['f1']:.3f} "
                f"acc={m['accuracy']:.3f} mean={s['latency']['mean_ms']}ms "
                f"p95={s['latency']['p95_ms']}ms fail_open={sum(s['fail_open'].values())}"
            )

    payload = {
        "benchmark": "stage2_verification",
        "dataset": stats,
        "config": {
            "strategies": strategies,
            "timeout_s": args.timeout,
            "n_ctx": args.n_ctx,
            "gpu": bool(args.gpu),
        },
        "models": model_results,
    }
    write_results("stage2", payload, build_markdown(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

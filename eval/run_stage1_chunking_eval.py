#!/usr/bin/env python3
"""Stage-1 chunking / gating benchmark: what does the embedder actually see?

`eval/run_stage1_rerank_eval.py` asks which *scorer* is best on already-clean
one-sentence chunks. This asks the upstream question that nothing else covers:
given a whole raw mid-turn document, does the chunker hand the embedder text it
can judge, and how many junk candidates does Stage-1 propose off it?

Reported per configuration:

  fp props        proposals on documents that should fire nothing. This is the
                  incident metric — the 2026-08-16 freeze was 11 of these off a
                  single tool result, each costing a Stage-2 forward pass.
  buried R        recall on documents where a real cue sits inside tool output.
                  Guards against "fix" that just gates everything off.
  clean R         recall on the bare cue. Regression guard vs today's suites.
  trunc chunks    chunks whose true token length exceeds the embedder's 512-token
                  limit, i.e. silently cut by fastembed before scoring.
  p95 tok         true (untruncated) token length of the chunks produced.

Usage:
  python -m eval.run_stage1_chunking_eval
  python -m eval.run_stage1_chunking_eval --windows 64,128,256 --gate both
  python -m eval.run_stage1_chunking_eval --windows 256 --gate on
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Stage-1 only: embed mode keeps recall_system_armed() true without loading the
# reranker GGUF (see harness/recall.recall_system_armed).
os.environ.setdefault("RECALL_SLM_VERIFY", "1")
os.environ.setdefault("RECALL_STAGE2_MODE", "embed")

from eval.chunk_dataset import build_chunk_dataset, chunk_dataset_stats  # noqa: E402
from eval.common import (  # noqa: E402
    current_rss_mb,
    eprint,
    latency_summary,
    md_table,
    percentile,
    write_results,
)

EMBED_TOKEN_LIMIT = 512  # BAAI/bge-small-en-v1.5 hard max

# Matches nothing, for measuring the structured-chunk gate's contribution.
_GATE_OFF_RE = re.compile(r"(?!)")


def _quiet_logs() -> None:
    for name in ("galadriel", "galadriel.recall", "harness.recall", "semantic_router"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    logging.getLogger().setLevel(logging.CRITICAL)


def _untruncated_tokenizer(hr):
    """Untruncated copy of the production encoder's tokenizer.

    The live tokenizer has truncation pinned at 512, so it cannot report how
    long a chunk really was — which is the number this benchmark exists to
    surface. Returns None if the encoder is not a fastembed one.
    """
    try:
        from tokenizers import Tokenizer

        tok = hr.get_encoder(force_type="fastembed")._client.model.tokenizer
        raw = Tokenizer.from_str(tok.to_str())
        raw.no_truncation()
        return raw
    except Exception as e:  # noqa: BLE001 — token stats are diagnostics, not gating
        eprint(f"warning: no untruncated tokenizer available ({e}); token stats disabled")
        return None


def _chunks_for(hr, doc: str) -> list[str]:
    """The chunk list scan_text_for_recalls will embed, from production itself."""
    return hr.split_scan_units(doc)


def run_config(
    hr, cases: list[dict], tokenizer, *, window: int, overlap: int, gate: bool, margin: float
) -> dict:
    """Scan every document under one (window, overlap, gate, margin) configuration."""
    orig_tokens = hr._RECALL_CHUNK_TOKENS
    orig_overlap = hr._RECALL_CHUNK_OVERLAP
    orig_gate = hr._STRUCTURED_CHUNK_RE
    orig_margin = os.environ.get("RECALL_STAGE1_MARGIN")
    hr._RECALL_CHUNK_TOKENS = window
    hr._RECALL_CHUNK_OVERLAP = overlap
    os.environ["RECALL_STAGE1_MARGIN"] = str(margin)
    if not gate:
        hr._STRUCTURED_CHUNK_RE = _GATE_OFF_RE

    recalls = list(hr._load_system_recalls())
    rows: list[dict] = []
    latencies: list[float] = []
    all_tokens: list[int] = []
    try:
        for i, case in enumerate(cases):
            doc = case["doc"]
            chunks = _chunks_for(hr, doc)
            tok_lens = [len(tokenizer.encode(c).ids) for c in chunks] if tokenizer else []
            all_tokens.extend(tok_lens)

            t0 = time.perf_counter()
            matches = hr.scan_text_for_recalls(doc, recalls)
            latency = time.perf_counter() - t0
            latencies.append(latency)

            proposed = {m.get("recall_id") for m in matches if m.get("recall_id")}
            expected = set(case["expected_ids"])
            rows.append({
                "name": case["name"],
                "family": case["family"],
                "group": case.get("group", case["name"]),
                "variant": case.get("variant", "only"),
                "expected_ids": sorted(expected),
                "proposed_ids": sorted(proposed),
                "proposals": [
                    {
                        "recall_id": m.get("recall_id"),
                        "score": hr._as_float_score(m.get("positive_score")),
                        "source": m.get("match_source"),
                        "chunk": (m.get("matched_chunk") or "")[:120],
                    }
                    for m in matches
                    if m.get("recall_id")
                ],
                "fp_ids": sorted(proposed - expected),
                "fn_ids": sorted(expected - proposed),
                "n_chunks": len(chunks),
                "max_tokens": max(tok_lens) if tok_lens else None,
                "truncated_chunks": sum(1 for n in tok_lens if n > EMBED_TOKEN_LIMIT),
                "sources": sorted({m.get("match_source") for m in matches if m.get("match_source")}),
                "latency_ms": round(latency * 1000, 1),
            })
            if (i + 1) % 25 == 0:
                eprint(f"  [w={window} gate={'on' if gate else 'off'}] {i + 1}/{len(cases)}")
    finally:
        hr._RECALL_CHUNK_TOKENS = orig_tokens
        hr._RECALL_CHUNK_OVERLAP = orig_overlap
        hr._STRUCTURED_CHUNK_RE = orig_gate
        if orig_margin is None:
            os.environ.pop("RECALL_STAGE1_MARGIN", None)
        else:
            os.environ["RECALL_STAGE1_MARGIN"] = orig_margin

    return {
        "window_tokens": window,
        "overlap_tokens": overlap,
        "structured_gate": gate,
        "stage1_margin": margin,
        "cap_survival": _cap_survival(rows),
        "invariance": _invariance(rows),
        "by_family": _family_metrics(rows),
        "totals": _totals(rows, all_tokens),
        "threshold_sweep": _threshold_sweep(rows),
        "latency": latency_summary(latencies),
        "rows": rows,
    }


def _cap_survival(rows: list[dict], cap: int = 3) -> dict:
    """Does the correct recall survive Stage-2's top-N candidate cut?

    Stage-2 verifies only the best `RECALL_STAGE2_MAX_CANDIDATES` proposals by
    Stage-1 score, so a document that over-proposes can push its real match out
    of the shortlist and Stage-2 never sees it. Bounded latency is only free if
    the ranking it truncates is trustworthy.
    """
    reached = dropped = 0
    dropped_ranks: list[int] = []
    for r in rows:
        want = set(r["expected_ids"])
        if not want:
            continue
        ordered = sorted(r["proposals"], key=lambda p: p["score"] or 0.0, reverse=True)
        rank = next((i for i, p in enumerate(ordered) if p["recall_id"] in want), None)
        if rank is None:
            continue  # Stage-1 miss, counted by recall, not by the cap
        if rank < cap:
            reached += 1
        else:
            dropped += 1
            dropped_ranks.append(rank)
    return {
        "cap": cap,
        "reached_stage2": reached,
        "dropped_by_cap": dropped,
        "dropped_ranks": sorted(dropped_ranks),
    }


def _invariance(rows: list[dict]) -> dict:
    """Do permutations of one document produce the same Stage-1 answer?

    Cases sharing a `group` carry identical content in a different line order,
    so anything that differs between them is the scan reacting to position. The
    suite generated these permutations from the start but scored them as
    independent cases, which renders a positional flip as mediocre recall — the
    reason the first-chunk-wins dedup in `scan_text_for_recalls` survived every
    previous run of this benchmark.

    Three properties per group, in increasing strictness: the proposed id set is
    identical across variants; each recall binds to the same chunk wherever it
    appears; and its Stage-1 score does not move.
    """
    by_group: dict[str, list[dict]] = {}
    for r in rows:
        by_group.setdefault(r["group"], []).append(r)

    groups: list[dict] = []
    for name, variants in sorted(by_group.items()):
        if len(variants) < 2:
            continue
        answers = {frozenset(v["proposed_ids"]) for v in variants}
        chunks: dict[str, set[str]] = {}
        scores: dict[str, list[float]] = {}
        for v in variants:
            for p in v["proposals"]:
                rid = p["recall_id"]
                chunks.setdefault(rid, set()).add(p.get("chunk") or "")
                if p["score"] is not None:
                    scores.setdefault(rid, []).append(p["score"])
        groups.append({
            "group": name,
            "family": variants[0]["family"],
            "variants": len(variants),
            "agrees": len(answers) == 1,
            "distinct_answers": sorted(sorted(a) for a in answers),
            "unstable_bindings": sorted(r for r, c in chunks.items() if len(c) > 1),
            "max_score_spread": round(
                max((max(s) - min(s) for s in scores.values() if len(s) > 1), default=0.0),
                4,
            ),
        })

    def _summary(subset: list[dict]) -> dict:
        if not subset:
            return {"groups": 0, "agreeing": 0, "agreement": None}
        agreeing = sum(1 for g in subset if g["agrees"])
        return {
            "groups": len(subset),
            "agreeing": agreeing,
            "agreement": round(agreeing / len(subset), 4),
            "groups_with_unstable_binding": sum(1 for g in subset if g["unstable_bindings"]),
            "max_score_spread": max((g["max_score_spread"] for g in subset), default=0.0),
        }

    return {
        "overall": _summary(groups),
        "by_family": {
            fam: _summary([g for g in groups if g["family"] == fam])
            for fam in sorted({g["family"] for g in groups})
        },
        "disagreements": [g for g in groups if not g["agrees"] or g["unstable_bindings"]],
    }


def _threshold_sweep(rows: list[dict]) -> list[dict]:
    """Re-decide every proposal at higher accept floors.

    Stage-1 accept is just `positive_score >= positive_threshold`, so the
    proposals recorded at the production floor can be re-filtered upward without
    re-embedding. Lexical hits score 1.0 and survive every threshold, which is
    the intended contract. Sweeping *below* the production floor would need the
    router's rejected routes, which this pass does not keep.
    """
    out = []
    for thr in [round(0.60 + 0.025 * i, 3) for i in range(15)]:
        fp_noise = fp_all = buried_hit = clean_hit = 0
        buried_total = clean_total = 0
        for r in rows:
            kept = {
                p["recall_id"] for p in r["proposals"]
                if p["score"] is not None and p["score"] >= thr
            }
            expected = set(r["expected_ids"])
            fp = len(kept - expected)
            fp_all += fp
            if not expected:
                fp_noise += fp
            elif r["family"] == "signal_in_noise":
                buried_total += len(expected)
                buried_hit += len(expected & kept)
            elif r["family"] == "clean_signal":
                clean_total += len(expected)
                clean_hit += len(expected & kept)
        out.append({
            "threshold": thr,
            "fp_proposals_noise_only": fp_noise,
            "fp_proposals": fp_all,
            "buried_recall": round(buried_hit / buried_total, 4) if buried_total else None,
            "clean_recall": round(clean_hit / clean_total, 4) if clean_total else None,
        })
    return out


def _family_metrics(rows: list[dict]) -> dict:
    out: dict = {}
    for fam in sorted({r["family"] for r in rows}):
        sub = [r for r in rows if r["family"] == fam]
        expected_total = sum(len(r["expected_ids"]) for r in sub)
        hits = sum(len(set(r["expected_ids"]) & set(r["proposed_ids"])) for r in sub)
        fp = sum(len(r["fp_ids"]) for r in sub)
        out[fam] = {
            "docs": len(sub),
            "fp_proposals": fp,
            "docs_with_fp": sum(1 for r in sub if r["fp_ids"]),
            "expected_fires": expected_total,
            "expected_hit": hits,
            "recall": round(hits / expected_total, 4) if expected_total else None,
            "proposals_per_doc_max": max((len(r["proposed_ids"]) for r in sub), default=0),
        }
    return out


def _totals(rows: list[dict], all_tokens: list[int]) -> dict:
    noise = [r for r in rows if not r["expected_ids"]]
    signal = [r for r in rows if r["expected_ids"]]
    expected_total = sum(len(r["expected_ids"]) for r in signal)
    hits = sum(len(set(r["expected_ids"]) & set(r["proposed_ids"])) for r in signal)
    return {
        "docs": len(rows),
        "chunks": sum(r["n_chunks"] for r in rows),
        "fp_proposals": sum(len(r["fp_ids"]) for r in rows),
        "fp_proposals_noise_only": sum(len(r["fp_ids"]) for r in noise),
        "docs_with_fp": sum(1 for r in rows if r["fp_ids"]),
        "signal_recall": round(hits / expected_total, 4) if expected_total else None,
        "truncated_chunks": sum(r["truncated_chunks"] for r in rows),
        "truncated_pct": (
            round(100 * sum(r["truncated_chunks"] for r in rows) / max(1, sum(r["n_chunks"] for r in rows)), 1)
        ),
        "tokens_p95": round(percentile([float(t) for t in all_tokens], 0.95), 1) if all_tokens else None,
        "tokens_max": max(all_tokens) if all_tokens else None,
    }


def build_markdown(payload: dict) -> str:
    lines = ["# Stage-1 chunking / gating benchmark", ""]
    stats = payload["dataset"]
    fam = ", ".join(f"{k} {v['docs']}" for k, v in stats["by_family"].items())
    lines.append(f"{stats['total']} raw documents ({fam})")
    lines.append("")
    headers = [
        "margin", "window (tok)", "structured gate",
        "agreement", "unstable bindings",
        "fp props (noise docs)", "fp props (all)", "docs w/ FP",
        "buried R", "clean R", "paragraph R", "runon R", "dropped by cap",
        "trunc chunks", "p95 tok", "mean ms",
    ]
    rows = []
    for c in payload["configs"]:
        t = c["totals"]
        buried = c["by_family"].get("signal_in_noise", {})
        clean = c["by_family"].get("clean_signal", {})
        cap = c["cap_survival"]
        inv = c["invariance"]["overall"]
        rows.append([
            c["stage1_margin"],
            c["window_tokens"],
            "on" if c["structured_gate"] else "off",
            f"{inv['agreeing']}/{inv['groups']} ({inv['agreement']})",
            inv["groups_with_unstable_binding"],
            t["fp_proposals_noise_only"],
            t["fp_proposals"],
            t["docs_with_fp"],
            f"{buried.get('recall')}" if buried else "-",
            f"{clean.get('recall')}" if clean else "-",
            f"{c['by_family'].get('long_paragraph', {}).get('recall', '-')}",
            f"{c['by_family'].get('runon_chat', {}).get('recall', '-')}",
            f"{cap['dropped_by_cap']}/{cap['reached_stage2'] + cap['dropped_by_cap']}",
            f"{t['truncated_chunks']} ({t['truncated_pct']}%)",
            t["tokens_p95"],
            c["latency"]["mean_ms"],
        ])
    lines.append(md_table(headers, rows))
    lines.append("")

    for c in payload["configs"]:
        inv = c["invariance"]
        lines.append(
            f"## Positional agreement (margin {c['stage1_margin']}, window {c['window_tokens']})"
        )
        lines.append("")
        lines.append(md_table(
            ["family", "groups", "agreeing", "agreement", "unstable bindings", "max score spread"],
            [
                [fam, s["groups"], s["agreeing"], s["agreement"],
                 s["groups_with_unstable_binding"], s["max_score_spread"]]
                for fam, s in inv["by_family"].items()
            ],
        ))
        lines.append("")
        if inv["disagreements"]:
            lines.append("Groups whose answer or binding moved with line order:")
            lines.append("")
            for g in inv["disagreements"]:
                answers = " | ".join(",".join(a) or "(none)" for a in g["distinct_answers"])
                lines.append(
                    f"- `{g['group']}` ({g['variants']} variants) -> {answers}"
                    + (f" · rebound: {', '.join(g['unstable_bindings'])}" if g["unstable_bindings"] else "")
                )
            lines.append("")

    for c in payload["configs"]:
        lines.append(
            f"## Accept-floor sweep (margin {c['stage1_margin']}, "
            f"window {c['window_tokens']}, gate {'on' if c['structured_gate'] else 'off'})"
        )
        lines.append("")
        lines.append(md_table(
            ["positive_threshold", "fp props (noise docs)", "fp props (all)", "buried R", "clean R"],
            [
                [s["threshold"], s["fp_proposals_noise_only"], s["fp_proposals"],
                 s["buried_recall"], s["clean_recall"]]
                for s in c["threshold_sweep"]
            ],
        ))
        lines.append("")

    lines.append(
        f"`margin` is `RECALL_STAGE1_MARGIN`: the top route must beat the runner-up by this much "
        "for the chunk to propose anything (0 = gate off, pre-2026-08-16 behaviour). "
        f"`window (tok)` is the chunker's budget (`_RECALL_CHUNK_TOKENS`). `trunc chunks` counts "
        f"chunks whose true length exceeds the embedder's {EMBED_TOKEN_LIMIT}-token limit and are "
        "therefore silently cut before scoring. `fp props (noise docs)` is the count of Stage-1 "
        "proposals on documents that should fire nothing — each one costs a Stage-2 forward pass "
        "in production. `dropped by cap` is real matches ranked outside the top 3 that Stage-2 "
        "therefore never sees. `buried R` must not drop while `fp props` falls, or the gate is "
        "simply suppressing everything."
    )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--windows", default="256", help="comma list of chunk token budgets to sweep")
    ap.add_argument(
        "--margins",
        default="",
        help="comma list of RECALL_STAGE1_MARGIN values to sweep (default: production)",
    )
    ap.add_argument("--overlap-pct", type=float, default=0.2, help="chunk overlap as a fraction of window")
    ap.add_argument(
        "--gate",
        choices=("on", "off", "both"),
        default="on",
        help="structured-chunk regex gate: on (production), off, or both",
    )
    ap.add_argument("--max-cases", type=int, default=0, help="limit documents (0 = all)")
    args = ap.parse_args()

    _quiet_logs()
    windows = [int(w.strip()) for w in args.windows.split(",") if w.strip()]
    gates = {"on": [True], "off": [False], "both": [True, False]}[args.gate]

    cases = build_chunk_dataset()
    if args.max_cases:
        cases = cases[: args.max_cases]
    stats = chunk_dataset_stats(cases)
    print(f"dataset: {json.dumps(stats)}")

    from harness import recall as hr  # heavy deps: semantic_router, fastembed

    tokenizer = _untruncated_tokenizer(hr)
    margins = (
        [float(m.strip()) for m in args.margins.split(",") if m.strip()]
        if args.margins
        else [hr._stage1_margin()]
    )

    configs = []
    for margin in margins:
        for window in windows:
            overlap = max(0, int(window * args.overlap_pct))
            for gate in gates:
                print(
                    f"\n=== margin={margin} window={window} overlap={overlap} "
                    f"gate={'on' if gate else 'off'} ==="
                )
                res = run_config(
                    hr, cases, tokenizer,
                    window=window, overlap=overlap, gate=gate, margin=margin,
                )
                configs.append(res)
                t = res["totals"]
                cap = res["cap_survival"]
                inv = res["invariance"]["overall"]
                fam = res["by_family"]
                print(
                    f"  chunks={t['chunks']} "
                    f"agreement={inv['agreeing']}/{inv['groups']} ({inv['agreement']}) "
                    f"unstable_bind={inv['groups_with_unstable_binding']} "
                    f"fp_noise={t['fp_proposals_noise_only']} "
                    f"fp_all={t['fp_proposals']} "
                    f"buried_R={fam.get('signal_in_noise', {}).get('recall')} "
                    f"clean_R={fam.get('clean_signal', {}).get('recall')} "
                    f"competing_R={fam.get('competing_signal', {}).get('recall')} "
                    f"paragraph_R={fam.get('long_paragraph', {}).get('recall')} "
                    f"runon_R={fam.get('runon_chat', {}).get('recall')} "
                    f"dropped_by_cap={cap['dropped_by_cap']} "
                    f"trunc={t['truncated_chunks']} mean={res['latency']['mean_ms']}ms"
                )

    payload = {
        "benchmark": "stage1_chunking",
        "dataset": stats,
        "config": {
            "embed_token_limit": EMBED_TOKEN_LIMIT,
            "overlap_pct": args.overlap_pct,
            "encoder": "fastembed BAAI/bge-small-en-v1.5",
            "rss_mb": (lambda v: round(v, 1) if v else None)(current_rss_mb()),
        },
        "configs": configs,
    }
    write_results("stage1_chunking", payload, build_markdown(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

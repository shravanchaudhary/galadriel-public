#!/usr/bin/env python3
"""Audit recall cue quality on system recalls (Stage-1 + Stage-2 embed).

Contract (positive-only Stage-1):
  - Stage-1 is a high-recall proposer: positives must propose their recall_id,
    lexical cues must hard-hit. Negatives are NOT gated at Stage-1.
  - Negative holdout is an end-to-end property: a negative "leaks" only if
    Stage-1 proposes it AND Stage-2 (embed margin) verifies it.
  - Chunks under the min-word gate must never propose semantically
    (lexical cues still may).

Also synthesizes a sample learn_recall-shaped rule and checks rematch.
Exit non-zero if cue rematch rates fall below thresholds.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Embed-only Stage-2: no GGUF required (shares the FastEmbed encoder).
os.environ["RECALL_SLM_VERIFY"] = "1"
os.environ["RECALL_STAGE2_MODE"] = "embed"

from harness.recall import (  # noqa: E402
    DEFAULT_POSITIVE_THRESHOLD,
    _MIN_SEMANTIC_SCAN_WORDS,
    _load_system_recalls,
    generate_recall_fire_text,
    scan_text_for_recalls,
    verify_recall_candidate_embed,
)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _stage1_ids(text: str, recalls: list[dict]) -> set[str]:
    return {m.get("recall_id") for m in scan_text_for_recalls(text, recalls) if m.get("recall_id")}


def _verified_ids(text: str, recalls: list[dict]) -> set[str]:
    """End-to-end: Stage-1 propose, then Stage-2 embed verify."""
    out = set()
    for m in scan_text_for_recalls(text, recalls):
        ok, _reason = verify_recall_candidate_embed(m.get("matched_chunk") or text, m)
        if ok and m.get("recall_id"):
            out.add(m["recall_id"])
    return out


def main() -> int:
    _assert(DEFAULT_POSITIVE_THRESHOLD == 0.6, f"pos_thr={DEFAULT_POSITIVE_THRESHOLD}")
    recalls = _load_system_recalls()
    _assert(len(recalls) >= 3, "expected system recalls")

    pos_ok = pos_total = 0
    neg_ok = neg_total = 0
    lex_ok = lex_total = 0
    failures: list[str] = []

    for recall in recalls:
        rid = recall["recall_id"]
        for text in recall.get("positive_examples") or []:
            pos_total += 1
            ids = _stage1_ids(text, recalls)
            if rid in ids:
                pos_ok += 1
            else:
                failures.append(f"POS miss {rid}: {text!r} → {sorted(ids)}")
        for text in recall.get("negative_examples") or []:
            neg_total += 1
            ids = _verified_ids(text, recalls)
            if rid not in ids:
                neg_ok += 1
            else:
                failures.append(f"NEG leak (stage1+2) {rid}: {text!r}")
        for cue in recall.get("lexical_cues") or []:
            lex_total += 1
            # Embed cue in a short sentence so chunking/word-boundary still hits.
            text = f"please check {cue} for me"
            ids = _stage1_ids(text, recalls)
            if rid in ids:
                lex_ok += 1
            else:
                failures.append(f"LEX miss {rid}: {cue!r} → {sorted(ids)}")

    # Min-word gate: bare tool names / tiny args must not propose semantically.
    for junk in ("read_file", "active", '{"path": "state/worker_control.md"}'):
        semantic_ids = {
            m.get("recall_id")
            for m in scan_text_for_recalls(junk, recalls)
            if m.get("match_source") == "semantic"
        }
        _assert(
            len(junk.split()) >= _MIN_SEMANTIC_SCAN_WORDS or not semantic_ids,
            f"min-word gate leaked semantic matches for {junk!r}: {sorted(semantic_ids)}",
        )

    # Synthetic learn_recall-shaped rule — should rematch its own positives.
    synthetic = {
        "recall_id": "user_pref_dark_mode",
        "instruction": "User prefers dark mode — check MEMORY.md / recall(user_pref_dark_mode).",
        "positive_examples": [
            "switch the editor to dark mode",
            "I want the dark theme please",
            "enable dark mode for me",
        ],
        "negative_examples": [
            "what is the capital of France?",
            "deploy to staging",
            "tell me a joke",
        ],
        "lexical_cues": ["dark mode", "dark theme"],
        "source": "user",
    }
    synth_recalls = recalls + [synthetic]
    for text in synthetic["positive_examples"]:
        ids = _stage1_ids(text, synth_recalls)
        if "user_pref_dark_mode" not in ids:
            failures.append(f"SYNTH POS miss: {text!r} → {sorted(ids)}")
    for text in synthetic["negative_examples"]:
        ids = _verified_ids(text, synth_recalls)
        if "user_pref_dark_mode" in ids:
            failures.append(f"SYNTH NEG leak: {text!r}")
    for cue in synthetic["lexical_cues"]:
        ids = _stage1_ids(f"please enable {cue}", synth_recalls)
        if "user_pref_dark_mode" not in ids:
            failures.append(f"SYNTH LEX miss: {cue!r}")

    fire = generate_recall_fire_text([synthetic])
    _assert("dark mode" in fire.lower() or "MEMORY.md" in fire, "fire text missing instruction")

    pos_rate = pos_ok / pos_total if pos_total else 0.0
    neg_rate = neg_ok / neg_total if neg_total else 0.0
    lex_rate = lex_ok / lex_total if lex_total else 0.0
    print(
        f"pos_thr={DEFAULT_POSITIVE_THRESHOLD} min_words={_MIN_SEMANTIC_SCAN_WORDS} "
        f"pos={pos_ok}/{pos_total} ({pos_rate:.2f}) "
        f"neg(e2e)={neg_ok}/{neg_total} ({neg_rate:.2f}) "
        f"lex={lex_ok}/{lex_total} ({lex_rate:.2f})"
    )
    for line in failures[:40]:
        print("FAIL:", line)
    if len(failures) > 40:
        print(f"... and {len(failures) - 40} more")

    _assert(pos_rate >= 0.70, f"positive rematch {pos_rate:.2f} < 0.70")
    _assert(neg_rate >= 0.70, f"end-to-end negative holdout {neg_rate:.2f} < 0.70")
    _assert(lex_rate >= 0.80, f"lexical hit {lex_rate:.2f} < 0.80")
    _assert(not any(f.startswith("SYNTH") for f in failures), "synthetic learn_recall rematch failed")
    print("ok recall_matcher_cues")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as e:
        print(f"ASSERT: {e}", file=sys.stderr)
        raise SystemExit(1)

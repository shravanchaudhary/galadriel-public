#!/usr/bin/env python3
"""Audit Stage-1 recall matcher cue quality on system recalls.

For each system recall:
  - positives should match that recall_id
  - negatives should not match that recall_id (veto or miss)
  - lexical cues should hard-hit

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

# Keep this audit Stage-1 only (no SLM / no GGUF required).
os.environ.setdefault("RECALL_SLM_VERIFY", "0")

from harness.recall import (  # noqa: E402
    _POSITIVE_SCORE_FLOOR,
    _load_system_recalls,
    generate_recall_fire_text,
    scan_text_for_recalls,
)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _matched_ids(text: str, recalls: list[dict]) -> set[str]:
    return {m.get("recall_id") for m in scan_text_for_recalls(text, recalls) if m.get("recall_id")}


def main() -> int:
    _assert(_POSITIVE_SCORE_FLOOR == 0.6, f"floor={_POSITIVE_SCORE_FLOOR}")
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
            ids = _matched_ids(text, recalls)
            if rid in ids:
                pos_ok += 1
            else:
                failures.append(f"POS miss {rid}: {text!r} → {sorted(ids)}")
        for text in recall.get("negative_examples") or []:
            neg_total += 1
            ids = _matched_ids(text, recalls)
            if rid not in ids:
                neg_ok += 1
            else:
                failures.append(f"NEG leak {rid}: {text!r}")
        for cue in recall.get("lexical_cues") or []:
            lex_total += 1
            # Embed cue in a short sentence so chunking/word-boundary still hits.
            text = f"please check {cue} for me"
            ids = _matched_ids(text, recalls)
            if rid in ids:
                lex_ok += 1
            else:
                failures.append(f"LEX miss {rid}: {cue!r} → {sorted(ids)}")

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
        ids = _matched_ids(text, synth_recalls)
        if "user_pref_dark_mode" not in ids:
            failures.append(f"SYNTH POS miss: {text!r} → {sorted(ids)}")
    for text in synthetic["negative_examples"]:
        ids = _matched_ids(text, synth_recalls)
        if "user_pref_dark_mode" in ids:
            failures.append(f"SYNTH NEG leak: {text!r}")
    for cue in synthetic["lexical_cues"]:
        ids = _matched_ids(f"please enable {cue}", synth_recalls)
        if "user_pref_dark_mode" not in ids:
            failures.append(f"SYNTH LEX miss: {cue!r}")

    fire = generate_recall_fire_text([synthetic])
    _assert("dark mode" in fire.lower() or "MEMORY.md" in fire, "fire text missing instruction")

    pos_rate = pos_ok / pos_total if pos_total else 0.0
    neg_rate = neg_ok / neg_total if neg_total else 0.0
    lex_rate = lex_ok / lex_total if lex_total else 0.0
    print(
        f"floor={_POSITIVE_SCORE_FLOOR} "
        f"pos={pos_ok}/{pos_total} ({pos_rate:.2f}) "
        f"neg={neg_ok}/{neg_total} ({neg_rate:.2f}) "
        f"lex={lex_ok}/{lex_total} ({lex_rate:.2f})"
    )
    for line in failures[:40]:
        print("FAIL:", line)
    if len(failures) > 40:
        print(f"... and {len(failures) - 40} more")

    _assert(pos_rate >= 0.70, f"positive rematch {pos_rate:.2f} < 0.70")
    _assert(neg_rate >= 0.70, f"negative holdout {neg_rate:.2f} < 0.70")
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

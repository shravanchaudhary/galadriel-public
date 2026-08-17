#!/usr/bin/env python3
"""Regression: Qwen3 reranker score readout must use the full [0,1] range.

After the double-sigmoid bug, clear hits sat near 0.73 and clear misses near
0.50 — the whole scale was squeezed into [0.50, 0.73]. With the fixed readout a
textbook relevance pair must clear 0.9 and an unrelated pair must fall below 0.1.

The probe is a plain question/answer pair on purpose. Qwen3-Reranker scores
query->document *relevance*; abstract meta-text ("the user is asking the agent
to...") scores ~0.01 even against a matching utterance, so it cannot be used to
assert scale. Skips when the GGUF is not present.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    from local_llm.config import reranker_path
    from local_llm.reranker import Qwen3Reranker

    path = reranker_path()
    if not path.exists() or path.stat().st_size < 1_000_000:
        print(f"SKIP: reranker GGUF missing at {path}")
        return 0

    rr = Qwen3Reranker()
    try:
        query = "what is the capital of France?"
        hit = rr.score(query, "The capital of France is Paris.")
        miss = rr.score(query, "Corporate tax rates for small enterprises in Estonia.")
        # The scale the local Stage-2 tier actually operates on: user utterance
        # against a stored positive example.
        utterance = rr.score(
            "please remember I like oranges",
            "can you remember my favorite fruit is mango",
        )
    finally:
        rr.close()

    print(f"hit={hit:.4f} miss={miss:.4f} utterance={utterance:.4f}")
    assert hit > miss, f"ranking inverted: hit={hit} miss={miss}"
    assert hit > 0.9, f"clear hit should be >0.9 after readout fix, got {hit}"
    assert miss < 0.1, f"clear miss should be <0.1 after readout fix, got {miss}"
    assert utterance > 0.1, (
        "paraphrase pair collapsed to noise; the local Stage-2 threshold is "
        f"calibrated against this band, got {utterance}"
    )
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Qwen3 cross-encoder reranker for recall Stage-2 verification.

llama-cpp-python exposes no rerank API, so this drives the low-level bindings:
the GGUF is loaded with RANK pooling (classifier head) and the per-sequence
score is read via llama_get_embeddings_seq.

The GGUF must be converted with the official convert_hf_to_gguf.py so the
cls.output.weight tensor survives — most community reranker quants are broken
and return constant ~0 scores (llama.cpp issue #16407). load() runs a sanity
check and raises if the head is dead, so callers can fail open.
"""

from __future__ import annotations

import ctypes
import math
import threading
from pathlib import Path

from .config import RERANKER_FILENAME, RERANKER_HF_REPO, reranker_path
from .engine import detect_runtime

__all__ = ["Qwen3Reranker", "RERANKER_FILENAME", "RERANKER_HF_REPO", "reranker_path", "build_prompt"]

# Official Qwen3-Reranker prompt format (model card).
_PREFIX = (
    "<|im_start|>system\nJudge whether the Document meets the requirements based "
    'on the Query and the Instruct provided. Note that the answer can only be '
    '"yes" or "no".<|im_end|>\n<|im_start|>user\n'
)
_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"

DEFAULT_INSTRUCT = (
    "Given a text snippet from an AI agent conversation, judge whether the "
    "Document describes a situation where the snippet applies."
)


def build_prompt(query: str, doc: str, *, instruct: str = DEFAULT_INSTRUCT) -> str:
    return f"{_PREFIX}<Instruct>: {instruct}\n<Query>: {query}\n<Document>: {doc}{_SUFFIX}"


class Qwen3Reranker:
    """Thread-safe cross-encoder scorer. score() returns P(yes) in [0, 1]."""

    def __init__(self, model_path: Path | str | None = None, *, n_ctx: int = 2048) -> None:
        import llama_cpp
        from llama_cpp import Llama

        path = Path(model_path) if model_path else reranker_path()
        if not path.exists():
            raise FileNotFoundError(
                f"reranker GGUF not found at {path}; run: python -m local_llm download --reranker"
            )
        rt = detect_runtime()
        self._llama_cpp = llama_cpp
        self._lock = threading.Lock()
        self._llm = Llama(
            model_path=str(path),
            n_ctx=n_ctx,
            n_threads=rt["n_threads"],
            n_gpu_layers=rt["n_gpu_layers"],
            embedding=True,
            pooling_type=llama_cpp.LLAMA_POOLING_TYPE_RANK,
            verbose=False,
        )
        self._sanity_check()

    def _eval_for_rank(self, tokens: list[int]) -> None:
        """Decode with every token marked as an output.

        Llama.eval() only flags the last token unless logits_all=True (which
        allocates a (n_ctx, n_vocab) scores buffer we do not need). RANK
        pooling needs every position; otherwise llama.cpp overrides the batch
        and prints 'embeddings required but some input tokens were not marked
        as outputs -> overriding' on every forward pass.
        """
        llm = self._llm
        llm._ctx.kv_cache_seq_rm(-1, llm.n_tokens, -1)
        n_batch = llm.n_batch
        for i in range(0, len(tokens), n_batch):
            batch = tokens[i : min(len(tokens), i + n_batch)]
            n_past = llm.n_tokens
            llm._batch.set_batch(batch=batch, n_past=n_past, logits_all=True)
            llm._ctx.decode(llm._batch)
            llm.input_ids[n_past : n_past + len(batch)] = batch
            llm.n_tokens += len(batch)

    def _score_unlocked(self, query: str, doc: str) -> float:
        llm = self._llm
        tokens = llm.tokenize(build_prompt(query, doc).encode("utf-8"), add_bos=False, special=True)
        if len(tokens) >= llm.n_ctx():
            tokens = tokens[: llm.n_ctx() - 8]
        llm.reset()
        self._eval_for_rank(tokens)
        ptr = self._llama_cpp.llama_get_embeddings_seq(llm._ctx.ctx, 0)
        if not ptr:
            raise RuntimeError("llama_get_embeddings_seq returned NULL (reranker GGUF has no rank head)")
        raw = float(ctypes.cast(ptr, ctypes.POINTER(ctypes.c_float))[0])
        if not math.isfinite(raw):
            raise RuntimeError(f"non-finite rerank score {raw}")
        return 1.0 / (1.0 + math.exp(-raw))

    def _sanity_check(self) -> None:
        hit = self._score_unlocked("what is the capital of France?", "The capital of France is Paris.")
        miss = self._score_unlocked("what is the capital of France?", "Corporate tax rates for small enterprises.")
        if not hit > miss:
            raise RuntimeError(
                f"reranker rank head appears dead (hit={hit:.4f} <= miss={miss:.4f})"
            )

    def score(self, query: str, doc: str) -> float:
        with self._lock:
            return self._score_unlocked(query, doc)

    def best_match(
        self, query: str, docs: list[str], *, stop_at: float | None = None
    ) -> tuple[float, str] | None:
        """Best (P(yes), doc) across docs.

        Each pair costs a full forward pass, so `stop_at` short-circuits as soon
        as one doc clears the caller's accept threshold — the common case for a
        true match is an early hit. The returned doc lets callers record which
        cue actually did the work (LRU usage tracking).
        """
        cleaned = [d.strip() for d in docs if isinstance(d, str) and d.strip()]
        if not cleaned:
            return None
        best, best_doc = 0.0, cleaned[0]
        with self._lock:
            for doc in cleaned:
                score = self._score_unlocked(query, doc[:400])
                if score > best:
                    best, best_doc = score, doc
                if stop_at is not None and best > stop_at:
                    break
        return best, best_doc

    def max_score(self, query: str, docs: list[str], *, stop_at: float | None = None) -> float | None:
        hit = self.best_match(query, docs, stop_at=stop_at)
        return None if hit is None else hit[0]

    def close(self) -> None:
        with self._lock:
            llm = self._llm
            self._llm = None
        del llm
        import gc

        gc.collect()

    def __enter__(self) -> Qwen3Reranker:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

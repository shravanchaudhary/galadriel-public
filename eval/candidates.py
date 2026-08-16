"""Model registry for the recall-verification benchmark.

All GGUF repos/filenames were verified against HuggingFace (HTTP 302 on the
resolve URL) on 2026-08-15. Sizes are the exact x-linked-size values.

Weights are downloaded into eval/models/ (NOT local_llm/models/) so the
benchmark never touches the production runtime's model directory. The download
itself reuses local_llm.download's resumable helper.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

EVAL_MODELS_DIR = REPO_ROOT / "eval" / "models"

# ---------------------------------------------------------------------------
# Generative Stage-2 candidates (GGUF, llama-cpp-python, CPU)
# ---------------------------------------------------------------------------
# chat_suffix: appended to the user message for models whose chat template
# defaults to thinking mode (Qwen3 soft switch).
GEN_CANDIDATES: dict[str, dict] = {
    "gemma3-270m": {
        "label": "Gemma 3 270M IT QAT",
        "hf_repo": "bartowski/google_gemma-3-270m-it-qat-GGUF",
        "filename": "google_gemma-3-270m-it-qat-Q4_K_M.gguf",
        "approx_mb": 241,
        "params": "270M",
        "chat_suffix": "",
    },
    "gemma3-1b": {
        "label": "Gemma 3 1B IT QAT",
        "hf_repo": "bartowski/google_gemma-3-1b-it-qat-GGUF",
        "filename": "google_gemma-3-1b-it-qat-Q4_K_M.gguf",
        "approx_mb": 769,
        "params": "1B",
        "chat_suffix": "",
    },
    "qwen3-0.6b": {
        "label": "Qwen3 0.6B",
        "hf_repo": "bartowski/Qwen_Qwen3-0.6B-GGUF",
        "filename": "Qwen_Qwen3-0.6B-Q4_K_M.gguf",
        "approx_mb": 462,
        "params": "0.6B",
        "chat_suffix": " /no_think",
    },
    "qwen3-1.7b": {
        "label": "Qwen3 1.7B",
        "hf_repo": "bartowski/Qwen_Qwen3-1.7B-GGUF",
        "filename": "Qwen_Qwen3-1.7B-Q4_K_M.gguf",
        "approx_mb": 1223,
        "params": "1.7B",
        "chat_suffix": " /no_think",
    },
    "qwen2.5-0.5b": {
        "label": "Qwen2.5 0.5B Instruct",
        "hf_repo": "bartowski/Qwen2.5-0.5B-Instruct-GGUF",
        "filename": "Qwen2.5-0.5B-Instruct-Q4_K_M.gguf",
        "approx_mb": 379,
        "params": "0.5B",
        "chat_suffix": "",
    },
    "qwen2.5-1.5b": {
        "label": "Qwen2.5 1.5B Instruct",
        "hf_repo": "bartowski/Qwen2.5-1.5B-Instruct-GGUF",
        "filename": "Qwen2.5-1.5B-Instruct-Q4_K_M.gguf",
        "approx_mb": 940,
        "params": "1.5B",
        "chat_suffix": "",
    },
    "qwen2.5-3b": {
        "label": "Qwen2.5 3B Instruct",
        "hf_repo": "bartowski/Qwen2.5-3B-Instruct-GGUF",
        "filename": "Qwen2.5-3B-Instruct-Q4_K_M.gguf",
        "approx_mb": 1841,
        "params": "3B",
        "chat_suffix": "",
    },
    "llama3.2-1b": {
        "label": "Llama 3.2 1B Instruct",
        "hf_repo": "bartowski/Llama-3.2-1B-Instruct-GGUF",
        "filename": "Llama-3.2-1B-Instruct-Q4_K_M.gguf",
        "approx_mb": 770,
        "params": "1B",
        "chat_suffix": "",
    },
    "smollm2-360m": {
        "label": "SmolLM2 360M Instruct",
        "hf_repo": "bartowski/SmolLM2-360M-Instruct-GGUF",
        "filename": "SmolLM2-360M-Instruct-Q4_K_M.gguf",
        "approx_mb": 258,
        "params": "360M",
        "chat_suffix": "",
    },
    "smollm2-1.7b": {
        "label": "SmolLM2 1.7B Instruct",
        "hf_repo": "bartowski/SmolLM2-1.7B-Instruct-GGUF",
        "filename": "SmolLM2-1.7B-Instruct-Q4_K_M.gguf",
        "approx_mb": 1007,
        "params": "1.7B",
        "chat_suffix": "",
    },
}

# ---------------------------------------------------------------------------
# Stage-1 axis: embedding / reranker candidates
# ---------------------------------------------------------------------------
# Qwen3-Embedding GGUF quirks (from the official model card):
#   - llama.cpp needs pooling_type=LAST
#   - the EOS token must be appended manually: "text<|endoftext|>"
EMBED_CANDIDATES: dict[str, dict] = {
    "qwen3-embedding-0.6b": {
        "label": "Qwen3 Embedding 0.6B (GGUF Q8_0)",
        "hf_repo": "Qwen/Qwen3-Embedding-0.6B-GGUF",
        "filename": "Qwen3-Embedding-0.6B-Q8_0.gguf",
        "approx_mb": 610,
        "params": "0.6B",
        "pooling": "last",
        "append_eos": "<|endoftext|>",
    },
}

# Reranker: the official Qwen repo publishes no GGUF for the reranker.
# Voodisss/Qwen3-Reranker-0.6B-GGUF-llama_cpp is converted with the official
# convert_hf_to_gguf.py and keeps the cls.output.weight classifier tensor +
# pooling_type=RANK metadata (most community reranker GGUFs are broken — they
# return ~0 scores; see llama.cpp issue #16407).
# llama-cpp-python has no rerank API, so the GGUF path drives the low-level
# bindings (pooling RANK + llama_get_embeddings_seq); if that fails we fall
# back to transformers CPU on Qwen/Qwen3-Reranker-0.6B (float32 ≈ 2.4 GB RSS —
# fine for a benchmark box, NOT for a 4 GB tenant).
RERANKER_CANDIDATES: dict[str, dict] = {
    "qwen3-reranker-0.6b": {
        "label": "Qwen3 Reranker 0.6B",
        "hf_repo": "Voodisss/Qwen3-Reranker-0.6B-GGUF-llama_cpp",
        "filename": "Qwen3-Reranker-0.6B-Q4_K_M.gguf",
        "approx_mb": 397,
        "params": "0.6B",
        "transformers_fallback_repo": "Qwen/Qwen3-Reranker-0.6B",
        "transformers_fallback_ram_gb": 2.4,
    },
}

# Current production Stage-1/Stage-2 baseline (downloaded by fastembed itself).
BASELINE_EMBEDDING = {
    "label": "FastEmbed BAAI/bge-small-en-v1.5 (current baseline)",
    "model_name": "BAAI/bge-small-en-v1.5",
    "approx_mb": 130,
}


def model_path(spec: dict) -> Path:
    return EVAL_MODELS_DIR / spec["filename"]


def ensure_candidate(spec: dict, *, force: bool = False) -> Path:
    """Download the GGUF for a candidate into eval/models/ if missing.

    Reuses local_llm.download's progress/partial-file helper (read-only import)
    but never writes into local_llm/models/.
    """
    path = model_path(spec)
    if path.exists() and path.stat().st_size > 1_000_000 and not force:
        return path

    from local_llm.config import hf_file_url
    from local_llm.download import _download_with_progress

    EVAL_MODELS_DIR.mkdir(parents=True, exist_ok=True)
    url = hf_file_url(spec["filename"], repo=spec["hf_repo"])
    print(f"Fetching {spec['filename']} (~{spec['approx_mb']} MB)")
    print(f"  from {url}")
    print(f"  into {path}")
    _download_with_progress(url, path)
    return path


def select_candidates(registry: dict[str, dict], models_arg: str | None) -> dict[str, dict]:
    """Filter a registry by a comma-separated --models argument."""
    if not models_arg:
        return dict(registry)
    wanted = [m.strip() for m in models_arg.split(",") if m.strip()]
    unknown = [m for m in wanted if m not in registry]
    if unknown:
        raise SystemExit(
            f"unknown model key(s): {unknown}; available: {sorted(registry)}"
        )
    return {k: registry[k] for k in wanted}


if __name__ == "__main__":
    total = 0
    for name, reg in (
        ("generative", GEN_CANDIDATES),
        ("embedding", EMBED_CANDIDATES),
        ("reranker", RERANKER_CANDIDATES),
    ):
        print(f"\n{name}:")
        for key, spec in reg.items():
            present = model_path(spec).exists()
            total += spec["approx_mb"]
            print(
                f"  {key:22s} {spec['approx_mb']:>5d} MB  "
                f"{'present' if present else 'missing'}  "
                f"{spec['hf_repo']}/{spec['filename']}"
            )
    print(f"\ntotal download if all missing: ~{total / 1024:.1f} GB")

"""Paths and model defaults for the local Gemma 3 270M runtime."""

from __future__ import annotations

import os
from pathlib import Path

# Package root: .../galadriel-public/local_llm
PACKAGE_DIR = Path(__file__).resolve().parent
# Weights live under the package so downloads stay scoped to this module.
MODELS_DIR = Path(os.environ.get("LOCAL_LLM_MODELS_DIR", PACKAGE_DIR / "models")).resolve()

# Same family as Ollama's gemma3:270m (instruction-tuned Gemma 3 270M).
# QAT checkpoints keep quality under 4-bit; Q4_K_M is the server default.
HF_REPO = os.environ.get(
    "LOCAL_LLM_HF_REPO",
    "bartowski/google_gemma-3-270m-it-qat-GGUF",
)
QUANT = os.environ.get("LOCAL_LLM_QUANT", "Q4_K_M")
MODEL_FILENAME = os.environ.get(
    "LOCAL_LLM_MODEL_FILENAME",
    f"google_gemma-3-270m-it-qat-{QUANT}.gguf",
)
MODEL_ID = os.environ.get("LOCAL_LLM_MODEL_ID", "gemma3:270m")

# Context: 270M is tiny; keep KV cache small for Fargate/CPU boxes.
N_CTX = int(os.environ.get("LOCAL_LLM_N_CTX", "8192"))
N_BATCH = int(os.environ.get("LOCAL_LLM_N_BATCH", "512"))
N_UBATCH = int(os.environ.get("LOCAL_LLM_N_UBATCH", "512"))

# OpenAI-compatible HTTP defaults
DEFAULT_HOST = os.environ.get("LOCAL_LLM_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("LOCAL_LLM_PORT", "8088"))


def default_model_path() -> Path:
    return MODELS_DIR / MODEL_FILENAME


def hf_file_url(filename: str | None = None) -> str:
    name = filename or MODEL_FILENAME
    return f"https://huggingface.co/{HF_REPO}/resolve/main/{name}"

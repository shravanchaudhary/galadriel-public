"""Paths and model defaults for the local Gemma Stage-2 SLM runtime."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# Package root: .../galadriel-public/local_llm
PACKAGE_DIR = Path(__file__).resolve().parent
# Weights live under the package so downloads stay scoped to this module.
MODELS_DIR = Path(os.environ.get("LOCAL_LLM_MODELS_DIR", PACKAGE_DIR / "models")).resolve()

QUANT = os.environ.get("LOCAL_LLM_QUANT", "Q4_K_M")

# Selectable Stage-2 recall verify profiles (UI + tower_settings).
# Keys are stable; filenames follow bartowski QAT GGUF naming.
RECALL_SLM_MODEL_PROFILES: dict[str, dict[str, str]] = {
    "270m": {
        "key": "270m",
        "label": "Gemma 3 270M",
        "model_id": "gemma3:270m",
        "hf_repo": "bartowski/google_gemma-3-270m-it-qat-GGUF",
        "filename": f"google_gemma-3-270m-it-qat-{QUANT}.gguf",
    },
    "1b": {
        "key": "1b",
        "label": "Gemma 3 1B",
        "model_id": "gemma3:1b",
        "hf_repo": "bartowski/google_gemma-3-1b-it-qat-GGUF",
        "filename": f"google_gemma-3-1b-it-qat-{QUANT}.gguf",
    },
}

DEFAULT_RECALL_SLM_MODEL = "1b"
RECALL_SLM_MODEL_OPTIONS: tuple[str, ...] = tuple(RECALL_SLM_MODEL_PROFILES.keys())

# Module defaults track the active default profile (1B). Override via env for
# local experiments or emergency rollback without code changes.
_DEFAULT_PROFILE = RECALL_SLM_MODEL_PROFILES[DEFAULT_RECALL_SLM_MODEL]
HF_REPO = os.environ.get("LOCAL_LLM_HF_REPO", _DEFAULT_PROFILE["hf_repo"])
MODEL_FILENAME = os.environ.get(
    "LOCAL_LLM_MODEL_FILENAME",
    _DEFAULT_PROFILE["filename"],
)
MODEL_ID = os.environ.get("LOCAL_LLM_MODEL_ID", _DEFAULT_PROFILE["model_id"])

# Context: Stage-2 verify prompts are short; keep KV cache modest for Fargate/CPU.
N_CTX = int(os.environ.get("LOCAL_LLM_N_CTX", "8192"))
N_BATCH = int(os.environ.get("LOCAL_LLM_N_BATCH", "512"))
N_UBATCH = int(os.environ.get("LOCAL_LLM_N_UBATCH", "512"))

# OpenAI-compatible HTTP defaults
DEFAULT_HOST = os.environ.get("LOCAL_LLM_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("LOCAL_LLM_PORT", "8088"))


def resolve_model_profile(key: str | None = None) -> dict[str, str]:
    """Return a copy of the profile for key (default: DEFAULT_RECALL_SLM_MODEL)."""
    k = (key or DEFAULT_RECALL_SLM_MODEL).strip().lower()
    if k not in RECALL_SLM_MODEL_PROFILES:
        raise ValueError(
            f"Unknown recall SLM model {key!r}; "
            f"expected one of {list(RECALL_SLM_MODEL_OPTIONS)}"
        )
    return dict(RECALL_SLM_MODEL_PROFILES[k])


def model_path_for(key: str | None = None) -> Path:
    profile = resolve_model_profile(key)
    return MODELS_DIR / profile["filename"]


def default_model_path() -> Path:
    return MODELS_DIR / MODEL_FILENAME


def hf_file_url(filename: str | None = None, *, repo: str | None = None) -> str:
    name = filename or MODEL_FILENAME
    r = repo or HF_REPO
    return f"https://huggingface.co/{r}/resolve/main/{name}"


def profile_hf_url(key: str | None = None) -> str:
    profile = resolve_model_profile(key)
    return hf_file_url(profile["filename"], repo=profile["hf_repo"])


def list_model_profiles() -> list[dict[str, Any]]:
    """UI/API-friendly profile list with on-disk presence."""
    out: list[dict[str, Any]] = []
    for key in RECALL_SLM_MODEL_OPTIONS:
        profile = resolve_model_profile(key)
        path = model_path_for(key)
        out.append({
            **profile,
            "path": str(path),
            "present": path.exists() and path.stat().st_size > 1_000_000,
        })
    return out

"""In-process Gemma Stage-2 SLM runtime (llama.cpp / GGUF).

No Ollama dependency. Selectable profiles: gemma3:270m and gemma3:1b,
loaded from repo-local QAT GGUFs.
"""

from .client import LocalLLMClient
from .config import (
    DEFAULT_RECALL_SLM_MODEL,
    MODEL_ID,
    MODELS_DIR,
    RECALL_SLM_MODEL_OPTIONS,
    RECALL_SLM_MODEL_PROFILES,
    default_model_path,
    list_model_profiles,
    model_path_for,
    resolve_model_profile,
)
from .download import ensure_all_profile_models, ensure_model, ensure_profile_model
from .engine import LocalGemma

__all__ = [
    "LocalGemma",
    "LocalLLMClient",
    "MODEL_ID",
    "MODELS_DIR",
    "DEFAULT_RECALL_SLM_MODEL",
    "RECALL_SLM_MODEL_OPTIONS",
    "RECALL_SLM_MODEL_PROFILES",
    "default_model_path",
    "model_path_for",
    "resolve_model_profile",
    "list_model_profiles",
    "ensure_model",
    "ensure_profile_model",
    "ensure_all_profile_models",
]

"""In-process Gemma 3 270M runtime (llama.cpp / GGUF), OpenAI-compatible surface.

No Ollama dependency. Same instruction-tuned Gemma 3 270M family as
`ollama run gemma3:270m`, loaded from a repo-local QAT GGUF.
"""

from .client import LocalLLMClient
from .config import MODEL_ID, MODELS_DIR, default_model_path
from .download import ensure_model
from .engine import LocalGemma

__all__ = [
    "LocalGemma",
    "LocalLLMClient",
    "MODEL_ID",
    "MODELS_DIR",
    "default_model_path",
    "ensure_model",
]

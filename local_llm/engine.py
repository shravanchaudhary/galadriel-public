"""llama.cpp-backed Gemma 3 1B engine with Mac/Linux runtime tuning."""

from __future__ import annotations

import os
import platform
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .config import MODEL_ID, N_BATCH, N_CTX, N_UBATCH, default_model_path
from .download import ensure_model


def _cpu_count() -> int:
    return max(1, os.cpu_count() or 1)


def detect_runtime() -> dict[str, Any]:
    """Pick llama.cpp knobs for this host (Metal on macOS, CUDA/CPU on Linux)."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    n_threads = int(os.environ.get("LOCAL_LLM_N_THREADS", str(_cpu_count())))
    # Leave one core free on multi-core servers so the web worker stays responsive.
    if "LOCAL_LLM_N_THREADS" not in os.environ and n_threads > 2:
        n_threads = max(1, n_threads - 1)

    n_gpu_layers = int(os.environ.get("LOCAL_LLM_N_GPU_LAYERS", "-1"))
    backend = "cpu"

    if system == "darwin":
        backend = "metal"
        # -1 = offload all layers to Metal when the wheel was built with GGML_METAL.
        if "LOCAL_LLM_N_GPU_LAYERS" not in os.environ:
            n_gpu_layers = -1
    elif system == "linux":
        if os.environ.get("LOCAL_LLM_FORCE_CPU", "").lower() in {"1", "true", "yes"}:
            backend = "cpu"
            n_gpu_layers = 0
        elif shutil_which_nvidia():
            backend = "cuda"
            if "LOCAL_LLM_N_GPU_LAYERS" not in os.environ:
                n_gpu_layers = -1
        else:
            backend = "cpu"
            if "LOCAL_LLM_N_GPU_LAYERS" not in os.environ:
                n_gpu_layers = 0

    return {
        "system": system,
        "machine": machine,
        "backend": backend,
        "n_threads": n_threads,
        "n_gpu_layers": n_gpu_layers,
        # Flash attention helps when the build supports it; llama-cpp ignores if not.
        "flash_attn": os.environ.get("LOCAL_LLM_FLASH_ATTN", "1") not in {"0", "false", "no"},
        "use_mmap": os.environ.get("LOCAL_LLM_MMAP", "1") not in {"0", "false", "no"},
        # mlock reduces page-fault jitter on Linux servers with enough RAM.
        "use_mlock": os.environ.get(
            "LOCAL_LLM_MLOCK",
            "1" if system == "linux" else "0",
        )
        not in {"0", "false", "no"},
    }


def shutil_which_nvidia() -> bool:
    # Avoid importing shutil at module top only for this; keep dep surface tiny.
    from shutil import which

    return which("nvidia-smi") is not None


@dataclass
class CompletionResult:
    text: str
    model: str
    finish_reason: str
    prompt_tokens: int
    completion_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LocalGemma:
    """Thread-safe wrapper around a single loaded GGUF."""

    def __init__(
        self,
        model_path: Path | str | None = None,
        *,
        ensure: bool = True,
        n_ctx: int | None = None,
        verbose: bool = False,
        model_id: str | None = None,
        hf_repo: str | None = None,
    ) -> None:
        self.model_id = model_id or MODEL_ID
        path = Path(model_path) if model_path else default_model_path()
        if ensure and not path.exists():
            path = ensure_model(filename=path.name, repo=hf_repo)
        if not path.exists():
            raise FileNotFoundError(
                f"model not found at {path}; run: python -m local_llm download"
            )
        self.model_path = path.resolve()
        self.n_ctx = n_ctx or N_CTX
        self.verbose = verbose
        self._runtime = detect_runtime()
        self._lock = threading.RLock()
        self._llm = self._load()
        self._yes_token_id: int | None = None
        self._no_token_id: int | None = None

    def _load(self):
        try:
            from llama_cpp import Llama
        except ImportError as exc:
            raise ImportError(
                "llama-cpp-python is required. Install with:\n"
                "  pip install -r requirements-local-llm.txt\n"
                "macOS (Metal): CMAKE_ARGS='-DGGML_METAL=on' pip install llama-cpp-python\n"
                "Linux CUDA:    CMAKE_ARGS='-DGGML_CUDA=on' pip install llama-cpp-python"
            ) from exc

        rt = self._runtime
        kwargs: dict[str, Any] = {
            "model_path": str(self.model_path),
            "n_ctx": self.n_ctx,
            "n_batch": N_BATCH,
            "n_ubatch": N_UBATCH,
            "n_threads": rt["n_threads"],
            "n_gpu_layers": rt["n_gpu_layers"],
            "use_mmap": rt["use_mmap"],
            "use_mlock": rt["use_mlock"],
            # Needed for yes_no_logit_margin (Stage-2 recall verify).
            "logits_all": True,
            "embedding": False,
            "verbose": self.verbose,
            # Gemma 3 chat template is embedded in the GGUF; let llama.cpp apply it.
            "chat_format": None,
        }
        # Optional kwargs that older wheels may not accept.
        try:
            return Llama(**kwargs, flash_attn=rt["flash_attn"])
        except TypeError:
            kwargs.pop("flash_attn", None)
            return Llama(**kwargs)

    @property
    def runtime(self) -> dict[str, Any]:
        return dict(self._runtime)

    def close(self) -> None:
        with self._lock:
            llm = self._llm
            self._llm = None
            self._yes_token_id = None
            self._no_token_id = None
        # Drop llama.cpp handle promptly so hot-swap does not keep two models.
        del llm
        try:
            import gc

            gc.collect()
        except Exception:  # noqa: BLE001 — best-effort reclaim
            pass

    def __enter__(self) -> LocalGemma:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def complete(
        self,
        prompt: str,
        *,
        max_tokens: int = 256,
        temperature: float = 0.0,
        top_p: float = 0.95,
        stop: list[str] | None = None,
        stream: bool = False,
    ) -> CompletionResult | Iterator[str]:
        if stream:
            return self._complete_stream(
                prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                stop=stop,
            )
        with self._lock:
            out = self._llm(
                prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                stop=stop or [],
                echo=False,
            )
        choice = out["choices"][0]
        usage = out.get("usage") or {}
        return CompletionResult(
            text=choice.get("text") or "",
            model=self.model_id,
            finish_reason=choice.get("finish_reason") or "stop",
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )

    def _complete_stream(
        self,
        prompt: str,
        *,
        max_tokens: int,
        temperature: float,
        top_p: float,
        stop: list[str] | None,
    ) -> Iterator[str]:
        with self._lock:
            stream = self._llm(
                prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                stop=stop or [],
                echo=False,
                stream=True,
            )
            for chunk in stream:
                text = chunk["choices"][0].get("text") or ""
                if text:
                    yield text

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = 256,
        temperature: float = 0.0,
        top_p: float = 0.95,
        stop: list[str] | None = None,
        stream: bool = False,
    ) -> CompletionResult | Iterator[str]:
        if stream:
            return self._chat_stream(
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                stop=stop,
            )
        with self._lock:
            out = self._llm.create_chat_completion(
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                stop=stop or [],
            )
        choice = out["choices"][0]
        message = choice.get("message") or {}
        usage = out.get("usage") or {}
        return CompletionResult(
            text=message.get("content") or "",
            model=self.model_id,
            finish_reason=choice.get("finish_reason") or "stop",
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )

    def _chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int,
        temperature: float,
        top_p: float,
        stop: list[str] | None,
    ) -> Iterator[str]:
        with self._lock:
            stream = self._llm.create_chat_completion(
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                stop=stop or [],
                stream=True,
            )
            for chunk in stream:
                delta = (chunk["choices"][0].get("delta") or {}).get("content") or ""
                if delta:
                    yield delta

    def _yes_no_token_ids(self) -> tuple[int, int]:
        if self._yes_token_id is None or self._no_token_id is None:
            yes_ids = self._llm.tokenize(b"YES", add_bos=False)
            no_ids = self._llm.tokenize(b"NO", add_bos=False)
            if len(yes_ids) != 1 or len(no_ids) != 1:
                raise RuntimeError("YES/NO did not tokenize to single tokens")
            self._yes_token_id = int(yes_ids[0])
            self._no_token_id = int(no_ids[0])
        return self._yes_token_id, self._no_token_id

    def yes_no_logit_margin(self, prompt: str) -> float:
        """Return logit(YES) - logit(NO) for the next token after prompt.

        Positive means the model prefers YES. Used by recall Stage-2 verify;
        tiny instruction-tuned models are more reliable on logits than free-text.
        """
        with self._lock:
            yes_id, no_id = self._yes_no_token_ids()
            tokens = self._llm.tokenize(prompt.encode("utf-8"), add_bos=True)
            if not tokens:
                raise ValueError("empty prompt")
            self._llm.reset()
            self._llm.eval(tokens)
            logits = self._llm.scores[len(tokens) - 1]
            return float(logits[yes_id]) - float(logits[no_id])

    def openai_models(self) -> dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {
                    "id": self.model_id,
                    "object": "model",
                    "owned_by": "local_llm",
                    "path": str(self.model_path),
                    "runtime": self.runtime,
                }
            ],
        }

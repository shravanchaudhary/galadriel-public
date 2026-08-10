"""OpenAI-compatible client for the local Gemma runtime.

Works two ways:
  1. In-process (preferred for harness code): LocalLLMClient(in_process=True)
  2. HTTP against `python -m local_llm serve`
"""

from __future__ import annotations

from typing import Any, Iterator
from urllib.parse import urljoin

import httpx

from .config import DEFAULT_HOST, DEFAULT_PORT, MODEL_ID
from .engine import CompletionResult, LocalGemma


class LocalLLMClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        in_process: bool = True,
        engine: LocalGemma | None = None,
        model: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.model = model or MODEL_ID
        self.in_process = in_process
        self._engine = engine
        self.base_url = (base_url or f"http://{DEFAULT_HOST}:{DEFAULT_PORT}").rstrip("/") + "/"
        self.timeout = timeout

    def _get_engine(self) -> LocalGemma:
        if self._engine is None:
            self._engine = LocalGemma()
        return self._engine

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
        if self.in_process:
            return self._get_engine().chat(
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                stop=stop,
                stream=stream,
            )
        return self._http_chat(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            stop=stop,
            stream=stream,
        )

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
        if self.in_process:
            return self._get_engine().complete(
                prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                stop=stop,
                stream=stream,
            )
        return self._http_complete(
            prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            stop=stop,
            stream=stream,
        )

    def yes_no_logit_margin(self, prompt: str) -> float:
        """In-process only: logit(YES) - logit(NO) for Stage-2 recall verify."""
        if not self.in_process:
            raise RuntimeError("yes_no_logit_margin requires in_process=True")
        return self._get_engine().yes_no_logit_margin(prompt)

    def _http_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int,
        temperature: float,
        top_p: float,
        stop: list[str] | None,
        stream: bool,
    ) -> CompletionResult | Iterator[str]:
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "stop": stop,
            "stream": stream,
        }
        url = urljoin(self.base_url, "v1/chat/completions")
        if stream:
            return self._iter_sse_content(url, payload, mode="chat")
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        choice = data["choices"][0]
        usage = data.get("usage") or {}
        return CompletionResult(
            text=(choice.get("message") or {}).get("content") or "",
            model=data.get("model") or self.model,
            finish_reason=choice.get("finish_reason") or "stop",
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )

    def _http_complete(
        self,
        prompt: str,
        *,
        max_tokens: int,
        temperature: float,
        top_p: float,
        stop: list[str] | None,
        stream: bool,
    ) -> CompletionResult | Iterator[str]:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "stop": stop,
            "stream": stream,
        }
        url = urljoin(self.base_url, "v1/completions")
        if stream:
            return self._iter_sse_content(url, payload, mode="completion")
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        choice = data["choices"][0]
        usage = data.get("usage") or {}
        return CompletionResult(
            text=choice.get("text") or "",
            model=data.get("model") or self.model,
            finish_reason=choice.get("finish_reason") or "stop",
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )

    def _iter_sse_content(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        mode: str,
    ) -> Iterator[str]:
        with httpx.Client(timeout=self.timeout) as client:
            with client.stream("POST", url, json=payload) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line:
                        continue
                    if line.startswith("data: "):
                        data = line[6:].strip()
                        if data == "[DONE]":
                            break
                        import json

                        obj = json.loads(data)
                        choice = obj["choices"][0]
                        if mode == "chat":
                            text = (choice.get("delta") or {}).get("content") or ""
                        else:
                            text = choice.get("text") or ""
                        if text:
                            yield text

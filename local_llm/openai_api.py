"""Minimal OpenAI-compatible HTTP API over LocalGemma."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .engine import LocalGemma

_ENGINE: LocalGemma | None = None


def get_engine() -> LocalGemma:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = LocalGemma()
    return _ENGINE


def create_app(engine: LocalGemma | None = None) -> FastAPI:
    global _ENGINE
    if engine is not None:
        _ENGINE = engine

    app = FastAPI(title="local_llm", version="0.1.0")

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        eng = get_engine()
        return {"ok": True, "model": eng.model_id, "runtime": eng.runtime}

    @app.get("/v1/models")
    def list_models() -> dict[str, Any]:
        return get_engine().openai_models()

    @app.post("/v1/completions")
    async def completions(request: Request):
        body = await request.json()
        eng = get_engine()
        prompt = body.get("prompt")
        if prompt is None:
            raise HTTPException(status_code=400, detail="prompt is required")
        if isinstance(prompt, list):
            prompt = "".join(str(p) for p in prompt)

        max_tokens = int(body.get("max_tokens") or 256)
        temperature = float(body.get("temperature") if body.get("temperature") is not None else 0.0)
        top_p = float(body.get("top_p") if body.get("top_p") is not None else 0.95)
        stop = body.get("stop")
        if isinstance(stop, str):
            stop = [stop]
        stream = bool(body.get("stream"))
        model = body.get("model") or eng.model_id
        created = int(time.time())
        completion_id = f"cmpl-{uuid.uuid4().hex[:24]}"

        if stream:
            return StreamingResponse(
                _stream_completion(
                    eng,
                    prompt=str(prompt),
                    max_tokens=max_tokens,
                    temperature=temperature,
                    top_p=top_p,
                    stop=stop,
                    model=model,
                    created=created,
                    completion_id=completion_id,
                ),
                media_type="text/event-stream",
            )

        result = eng.complete(
            str(prompt),
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            stop=stop,
            stream=False,
        )
        return {
            "id": completion_id,
            "object": "text_completion",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "text": result.text,
                    "finish_reason": result.finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "total_tokens": result.total_tokens,
            },
        }

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        body = await request.json()
        eng = get_engine()
        messages = body.get("messages")
        if not messages:
            raise HTTPException(status_code=400, detail="messages is required")

        max_tokens = int(body.get("max_tokens") or 256)
        temperature = float(body.get("temperature") if body.get("temperature") is not None else 0.0)
        top_p = float(body.get("top_p") if body.get("top_p") is not None else 0.95)
        stop = body.get("stop")
        if isinstance(stop, str):
            stop = [stop]
        stream = bool(body.get("stream"))
        model = body.get("model") or eng.model_id
        created = int(time.time())
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

        if stream:
            return StreamingResponse(
                _stream_chat(
                    eng,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    top_p=top_p,
                    stop=stop,
                    model=model,
                    created=created,
                    completion_id=completion_id,
                ),
                media_type="text/event-stream",
            )

        result = eng.chat(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            stop=stop,
            stream=False,
        )
        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": result.text},
                    "finish_reason": result.finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "total_tokens": result.total_tokens,
            },
        }

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception):
        return JSONResponse(status_code=500, content={"error": {"message": str(exc), "type": type(exc).__name__}})

    return app


async def _stream_completion(
    eng: LocalGemma,
    *,
    prompt: str,
    max_tokens: int,
    temperature: float,
    top_p: float,
    stop: list[str] | None,
    model: str,
    created: int,
    completion_id: str,
) -> AsyncIterator[str]:
    for text in eng.complete(
        prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        top_p=top_p,
        stop=stop,
        stream=True,
    ):
        payload = {
            "id": completion_id,
            "object": "text_completion",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "text": text, "finish_reason": None}],
        }
        yield f"data: {json.dumps(payload)}\n\n"
    final = {
        "id": completion_id,
        "object": "text_completion",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "text": "", "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(final)}\n\n"
    yield "data: [DONE]\n\n"


async def _stream_chat(
    eng: LocalGemma,
    *,
    messages: list[dict[str, Any]],
    max_tokens: int,
    temperature: float,
    top_p: float,
    stop: list[str] | None,
    model: str,
    created: int,
    completion_id: str,
) -> AsyncIterator[str]:
    for text in eng.chat(
        messages,
        max_tokens=max_tokens,
        temperature=temperature,
        top_p=top_p,
        stop=stop,
        stream=True,
    ):
        payload = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": text},
                    "finish_reason": None,
                }
            ],
        }
        yield f"data: {json.dumps(payload)}\n\n"
    final = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(final)}\n\n"
    yield "data: [DONE]\n\n"


app = create_app()

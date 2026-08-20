"""Transient-error retry for LLM provider calls.

Worker / scheduler / Discord layers stay dumb: they call `create_message` /
`stream_message` once. This module owns backoff for 429 / 5xx / timeouts so a
brief Gemini UNAVAILABLE (or Anthropic overload) cools down inside the call
instead of killing the turn with a raw traceback.

Honours `Retry-After` when the API sends it; otherwise exponential jitter.
Stops after a fixed attempt budget — outer cancellation (turn cancel, process
shutdown) still aborts immediately because `CancelledError` is not retried.

The budget is deliberately small. It used to be 8 attempts with delays up to
60s, so a sustained rate limit spent minutes silently backing off and then
surfaced as a bare exception string. Three attempts fail fast enough that the
real error reaches the user while it still explains anything; `describe_error`
turns that exception into the payload the UI renders.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar

import httpx
import tenacity

log = logging.getLogger("galadriel.llm_retry")

T = TypeVar("T")

# Match google-genai's default retriable set, plus Anthropic's 529 overloaded.
RETRYABLE_HTTP_CODES = frozenset({408, 429, 500, 502, 503, 504, 529})

# Three attempts total (two retries). Applies to every provider and every error
# class: past this the request is reported, not retried.
DEFAULT_ATTEMPTS = 3
DEFAULT_INITIAL_DELAY = 1.0
# Capped well below the old 60s: a cooldown longer than this outlives the
# user's patience, and the point now is to surface the error, not outwait it.
DEFAULT_MAX_DELAY = 8.0
DEFAULT_EXP_BASE = 2.0

# "Please retry in 12.5s" / "retry after 30 seconds" in Gemini error bodies.
_RETRY_IN_BODY_RE = re.compile(
    r"retry\s+(?:in|after)\s+(\d+(?:\.\d+)?)\s*(?:s|sec|seconds)?",
    re.IGNORECASE,
)


def is_transient_llm_error(exc: BaseException) -> bool:
    """True for rate limits, overload, and transport blips — not auth/4xx bugs."""
    if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
        return False
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)):
        return True

    # google.genai.errors.APIError (and subclasses) expose `.code`.
    code = getattr(exc, "code", None)
    if isinstance(code, int) and code in RETRYABLE_HTTP_CODES:
        return True

    # anthropic.APIStatusError and friends expose `.status_code`.
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status in RETRYABLE_HTTP_CODES:
        return True

    # Duck-type common SDK timeout / connection names without importing every
    # provider package at module load.
    name = type(exc).__name__
    if name in {
        "APITimeoutError",
        "APIConnectionError",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
    }:
        return True

    return False


def _header_retry_after(exc: BaseException) -> float | None:
    response = getattr(exc, "response", None)
    if response is None:
        return None
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw = None
    try:
        raw = headers.get("retry-after")
    except Exception:
        raw = None
    if raw is None and isinstance(headers, dict):
        raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def _body_retry_after(exc: BaseException) -> float | None:
    for attr in ("message", "body"):
        blob = getattr(exc, attr, None)
        if blob is None:
            continue
        text = blob if isinstance(blob, str) else str(blob)
        m = _RETRY_IN_BODY_RE.search(text)
        if m:
            try:
                return max(0.0, float(m.group(1)))
            except ValueError:
                continue
    details = getattr(exc, "details", None)
    if details is not None:
        m = _RETRY_IN_BODY_RE.search(str(details))
        if m:
            try:
                return max(0.0, float(m.group(1)))
            except ValueError:
                pass
    return None


def retry_after_seconds(exc: BaseException) -> float | None:
    """Seconds the API asked us to wait, or None if it didn't say."""
    return _header_retry_after(exc) or _body_retry_after(exc)


def _unwrap_json_message(text: str, _depth: int = 0) -> str:
    """Peel off a JSON-encoded error body that's been stuffed into a message.

    google-genai's `APIError._get_message` prefers a top-level `message` key
    over `error.message`, but some backends (Gemini's own quota errors) put
    the *entire* `{"error": {...}}` body, JSON-encoded as a string, in that
    top-level `message` field. Left alone, the UI renders that raw blob
    instead of the one sentence a human should see. Capped depth guards
    against a pathological double-wrap.
    """
    if _depth >= 3:
        return text
    stripped = text.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return text
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        return text
    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, dict) and error.get("message"):
            return _unwrap_json_message(str(error["message"]), _depth + 1)
        if parsed.get("message"):
            return _unwrap_json_message(str(parsed["message"]), _depth + 1)
    return text


def _error_message(exc: BaseException) -> str:
    """The provider's own explanation, preferred over the exception repr.

    SDKs vary: google-genai puts the useful text in `.message`, the OpenAI and
    Anthropic SDKs put a parsed dict in `.body` with the real reason under
    `error.message` (or `message` on Bedrock's own errors). `str(exc)` is the
    last resort because for some SDKs it is just "Error code: 429".
    """
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return _unwrap_json_message(str(error["message"]))
        if body.get("message"):
            return _unwrap_json_message(str(body["message"]))
    message = getattr(exc, "message", None)
    if isinstance(message, str) and message.strip():
        return _unwrap_json_message(message.strip())
    text = str(exc).strip()
    return _unwrap_json_message(text) if text else type(exc).__name__


def describe_error(
    exc: BaseException,
    *,
    provider: str | None = None,
    model: str | None = None,
    attempts: int | None = None,
) -> dict:
    """Structured description of a failed LLM call, for logs and the UI.

    Callers surface this instead of `str(exc)`, which for most SDKs is an opaque
    "Error code: 429" that tells the user nothing about which provider failed or
    why.
    """
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        code = getattr(exc, "code", None)
        status = code if isinstance(code, int) else None
    return {
        "kind": type(exc).__name__,
        "provider": provider,
        "model": model,
        "status": status,
        "message": _error_message(exc),
        "retryable": is_transient_llm_error(exc),
        "attempts": attempts if attempts is not None else DEFAULT_ATTEMPTS,
        "retry_after": retry_after_seconds(exc),
    }


def format_error(detail: dict) -> str:
    """One-line human summary of a `describe_error` payload."""
    parts = []
    if detail.get("model"):
        parts.append(str(detail["model"]))
    elif detail.get("provider"):
        parts.append(str(detail["provider"]))
    if detail.get("status") is not None:
        parts.append(f"HTTP {detail['status']}")
    prefix = " · ".join(parts)
    message = detail.get("message") or detail.get("kind") or "unknown error"
    line = f"{prefix}: {message}" if prefix else str(message)
    if detail.get("retryable") and detail.get("attempts"):
        line += f" (gave up after {detail['attempts']} attempts)"
    return line


def wait_seconds(
    attempt_number: int,
    exc: BaseException | None = None,
    *,
    initial: float = DEFAULT_INITIAL_DELAY,
    maximum: float = DEFAULT_MAX_DELAY,
    exp_base: float = DEFAULT_EXP_BASE,
) -> float:
    """Cooldown before the next attempt (1-based attempt_number of the failure)."""
    if exc is not None:
        ra = retry_after_seconds(exc)
        if ra is not None:
            # Small jitter so concurrent worker + reflection don't stampede.
            return min(maximum, ra + random.uniform(0, 1))

    delay = initial * (exp_base ** max(attempt_number - 1, 0))
    delay = min(maximum, delay)
    return delay + random.uniform(0, min(1.0, delay * 0.25))


def _wait_tenacity(retry_state: tenacity.RetryCallState) -> float:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    return wait_seconds(retry_state.attempt_number, exc)


def _before_sleep(retry_state: tenacity.RetryCallState) -> None:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    sleep = retry_state.next_action.sleep if retry_state.next_action else "?"
    kind = type(exc).__name__ if exc else "error"
    detail = ""
    if exc is not None:
        code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        if code is not None:
            detail = f" HTTP {code}"
        ra = retry_after_seconds(exc)
        if ra is not None:
            detail += f" Retry-After={ra:.1f}s"
    log.warning(
        f"LLM call transient failure ({kind}{detail}); "
        f"backing off {sleep}s before attempt {retry_state.attempt_number + 1}"
    )


def llm_retrying(
    *,
    attempts: int = DEFAULT_ATTEMPTS,
) -> tenacity.AsyncRetrying:
    """AsyncRetrying configured for provider LLM calls."""
    return tenacity.AsyncRetrying(
        stop=tenacity.stop_after_attempt(attempts),
        wait=_wait_tenacity,
        retry=tenacity.retry_if_exception(is_transient_llm_error),
        before_sleep=_before_sleep,
        reraise=True,
    )


DETAIL_ATTR = "galadriel_error_detail"


def annotate(exc: BaseException, provider=None, model=None, attempts=None) -> None:
    """Attach a `describe_error` payload to the exception on its way out.

    The Tower stream handler that renders the failure is far from the provider
    that knows which model and backend were involved, and the exception is the
    only thing that travels between them.
    """
    try:
        setattr(
            exc,
            DETAIL_ATTR,
            describe_error(exc, provider=provider, model=model, attempts=attempts),
        )
    except Exception:
        pass  # never let error reporting mask the original failure


def error_detail(exc: BaseException) -> dict:
    """Structured detail for `exc`, computed on the spot if none was attached."""
    detail = getattr(exc, DETAIL_ATTR, None)
    return detail if isinstance(detail, dict) else describe_error(exc)


async def with_llm_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    provider: str | None = None,
    model: str | None = None,
) -> T:
    """Run an async LLM call, retrying transient failures with cooldown."""
    try:
        async for attempt in llm_retrying(attempts=attempts):
            with attempt:
                return await fn()
    except BaseException as exc:
        if not isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
            annotate(exc, provider, model, attempts)
        raise
    raise RuntimeError("llm_retrying exhausted without result")  # pragma: no cover


async def stream_with_llm_retry(
    factory: Callable[[], AsyncIterator[Any]],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    provider: str | None = None,
    model: str | None = None,
) -> AsyncIterator[Any]:
    """Retry a streaming LLM call only while nothing has been yielded yet.

    Once the first delta reaches the caller we cannot safely restart (UI would
    see duplicate text), so mid-stream failures propagate.
    """
    last_exc: BaseException | None = None
    for attempt_number in range(1, attempts + 1):
        yielded = False
        try:
            async for item in factory():
                yielded = True
                yield item
            return
        except BaseException as exc:
            if yielded or not is_transient_llm_error(exc) or attempt_number >= attempts:
                if not isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
                    annotate(exc, provider, model, attempts)
                raise
            last_exc = exc
            sleep_for = wait_seconds(attempt_number, exc)
            code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
            detail = f" HTTP {code}" if code is not None else ""
            ra = retry_after_seconds(exc)
            if ra is not None:
                detail += f" Retry-After={ra:.1f}s"
            log.warning(
                f"LLM stream transient failure ({type(exc).__name__}{detail}); "
                f"backing off {sleep_for:.1f}s before attempt {attempt_number + 1}"
            )
            await asyncio.sleep(sleep_for)
    if last_exc is not None:  # pragma: no cover
        raise last_exc

"""In-process Headroom compression for the agent API path.

Compresses a *copy* of messages before each provider call. Never mutates
stored conversation history. Uses Headroom's pipeline directly so we can
pass ``frozen_message_count`` (the public ``compress()`` helper drops it).

Cache-safe defaults: user/assistant text protected; tool_result / structured
tool output is the main target. Failures passthrough originals — never break
a turn.

Also prunes old screenshot image blocks from the API-bound copy (keep last N)
so browser sessions do not balloon context with base64 that Headroom cannot
compress.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("galadriel.headroom")

# Cache-safe: do not rewrite user/assistant prose. Tool outputs compress.
_COMPRESS_USER = False
_COMPRESS_SYSTEM = True
_PROTECT_RECENT = 0  # freeze window owns "what's already been sent"
_PROTECT_ANALYSIS = True
_MIN_TOKENS = 250

@dataclass(frozen=True)
class HeadroomMetrics:
    tokens_before: int = 0
    tokens_after: int = 0
    tokens_saved: int = 0
    compression_ratio: float = 0.0
    transforms_applied: tuple[str, ...] = ()

    def as_cost_fields(self, enabled: bool) -> dict[str, Any]:
        return {
            "headroom_enabled": enabled,
            "headroom_tokens_before": self.tokens_before,
            "headroom_tokens_after": self.tokens_after,
            "headroom_tokens_saved": self.tokens_saved,
        }


_EMPTY = HeadroomMetrics()


def _image_refs(messages: list[dict]) -> list[tuple[int, tuple]]:
    """(msg_idx, location) for every image block, in chronological order.

    Walks Anthropic-style content: top-level ``image`` blocks and ``image``
    blocks nested inside ``tool_result`` list content. location is
    ("top", block_idx) or ("tool_result", block_idx, inner_idx).
    """
    refs: list[tuple[int, tuple]] = []
    for mi, msg in enumerate(messages):
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for bi, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "image":
                refs.append((mi, ("top", bi)))
                continue
            if block.get("type") != "tool_result":
                continue
            inner = block.get("content")
            if not isinstance(inner, list):
                continue
            for ii, ib in enumerate(inner):
                if isinstance(ib, dict) and ib.get("type") == "image":
                    refs.append((mi, ("tool_result", bi, ii)))
    return refs


def _replace_images(
    messages: list[dict],
    refs: list[tuple[int, tuple]],
    text: str,
) -> None:
    """Swap each referenced image for a `text` block, in place.

    Lengths never change, so the indices in `refs` stay valid throughout.
    """
    for mi, loc in refs:
        content = messages[mi]["content"]
        if loc[0] == "top":
            _, bi = loc
            content[bi] = {"type": "text", "text": text}
        else:
            _, bi, ii = loc
            inner = content[bi]["content"]
            inner[ii] = {"type": "text", "text": text}


def strip_images(messages: list[dict], *, reason: str) -> tuple[list[dict], int]:
    """Return a deep copy with EVERY image block replaced by `reason` text.

    The vision gate for text-only models (harness/model_catalog.supports_vision).
    Applied to the API-bound copy only, so the pixels survive in the stored
    conversation and reappear if the channel moves back to a seeing model.
    """
    if not messages:
        return messages, 0
    out = copy.deepcopy(messages)
    refs = _image_refs(out)
    if not refs:
        return out, 0
    _replace_images(out, refs, reason)
    return out, len(refs)


def _compress_sync(
    messages: list[dict],
    model: str,
    frozen_message_count: int,
    model_limit: int,
) -> tuple[list[dict], HeadroomMetrics]:
    if not messages:
        return messages, HeadroomMetrics()

    try:
        from headroom.compress import _get_pipeline
    except Exception as e:
        log.warning(f"Headroom unavailable, passthrough: {e}")
        return messages, HeadroomMetrics()

    try:
        pipeline = _get_pipeline()
        result = pipeline.apply(
            messages=messages,
            model=model,
            model_limit=model_limit,
            frozen_message_count=max(0, frozen_message_count),
            compress_user_messages=_COMPRESS_USER,
            compress_system_messages=_COMPRESS_SYSTEM,
            protect_recent=_PROTECT_RECENT,
            protect_analysis_context=_PROTECT_ANALYSIS,
            min_tokens_to_compress=_MIN_TOKENS,
        )
        before = int(getattr(result, "tokens_before", 0) or 0)
        after = int(getattr(result, "tokens_after", 0) or 0)
        if after > before > 0:
            log.warning(
                f"Headroom inflated tokens ({before} -> {after}); reverting"
            )
            return messages, HeadroomMetrics(
                tokens_before=before,
                tokens_after=before,
                tokens_saved=0,
                transforms_applied=("inflation_guard:reverted",),
            )
        saved = max(0, before - after)
        ratio = (saved / before) if before > 0 else 0.0
        transforms = tuple(getattr(result, "transforms_applied", None) or ())
        out = getattr(result, "messages", messages) or messages
        metrics = HeadroomMetrics(
            tokens_before=before,
            tokens_after=after,
            tokens_saved=saved,
            compression_ratio=ratio,
            transforms_applied=transforms,
        )
        if saved:
            log.info(
                f"Headroom | before={before} after={after} saved={saved} "
                f"({ratio:.0%}) frozen={frozen_message_count}"
            )
        return out, metrics
    except Exception as e:
        log.warning(f"Headroom compress failed, passthrough: {e}", exc_info=True)
        return messages, HeadroomMetrics()


async def compress_for_api(
    messages: list[dict],
    *,
    model: str,
    frozen_message_count: int = 0,
    model_limit: int = 200_000,
) -> tuple[list[dict], HeadroomMetrics]:
    """Compress an API-bound copy off the event loop.

    Compression applies to messages past ``frozen_message_count`` only — the
    frozen prefix is bytes already sent, and nothing here may touch it. Images
    are NOT pruned per call any more: the old keep-last-3 sliding window
    rewrote an already-sent message every time a new screenshot pushed an old
    one out, busting the cached prefix mid-session on every browser step.
    Images now ride until compaction disposes of them (the estimator counts
    them large, so image-heavy buffers compact sooner), and text-only models
    never see them anyway (strip_images vision gate, which is deterministic
    per call and therefore prefix-stable).
    """
    return await asyncio.to_thread(
        _compress_sync,
        messages,
        model,
        frozen_message_count,
        model_limit,
    )

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
import re
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("galadriel.headroom")

# Cache-safe: do not rewrite user/assistant prose. Tool outputs compress.
_COMPRESS_USER = False
_COMPRESS_SYSTEM = True
_PROTECT_RECENT = 0  # freeze window owns "what's already been sent"
_PROTECT_ANALYSIS = True
_MIN_TOKENS = 250

# Anthropic computer-use default: keep the N most recent screenshots.
KEEP_LAST_SCREENSHOTS = 3

_SCREENSHOT_PATH_RE = re.compile(
    r"(?:state/screenshots/[^\s\]\"']+\.(?:png|jpe?g|webp))"
    r"|(?:[^\s\]\"']+/screenshots/[^\s\]\"']+\.(?:png|jpe?g|webp))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ScreenshotPruneStats:
    images_total: int = 0
    images_kept: int = 0
    images_pruned: int = 0


@dataclass(frozen=True)
class HeadroomMetrics:
    tokens_before: int = 0
    tokens_after: int = 0
    tokens_saved: int = 0
    compression_ratio: float = 0.0
    transforms_applied: tuple[str, ...] = ()
    images_kept: int = 0
    images_pruned: int = 0

    def as_cost_fields(self, enabled: bool) -> dict[str, Any]:
        return {
            "headroom_enabled": enabled,
            "headroom_tokens_before": self.tokens_before,
            "headroom_tokens_after": self.tokens_after,
            "headroom_tokens_saved": self.tokens_saved,
            "images_kept": self.images_kept,
            "images_pruned": self.images_pruned,
        }


_EMPTY = HeadroomMetrics()


def _path_hint_from_blocks(blocks: list) -> str | None:
    """Best-effort screenshot path from sibling text in a tool_result list."""
    for b in blocks:
        if not isinstance(b, dict) or b.get("type") != "text":
            continue
        text = b.get("text") or ""
        m = _SCREENSHOT_PATH_RE.search(text)
        if m:
            return m.group(0)
    return None


def _placeholder_for_image(path_hint: str | None) -> dict:
    if path_hint:
        text = f"[screenshot omitted — still on disk at {path_hint}]"
    else:
        text = "[screenshot omitted — still on disk under state/screenshots/]"
    return {"type": "text", "text": text}


def prune_old_screenshots(
    messages: list[dict],
    *,
    keep_last: int = KEEP_LAST_SCREENSHOTS,
) -> tuple[list[dict], ScreenshotPruneStats]:
    """Return a deep copy with all but the last ``keep_last`` images replaced.

    Walks Anthropic-style content: top-level ``image`` blocks and ``image``
    blocks nested inside ``tool_result`` list content. Does not mutate the
    input list. When ``keep_last <= 0``, every image is replaced.
    """
    if not messages:
        return messages, ScreenshotPruneStats()

    out = copy.deepcopy(messages)
    # Collect (msg_idx, location) for every image in chronological order.
    # location is ("top", block_idx) or ("tool_result", block_idx, inner_idx).
    refs: list[tuple[int, tuple]] = []
    for mi, msg in enumerate(out):
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

    total = len(refs)
    keep = max(0, int(keep_last))
    if total <= keep:
        return out, ScreenshotPruneStats(
            images_total=total, images_kept=total, images_pruned=0
        )

    drop = refs[:-keep] if keep else refs
    # Replace oldest first; indices stay valid because we replace in place
    # (same list lengths).
    for mi, loc in drop:
        content = out[mi]["content"]
        if loc[0] == "top":
            _, bi = loc
            content[bi] = _placeholder_for_image(None)
        else:
            _, bi, ii = loc
            tool_block = content[bi]
            inner = tool_block["content"]
            hint = _path_hint_from_blocks(inner)
            inner[ii] = _placeholder_for_image(hint)

    pruned = len(drop)
    kept = total - pruned
    stats = ScreenshotPruneStats(
        images_total=total, images_kept=kept, images_pruned=pruned
    )
    if pruned:
        log.info(
            f"Screenshot prune | total={total} kept={kept} pruned={pruned} "
            f"keep_last={keep}"
        )
    return out, stats


def prepare_messages_for_api(
    messages: list[dict],
    *,
    keep_last_screenshots: int = KEEP_LAST_SCREENSHOTS,
) -> tuple[list[dict], ScreenshotPruneStats]:
    """Deep-copy + prune old screenshots + merge consecutive roles for an API-bound message list."""
    out, stats = prune_old_screenshots(messages, keep_last=keep_last_screenshots)
    
    # Merge consecutive messages of the same role to satisfy strict provider requirements
    merged = []
    for msg in out:
        if not merged:
            merged.append(msg)
            continue
            
        last_msg = merged[-1]
        if last_msg.get("role") == msg.get("role") and last_msg.get("role") in ("user", "assistant"):
            new_last = copy.deepcopy(last_msg)
            
            c1 = new_last["content"] if isinstance(new_last["content"], list) else [{"type": "text", "text": new_last.get("content", "")}]
            c2 = msg["content"] if isinstance(msg["content"], list) else [{"type": "text", "text": msg.get("content", "")}]
            
            # Combine content blocks
            new_last["content"] = c1 + c2
            merged[-1] = new_last
        else:
            merged.append(msg)
            
    return merged, stats


def _compress_sync(
    messages: list[dict],
    model: str,
    frozen_message_count: int,
    model_limit: int,
    images_kept: int = 0,
    images_pruned: int = 0,
) -> tuple[list[dict], HeadroomMetrics]:
    if not messages:
        return messages, HeadroomMetrics(
            images_kept=images_kept, images_pruned=images_pruned
        )

    try:
        from headroom.compress import _get_pipeline
    except Exception as e:
        log.warning(f"Headroom unavailable, passthrough: {e}")
        return messages, HeadroomMetrics(
            images_kept=images_kept, images_pruned=images_pruned
        )

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
                images_kept=images_kept,
                images_pruned=images_pruned,
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
            images_kept=images_kept,
            images_pruned=images_pruned,
        )
        if saved:
            log.info(
                f"Headroom | before={before} after={after} saved={saved} "
                f"({ratio:.0%}) frozen={frozen_message_count}"
            )
        return out, metrics
    except Exception as e:
        log.warning(f"Headroom compress failed, passthrough: {e}", exc_info=True)
        return messages, HeadroomMetrics(
            images_kept=images_kept, images_pruned=images_pruned
        )


async def compress_for_api(
    messages: list[dict],
    *,
    model: str,
    frozen_message_count: int = 0,
    model_limit: int = 200_000,
    keep_last_screenshots: int = KEEP_LAST_SCREENSHOTS,
) -> tuple[list[dict], HeadroomMetrics]:
    """Prune old screenshots, then compress an API-bound copy off the event loop."""
    pruned, prune_stats = prepare_messages_for_api(
        messages, keep_last_screenshots=keep_last_screenshots
    )
    return await asyncio.to_thread(
        _compress_sync,
        pruned,
        model,
        frozen_message_count,
        model_limit,
        prune_stats.images_kept,
        prune_stats.images_pruned,
    )

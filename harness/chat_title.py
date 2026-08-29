"""LLM-generated short titles for conversation runs."""

from __future__ import annotations

import logging
import re

log = logging.getLogger("galadriel.chat_title")

_TITLE_MAX = 72
# ~5–10 tokens of title text; small headroom, thinking disabled separately.
_MAX_TOKENS = 24
_SYSTEM = (
    "Write a short chat title (3–6 words, about 5–10 tokens) for the user's message. "
    "Return only the title — no quotes, no trailing punctuation, no explanation."
)


def _seed_text(raw: str) -> str:
    text = (raw or "").strip()
    for prefix in ("[User instruction]\n",):
        if prefix in text:
            text = text.split(prefix, 1)[-1].strip()
    return re.sub(r"\s+", " ", text).strip()


def normalize_title(raw: str | None) -> str | None:
    """Clean model output into a list-safe title."""
    text = (raw or "").strip()
    if not text:
        return None
    # Take first line only; models sometimes add a note after.
    text = text.splitlines()[0].strip()
    text = text.strip("\"'`“”‘’").strip()
    text = re.sub(r"\s+", " ", text)
    text = text.rstrip(" .:;—-")
    if not text:
        return None
    if len(text) > _TITLE_MAX:
        return text[: _TITLE_MAX - 1] + "…"
    return text


async def generate_chat_title(user_text: str) -> str | None:
    """Ask a cheap model for a short title. Returns None on failure/empty."""
    seed = _seed_text(user_text)
    if not seed:
        return None
    try:
        from . import model_registry

        provider = model_registry.get_provider("chat_title")
        model = model_registry.model_for("chat_title")
        response = await provider.create_message(
            model=model,
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM,
            messages=[{"role": "user", "content": seed[:500]}],
            thinking=False,
        )
        parts = [
            getattr(block, "text", "") or ""
            for block in (getattr(response, "content", None) or [])
            if getattr(block, "type", None) == "text"
            or getattr(block, "text", None)
        ]
        return normalize_title(" ".join(p for p in parts if p))
    except Exception as exc:
        log.warning("Chat title generation failed: %s", exc)
        return None

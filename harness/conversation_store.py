"""Persist per-channel conversation buffers so restarts recover in-context history.

Buffers live under ``state/conversation_buffers/{channel_id}.json``. Ephemeral
system channels (worker ticks, scheduler routines) are skipped — their durable
state lives on the board / DB / palace, not in the transcript buffer.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("galadriel.conversations")

BUFFERS_SUBDIR = "state/conversation_buffers"

# Channels that must not be persisted — worker resets each tick; scheduler
# channels are synthetic one-shot prompts, not user conversation history.
SKIP_CHANNELS = frozenset({
    "worker",
    "wake",
    "heartbeat",
    "morning",
    "reflection",
    "goodnight",
    "catchup",
})


def _buffers_dir(working_dir: str | Path) -> Path:
    return Path(working_dir) / BUFFERS_SUBDIR


def _buffer_path(working_dir: str | Path, channel_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(channel_id))
    return _buffers_dir(working_dir) / f"{safe}.json"


def should_persist(channel_id: str) -> bool:
    return channel_id not in SKIP_CHANNELS


def load_all(working_dir: str | Path) -> dict[str, list]:
    """Load every saved channel buffer. Returns {channel_id: messages}."""
    root = _buffers_dir(working_dir)
    if not root.is_dir():
        return {}
    loaded: dict[str, list] = {}
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"Conversation buffer read failed ({path}): {e}")
            continue
        channel_id = data.get("channel_id") or path.stem
        messages = data.get("messages")
        if not isinstance(messages, list) or not messages:
            continue
        loaded[channel_id] = messages
    return loaded


def save_channel(working_dir: str | Path, channel_id: str, messages: list) -> None:
    """Write one channel's buffer to disk (no-op for skipped channels)."""
    if not should_persist(channel_id) or not messages:
        return
    path = _buffer_path(working_dir, channel_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "channel_id": channel_id,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "message_count": len(messages),
            "messages": messages,
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:
        log.warning(f"Conversation buffer save failed (channel={channel_id}): {e}")


def save_all(working_dir: str | Path, conversations: dict[str, list]) -> int:
    """Persist every non-skipped channel. Returns count written."""
    written = 0
    for channel_id, messages in conversations.items():
        if should_persist(channel_id) and messages:
            save_channel(working_dir, channel_id, messages)
            written += 1
    return written


def delete_channel(working_dir: str | Path, channel_id: str) -> None:
    path = _buffer_path(working_dir, channel_id)
    try:
        path.unlink(missing_ok=True)
    except Exception as e:
        log.warning(f"Conversation buffer delete failed (channel={channel_id}): {e}")

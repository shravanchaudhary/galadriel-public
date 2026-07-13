"""Tower UI settings persisted in MongoDB.

Infra config (not a workflow entity) — same pattern as harness/cost_tracker.py:
one sync pymongo client for reads/writes from Flask routes and agent startup.
"""

import os
from datetime import datetime, timezone

from pymongo import MongoClient

COLLECTION = "tower_settings"
AGENT_MODEL_DOC_ID = "agent_model"  # legacy — migrated to main_model on read
MAIN_MODEL_DOC_ID = "main_model"
WORKER_MODEL_DOC_ID = "worker_model"
HEADROOM_DOC_ID = "headroom"

# Channels with a user-selectable model in Tower.
CONFIGURABLE_CHANNELS: tuple[str, ...] = ("main", "worker")
_CHANNEL_DOC_IDS = {
    "main": MAIN_MODEL_DOC_ID,
    "worker": WORKER_MODEL_DOC_ID,
}

# Selectable agent models in Tower. Provider is resolved from the model name
# via model_registry.provider_for_model (gemini-* → Gemini, *:tag → Ollama).
AGENT_MODEL_OPTIONS: tuple[str, ...] = (
    "gemini-3.1-pro-preview",
    "gemini-3.5-flash",
    "qwen3-vl:8b",
)

_sync_db = None


def _db():
    global _sync_db
    if _sync_db is not None:
        return _sync_db
    uri = os.environ.get("MONGO_URI")
    name = os.environ.get("MONGO_DB")
    if not uri or not name:
        return None
    _sync_db = MongoClient(uri)[name]
    return _sync_db


def is_configured() -> bool:
    return _db() is not None


def _valid_model(model: str | None) -> str | None:
    if model in AGENT_MODEL_OPTIONS:
        return model
    return None


def get_channel_model(channel: str) -> str | None:
    """Return the persisted model for a channel, or None if unset / Mongo unavailable."""
    if channel not in _CHANNEL_DOC_IDS:
        return None
    db = _db()
    if db is None:
        return None
    doc = db[COLLECTION].find_one({"_id": _CHANNEL_DOC_IDS[channel]})
    model = _valid_model((doc or {}).get("model"))
    if model:
        return model
    if channel == "main":
        legacy = db[COLLECTION].find_one({"_id": AGENT_MODEL_DOC_ID})
        return _valid_model((legacy or {}).get("model"))
    return None


def set_channel_model(channel: str, model: str) -> None:
    """Persist a channel's model choice. Raises if Mongo is unavailable."""
    if channel not in _CHANNEL_DOC_IDS:
        raise ValueError(f"Unsupported channel: {channel}")
    if model not in AGENT_MODEL_OPTIONS:
        raise ValueError(f"Unsupported model: {model}")
    db = _db()
    if db is None:
        raise RuntimeError("MONGO_URI / MONGO_DB not configured")
    db[COLLECTION].replace_one(
        {"_id": _CHANNEL_DOC_IDS[channel]},
        {
            "_id": _CHANNEL_DOC_IDS[channel],
            "model": model,
            "updated_at": datetime.now(timezone.utc),
        },
        upsert=True,
    )


def get_agent_model() -> str | None:
    """Return the persisted main-channel model (legacy alias)."""
    return get_channel_model("main")


def set_agent_model(model: str) -> None:
    """Persist the main-channel model (legacy alias)."""
    set_channel_model("main", model)


def get_headroom_enabled() -> bool:
    """Return whether in-agent Headroom compression is enabled (default False)."""
    db = _db()
    if db is None:
        return False
    doc = db[COLLECTION].find_one({"_id": HEADROOM_DOC_ID})
    return bool((doc or {}).get("enabled", False))


def set_headroom_enabled(enabled: bool) -> None:
    """Persist the Headroom ON/OFF toggle. Raises if Mongo is unavailable."""
    db = _db()
    if db is None:
        raise RuntimeError("MONGO_URI / MONGO_DB not configured")
    db[COLLECTION].replace_one(
        {"_id": HEADROOM_DOC_ID},
        {
            "_id": HEADROOM_DOC_ID,
            "enabled": bool(enabled),
            "updated_at": datetime.now(timezone.utc),
        },
        upsert=True,
    )

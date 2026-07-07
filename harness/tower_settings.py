"""Tower UI settings persisted in MongoDB.

Infra config (not a workflow entity) — same pattern as harness/cost_tracker.py:
one sync pymongo client for reads/writes from Flask routes and agent startup.
"""

import os
from datetime import datetime, timezone

from pymongo import MongoClient

COLLECTION = "tower_settings"
AGENT_MODEL_DOC_ID = "agent_model"

# Selectable agent models in Tower. Both are Gemini — provider stays fixed.
AGENT_MODEL_OPTIONS: tuple[str, ...] = (
    "gemini-3.1-pro-preview",
    "gemini-3.5-flash",
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


def get_agent_model() -> str | None:
    """Return the persisted agent model, or None if unset / Mongo unavailable."""
    db = _db()
    if db is None:
        return None
    doc = db[COLLECTION].find_one({"_id": AGENT_MODEL_DOC_ID})
    model = (doc or {}).get("model")
    if model in AGENT_MODEL_OPTIONS:
        return model
    return None


def set_agent_model(model: str) -> None:
    """Persist the agent model choice. Raises if Mongo is unavailable."""
    if model not in AGENT_MODEL_OPTIONS:
        raise ValueError(f"Unsupported model: {model}")
    db = _db()
    if db is None:
        raise RuntimeError("MONGO_URI / MONGO_DB not configured")
    db[COLLECTION].replace_one(
        {"_id": AGENT_MODEL_DOC_ID},
        {
            "_id": AGENT_MODEL_DOC_ID,
            "model": model,
            "updated_at": datetime.now(timezone.utc),
        },
        upsert=True,
    )

"""Tower UI settings persisted in MongoDB.

Infra config (not a workflow entity) — same pattern as harness/cost_tracker.py:
one sync pymongo client for reads/writes from Flask routes and agent startup.
"""

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, available_timezones

from pymongo import MongoClient

COLLECTION = "tower_settings"
AGENT_MODEL_DOC_ID = "agent_model"  # legacy — migrated to main_model on read
MAIN_MODEL_DOC_ID = "main_model"
WORKER_MODEL_DOC_ID = "worker_model"
HEADROOM_DOC_ID = "headroom"
EXPERIENTIAL_STATE_DOC_ID = "experiential_state"
WORKER_IDLE_DOC_ID = "worker_idle_interval"
TIMEZONE_DOC_ID = "agent_timezone"
RECALL_SLM_MODEL_DOC_ID = "recall_slm_model"
RECALL_ENABLED_DOC_ID = "recall_enabled"
RECALL_STAGE2_TIER_DOC_ID = "recall_stage2_tier"
RECALL_JUDGE_MODEL_DOC_ID = "recall_judge_model"
# Matches scheduler defaults until the user sets Agent time in Configuration.
DEFAULT_AGENT_TIMEZONE = "Europe/Stockholm"
_AVAILABLE_TIMEZONES = available_timezones()

# Stage-2 SLM profile keys (must match local_llm.config.RECALL_SLM_MODEL_OPTIONS).
RECALL_SLM_MODEL_OPTIONS: tuple[str, ...] = ("270m", "1b")
DEFAULT_RECALL_SLM_MODEL = "1b"

# Stage-2 verification tier: paid Gemini judge vs free local cross-encoder.
RECALL_STAGE2_TIER_OPTIONS: tuple[str, ...] = ("judge", "local")
DEFAULT_RECALL_STAGE2_TIER = "judge"

# Judge model for the paid tier — any AGENT_MODEL_OPTIONS entry. Measured on the
# 208-case leave-one-out set: flash P=0.952 R=0.952, flash-lite P=0.899 R=0.952
# at the same latency. Flash-lite reads an exclusion, restates it, and fires
# anyway often enough to miss the precision bar, so flash is the default.
DEFAULT_RECALL_JUDGE_MODEL = "gemini-2.5-flash"

# Idle-poll minutes when the worker has nothing to do (default 10).
VALID_WORKER_IDLE_MINUTES: tuple[int, ...] = (5, 10, 15, 20, 30, 60)
DEFAULT_WORKER_IDLE_MINUTES = 10

# Channels with a user-selectable model in Tower (main chat + autonomous loops).
CONFIGURABLE_CHANNELS: tuple[str, ...] = (
    "main",
    "worker",
    "heartbeat",
    "wake",
    "morning",
    "reflection",
    "goodnight",
    "completions",
)
_CHANNEL_DOC_IDS = {
    "main": MAIN_MODEL_DOC_ID,
    "worker": WORKER_MODEL_DOC_ID,
}


def _channel_setting_id(channel: str) -> str:
    """Mongo setting id for a channel's model choice."""
    if channel in _CHANNEL_DOC_IDS:
        return _CHANNEL_DOC_IDS[channel]
    return f"channel_model_{channel}"

# Selectable agent models in Tower. Provider is resolved from the model name
# via model_registry.provider_for_model (gemini-* → Gemini).
AGENT_MODEL_OPTIONS: tuple[str, ...] = (
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-pro-preview",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-pro",
    "gemini-1.5-flash",
)

_sync_db = None


def _tenant_id() -> str:
    return os.environ.get("REPLIKA_TENANT_ID", "default").strip() or "default"


def _doc_id(setting: str) -> str:
    return f"{_tenant_id()}:{setting}"


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
    if channel not in CONFIGURABLE_CHANNELS:
        return None
    db = _db()
    if db is None:
        return None
    setting_id = _channel_setting_id(channel)
    doc = db[COLLECTION].find_one(
        {"_id": _doc_id(setting_id), "tenant_id": _tenant_id()}
    )
    model = _valid_model((doc or {}).get("model"))
    if model:
        return model
    if channel == "main" and _tenant_id() == "default":
        legacy = db[COLLECTION].find_one({"_id": AGENT_MODEL_DOC_ID})
        return _valid_model((legacy or {}).get("model"))
    return None


def set_channel_model(channel: str, model: str) -> None:
    """Persist a channel's model choice. Raises if Mongo is unavailable."""
    if channel not in CONFIGURABLE_CHANNELS:
        raise ValueError(f"Unsupported channel: {channel}")
    if model not in AGENT_MODEL_OPTIONS:
        raise ValueError(f"Unsupported model: {model}")
    db = _db()
    if db is None:
        raise RuntimeError("MONGO_URI / MONGO_DB not configured")
    setting_id = _channel_setting_id(channel)
    db[COLLECTION].replace_one(
        {"_id": _doc_id(setting_id)},
        {
            "_id": _doc_id(setting_id),
            "tenant_id": _tenant_id(),
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
    doc = db[COLLECTION].find_one(
        {"_id": _doc_id(HEADROOM_DOC_ID), "tenant_id": _tenant_id()}
    )
    return bool((doc or {}).get("enabled", False))


def set_headroom_enabled(enabled: bool) -> None:
    """Persist the Headroom ON/OFF toggle. Raises if Mongo is unavailable."""
    db = _db()
    if db is None:
        raise RuntimeError("MONGO_URI / MONGO_DB not configured")
    db[COLLECTION].replace_one(
        {"_id": _doc_id(HEADROOM_DOC_ID)},
        {
            "_id": _doc_id(HEADROOM_DOC_ID),
            "tenant_id": _tenant_id(),
            "enabled": bool(enabled),
            "updated_at": datetime.now(timezone.utc),
        },
        upsert=True,
    )


def get_experiential_enabled() -> bool:
    """Return whether experiential appraisal influences the agent (default True)."""
    db = _db()
    if db is None:
        return True
    doc = db[COLLECTION].find_one(
        {
            "_id": _doc_id(EXPERIENTIAL_STATE_DOC_ID),
            "tenant_id": _tenant_id(),
        }
    )
    if not doc or "enabled" not in doc:
        return True
    return bool(doc["enabled"])


def set_experiential_enabled(enabled: bool) -> None:
    """Persist the default-on experiential-state toggle."""
    db = _db()
    if db is None:
        raise RuntimeError("MONGO_URI / MONGO_DB not configured")
    db[COLLECTION].replace_one(
        {"_id": _doc_id(EXPERIENTIAL_STATE_DOC_ID)},
        {
            "_id": _doc_id(EXPERIENTIAL_STATE_DOC_ID),
            "tenant_id": _tenant_id(),
            "enabled": bool(enabled),
            "updated_at": datetime.now(timezone.utc),
        },
        upsert=True,
    )


def get_recall_enabled() -> bool:
    """Return whether semantic recall scanning is enabled (default True)."""
    db = _db()
    if db is None:
        return True
    doc = db[COLLECTION].find_one(
        {"_id": _doc_id(RECALL_ENABLED_DOC_ID), "tenant_id": _tenant_id()}
    )
    if not doc or "enabled" not in doc:
        return True
    return bool(doc["enabled"])


def set_recall_enabled(enabled: bool) -> None:
    """Persist the default-on semantic-recall toggle."""
    db = _db()
    if db is None:
        raise RuntimeError("MONGO_URI / MONGO_DB not configured")
    db[COLLECTION].replace_one(
        {"_id": _doc_id(RECALL_ENABLED_DOC_ID)},
        {
            "_id": _doc_id(RECALL_ENABLED_DOC_ID),
            "tenant_id": _tenant_id(),
            "enabled": bool(enabled),
            "updated_at": datetime.now(timezone.utc),
        },
        upsert=True,
    )


def get_worker_idle_minutes() -> int:
    """Return the worker idle-poll interval in minutes (default 10)."""
    db = _db()
    if db is None:
        return DEFAULT_WORKER_IDLE_MINUTES
    doc = db[COLLECTION].find_one(
        {"_id": _doc_id(WORKER_IDLE_DOC_ID), "tenant_id": _tenant_id()}
    )
    minutes = (doc or {}).get("minutes")
    if minutes in VALID_WORKER_IDLE_MINUTES:
        return int(minutes)
    return DEFAULT_WORKER_IDLE_MINUTES


def set_worker_idle_minutes(minutes: int) -> None:
    """Persist the worker idle-poll interval. Raises if Mongo is unavailable."""
    if minutes not in VALID_WORKER_IDLE_MINUTES:
        raise ValueError(f"Unsupported idle interval: {minutes}")
    db = _db()
    if db is None:
        raise RuntimeError("MONGO_URI / MONGO_DB not configured")
    db[COLLECTION].replace_one(
        {"_id": _doc_id(WORKER_IDLE_DOC_ID)},
        {
            "_id": _doc_id(WORKER_IDLE_DOC_ID),
            "tenant_id": _tenant_id(),
            "minutes": int(minutes),
            "updated_at": datetime.now(timezone.utc),
        },
        upsert=True,
    )


def _valid_timezone(name: str | None) -> str | None:
    if not name or not isinstance(name, str):
        return None
    tz = name.strip()
    if tz in _AVAILABLE_TIMEZONES:
        return tz
    # ZoneInfo accepts some aliases even when missing from available_timezones().
    try:
        ZoneInfo(tz)
        return tz
    except Exception:
        return None


def get_agent_timezone() -> str:
    """Return the persisted agent display/operating timezone (IANA name)."""
    db = _db()
    if db is None:
        return DEFAULT_AGENT_TIMEZONE
    doc = db[COLLECTION].find_one(
        {"_id": _doc_id(TIMEZONE_DOC_ID), "tenant_id": _tenant_id()}
    )
    return _valid_timezone((doc or {}).get("timezone")) or DEFAULT_AGENT_TIMEZONE


def agent_zoneinfo() -> ZoneInfo:
    """ZoneInfo for the configured agent timezone (falls back to default)."""
    name = get_agent_timezone()
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo(DEFAULT_AGENT_TIMEZONE)


def agent_now() -> datetime:
    """Current wall-clock time in the configured agent timezone."""
    return datetime.now(agent_zoneinfo())


def agent_today() -> str:
    """Today's date (YYYY-MM-DD) in the configured agent timezone."""
    return agent_now().strftime("%Y-%m-%d")


def set_agent_timezone(tz_name: str) -> str:
    """Persist the agent timezone. Returns the normalized IANA name."""
    tz = _valid_timezone(tz_name)
    if tz is None:
        raise ValueError(f"Unsupported timezone: {tz_name}")
    db = _db()
    if db is None:
        raise RuntimeError("MONGO_URI / MONGO_DB not configured")
    db[COLLECTION].replace_one(
        {"_id": _doc_id(TIMEZONE_DOC_ID)},
        {
            "_id": _doc_id(TIMEZONE_DOC_ID),
            "tenant_id": _tenant_id(),
            "timezone": tz,
            "updated_at": datetime.now(timezone.utc),
        },
        upsert=True,
    )
    return tz


def normalize_recall_slm_model(key: str | None) -> str | None:
    """Return a normalized Stage-2 profile key, or None if unsupported."""
    if not key or not isinstance(key, str):
        return None
    k = key.strip().lower()
    if k in RECALL_SLM_MODEL_OPTIONS:
        return k
    return None


def get_recall_slm_model() -> str:
    """Return the persisted Stage-2 SLM profile key (default 1b)."""
    db = _db()
    if db is None:
        return DEFAULT_RECALL_SLM_MODEL
    doc = db[COLLECTION].find_one(
        {"_id": _doc_id(RECALL_SLM_MODEL_DOC_ID), "tenant_id": _tenant_id()}
    )
    return normalize_recall_slm_model((doc or {}).get("model")) or DEFAULT_RECALL_SLM_MODEL


def set_recall_slm_model(key: str) -> str:
    """Persist Stage-2 SLM profile key. Returns the normalized key."""
    model = normalize_recall_slm_model(key)
    if model is None:
        raise ValueError(
            f"Unsupported recall SLM model: {key}; "
            f"expected one of {list(RECALL_SLM_MODEL_OPTIONS)}"
        )
    db = _db()
    if db is None:
        raise RuntimeError("MONGO_URI / MONGO_DB not configured")
    db[COLLECTION].replace_one(
        {"_id": _doc_id(RECALL_SLM_MODEL_DOC_ID)},
        {
            "_id": _doc_id(RECALL_SLM_MODEL_DOC_ID),
            "tenant_id": _tenant_id(),
            "model": model,
            "updated_at": datetime.now(timezone.utc),
        },
        upsert=True,
    )
    return model


def normalize_recall_stage2_tier(key: str | None) -> str | None:
    if not key or not isinstance(key, str):
        return None
    k = key.strip().lower()
    if k in RECALL_STAGE2_TIER_OPTIONS:
        return k
    if k == "rerank":
        return "local"
    return None


def get_recall_stage2_tier() -> str:
    """Return paid `judge` or free `local` Stage-2 tier (default judge)."""
    # Explicit env wins so CI/eval can force embed/rerank without Mongo.
    env = (os.environ.get("RECALL_STAGE2_MODE") or "").strip().lower()
    if env in ("judge",):
        return "judge"
    if env in ("rerank", "local"):
        return "local"
    if env in ("embed", "logit"):
        return "local"
    db = _db()
    if db is None:
        return DEFAULT_RECALL_STAGE2_TIER
    doc = db[COLLECTION].find_one(
        {"_id": _doc_id(RECALL_STAGE2_TIER_DOC_ID), "tenant_id": _tenant_id()}
    )
    return (
        normalize_recall_stage2_tier((doc or {}).get("tier"))
        or DEFAULT_RECALL_STAGE2_TIER
    )


def normalize_recall_judge_model(model: str | None) -> str | None:
    if not model or not isinstance(model, str):
        return None
    m = model.strip()
    return m if m in AGENT_MODEL_OPTIONS else None


def get_recall_judge_model() -> str:
    """Return the judge model for the paid Stage-2 tier. Env wins over Mongo."""
    env = normalize_recall_judge_model(os.environ.get("RECALL_JUDGE_MODEL"))
    if env:
        return env
    db = _db()
    if db is None:
        return DEFAULT_RECALL_JUDGE_MODEL
    doc = db[COLLECTION].find_one(
        {"_id": _doc_id(RECALL_JUDGE_MODEL_DOC_ID), "tenant_id": _tenant_id()}
    )
    return (
        normalize_recall_judge_model((doc or {}).get("model"))
        or DEFAULT_RECALL_JUDGE_MODEL
    )


def set_recall_judge_model(model: str) -> str:
    """Persist the judge model. Returns the normalized model name."""
    name = normalize_recall_judge_model(model)
    if name is None:
        raise ValueError(
            f"Unsupported recall judge model: {model}; "
            f"expected one of {list(AGENT_MODEL_OPTIONS)}"
        )
    db = _db()
    if db is None:
        raise RuntimeError("MONGO_URI / MONGO_DB not configured")
    db[COLLECTION].replace_one(
        {"_id": _doc_id(RECALL_JUDGE_MODEL_DOC_ID)},
        {
            "_id": _doc_id(RECALL_JUDGE_MODEL_DOC_ID),
            "tenant_id": _tenant_id(),
            "model": name,
            "updated_at": datetime.now(timezone.utc),
        },
        upsert=True,
    )
    return name


def set_recall_stage2_tier(key: str) -> str:
    """Persist Stage-2 tier (`judge` | `local`). Returns the normalized key."""
    tier = normalize_recall_stage2_tier(key)
    if tier is None:
        raise ValueError(
            f"Unsupported recall Stage-2 tier: {key}; "
            f"expected one of {list(RECALL_STAGE2_TIER_OPTIONS)}"
        )
    db = _db()
    if db is None:
        raise RuntimeError("MONGO_URI / MONGO_DB not configured")
    db[COLLECTION].replace_one(
        {"_id": _doc_id(RECALL_STAGE2_TIER_DOC_ID)},
        {
            "_id": _doc_id(RECALL_STAGE2_TIER_DOC_ID),
            "tenant_id": _tenant_id(),
            "tier": tier,
            "updated_at": datetime.now(timezone.utc),
        },
        upsert=True,
    )
    return tier

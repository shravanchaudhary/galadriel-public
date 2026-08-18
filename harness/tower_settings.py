"""Tower UI settings persisted in MongoDB.

Infra config (not a workflow entity) — same pattern as harness/cost_tracker.py:
one sync pymongo client for reads/writes from Flask routes and agent startup.
"""

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, available_timezones

from pymongo import MongoClient

from .thinking_effort import (  # noqa: F401 — re-exported for Tower routes
    DEFAULT_EFFORT as DEFAULT_THINKING_EFFORT,
    EFFORT_LABELS,
    EFFORT_OPTIONS,
    clamp_effort_for_model,
    default_effort_for_model,
    effort_catalog_for_model,
    effort_options_for_model,
    normalize_effort as normalize_thinking_effort,
)

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
RECALL_JUDGE_MODEL_DOC_ID = "recall_judge_model"
COMPACT_THRESHOLD_DOC_ID = "compact_threshold"
THINKING_EFFORT_DOC_ID = "thinking_effort"
MODEL_RUNTIME_DOC_ID = "model_runtime"
# Matches scheduler defaults until the user sets Agent time in Configuration.
DEFAULT_AGENT_TIMEZONE = "Europe/Stockholm"
_AVAILABLE_TIMEZONES = available_timezones()

# Stage-2 SLM profile keys (must match local_llm.config.RECALL_SLM_MODEL_OPTIONS).
RECALL_SLM_MODEL_OPTIONS: tuple[str, ...] = ("270m", "1b")
DEFAULT_RECALL_SLM_MODEL = "1b"

# Judge model for the paid tier — any AGENT_MODEL_OPTIONS entry. Measured on the
# 208-case leave-one-out set 2026-08-18: flash and flash-lite both P=0.916
# R=0.952 F1=0.933, identical case-level verdicts, ~1.3 s/scan. Flash-lite is
# ~3.8x cheaper on the judge payload, so it is the default.
DEFAULT_RECALL_JUDGE_MODEL = "gemini-2.5-flash-lite"

# Idle-poll minutes when the worker has nothing to do (default 10).
VALID_WORKER_IDLE_MINUTES: tuple[int, ...] = (5, 10, 15, 20, 30, 60)
DEFAULT_WORKER_IDLE_MINUTES = 10

# Compaction trigger (input tokens). Gemini's window is ~1M; 300K is the
# historical default so long chats fold before they get expensive.
CONTEXT_OPTIONS: tuple[int, ...] = (300_000, 1_000_000)
DEFAULT_COMPACT_THRESHOLD = 300_000
CONTEXT_LABELS: dict[int, str] = {300_000: "300K", 1_000_000: "1M"}

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
# Kept in sync with https://ai.google.dev/gemini-api/docs/models — only
# current (non-shut-down) Gemini text/agentic models are listed here. Gemini
# 2.0 and 1.5 have been shut down upstream / delisted; do not add them back.
AGENT_MODEL_OPTIONS: tuple[str, ...] = (
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.1-pro-preview",
    "gemini-3-flash-preview",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
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


def normalize_compact_threshold(value) -> int | None:
    try:
        tokens = int(value)
    except (TypeError, ValueError):
        return None
    return tokens if tokens in CONTEXT_OPTIONS else None


def get_compact_threshold() -> int | None:
    """Persisted compaction threshold, or None if unset / Mongo unavailable."""
    db = _db()
    if db is None:
        return None
    doc = db[COLLECTION].find_one(
        {"_id": _doc_id(COMPACT_THRESHOLD_DOC_ID), "tenant_id": _tenant_id()}
    )
    return normalize_compact_threshold((doc or {}).get("tokens"))


def resolve_compact_threshold() -> int:
    """Mongo, then AGENT_COMPACT_THRESHOLD, then 300K."""
    saved = get_compact_threshold()
    if saved is not None:
        return saved
    env = (os.environ.get("AGENT_COMPACT_THRESHOLD") or "").strip()
    if env.isdigit() and int(env) > 0:
        return int(env)
    return DEFAULT_COMPACT_THRESHOLD


def set_compact_threshold(tokens: int) -> int:
    """Persist 300K or 1M. Returns the stored value."""
    value = normalize_compact_threshold(tokens)
    if value is None:
        raise ValueError(
            f"Unsupported context: {tokens}; expected one of {list(CONTEXT_OPTIONS)}"
        )
    db = _db()
    if db is None:
        raise RuntimeError("MONGO_URI / MONGO_DB not configured")
    db[COLLECTION].replace_one(
        {"_id": _doc_id(COMPACT_THRESHOLD_DOC_ID)},
        {
            "_id": _doc_id(COMPACT_THRESHOLD_DOC_ID),
            "tenant_id": _tenant_id(),
            "tokens": value,
            "updated_at": datetime.now(timezone.utc),
        },
        upsert=True,
    )
    return value


def get_thinking_effort() -> str:
    """Persisted Gemini thinking effort (default high)."""
    db = _db()
    if db is None:
        return DEFAULT_THINKING_EFFORT
    doc = db[COLLECTION].find_one(
        {"_id": _doc_id(THINKING_EFFORT_DOC_ID), "tenant_id": _tenant_id()}
    )
    return (
        normalize_thinking_effort((doc or {}).get("effort"))
        or DEFAULT_THINKING_EFFORT
    )


def set_thinking_effort(effort: str) -> str:
    """Persist a Gemini thinking effort. Returns the normalized key."""
    value = normalize_thinking_effort(effort)
    if value is None:
        raise ValueError(
            f"Unsupported effort: {effort}; expected one of {list(EFFORT_OPTIONS)}"
        )
    db = _db()
    if db is None:
        raise RuntimeError("MONGO_URI / MONGO_DB not configured")
    db[COLLECTION].replace_one(
        {"_id": _doc_id(THINKING_EFFORT_DOC_ID)},
        {
            "_id": _doc_id(THINKING_EFFORT_DOC_ID),
            "tenant_id": _tenant_id(),
            "effort": value,
            "updated_at": datetime.now(timezone.utc),
        },
        upsert=True,
    )
    return value


def _runtime_entry(model: str, cfg) -> dict:
    if not isinstance(cfg, dict):
        return {}
    entry: dict = {}
    context = normalize_compact_threshold(cfg.get("context"))
    if context is not None:
        entry["context"] = context
    effort = normalize_thinking_effort(cfg.get("effort"))
    if effort is not None:
        entry["effort"] = clamp_effort_for_model(model, effort)
    return entry


def get_model_runtime_map() -> dict[str, dict]:
    """Per-model context/effort map. Empty if unset or Mongo is unavailable."""
    db = _db()
    if db is None:
        return {}
    doc = db[COLLECTION].find_one(
        {"_id": _doc_id(MODEL_RUNTIME_DOC_ID), "tenant_id": _tenant_id()}
    )
    raw = (doc or {}).get("configs") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for model, cfg in raw.items():
        if model not in AGENT_MODEL_OPTIONS:
            continue
        entry = _runtime_entry(model, cfg)
        if entry:
            out[model] = entry
    return out


def resolve_model_runtime(model: str, saved_map: dict[str, dict] | None = None) -> dict:
    """Last context/effort for `model`, or that model's defaults.

    An empty map falls back to the legacy global compact threshold so an
    existing 1M choice is not reset on first deploy. After any per-model
    row exists, unseen models start at 300K + the model's default effort.
    """
    configs = get_model_runtime_map() if saved_map is None else saved_map
    saved = configs.get(model) or {}
    if saved.get("context") is not None:
        context = saved["context"]
    elif not configs:
        context = resolve_compact_threshold()
    else:
        context = DEFAULT_COMPACT_THRESHOLD
    if saved.get("effort") is not None:
        effort = clamp_effort_for_model(model, saved["effort"])
    else:
        effort = default_effort_for_model(model)
    return {"context": int(context), "effort": effort}


def set_model_runtime(
    model: str, *, context: int | None = None, effort: str | None = None
) -> dict:
    """Merge and persist one model's context/effort. Returns the resolved pair."""
    if model not in AGENT_MODEL_OPTIONS:
        raise ValueError(f"Unsupported model: {model}")
    entry: dict = {}
    if context is not None:
        tokens = normalize_compact_threshold(context)
        if tokens is None:
            raise ValueError(
                f"Unsupported context: {context}; expected one of {list(CONTEXT_OPTIONS)}"
            )
        entry["context"] = tokens
    if effort is not None:
        value = normalize_thinking_effort(effort)
        allowed = effort_options_for_model(model)
        if value is None or (allowed and value not in allowed):
            raise ValueError(
                f"Unsupported effort: {effort}; expected one of {list(allowed or EFFORT_OPTIONS)}"
            )
        entry["effort"] = value
    if not entry:
        return resolve_model_runtime(model)
    db = _db()
    if db is None:
        raise RuntimeError("MONGO_URI / MONGO_DB not configured")
    merged = {**(get_model_runtime_map().get(model) or {}), **entry}
    db[COLLECTION].update_one(
        {"_id": _doc_id(MODEL_RUNTIME_DOC_ID)},
        {
            "$set": {
                f"configs.{model}": merged,
                "tenant_id": _tenant_id(),
                "updated_at": datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )
    return resolve_model_runtime(model)

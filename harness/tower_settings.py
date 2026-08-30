"""Tower UI settings persisted in MongoDB.

Infra config (not a workflow entity) — same pattern as harness/cost_tracker.py:
one sync pymongo client for reads/writes from Flask routes and agent startup.
"""

import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, available_timezones

from pymongo import MongoClient

from . import model_catalog
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
RECALL_ENABLED_DOC_ID = "recall_enabled"
LEARNING_ENABLED_DOC_ID = "learning_enabled"
RECALL_JUDGE_MODEL_DOC_ID = "recall_judge_model"
COMPACT_THRESHOLD_DOC_ID = "compact_threshold"
THINKING_EFFORT_DOC_ID = "thinking_effort"
MODEL_RUNTIME_DOC_ID = "model_runtime"
# Matches scheduler defaults until the user sets Agent time in Configuration.
DEFAULT_AGENT_TIMEZONE = "Europe/Stockholm"
_AVAILABLE_TIMEZONES = available_timezones()

# Judge model for the paid tier — any JUDGE_MODEL_OPTIONS entry (the judge never
# calls a tool, so no-tool models stay eligible). Defaults to the fast tier,
# which currently targets gpt-oss-20b: cheapest model that scored 100% on the
# accuracy-vs-cost judge eval (knowledge/reference/bedrock_providers.md):
# ~$0.05/1k calls at p90 1.1s. Superseded 2026-08-18 measurement on the earlier
# Gemini-only judge, kept for reference: gemini-2.5-flash and -flash-lite both
# P=0.916 R=0.952 F1=0.933, identical case-level verdicts, ~1.3 s/scan.
DEFAULT_RECALL_JUDGE_MODEL = "replika-fast"

# Idle-poll minutes when the worker has nothing to do (default 10).
VALID_WORKER_IDLE_MINUTES: tuple[int, ...] = (5, 10, 15, 20, 30, 60)
DEFAULT_WORKER_IDLE_MINUTES = 10

# Compaction trigger (input tokens). Gemini's window is ~1M; 300K is the
# historical default so long chats fold before they get expensive.
#
# Neither option fits every model: every Mantle model tops out at 262,144 and
# the Claude 4.5 family at 200,000, both below the 300K "small" option. Using
# 300K there would mean compaction never fires — the model's own context
# limit is hit first and the turn 400s instead of summarizing. Callers that
# know the model must go through `context_options_for_model` /
# `default_compact_threshold_for_model` below rather than these raw
# constants, which stay as the two canonical choices for models that fit them
# (currently the 1M-context Gemini and Claude 4.6 tiers).
CONTEXT_OPTIONS: tuple[int, ...] = (300_000, 1_000_000)
DEFAULT_COMPACT_THRESHOLD = 300_000
CONTEXT_LABELS: dict[int, str] = {300_000: "300K", 1_000_000: "1M"}


def _model_context_ceiling(model: str | None) -> int | None:
    """Real context window for `model`, or None if it is not in the catalog."""
    entry = model_catalog.get(model) if model else None
    return entry.context if entry is not None else None


def context_options_for_model(model: str | None) -> tuple[int, ...]:
    """CONTEXT_OPTIONS filtered to what `model` can actually hold.

    Falls back to the model's own ceiling as the sole option when even 300K
    would exceed it — every Mantle model and the Claude 4.5 family land here.
    Unknown models (Ollama tags) keep the unfiltered pair.
    """
    ceiling = _model_context_ceiling(model)
    if ceiling is None:
        return CONTEXT_OPTIONS
    fitting = tuple(t for t in CONTEXT_OPTIONS if t <= ceiling)
    return fitting or (ceiling,)


def default_compact_threshold_for_model(model: str | None) -> int:
    """DEFAULT_COMPACT_THRESHOLD, clamped down to the model's own context
    window when that window is smaller."""
    ceiling = _model_context_ceiling(model)
    return DEFAULT_COMPACT_THRESHOLD if ceiling is None else min(DEFAULT_COMPACT_THRESHOLD, ceiling)

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


def _channel_effort_id(channel: str) -> str:
    """Mongo setting id for a channel's reasoning-effort choice."""
    return f"channel_effort_{channel}"

# Selectable models in Tower, both derived from `model_catalog` so a model is
# added in exactly one place. Provider is resolved from the model name via
# model_registry.provider_for_model.
#
# The two lists differ deliberately: an agent model must be able to call tools
# (Gemma 3 27B accepts a tools array and then answers in prose, which would look
# like a working agent that never acts), while the recall judge only emits a JSON
# verdict and so can use the cheaper no-tool models.
AGENT_MODEL_OPTIONS: tuple[str, ...] = model_catalog.agent_options()
JUDGE_MODEL_OPTIONS: tuple[str, ...] = model_catalog.judge_options()

_sync_db = None
_timezone_cache: tuple[float, str] | None = None
_TIMEZONE_TTL_SEC = 30.0


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


def get_channel_effort(channel: str) -> str | None:
    """Persisted reasoning effort for a channel, or None if unset.

    Stored raw and clamped at use (`clamp_effort_for_model`) so a later model
    change on the channel cannot leave an illegal pair persisted. The main
    channel keeps the composer's per-model memory (`model_runtime`) instead —
    this is only for the autonomous channels on the Agent page.
    """
    if channel not in CONFIGURABLE_CHANNELS:
        return None
    db = _db()
    if db is None:
        return None
    doc = db[COLLECTION].find_one(
        {"_id": _doc_id(_channel_effort_id(channel)), "tenant_id": _tenant_id()}
    )
    return normalize_thinking_effort((doc or {}).get("effort"))


def set_channel_effort(channel: str, effort: str) -> str:
    """Persist a channel's reasoning effort. Raises if Mongo is unavailable."""
    if channel not in CONFIGURABLE_CHANNELS:
        raise ValueError(f"Unsupported channel: {channel}")
    value = normalize_thinking_effort(effort)
    if value is None:
        raise ValueError(f"Unsupported effort: {effort}")
    db = _db()
    if db is None:
        raise RuntimeError("MONGO_URI / MONGO_DB not configured")
    setting_id = _channel_effort_id(channel)
    db[COLLECTION].replace_one(
        {"_id": _doc_id(setting_id)},
        {
            "_id": _doc_id(setting_id),
            "tenant_id": _tenant_id(),
            "effort": value,
            "updated_at": datetime.now(timezone.utc),
        },
        upsert=True,
    )
    return value


def get_agent_model() -> str | None:
    """Return the persisted main-channel model (legacy alias)."""
    return get_channel_model("main")


def set_agent_model(model: str) -> None:
    """Persist the main-channel model (legacy alias)."""
    set_channel_model("main", model)


def get_headroom_enabled() -> bool:
    """Return whether in-agent Headroom compression is enabled (default True)."""
    db = _db()
    if db is None:
        return True
    doc = db[COLLECTION].find_one(
        {"_id": _doc_id(HEADROOM_DOC_ID), "tenant_id": _tenant_id()}
    )
    return bool((doc or {}).get("enabled", True))


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


def get_learning_enabled() -> bool:
    """Return whether memory consolidation is enabled (default True).

    Separate from the recall toggle on purpose: recall is how a memory comes
    back, learning is whether one is written at all. Turning off retrieval to
    measure its value used to silently stop the agent learning too, which made
    the two impossible to evaluate independently.
    """
    db = _db()
    if db is None:
        return True
    doc = db[COLLECTION].find_one(
        {"_id": _doc_id(LEARNING_ENABLED_DOC_ID), "tenant_id": _tenant_id()}
    )
    if not doc or "enabled" not in doc:
        # Never set, so inherit the recall toggle this used to be gated on and
        # persist that answer once. Defaulting to True would silently switch
        # the paid consolidation pass back on for every tenant that had turned
        # recall off; re-deriving it on every boot instead would make learning
        # depend on when the process last restarted. Writing it here settles
        # the migration on first read, after which the two are independent.
        inherited = get_recall_enabled()
        try:
            set_learning_enabled(inherited)
        except Exception:
            pass
        return inherited
    return bool(doc["enabled"])


def set_learning_enabled(enabled: bool) -> None:
    """Persist the default-on memory-consolidation toggle."""
    db = _db()
    if db is None:
        raise RuntimeError("MONGO_URI / MONGO_DB not configured")
    db[COLLECTION].replace_one(
        {"_id": _doc_id(LEARNING_ENABLED_DOC_ID)},
        {
            "_id": _doc_id(LEARNING_ENABLED_DOC_ID),
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
    """Return the persisted agent display/operating timezone (IANA name).

    Cached: list renderers call this per row (bucket + time label), so an
    uncached read costs one Mongo round-trip per cell.
    """
    global _timezone_cache
    if _timezone_cache is not None:
        cached_at, cached_tz = _timezone_cache
        if time.monotonic() - cached_at < _TIMEZONE_TTL_SEC:
            return cached_tz
    db = _db()
    if db is None:
        return DEFAULT_AGENT_TIMEZONE
    doc = db[COLLECTION].find_one(
        {"_id": _doc_id(TIMEZONE_DOC_ID), "tenant_id": _tenant_id()},
        {"timezone": 1, "_id": 0},
    )
    tz = _valid_timezone((doc or {}).get("timezone")) or DEFAULT_AGENT_TIMEZONE
    _timezone_cache = (time.monotonic(), tz)
    return tz


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
    global _timezone_cache
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
    _timezone_cache = (time.monotonic(), tz)
    return tz


def normalize_recall_judge_model(model: str | None) -> str | None:
    if not model or not isinstance(model, str):
        return None
    m = model.strip()
    return m if m in JUDGE_MODEL_OPTIONS else None


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
            f"expected one of {list(JUDGE_MODEL_OPTIONS)}"
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


def normalize_compact_threshold(value, model: str | None = None) -> int | None:
    """`value` if it is a valid compaction-trigger choice, else None.

    Pass `model` to validate against that model's own fitting options
    (`context_options_for_model`); without it, validates against the flat
    300K/1M pair only — used for the legacy tenant-wide preference below,
    which predates per-model settings and was never anything else.
    """
    try:
        tokens = int(value)
    except (TypeError, ValueError):
        return None
    valid = context_options_for_model(model) if model else CONTEXT_OPTIONS
    return tokens if tokens in valid else None


def get_compact_threshold() -> int | None:
    """Persisted (legacy, tenant-wide) compaction threshold, or None if unset
    / Mongo unavailable."""
    db = _db()
    if db is None:
        return None
    doc = db[COLLECTION].find_one(
        {"_id": _doc_id(COMPACT_THRESHOLD_DOC_ID), "tenant_id": _tenant_id()}
    )
    return normalize_compact_threshold((doc or {}).get("tokens"))


def resolve_compact_threshold(model: str | None = None) -> int:
    """Legacy tenant-wide preference (Mongo, then AGENT_COMPACT_THRESHOLD),
    honored only for a `model` that can actually hold it — otherwise clamped
    to that model's own default. Used solely to bootstrap
    `resolve_model_runtime` before any per-model setting exists.
    """
    saved = get_compact_threshold()
    if saved is None:
        env = (os.environ.get("AGENT_COMPACT_THRESHOLD") or "").strip()
        if env.isdigit() and int(env) > 0:
            saved = int(env)
    if saved is None:
        return default_compact_threshold_for_model(model)
    ceiling = _model_context_ceiling(model)
    if ceiling is not None and saved > ceiling:
        return default_compact_threshold_for_model(model)
    return saved


def set_compact_threshold(tokens: int) -> int:
    """Persist the legacy tenant-wide 300K/1M preference. Returns the stored
    value. Per-model choices go through `set_model_runtime` instead."""
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
    context = normalize_compact_threshold(cfg.get("context"), model)
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

    An empty map falls back to the legacy global compact threshold (clamped
    to what `model` can hold) so an existing 1M choice is not reset on first
    deploy. After any per-model row exists, unseen models start at
    `default_compact_threshold_for_model(model)` — 300K, or that model's own
    context window when it is smaller — plus the model's default effort.
    """
    configs = get_model_runtime_map() if saved_map is None else saved_map
    saved = configs.get(model) or {}
    if saved.get("context") is not None:
        context = saved["context"]
    elif not configs:
        context = resolve_compact_threshold(model)
    else:
        context = default_compact_threshold_for_model(model)
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
        tokens = normalize_compact_threshold(context, model)
        if tokens is None:
            raise ValueError(
                f"Unsupported context for {model}: {context}; "
                f"expected one of {list(context_options_for_model(model))}"
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

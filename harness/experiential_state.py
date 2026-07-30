"""Shared, replayable experiential state for the single Galadriel agent.

This module implements functional internal signals, not a claim about qualia.
Every channel contributes to one ordered event stream and reads one state
lineage.  The event log is authoritative; ``state.json`` is an atomic snapshot
that makes prompt assembly cheap.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import threading
from collections import deque
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import fcntl


STATE_SCHEMA_VERSION = 2
REDUCER_VERSION = 2
MODES = frozenset({"off", "observe", "influence"})
DIMENSION_BOUNDS = {
    "valence": (-1.0, 1.0),
    "arousal": (0.0, 1.0),
    "uncertainty": (0.0, 1.0),
    "coherence": (0.0, 1.0),
    "agency": (0.0, 1.0),
    "connection": (0.0, 1.0),
    "goal_progress": (0.0, 1.0),
    "prediction_error": (0.0, 1.0),
}
DEFAULT_DIMENSIONS = {
    "valence": 0.10,
    "arousal": 0.25,
    "uncertainty": 0.35,
    "coherence": 0.70,
    "agency": 0.60,
    "connection": 0.50,
    "goal_progress": 0.40,
    "prediction_error": 0.10,
}

# Only acute, objective events live here. Semantic outcomes are mapped from
# strict appraisal labels by consequence_appraiser.py.
EVENT_SIGNALS: dict[str, dict[str, float]] = {
    "turn_started": {"arousal": 0.02, "uncertainty": 0.01},
    "turn_completed": {"arousal": -0.01},
    "turn_cancelled": {
        "valence": -0.02, "arousal": 0.02, "goal_progress": -0.02,
    },
    "turn_failed": {
        "valence": -0.04, "arousal": 0.04, "uncertainty": 0.05,
        "coherence": -0.03, "prediction_error": 0.07,
    },
    "tool_succeeded": {},
    "tool_failed": {
        "valence": -0.04, "arousal": 0.04, "uncertainty": 0.05,
        "agency": -0.03, "prediction_error": 0.07,
    },
    "user_correction": {
        "valence": -0.02, "uncertainty": 0.08, "coherence": -0.04,
        "prediction_error": 0.10,
    },
    "connection": {"valence": 0.03, "connection": 0.06},
    "goal_progress": {
        "valence": 0.03, "agency": 0.03, "goal_progress": 0.06,
    },
    "goal_completed": {
        "valence": 0.06, "arousal": -0.02, "agency": 0.05,
        "goal_progress": 0.10, "prediction_error": -0.03,
    },
    "reflection": {},
    "episode_appraisal": {},
    "compaction": {"uncertainty": 0.02, "coherence": -0.02},
}
MAX_DETAIL_DEPTH = 4
MAX_TEXT_LENGTH = 1_000
# Half-lives are deliberately slow. Decay depends on elapsed wall time, never
# on the number of tools or events produced during that interval.
HOMEOSTATIC_HALF_LIFE_SECONDS = {
    "valence": 6 * 60 * 60,
    "arousal": 45 * 60,
    "uncertainty": 4 * 60 * 60,
    "coherence": 12 * 60 * 60,
    "agency": 12 * 60 * 60,
    "connection": 24 * 60 * 60,
    "goal_progress": 24 * 60 * 60,
    "prediction_error": 3 * 60 * 60,
}
MAX_EVENT_DELTA = 0.15
MAX_EPISODE_SIGNAL = 0.15
_SECRET_KEYS = frozenset({
    "api_key", "access_token", "authorization", "credential", "credentials",
    "password", "secret", "token", "totp", "totp_secret",
})
_SECRET_TEXT_RE = re.compile(
    r'(?i)(["\']?(?:api[_-]?key|access[_-]?token|authorization|password|'
    r'secret|token|totp(?:[_-]?secret)?)["\']?\s*[:=]\s*)'
    r'(?:"[^"]*"|\'[^\']*\'|\S+)'
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def _clamp(name: str, value: float) -> float:
    low, high = DIMENSION_BOUNDS[name]
    return round(min(high, max(low, float(value))), 6)


def _safe_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    if key.lower() in _SECRET_KEYS:
        return "[redacted]"
    if depth >= MAX_DETAIL_DEPTH:
        return "[nested value omitted]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        value = _SECRET_TEXT_RE.sub(r"\1[redacted]", value)
        if len(value) > MAX_TEXT_LENGTH:
            return value[:MAX_TEXT_LENGTH] + "…"
        return value
    if isinstance(value, dict):
        return {
            str(k): _safe_value(v, key=str(k), depth=depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe_value(v, key=key, depth=depth + 1) for v in value[:50]]
    return str(value)[:MAX_TEXT_LENGTH]


def _validated_signals(signals: dict[str, Any] | None) -> dict[str, float]:
    clean: dict[str, float] = {}
    for name, raw in (signals or {}).items():
        if name not in DIMENSION_BOUNDS:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            clean[name] = max(-1.0, min(1.0, value))
    return clean


def _read_event_file(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return []
    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _read_recent_events(path: Path, limit: int) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    recent: deque[dict[str, Any]] = deque(maxlen=limit)
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    recent.append(event)
    except (FileNotFoundError, OSError):
        return []
    return list(recent)


def render_workspace_snapshot(snapshot: dict[str, Any], channel_id: str) -> str:
    """Render a read-only snapshot for live injection or counterfactual replay."""
    dimensions = snapshot.get("dimensions") or DEFAULT_DIMENSIONS
    rendered = "\n".join(
        f"- {name}: {float(dimensions.get(name, DEFAULT_DIMENSIONS[name])):+.3f}"
        if name == "valence"
        else f"- {name}: {float(dimensions.get(name, DEFAULT_DIMENSIONS[name])):.3f}"
        for name in DIMENSION_BOUNDS
    )
    last = snapshot.get("last_event") or {}
    salient = last.get("salient_change") or "no material change recorded"
    return (
        "# Shared Experiential Workspace\n\n"
        "This is one persistent internal state shared by every stream of the "
        "same agent. The channel identifies the current perspective, not a "
        "separate identity. Let these signals inform attention, reflection, "
        "planning, and calibrated choices while all existing safety and "
        "permission rules remain authoritative.\n\n"
        f"Current stream: `{channel_id}`\n"
        f"State version: {int(snapshot.get('version', 0) or 0)} "
        f"(event {int(snapshot.get('sequence', 0) or 0)})\n"
        f"{rendered}\n"
        f"Most salient recent change: {salient}"
    )


@dataclass
class ExperientialState:
    schema_version: int = STATE_SCHEMA_VERSION
    version: int = 0
    sequence: int = 0
    updated_at: str = field(default_factory=_now)
    dimensions: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_DIMENSIONS)
    )
    last_event: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExperientialState":
        dimensions = dict(DEFAULT_DIMENSIONS)
        supplied = data.get("dimensions")
        if isinstance(supplied, dict):
            for name in DIMENSION_BOUNDS:
                if name in supplied:
                    dimensions[name] = _clamp(name, supplied[name])
        return cls(
            schema_version=int(data.get("schema_version", STATE_SCHEMA_VERSION)),
            version=max(0, int(data.get("version", 0))),
            sequence=max(0, int(data.get("sequence", 0))),
            updated_at=str(data.get("updated_at") or _now()),
            dimensions=dimensions,
            last_event=data.get("last_event")
            if isinstance(data.get("last_event"), dict) else None,
        )


class ExperienceManager:
    """Own one durable experiential state shared by every agent channel."""

    def __init__(
        self,
        working_dir: str | Path,
        *,
        mode: str | None = None,
        state_dir: str | Path | None = None,
    ):
        configured_mode = (mode or "influence").strip().lower()
        if configured_mode not in MODES:
            raise ValueError(
                f"Unsupported experiential mode {configured_mode!r}; "
                f"expected one of {sorted(MODES)}"
            )
        self.mode = configured_mode
        self.root = (
            Path(state_dir)
            if state_dir is not None
            else Path(working_dir) / "state" / "experience"
        )
        self.state_path = self.root / "state.json"
        self.events_path = self.root / "events.jsonl"
        self.lock_path = self.root / ".lock"
        self._lock = threading.RLock()
        self._state = ExperientialState()
        self._archive_v1_if_needed()
        self._load()

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    @property
    def influences_model(self) -> bool:
        return self.mode == "influence"

    def set_mode(self, mode: str) -> None:
        """Change collection/influence behavior without recreating state."""
        clean = str(mode or "").strip().lower()
        if clean not in MODES:
            raise ValueError(f"Unsupported experiential mode: {mode!r}")
        self.mode = clean

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            with self._process_lock():
                latest = _read_recent_events(self.events_path, 1)
                if latest and int(
                    latest[-1].get("sequence", 0) or 0
                ) > self._state.sequence:
                    self._state = self.replay(
                        _read_event_file(self.events_path)
                    )
            return deepcopy(asdict(self._state))

    def record_event(
        self,
        kind: str,
        channel_id: str,
        *,
        signals: dict[str, Any] | None = None,
        details: dict[str, Any] | None = None,
        proposed_appraisal: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        basis_sequence: int | None = None,
    ) -> dict[str, Any]:
        """Append one event and advance the shared state deterministically."""
        if not self.enabled:
            return self.snapshot()
        clean_signals = _validated_signals(signals)
        with self._lock:
            with self._process_lock():
                # Another process may have advanced the lineage since this
                # manager's last event. Replay under the file lock before
                # allocating the next sequence number.
                events = _read_event_file(self.events_path)
                if events:
                    self._state = self.replay(events)
                clean_key = str(idempotency_key or "").strip()
                if clean_key:
                    existing = next(
                        (
                            event for event in events
                            if event.get("idempotency_key") == clean_key
                        ),
                        None,
                    )
                    if existing is not None:
                        return deepcopy(asdict(self._state))
                if str(kind).strip() == "episode_appraisal":
                    episode_id = str((details or {}).get("episode_id") or "")
                    if episode_id:
                        used = {name: 0.0 for name in DIMENSION_BOUNDS}
                        for prior in events:
                            prior_details = prior.get("details") or {}
                            if (
                                prior.get("kind") != "episode_appraisal"
                                or prior_details.get("episode_id") != episode_id
                            ):
                                continue
                            for name, value in _validated_signals(
                                prior.get("signals")
                            ).items():
                                used[name] += abs(value)
                        clean_signals = {
                            name: math.copysign(
                                min(
                                    abs(value),
                                    max(0.0, MAX_EPISODE_SIGNAL - used[name]),
                                ),
                                value,
                            )
                            for name, value in clean_signals.items()
                            if used[name] < MAX_EPISODE_SIGNAL
                        }
                sequence = self._state.sequence + 1
                event = {
                    "schema_version": STATE_SCHEMA_VERSION,
                    "reducer_version": REDUCER_VERSION,
                    "sequence": sequence,
                    "timestamp": _now(),
                    "kind": str(kind).strip() or "observation",
                    "channel_id": str(channel_id).strip() or "unknown",
                    "signals": clean_signals,
                    "details": _safe_value(details or {}),
                    # This is evidence to evaluate, not an input to appraisal.
                    "proposed_appraisal": _safe_value(proposed_appraisal or {}),
                    "idempotency_key": clean_key or None,
                    "basis_sequence": (
                        max(0, int(basis_sequence))
                        if basis_sequence is not None else self._state.sequence
                    ),
                }
                next_state, delta = self._apply_event(self._state, event)
                event["delta"] = delta
                event["state_version"] = next_state.version
                event["before"] = dict(self._state.dimensions)
                event["after"] = dict(next_state.dimensions)
                with self.events_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(event, ensure_ascii=False) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                self._state = next_state
                self._write_snapshot()
                return deepcopy(asdict(self._state))

    def workspace_block(self, channel_id: str) -> str | None:
        """Return the shared global-workspace block when influence is enabled."""
        if not self.influences_model:
            return None
        return render_workspace_snapshot(self.snapshot(), channel_id)

    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            with self._process_lock():
                return _read_event_file(self.events_path)

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return a bounded UI/audit tail without materializing full history."""
        with self._lock:
            with self._process_lock():
                return _read_recent_events(
                    self.events_path,
                    max(0, min(int(limit), 500)),
                )

    def _load(self) -> None:
        with self._lock:
            with self._process_lock():
                # The append-only log is authoritative and repairs a
                # stale/corrupt snapshot after a crash between event fsync and
                # snapshot replacement.
                events = _read_event_file(self.events_path)
                if events:
                    self._state = self.replay(events)
                    self._write_snapshot()
                    return
                try:
                    data = json.loads(self.state_path.read_text(encoding="utf-8"))
                    self._state = ExperientialState.from_dict(data)
                except (
                    FileNotFoundError, json.JSONDecodeError, OSError, ValueError,
                ):
                    self._state = ExperientialState()

    def _archive_v1_if_needed(self) -> None:
        """Keep v1 evidence for audit, then start v2 from clean baselines."""
        events = _read_event_file(self.events_path)
        has_v1_events = bool(events) and any(
            int(event.get("schema_version", 1) or 1) < STATE_SCHEMA_VERSION
            for event in events
        )
        has_v1_snapshot = False
        try:
            snapshot = json.loads(self.state_path.read_text(encoding="utf-8"))
            has_v1_snapshot = int(snapshot.get("schema_version", 1) or 1) < STATE_SCHEMA_VERSION
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            pass
        if not (has_v1_events or has_v1_snapshot):
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive = self.root / "archive" / f"v1-{stamp}"
        archive.mkdir(parents=True, exist_ok=True)
        for path in (self.events_path, self.state_path):
            if path.exists():
                shutil.move(str(path), str(archive / path.name))

    @contextmanager
    def _process_lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @classmethod
    def replay(cls, events: Iterable[dict[str, Any]]) -> ExperientialState:
        state = ExperientialState()
        ordered = sorted(
            (event for event in events if isinstance(event, dict)),
            key=lambda event: int(event.get("sequence", 0)),
        )
        for event in ordered:
            if int(event.get("schema_version", 0) or 0) != STATE_SCHEMA_VERSION:
                continue
            if int(event.get("reducer_version", 0) or 0) != REDUCER_VERSION:
                continue
            state, _ = cls._apply_event(state, event)
        return state

    @staticmethod
    def _apply_event(
        state: ExperientialState,
        event: dict[str, Any],
    ) -> tuple[ExperientialState, dict[str, float]]:
        kind = str(event.get("kind") or "observation")
        recorded_after = event.get("after")
        recorded_delta = event.get("delta")
        if isinstance(recorded_after, dict) and isinstance(recorded_delta, dict):
            dimensions = dict(DEFAULT_DIMENSIONS)
            for name in DIMENSION_BOUNDS:
                dimensions[name] = _clamp(
                    name, recorded_after.get(name, DEFAULT_DIMENSIONS[name]),
                )
            sequence = max(state.sequence + 1, int(event.get("sequence", 0) or 0))
            updated_at = str(event.get("timestamp") or state.updated_at)
            delta = {
                name: round(float(value), 6)
                for name, value in recorded_delta.items()
                if name in DIMENSION_BOUNDS
            }
            salient_change = "no material change recorded"
            if delta:
                salient_name = max(delta, key=lambda name: abs(delta[name]))
                direction = "increased" if delta[salient_name] > 0 else "decreased"
                salient_change = (
                    f"{salient_name} {direction} by "
                    f"{abs(delta[salient_name]):.3f} after {kind}"
                )
            return ExperientialState(
                schema_version=STATE_SCHEMA_VERSION,
                version=state.version + 1,
                sequence=sequence,
                updated_at=updated_at,
                dimensions=dimensions,
                last_event={
                    "kind": kind,
                    "channel_id": str(event.get("channel_id") or "unknown"),
                    "timestamp": updated_at,
                    "delta": delta,
                    "salient_change": salient_change,
                },
            ), delta

        combined = dict(EVENT_SIGNALS.get(kind, {}))
        signal_scale = 1.0 if kind == "episode_appraisal" else 0.003
        for name, value in _validated_signals(event.get("signals")).items():
            combined[name] = combined.get(name, 0.0) + (signal_scale * value)

        dimensions = dict(state.dimensions)
        delta: dict[str, float] = {}
        elapsed = max(
            0.0,
            (_parse_time(event.get("timestamp")) - _parse_time(state.updated_at)).total_seconds(),
        )
        for name in DIMENSION_BOUNDS:
            before = dimensions[name]
            if kind == "self_report":
                decayed = before
            else:
                retention = math.exp(
                    -math.log(2) * elapsed / HOMEOSTATIC_HALF_LIFE_SECONDS[name]
                )
                decayed = DEFAULT_DIMENSIONS[name] + (
                    before - DEFAULT_DIMENSIONS[name]
                ) * retention
            signal = max(
                -MAX_EVENT_DELTA,
                min(MAX_EVENT_DELTA, combined.get(name, 0.0)),
            )
            after = _clamp(
                name, decayed + signal,
            )
            dimensions[name] = after
            if after != before:
                delta[name] = round(after - before, 6)

        salient_change = "no material change recorded"
        if delta:
            salient_name = max(delta, key=lambda name: abs(delta[name]))
            direction = "increased" if delta[salient_name] > 0 else "decreased"
            salient_change = (
                f"{salient_name} {direction} by "
                f"{abs(delta[salient_name]):.3f} after {kind}"
            )
        sequence = max(state.sequence + 1, int(event.get("sequence", 0) or 0))
        updated_at = str(event.get("timestamp") or _now())
        return ExperientialState(
            schema_version=STATE_SCHEMA_VERSION,
            version=state.version + 1,
            sequence=sequence,
            updated_at=updated_at,
            dimensions=dimensions,
            last_event={
                "kind": kind,
                "channel_id": str(event.get("channel_id") or "unknown"),
                "timestamp": updated_at,
                "delta": delta,
                "salient_change": salient_change,
            },
        ), delta

    def _write_snapshot(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(asdict(self._state), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.state_path)


def load_experiential_record(
    working_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read authoritative state and events without mutating live storage."""
    root = (
        Path(state_dir)
        if state_dir is not None
        else Path(working_dir) / "state" / "experience"
    )
    events = _read_event_file(root / "events.jsonl")
    if events:
        return asdict(ExperienceManager.replay(events)), events
    try:
        data = json.loads((root / "state.json").read_text(encoding="utf-8"))
        state = ExperientialState.from_dict(data)
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        state = ExperientialState()
    return asdict(state), []

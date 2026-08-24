"""DB primitives — the agent's only sanctioned path to MongoDB.

The agent never writes freestyle pymongo. It operates the operational DB through
this fixed set of primitives, which resolve each entity against its workflow spec
(`harness/workflows.py`) and enforce the two invariants that keep the backend
honest:

  - **State machine.** `move_state` rejects any status transition not declared in
    the spec's `transitions`, using an atomic, precondition-guarded update so a
    concurrent double-move is impossible.
  - **Audit.** Every write appends an entry to the doc's `history[]`.

Caps, ordering, and approval gates are NOT enforced here by design — those stay
as prose in the job cookbooks (the spec only *declares* `approval_states` so the
UI can surface them). This is the deliberately lighter enforcement model.

The connection comes from the shared connector at `scripts/lib/db.py`
(`get_db()`, env: MONGO_URI / MONGO_DB), imported as internal infrastructure.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from .workflows import resolve, WorkflowSpecError

# The connector lives under scripts/lib (shared infra). Put scripts/ on the path
# so `from lib.db import get_db` resolves, then reuse the cached async client.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from lib.db import get_db  # noqa: E402  (path set above)

COUNTERS_COLLECTION = "counters"


def _now():
    return datetime.now(timezone.utc)


def _jsonable(value):
    """Recursively convert Mongo values (datetime, ObjectId, …) to JSON-safe."""
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)  # ObjectId and anything else → string


def _dump(value) -> str:
    return json.dumps(_jsonable(value), indent=2, ensure_ascii=False)


# ── Primitives ────────────────────────────────────────────────────────


async def create(entity: str, doc: dict) -> str:
    """Insert a new entity doc. Forces status=initial, inits history[], dedups on
    the unique key. Returns the created doc, or a notice if the key already exists.
    """
    try:
        spec = resolve(entity)
    except WorkflowSpecError as e:
        return f"[error] {e}"
    if not isinstance(doc, dict):
        return "[error] 'doc' must be an object."
    if spec.key not in doc:
        return f"[error] doc is missing the unique key '{spec.key}'."

    db = get_db()
    coll = db[spec.collection]
    await coll.create_index(spec.key, unique=True)

    now = _now()
    record = {k: v for k, v in doc.items() if k != "status"}
    record["status"] = spec.initial
    record["created_at"] = now
    record["updated_at"] = now
    record["last_action_at"] = now
    record["history"] = [{"ts": now, "action": "created", "to": spec.initial}]
    try:
        await coll.insert_one(record)
    except DuplicateKeyError:
        existing = await coll.find_one({spec.key: doc[spec.key]})
        return (
            f"exists: a {entity} with {spec.key}={doc[spec.key]!r} is already "
            f"present (status={existing.get('status') if existing else '?'}). "
            f"No duplicate created."
        )
    return f"created {entity}:\n{_dump(record)}"


async def get(entity: str, key) -> str:
    """Exact read of one entity by its unique key."""
    try:
        spec = resolve(entity)
    except WorkflowSpecError as e:
        return f"[error] {e}"
    db = get_db()
    doc = await db[spec.collection].find_one({spec.key: key})
    if not doc:
        return f"not found: no {entity} with {spec.key}={key!r}."
    return _dump(doc)


async def query(
    entity: str,
    filter: dict | None = None,
    sort: str | None = None,
    descending: bool = False,
    limit: int = 50,
) -> str:
    """List entities matching an optional filter. Returns up to `limit` docs.

    Omits `history[]` — that array is unbounded. Use `get` for one full doc.
    """
    try:
        spec = resolve(entity)
    except WorkflowSpecError as e:
        return f"[error] {e}"
    db = get_db()
    cursor = db[spec.collection].find(filter or {}, {"history": 0})
    if sort:
        cursor = cursor.sort(sort, -1 if descending else 1)
    cursor = cursor.limit(int(limit))
    docs = [doc async for doc in cursor]
    return f"{len(docs)} {entity}(s):\n{_dump(docs)}"


async def delete(entity: str, key) -> str:
    """Delete one entity doc by its unique key (like Mongo's deleteOne). Irreversible."""
    try:
        spec = resolve(entity)
    except WorkflowSpecError as e:
        return f"[error] {e}"
    db = get_db()
    result = await db[spec.collection].delete_one({spec.key: key})
    if result.deleted_count == 0:
        return f"not found: no {entity} with {spec.key}={key!r}."
    return f"deleted {entity} {key!r}."


async def move_state(entity: str, key, to: str, note: str | None = None) -> str:
    """Enforced state transition. Rejects any move not allowed by the spec, then
    performs it atomically (precondition-guarded) and appends to history[].

    This is also how you request approval (move into an approval_state) and mark
    something done (move into a terminal state).
    """
    try:
        spec = resolve(entity)
    except WorkflowSpecError as e:
        return f"[error] {e}"
    if to not in spec.states:
        return (
            f"[error] '{to}' is not a valid state for {entity}. "
            f"Valid states: {', '.join(spec.states)}."
        )

    db = get_db()
    coll = db[spec.collection]
    doc = await coll.find_one({spec.key: key})
    if not doc:
        return f"not found: no {entity} with {spec.key}={key!r}."
    current = doc.get("status")
    if to == current:
        return f"no-op: {entity} {key!r} is already in state '{to}'."
    if not spec.can_transition(current, to):
        allowed = spec.allowed_from(current) or ["(none — terminal state)"]
        return (
            f"[error] illegal transition for {entity} {key!r}: "
            f"'{current}' -> '{to}'. Allowed from '{current}': "
            f"{', '.join(allowed)}."
        )

    now = _now()
    event = {"ts": now, "action": "moved", "from": current, "to": to}
    if note:
        event["note"] = note
    updated = await coll.find_one_and_update(
        {spec.key: key, "status": current},  # precondition: still in `current`
        {
            "$set": {"status": to, "last_action_at": now, "updated_at": now},
            "$push": {"history": event},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        return (
            f"skipped: {entity} {key!r} changed state concurrently "
            f"(no longer in '{current}'). Re-read before retrying."
        )
    return f"moved {entity} {key!r}: '{current}' -> '{to}'.\n{_dump(updated)}"


async def update(entity: str, key, fields: dict) -> str:
    """Set non-status fields on an entity and append an update event to history[].
    Status changes are rejected — use move_state for those.
    """
    try:
        spec = resolve(entity)
    except WorkflowSpecError as e:
        return f"[error] {e}"
    if not isinstance(fields, dict) or not fields:
        return "[error] 'fields' must be a non-empty object."
    if "status" in fields:
        return "[error] use db_move_state to change status, not db_update."

    db = get_db()
    coll = db[spec.collection]
    now = _now()
    payload = dict(fields)
    payload["updated_at"] = now
    updated = await coll.find_one_and_update(
        {spec.key: key},
        {
            "$set": payload,
            "$push": {
                "history": {"ts": now, "action": "updated", "fields": list(fields)}
            },
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        return f"not found: no {entity} with {spec.key}={key!r}."
    return f"updated {entity} {key!r}.\n{_dump(updated)}"


async def add_event(entity: str, key, event) -> str:
    """Append an arbitrary event to an entity's history[] (the timeline)."""
    try:
        spec = resolve(entity)
    except WorkflowSpecError as e:
        return f"[error] {e}"
    db = get_db()
    now = _now()
    entry = {"ts": now, "action": "event"}
    if isinstance(event, dict):
        entry.update(event)
    else:
        entry["event"] = str(event)
    updated = await db[spec.collection].find_one_and_update(
        {spec.key: key},
        {"$push": {"history": entry}, "$set": {"updated_at": now}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        return f"not found: no {entity} with {spec.key}={key!r}."
    return f"event added to {entity} {key!r}."


async def counter(name: str, period: str, incr: int = 0, cap: int | None = None) -> str:
    """Read or increment a rate counter in the `counters` collection.

    Keyed by {name, period}. With incr>0 it atomically increments and returns the
    new count; with incr=0 it just reads. `cap` is informational only (the lighter
    model does not hard-block) — the returned line flags whether the cap is hit so
    the cookbook logic can decide to stop.
    """
    db = get_db()
    coll = db[COUNTERS_COLLECTION]
    if incr:
        doc = await coll.find_one_and_update(
            {"name": name, "period": period},
            {"$inc": {"count": int(incr)}, "$set": {"updated_at": _now()}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        count = doc.get("count", 0)
    else:
        doc = await coll.find_one({"name": name, "period": period})
        count = doc.get("count", 0) if doc else 0
    line = f"counter {name}/{period}: count={count}"
    if cap is not None:
        line += f", cap={cap}, at_cap={count >= cap}"
    return line

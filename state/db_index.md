# DB Index — operational system of record (the map)

> Read this before any DB work. Update it whenever the schema changes. This is a terse map, not a manual. Episodic detail → palace.

## Connection

- Access via `run_shell`: python (pymongo) or bash (mongosh). No dedicated tools.
- Config: `MONGO_URI`, `MONGO_DB` (env).
- Prereq for the python path: `pymongo` in the venv (`pip install pymongo`).

## The connector

DB work uses the shared async connector **inline** via `run_shell` — don't author your own script files. `get_db()` is the whole API; compose the operation yourself. Import from `scripts/` so `from lib.db` resolves:

```bash
cd scripts && python - <<'PY'
import asyncio
from datetime import datetime, timezone
from pymongo import ReturnDocument
from lib.db import get_db                        # scripts/ is on sys.path

async def main():
    db = get_db()                                # MONGO_DB by default
    now = datetime.now(timezone.utc)
    doc = await db.linkedin_profiles.find_one_and_update(
        {"profile_url": url, "status": "queued"},               # precondition
        {"$set": {"status": "request_sent", "last_action_at": now},
         "$push": {"history": {"ts": now, "action": "request_sent"}}},
        return_document=ReturnDocument.AFTER,
    )
    print("acted" if doc else "skipped (precondition not met)")

asyncio.run(main())
PY
```

### Connector API — `scripts/lib/db.py` (shared infra; use as-is, don't fork)
- `get_client()` / `get_db(name=None)` — cached `AsyncMongoClient` / db handle (env: `MONGO_URI`, `MONGO_DB`). That is the entire surface — **no query helpers**. Write the operation you need inline with pymongo, following the doctrine in `config/DATA.md`.

### The operations you compose (patterns, not helpers)
- **Indexes** (once per new collection): `await db.coll.create_index("profile_url", unique=True)` + secondaries like `await db.coll.create_index([("status", 1), ("next_action_at", 1)])`.
- **Exact read for state:** `await db.coll.find_one({"profile_url": url})` — never search.
- **Atomic guarded transition + audit:** `find_one_and_update` with the precondition in the filter (e.g. `"status": "queued"`), `$set` the new status + `last_action_at`, `$push` an event to `history[]`. A `None` return means already-processed — your exactly-once / idempotency guard.
- **"What's due now?":** `db.coll.find({"status": "queued", "$or": [{"next_action_at": None}, {"next_action_at": {"$lte": now}}]}).sort("next_action_at", 1)` — `next_action_at` is how you schedule a future follow-up.
- **Rate caps:** `find_one_and_update({"name": ..., "period": ...}, {"$inc": {"count": 1}}, upsert=True, return_document=ReturnDocument.AFTER)`, then check `count` against the cap before acting.

## Ad-hoc snippets

One-time index setup for a new collection (`cd scripts` so `from lib.db` resolves under `python -`):

```bash
cd scripts && python - <<'PY'
import asyncio
from lib.db import get_db
async def main():
    db = get_db()
    await db.linkedin_profiles.create_index("profile_url", unique=True)
    await db.linkedin_profiles.create_index([("status", 1), ("next_action_at", 1)])
asyncio.run(main())
PY
```

## Collections

### linkedin_profiles
- Purpose: one doc per LinkedIn person; the outreach ledger.
- Unique key: `profile_url`  ← dedup + idempotency
- Status: `discovered` → `queued` → `request_sent` → `connected` → `replied` → `done` | `skipped`
- Indexes: `unique(profile_url)`, `(status, next_action_at)`
- Fields: `name`, `headline`, `company`, `status`, `next_action_at`, `last_action_at`, `attempts`, `history[]`, `notes`, `source`

### counters
- Purpose: enforce platform caps globally (don't trip rate limits).
- Key: `{ name, period }` — e.g. `{name: "linkedin_invites", period: "2026-W26", count: 14}`

### credentials
- Purpose: the ONLY place secrets live (logins, TOTP secrets, API keys). Never hardcode creds in the repo.
- Unique key: `name` (credential-set id, e.g. `linkedin`)
- Fields: `service`, `kind`, secret fields per service (`username`, `password`, `totp_secret`, `api_key`, …), `notes`
- Map of what's stored (metadata only) + read/write snippets: `state/credentials_map.md`

<!-- Add new collections above. Remove dead ones. Keep each to a few lines. -->

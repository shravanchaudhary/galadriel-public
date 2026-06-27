"""Async MongoDB connector for agent automation.

Shared infrastructure — import it, don't fork it per script. Uses PyMongo's
native async client (`AsyncMongoClient`); `motor` is end-of-life as of 2026.

This module is ONLY the connection. There are deliberately no query/transition
helpers — you (the agent) compose the operation you need inline, following the
persistence doctrine in config/DATA.md and state/db_index.md:
  - exact lookups, never search, for state
  - atomic, precondition-guarded transitions (find_one_and_update)
  - append every state change to history[]
  - one unique key per entity for dedup / idempotency

Connection comes from the environment, which scripts inherit from the harness:
    MONGO_URI   full connection string
    MONGO_DB    default database name

Call it inline via run_shell (cd scripts so `from lib.db` resolves); don't author
standalone script files, and don't re-add helpers here — keep ops at the call site:

    cd scripts && python - <<'PY'
    import asyncio
    from datetime import datetime, timezone
    from pymongo import ReturnDocument
    from lib.db import get_db

    async def main():
        db = get_db()
        now = datetime.now(timezone.utc)
        # atomic, exactly-once transition: acts only if still queued
        doc = await db.linkedin_profiles.find_one_and_update(
            {"profile_url": url, "status": "queued"},          # precondition
            {"$set": {"status": "request_sent", "last_action_at": now},
             "$push": {"history": {"ts": now, "action": "request_sent"}}},
            return_document=ReturnDocument.AFTER,
        )
        print("acted" if doc else "skipped (precondition not met)")

    asyncio.run(main())
    PY
"""

import os

from pymongo import AsyncMongoClient

_client: AsyncMongoClient | None = None


def get_client() -> AsyncMongoClient:
    """Return a process-cached async client. Bound to the running loop on first await."""
    global _client
    if _client is None:
        uri = os.environ.get("MONGO_URI")
        if not uri:
            raise RuntimeError("MONGO_URI not set in environment")
        _client = AsyncMongoClient(uri)
    return _client


def get_db(name: str | None = None):
    """Return a database handle. Falls back to MONGO_DB when name is omitted."""
    db_name = name or os.environ.get("MONGO_DB")
    if not db_name:
        raise RuntimeError("no database name given and MONGO_DB not set")
    return get_client()[db_name]

"""Async MongoDB connector — internal infrastructure.

This is the shared connection used by the DB primitives in `harness/db_ops.py`.
It is NOT agent-facing: the agent touches MongoDB only through the db_* primitive
tools, never freestyle pymongo in run_shell (that path has been removed). Uses
PyMongo's native async client (`AsyncMongoClient`); `motor` is end-of-life as of
2026.

This module is ONLY the connection — no query/transition helpers. The primitives
in `harness/db_ops.py` compose the operations and enforce the workflow spec
(state machine + history), per the doctrine in knowledge/reference/data.md and
state/db_index.md.

Connection comes from the environment, inherited from the harness:
    MONGO_URI   full connection string
    MONGO_DB    default database name
"""

import os
import asyncio
import weakref

from pymongo import AsyncMongoClient

_clients = weakref.WeakKeyDictionary()


def get_client() -> AsyncMongoClient:
    """Return a process-cached async client. Bound to the running loop on first await."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop in _clients:
        return _clients[loop]

    uri = os.environ.get("MONGO_URI")
    if not uri:
        raise RuntimeError("MONGO_URI not set in environment")
        
    client = AsyncMongoClient(uri)
    
    if loop is not None:
        _clients[loop] = client
        
    return client


def get_db(name: str | None = None):
    """Return a database handle. Falls back to MONGO_DB when name is omitted."""
    db_name = name or os.environ.get("MONGO_DB")
    if not db_name:
        raise RuntimeError("no database name given and MONGO_DB not set")
    return get_client()[db_name]

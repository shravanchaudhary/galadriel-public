"""Provider-side creation of database-scoped DocumentDB IAM identities."""

from __future__ import annotations

import os
import re
from urllib.parse import parse_qsl, urlencode

from pymongo import MongoClient
from pymongo.errors import OperationFailure


def database_name(tenant_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", tenant_id)
    if not safe:
        raise ValueError("Invalid tenant identifier")
    return f"{os.environ.get('REPLIKA_MONGO_DB_PREFIX', 'replika_')}{safe}"


def iam_runtime_uri(admin_uri: str) -> str:
    """Remove admin credentials and force MONGODB-AWS authentication."""
    if not admin_uri.startswith(("mongodb://", "mongodb+srv://")):
        raise ValueError("MONGO_URI must be a MongoDB connection string")
    scheme, remainder = admin_uri.split("://", 1)
    authority_and_path, _separator, query = remainder.partition("?")
    authority, _slash, _path = authority_and_path.partition("/")
    hosts = authority.rsplit("@", 1)[-1]
    options = dict(parse_qsl(query, keep_blank_values=True))
    options["authSource"] = "$external"
    options["authMechanism"] = "MONGODB-AWS"
    return f"{scheme}://{hosts}/{('?' + urlencode(options)) if options else ''}"


def ensure_tenant_identity(
    tenant_id: str,
    task_role_arn: str,
    *,
    client=None,
) -> dict[str, str]:
    admin_uri = os.environ.get("MONGO_URI", "")
    if not admin_uri:
        raise RuntimeError("MONGO_URI is not configured")
    db_name = database_name(tenant_id)
    mongo = client or MongoClient(admin_uri)
    external = mongo["$external"]
    roles = [{"role": "readWrite", "db": db_name}]
    try:
        external.command(
            {
                "createUser": task_role_arn,
                "mechanisms": ["MONGODB-AWS"],
                "roles": roles,
            }
        )
    except OperationFailure as exc:
        if exc.code not in {51003, 11000} and "already exists" not in str(exc).lower():
            raise
        external.command({"updateUser": task_role_arn, "roles": roles})
    return {"mongo_uri": iam_runtime_uri(admin_uri), "mongo_db": db_name}

"""Tenant-scoped BYOM credentials encrypted with provider-owned KMS."""

from __future__ import annotations

import base64
import os
from datetime import datetime, timezone

from pymongo import MongoClient

COLLECTION = "provider_credentials"
SUPPORTED_PROVIDERS = frozenset({"anthropic", "gemini"})
_sync_db = None


def tenant_id() -> str:
    value = os.environ.get("REPLIKA_TENANT_ID", "").strip()
    if not value:
        raise RuntimeError("REPLIKA_TENANT_ID is not configured")
    return value


def _db():
    global _sync_db
    if _sync_db is not None:
        return _sync_db
    uri = os.environ.get("MONGO_URI")
    name = os.environ.get("MONGO_DB")
    if not uri or not name:
        raise RuntimeError("MONGO_URI / MONGO_DB not configured")
    _sync_db = MongoClient(uri)[name]
    return _sync_db


def _kms():
    import boto3

    return boto3.client("kms")


def _provider(value: str) -> str:
    provider = (value or "").strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError("Unsupported model provider")
    return provider


def _context(provider: str) -> dict[str, str]:
    return {"tenant_id": tenant_id(), "provider": provider, "purpose": "byom"}


def put(provider: str, api_key: str, *, kms_client=None, db=None) -> dict:
    provider = _provider(provider)
    secret = (api_key or "").strip()
    if not secret:
        raise ValueError("API key is required")
    key_id = os.environ.get("REPLIKA_KMS_KEY_ID")
    if not key_id:
        raise RuntimeError("REPLIKA_KMS_KEY_ID is not configured")
    encrypted = (kms_client or _kms()).encrypt(
        KeyId=key_id,
        Plaintext=secret.encode("utf-8"),
        EncryptionContext=_context(provider),
    )["CiphertextBlob"]
    now = datetime.now(timezone.utc)
    document = {
        "_id": f"{tenant_id()}:{provider}",
        "tenant_id": tenant_id(),
        "provider": provider,
        "ciphertext": base64.b64encode(encrypted).decode("ascii"),
        "key_fingerprint": secret[-4:],
        "created_at": now,
        "updated_at": now,
    }
    database = db if db is not None else _db()
    database[COLLECTION].replace_one(
        {"_id": document["_id"]},
        document,
        upsert=True,
    )
    return summary(document)


def get(provider: str, *, kms_client=None, db=None) -> str | None:
    provider = _provider(provider)
    database = db if db is not None else _db()
    document = database[COLLECTION].find_one(
        {"_id": f"{tenant_id()}:{provider}", "tenant_id": tenant_id()}
    )
    if not document:
        return None
    plaintext = (kms_client or _kms()).decrypt(
        CiphertextBlob=base64.b64decode(document["ciphertext"]),
        EncryptionContext=_context(provider),
    )["Plaintext"]
    return plaintext.decode("utf-8")


def delete(provider: str, *, db=None) -> bool:
    provider = _provider(provider)
    database = db if db is not None else _db()
    result = database[COLLECTION].delete_one(
        {"_id": f"{tenant_id()}:{provider}", "tenant_id": tenant_id()}
    )
    return bool(result.deleted_count)


def list_summaries(*, db=None) -> list[dict]:
    database = db if db is not None else _db()
    rows = database[COLLECTION].find(
        {"tenant_id": tenant_id()},
        {"ciphertext": 0},
    )
    return [summary(row) for row in rows]


def summary(document: dict) -> dict:
    return {
        "provider": document["provider"],
        "configured": True,
        "masked": f"••••{document.get('key_fingerprint', '')}",
        "updated_at": document.get("updated_at"),
    }

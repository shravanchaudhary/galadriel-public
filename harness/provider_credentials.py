"""Tenant-scoped BYOM credentials encrypted with provider-owned KMS."""

from __future__ import annotations

import base64
import os
from datetime import datetime, timezone

from pymongo import MongoClient

COLLECTION = "provider_credentials"
SUPPORTED_PROVIDERS = frozenset(
    {"bedrock_anthropic", "bedrock_mantle", "gemini"}
)
# Providers shown (and BYOM-savable) in the Tower UI. "bedrock" is a single
# entry covering both bedrock_anthropic and bedrock_mantle, which share one
# credential (AWS_BEARER_TOKEN_BEDROCK) — see `_STORAGE_PROVIDER` below.
UI_PROVIDERS = frozenset({"gemini", "bedrock"})
BYOM_PROVIDERS = frozenset({"gemini", "bedrock"})
# Internal provider ids that alias to a shared UI/storage credential.
_STORAGE_PROVIDER = {"bedrock_anthropic": "bedrock", "bedrock_mantle": "bedrock"}
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
    provider = _STORAGE_PROVIDER.get(provider, provider)
    if provider not in SUPPORTED_PROVIDERS and provider not in UI_PROVIDERS:
        raise ValueError("Unsupported model provider")
    return provider


def _context(provider: str) -> dict[str, str]:
    return {"tenant_id": tenant_id(), "provider": provider, "purpose": "byom"}


def put(provider: str, api_key: str, *, kms_client=None, db=None) -> dict:
    provider = _provider(provider)
    if provider not in BYOM_PROVIDERS:
        raise ValueError(f"{provider.title()} API keys are coming soon")
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


def _env_key_for(provider: str) -> str | None:
    """Plaintext API key from process env, if present."""
    if provider in ("bedrock", "bedrock_anthropic", "bedrock_mantle"):
        # Both Bedrock providers authenticate with the same Bedrock API key.
        return (os.environ.get("AWS_BEARER_TOKEN_BEDROCK") or "").strip() or None
    if provider == "gemini":
        return (
            (os.environ.get("GEMINI_API_KEY") or "").strip()
            or (os.environ.get("GOOGLE_API_KEY") or "").strip()
            or None
        )
    return None


def _mask_secret(secret: str) -> str:
    tail = secret[-4:] if len(secret) >= 4 else secret
    return f"****{tail}"


def list_summaries(*, db=None) -> list[dict]:
    """Return per-provider config status (BYOM Mongo first, else process env)."""
    by_provider: dict[str, dict] = {}
    try:
        database = db if db is not None else _db()
        rows = database[COLLECTION].find(
            {"tenant_id": tenant_id()},
            {"ciphertext": 0},
        )
        for row in rows:
            by_provider[row["provider"]] = summary(row, source="byom")
    except Exception:
        # Local / env-only deployments still report configured keys below.
        pass
    for provider in sorted(UI_PROVIDERS):
        if provider in by_provider:
            continue
        secret = _env_key_for(provider)
        if secret:
            # Platform/env keys are never fingerprinted to the browser.
            by_provider[provider] = {
                "provider": provider,
                "configured": True,
                "masked": None,
                "source": "env",
                "updated_at": None,
            }
        else:
            by_provider[provider] = {
                "provider": provider,
                "configured": False,
                "masked": None,
                "source": None,
                "updated_at": None,
            }
    return [by_provider[p] for p in sorted(UI_PROVIDERS)]


def summary(document: dict, *, source: str = "byom") -> dict:
    fingerprint = document.get("key_fingerprint") or ""
    return {
        "provider": document["provider"],
        "configured": True,
        "masked": f"****{fingerprint}" if fingerprint else "****",
        "source": source,
        "updated_at": document.get("updated_at"),
    }

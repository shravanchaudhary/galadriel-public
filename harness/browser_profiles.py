"""Tenant-scoped browser connection profiles persisted in MongoDB."""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

from pymongo import MongoClient

from .bce_client import BCEError, normalize_pairing_code

COLLECTION = "browser_profiles"
MIGRATIONS_COLLECTION = "runtime_migrations"
LEGACY_MIGRATION = "browser_profiles_markdown_v1"
LEGACY_PATH = Path("state/browser_profiles.md")
SUPPORTED_BACKENDS = frozenset({"bce", "browser-use"})
_PROFILE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_sync_db = None


def tenant_id() -> str:
    return os.environ.get("REPLIKA_TENANT_ID", "default").strip() or "default"


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


def _database(db=None):
    return db if db is not None else _db()


def _document_id(profile_id: str) -> str:
    return f"{tenant_id()}:{profile_id}"


def validate_profile_id(value: str) -> str:
    profile_id = (value or "").strip().lower()
    if not _PROFILE_RE.fullmatch(profile_id):
        raise ValueError(
            "profile_id must start with a lowercase letter or digit and contain "
            "only lowercase letters, digits, underscores, or hyphens (max 64)"
        )
    return profile_id


def _backend(value: str) -> str:
    backend = (value or "").strip().lower().replace("_", "-")
    if backend not in SUPPORTED_BACKENDS:
        raise ValueError("backend must be 'bce' or 'browser-use'")
    return backend


def _purpose(value: str | None) -> str:
    purpose = (value or "").strip()
    if len(purpose) > 200:
        raise ValueError("purpose must be 200 characters or fewer")
    return purpose


def _cdp_port(value) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("cdp_port must be an integer") from exc
    if not 1024 <= port <= 65535:
        raise ValueError("cdp_port must be between 1024 and 65535")
    return port


def _serializable(document: dict) -> dict:
    return {
        key: document.get(key)
        for key in (
            "profile_id",
            "backend",
            "pairing_code",
            "cdp_port",
            "purpose",
            "created_at",
            "updated_at",
        )
        if document.get(key) is not None
    }


def legacy_path() -> Path:
    root = os.environ.get("GALADRIEL_STORAGE_ROOT")
    return Path(root) / LEGACY_PATH if root else LEGACY_PATH


def parse_legacy_profiles(path: Path | None = None) -> list[dict]:
    """Parse the old three-column Markdown registry without modifying it."""
    path = path or legacy_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    profiles = []
    for raw_line in lines:
        line = raw_line.strip()
        if not (line.startswith("|") and line.endswith("|")):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 3:
            continue
        try:
            profile_id = validate_profile_id(cells[0])
        except ValueError:
            continue
        try:
            pairing_code = normalize_pairing_code(cells[1])
            profiles.append({
                "profile_id": profile_id,
                "backend": "bce",
                "pairing_code": pairing_code,
                "purpose": cells[2],
            })
            continue
        except BCEError:
            pass
        try:
            port = _cdp_port(cells[1])
        except ValueError:
            continue
        profiles.append({
            "profile_id": profile_id,
            "backend": "browser-use",
            "cdp_port": port,
            "purpose": cells[2],
        })
    return profiles


def migrate_legacy_if_empty(*, db=None, path: Path | None = None) -> int:
    """Import the Markdown registry once, only when this tenant has no records."""
    database = _database(db)
    migration_id = f"{tenant_id()}:{LEGACY_MIGRATION}"
    if database[MIGRATIONS_COLLECTION].find_one({"_id": migration_id}):
        return 0
    has_profiles = bool(
        database[COLLECTION].find_one({"tenant_id": tenant_id()}, {"_id": 1})
    )
    imported = 0
    if not has_profiles:
        for profile in parse_legacy_profiles(path):
            upsert(db=database, **profile)
            imported += 1
    database[MIGRATIONS_COLLECTION].replace_one(
        {"_id": migration_id},
        {
            "_id": migration_id,
            "tenant_id": tenant_id(),
            "migration": LEGACY_MIGRATION,
            "completed_at": datetime.now(timezone.utc),
            "imported": imported,
        },
        upsert=True,
    )
    return imported


def list_profiles(*, db=None, migrate: bool = True) -> list[dict]:
    database = _database(db)
    if migrate:
        migrate_legacy_if_empty(db=database)
    rows = database[COLLECTION].find({"tenant_id": tenant_id()})
    return sorted((_serializable(row) for row in rows), key=lambda row: row["profile_id"])


def get(profile_id: str, *, db=None, migrate: bool = True) -> dict | None:
    profile_id = validate_profile_id(profile_id)
    database = _database(db)
    if migrate:
        migrate_legacy_if_empty(db=database)
    document = database[COLLECTION].find_one({
        "_id": _document_id(profile_id),
        "tenant_id": tenant_id(),
    })
    return _serializable(document) if document else None


def upsert(
    profile_id: str,
    backend: str,
    *,
    pairing_code: str | None = None,
    cdp_port: int | None = None,
    purpose: str | None = None,
    db=None,
) -> dict:
    profile_id = validate_profile_id(profile_id)
    backend = _backend(backend)
    document = {
        "_id": _document_id(profile_id),
        "tenant_id": tenant_id(),
        "profile_id": profile_id,
        "backend": backend,
        "purpose": _purpose(purpose),
        "updated_at": datetime.now(timezone.utc),
    }
    if backend == "bce":
        if not pairing_code:
            raise ValueError("pairing_code is required for BCE profiles")
        document["pairing_code"] = normalize_pairing_code(pairing_code)
    else:
        if cdp_port is None:
            raise ValueError("cdp_port is required for browser-use profiles")
        document["cdp_port"] = _cdp_port(cdp_port)

    database = _database(db)
    existing = database[COLLECTION].find_one(
        {"_id": document["_id"], "tenant_id": tenant_id()},
        {"created_at": 1},
    )
    document["created_at"] = (
        existing.get("created_at") if existing else document["updated_at"]
    )
    database[COLLECTION].replace_one(
        {"_id": document["_id"], "tenant_id": tenant_id()},
        document,
        upsert=True,
    )
    return _serializable(document)


def delete(profile_id: str, *, db=None) -> bool:
    profile_id = validate_profile_id(profile_id)
    result = _database(db)[COLLECTION].delete_one({
        "_id": _document_id(profile_id),
        "tenant_id": tenant_id(),
    })
    return bool(result.deleted_count)

"""Readiness checks for externally managed operational services."""

import os
from pathlib import Path
import shutil


def readiness_error() -> str | None:
    """Return a concise error when configured Valkey or Mongo-compatible DB is unavailable."""
    if os.environ.get("PHONE_BRIDGE_ENABLED", "0").lower() in {
        "1",
        "true",
        "yes",
    }:
        adb_binary = os.environ.get("ADB_BINARY", "adb")
        if shutil.which(adb_binary) is None:
            return "Phone bridge ADB binary unavailable"
        adb_key = Path(
            os.environ.get(
                "ADB_VENDOR_KEYS",
                str(Path.home() / ".android" / "adbkey"),
            )
        )
        if not adb_key.is_file():
            return "Phone bridge ADB identity unavailable"

    redis_url = os.environ.get("REDIS_URL")
    if redis_url:
        try:
            import redis

            redis.Redis.from_url(
                redis_url,
                socket_connect_timeout=1,
                socket_timeout=1,
            ).ping()
        except Exception:
            return "Valkey unavailable"

    mongo_uri = os.environ.get("MONGO_URI")
    if mongo_uri:
        try:
            from pymongo import MongoClient

            MongoClient(
                mongo_uri,
                serverSelectionTimeoutMS=1000,
                connectTimeoutMS=1000,
            ).admin.command("ping")
        except Exception:
            return "MongoDB/DocumentDB unavailable"

    return None

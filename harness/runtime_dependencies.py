"""Readiness checks for externally managed operational services."""

import os


def readiness_error() -> str | None:
    """Return a concise error when configured Valkey or DocumentDB is unavailable."""
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
            return "DocumentDB unavailable"

    return None

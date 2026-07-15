"""Smoke-test public health checks and protected Tower routes."""

import base64
import os
import sys
from pathlib import Path

os.environ["TOWER_AUTH_REQUIRED"] = "true"
os.environ["TOWER_AUTH_TOKEN"] = "test-token"
os.environ.pop("MONGO_URI", None)
os.environ.pop("REDIS_URL", None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tower.app import create_tower


class _Agent:
    pass


app = create_tower(_Agent())
client = app.test_client()

assert client.get("/healthz").status_code == 200
assert client.get("/readyz").status_code == 200
assert client.get("/").status_code == 401

credentials = base64.b64encode(b"clyra:test-token").decode()
assert client.get("/", headers={"Authorization": f"Basic {credentials}"}).status_code == 200

print("Tower health and authentication checks passed.")

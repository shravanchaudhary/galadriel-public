"""Smoke-test the lightweight Replika control-plane app."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ["REPLIKA_ALLOW_LOCAL_PROVISIONING"] = "true"
os.environ["REPLIKA_CONTROL_PLANE_ONLY"] = "true"
os.environ["TOWER_AUTH_REQUIRED"] = "false"
os.environ.pop("MONGO_URI", None)
os.environ.pop("REDIS_URL", None)

from tower.control_app import create_control_app  # noqa: E402

assert "harness.agent" not in sys.modules

client = create_control_app().test_client()
assert client.get("/healthz").status_code == 200
assert client.get("/readyz").status_code == 200

root = client.get("/", follow_redirects=False)
assert root.status_code == 302
assert root.headers["Location"] == "/replika"
replika = client.get("/replika")
assert replika.status_code == 200
assert b'href="/integrations"' in replika.data
assert client.get("/api/chat").status_code == 404

print("Control-plane app check passed.")

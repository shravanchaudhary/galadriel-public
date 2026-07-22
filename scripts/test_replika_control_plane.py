"""Smoke tests for customer-safe Replika onboarding."""

from __future__ import annotations

import os
import sys
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.update(
    {
        "TOWER_AUTH_REQUIRED": "true",
        "TOWER_AUTH_USERNAME": "account-123",
        "TOWER_AUTH_TOKEN": "test-token",
        "TOWER_SECRET_KEY": "test-secret-key-not-default",
        "TOWER_COOKIE_SECURE": "false",
        "REPLIKA_PRODUCT_DOMAIN": "replika.example",
        "REPLIKA_PROVISIONING_MODE": "local",
        "REPLIKA_ALLOW_LOCAL_PROVISIONING": "true",
        "REPLIKA_CONTROL_PLANE_ONLY": "true",
    }
)
os.environ.pop("MONGO_URI", None)
os.environ.pop("REDIS_URL", None)

from flask import Flask  # noqa: E402
from tower import auth as tower_auth  # noqa: E402
from tower.replika_control_plane import (  # noqa: E402
    Provisioner,
    ReplikaAlreadyExists,
    UsernameUnavailable,
    normalize_replika_type,
    normalize_username,
    register_replika_control_plane,
)


class _Store:
    def __init__(self):
        self.by_owner = {}
        self.by_username = {}

    def find_for_owner(self, owner_id):
        return self.by_owner.get(owner_id)

    def username_available(self, username, owner_id=None):
        current = self.by_username.get(username)
        return current is None or current["owner_id"] == owner_id

    def reserve(self, owner_id, username, replika_type):
        current = self.by_owner.get(owner_id)
        if current and (
            current["username"] != username
            or current["replika_type"] != replika_type
        ):
            raise ReplikaAlreadyExists("This account already has a Replika.")
        claimed = self.by_username.get(username)
        if claimed and claimed["owner_id"] != owner_id:
            raise UsernameUnavailable("That username is not available.")
        if current:
            return current
        doc = {
            "owner_id": owner_id,
            "username": username,
            "replika_type": replika_type,
            "status": "creating",
            "provisioning_requested_at": None,
            "product_url": f"https://{username}.replika.example",
            "release_version": "v0",
        }
        self.by_owner[owner_id] = doc
        self.by_username[username] = doc
        return doc

    def update_status(self, owner_id, status, **_kwargs):
        self.by_owner[owner_id]["status"] = status
        if status == "creating":
            self.by_owner[owner_id]["provisioning_requested_at"] = None
        return self.by_owner[owner_id]

    def claim_provisioning(self, owner_id):
        document = self.by_owner[owner_id]
        if document["status"] != "creating" or document["provisioning_requested_at"]:
            return None
        document["provisioning_requested_at"] = "now"
        return document


class _Provisioner:
    def __init__(self):
        self.calls = []

    def start(self, replika):
        self.calls.append((replika["username"], replika["replika_type"]))


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


_assert(normalize_username(" Alice-01 ") == "alice-01", "username normalization")
_assert(normalize_replika_type(" Organization ") == "organization", "type normalization")
for invalid in ("ab", "-alice", "alice-", "alice--x", "ADMIN", "has space"):
    try:
        normalize_username(invalid)
    except ValueError:
        pass
    else:
        raise AssertionError(f"{invalid!r} should be invalid")


class _Lambda:
    def invoke(self, **kwargs):
        self.payload = json.loads(kwargs["Payload"])
        return {"StatusCode": 202}


lambda_client = _Lambda()
Provisioner("arn:test", lambda_client).start(
    {
        "owner_id": "account-123",
        "username": "alice",
        "replika_type": "individual",
        "product_url": "https://alice.replika.example",
        "release_version": "v0",
    }
)
_assert(lambda_client.payload["replika_type"] == "individual", "provisioner type payload")

app = Flask(
    __name__,
    template_folder=str(ROOT / "tower" / "templates"),
    static_folder=str(ROOT / "tower" / "static"),
)
tower_auth.configure_app_sessions(app)
register_replika_control_plane(app)
store = _Store()
provisioner = _Provisioner()
app.config["REPLIKA_STORE"] = store
app.config["REPLIKA_PROVISIONER"] = provisioner
client = app.test_client()

anonymous = client.get("/api/replika")
_assert(anonymous.status_code == 401, "anonymous control-plane access must be denied")

with client.session_transaction() as session:
    session[tower_auth.SESSION_AUTH_KEY] = True
    session[tower_auth.SESSION_USER_KEY] = "account-123"

available = client.get("/api/replika/username/alice")
_assert(available.status_code == 200 and available.get_json()["available"], "availability")

created = client.post(
    "/api/replika",
    json={"username": "Alice", "replika_type": "organization"},
    headers={"Origin": "http://localhost"},
)
body = created.get_json()
_assert(created.status_code == 202, f"create failed: {body}")
_assert(body["replika"]["status"] == "ready", "local provisioner should complete")
_assert(body["replika"]["url"] == "https://alice.replika.example", "product URL")
_assert(body["replika"]["replika_type"] == "organization", "customer type")
_assert(provisioner.calls == [("alice", "organization")], "provisioner type payload")
_assert("internal_error" not in str(body), "internal fields must never be exposed")

again = client.post(
    "/api/replika",
    json={"username": "alice", "replika_type": "organization"},
    headers={"Origin": "http://localhost"},
)
_assert(again.status_code == 202, "same reservation should be idempotent")
_assert(provisioner.calls == [("alice", "organization")], "ready Replika must not be reprovisioned")

store.update_status("account-123", "error")
retried = client.post(
    "/api/replika",
    json={"username": "alice", "replika_type": "organization"},
    headers={"Origin": "http://localhost"},
)
_assert(retried.status_code == 202, "failed provisioning should be retryable")
_assert(
    provisioner.calls == [("alice", "organization"), ("alice", "organization")],
    "retry should invoke provisioning once",
)

conflict = client.post(
    "/api/replika",
    json={"username": "bob", "replika_type": "organization"},
    headers={"Origin": "http://localhost"},
)
_assert(conflict.status_code == 409, "one Replika per account must be enforced")

immutable = client.post(
    "/api/replika",
    json={"username": "alice", "replika_type": "individual"},
    headers={"Origin": "http://localhost"},
)
_assert(immutable.status_code == 409, "Replika type must be immutable")

os.environ["REPLIKA_TRUST_ALB_IDENTITY"] = "true"
os.environ["REPLIKA_TENANT_ID"] = "tenant-a"
with app.test_request_context(
    "/",
    headers={"x-amzn-oidc-identity": "tenant-a"},
):
    result = tower_auth.authenticate_request()
    _assert(result is not None and result.method == "alb", "assigned ALB identity")
with app.test_request_context(
    "/",
    headers={"x-amzn-oidc-identity": "tenant-b"},
):
    _assert(
        tower_auth.authenticate_request() is None,
        "cross-tenant ALB identity must be rejected",
    )
os.environ.pop("REPLIKA_TRUST_ALB_IDENTITY", None)
os.environ.pop("REPLIKA_TENANT_ID", None)

print("Replika control-plane checks passed.")

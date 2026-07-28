"""Smoke tests for customer-safe multi-Replika onboarding and deletion."""

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
    UsernameUnavailable,
    normalize_replika_type,
    normalize_username,
    register_replika_control_plane,
)


class _Store:
    def __init__(self):
        self.by_id = {}
        self.by_username = {}
        self._seq = 0

    def find_by_id(self, replika_id):
        return self.by_id.get(replika_id)

    def find_owned(self, replika_id, owner_id):
        document = self.by_id.get(replika_id)
        if document and document["owner_id"] == owner_id:
            return document
        return None

    def list_for_owner(self, owner_id):
        return [
            document
            for document in self.by_id.values()
            if document["owner_id"] == owner_id
        ]

    def find_for_owner(self, owner_id):
        documents = self.list_for_owner(owner_id)
        return documents[0] if documents else None

    def username_available(self, username, owner_id=None):
        return username not in self.by_username

    def reserve(self, owner_id, username, replika_type):
        if username in self.by_username:
            raise UsernameUnavailable("That username is not available.")
        self._seq += 1
        replika_id = f"replika-{self._seq}"
        doc = {
            "_id": replika_id,
            "replika_id": replika_id,
            "owner_id": owner_id,
            "username": username,
            "replika_type": replika_type,
            "status": "creating",
            "provisioning_requested_at": None,
            "product_url": f"https://{username}.replika.example",
            "release_version": "v0",
        }
        self.by_id[replika_id] = doc
        self.by_username[username] = doc
        return doc

    def update_status(self, replika_id, status, **kwargs):
        document = self.by_id[replika_id]
        document["status"] = status
        if status == "creating":
            document["provisioning_requested_at"] = None
        if "internal_error" in kwargs:
            document["internal_error"] = kwargs["internal_error"]
        return document

    def claim_provisioning(self, replika_id):
        document = self.by_id[replika_id]
        if document["status"] != "creating" or document["provisioning_requested_at"]:
            return None
        document["provisioning_requested_at"] = "now"
        return document

    def claim_deletion(self, replika_id, owner_id):
        document = self.find_owned(replika_id, owner_id)
        if not document:
            return None
        document["status"] = "deleting"
        return document

    def delete_record(self, replika_id):
        document = self.by_id.pop(replika_id, None)
        if not document:
            return False
        self.by_username.pop(document["username"], None)
        return True


class _Provisioner:
    def __init__(self):
        self.calls = []
        self.deletes = []

    def start(self, replika):
        self.calls.append(
            (replika["replika_id"], replika["username"], replika["replika_type"])
        )

    def delete(self, replika):
        self.deletes.append(replika["replika_id"])


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
        "_id": "replika-1",
        "replika_id": "replika-1",
        "owner_id": "account-123",
        "username": "alice",
        "replika_type": "individual",
        "product_url": "https://alice.replika.example",
        "release_version": "v0",
    }
)
_assert(lambda_client.payload["replika_type"] == "individual", "provisioner type payload")
_assert(lambda_client.payload["replika_id"] == "replika-1", "provisioner replika_id")
_assert(lambda_client.payload["operation"] == "create", "provisioner create operation")
_assert(lambda_client.payload["owner_id"] == "account-123", "provisioner owner_id")

app = Flask(
    __name__,
    template_folder=str(ROOT / "tower" / "templates"),
    static_folder=str(ROOT / "tower" / "static"),
)
tower_auth.configure_app_sessions(app)


@app.context_processor
def _inject_page_context():
    return {"page_context": {}, "control_plane_only": True}


register_replika_control_plane(app)
store = _Store()
provisioner = _Provisioner()
purged = []
app.config["REPLIKA_STORE"] = store
app.config["REPLIKA_PROVISIONER"] = provisioner
app.config["REPLIKA_SLACK_PURGE"] = purged.append
client = app.test_client()

anonymous = client.get("/api/replikas")
_assert(anonymous.status_code == 401, "anonymous control-plane access must be denied")

with client.session_transaction() as session:
    session[tower_auth.SESSION_AUTH_KEY] = True
    session[tower_auth.SESSION_USER_KEY] = "account-123"

setup = client.get("/replika")
_assert(setup.status_code == 200, "setup page should render")
_assert(b'settings-page' in setup.data, "setup uses settings design system")
_assert(b'id="replika-form"' in setup.data, "empty state shows create form")
_assert(b'id="replika-list"' in setup.data, "list container is present")
_assert(b'.replika.example' in setup.data, "product domain is shown")

available = client.get("/api/replikas/username/alice")
_assert(available.status_code == 200 and available.get_json()["available"], "availability")

created = client.post(
    "/api/replikas",
    json={"username": "Alice", "replika_type": "organization"},
    headers={"Origin": "http://localhost"},
)
body = created.get_json()
_assert(created.status_code == 202, f"create failed: {body}")
_assert(body["replika"]["status"] == "ready", "local provisioner should complete")
_assert(body["replika"]["status_label"] == "Created", "ready label is Created")
_assert(body["replika"]["url"] == "https://alice.replika.example", "product URL")
_assert(body["replika"]["replika_type"] == "organization", "customer type")
_assert(body["replika"]["id"], "customer view exposes replika id")
first_id = body["replika"]["id"]
_assert(
    provisioner.calls == [(first_id, "alice", "organization")],
    "provisioner type payload",
)
_assert("internal_error" not in str(body), "internal fields must never be exposed")

ready_page = client.get("/replika")
_assert(ready_page.status_code == 200, "ready page should render")
_assert(b'data-status="ready"' in ready_page.data, "ready status pill is set")
_assert(b'Created' in ready_page.data, "created label renders")
_assert(b'Open' in ready_page.data, "ready state exposes open action")
_assert(b'Integrations' in ready_page.data, "ready state exposes integrations")
_assert(b'id="replika-form"' in ready_page.data, "create form remains available")

second = client.post(
    "/api/replikas",
    json={"username": "bob", "replika_type": "individual"},
    headers={"Origin": "http://localhost"},
)
second_body = second.get_json()
_assert(second.status_code == 202, f"second create failed: {second_body}")
second_id = second_body["replika"]["id"]
_assert(second_id != first_id, "each Replika gets a distinct id")
listed = client.get("/api/replikas").get_json()
_assert(len(listed["replikas"]) == 2, "owner can list multiple Replikas")

taken = client.post(
    "/api/replikas",
    json={"username": "alice", "replika_type": "organization"},
    headers={"Origin": "http://localhost"},
)
_assert(taken.status_code == 409, "global username uniqueness is enforced")

with client.session_transaction() as session:
    session[tower_auth.SESSION_USER_KEY] = "account-999"
cross = client.get(f"/api/replikas/{first_id}")
_assert(cross.status_code == 404, "cross-owner access is denied")
with client.session_transaction() as session:
    session[tower_auth.SESSION_USER_KEY] = "account-123"

store.update_status(first_id, "error")
retried = client.post(
    "/api/replikas",
    json={
        "username": "alice",
        "replika_type": "organization",
        "replika_id": first_id,
    },
    headers={"Origin": "http://localhost"},
)
_assert(retried.status_code == 202, "failed provisioning should be retryable")
_assert(len(provisioner.calls) == 3, "retry should invoke provisioning again")

deleted = client.delete(
    f"/api/replikas/{first_id}",
    headers={"Origin": "http://localhost"},
)
deleted_body = deleted.get_json()
_assert(deleted.status_code == 200, f"local delete failed: {deleted_body}")
_assert(deleted_body.get("deleted") is True, "local mode completes delete")
_assert(provisioner.deletes == [first_id], "delete invokes provisioner")
_assert(purged == [first_id], "delete purges Slack for that Replika")
_assert(store.find_by_id(first_id) is None, "record removed after teardown")
_assert(
    client.get("/api/replikas/username/alice").get_json()["available"],
    "username frees after complete teardown",
)

recreated = client.post(
    "/api/replikas",
    json={"username": "alice", "replika_type": "organization"},
    headers={"Origin": "http://localhost"},
)
_assert(recreated.status_code == 202, "same-name recreation works after delete")

# Simulate async deletion: username stays reserved while deleting.
deleting_id = second_id
store.claim_deletion(deleting_id, "account-123")
blocked = client.post(
    "/api/replikas",
    json={"username": "bob", "replika_type": "individual"},
    headers={"Origin": "http://localhost"},
)
_assert(blocked.status_code == 409, "username stays reserved while deleting")
store.delete_record(deleting_id)
_assert(
    client.get("/api/replikas/username/bob").get_json()["available"],
    "username frees after record removal",
)

os.environ["REPLIKA_TRUST_ALB_IDENTITY"] = "true"
os.environ["REPLIKA_TENANT_ID"] = "tenant-a"
os.environ["REPLIKA_OWNER_ID"] = "owner-a"
with app.test_request_context(
    "/",
    headers={"x-amzn-oidc-identity": "owner-a"},
):
    result = tower_auth.authenticate_request()
    _assert(result is not None and result.method == "alb", "assigned ALB owner identity")
with app.test_request_context(
    "/",
    headers={"x-amzn-oidc-identity": "tenant-a"},
):
    _assert(
        tower_auth.authenticate_request() is None,
        "tenant id alone must not authorize when owner id is set",
    )
with app.test_request_context(
    "/",
    headers={"x-amzn-oidc-identity": "owner-b"},
):
    _assert(
        tower_auth.authenticate_request() is None,
        "cross-owner ALB identity must be rejected",
    )
os.environ.pop("REPLIKA_TRUST_ALB_IDENTITY", None)
os.environ.pop("REPLIKA_TENANT_ID", None)
os.environ.pop("REPLIKA_OWNER_ID", None)

print("Replika control-plane checks passed.")

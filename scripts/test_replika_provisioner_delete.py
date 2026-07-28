"""Unit checks for provisioner create/delete dispatch and teardown helpers."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HANDLER_PATH = ROOT / "infra" / "provisioner" / "handler.py"

# Load the Lambda module without packaging it as a package import.
spec = importlib.util.spec_from_file_location("replika_provisioner_handler", HANDLER_PATH)
handler = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = handler
spec.loader.exec_module(handler)


class _ClientError(Exception):
    def __init__(self, code: str):
        self.response = {"Error": {"Code": code}}


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


# --- create/delete dispatch ---
posts = []


def fake_post(url, payload):
    posts.append((url, payload))
    if payload.get("action") == "delete":
        return {"status": "deleted"}
    return {"mongo_uri": "mongodb://iam", "mongo_db": "replika_r1"}


class _FakeClients:
    def __init__(self):
        self.calls = []

    def client(self, name):
        self.calls.append(name)
        return object()


with patch.object(handler, "_provider_post", side_effect=fake_post), patch.object(
    handler, "_create_access_point", return_value=("ap-arn", "ap-id")
), patch.object(handler, "_slack_auth_secret", return_value="secret-arn"), patch.object(
    handler, "_task_role", return_value="arn:aws:iam::1:role/replika-r1"
), patch.object(handler, "_target_group", side_effect=["tg", "tg-phone"]), patch.object(
    handler, "_ensure_rule"
), patch.object(handler, "_task_definition", return_value="task:1"), patch.object(
    handler, "_service"
), patch.dict(
    "os.environ",
    {
        "CALLBACK_URL": "https://control/internal/replika/provisioning",
        "DATABASE_BROKER_URL": "https://control/internal/replika/database",
        "CALLBACK_TOKEN_SECRET_ARN": "arn:secret",
    },
    clear=False,
), patch.object(handler.boto3, "client", side_effect=_FakeClients().client):
    result = handler._create_replika(
        replika_id="r1",
        owner_id="owner-1",
        username="alice",
        replika_type="organization",
        release_version="v0",
    )
_assert(result["status"] == "ready", "create returns ready")
_assert(posts[-1][1]["status"] == "ready", "create callbacks ready")
_assert(posts[-1][1]["replika_id"] == "r1", "callback includes replika_id")
_assert(posts[-1][1]["owner_id"] == "owner-1", "callback keeps owner_id")

posts.clear()
with patch.object(handler, "_provider_post", side_effect=fake_post), patch.object(
    handler, "_delete_service"
) as delete_service, patch.object(
    handler, "_delete_listener_rules"
) as delete_rules, patch.object(
    handler, "_delete_target_groups"
) as delete_tgs, patch.object(
    handler, "_delete_cognito_client"
) as delete_cognito, patch.object(
    handler, "_deregister_task_definitions"
) as deregister, patch.object(
    handler, "_delete_task_role", return_value="arn:aws:iam::1:role/replika-r1"
) as delete_role, patch.object(
    handler, "_delete_slack_auth_secret"
) as delete_secret, patch.object(
    handler, "_delete_access_point"
) as delete_ap, patch.dict(
    "os.environ",
    {
        "CALLBACK_URL": "https://control/internal/replika/provisioning",
        "DATABASE_BROKER_URL": "https://control/internal/replika/database",
    },
    clear=False,
), patch.object(handler.boto3, "client", side_effect=_FakeClients().client):
    deleted = handler._delete_replika(
        replika_id="r1",
        owner_id="owner-1",
        username="alice",
    )

_assert(deleted["status"] == "deleted", "delete returns deleted")
_assert(delete_service.called, "delete stops ECS service")
_assert(delete_rules.called, "delete removes ALB rules")
_assert(delete_tgs.called, "delete removes target groups")
_assert(delete_cognito.called, "delete removes Cognito client")
_assert(deregister.called, "delete deregisters task definitions")
_assert(delete_role.called, "delete removes IAM role")
_assert(delete_secret.called, "delete removes Slack auth secret")
_assert(delete_ap.called, "delete removes S3 Files access point")
_assert(any(payload.get("action") == "delete" for _, payload in posts), "DB delete broker")
_assert(posts[-1][1]["status"] == "deleted", "delete callbacks deleted")

# Missing resources are ignored.
_assert(
    handler._ignore_missing(
        _ClientError("ServiceNotFoundException"), "ServiceNotFoundException"
    ),
    "missing service is ignored",
)
_assert(
    not handler._ignore_missing(_ClientError("AccessDenied"), "ServiceNotFoundException"),
    "other errors are not ignored",
)

# Retrying deletion after ECS has already made the service inactive is harmless.
class _InactiveECS:
    def update_service(self, **_kwargs):
        raise handler.ClientError(
            {"Error": {"Code": "ServiceNotActiveException", "Message": "inactive"}},
            "UpdateService",
        )


with patch.dict("os.environ", {"ECS_CLUSTER": "cluster"}, clear=False):
    handler._delete_service(_InactiveECS(), "r1")


# S3 Files deletion accepts only the access point ID.
class _AccessPointPaginator:
    def paginate(self, **kwargs):
        _assert(kwargs == {"fileSystemId": "fs-1"}, "list filters by file system")
        return [
            {
                "accessPoints": [
                    {
                        "accessPointId": "ap-1",
                        "rootDirectory": {"path": "/tenants/r1"},
                    }
                ]
            }
        ]


class _S3Files:
    def __init__(self):
        self.delete_kwargs = None

    def get_paginator(self, name):
        _assert(name == "list_access_points", "uses access point paginator")
        return _AccessPointPaginator()

    def delete_access_point(self, **kwargs):
        self.delete_kwargs = kwargs


s3files = _S3Files()
with patch.dict("os.environ", {"S3FILES_FILE_SYSTEM_ID": "fs-1"}, clear=False):
    handler._delete_access_point(s3files, "r1")
_assert(
    s3files.delete_kwargs == {"accessPointId": "ap-1"},
    "delete passes only the access point ID",
)

# Handler dispatches by operation and accepts legacy owner_id-only payloads.
with patch.object(handler, "_create_replika", return_value={"status": "ready"}) as create, patch.object(
    handler, "_delete_replika", return_value={"status": "deleted"}
) as delete:
    handler.handler(
        {
            "operation": "create",
            "replika_id": "r1",
            "owner_id": "owner-1",
            "username": "alice",
            "replika_type": "individual",
        },
        None,
    )
    create.assert_called_once()
    handler.handler(
        {
            "operation": "delete",
            "owner_id": "legacy-owner",
            "username": "alice",
        },
        None,
    )
    _assert(delete.call_args.kwargs["replika_id"] == "legacy-owner", "legacy id fallback")

print("Replika provisioner delete checks passed.")

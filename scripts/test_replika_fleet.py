"""Unit checks for immutable fleet deployment and rollback inputs."""

from __future__ import annotations

import sys
import os
import importlib.util
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.deploy_replika_fleet import deploy  # noqa: E402


class _Paginator:
    def paginate(self, **_kwargs):
        return [
            {
                "serviceArns": [
                    "runtime",
                    "legacy-runtime",
                    "control",
                    "unmanaged",
                ]
            }
        ]


class _Waiter:
    def __init__(self, calls):
        self.calls = calls

    def wait(self, **_kwargs):
        self.calls.append(_kwargs)
        return None


class _ECS:
    def __init__(self):
        self.registered = None
        self.registrations = []
        self.updates = []
        self.tags = []
        self.wait_calls = []
        self.paginator_calls = 0

    def get_paginator(self, name):
        assert name == "list_services"
        self.paginator_calls += 1
        return _Paginator()

    def describe_services(self, **kwargs):
        services = {
            "runtime": {
                "serviceArn": "runtime",
                "taskDefinition": "runtime:1",
                "tags": [
                    {"key": "ReplikaManaged", "value": "true"},
                    {"key": "ReplikaPlane", "value": "runtime"},
                    {"key": "ReplikaTenant", "value": "tenant-a"},
                ],
            },
            "legacy-runtime": {
                "serviceArn": "legacy-runtime",
                "taskDefinition": "legacy:1",
                "tags": [
                    {"key": "ReplikaManaged", "value": "true"},
                    {"key": "ReplikaTenant", "value": "tenant-legacy"},
                ],
            },
            "control": {
                "serviceArn": "control",
                "taskDefinition": "control:1",
                "status": "ACTIVE",
                "tags": [
                    {"key": "ReplikaManaged", "value": "true"},
                    {"key": "ReplikaPlane", "value": "control"},
                ],
            },
            "unmanaged": {
                "serviceArn": "unmanaged",
                "taskDefinition": "other:1",
                "tags": [],
            },
        }
        requested = kwargs["services"]
        return {"services": [services[name] for name in requested]}

    def describe_task_definition(self, **_kwargs):
        return {
            "taskDefinition": {
                "family": "replika-a",
                "taskRoleArn": "role",
                "executionRoleArn": "execution",
                "networkMode": "awsvpc",
                "requiresCompatibilities": ["FARGATE"],
                "cpu": "1024",
                "memory": "2048",
                "containerDefinitions": [
                    {"name": "clyra", "image": "old-image"},
                ],
                "volumes": [
                    {
                        "name": "state",
                        "s3filesVolumeConfiguration": {
                            "fileSystemArn": "fs",
                            "accessPointArn": "ap-tenant-a",
                        },
                    }
                ],
            },
            "tags": [{"key": "ReplikaTenant", "value": "tenant-a"}],
        }

    def register_task_definition(self, **kwargs):
        self.registered = kwargs
        self.registrations.append(kwargs)
        return {
            "taskDefinition": {
                "taskDefinitionArn": f"task:{len(self.registrations) + 1}"
            }
        }

    def update_service(self, **kwargs):
        self.updates.append(kwargs)

    def tag_resource(self, **kwargs):
        self.tags.append(kwargs)

    def get_waiter(self, name):
        assert name == "services_stable"
        return _Waiter(self.wait_calls)


ecs = _ECS()
with patch("scripts.deploy_replika_fleet.boto3.client", return_value=ecs):
    result = deploy("cluster", "immutable-image", "clyra", "release-1")

assert result["managed_services"] == 2
assert result["results"][0]["status"] == "ready"
assert ecs.registrations[0]["containerDefinitions"][0]["image"] == "immutable-image"
assert (
    ecs.registrations[0]["volumes"][0]["s3filesVolumeConfiguration"]["accessPointArn"]
    == "ap-tenant-a"
), "fleet rollout must preserve the tenant access point"
assert ecs.updates[0]["taskDefinition"] == "task:2"
assert ecs.wait_calls[0]["WaiterConfig"]["MaxAttempts"] == 80
assert any(
    tag["key"] == "ReplikaRollout" and tag["value"] == "ready"
    for call in ecs.tags
    for tag in call["tags"]
)


class _EmptyTagECS(_ECS):
    def describe_task_definition(self, **kwargs):
        response = super().describe_task_definition(**kwargs)
        response["tags"] = []
        return response


empty_tag_ecs = _EmptyTagECS()
with patch("scripts.deploy_replika_fleet.boto3.client", return_value=empty_tag_ecs):
    empty_tag_result = deploy("cluster", "immutable-image", "clyra", "release-2")
assert empty_tag_result["results"][0]["status"] == "ready"
assert all(
    "tags" not in request for request in empty_tag_ecs.registrations
), "ECS rejects an explicitly empty tag list"

base_ecs = _ECS()
with patch("scripts.deploy_replika_fleet.boto3.client", return_value=base_ecs):
    base_result = deploy(
        "cluster",
        "runtime-image",
        "clyra",
        "release-3",
        base_task_definition="runtime-base",
    )
assert base_result["runtime_base_task_definition"] == "task:2"
assert len(base_ecs.registrations) == 3
assert base_ecs.registrations[0]["containerDefinitions"][0]["image"] == "runtime-image"

control_ecs = _ECS()
with patch("scripts.deploy_replika_fleet.boto3.client", return_value=control_ecs):
    control_result = deploy(
        "cluster",
        "control-image",
        "clyra",
        "release-4",
        plane="control",
        service="control",
    )
assert control_result["managed_services"] == 1
assert control_result["plane"] == "control"
assert control_ecs.paginator_calls == 0, "control deploy must never enumerate tenants"
assert control_ecs.updates[0]["service"] == "control"


class _FailOnceWaiter(_Waiter):
    def wait(self, **kwargs):
        super().wait(**kwargs)
        if len(self.calls) == 1:
            raise RuntimeError("deployment failed")


class _RollbackECS(_ECS):
    def describe_services(self, **kwargs):
        response = super().describe_services(**kwargs)
        response["services"] = [
            service
            for service in response["services"]
            if service["serviceArn"] == "runtime"
        ]
        return response

    def get_waiter(self, name):
        assert name == "services_stable"
        return _FailOnceWaiter(self.wait_calls)


rollback_ecs = _RollbackECS()
with patch("scripts.deploy_replika_fleet.boto3.client", return_value=rollback_ecs):
    rollback_result = deploy("cluster", "bad-image", "clyra", "release-5")
assert rollback_result["results"][0]["status"] == "rolled-back"
assert [update["taskDefinition"] for update in rollback_ecs.updates] == [
    "task:2",
    "runtime:1",
]
assert any(
    tag["key"] == "ReplikaRollout" and tag["value"] == "rolled-back"
    for call in rollback_ecs.tags
    for tag in call["tags"]
)

print("Replika fleet deployment checks passed.")

spec = importlib.util.spec_from_file_location(
    "replika_provisioner",
    ROOT / "infra" / "provisioner" / "handler.py",
)
provisioner = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(provisioner)


class _AccessPointPages:
    def paginate(self, **kwargs):
        assert kwargs == {"fileSystemId": "fs"}
        return [
            {
                "accessPoints": [
                    {
                        "accessPointArn": "tenant-ap-arn",
                        "accessPointId": "tenant-ap",
                        "rootDirectory": {"path": "/tenants/tenant-a"},
                    }
                ]
            }
        ]


class _ExistingAccessPoint:
    def create_access_point(self, **_kwargs):
        raise provisioner.ClientError(
            {"Error": {"Code": "ConflictException", "Message": "already exists"}},
            "CreateAccessPoint",
        )

    def get_paginator(self, name):
        assert name == "list_access_points"
        return _AccessPointPages()


os.environ["S3FILES_FILE_SYSTEM_ID"] = "fs"
assert provisioner._create_access_point(_ExistingAccessPoint(), "tenant-a") == (
    "tenant-ap-arn",
    "tenant-ap",
), "provisioning retries must reuse the tenant access point"


class _ProvisioningECS:
    def __init__(self):
        self.request = None

    def describe_task_definition(self, **_kwargs):
        return {
            "taskDefinition": {
                "family": "base",
                "executionRoleArn": "execution",
                "taskRoleArn": "base-role",
                "networkMode": "awsvpc",
                "requiresCompatibilities": ["FARGATE"],
                "cpu": "1024",
                "memory": "2048",
                "volumes": [
                    {
                        "name": "state",
                        "s3filesVolumeConfiguration": {
                            "fileSystemArn": "fs",
                            "accessPointArn": "base-ap",
                        },
                    }
                ],
                "containerDefinitions": [
                    {"name": "appconfig", "image": "appconfig"},
                    {
                        "name": "clyra",
                        "image": "image",
                        "portMappings": [
                            {"containerPort": 8080, "hostPort": 8080, "protocol": "tcp"}
                        ],
                        "environment": [{"name": "APPCONFIG_REQUIRED", "value": "true"}],
                        "secrets": [
                            {"name": "TOWER_SECRET_KEY", "valueFrom": "session"},
                            {"name": "GEMINI_API_KEY", "valueFrom": "platform-key"},
                        ],
                        "dependsOn": [
                            {"containerName": "appconfig", "condition": "START"}
                        ],
                    },
                ],
            }
        }

    def register_task_definition(self, **kwargs):
        self.request = kwargs
        return {"taskDefinition": {"taskDefinitionArn": "tenant-task:1"}}


os.environ.update(
    {
        "BASE_TASK_DEFINITION": "base",
        "CONTAINER_NAME": "clyra",
        "PRODUCT_DOMAIN": "replika.example",
        "RUNTIME_SECRET_NAMES": '["TOWER_SECRET_KEY"]',
    }
)
provisioning_ecs = _ProvisioningECS()
provisioner._task_definition(
    provisioning_ecs,
    "tenant-a",
    "owner-a",
    "alice",
    "organization",
    "tenant-ap",
    "tenant-role",
    {
        "mongo_uri": "mongodb://docdb/?authMechanism=MONGODB-AWS",
        "mongo_db": "replika_tenant-a",
    },
    "arn:aws:secretsmanager:test:slack-auth",
)
request = provisioning_ecs.request
assert request is not None
assert [container["name"] for container in request["containerDefinitions"]] == ["clyra"]
runtime = request["containerDefinitions"][0]
environment = {row["name"]: row["value"] for row in runtime["environment"]}
assert environment["REPLIKA_TENANT_ID"] == "tenant-a"
assert environment["REPLIKA_OWNER_ID"] == "owner-a"
assert environment["REPLIKA_TYPE"] == "organization"
assert environment["REPLIKA_MANAGED_RUNTIME"] == "true"
assert environment["PHONE_BRIDGE_ENABLED"] == "1"
assert environment["PHONE_BRIDGE_WS_HOST"] == "0.0.0.0"
assert environment["PHONE_BRIDGE_WS_PORT"] == "8765"
assert environment["APPCONFIG_REQUIRED"] == "false"
assert environment["MONGO_DB"] == "replika_tenant-a"
assert runtime["secrets"] == [
    {"name": "TOWER_SECRET_KEY", "valueFrom": "session"},
    {
        "name": "SLACK_TENANT_AUTH_SECRET",
        "valueFrom": "arn:aws:secretsmanager:test:slack-auth",
    },
]
assert runtime["dependsOn"] == []
assert runtime["portMappings"] == [
    {"containerPort": 8080, "hostPort": 8080, "protocol": "tcp"},
    {"containerPort": 8765, "hostPort": 8765, "protocol": "tcp"},
]
assert {"key": "ReplikaPlane", "value": "runtime"} in request["tags"]
assert (
    request["volumes"][0]["s3filesVolumeConfiguration"]["accessPointArn"]
    == "tenant-ap"
)


class _InvalidServiceECS:
    def __init__(self):
        self.updated = False

    def create_service(self, **_kwargs):
        raise provisioner.ClientError(
            {
                "Error": {
                    "Code": "InvalidParameterException",
                    "Message": "invalid service definition",
                }
            },
            "CreateService",
        )

    def update_service(self, **_kwargs):
        self.updated = True


invalid_service_ecs = _InvalidServiceECS()
os.environ.update(
    {
        "ECS_CLUSTER": "cluster",
        "PRIVATE_SUBNET_IDS": '["subnet-a"]',
        "TASK_SECURITY_GROUP_ID": "sg-1",
    }
)
try:
    provisioner._service(
        invalid_service_ecs,
        "tenant-a",
        "task:1",
        "target-group",
        "phone-target-group",
        "v0",
    )
except provisioner.ClientError as exc:
    assert exc.response["Error"]["Code"] == "InvalidParameterException"
else:
    raise AssertionError("invalid create-service errors must be preserved")
assert not invalid_service_ecs.updated, "invalid creates must not become updates"


class _ExistingServiceECS(_InvalidServiceECS):
    def __init__(self):
        super().__init__()
        self.wait_calls = []

    def create_service(self, **_kwargs):
        raise provisioner.ClientError(
            {
                "Error": {
                    "Code": "InvalidParameterException",
                    "Message": "Creation of service was not idempotent.",
                }
            },
            "CreateService",
        )

    def get_waiter(self, name):
        assert name == "services_stable"
        return _Waiter(self.wait_calls)


existing_service_ecs = _ExistingServiceECS()
provisioner._service(
    existing_service_ecs,
    "tenant-a",
    "task:2",
    "target-group",
    "phone-target-group",
    "v1",
)
assert existing_service_ecs.updated, "existing services must be updated on retry"
assert existing_service_ecs.wait_calls, "updated services must become stable"


print("Replika isolated runtime checks passed.")

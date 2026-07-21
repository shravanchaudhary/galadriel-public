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
        return [{"serviceArns": ["managed", "unmanaged"]}]


class _Waiter:
    def wait(self, **_kwargs):
        return None


class _ECS:
    def __init__(self):
        self.registered = None
        self.updates = []
        self.tags = []

    def get_paginator(self, name):
        assert name == "list_services"
        return _Paginator()

    def describe_services(self, **_kwargs):
        return {
            "services": [
                {
                    "serviceArn": "managed",
                    "taskDefinition": "task:1",
                    "tags": [{"key": "ReplikaManaged", "value": "true"}],
                },
                {
                    "serviceArn": "unmanaged",
                    "taskDefinition": "other:1",
                    "tags": [],
                },
            ]
        }

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
        return {"taskDefinition": {"taskDefinitionArn": "task:2"}}

    def update_service(self, **kwargs):
        self.updates.append(kwargs)

    def tag_resource(self, **kwargs):
        self.tags.append(kwargs)

    def get_waiter(self, name):
        assert name == "services_stable"
        return _Waiter()


ecs = _ECS()
with patch("scripts.deploy_replika_fleet.boto3.client", return_value=ecs):
    result = deploy("cluster", "immutable-image", "clyra", "release-1")

assert result["managed_services"] == 1
assert result["results"][0]["status"] == "ready"
assert ecs.registered["containerDefinitions"][0]["image"] == "immutable-image"
assert (
    ecs.registered["volumes"][0]["s3filesVolumeConfiguration"]["accessPointArn"]
    == "ap-tenant-a"
), "fleet rollout must preserve the tenant access point"
assert ecs.updates[0]["taskDefinition"] == "task:2"
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
assert "tags" not in empty_tag_ecs.registered, "ECS rejects an explicitly empty tag list"

print("Replika fleet deployment checks passed.")

spec = importlib.util.spec_from_file_location(
    "replika_provisioner",
    ROOT / "infra" / "provisioner" / "handler.py",
)
provisioner = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(provisioner)


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
    "alice",
    "tenant-ap",
    "tenant-role",
    {
        "mongo_uri": "mongodb://docdb/?authMechanism=MONGODB-AWS",
        "mongo_db": "replika_tenant-a",
    },
)
request = provisioning_ecs.request
assert request is not None
assert [container["name"] for container in request["containerDefinitions"]] == ["clyra"]
runtime = request["containerDefinitions"][0]
environment = {row["name"]: row["value"] for row in runtime["environment"]}
assert environment["REPLIKA_TENANT_ID"] == "tenant-a"
assert environment["REPLIKA_MANAGED_RUNTIME"] == "true"
assert environment["APPCONFIG_REQUIRED"] == "false"
assert environment["MONGO_DB"] == "replika_tenant-a"
assert runtime["secrets"] == [{"name": "TOWER_SECRET_KEY", "valueFrom": "session"}]
assert runtime["dependsOn"] == []
assert (
    request["volumes"][0]["s3filesVolumeConfiguration"]["accessPointArn"]
    == "tenant-ap"
)

print("Replika isolated runtime checks passed.")

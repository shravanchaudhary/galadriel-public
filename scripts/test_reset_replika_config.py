#!/usr/bin/env python3
"""Regression checks for the Replika config-reset ECS task payload."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra" / "provisioner"))

import handler  # noqa: E402
from handler import RESET_CONFIG_PAYLOAD  # noqa: E402


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _run_payload(defaults_root: Path, storage_root: Path) -> dict:
    env = dict(os.environ)
    env["GALADRIEL_DEFAULTS_ROOT"] = str(defaults_root)
    env["GALADRIEL_STORAGE_ROOT"] = str(storage_root)
    result = subprocess.run(
        [sys.executable, "-c", RESET_CONFIG_PAYLOAD],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_overwrites_edited_files_and_fills_in_missing_ones() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        defaults = root / "defaults"
        storage = root / "storage"
        _write(defaults / "config" / "SOUL.md", "latest soul")
        _write(defaults / "config" / "MEMORY.md", "latest memory")
        _write(storage / "config" / "SOUL.md", "tenant-edited soul")

        output = _run_payload(defaults, storage)

        assert sorted(output["changed_files"]) == ["MEMORY.md", "SOUL.md"]
        assert (storage / "config" / "SOUL.md").read_text() == "latest soul"
        assert (storage / "config" / "MEMORY.md").read_text() == "latest memory"


def test_preserves_tenant_only_runtime_files_and_other_dirs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        defaults = root / "defaults"
        storage = root / "storage"
        _write(defaults / "config" / "SOUL.md", "latest soul")
        _write(storage / "config" / "SOUL.md", "latest soul")
        _write(storage / "config" / "scheduler_state.json", "tenant scheduler state")
        _write(storage / "memory" / "2026-07-28.md", "tenant memory log")

        output = _run_payload(defaults, storage)

        assert output["changed_files"] == []
        assert (
            storage / "config" / "scheduler_state.json"
        ).read_text() == "tenant scheduler state"
        assert (storage / "memory" / "2026-07-28.md").read_text() == "tenant memory log"


def test_idempotent_when_already_up_to_date() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        defaults = root / "defaults"
        storage = root / "storage"
        _write(defaults / "config" / "SOUL.md", "latest soul")
        _write(storage / "config" / "SOUL.md", "latest soul")

        assert _run_payload(defaults, storage)["changed_files"] == []


def test_bind_state_volume_restores_s3files_config_omitted_by_describe() -> None:
    os.environ["S3FILES_FILE_SYSTEM_ARN"] = "arn:aws:s3files:test:file-system/fs"
    volumes = [{"name": "state"}]  # describe_task_definition shape
    handler._bind_state_volume(volumes, "tenant-ap-arn")
    assert volumes[0]["s3filesVolumeConfiguration"] == {
        "fileSystemArn": "arn:aws:s3files:test:file-system/fs",
        "accessPointArn": "tenant-ap-arn",
        "rootDirectory": "/",
    }
    assert volumes[0]["configuredAtLaunch"] is False


def test_reset_registers_task_def_with_tenant_s3files_volume() -> None:
    """Reset must not re-register the describe payload's bare {name: state} volume."""
    os.environ.update(
        {
            "ECS_CLUSTER": "cluster",
            "CONTAINER_NAME": "clyra",
            "S3FILES_FILE_SYSTEM_ID": "fs",
            "S3FILES_FILE_SYSTEM_ARN": "arn:aws:s3files:test:file-system/fs",
        }
    )
    replika_id = "tenant-a"
    registered = {}

    class _ECS:
        def describe_services(self, **_kwargs):
            return {
                "services": [
                    {
                        "status": "ACTIVE",
                        "taskDefinition": "replika-tenant:1",
                        "networkConfiguration": {"awsvpcConfiguration": {}},
                    }
                ]
            }

        def describe_task_definition(self, **_kwargs):
            return {
                "taskDefinition": {
                    "family": "replika-tenant",
                    "executionRoleArn": "execution",
                    "taskRoleArn": "tenant-role",
                    "networkMode": "awsvpc",
                    "requiresCompatibilities": ["FARGATE"],
                    "cpu": "1024",
                    "memory": "2048",
                    # Real AWS describe omits s3filesVolumeConfiguration.
                    "volumes": [{"name": "state"}],
                    "containerDefinitions": [
                        {
                            "name": "clyra",
                            "image": "image",
                            "mountPoints": [
                                {
                                    "sourceVolume": "state",
                                    "containerPath": "/mnt/efs",
                                    "readOnly": False,
                                }
                            ],
                        }
                    ],
                }
            }

        def register_task_definition(self, **kwargs):
            registered.update(kwargs)
            return {
                "taskDefinition": {
                    "taskDefinitionArn": "arn:aws:ecs:test:task-definition/reset:1"
                }
            }

        def run_task(self, **_kwargs):
            return {"tasks": [{"taskArn": "task/1"}], "failures": []}

        def get_waiter(self, name):
            assert name == "tasks_stopped"
            waiter = MagicMock()
            waiter.wait = MagicMock()
            return waiter

        def describe_tasks(self, **_kwargs):
            return {
                "tasks": [
                    {
                        "containers": [{"exitCode": 0}],
                        "stoppedReason": "",
                    }
                ]
            }

        def deregister_task_definition(self, **_kwargs):
            return {}

    class _S3Files:
        def create_access_point(self, **_kwargs):
            raise handler.ClientError(
                {
                    "Error": {
                        "Code": "ConflictException",
                        "Message": "already exists",
                    }
                },
                "CreateAccessPoint",
            )

        def get_paginator(self, name):
            assert name == "list_access_points"

            class _Pages:
                def paginate(self, **_kwargs):
                    yield {
                        "accessPoints": [
                            {
                                "accessPointArn": "tenant-ap-arn",
                                "accessPointId": "tenant-ap",
                                "rootDirectory": {"path": f"/tenants/{replika_id}"},
                            }
                        ]
                    }

            return _Pages()

    def _client(name, **_kwargs):
        if name == "ecs":
            return _ECS()
        if name == "s3files":
            return _S3Files()
        raise AssertionError(f"unexpected client {name}")

    with patch.object(handler.boto3, "client", side_effect=_client):
        result = handler._reset_replika_config(replika_id=replika_id)

    assert result == {"status": "reset"}
    assert registered["volumes"][0]["s3filesVolumeConfiguration"] == {
        "fileSystemArn": "arn:aws:s3files:test:file-system/fs",
        "accessPointArn": "tenant-ap-arn",
        "rootDirectory": "/",
    }
    assert registered["containerDefinitions"][0]["entryPoint"] == ["python", "-c"]


def main() -> int:
    tests = (
        test_overwrites_edited_files_and_fills_in_missing_ones,
        test_preserves_tenant_only_runtime_files_and_other_dirs,
        test_idempotent_when_already_up_to_date,
        test_bind_state_volume_restores_s3files_config_omitted_by_describe,
        test_reset_registers_task_def_with_tenant_s3files_volume,
    )
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print(f"{len(tests)}/{len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

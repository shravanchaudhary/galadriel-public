#!/usr/bin/env python3
"""Migrate a quiesced legacy runtime into a managed Replika tenant."""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
import time
from urllib.parse import unquote

import boto3


REGISTERABLE_TASK_FIELDS = {
    "family",
    "taskRoleArn",
    "executionRoleArn",
    "networkMode",
    "containerDefinitions",
    "volumes",
    "placementConstraints",
    "requiresCompatibilities",
    "cpu",
    "memory",
    "pidMode",
    "ipcMode",
    "proxyConfiguration",
    "inferenceAccelerators",
    "ephemeralStorage",
    "runtimePlatform",
    "enableFaultInjection",
}

PAYLOAD = r"""
import hashlib
import json
import os
import shutil
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

from pymongo import MongoClient, ReplaceOne

sys.path.insert(0, "/app/scripts")
from clyra_storage_manifest import build_manifest, comparison_view

source_root = Path(os.environ["SOURCE_ROOT"])
target_root = Path(os.environ["TARGET_ROOT"])
source_db_name = os.environ["SOURCE_DB"]
target_db_name = os.environ["TARGET_DB"]
target_tenant_id = os.environ["TARGET_TENANT_ID"]
excluded = set(json.loads(os.environ["EXCLUDED_COLLECTIONS"]))
skip_file_copy = os.environ.get("SKIP_FILE_COPY") == "true"

if skip_file_copy:
    print(json.dumps({"status": "files-skipped"}, sort_keys=True))
else:
    source_manifest = build_manifest(source_root, exclude={".clyra-migration.json"})
    for source_path in sorted(source_root.rglob("*")):
        target_path = target_root / source_path.relative_to(source_root)
        info = source_path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if source_path.is_symlink():
            if target_path.exists() or target_path.is_symlink():
                if target_path.is_dir() and not target_path.is_symlink():
                    shutil.rmtree(target_path)
                else:
                    target_path.unlink()
            target_path.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(os.readlink(source_path), target_path)
        elif source_path.is_dir():
            target_path.mkdir(parents=True, exist_ok=True)
            target_path.chmod(mode)
        elif source_path.is_file():
            if target_path.exists() or target_path.is_symlink():
                if target_path.is_dir() and not target_path.is_symlink():
                    shutil.rmtree(target_path)
                else:
                    target_path.unlink()
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, target_path)
            target_path.chmod(mode)

    target_manifest = build_manifest(target_root, exclude={".clyra-migration.json"})
    source_view = [
        {key: value for key, value in entry.items() if key != "mode"}
        for entry in comparison_view(source_manifest)
    ]
    target_by_path = {
        entry["path"]: {key: value for key, value in entry.items() if key != "mode"}
        for entry in comparison_view(target_manifest)
        if entry["path"] in {item["path"] for item in source_view}
    }
    mismatches = [
        {
            "path": item["path"],
            "source": item,
            "target": target_by_path.get(item["path"]),
        }
        for item in source_view
        if target_by_path.get(item["path"]) != item
    ]
    if mismatches:
        print(json.dumps({"status": "file-mismatch", "mismatches": mismatches[:100]}, sort_keys=True))
        raise RuntimeError("portable file manifest verification failed")

    marker = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "source_manifest_sha256": source_manifest["manifest_sha256"],
        "source_files": source_manifest["files"],
        "source_directories": source_manifest["directories"],
        "source_symlinks": source_manifest["symlinks"],
        "source_bytes": source_manifest["bytes"],
    }
    (target_root / ".clyra-migration.json").write_text(
        json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"status": "files-copied", **marker}, sort_keys=True))

client = MongoClient(os.environ["MONGO_URI"])
source = client[source_db_name]
target = client[target_db_name]
stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
backup_name = "replika_backup_" + hashlib.sha256(target_db_name.encode()).hexdigest()[:10] + "_" + stamp
backup = client[backup_name]

backup_counts = {}
for name in sorted(target.list_collection_names()):
    if name.startswith("system."):
        continue
    documents = list(target[name].find({}))
    if documents:
        backup[name].insert_many(documents, ordered=False)
    backup_counts[name] = len(documents)

copied = {}
for name in sorted(source.list_collection_names()):
    if name.startswith("system.") or name in excluded:
        continue
    if name == "tower_settings":
        continue
    collection = source[name]
    operations = []
    count = 0
    for document in collection.find({}):
        if name == "llm_calls":
            document["tenant_id"] = target_tenant_id
        operations.append(ReplaceOne({"_id": document["_id"]}, document, upsert=True))
        if len(operations) == 500:
            target[name].bulk_write(operations, ordered=False)
            count += len(operations)
            operations = []
    if operations:
        target[name].bulk_write(operations, ordered=False)
        count += len(operations)
    copied[name] = count

settings_by_name = {}
for document in source["tower_settings"].find({}):
    legacy_id = str(document["_id"])
    setting_name = legacy_id.split(":", 1)[-1]
    if setting_name == "agent_model":
        setting_name = "main_model"
    normalized = dict(document)
    normalized["_id"] = f"{target_tenant_id}:{setting_name}"
    normalized["tenant_id"] = target_tenant_id
    if setting_name not in settings_by_name or legacy_id != "agent_model":
        settings_by_name[setting_name] = normalized

target["tower_settings"].delete_many({})
if settings_by_name:
    target["tower_settings"].insert_many(list(settings_by_name.values()), ordered=False)
copied["tower_settings"] = len(settings_by_name)

cost_normalized = target["llm_calls"].update_many(
    {"tenant_id": {"$ne": target_tenant_id}},
    {"$set": {"tenant_id": target_tenant_id}},
).modified_count

verification = {}
for name, source_count in copied.items():
    target_count = target[name].count_documents({})
    if target_count < source_count:
        raise RuntimeError(
            f"{name}: target count {target_count} is below source count {source_count}"
        )
    verification[name] = {"source": source_count, "target": target_count}

print(
    json.dumps(
        {
            "status": "ok",
            "backup_database": backup_name,
            "backup_counts": backup_counts,
            "collections": verification,
            "normalization": {
                "cost_rows_updated": cost_normalized,
                "tower_settings": sorted(settings_by_name),
                "tenant_id": target_tenant_id,
            },
        },
        sort_keys=True,
    )
)
"""


def _role_name(arn: str) -> str:
    return arn.rsplit("/", 1)[-1]


def _migration_policy(policy: dict, access_points: list[str]) -> dict:
    updated = copy.deepcopy(policy)
    for statement in updated.get("Statement", []):
        actions = statement.get("Action", [])
        if isinstance(actions, str):
            actions = [actions]
        if "s3files:ClientMount" not in actions:
            continue
        condition = statement.setdefault("Condition", {}).setdefault("StringEquals", {})
        condition["s3files:AccessPointArn"] = access_points
        return updated
    raise RuntimeError("tenant role has no S3 Files mount policy")


def _wait_for_task(ecs, cluster: str, task_arn: str) -> dict:
    ecs.get_waiter("tasks_stopped").wait(
        cluster=cluster,
        tasks=[task_arn],
        WaiterConfig={"Delay": 15, "MaxAttempts": 240},
    )
    return ecs.describe_tasks(cluster=cluster, tasks=[task_arn])["tasks"][0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cluster", required=True)
    parser.add_argument("--source-task-definition", required=True)
    parser.add_argument("--tenant-service", required=True)
    parser.add_argument("--mongo-secret-arn", required=True)
    parser.add_argument("--source-db", required=True)
    parser.add_argument("--target-db", required=True)
    parser.add_argument("--target-tenant-id", required=True)
    parser.add_argument(
        "--exclude-collection",
        action="append",
        default=["replikas", "provider_credentials"],
    )
    parser.add_argument("--restore-desired-count", type=int)
    parser.add_argument("--skip-file-copy", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    ecs = boto3.client("ecs")
    iam = boto3.client("iam")
    source_task = ecs.describe_task_definition(
        taskDefinition=args.source_task_definition
    )["taskDefinition"]
    service = ecs.describe_services(
        cluster=args.cluster, services=[args.tenant_service]
    )["services"][0]
    if not service:
        raise RuntimeError("tenant service was not found")
    tenant_task = ecs.describe_task_definition(
        taskDefinition=service["taskDefinition"]
    )["taskDefinition"]
    source_volume = source_task["volumes"][0]["s3filesVolumeConfiguration"]
    target_volume = tenant_task["volumes"][0]["s3filesVolumeConfiguration"]
    source_ap = source_volume["accessPointArn"]
    target_ap = target_volume["accessPointArn"]
    task_role_arn = tenant_task["taskRoleArn"]

    summary = {
        "tenant_service": args.tenant_service,
        "tenant_task_definition": tenant_task["taskDefinitionArn"],
        "source_access_point": source_ap,
        "target_access_point": target_ap,
        "source_database": args.source_db,
        "target_database": args.target_db,
        "target_tenant_id": args.target_tenant_id,
        "excluded_collections": sorted(set(args.exclude_collection)),
    }
    print(json.dumps({"status": "planned", **summary}, indent=2, sort_keys=True))
    if not args.apply:
        return 0

    original_desired = (
        args.restore_desired_count
        if args.restore_desired_count is not None
        else service["desiredCount"]
    )
    role_name = _role_name(task_role_arn)
    policy_names = iam.list_role_policies(RoleName=role_name)["PolicyNames"]
    if len(policy_names) != 1:
        raise RuntimeError("expected exactly one tenant inline role policy")
    policy_name = policy_names[0]
    original_policy = iam.get_role_policy(
        RoleName=role_name, PolicyName=policy_name
    )["PolicyDocument"]
    if isinstance(original_policy, str):
        original_policy = json.loads(unquote(original_policy))

    migration_task_definition = None
    migration_succeeded = False
    ecs.update_service(
        cluster=args.cluster, service=args.tenant_service, desiredCount=0
    )
    ecs.get_waiter("services_stable").wait(
        cluster=args.cluster,
        services=[args.tenant_service],
        WaiterConfig={"Delay": 15, "MaxAttempts": 60},
    )
    try:
        iam.put_role_policy(
            RoleName=role_name,
            PolicyName=policy_name,
            PolicyDocument=json.dumps(
                _migration_policy(original_policy, [source_ap, target_ap])
            ),
        )
        time.sleep(10)

        base_container = next(
            item
            for item in tenant_task["containerDefinitions"]
            if item["name"] == "clyra"
        )
        container = {
            "name": "migration",
            "image": base_container["image"],
            "essential": True,
            "user": "1000",
            "entryPoint": ["python", "-c"],
            "command": [PAYLOAD],
            "environment": [
                {"name": "SOURCE_ROOT", "value": "/mnt/source"},
                {"name": "TARGET_ROOT", "value": "/mnt/target"},
                {"name": "SOURCE_DB", "value": args.source_db},
                {"name": "TARGET_DB", "value": args.target_db},
                {"name": "TARGET_TENANT_ID", "value": args.target_tenant_id},
                {
                    "name": "EXCLUDED_COLLECTIONS",
                    "value": json.dumps(sorted(set(args.exclude_collection))),
                },
                {
                    "name": "SKIP_FILE_COPY",
                    "value": str(args.skip_file_copy).lower(),
                },
            ],
            "secrets": [{"name": "MONGO_URI", "valueFrom": args.mongo_secret_arn}],
            "mountPoints": [
                {
                    "sourceVolume": "legacy-state",
                    "containerPath": "/mnt/source",
                    "readOnly": True,
                },
                {
                    "sourceVolume": "tenant-state",
                    "containerPath": "/mnt/target",
                    "readOnly": False,
                },
            ],
            "readonlyRootFilesystem": True,
            "linuxParameters": {"capabilities": {"drop": ["ALL"]}},
            "logConfiguration": copy.deepcopy(base_container["logConfiguration"]),
        }
        request = {
            key: copy.deepcopy(value)
            for key, value in tenant_task.items()
            if key in REGISTERABLE_TASK_FIELDS
        }
        request["family"] = "replika-legacy-migration"
        request["containerDefinitions"] = [container]
        request["volumes"] = [
            {
                "name": "legacy-state",
                "s3filesVolumeConfiguration": copy.deepcopy(source_volume),
            },
            {
                "name": "tenant-state",
                "s3filesVolumeConfiguration": copy.deepcopy(target_volume),
            },
        ]
        migration_task_definition = ecs.register_task_definition(**request)[
            "taskDefinition"
        ]["taskDefinitionArn"]
        network = service["networkConfiguration"]
        response = ecs.run_task(
            cluster=args.cluster,
            taskDefinition=migration_task_definition,
            launchType="FARGATE",
            networkConfiguration=network,
            count=1,
            startedBy="legacy-replika-migration",
        )
        if response.get("failures"):
            raise RuntimeError(f"migration task failed to start: {response['failures']}")
        task = _wait_for_task(ecs, args.cluster, response["tasks"][0]["taskArn"])
        container_result = task["containers"][0]
        if container_result.get("exitCode") != 0:
            raise RuntimeError(
                f"migration task exited {container_result.get('exitCode')}: "
                f"{container_result.get('reason') or task.get('stoppedReason')}"
            )
        migration_succeeded = True
    finally:
        iam.put_role_policy(
            RoleName=role_name,
            PolicyName=policy_name,
            PolicyDocument=json.dumps(original_policy),
        )
        if migration_task_definition:
            ecs.deregister_task_definition(taskDefinition=migration_task_definition)
        ecs.update_service(
            cluster=args.cluster,
            service=args.tenant_service,
            desiredCount=original_desired,
            forceNewDeployment=True,
        )
        ecs.get_waiter("services_stable").wait(
            cluster=args.cluster,
            services=[args.tenant_service],
            WaiterConfig={"Delay": 15, "MaxAttempts": 80},
        )

    if migration_succeeded:
        print(json.dumps({"status": "complete", **summary}, sort_keys=True))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

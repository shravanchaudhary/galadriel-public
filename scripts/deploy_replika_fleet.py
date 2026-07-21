"""Roll an immutable image across provider-managed Replika ECS services."""

from __future__ import annotations

import argparse
import copy
import json
import sys

import boto3

MANAGED_TAG = "ReplikaManaged"
RELEASE_TAG = "ReplikaRelease"
ROLLOUT_TAG = "ReplikaRollout"
REGISTERABLE_FIELDS = {
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
    "tags",
    "pidMode",
    "ipcMode",
    "proxyConfiguration",
    "inferenceAccelerators",
    "ephemeralStorage",
    "runtimePlatform",
    "enableFaultInjection",
}


def _managed_services(ecs, cluster: str) -> list[dict]:
    arns: list[str] = []
    paginator = ecs.get_paginator("list_services")
    for page in paginator.paginate(cluster=cluster, launchType="FARGATE"):
        arns.extend(page.get("serviceArns", []))
    managed: list[dict] = []
    for offset in range(0, len(arns), 10):
        response = ecs.describe_services(
            cluster=cluster,
            services=arns[offset : offset + 10],
            include=["TAGS"],
        )
        for service in response.get("services", []):
            tags = {tag["key"]: tag["value"] for tag in service.get("tags", [])}
            if tags.get(MANAGED_TAG, "").lower() == "true":
                managed.append(service)
    return managed


def _next_task_definition(ecs, task_definition_arn: str, image: str, container: str) -> str:
    current = ecs.describe_task_definition(
        taskDefinition=task_definition_arn, include=["TAGS"]
    )
    definition = current["taskDefinition"]
    request = {
        key: copy.deepcopy(value)
        for key, value in definition.items()
        if key in REGISTERABLE_FIELDS
    }
    found = False
    for item in request["containerDefinitions"]:
        if item["name"] == container:
            item["image"] = image
            found = True
    if not found:
        raise RuntimeError(f"container {container!r} not found in {task_definition_arn}")
    if current.get("tags"):
        request["tags"] = current["tags"]
    return ecs.register_task_definition(**request)["taskDefinition"]["taskDefinitionArn"]


def deploy(cluster: str, image: str, container: str, release: str) -> dict:
    ecs = boto3.client("ecs")
    services = _managed_services(ecs, cluster)
    results = []
    for service in services:
        arn = service["serviceArn"]
        previous = service["taskDefinition"]
        ecs.tag_resource(
            resourceArn=arn,
            tags=[
                {"key": RELEASE_TAG, "value": release},
                {"key": ROLLOUT_TAG, "value": "upgrading"},
            ],
        )
        try:
            next_definition = _next_task_definition(ecs, previous, image, container)
            ecs.update_service(
                cluster=cluster,
                service=arn,
                taskDefinition=next_definition,
                forceNewDeployment=True,
            )
            ecs.get_waiter("services_stable").wait(cluster=cluster, services=[arn])
            ecs.tag_resource(
                resourceArn=arn,
                tags=[{"key": ROLLOUT_TAG, "value": "ready"}],
            )
            results.append({"service": arn, "status": "ready", "taskDefinition": next_definition})
        except Exception as exc:
            ecs.update_service(
                cluster=cluster,
                service=arn,
                taskDefinition=previous,
                forceNewDeployment=True,
            )
            ecs.tag_resource(
                resourceArn=arn,
                tags=[{"key": ROLLOUT_TAG, "value": "rolled-back"}],
            )
            results.append({"service": arn, "status": "rolled-back", "error": str(exc)})
    return {
        "release": release,
        "image": image,
        "managed_services": len(services),
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cluster", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--container", default="clyra")
    parser.add_argument("--release", required=True)
    args = parser.parse_args()
    summary = deploy(args.cluster, args.image, args.container, args.release)
    print(json.dumps(summary, indent=2))
    failed = [row for row in summary["results"] if row["status"] != "ready"]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

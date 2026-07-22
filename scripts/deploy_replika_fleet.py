"""Roll an immutable image across provider-managed Replika ECS services."""

from __future__ import annotations

import argparse
import copy
import json
import sys

import boto3

MANAGED_TAG = "ReplikaManaged"
PLANE_TAG = "ReplikaPlane"
TENANT_TAG = "ReplikaTenant"
RELEASE_TAG = "ReplikaRelease"
ROLLOUT_TAG = "ReplikaRollout"
WAITER_CONFIG = {"Delay": 15, "MaxAttempts": 80}
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


def _managed_services(ecs, cluster: str, plane: str) -> list[dict]:
    if plane != "runtime":
        raise ValueError(f"unsupported enumerated plane: {plane}")
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
            is_managed = tags.get(MANAGED_TAG, "").lower() == "true"
            is_runtime = (
                tags.get(PLANE_TAG) == "runtime" or bool(tags.get(TENANT_TAG))
            )
            if is_managed and is_runtime:
                managed.append(service)
    return managed


def _control_service(ecs, cluster: str, service_name: str) -> list[dict]:
    response = ecs.describe_services(
        cluster=cluster,
        services=[service_name],
        include=["TAGS"],
    )
    services = response.get("services", [])
    if len(services) != 1 or services[0].get("status") == "INACTIVE":
        raise RuntimeError(f"control service {service_name!r} was not found")
    return services


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


def deploy(
    cluster: str,
    image: str,
    container: str,
    release: str,
    *,
    plane: str = "runtime",
    service: str | None = None,
    base_task_definition: str | None = None,
) -> dict:
    ecs = boto3.client("ecs")
    if plane == "control":
        if not service:
            raise ValueError("control deployments require an explicit service")
        if base_task_definition:
            raise ValueError("control deployments cannot advance a runtime base")
        services = _control_service(ecs, cluster, service)
    elif plane == "runtime":
        if service:
            raise ValueError("runtime deployments select tenants by ownership tags")
        services = _managed_services(ecs, cluster, plane)
    else:
        raise ValueError(f"unsupported deployment plane: {plane}")

    runtime_base = None
    if base_task_definition:
        runtime_base = _next_task_definition(
            ecs, base_task_definition, image, container
        )

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
            ecs.get_waiter("services_stable").wait(
                cluster=cluster,
                services=[arn],
                WaiterConfig=WAITER_CONFIG,
            )
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
            rollback_error = None
            try:
                ecs.get_waiter("services_stable").wait(
                    cluster=cluster,
                    services=[arn],
                    WaiterConfig=WAITER_CONFIG,
                )
            except Exception as rollback_exc:
                rollback_error = str(rollback_exc)
            ecs.tag_resource(
                resourceArn=arn,
                tags=[{"key": ROLLOUT_TAG, "value": "rolled-back"}],
            )
            result = {"service": arn, "status": "rolled-back", "error": str(exc)}
            if rollback_error:
                result["rollbackError"] = rollback_error
            results.append(result)
    return {
        "release": release,
        "image": image,
        "plane": plane,
        "runtime_base_task_definition": runtime_base,
        "managed_services": len(services),
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cluster", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--container", default="clyra")
    parser.add_argument("--release", required=True)
    parser.add_argument("--plane", choices=["control", "runtime"], default="runtime")
    parser.add_argument("--service")
    parser.add_argument("--base-task-definition")
    args = parser.parse_args()
    summary = deploy(
        args.cluster,
        args.image,
        args.container,
        args.release,
        plane=args.plane,
        service=args.service,
        base_task_definition=args.base_task_definition,
    )
    print(json.dumps(summary, indent=2))
    failed = [row for row in summary["results"] if row["status"] != "ready"]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

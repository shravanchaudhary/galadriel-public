"""Private Lambda that creates one isolated ECS runtime per Replika."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import urllib.request

import boto3
from botocore.exceptions import ClientError

_callback_secret = None
REGISTERABLE_TASK_FIELDS = {
    "family",
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


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"{name} is not configured")
    return value


def _slug(value: str, limit: int = 24) -> str:
    normalized = re.sub(r"[^a-z0-9-]", "-", value.lower()).strip("-")
    digest = hashlib.sha256(value.encode()).hexdigest()[:8]
    return f"{normalized[: max(1, limit - 9)]}-{digest}"


def _callback_token() -> str:
    global _callback_secret
    if _callback_secret is None:
        response = boto3.client("secretsmanager").get_secret_value(
            SecretId=_required("CALLBACK_TOKEN_SECRET_ARN")
        )
        _callback_secret = response.get("SecretString")
        if not _callback_secret:
            raise RuntimeError("provisioner callback token secret is empty")
    return _callback_secret


def _provider_post(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {_callback_token()}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        if response.status != 200:
            raise RuntimeError(f"control-plane callback returned {response.status}")
        return json.loads(response.read().decode("utf-8"))


def _callback(owner_id: str, status: str, error: str | None = None) -> None:
    _provider_post(
        _required("CALLBACK_URL"),
        {"owner_id": owner_id, "status": status, "error": error},
    )


def _database_identity(owner_id: str, task_role_arn: str) -> dict:
    return _provider_post(
        _required("DATABASE_BROKER_URL"),
        {"owner_id": owner_id, "task_role_arn": task_role_arn},
    )


def _create_access_point(s3files, owner_id: str) -> tuple[str, str]:
    file_system_id = _required("S3FILES_FILE_SYSTEM_ID")
    path = f"/tenants/{owner_id}"
    try:
        response = s3files.create_access_point(
            fileSystemId=file_system_id,
            clientToken=hashlib.sha256(owner_id.encode()).hexdigest(),
            tags=[
                {"key": "ReplikaManaged", "value": "true"},
                {"key": "ReplikaTenant", "value": owner_id},
            ],
            posixUser={"uid": 1000, "gid": 1000},
            rootDirectory={
                "path": path,
                "creationPermissions": {
                    "ownerUid": 1000,
                    "ownerGid": 1000,
                    "permissions": "0750",
                },
            },
        )
        return response["accessPointArn"], response["accessPointId"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConflictException":
            raise

    for page in s3files.get_paginator("list_access_points").paginate(
        fileSystemId=file_system_id
    ):
        for access_point in page.get("accessPoints", []):
            if access_point.get("rootDirectory", {}).get("path") == path:
                return access_point["accessPointArn"], access_point["accessPointId"]
    raise RuntimeError(f"conflicting access point for tenant {owner_id} was not found")


def _assume_policy() -> str:
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        }
    )


def _slack_auth_secret(secretsmanager, owner_id: str) -> str:
    prefix = os.environ.get(
        "SLACK_TENANT_AUTH_SECRET_PREFIX", "replika/slack-auth"
    ).strip("/")
    name = f"{prefix}/{hashlib.sha256(owner_id.encode()).hexdigest()}"
    try:
        return secretsmanager.create_secret(
            Name=name,
            SecretString=secrets.token_urlsafe(48),
            KmsKeyId=os.environ.get(
                "SLACK_TENANT_AUTH_KMS_KEY_ID", "alias/aws/secretsmanager"
            ),
            Tags=[
                {"Key": "Service", "Value": "replika-slack-internal"},
                {"Key": "TenantDigest", "Value": hashlib.sha256(owner_id.encode()).hexdigest()},
            ],
        )["ARN"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceExistsException":
            raise
        return secretsmanager.describe_secret(SecretId=name)["ARN"]


def _task_role(iam, owner_id: str, access_point_arn: str) -> str:
    role_name = f"replika-{_slug(owner_id)}"
    try:
        role = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=_assume_policy(),
            Tags=[
                {"Key": "ReplikaManaged", "Value": "true"},
                {"Key": "ReplikaTenant", "Value": owner_id},
            ],
        )["Role"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
        role = iam.get_role(RoleName=role_name)["Role"]
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3files:ClientMount", "s3files:ClientWrite"],
                "Resource": _required("S3FILES_FILE_SYSTEM_ARN"),
                "Condition": {
                    "StringEquals": {"s3files:AccessPointArn": access_point_arn}
                },
            },
            {
                "Effect": "Allow",
                "Action": ["kms:Encrypt", "kms:Decrypt"],
                "Resource": _required("BYOM_KMS_KEY_ARN"),
                "Condition": {
                    "StringEquals": {
                        "kms:EncryptionContext:tenant_id": owner_id
                    }
                },
            },
        ],
    }
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName="replika-runtime",
        PolicyDocument=json.dumps(policy),
    )
    return role["Arn"]


def _target_group(elbv2, username: str) -> str:
    name = f"rp-{_slug(username, 29)}"[:32]
    try:
        existing = elbv2.describe_target_groups(Names=[name]).get("TargetGroups", [])
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "TargetGroupNotFound":
            raise
        existing = []
    if existing:
        return existing[0]["TargetGroupArn"]
    return elbv2.create_target_group(
        Name=name,
        Protocol="HTTP",
        Port=8080,
        VpcId=_required("VPC_ID"),
        TargetType="ip",
        HealthCheckProtocol="HTTP",
        HealthCheckPath="/healthz",
        Matcher={"HttpCode": "200"},
        Tags=[
            {"Key": "ReplikaManaged", "Value": "true"},
            {"Key": "ReplikaUsername", "Value": username},
        ],
    )["TargetGroups"][0]["TargetGroupArn"]


def _listener_priority(elbv2, listener_arn: str, owner_id: str) -> int:
    used = {
        int(rule["Priority"])
        for rule in elbv2.describe_rules(ListenerArn=listener_arn)["Rules"]
        if rule["Priority"].isdigit()
    }
    candidate = 1000 + int(hashlib.sha256(owner_id.encode()).hexdigest()[:6], 16) % 40000
    while candidate in used:
        candidate += 1
        if candidate > 49999:
            candidate = 1000
    return candidate


def _internal_listener_priority(elbv2, listener_arn: str, owner_id: str) -> int:
    used = {
        int(rule["Priority"])
        for rule in elbv2.describe_rules(ListenerArn=listener_arn)["Rules"]
        if rule["Priority"].isdigit()
    }
    candidate = 1 + int(hashlib.sha256(owner_id.encode()).hexdigest()[:6], 16) % 900
    while candidate in used:
        candidate = 1 if candidate >= 999 else candidate + 1
    return candidate


def _cognito_client(cognito, username: str) -> str:
    user_pool_id = _required("COGNITO_USER_POOL_ARN").rsplit("/", 1)[-1]
    client_name = f"replika-{_slug(username)}"
    next_token = None
    while True:
        request = {"UserPoolId": user_pool_id, "MaxResults": 60}
        if next_token:
            request["NextToken"] = next_token
        response = cognito.list_user_pool_clients(**request)
        for client in response.get("UserPoolClients", []):
            if client.get("ClientName") == client_name:
                return client["ClientId"]
        next_token = response.get("NextToken")
        if not next_token:
            break

    host = f"{username}.{_required('PRODUCT_DOMAIN')}"
    response = cognito.create_user_pool_client(
        UserPoolId=user_pool_id,
        ClientName=client_name,
        GenerateSecret=True,
        AllowedOAuthFlowsUserPoolClient=True,
        AllowedOAuthFlows=["code"],
        AllowedOAuthScopes=["openid", "email", "profile"],
        CallbackURLs=[f"https://{host}/oauth2/idpresponse"],
        LogoutURLs=[f"https://{host}/login"],
        SupportedIdentityProviders=["COGNITO"],
        PreventUserExistenceErrors="ENABLED",
    )
    return response["UserPoolClient"]["ClientId"]


def _ensure_rule(
    elbv2, cognito, username: str, owner_id: str, target_group_arn: str
) -> None:
    listener = _required("HTTPS_LISTENER_ARN")
    host = f"{username}.{_required('PRODUCT_DOMAIN')}"
    actions = []
    if os.environ.get("MANAGED_AUTH_ENABLED", "").lower() == "true":
        actions.append(
            {
                "Type": "authenticate-cognito",
                "Order": 1,
                "AuthenticateCognitoConfig": {
                    "UserPoolArn": _required("COGNITO_USER_POOL_ARN"),
                    "UserPoolClientId": _cognito_client(cognito, username),
                    "UserPoolDomain": _required("COGNITO_USER_POOL_DOMAIN"),
                },
            }
        )
    actions.append(
        {
            "Type": "forward",
            "Order": len(actions) + 1,
            "TargetGroupArn": target_group_arn,
        }
    )
    rules = elbv2.describe_rules(ListenerArn=listener)["Rules"]
    internal_actions = [
        {
            "Type": "forward",
            "Order": 1,
            "TargetGroupArn": target_group_arn,
        }
    ]
    internal_rule = None
    for rule in rules:
        fields = {condition.get("Field") for condition in rule.get("Conditions", [])}
        values = [
            value
            for condition in rule.get("Conditions", [])
            if condition.get("Field") == "host-header"
            for value in condition.get("Values", [])
        ]
        if host in values and "path-pattern" in fields:
            patterns = [
                value
                for condition in rule.get("Conditions", [])
                if condition.get("Field") == "path-pattern"
                for value in condition.get("Values", [])
            ]
            if "/internal/slack/ingress" in patterns:
                internal_rule = rule
                break
    if internal_rule:
        elbv2.modify_rule(
            RuleArn=internal_rule["RuleArn"], Actions=internal_actions
        )
    else:
        elbv2.create_rule(
            ListenerArn=listener,
            Priority=_internal_listener_priority(elbv2, listener, owner_id),
            Conditions=[
                {"Field": "host-header", "Values": [host]},
                {"Field": "path-pattern", "Values": ["/internal/slack/ingress"]},
            ],
            Actions=internal_actions,
            Tags=[
                {"Key": "ReplikaManaged", "Value": "true"},
                {"Key": "ReplikaTenant", "Value": owner_id},
            ],
        )

    for rule in rules:
        if any(
            condition.get("Field") == "path-pattern"
            for condition in rule.get("Conditions", [])
        ):
            continue
        values = [
            value
            for condition in rule.get("Conditions", [])
            if condition.get("Field") == "host-header"
            for value in condition.get("Values", [])
        ]
        if host in values:
            elbv2.modify_rule(RuleArn=rule["RuleArn"], Actions=actions)
            return
    elbv2.create_rule(
        ListenerArn=listener,
        Priority=_listener_priority(elbv2, listener, owner_id),
        Conditions=[{"Field": "host-header", "Values": [host]}],
        Actions=actions,
        Tags=[
            {"Key": "ReplikaManaged", "Value": "true"},
            {"Key": "ReplikaTenant", "Value": owner_id},
        ],
    )


def _task_definition(
    ecs,
    owner_id: str,
    username: str,
    replika_type: str,
    access_point_arn: str,
    task_role_arn: str,
    runtime_database: dict,
    slack_auth_secret_arn: str,
) -> str:
    current = ecs.describe_task_definition(
        taskDefinition=_required("BASE_TASK_DEFINITION")
    )["taskDefinition"]
    request = {
        key: copy.deepcopy(value)
        for key, value in current.items()
        if key in REGISTERABLE_TASK_FIELDS
    }
    request["family"] = f"replika-{_slug(owner_id)}"
    request["taskRoleArn"] = task_role_arn
    request["volumes"] = copy.deepcopy(current.get("volumes", []))
    for volume in request["volumes"]:
        if volume["name"] == "state":
            volume["s3filesVolumeConfiguration"]["accessPointArn"] = access_point_arn
    request["containerDefinitions"] = [
        container
        for container in request["containerDefinitions"]
        if container["name"] != "appconfig"
    ]
    allowed_secrets = set(json.loads(os.environ.get("RUNTIME_SECRET_NAMES", "[]")))
    for container in request["containerDefinitions"]:
        if container["name"] != _required("CONTAINER_NAME"):
            continue
        environment = {
            row["name"]: row["value"] for row in container.get("environment", [])
        }
        environment.update(
            {
                "REPLIKA_TENANT_ID": owner_id,
                "REPLIKA_USERNAME": username,
                "REPLIKA_TYPE": replika_type,
                "REPLIKA_PRODUCT_DOMAIN": _required("PRODUCT_DOMAIN"),
                "REPLIKA_CONTROL_PLANE_ONLY": "false",
                "REPLIKA_MANAGED_RUNTIME": "true",
                "REPLIKA_PROVISIONING_MODE": "runtime",
                "REPLIKA_PROVISIONER_FUNCTION_ARN": "",
                "APPCONFIG_REQUIRED": "false",
                "MONGO_DB": runtime_database["mongo_db"],
                "MONGO_URI": runtime_database["mongo_uri"],
            }
        )
        container["environment"] = [
            {"name": key, "value": value} for key, value in sorted(environment.items())
        ]
        container["secrets"] = [
            secret
            for secret in container.get("secrets", [])
            if secret.get("name") in allowed_secrets
        ]
        container["secrets"].append(
            {
                "name": "SLACK_TENANT_AUTH_SECRET",
                "valueFrom": slack_auth_secret_arn,
            }
        )
        container["dependsOn"] = [
            dependency
            for dependency in container.get("dependsOn", [])
            if dependency.get("containerName") != "appconfig"
        ]
    request["tags"] = [
        {"key": "ReplikaManaged", "value": "true"},
        {"key": "ReplikaPlane", "value": "runtime"},
        {"key": "ReplikaTenant", "value": owner_id},
    ]
    return ecs.register_task_definition(**request)["taskDefinition"]["taskDefinitionArn"]


def _service(
    ecs,
    owner_id: str,
    task_definition: str,
    target_group_arn: str,
    release: str,
) -> None:
    name = f"replika-{_slug(owner_id)}"
    kwargs = {
        "cluster": _required("ECS_CLUSTER"),
        "serviceName": name,
        "taskDefinition": task_definition,
        "desiredCount": 1,
        "launchType": "FARGATE",
        "platformVersion": "LATEST",
        "deploymentConfiguration": {
            "minimumHealthyPercent": 0,
            "maximumPercent": 100,
            "deploymentCircuitBreaker": {"enable": True, "rollback": True},
        },
        "networkConfiguration": {
            "awsvpcConfiguration": {
                "subnets": json.loads(_required("PRIVATE_SUBNET_IDS")),
                "securityGroups": [_required("TASK_SECURITY_GROUP_ID")],
                "assignPublicIp": "DISABLED",
            }
        },
        "loadBalancers": [
            {
                "targetGroupArn": target_group_arn,
                "containerName": _required("CONTAINER_NAME"),
                "containerPort": 8080,
            }
        ],
        "healthCheckGracePeriodSeconds": 90,
        "enableExecuteCommand": True,
        "propagateTags": "SERVICE",
        "tags": [
            {"key": "ReplikaManaged", "value": "true"},
            {"key": "ReplikaPlane", "value": "runtime"},
            {"key": "ReplikaTenant", "value": owner_id},
            {"key": "ReplikaRelease", "value": release},
            {"key": "ReplikaRollout", "value": "ready"},
        ],
    }
    try:
        ecs.create_service(**kwargs)
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in {
            "InvalidParameterException",
            "ServiceAlreadyExistsException",
        }:
            raise
        ecs.update_service(
            cluster=kwargs["cluster"],
            service=name,
            taskDefinition=task_definition,
            desiredCount=1,
            forceNewDeployment=True,
        )
    ecs.get_waiter("services_stable").wait(
        cluster=kwargs["cluster"],
        services=[name],
        WaiterConfig={"Delay": 15, "MaxAttempts": 50},
    )


def handler(event, _context):
    owner_id = str(event["owner_id"])
    username = str(event["username"])
    replika_type = str(event["replika_type"])
    if replika_type not in {"organization", "individual"}:
        raise ValueError("invalid replika_type")
    try:
        s3files = boto3.client("s3files")
        iam = boto3.client("iam")
        secretsmanager = boto3.client("secretsmanager")
        elbv2 = boto3.client("elbv2")
        cognito = boto3.client("cognito-idp")
        ecs = boto3.client("ecs")
        access_point_arn, _ = _create_access_point(s3files, owner_id)
        slack_auth_secret_arn = _slack_auth_secret(secretsmanager, owner_id)
        role_arn = _task_role(iam, owner_id, access_point_arn)
        runtime_database = _database_identity(owner_id, role_arn)
        target_group_arn = _target_group(elbv2, username)
        _ensure_rule(elbv2, cognito, username, owner_id, target_group_arn)
        task_definition = _task_definition(
            ecs,
            owner_id,
            username,
            replika_type,
            access_point_arn,
            role_arn,
            runtime_database,
            slack_auth_secret_arn,
        )
        _service(
            ecs,
            owner_id,
            task_definition,
            target_group_arn,
            str(event.get("release_version") or "v0"),
        )
        _callback(owner_id, "ready")
        return {"status": "ready"}
    except Exception as exc:
        try:
            _callback(owner_id, "error", str(exc))
        finally:
            raise

"""Central Slack OAuth installation storage and durable event routing."""

from __future__ import annotations

import base64
import atexit
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from flask import Blueprint, current_app, g, jsonify, redirect, render_template, request, session
from pymongo import ASCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

from .auth import SESSION_USER_KEY

INSTALLATIONS = "slack_installations"
OAUTH_STATES = "slack_oauth_states"
OUTBOX = "slack_event_outbox"
STATE_TTL_SECONDS = 600
SIGNATURE_TOLERANCE_SECONDS = 300
CLAIM_SECONDS = 30
MAX_BACKOFF_SECONDS = 300
MAX_DELIVERY_ATTEMPTS = 20
MAX_SLACK_REQUEST_BYTES = 1_000_000
MAX_OUTBOUND_TEXT_CHARS = 40_000
MAX_SLACK_EVENT_TEXT_CHARS = 40_000
MAX_SLACK_FILES = 20
log = logging.getLogger("galadriel.slack.central")


class SlackApi:
    """Small injectable Slack Web API client."""

    def call(self, method: str, *, token: str | None = None, **params) -> dict[str, Any]:
        data = urllib.parse.urlencode(params).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(
            f"https://slack.com/api/{method}", data=data, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise RuntimeError("Slack API request failed") from exc
        if not payload.get("ok"):
            raise RuntimeError(f"Slack API rejected {method}: {payload.get('error', 'unknown')}")
        return payload


class SlackTokenVault:
    """Token-store abstraction; implementations must never return token material in refs."""

    def put(self, replika_id: str, team_id: str, token: str) -> str:
        raise NotImplementedError

    def get(self, token_ref: str) -> str:
        raise NotImplementedError

    def delete(self, token_ref: str) -> None:
        raise NotImplementedError


class SecretsManagerTokenVault(SlackTokenVault):
    """AWS Secrets Manager vault using a required customer-configured KMS key."""

    def __init__(self, client=None):
        self.client = client

    def _client(self):
        if self.client is None:
            import boto3

            self.client = boto3.client("secretsmanager")
        return self.client

    def put(self, replika_id: str, team_id: str, token: str) -> str:
        kms_key_id = os.environ.get("SLACK_TOKEN_KMS_KEY_ID", "").strip()
        prefix = os.environ.get("SLACK_TOKEN_SECRET_PREFIX", "").strip().strip("/")
        if not kms_key_id or not prefix:
            raise RuntimeError(
                "SLACK_TOKEN_KMS_KEY_ID and SLACK_TOKEN_SECRET_PREFIX are required"
            )
        digest = hashlib.sha256(f"{replika_id}\0{team_id}".encode()).hexdigest()
        name = f"{prefix}/{digest}"
        client = self._client()
        try:
            result = client.create_secret(
                Name=name,
                SecretString=token,
                KmsKeyId=kms_key_id,
                Tags=[{"Key": "Service", "Value": "replika-slack"}],
            )
            return result["ARN"]
        except client.exceptions.ResourceExistsException:
            client.put_secret_value(SecretId=name, SecretString=token)
            return client.describe_secret(SecretId=name)["ARN"]

    def get(self, token_ref: str) -> str:
        token = self._client().get_secret_value(SecretId=token_ref).get("SecretString")
        if not token:
            raise RuntimeError("Slack token secret is empty")
        return token

    def delete(self, token_ref: str) -> None:
        self._client().delete_secret(
            SecretId=token_ref, ForceDeleteWithoutRecovery=True
        )


class TenantAuthVault:
    """Per-tenant HMAC keys stored only in Secrets Manager."""

    def __init__(self, client=None):
        self.client = client

    def _client(self):
        if self.client is None:
            import boto3

            self.client = boto3.client("secretsmanager")
        return self.client

    @staticmethod
    def secret_name(replika_id: str) -> str:
        prefix = os.environ.get(
            "SLACK_TENANT_AUTH_SECRET_PREFIX", "replika/slack-auth"
        ).strip("/")
        digest = hashlib.sha256(replika_id.encode()).hexdigest()
        return f"{prefix}/{digest}"

    def ensure(self, replika_id: str) -> str:
        client = self._client()
        name = self.secret_name(replika_id)
        try:
            result = client.create_secret(
                Name=name,
                SecretString=secrets.token_urlsafe(48),
                KmsKeyId=os.environ.get(
                    "SLACK_TENANT_AUTH_KMS_KEY_ID", "alias/aws/secretsmanager"
                ),
                Tags=[
                    {"Key": "Service", "Value": "replika-slack-internal"},
                    {
                        "Key": "TenantDigest",
                        "Value": hashlib.sha256(replika_id.encode()).hexdigest(),
                    },
                ],
            )
            return result["ARN"]
        except client.exceptions.ResourceExistsException:
            return client.describe_secret(SecretId=name)["ARN"]

    def delete(self, replika_id: str) -> None:
        try:
            self._client().delete_secret(
                SecretId=self.secret_name(replika_id),
                ForceDeleteWithoutRecovery=True,
            )
        except Exception as exc:
            message = str(exc).lower()
            code = ""
            if hasattr(exc, "response"):
                code = str(exc.response.get("Error", {}).get("Code") or "")
            if (
                code != "ResourceNotFoundException"
                and "resourcenotfoundexception" not in type(exc).__name__.lower()
                and "not found" not in message
                and "does not exist" not in message
            ):
                raise

    def get(self, secret_ref: str) -> str:
        value = self._client().get_secret_value(SecretId=secret_ref).get("SecretString")
        if not value:
            raise RuntimeError("Slack tenant authentication secret is empty")
        return value


def signed_internal_headers(replika_id: str, body: bytes, secret: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    base = replika_id.encode() + b":" + timestamp.encode() + b":" + body
    signature = hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Replika-Tenant": replika_id,
        "X-Replika-Timestamp": timestamp,
        "X-Replika-Signature": f"v1={signature}",
    }


def internal_signature_valid(
    replika_id: str, body: bytes, secret: str, headers
) -> bool:
    supplied_tenant = str(headers.get("X-Replika-Tenant") or "")
    timestamp = str(headers.get("X-Replika-Timestamp") or "")
    supplied = str(headers.get("X-Replika-Signature") or "")
    try:
        if abs(time.time() - int(timestamp)) > SIGNATURE_TOLERANCE_SECONDS:
            return False
    except ValueError:
        return False
    if not hmac.compare_digest(replika_id, supplied_tenant):
        return False
    base = replika_id.encode() + b":" + timestamp.encode() + b":" + body
    expected = "v1=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, supplied)


class TenantTransport:
    """Injectable authenticated HTTP transport to tenant runtimes."""

    def post(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        opener = urllib.request.build_opener(_RejectRedirects())
        try:
            with opener.open(req, timeout=10) as response:
                if response.status not in {200, 202}:
                    raise RuntimeError(f"tenant returned HTTP {response.status}")
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError("tenant Slack ingress failed") from exc


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SlackInstallationStore:
    """Mongo-backed installations, one-time OAuth states, dedupe, and outbox."""

    def __init__(self, db):
        self.installations = db[INSTALLATIONS]
        self.states = db[OAUTH_STATES]
        self.outbox = db[OUTBOX]
        self._ensure_indexes()
        self._migrate_legacy_installations()

    def _ensure_indexes(self) -> None:
        try:
            self.installations.drop_index("unique_slack_owner")
        except Exception:
            pass
        self.installations.create_index(
            [("replika_id", ASCENDING)], unique=True, name="unique_slack_replika"
        )
        self.installations.create_index(
            [("owner_id", ASCENDING)], unique=False, name="slack_owner"
        )
        self.installations.create_index(
            [("team_id", ASCENDING)], unique=True, name="unique_slack_team_route"
        )
        self.states.create_index(
            [("expires_at", ASCENDING)], expireAfterSeconds=0, name="expire_slack_oauth_state"
        )
        self.outbox.create_index(
            [("dedupe_key", ASCENDING)], unique=True, name="unique_slack_outbox_event"
        )
        self.outbox.create_index(
            [("status", ASCENDING), ("next_attempt_at", ASCENDING), ("created_at", ASCENDING)],
            name="slack_outbox_delivery",
        )

    def _migrate_legacy_installations(self) -> None:
        for document in self.installations.find(
            {"$or": [{"replika_id": {"$exists": False}}, {"replika_id": None}]}
        ):
            self.installations.update_one(
                {"_id": document["_id"]},
                {"$set": {"replika_id": str(document.get("owner_id") or document["_id"])}},
            )

    def save_state(
        self, nonce: str, owner_id: str, replika_id: str, replika_type: str
    ) -> None:
        now = datetime.now(timezone.utc)
        self.states.insert_one(
            {
                "_id": nonce,
                "owner_id": owner_id,
                "replika_id": replika_id,
                "replika_type": replika_type,
                "created_at": now,
                "expires_at": now + timedelta(seconds=STATE_TTL_SECONDS),
            }
        )

    def consume_state(self, nonce: str) -> dict[str, Any] | None:
        return self.states.find_one_and_delete(
            {"_id": nonce, "expires_at": {"$gt": datetime.now(timezone.utc)}}
        )

    def upsert_installation(self, document: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc)
        document = {**document, "updated_at": now}
        self.installations.update_one(
            {"replika_id": document["replika_id"]},
            {"$set": document, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )

    def set_auth_ref(self, replika_id: str, auth_ref: str) -> None:
        self.installations.update_one(
            {"replika_id": replika_id},
            {
                "$set": {
                    "internal_auth_ref": auth_ref,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )

    def for_replika(self, replika_id: str) -> dict[str, Any] | None:
        return self.installations.find_one({"replika_id": replika_id})

    def for_owner(self, owner_id: str) -> dict[str, Any] | None:
        """Compatibility helper for legacy owner-keyed installs."""
        return self.installations.find_one({"owner_id": owner_id})

    def for_team(self, team_id: str) -> dict[str, Any] | None:
        return self.installations.find_one({"team_id": team_id})

    def outbox_item(self, dedupe_key: str) -> dict[str, Any] | None:
        return self.outbox.find_one({"_id": dedupe_key})

    def select_channel(
        self, replika_id: str, team_id: str, channel: dict[str, Any]
    ) -> bool:
        result = self.installations.update_one(
            {
                "replika_id": replika_id,
                "team_id": team_id,
                "replika_type": "organization",
            },
            {
                "$set": {
                    "selected_channel": {
                        "id": channel["id"],
                        "name": channel.get("name") or channel["id"],
                    },
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        return bool(result.matched_count)

    def set_admins(
        self, replika_id: str, team_id: str, admin_user_ids: list[str]
    ) -> bool:
        result = self.installations.update_one(
            {
                "replika_id": replika_id,
                "team_id": team_id,
                "replika_type": "organization",
            },
            {
                "$set": {
                    "admin_user_ids": admin_user_ids,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        return bool(result.matched_count)

    def delete_installation(self, replika_id: str, team_id: str | None = None) -> bool:
        query: dict[str, Any] = {"replika_id": replika_id}
        if team_id is not None:
            query["team_id"] = team_id
        return bool(self.installations.delete_one(query).deleted_count)

    def delete_outbox_for_replika(self, replika_id: str) -> int:
        result = self.outbox.delete_many({"replika_id": replika_id})
        return int(result.deleted_count)

    def enqueue(
        self,
        dedupe_key: str,
        installation: dict[str, Any],
        kind: str,
        payload: dict[str, Any],
    ) -> bool:
        now = datetime.now(timezone.utc)
        replika_id = str(
            installation.get("replika_id") or installation.get("owner_id") or ""
        )
        try:
            self.outbox.insert_one(
                {
                    "_id": dedupe_key,
                    "dedupe_key": dedupe_key,
                    "replika_id": replika_id,
                    "owner_id": installation.get("owner_id"),
                    "team_id": installation["team_id"],
                    "replika_type": installation["replika_type"],
                    "kind": kind,
                    "payload": payload,
                    "status": "pending",
                    "attempts": 0,
                    "next_attempt_at": now,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            return True
        except DuplicateKeyError:
            return False

    def claim(self, worker_id: str) -> dict[str, Any] | None:
        from pymongo import ReturnDocument

        now = datetime.now(timezone.utc)
        return self.outbox.find_one_and_update(
            {
                "$or": [
                    {
                        "status": {"$in": ["pending", "retry"]},
                        "$or": [
                            {"next_attempt_at": {"$lte": now}},
                            {"next_attempt_at": {"$exists": False}},
                        ],
                    },
                    {"status": "claimed", "claim_expires_at": {"$lte": now}},
                ],
            },
            {
                "$set": {
                    "status": "claimed",
                    "claimed_by": worker_id,
                    "claimed_at": now,
                    "claim_expires_at": now + timedelta(seconds=CLAIM_SECONDS),
                    "updated_at": now,
                },
                "$inc": {"attempts": 1},
            },
            sort=[("created_at", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )

    def delivered(self, item_id: str, worker_id: str) -> None:
        now = datetime.now(timezone.utc)
        self.outbox.update_one(
            {"_id": item_id, "status": "claimed", "claimed_by": worker_id},
            {"$set": {
                "status": "delivered", "delivered_at": now, "updated_at": now,
                "last_error": None,
            }, "$unset": {"claim_expires_at": "", "claimed_by": ""}},
        )

    def set_runtime_placeholder(self, item_id: str, placeholder_ts: str) -> bool:
        result = self.outbox.update_one(
            {"_id": item_id, "placeholder_ts": {"$exists": False}},
            {"$set": {
                "placeholder_ts": placeholder_ts,
                "updated_at": datetime.now(timezone.utc),
            }},
        )
        return bool(result.matched_count)

    def failed(self, item_id: str, worker_id: str, error: str, attempts: int) -> None:
        now = datetime.now(timezone.utc)
        delay = min(MAX_BACKOFF_SECONDS, 2 ** min(max(attempts, 1), 8))
        status = "dead" if attempts >= MAX_DELIVERY_ATTEMPTS else "retry"
        self.outbox.update_one(
            {"_id": item_id, "status": "claimed", "claimed_by": worker_id},
            {"$set": {
                "status": status,
                "last_error": error[:2000],
                "last_failed_at": now,
                "next_attempt_at": now + timedelta(seconds=delay),
                "updated_at": now,
            }, "$unset": {"claim_expires_at": "", "claimed_by": ""}},
        )


def _chunk_slack(text: str, limit: int = 3900) -> list[str]:
    chunks: list[str] = []
    value = text
    while value:
        if len(value) <= limit:
            chunks.append(value)
            break
        split_at = value.rfind("\n", 0, limit)
        if split_at < 1:
            split_at = limit
        chunks.append(value[:split_at])
        value = value[split_at:].lstrip("\n")
    return chunks


def validated_tenant_url(value: str) -> str:
    """Reject tenant destinations outside the configured HTTPS product domain."""
    parsed = urllib.parse.urlparse(str(value or ""))
    allow_insecure = os.environ.get(
        "SLACK_ALLOW_INSECURE_TENANT_URLS", ""
    ).lower() in {"1", "true", "yes"}
    if allow_insecure:
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise RuntimeError("Invalid tenant runtime URL")
        return parsed.geturl().rstrip("/")
    domain = os.environ.get("SLACK_TENANT_PRODUCT_DOMAIN", "").strip().lower().strip(".")
    host = (parsed.hostname or "").lower().strip(".")
    if (
        parsed.scheme != "https"
        or not domain
        or not host
        or (host != domain and not host.endswith("." + domain))
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("Tenant runtime URL is outside the configured product domain")
    return parsed.geturl().rstrip("/")


class SlackOutboxDispatcher:
    """Single-process durable worker; Mongo claims make multiple workers safe."""

    def __init__(
        self,
        store,
        *,
        installation_resolver,
        tenant_url_resolver,
        auth_vault,
        token_vault,
        transport,
        slack_api,
        poll_seconds: float = 0.5,
    ):
        self.store = store
        self.installation_resolver = installation_resolver
        self.tenant_url_resolver = tenant_url_resolver
        self.auth_vault = auth_vault
        self.token_vault = token_vault
        self.transport = transport
        self.slack_api = slack_api
        self.poll_seconds = poll_seconds
        self.worker_id = str(uuid.uuid4())
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="slack-outbox-dispatcher", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)

    def dispatch_once(self) -> bool:
        item = self.store.claim(self.worker_id)
        if not item:
            return False
        try:
            replika_id = str(
                item.get("replika_id") or item.get("owner_id") or ""
            )
            installation = self.installation_resolver(replika_id)
            if not installation or installation.get("team_id") != item.get("team_id"):
                raise RuntimeError("Slack installation no longer exists")
            if item["kind"] == "outbound":
                self._deliver_slack(installation, item["payload"])
            else:
                self._deliver_tenant(installation, item)
            self.store.delivered(item["_id"], self.worker_id)
        except Exception as exc:
            log.warning("Slack outbox delivery failed for %s: %s", item.get("_id"), exc)
            self.store.failed(
                item["_id"], self.worker_id, str(exc), int(item.get("attempts") or 1)
            )
        return True

    def _deliver_tenant(self, installation: dict[str, Any], item: dict[str, Any]) -> None:
        replika_id = str(
            installation.get("replika_id") or installation.get("owner_id") or ""
        )
        auth_ref = installation.get("internal_auth_ref")
        if not auth_ref:
            auth_ref = self.auth_vault.ensure(replika_id)
            self.store.set_auth_ref(replika_id, auth_ref)
        payload = {
            "tenant_id": replika_id,
            "team_id": installation["team_id"],
            "replika_type": installation["replika_type"],
            "installer_user_id": installation.get("installer_user_id"),
            "admin_user_ids": installation.get("admin_user_ids") or [],
            "selected_channel": installation.get("selected_channel"),
            "bot_user_id": installation.get("bot_user_id"),
            "kind": item["kind"],
            "dedupe_key": item["dedupe_key"],
            "payload": item["payload"],
            "placeholder_ts": item.get("placeholder_ts"),
        }
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers = signed_internal_headers(
            replika_id, body, self.auth_vault.get(auth_ref)
        )
        url = validated_tenant_url(
            self.tenant_url_resolver(replika_id)
        ) + "/internal/slack/ingress"
        self.transport.post(url, payload, headers)

    def _deliver_slack(self, installation: dict[str, Any], payload: dict[str, Any]) -> None:
        token = self.token_vault.get(installation["token_ref"])
        placeholder_ts = str(payload.get("placeholder_ts") or "")
        if payload.get("delete_placeholder"):
            if placeholder_ts:
                self.slack_api.call(
                    "chat.delete",
                    token=token,
                    channel=payload["channel"],
                    ts=placeholder_ts,
                )
            return
        chunks = _chunk_slack(str(payload["text"]))
        if placeholder_ts and chunks:
            self.slack_api.call(
                "chat.update",
                token=token,
                channel=payload["channel"],
                ts=placeholder_ts,
                text=chunks.pop(0),
            )
        for chunk in chunks:
            params = {"channel": payload["channel"], "text": chunk}
            if payload.get("thread_ts"):
                params["thread_ts"] = payload["thread_ts"]
            self.slack_api.call("chat.postMessage", token=token, **params)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.dispatch_once()
            except Exception:
                log.exception("Slack outbox dispatcher iteration failed")
                worked = False
            if not worked:
                self._stop.wait(self.poll_seconds)


_db = None
_store_instance = None


def _store() -> SlackInstallationStore:
    injected = current_app.config.get("SLACK_INSTALLATION_STORE")
    if injected is not None:
        return injected
    global _db, _store_instance
    if _store_instance is None:
        uri = os.environ.get("SLACK_INTEGRATIONS_MONGO_URI") or os.environ.get("MONGO_URI")
        name = os.environ.get("SLACK_INTEGRATIONS_MONGO_DB") or os.environ.get("MONGO_DB")
        if not uri or not name:
            raise RuntimeError("Central Slack integration storage is not configured")
        _db = MongoClient(uri)[name]
        _store_instance = SlackInstallationStore(_db)
    return _store_instance


def _vault() -> SlackTokenVault:
    return current_app.config.get("SLACK_TOKEN_VAULT") or SecretsManagerTokenVault()


def _auth_vault() -> TenantAuthVault:
    return current_app.config.get("SLACK_TENANT_AUTH_VAULT") or TenantAuthVault()


def _slack() -> SlackApi:
    return current_app.config.get("SLACK_API") or SlackApi()


def _owner_id() -> str:
    auth_result = getattr(g, "tower_auth", None)
    value = (
        getattr(auth_result, "username", None)
        or session.get(SESSION_USER_KEY)
        or os.environ.get("REPLIKA_OWNER_ID")
        or os.environ.get("REPLIKA_TENANT_ID")
    )
    if not value or value == "default":
        raise PermissionError("Authenticated account required")
    return str(value)


def _replika_type(replika_id: str) -> str:
    configured = os.environ.get("REPLIKA_TYPE", "").strip()
    if configured in {"organization", "individual"}:
        return configured
    resolver = current_app.config.get("REPLIKA_TYPE_RESOLVER")
    if resolver:
        value = resolver(replika_id)
        if value in {"organization", "individual"}:
            return value
    raise RuntimeError("Create a typed Replika before installing Slack")


def _owned_replika(replika_id: str, owner_id: str) -> dict[str, Any]:
    checker = current_app.config.get("REPLIKA_OWNERSHIP_CHECKER")
    if checker is not None:
        document = checker(replika_id, owner_id)
    else:
        from .replika_control_plane import _store as replika_store

        document = replika_store().find_owned(replika_id, owner_id)
    if not document:
        raise LookupError("Unknown Replika")
    if document.get("status") == "deleting":
        raise RuntimeError("That Replika is being deleted")
    return document


def purge_replika_slack(replika_id: str) -> None:
    """Revoke Slack tokens and remove all Replika-scoped Slack rows/secrets."""
    store = _store()
    installation = store.for_replika(replika_id)
    if installation:
        token_ref = installation.get("token_ref")
        if token_ref:
            try:
                token = _vault().get(token_ref)
                try:
                    _slack().call("auth.revoke", token=token)
                except RuntimeError:
                    log.warning(
                        "Slack token revoke failed during purge for %s",
                        replika_id,
                        exc_info=True,
                    )
                _vault().delete(token_ref)
            except Exception:
                log.warning(
                    "Slack token secret cleanup failed for %s",
                    replika_id,
                    exc_info=True,
                )
        store.delete_installation(replika_id, installation.get("team_id"))
    else:
        store.delete_installation(replika_id)
    store.delete_outbox_for_replika(replika_id)
    try:
        _auth_vault().delete(replika_id)
    except Exception:
        log.warning(
            "Slack tenant auth secret cleanup failed for %s",
            replika_id,
            exc_info=True,
        )


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not configured")
    return value


def _state_secret() -> bytes:
    return _required("SLACK_OAUTH_STATE_SECRET").encode()


def _encode_state(nonce: str) -> str:
    body = json.dumps({"nonce": nonce}, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(body).rstrip(b"=")
    signature = hmac.new(_state_secret(), encoded, hashlib.sha256).digest()
    return (
        encoded.decode()
        + "."
        + base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
    )


def _decode_state(value: str) -> str:
    try:
        encoded, supplied = value.split(".", 1)
        expected = hmac.new(_state_secret(), encoded.encode(), hashlib.sha256).digest()
        supplied_bytes = base64.urlsafe_b64decode(supplied + "=" * (-len(supplied) % 4))
        if not hmac.compare_digest(expected, supplied_bytes):
            raise ValueError
        body = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        nonce = json.loads(body)["nonce"]
        if not isinstance(nonce, str) or not nonce:
            raise ValueError
        return nonce
    except (ValueError, KeyError, json.JSONDecodeError):
        raise ValueError("Invalid OAuth state")


def _signature_valid(raw_body: bytes) -> bool:
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    supplied = request.headers.get("X-Slack-Signature", "")
    try:
        if abs(time.time() - int(timestamp)) > SIGNATURE_TOLERANCE_SECONDS:
            return False
    except ValueError:
        return False
    base = b"v0:" + timestamp.encode() + b":" + raw_body
    expected = "v0=" + hmac.new(
        _required("SLACK_SIGNING_SECRET").encode(), base, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, supplied)


def _installation_view(document: dict[str, Any] | None) -> dict[str, Any]:
    if not document:
        return {"connected": False}
    return {
        "connected": True,
        "replika_id": document.get("replika_id"),
        "team_id": document["team_id"],
        "team_name": document.get("team_name") or document["team_id"],
        "replika_type": document["replika_type"],
        "installer_user_id": document.get("installer_user_id"),
        "admin_user_ids": document.get("admin_user_ids") or [],
        "selected_channel": document.get("selected_channel"),
        "mode": "dm" if document["replika_type"] == "individual" else "channel",
    }


def _event_is_routable(installation: dict[str, Any], event: dict[str, Any]) -> bool:
    if event.get("type") not in {"message", "app_mention"} or event.get("bot_id"):
        return False
    if len(event.get("files") or []) > MAX_SLACK_FILES:
        return False
    if installation["replika_type"] == "individual":
        if (
            event.get("type") != "message"
            or event.get("subtype") not in {None, "file_share"}
            or event.get("user") == installation.get("bot_user_id")
            or not isinstance(event.get("text"), str)
            or not event.get("text", "").strip()
            or len(event.get("text", "")) > MAX_SLACK_EVENT_TEXT_CHARS
        ):
            return False
        is_dm = event.get("channel_type") == "im" or str(
            event.get("channel", "")
        ).startswith("D")
        return is_dm and hmac.compare_digest(
            str(installation.get("installer_user_id") or ""),
            str(event.get("user") or ""),
        )
    selected = (installation.get("selected_channel") or {}).get("id")
    if not selected or not hmac.compare_digest(
        str(selected), str(event.get("channel", ""))
    ):
        return False
    subtype = event.get("subtype")
    if subtype in {"message_changed", "message_deleted"}:
        nested = event.get("message") or event.get("previous_message") or {}
        return (
            event.get("type") == "message"
            and isinstance(nested, dict)
            and not nested.get("bot_id")
            and bool(nested.get("user"))
            and nested.get("user") != installation.get("bot_user_id")
            and len(str(nested.get("text") or "")) <= MAX_SLACK_EVENT_TEXT_CHARS
        )
    return (
        subtype in {None, "file_share"}
        and event.get("user") != installation.get("bot_user_id")
        and bool(event.get("user"))
        and isinstance(event.get("text"), str)
        and bool(event.get("text", "").strip() or event.get("files"))
        and len(event.get("text", "")) <= MAX_SLACK_EVENT_TEXT_CHARS
    )


def _tenant_url_resolver(replika_id: str) -> str:
    injected = current_app.config.get("SLACK_TENANT_URL_RESOLVER")
    if injected:
        return injected(replika_id)
    from .replika_control_plane import _store as replika_store

    replika = replika_store().find_by_id(replika_id)
    if not replika or replika.get("status") != "ready" or not replika.get("product_url"):
        raise RuntimeError("Tenant runtime is not ready")
    return str(replika["product_url"])


def start_slack_dispatcher(app) -> SlackOutboxDispatcher | None:
    if not app.config.get(
        "SLACK_DISPATCHER_ENABLED",
        os.environ.get("SLACK_DISPATCHER_ENABLED", "").lower() in {"1", "true", "yes"},
    ):
        return None
    dispatcher = app.config.get("SLACK_OUTBOX_DISPATCHER")
    if dispatcher is None:
        with app.app_context():
            tenant_url_resolver = app.config.get("SLACK_TENANT_URL_RESOLVER")
            if tenant_url_resolver is None:
                from .replika_control_plane import _store as replika_store

                control_store = replika_store()

                def tenant_url_resolver(replika_id):
                    replika = control_store.find_by_id(replika_id)
                    if (
                        not replika
                        or replika.get("status") != "ready"
                        or not replika.get("product_url")
                    ):
                        raise RuntimeError("Tenant runtime is not ready")
                    return str(replika["product_url"])
            store = _store()
            dispatcher = SlackOutboxDispatcher(
                store,
                installation_resolver=store.for_replika,
                tenant_url_resolver=tenant_url_resolver,
                auth_vault=_auth_vault(),
                token_vault=_vault(),
                transport=app.config.get("SLACK_TENANT_TRANSPORT") or TenantTransport(),
                slack_api=_slack(),
            )
    dispatcher.start()
    app.extensions["slack_dispatcher"] = dispatcher
    atexit.register(dispatcher.stop)
    return dispatcher


def register_slack_integration(app) -> None:
    bp = Blueprint("slack_integration", __name__)

    def _integrations_redirect_for_owner():
        from .replika_control_plane import _store as replika_store

        documents = replika_store().list_for_owner(_owner_id())
        if not documents:
            return redirect("/replika")
        return redirect(
            f"/replika/{documents[0].get('replika_id') or documents[0]['_id']}/integrations"
        )

    def _render_integrations(replika_id: str):
        owner_id = _owner_id()
        try:
            replika = _owned_replika(replika_id, owner_id)
        except LookupError:
            return jsonify({"error": "Unknown Replika"}), 404
        except PermissionError:
            return jsonify({"error": "Unauthorized"}), 401
        except RuntimeError as exc:
            return render_template(
                "integrations/index.html",
                slack={"connected": False},
                replika_id=replika_id,
                replika_username=None,
                replika_type=None,
                configuration_error=str(exc),
            )
        installation = _store().for_replika(replika_id)
        replika_type = (
            installation["replika_type"]
            if installation
            else replika.get("replika_type")
        )
        return render_template(
            "integrations/index.html",
            slack=_installation_view(installation),
            replika_id=replika_id,
            replika_username=replika.get("username"),
            replika_type=replika_type,
            configuration_error=None,
        )

    @bp.get("/integrations")
    def integrations_page():
        try:
            return _integrations_redirect_for_owner()
        except PermissionError:
            return jsonify({"error": "Unauthorized"}), 401

    @bp.get("/replika/<replika_id>/integrations")
    def replika_integrations_page(replika_id: str):
        return _render_integrations(replika_id)

    @bp.get("/integrations/slack/install")
    def slack_install_legacy():
        replika_id = str(request.args.get("replika_id") or "").strip()
        if not replika_id:
            return _integrations_redirect_for_owner()
        return redirect(f"/replika/{replika_id}/integrations/slack/install")

    @bp.get("/replika/<replika_id>/integrations/slack/install")
    def slack_install(replika_id: str):
        owner_id = _owner_id()
        replika = _owned_replika(replika_id, owner_id)
        if replika.get("status") != "ready":
            return redirect(f"/replika/{replika_id}/integrations?slack=not-ready")
        replika_type = str(replika.get("replika_type") or _replika_type(replika_id))
        nonce = secrets.token_urlsafe(32)
        _store().save_state(nonce, owner_id, replika_id, replika_type)
        params = {
            "client_id": _required("SLACK_CLIENT_ID"),
            "scope": "app_mentions:read,channels:history,channels:read,chat:write,commands,groups:history,groups:read,im:history,im:read,im:write,users:read",
            "redirect_uri": _required("SLACK_OAUTH_REDIRECT_URI"),
            "state": _encode_state(nonce),
        }
        return redirect(
            "https://slack.com/oauth/v2/authorize?" + urllib.parse.urlencode(params)
        )

    @bp.get("/integrations/slack/oauth/callback")
    def slack_oauth_callback():
        replika_id = ""
        try:
            if request.args.get("error"):
                return redirect("/replika?slack=denied")
            nonce = _decode_state(request.args.get("state", ""))
            state = _store().consume_state(nonce)
            if not state:
                raise ValueError("Expired or already-used OAuth state")
            replika_id = str(state.get("replika_id") or state.get("owner_id") or "")
            result = _slack().call(
                "oauth.v2.access",
                client_id=_required("SLACK_CLIENT_ID"),
                client_secret=_required("SLACK_CLIENT_SECRET"),
                code=request.args.get("code", ""),
                redirect_uri=_required("SLACK_OAUTH_REDIRECT_URI"),
            )
            team = result.get("team") or {}
            team_id = str(team.get("id") or "")
            token = str(result.get("access_token") or "")
            installer_user_id = str((result.get("authed_user") or {}).get("id") or "")
            if not team_id or not token or not installer_user_id or not replika_id:
                raise RuntimeError("Slack OAuth response was incomplete")
            existing = _store().for_team(team_id)
            if existing and existing.get("replika_id") != replika_id:
                raise RuntimeError("That Slack workspace is linked to another Replika")
            current = _store().for_replika(replika_id)
            if current and current["team_id"] != team_id:
                raise RuntimeError("Disconnect the current Slack workspace first")
            token_ref = _vault().put(replika_id, team_id, token)
            auth_ref = _auth_vault().ensure(replika_id)
            _store().upsert_installation(
                {
                    "owner_id": state["owner_id"],
                    "replika_id": replika_id,
                    "team_id": team_id,
                    "team_name": team.get("name"),
                    "replika_type": state["replika_type"],
                    "token_ref": token_ref,
                    "internal_auth_ref": auth_ref,
                    "bot_user_id": result.get("bot_user_id"),
                    "installer_user_id": installer_user_id,
                    "admin_user_ids": [installer_user_id],
                    "scope": result.get("scope"),
                }
            )
            return redirect(f"/replika/{replika_id}/integrations?slack=connected")
        except ValueError:
            current_app.logger.warning("Rejected invalid Slack OAuth state")
            target = (
                f"/replika/{replika_id}/integrations?slack=error"
                if replika_id
                else "/replika?slack=error"
            )
            return redirect(target)
        except Exception:
            current_app.logger.exception("Slack OAuth callback failed")
            target = (
                f"/replika/{replika_id}/integrations?slack=error"
                if replika_id
                else "/replika?slack=error"
            )
            return redirect(target)

    def _require_owned_installation(replika_id: str):
        owner_id = _owner_id()
        _owned_replika(replika_id, owner_id)
        return _store().for_replika(replika_id)

    @bp.get("/api/replikas/<replika_id>/integrations/slack/status")
    @bp.get("/api/integrations/slack/status")
    def slack_status(replika_id: str | None = None):
        replika_id = replika_id or str(request.args.get("replika_id") or "").strip()
        if not replika_id:
            return jsonify({"error": "replika_id is required"}), 400
        try:
            installation = _require_owned_installation(replika_id)
        except LookupError:
            return jsonify({"error": "Unknown Replika"}), 404
        except PermissionError:
            return jsonify({"error": "Unauthorized"}), 401
        return jsonify(_installation_view(installation))

    @bp.get("/api/replikas/<replika_id>/integrations/slack/channels")
    @bp.get("/api/integrations/slack/channels")
    def slack_channels(replika_id: str | None = None):
        replika_id = replika_id or str(request.args.get("replika_id") or "").strip()
        if not replika_id:
            return jsonify({"error": "replika_id is required"}), 400
        try:
            installation = _require_owned_installation(replika_id)
        except LookupError:
            return jsonify({"error": "Unknown Replika"}), 404
        except PermissionError:
            return jsonify({"error": "Unauthorized"}), 401
        if not installation:
            return jsonify({"error": "Slack is not connected"}), 404
        if installation["replika_type"] != "organization":
            return jsonify({"channels": [], "mode": "dm"})
        result = _slack().call(
            "conversations.list",
            token=_vault().get(installation["token_ref"]),
            types="public_channel,private_channel",
            exclude_archived="true",
            limit="200",
        )
        channels = [
            {
                "id": row["id"],
                "name": row.get("name") or row["id"],
                "is_private": bool(row.get("is_private")),
                "is_member": bool(row.get("is_member")),
            }
            for row in result.get("channels", [])
            if row.get("id")
            and (not row.get("is_private") or row.get("is_member"))
        ]
        return jsonify({"channels": channels})

    @bp.put("/api/replikas/<replika_id>/integrations/slack/channel")
    @bp.put("/api/integrations/slack/channel")
    def slack_select_channel(replika_id: str | None = None):
        replika_id = replika_id or str(
            (request.get_json(silent=True) or {}).get("replika_id")
            or request.args.get("replika_id")
            or ""
        ).strip()
        if not replika_id:
            return jsonify({"error": "replika_id is required"}), 400
        try:
            installation = _require_owned_installation(replika_id)
        except LookupError:
            return jsonify({"error": "Unknown Replika"}), 404
        except PermissionError:
            return jsonify({"error": "Unauthorized"}), 401
        if not installation:
            return jsonify({"error": "Slack is not connected"}), 404
        if installation["replika_type"] != "organization":
            return jsonify({"error": "Individual Replikas use Slack DM mode"}), 400
        channel_id = str((request.get_json(silent=True) or {}).get("channel_id") or "")
        channels_response = slack_channels(replika_id)
        if isinstance(channels_response, tuple):
            return channels_response
        channels = channels_response.get_json()["channels"]
        selected = next((row for row in channels if row["id"] == channel_id), None)
        if not selected:
            return jsonify({"error": "Select one accessible Slack channel"}), 400
        members = {
            str(value) for value in _slack().call(
                "conversations.members",
                token=_vault().get(installation["token_ref"]),
                channel=channel_id,
                limit="1000",
            ).get("members", [])
        }
        installer = str(installation.get("installer_user_id") or "")
        if installer not in members:
            return jsonify({"error": "The OAuth installer must belong to that channel"}), 400
        if not _store().select_channel(
            replika_id, installation["team_id"], selected
        ):
            return jsonify({"error": "Slack installation changed"}), 409
        retained_admins = [
            value for value in (installation.get("admin_user_ids") or [installer])
            if value in members
        ]
        _store().set_admins(
            replika_id, installation["team_id"],
            list(dict.fromkeys([installer, *retained_admins])),
        )
        return jsonify({"selected_channel": {"id": selected["id"], "name": selected["name"]}})

    @bp.put("/api/replikas/<replika_id>/integrations/slack/admins")
    @bp.put("/api/integrations/slack/admins")
    def slack_set_admins(replika_id: str | None = None):
        data = request.get_json(silent=True) or {}
        replika_id = replika_id or str(
            data.get("replika_id") or request.args.get("replika_id") or ""
        ).strip()
        if not replika_id:
            return jsonify({"error": "replika_id is required"}), 400
        try:
            installation = _require_owned_installation(replika_id)
        except LookupError:
            return jsonify({"error": "Unknown Replika"}), 404
        except PermissionError:
            return jsonify({"error": "Unauthorized"}), 401
        if not installation:
            return jsonify({"error": "Slack is not connected"}), 404
        if installation["replika_type"] != "organization":
            return jsonify({"error": "Admins apply only to organization Replikas"}), 400
        selected = (installation.get("selected_channel") or {}).get("id")
        if not selected:
            return jsonify({"error": "Select the Replika channel first"}), 400
        raw_ids = data.get("admin_user_ids")
        if not isinstance(raw_ids, list) or len(raw_ids) > 25:
            return jsonify({"error": "admin_user_ids must be a list of at most 25 IDs"}), 400
        installer = str(installation.get("installer_user_id") or "")
        ids = list(dict.fromkeys(
            [installer] + [str(value).strip() for value in raw_ids]
        ))
        if any(not re.fullmatch(r"[UW][A-Z0-9]{2,30}", value) for value in ids):
            return jsonify({"error": "One or more Slack user IDs are invalid"}), 400
        token = _vault().get(installation["token_ref"])
        members_result = _slack().call(
            "conversations.members", token=token, channel=selected, limit="1000"
        )
        members = {str(value) for value in members_result.get("members", [])}
        if any(value not in members for value in ids):
            return jsonify({"error": "Every admin must be a member of the selected channel"}), 400
        try:
            for user_id in ids:
                user = (_slack().call("users.info", token=token, user=user_id).get("user") or {})
                if (
                    str(user.get("team_id") or installation["team_id"]) != installation["team_id"]
                    or user.get("deleted")
                    or user.get("is_bot")
                ):
                    raise ValueError
        except (RuntimeError, ValueError):
            return jsonify({"error": "Every admin must be an active human workspace member"}), 400
        if not _store().set_admins(replika_id, installation["team_id"], ids):
            return jsonify({"error": "Slack installation changed"}), 409
        return jsonify({"admin_user_ids": ids})

    @bp.delete("/api/replikas/<replika_id>/integrations/slack")
    @bp.delete("/api/integrations/slack")
    def slack_disconnect(replika_id: str | None = None):
        replika_id = replika_id or str(
            (request.get_json(silent=True) or {}).get("replika_id")
            or request.args.get("replika_id")
            or ""
        ).strip()
        if not replika_id:
            return jsonify({"error": "replika_id is required"}), 400
        try:
            installation = _require_owned_installation(replika_id)
        except LookupError:
            return jsonify({"error": "Unknown Replika"}), 404
        except PermissionError:
            return jsonify({"error": "Unauthorized"}), 401
        if not installation:
            return jsonify({"disconnected": True})
        token_ref = installation["token_ref"]
        token = _vault().get(token_ref)
        try:
            _slack().call("auth.revoke", token=token)
        except RuntimeError:
            current_app.logger.warning("Slack token revoke failed", exc_info=True)
        _store().delete_installation(replika_id, installation["team_id"])
        _vault().delete(token_ref)
        return jsonify({"disconnected": True})

    @bp.post("/slack/events")
    def slack_events():
        if (request.content_length or 0) > MAX_SLACK_REQUEST_BYTES:
            return jsonify({"error": "Payload too large"}), 413
        raw = request.get_data(cache=True)
        if len(raw) > MAX_SLACK_REQUEST_BYTES:
            return jsonify({"error": "Payload too large"}), 413
        try:
            valid = _signature_valid(raw)
        except RuntimeError:
            return jsonify({"error": "Slack signing is not configured"}), 503
        if not valid:
            return jsonify({"error": "Invalid Slack signature"}), 401
        payload = request.get_json(silent=True) or {}
        if payload.get("type") == "url_verification":
            return jsonify({"challenge": payload.get("challenge")})
        team_id = str(payload.get("team_id") or "")
        installation = _store().for_team(team_id)
        event = payload.get("event") or {}
        if not installation or not _event_is_routable(installation, event):
            return jsonify({"accepted": False})
        event_ts = str(event.get("event_ts") or event.get("ts") or "")
        dedupe_key = (
            f"event:{team_id}:{event.get('channel')}:{event_ts}"
            if event_ts
            else str(payload.get("event_id") or hashlib.sha256(raw).hexdigest())
        )
        routed_payload = {
            "team_id": team_id,
            "event_id": payload.get("event_id"),
            "event_time": payload.get("event_time"),
            "event": event,
        }
        accepted = _store().enqueue(
            dedupe_key, installation, "event", routed_payload
        )
        return jsonify({"accepted": accepted})

    @bp.post("/slack/commands")
    def slack_commands():
        if (request.content_length or 0) > MAX_SLACK_REQUEST_BYTES:
            return jsonify({"error": "Payload too large"}), 413
        raw = request.get_data(cache=True)
        if len(raw) > MAX_SLACK_REQUEST_BYTES:
            return jsonify({"error": "Payload too large"}), 413
        try:
            valid = _signature_valid(raw)
        except RuntimeError:
            return jsonify({"error": "Slack signing is not configured"}), 503
        if not valid:
            return jsonify({"error": "Invalid Slack signature"}), 401
        form = request.form.to_dict()
        installation = _store().for_team(str(form.get("team_id") or ""))
        if not installation:
            return jsonify({"response_type": "ephemeral", "text": "Slack is not connected."})
        if installation["replika_type"] == "organization":
            selected = (installation.get("selected_channel") or {}).get("id")
            if not selected or selected != form.get("channel_id"):
                return jsonify(
                    {"response_type": "ephemeral", "text": "Use the selected Replika channel."}
                )
            if not str(form.get("user_id") or ""):
                return jsonify(
                    {"response_type": "ephemeral", "text": "Slack user identity is required."}
                )
        else:
            if installation.get("installer_user_id") != form.get("user_id"):
                return jsonify(
                    {"response_type": "ephemeral", "text": "This Replika is linked to another user."}
                )
            if not str(form.get("channel_id") or "").startswith("D"):
                return jsonify(
                    {"response_type": "ephemeral", "text": "Use this command in your Replika DM."}
                )
        command_name = str(form.get("command") or "").lower()
        if command_name not in {"/stop", "/cancel"}:
            return jsonify(
                {
                    "response_type": "ephemeral",
                    "text": "Unsupported command. Use /stop or /cancel.",
                }
            )
        dedupe_key = "command:" + str(
            form.get("trigger_id") or hashlib.sha256(raw).hexdigest()
        )
        routed_command = {
            key: form.get(key)
            for key in (
                "team_id",
                "channel_id",
                "channel_name",
                "user_id",
                "user_name",
                "command",
                "text",
                "trigger_id",
            )
        }
        accepted = _store().enqueue(
            dedupe_key, installation, "control", routed_command
        )
        return jsonify(
            {
                "response_type": "ephemeral",
                "text": (
                    "Stopping the current Replika turn and pausing queued work."
                    if accepted
                    else "Stop was already requested."
                ),
            }
        )

    @bp.post("/internal/slack/deliver")
    def slack_outbound_delivery():
        if (request.content_length or 0) > MAX_SLACK_REQUEST_BYTES:
            return jsonify({"error": "Payload too large"}), 413
        raw = request.get_data(cache=True)
        if len(raw) > MAX_SLACK_REQUEST_BYTES:
            return jsonify({"error": "Payload too large"}), 413
        data = request.get_json(silent=True) or {}
        replika_id = str(data.get("tenant_id") or "")
        installation = _store().for_replika(replika_id) if replika_id else None
        if not installation or not installation.get("internal_auth_ref"):
            return jsonify({"error": "Unauthorized"}), 401
        try:
            secret = _auth_vault().get(installation["internal_auth_ref"])
        except Exception:
            current_app.logger.exception("Could not resolve Slack tenant auth secret")
            return jsonify({"error": "Authentication unavailable"}), 503
        if not internal_signature_valid(replika_id, raw, secret, request.headers):
            return jsonify({"error": "Unauthorized"}), 401
        team_id = str(data.get("team_id") or "")
        channel = str(data.get("channel") or "")
        text = data.get("text")
        thread_ts = data.get("thread_ts")
        placeholder_ts = str(data.get("placeholder_ts") or "")
        create_placeholder = data.get("create_placeholder") is True
        delete_placeholder = data.get("delete_placeholder") is True
        source_dedupe_key = str(data.get("source_dedupe_key") or "")
        if (
            team_id != installation.get("team_id")
            or not channel
            or (
                not delete_placeholder
                and not create_placeholder
                and (
                    not isinstance(text, str)
                    or not text.strip()
                    or len(text) > MAX_OUTBOUND_TEXT_CHARS
                )
            )
            or (delete_placeholder and (not placeholder_ts or text is not None))
            or (
                create_placeholder
                and (delete_placeholder or placeholder_ts or text is not None)
            )
            or (thread_ts is not None and not isinstance(thread_ts, str))
            or not source_dedupe_key
        ):
            return jsonify({"error": "Invalid delivery request"}), 400
        source = _store().outbox_item(source_dedupe_key)
        source_event = ((source or {}).get("payload") or {}).get("event") or {}
        source_replika_id = str(
            source.get("replika_id") or source.get("owner_id") or ""
        ) if source else ""
        if (
            not source
            or source.get("kind") != "event"
            or source_replika_id != replika_id
            or source.get("team_id") != team_id
            or not hmac.compare_digest(str(source_event.get("channel") or ""), channel)
            or source_event.get("thread_ts") != thread_ts
            or (
                placeholder_ts
                and not hmac.compare_digest(
                    str(source.get("placeholder_ts") or ""), placeholder_ts
                )
            )
        ):
            return jsonify({"error": "Original Slack target does not match"}), 403
        if create_placeholder:
            existing = str(source.get("placeholder_ts") or "")
            if existing:
                return jsonify({"placeholder_ts": existing})
            params = {"channel": channel, "text": "_thinking…_"}
            if thread_ts:
                params["thread_ts"] = thread_ts
            result = _slack().call(
                "chat.postMessage",
                token=_vault().get(installation["token_ref"]),
                **params,
            )
            created = str(result.get("ts") or "")
            if not created:
                raise RuntimeError("Slack did not return a placeholder timestamp")
            if _store().set_runtime_placeholder(source_dedupe_key, created):
                return jsonify({"placeholder_ts": created})
            winner = str(
                (_store().outbox_item(source_dedupe_key) or {}).get("placeholder_ts")
                or ""
            )
            try:
                _slack().call(
                    "chat.delete",
                    token=_vault().get(installation["token_ref"]),
                    channel=channel,
                    ts=created,
                )
            except RuntimeError:
                current_app.logger.warning(
                    "Could not delete duplicate Slack placeholder", exc_info=True
                )
            if winner:
                return jsonify({"placeholder_ts": winner})
            raise RuntimeError("Could not persist Slack placeholder")
        dedupe_key = "outbound:" + str(data.get("dedupe_key") or "")
        if dedupe_key == "outbound:":
            return jsonify({"error": "dedupe_key is required"}), 400
        accepted = _store().enqueue(
            dedupe_key,
            installation,
            "outbound",
            {
                "channel": channel,
                "thread_ts": thread_ts,
                "text": text,
                "placeholder_ts": placeholder_ts or None,
                "delete_placeholder": delete_placeholder,
            },
        )
        return jsonify({"accepted": accepted}), 202

    app.register_blueprint(bp)

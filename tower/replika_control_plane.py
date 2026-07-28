"""Provider-managed Replika onboarding and provisioning control plane."""

from __future__ import annotations

import json
import os
import re
import hmac
from datetime import datetime, timezone
from typing import Any

from flask import Blueprint, abort, current_app, g, jsonify, render_template, request, session
from pymongo import ASCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

from .auth import SESSION_USER_KEY

COLLECTION = "replikas"
USERNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,30}[a-z0-9])?$")
REPLIKA_TYPES = frozenset({"organization", "individual"})
RESERVED_USERNAMES = frozenset(
    {
        "admin",
        "api",
        "app",
        "assets",
        "auth",
        "billing",
        "help",
        "login",
        "replika",
        "settings",
        "signup",
        "status",
        "support",
        "www",
    }
)
CUSTOMER_ERROR = "We could not create your Replika yet. Please try again."


class UsernameUnavailable(ValueError):
    pass


class ReplikaAlreadyExists(ValueError):
    pass


def local_provisioning_allowed() -> bool:
    return os.environ.get("REPLIKA_ALLOW_LOCAL_PROVISIONING", "").lower() in {
        "1",
        "true",
        "yes",
    }


def normalize_username(value: str) -> str:
    username = (value or "").strip().lower()
    if not USERNAME_RE.fullmatch(username):
        raise ValueError(
            "Username must be 3-32 characters using lowercase letters, numbers, or single hyphens."
        )
    if "--" in username:
        raise ValueError("Username cannot contain consecutive hyphens.")
    if username in RESERVED_USERNAMES:
        raise ValueError("That username is reserved.")
    return username


def normalize_replika_type(value: str) -> str:
    replika_type = (value or "").strip().lower()
    if replika_type not in REPLIKA_TYPES:
        raise ValueError("Replika type must be organization or individual.")
    return replika_type


def product_url(username: str) -> str:
    domain = os.environ.get("REPLIKA_PRODUCT_DOMAIN", "replika.local").strip().lower()
    return f"https://{username}.{domain}"


class ReplikaStore:
    """Mongo-backed source of truth for username ownership and lifecycle state."""

    def __init__(self, collection):
        self.collection = collection
        self.collection.create_index(
            [("username", ASCENDING)], unique=True, name="unique_replika_username"
        )
        self.collection.create_index(
            [("owner_id", ASCENDING)], unique=True, name="one_replika_per_owner"
        )

    def find_for_owner(self, owner_id: str) -> dict[str, Any] | None:
        return self.collection.find_one({"owner_id": owner_id})

    def username_available(self, username: str, owner_id: str | None = None) -> bool:
        existing = self.collection.find_one({"username": username}, {"owner_id": 1})
        return existing is None or (
            owner_id is not None and existing.get("owner_id") == owner_id
        )

    def reserve(
        self, owner_id: str, username: str, replika_type: str
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        document = {
            "_id": owner_id,
            "owner_id": owner_id,
            "username": username,
            "replika_type": replika_type,
            "status": "creating",
            "provisioning_requested_at": None,
            "product_url": product_url(username),
            "created_at": now,
            "updated_at": now,
            "release_version": os.environ.get("REPLIKA_RELEASE_VERSION", "v0"),
        }
        try:
            self.collection.insert_one(document)
            return document
        except DuplicateKeyError:
            current = self.find_for_owner(owner_id)
            if (
                current
                and current.get("username") == username
                and current.get("replika_type") == replika_type
            ):
                return current
            if current:
                raise ReplikaAlreadyExists("This account already has a Replika.")
            raise UsernameUnavailable("That username is not available.")

    def update_status(
        self,
        owner_id: str,
        status: str,
        *,
        internal_error: str | None = None,
    ) -> dict[str, Any] | None:
        changes: dict[str, Any] = {
            "status": status,
            "updated_at": datetime.now(timezone.utc),
        }
        if internal_error:
            changes["internal_error"] = internal_error[:2000]
        else:
            changes["internal_error"] = None
        if status == "creating":
            changes["provisioning_requested_at"] = None
        return self.collection.find_one_and_update(
            {"owner_id": owner_id},
            {"$set": changes},
            return_document=ReturnDocument.AFTER,
        )

    def claim_provisioning(self, owner_id: str) -> dict[str, Any] | None:
        return self.collection.find_one_and_update(
            {
                "owner_id": owner_id,
                "status": "creating",
                "provisioning_requested_at": None,
            },
            {"$set": {"provisioning_requested_at": datetime.now(timezone.utc)}},
            return_document=ReturnDocument.AFTER,
        )


class Provisioner:
    """Invokes the private provider-side provisioner without exposing AWS."""

    def __init__(self, function_arn: str | None = None, lambda_client=None):
        self.function_arn = function_arn or os.environ.get(
            "REPLIKA_PROVISIONER_FUNCTION_ARN"
        )
        self._lambda_client = lambda_client

    def start(self, replika: dict[str, Any]) -> None:
        if not self.function_arn:
            if (
                os.environ.get("REPLIKA_PROVISIONING_MODE", "local") == "local"
                and local_provisioning_allowed()
            ):
                return
            raise RuntimeError("provider provisioner is not configured")
        client = self._lambda_client
        if client is None:
            import boto3

            client = boto3.client("lambda")
        payload = {
            "owner_id": replika["owner_id"],
            "username": replika["username"],
            "replika_type": replika["replika_type"],
            "product_url": replika["product_url"],
            "release_version": replika["release_version"],
        }
        response = client.invoke(
            FunctionName=self.function_arn,
            InvocationType="Event",
            Payload=json.dumps(payload).encode("utf-8"),
        )
        if response.get("StatusCode") != 202:
            raise RuntimeError("provider provisioner rejected the request")


_sync_db = None
_sync_store = None


def _store() -> ReplikaStore:
    injected = current_app.config.get("REPLIKA_STORE")
    if injected is not None:
        return injected
    global _sync_db, _sync_store
    if _sync_store is not None:
        return _sync_store
    if _sync_db is None:
        uri = os.environ.get("MONGO_URI")
        name = os.environ.get("MONGO_DB")
        if not uri or not name:
            raise RuntimeError("Replika control plane storage is not configured")
        _sync_db = MongoClient(uri)[name]
    _sync_store = ReplikaStore(_sync_db[COLLECTION])
    return _sync_store


def _provisioner() -> Provisioner:
    return current_app.config.get("REPLIKA_PROVISIONER") or Provisioner()


def replika_type_for_owner(owner_id: str) -> str | None:
    document = _store().find_for_owner(owner_id)
    return document.get("replika_type") if document else None


def _owner_id() -> str:
    auth_result = getattr(g, "tower_auth", None)
    owner_id = getattr(auth_result, "username", None) or session.get(SESSION_USER_KEY)
    if not owner_id:
        raise PermissionError("Authenticated account required")
    return str(owner_id)


def _customer_view(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "username": document["username"],
        "replika_type": document["replika_type"],
        "status": document["status"],
        "url": document["product_url"],
        "release": document.get("release_version"),
        "message": CUSTOMER_ERROR if document["status"] == "error" else None,
    }


def _callback_authenticated() -> bool:
    expected = os.environ.get("REPLIKA_PROVISIONER_CALLBACK_TOKEN", "")
    supplied = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    return bool(expected) and hmac.compare_digest(expected, supplied)


def register_replika_control_plane(app) -> None:
    bp = Blueprint("replika_control_plane", __name__)

    @bp.before_request
    def control_plane_only():
        if os.environ.get("REPLIKA_CONTROL_PLANE_ONLY", "").lower() not in {
            "1", "true", "yes",
        }:
            abort(404)

    @bp.get("/replika")
    def replika_setup():
        try:
            document = _store().find_for_owner(_owner_id())
        except RuntimeError:
            document = None
        return render_template(
            "replika/setup.html",
            replika=_customer_view(document) if document else None,
            product_domain=os.environ.get(
                "REPLIKA_PRODUCT_DOMAIN", "replika.local"
            ).strip().lower(),
        )

    @bp.get("/api/replika")
    def get_replika():
        try:
            document = _store().find_for_owner(_owner_id())
        except PermissionError:
            return jsonify({"error": "Unauthorized"}), 401
        except RuntimeError:
            return jsonify({"error": "Replika service is temporarily unavailable"}), 503
        if not document:
            return jsonify({"replika": None})
        return jsonify({"replika": _customer_view(document)})

    @bp.get("/api/replika/username/<username>")
    def username_available(username: str):
        try:
            normalized = normalize_username(username)
            available = _store().username_available(normalized, _owner_id())
        except ValueError as exc:
            return jsonify({"available": False, "error": str(exc)}), 400
        except PermissionError:
            return jsonify({"error": "Unauthorized"}), 401
        except RuntimeError:
            return jsonify({"error": "Replika service is temporarily unavailable"}), 503
        return jsonify({"username": normalized, "available": available})

    @bp.post("/api/replika")
    def create_replika():
        try:
            owner_id = _owner_id()
            data = request.get_json(silent=True) or {}
            username = normalize_username(data.get("username", ""))
            replika_type = normalize_replika_type(data.get("replika_type", ""))
            document = _store().reserve(owner_id, username, replika_type)
            if document["status"] == "error":
                document = _store().update_status(owner_id, "creating") or document
            claimed = (
                _store().claim_provisioning(owner_id)
                if document["status"] == "creating"
                else None
            )
            if claimed:
                document = claimed
                _provisioner().start(claimed)
                if (
                    os.environ.get("REPLIKA_PROVISIONING_MODE", "local") == "local"
                    and local_provisioning_allowed()
                ):
                    document = _store().update_status(owner_id, "ready") or document
            return jsonify({"replika": _customer_view(document)}), 202
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 409 if isinstance(
                exc, (UsernameUnavailable, ReplikaAlreadyExists)
            ) else 400
        except PermissionError:
            return jsonify({"error": "Unauthorized"}), 401
        except Exception as exc:
            current_app.logger.exception("Replika provisioning failed")
            try:
                document = _store().update_status(_owner_id(), "error", internal_error=str(exc))
            except Exception:
                document = None
            body = {"error": CUSTOMER_ERROR}
            if document:
                body["replika"] = _customer_view(document)
            return jsonify(body), 503

    @bp.post("/internal/replika/provisioning")
    def provisioning_callback():
        if not _callback_authenticated():
            return jsonify({"error": "Unauthorized"}), 401
        data = request.get_json(silent=True) or {}
        owner_id = str(data.get("owner_id") or "")
        status = str(data.get("status") or "")
        if not owner_id or status not in {"ready", "error"}:
            return jsonify({"error": "Invalid callback"}), 400
        document = _store().update_status(
            owner_id,
            status,
            internal_error=str(data.get("error") or "") or None,
        )
        if not document:
            return jsonify({"error": "Unknown Replika"}), 404
        return jsonify({"status": "accepted"})

    @bp.post("/internal/replika/database")
    def provision_database_identity():
        if not _callback_authenticated():
            return jsonify({"error": "Unauthorized"}), 401
        data = request.get_json(silent=True) or {}
        owner_id = str(data.get("owner_id") or "")
        task_role_arn = str(data.get("task_role_arn") or "")
        if not owner_id or not task_role_arn.startswith("arn:aws:iam::"):
            return jsonify({"error": "Invalid database identity request"}), 400
        try:
            from harness.tenant_database import ensure_tenant_identity

            return jsonify(ensure_tenant_identity(owner_id, task_role_arn))
        except Exception:
            current_app.logger.exception("Tenant database identity provisioning failed")
            return jsonify({"error": "Database provisioning failed"}), 503

    app.register_blueprint(bp)

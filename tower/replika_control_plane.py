"""Provider-managed Replika onboarding and provisioning control plane."""

from __future__ import annotations

import json
import os
import re
import hmac
import uuid
from datetime import datetime, timezone
from typing import Any

from flask import Blueprint, abort, current_app, g, jsonify, render_template, request, session
from pymongo import ASCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure

from .auth import SESSION_USER_KEY

COLLECTION = "replikas"
USERNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,30}[a-z0-9])?$")
REPLIKA_TYPES = frozenset({"organization", "individual"})
CALLBACK_STATUSES = frozenset({"ready", "error", "deleted", "stopped"})
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
CUSTOMER_DELETE_ERROR = "We could not delete your Replika yet. Please try again."
STATUS_LABELS = {
    "creating": "Provisioning",
    "ready": "Provisioned",
    "error": "Unavailable",
    "deleting": "Deleting",
    "stopped": "Stopped",
    "stopping": "Stopping",
    "starting": "Starting",
    "restarting": "Restarting",
}


class UsernameUnavailable(ValueError):
    pass


class ReplikaNotFound(LookupError):
    pass


class ReplikaConflict(ValueError):
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


def new_replika_id() -> str:
    return str(uuid.uuid4())


class ReplikaStore:
    """Mongo-backed source of truth for username ownership and lifecycle state."""

    def __init__(self, collection):
        self.collection = collection
        self.collection.create_index(
            [("username", ASCENDING)], unique=True, name="unique_replika_username"
        )
        self._ensure_owner_index()
        self._migrate_legacy_documents()

    def _ensure_owner_index(self) -> None:
        for name in ("one_replika_per_owner", "replika_owner"):
            try:
                self.collection.drop_index(name)
            except OperationFailure:
                pass
        self.collection.create_index(
            [("owner_id", ASCENDING)], unique=False, name="replika_owner"
        )

    def _migrate_legacy_documents(self) -> None:
        """Ensure legacy owner-keyed documents expose an explicit replika_id."""
        for document in self.collection.find(
            {"$or": [{"replika_id": {"$exists": False}}, {"replika_id": None}]}
        ):
            self.collection.update_one(
                {"_id": document["_id"]},
                {"$set": {"replika_id": str(document["_id"])}},
            )

    def find_by_id(self, replika_id: str) -> dict[str, Any] | None:
        return self.collection.find_one(
            {"$or": [{"_id": replika_id}, {"replika_id": replika_id}]}
        )

    def find_owned(self, replika_id: str, owner_id: str) -> dict[str, Any] | None:
        document = self.find_by_id(replika_id)
        if not document or document.get("owner_id") != owner_id:
            return None
        return document

    def list_for_owner(self, owner_id: str) -> list[dict[str, Any]]:
        return list(
            self.collection.find({"owner_id": owner_id}).sort(
                [("created_at", ASCENDING)]
            )
        )

    def find_for_owner(self, owner_id: str) -> dict[str, Any] | None:
        """Compatibility helper: return the owner's oldest Replika if any."""
        documents = self.list_for_owner(owner_id)
        return documents[0] if documents else None

    def username_available(self, username: str, owner_id: str | None = None) -> bool:
        existing = self.collection.find_one({"username": username}, {"owner_id": 1})
        return existing is None

    def reserve(
        self, owner_id: str, username: str, replika_type: str
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        replika_id = new_replika_id()
        document = {
            "_id": replika_id,
            "replika_id": replika_id,
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
            raise UsernameUnavailable("That username is not available.")

    def update_status(
        self,
        replika_id: str,
        status: str,
        *,
        internal_error: str | None = None,
        owner_id: str | None = None,
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
        query: dict[str, Any] = {
            "$or": [{"_id": replika_id}, {"replika_id": replika_id}]
        }
        if owner_id is not None:
            query = {
                "$and": [
                    query,
                    {"owner_id": owner_id},
                ]
            }
        return self.collection.find_one_and_update(
            query,
            {"$set": changes},
            return_document=ReturnDocument.AFTER,
        )

    def claim_provisioning(self, replika_id: str) -> dict[str, Any] | None:
        return self.collection.find_one_and_update(
            {
                "$or": [{"_id": replika_id}, {"replika_id": replika_id}],
                "status": "creating",
                "provisioning_requested_at": None,
            },
            {"$set": {"provisioning_requested_at": datetime.now(timezone.utc)}},
            return_document=ReturnDocument.AFTER,
        )

    def claim_deletion(self, replika_id: str, owner_id: str) -> dict[str, Any] | None:
        return self.collection.find_one_and_update(
            {
                "$or": [{"_id": replika_id}, {"replika_id": replika_id}],
                "owner_id": owner_id,
                "status": {"$in": ["ready", "error", "creating", "deleting", "stopped"]},
            },
            {
                "$set": {
                    "status": "deleting",
                    "updated_at": datetime.now(timezone.utc),
                    "internal_error": None,
                    "deletion_requested_at": datetime.now(timezone.utc),
                }
            },
            return_document=ReturnDocument.AFTER,
        )

    def delete_record(self, replika_id: str) -> bool:
        result = self.collection.delete_one(
            {"$or": [{"_id": replika_id}, {"replika_id": replika_id}]}
        )
        return bool(result.deleted_count)


class Provisioner:
    """Invokes the private provider-side provisioner without exposing AWS."""

    def __init__(self, function_arn: str | None = None, lambda_client=None):
        self.function_arn = function_arn or os.environ.get(
            "REPLIKA_PROVISIONER_FUNCTION_ARN"
        )
        self._lambda_client = lambda_client

    def _payload(self, replika: dict[str, Any], operation: str) -> dict[str, Any]:
        replika_id = str(replika.get("replika_id") or replika["_id"])
        return {
            "operation": operation,
            "replika_id": replika_id,
            "owner_id": replika["owner_id"],
            "username": replika["username"],
            "replika_type": replika["replika_type"],
            "product_url": replika["product_url"],
            "release_version": replika.get("release_version") or "v0",
        }

    def start(self, replika: dict[str, Any]) -> None:
        self._invoke(replika, "create")

    def delete(self, replika: dict[str, Any]) -> None:
        self._invoke(replika, "delete")

    def stop(self, replika: dict[str, Any]) -> None:
        self._invoke(replika, "stop")

    def start_replika(self, replika: dict[str, Any]) -> None:
        self._invoke(replika, "start")

    def restart(self, replika: dict[str, Any]) -> None:
        self._invoke(replika, "restart")

    def reset_config(self, replika: dict[str, Any]) -> dict[str, Any]:
        """Force-overwrite this Replika's persisted config/ files with the
        latest defaults baked into its currently deployed image. Not a
        lifecycle transition, so this waits for the result instead of firing
        an async request like start()/delete()."""
        return self._invoke_sync(replika, "reset_config")

    def _invoke(self, replika: dict[str, Any], operation: str) -> None:
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
        response = client.invoke(
            FunctionName=self.function_arn,
            InvocationType="Event",
            Payload=json.dumps(self._payload(replika, operation)).encode("utf-8"),
        )
        if response.get("StatusCode") != 202:
            raise RuntimeError("provider provisioner rejected the request")

    def _invoke_sync(self, replika: dict[str, Any], operation: str) -> dict[str, Any]:
        if not self.function_arn:
            if (
                os.environ.get("REPLIKA_PROVISIONING_MODE", "local") == "local"
                and local_provisioning_allowed()
            ):
                return {"status": "skipped"}
            raise RuntimeError("provider provisioner is not configured")
        client = self._lambda_client
        if client is None:
            import boto3

            client = boto3.client("lambda")
        response = client.invoke(
            FunctionName=self.function_arn,
            InvocationType="RequestResponse",
            Payload=json.dumps(self._payload(replika, operation)).encode("utf-8"),
        )
        payload = json.loads(response["Payload"].read() or b"{}")
        if response.get("FunctionError"):
            raise RuntimeError(
                payload.get("errorMessage") or "provider provisioner reported an error"
            )
        return payload


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


def replika_id_of(document: dict[str, Any]) -> str:
    return str(document.get("replika_id") or document["_id"])


def replika_type_for_owner(owner_id: str) -> str | None:
    document = _store().find_for_owner(owner_id)
    return document.get("replika_type") if document else None


def replika_type_for_id(replika_id: str) -> str | None:
    document = _store().find_by_id(replika_id)
    return document.get("replika_type") if document else None


def _owner_id() -> str:
    auth_result = getattr(g, "tower_auth", None)
    owner_id = getattr(auth_result, "username", None) or session.get(SESSION_USER_KEY)
    if not owner_id:
        raise PermissionError("Authenticated account required")
    return str(owner_id)


def _customer_view(document: dict[str, Any]) -> dict[str, Any]:
    status = document["status"]
    message = None
    if status == "error":
        message = CUSTOMER_ERROR
    elif status == "deleting":
        message = "Deleting your Replika…"
    elif status == "stopping":
        message = "Stopping your Replika…"
    elif status == "starting":
        message = "Starting your Replika…"
    elif status == "restarting":
        message = "Restarting your Replika…"
    return {
        "id": replika_id_of(document),
        "username": document["username"],
        "replika_type": document["replika_type"],
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "url": document["product_url"] if status == "ready" else None,
        "release": document.get("release_version"),
        "message": message,
    }


def _callback_authenticated() -> bool:
    expected = os.environ.get("REPLIKA_PROVISIONER_CALLBACK_TOKEN", "")
    supplied = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    return bool(expected) and hmac.compare_digest(expected, supplied)


def _purge_slack_for_replika(replika_id: str) -> None:
    purge = current_app.config.get("REPLIKA_SLACK_PURGE")
    if purge is not None:
        purge(replika_id)
        return
    try:
        from .slack_integration import purge_replika_slack

        purge_replika_slack(replika_id)
    except RuntimeError as exc:
        # Slack may be optional in local/dev control-plane setups.
        if "not configured" in str(exc).lower():
            current_app.logger.info(
                "Skipping Slack purge for %s: %s", replika_id, exc
            )
            return
        current_app.logger.exception(
            "Failed to purge Slack state for Replika %s", replika_id
        )
        raise
    except Exception:
        current_app.logger.exception(
            "Failed to purge Slack state for Replika %s", replika_id
        )
        raise


def _start_create(document: dict[str, Any]) -> dict[str, Any]:
    replika_id = replika_id_of(document)
    if document["status"] == "error":
        document = _store().update_status(replika_id, "creating") or document
    claimed = (
        _store().claim_provisioning(replika_id)
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
            document = _store().update_status(replika_id, "ready") or document
    return document


def _start_delete(document: dict[str, Any], owner_id: str) -> dict[str, Any]:
    replika_id = replika_id_of(document)
    claimed = _store().claim_deletion(replika_id, owner_id)
    if not claimed:
        raise ReplikaNotFound("Unknown Replika")
    document = claimed
    _purge_slack_for_replika(replika_id)
    _provisioner().delete(document)
    if (
        os.environ.get("REPLIKA_PROVISIONING_MODE", "local") == "local"
        and local_provisioning_allowed()
    ):
        _store().delete_record(replika_id)
        return {**document, "status": "deleted"}
    return document


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
            documents = _store().list_for_owner(_owner_id())
        except RuntimeError:
            documents = []
        return render_template(
            "replika/setup.html",
            replikas=[_customer_view(document) for document in documents],
            product_domain=os.environ.get(
                "REPLIKA_PRODUCT_DOMAIN", "replika.local"
            ).strip().lower(),
        )

    @bp.get("/api/replikas")
    @bp.get("/api/replika")
    def list_replikas():
        try:
            documents = _store().list_for_owner(_owner_id())
        except PermissionError:
            return jsonify({"error": "Unauthorized"}), 401
        except RuntimeError:
            return jsonify({"error": "Replika service is temporarily unavailable"}), 503
        views = [_customer_view(document) for document in documents]
        # Keep singular key for older clients while exposing the collection.
        return jsonify({
            "replikas": views,
            "replika": views[0] if len(views) == 1 else None,
        })

    @bp.get("/api/replikas/<replika_id>")
    def get_replika(replika_id: str):
        try:
            document = _store().find_owned(replika_id, _owner_id())
        except PermissionError:
            return jsonify({"error": "Unauthorized"}), 401
        except RuntimeError:
            return jsonify({"error": "Replika service is temporarily unavailable"}), 503
        if not document:
            return jsonify({"error": "Unknown Replika"}), 404
        return jsonify({"replika": _customer_view(document)})

    @bp.get("/api/replika/username/<username>")
    @bp.get("/api/replikas/username/<username>")
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

    @bp.post("/api/replikas")
    @bp.post("/api/replika")
    def create_replika():
        try:
            owner_id = _owner_id()
            data = request.get_json(silent=True) or {}
            username = normalize_username(data.get("username", ""))
            replika_type = normalize_replika_type(data.get("replika_type", ""))
            retry_id = str(data.get("replika_id") or "").strip()
            if retry_id:
                document = _store().find_owned(retry_id, owner_id)
                if not document:
                    return jsonify({"error": "Unknown Replika"}), 404
                if document["status"] == "deleting":
                    raise ReplikaConflict("That Replika is being deleted.")
                if (
                    document.get("username") != username
                    or document.get("replika_type") != replika_type
                ):
                    raise ReplikaConflict("Replika identity is immutable.")
            else:
                document = _store().reserve(owner_id, username, replika_type)
            document = _start_create(document)
            return jsonify({"replika": _customer_view(document)}), 202
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 409 if isinstance(
                exc, (UsernameUnavailable, ReplikaConflict)
            ) else 400
        except PermissionError:
            return jsonify({"error": "Unauthorized"}), 401
        except Exception as exc:
            current_app.logger.exception("Replika provisioning failed")
            document = None
            try:
                owner_id = _owner_id()
                data = request.get_json(silent=True) or {}
                retry_id = str(data.get("replika_id") or "").strip()
                target_id = retry_id
                if not target_id:
                    # Best-effort: mark the newest creating Replika for this owner.
                    owned = _store().list_for_owner(owner_id)
                    creating = [
                        row for row in owned if row.get("status") == "creating"
                    ]
                    if creating:
                        target_id = replika_id_of(creating[-1])
                if target_id:
                    document = _store().update_status(
                        target_id, "error", internal_error=str(exc), owner_id=owner_id
                    )
            except Exception:
                document = None
            body = {"error": CUSTOMER_ERROR}
            if document:
                body["replika"] = _customer_view(document)
            return jsonify(body), 503

    @bp.delete("/api/replikas/<replika_id>")
    def delete_replika(replika_id: str):
        try:
            owner_id = _owner_id()
            document = _store().find_owned(replika_id, owner_id)
            if not document:
                return jsonify({"error": "Unknown Replika"}), 404
            document = _start_delete(document, owner_id)
            if document.get("status") == "deleted":
                return jsonify({"deleted": True, "id": replika_id})
            return jsonify({"replika": _customer_view(document)}), 202
        except PermissionError:
            return jsonify({"error": "Unauthorized"}), 401
        except ReplikaNotFound:
            return jsonify({"error": "Unknown Replika"}), 404
        except Exception as exc:
            current_app.logger.exception("Replika deletion failed")
            try:
                document = _store().update_status(
                    replika_id,
                    "deleting",
                    internal_error=str(exc),
                    owner_id=_owner_id(),
                )
            except Exception:
                document = None
            body = {"error": CUSTOMER_DELETE_ERROR}
            if document:
                body["replika"] = _customer_view(document)
            return jsonify(body), 503

    @bp.post("/api/replikas/<replika_id>/reset-config")
    def reset_replika_config(replika_id: str):
        try:
            owner_id = _owner_id()
            document = _store().find_owned(replika_id, owner_id)
            if not document:
                return jsonify({"error": "Unknown Replika"}), 404
            if document["status"] != "ready":
                return jsonify(
                    {"error": "That Replika must be ready before resetting its config."}
                ), 409
            _provisioner().reset_config(document)
            return jsonify({"status": "ok"})
        except PermissionError:
            return jsonify({"error": "Unauthorized"}), 401
        except Exception:
            current_app.logger.exception("Replika config reset failed")
            return jsonify(
                {"error": "We could not reset this Replika's config. Please try again."}
            ), 503

    @bp.post("/api/replikas/<replika_id>/stop")
    def stop_replika(replika_id: str):
        try:
            owner_id = _owner_id()
            document = _store().find_owned(replika_id, owner_id)
            if not document:
                return jsonify({"error": "Unknown Replika"}), 404
            _provisioner().stop(document)
            _store().update_status(replika_id, "stopping")
            return jsonify({"status": "stopping"})
        except PermissionError:
            return jsonify({"error": "Unauthorized"}), 401
        except Exception:
            current_app.logger.exception("Replika stop failed")
            return jsonify({"error": "We could not stop this Replika. Please try again."}), 503

    @bp.post("/api/replikas/<replika_id>/start")
    def start_replika(replika_id: str):
        try:
            owner_id = _owner_id()
            document = _store().find_owned(replika_id, owner_id)
            if not document:
                return jsonify({"error": "Unknown Replika"}), 404
            _provisioner().start_replika(document)
            _store().update_status(replika_id, "starting")
            return jsonify({"status": "starting"})
        except PermissionError:
            return jsonify({"error": "Unauthorized"}), 401
        except Exception:
            current_app.logger.exception("Replika start failed")
            return jsonify({"error": "We could not start this Replika. Please try again."}), 503

    @bp.post("/api/replikas/<replika_id>/restart")
    def restart_replika(replika_id: str):
        try:
            owner_id = _owner_id()
            document = _store().find_owned(replika_id, owner_id)
            if not document:
                return jsonify({"error": "Unknown Replika"}), 404
            _provisioner().restart(document)
            _store().update_status(replika_id, "restarting")
            return jsonify({"status": "restarting"})
        except PermissionError:
            return jsonify({"error": "Unauthorized"}), 401
        except Exception:
            current_app.logger.exception("Replika restart failed")
            return jsonify({"error": "We could not restart this Replika. Please try again."}), 503

    @bp.post("/internal/replika/provisioning")
    def provisioning_callback():
        if not _callback_authenticated():
            return jsonify({"error": "Unauthorized"}), 401
        data = request.get_json(silent=True) or {}
        replika_id = str(data.get("replika_id") or data.get("owner_id") or "")
        status = str(data.get("status") or "")
        if not replika_id or status not in CALLBACK_STATUSES:
            return jsonify({"error": "Invalid callback"}), 400
        if status == "deleted":
            _store().delete_record(replika_id)
            return jsonify({"status": "accepted"})
        document = _store().update_status(
            replika_id,
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
        replika_id = str(data.get("replika_id") or data.get("owner_id") or "")
        task_role_arn = str(data.get("task_role_arn") or "")
        action = str(data.get("action") or "create").strip().lower()
        if not replika_id or (
            action == "create" and not task_role_arn.startswith("arn:aws:iam::")
        ):
            return jsonify({"error": "Invalid database identity request"}), 400
        try:
            from harness.tenant_database import (
                delete_tenant_identity,
                ensure_tenant_identity,
            )

            if action == "delete":
                delete_tenant_identity(
                    replika_id,
                    task_role_arn=task_role_arn or None,
                )
                return jsonify({"status": "deleted", "mongo_db": None})
            return jsonify(ensure_tenant_identity(replika_id, task_role_arn))
        except Exception:
            current_app.logger.exception("Tenant database identity provisioning failed")
            return jsonify({"error": "Database provisioning failed"}), 503

    app.register_blueprint(bp)

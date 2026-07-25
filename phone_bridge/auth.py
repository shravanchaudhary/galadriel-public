"""Persistent enrollment and ECDSA authentication for the phone bridge."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import threading
import uuid

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

AUTH_CONTEXT = b"galadriel-phone-bridge-v1:"
DEFAULT_CODE_TTL_SECONDS = 600
DEFAULT_SESSION_SECONDS = 3600


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def auth_store_path() -> Path:
    configured = os.environ.get("PHONE_BRIDGE_AUTH_STORE")
    if configured:
        return Path(configured)
    root = Path(os.environ.get("GALADRIEL_STORAGE_ROOT", "."))
    return root / "state" / "phone_bridge_auth.json"


def current_tenant_id() -> str:
    return os.environ.get("REPLIKA_TENANT_ID", "default")


def challenge_payload(nonce: str) -> bytes:
    return AUTH_CONTEXT + nonce.encode("ascii")


class AuthStore:
    """Small atomic JSON store for one tenant's enrolled phones."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        if not isinstance(data, dict):
            raise RuntimeError("Phone bridge auth store is malformed")
        data.setdefault("codes", [])
        data.setdefault("devices", {})
        return data

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        replacement = self.path.with_name(
            f".{self.path.name}.{secrets.token_hex(6)}.tmp"
        )
        try:
            descriptor = os.open(
                replacement,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(replacement, self.path)
            os.chmod(self.path, 0o600)
        finally:
            replacement.unlink(missing_ok=True)

    @staticmethod
    def _code_hash(code: str) -> str:
        return hashlib.sha256(code.encode("ascii")).hexdigest()

    def create_enrollment_code(
        self,
        tenant_id: str,
        *,
        ttl_seconds: int = DEFAULT_CODE_TTL_SECONDS,
    ) -> tuple[str, datetime]:
        code = f"{secrets.randbelow(100_000_000):08d}"
        expires_at = _utc_now() + timedelta(seconds=ttl_seconds)
        with self._lock:
            data = self._read()
            now = _utc_now()
            data["codes"] = [
                item
                for item in data["codes"]
                if _parse_time(item["expires_at"]) > now
            ]
            data["codes"].append({
                "tenant_id": tenant_id,
                "code_hash": self._code_hash(code),
                "expires_at": _iso(expires_at),
            })
            self._write(data)
        return code, expires_at

    def enroll(
        self,
        tenant_id: str,
        code: str,
        public_key_der_b64: str,
        nonce: str,
        signature_b64: str,
    ) -> str:
        if len(code) != 8 or not code.isdigit():
            raise ValueError("Invalid enrollment code")
        public_key = _load_public_key(public_key_der_b64)
        _verify_signature(public_key, nonce, signature_b64)
        now = _utc_now()
        code_hash = self._code_hash(code)
        with self._lock:
            data = self._read()
            match = next(
                (
                    item
                    for item in data["codes"]
                    if item["tenant_id"] == tenant_id
                    and secrets.compare_digest(item["code_hash"], code_hash)
                    and _parse_time(item["expires_at"]) > now
                ),
                None,
            )
            if match is None:
                raise ValueError("Invalid or expired enrollment code")
            data["codes"].remove(match)
            device_id = f"phone_{uuid.uuid4().hex}"
            data["devices"][device_id] = {
                "tenant_id": tenant_id,
                "public_key": public_key_der_b64,
                "enrolled_at": _iso(now),
                "revoked_at": None,
            }
            self._write(data)
        return device_id

    def authenticate(
        self,
        tenant_id: str,
        device_id: str,
        nonce: str,
        signature_b64: str,
    ) -> None:
        with self._lock:
            device = self._read()["devices"].get(device_id)
        if (
            not device
            or device.get("tenant_id") != tenant_id
            or device.get("revoked_at") is not None
        ):
            raise ValueError("Unknown or revoked device")
        public_key = _load_public_key(device["public_key"])
        _verify_signature(public_key, nonce, signature_b64)

    def device_active(self, tenant_id: str, device_id: str) -> bool:
        with self._lock:
            device = self._read()["devices"].get(device_id)
        return bool(
            device
            and device.get("tenant_id") == tenant_id
            and device.get("revoked_at") is None
        )

    def list_devices(self, tenant_id: str) -> list[dict]:
        with self._lock:
            devices = self._read()["devices"]
        return [
            {
                "device_id": device_id,
                "enrolled_at": record.get("enrolled_at"),
                "revoked_at": record.get("revoked_at"),
            }
            for device_id, record in devices.items()
            if record.get("tenant_id") == tenant_id
        ]

    def revoke_device(self, tenant_id: str, device_id: str) -> bool:
        with self._lock:
            data = self._read()
            device = data["devices"].get(device_id)
            if not device or device.get("tenant_id") != tenant_id:
                return False
            if device.get("revoked_at") is None:
                device["revoked_at"] = _iso(_utc_now())
                self._write(data)
            return True


def _load_public_key(value: str) -> ec.EllipticCurvePublicKey:
    try:
        raw = base64.b64decode(value, validate=True)
        key = serialization.load_der_public_key(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid public key") from exc
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(
        key.curve, ec.SECP256R1
    ):
        raise ValueError("Public key must be ECDSA P-256")
    return key


def _verify_signature(
    public_key: ec.EllipticCurvePublicKey,
    nonce: str,
    signature_b64: str,
) -> None:
    try:
        signature = base64.b64decode(signature_b64, validate=True)
        public_key.verify(signature, challenge_payload(nonce), ec.ECDSA(hashes.SHA256()))
    except (ValueError, InvalidSignature) as exc:
        raise ValueError("Invalid device signature") from exc


_stores: dict[Path, AuthStore] = {}
_stores_lock = threading.Lock()


def get_auth_store(path: Path | None = None) -> AuthStore:
    resolved = (path or auth_store_path()).resolve()
    with _stores_lock:
        store = _stores.get(resolved)
        if store is None:
            store = AuthStore(resolved)
            _stores[resolved] = store
        return store

"""Shared browser profile management and live connection status."""

from __future__ import annotations

import os
import urllib.request

from . import browser_profiles
from .bce_client import BCEClient


def configured_backend() -> str:
    value = os.environ.get("BROWSER_BACKEND", "browser-use").strip().lower()
    if value in {"browser_use", "browseruse"}:
        return "browser-use"
    if value in {"browser-command-executor", "browser_command_executor"}:
        return "bce"
    return value


def _implicit_main() -> dict:
    backend = configured_backend()
    profile = {"profile_id": "main", "backend": backend, "source": "environment"}
    if backend == "bce":
        code = os.environ.get("BCE_PAIRING_CODE", "").strip()
        if code:
            profile["pairing_code"] = code
    elif backend == "browser-use":
        profile["cdp_port"] = int(os.environ.get("BROWSER_CDP_PORT", "9222"))
    return profile


def resolve(profile_id: str | None = None) -> dict | None:
    profile_id = (profile_id or "main").strip() or "main"
    try:
        profile = browser_profiles.get(profile_id)
    except RuntimeError:
        profile = None
    if profile:
        return profile
    if profile_id == "main":
        return _implicit_main()
    return None


def _public(profile: dict, *, include_pairing_code: bool = False) -> dict:
    result = {
        "profile_id": profile["profile_id"],
        "backend": profile["backend"],
        "purpose": profile.get("purpose", ""),
        "source": profile.get("source", "database"),
    }
    if profile["backend"] == "bce":
        result["configured"] = bool(profile.get("pairing_code"))
        if include_pairing_code and profile.get("pairing_code"):
            result["pairing_code"] = profile["pairing_code"]
    else:
        result["cdp_port"] = profile.get("cdp_port")
        result["configured"] = profile.get("cdp_port") is not None
    return result


def _bce_status(profile: dict) -> dict:
    code = profile.get("pairing_code")
    if not code:
        return {"state": "not_configured", "online": False}
    try:
        client = BCEClient(
            base_url=os.environ.get("BCE_BASE_URL", "http://localhost:8000"),
            api_key=os.environ.get("BCE_API_KEY", "dev-api-key"),
            pairing_code=code,
            default_timeout_ms=min(
                int(os.environ.get("BCE_TIMEOUT_MS", "10000")),
                2000,
            ),
        )
        device = client.device_status()
        return {
            "state": "online" if device.online else "offline",
            "online": device.online,
            "device_id": device.device_id,
            "device_name": device.name,
            "last_seen_at": device.last_seen_at,
        }
    except Exception as exc:
        return {
            "state": "unreachable",
            "online": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _local_status(profile: dict) -> dict:
    port = profile.get("cdp_port")
    if port is None:
        return {"state": "not_configured", "online": False}
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{int(port)}/json/version",
            timeout=1,
        ) as response:
            online = response.status == 200
    except Exception:
        online = False
    return {
        "state": "online" if online else "offline",
        "online": online,
        "cdp_port": int(port),
    }


def status(
    profile_id: str | None = None,
    *,
    include_pairing_code: bool = False,
) -> dict:
    profile_id = (profile_id or "main").strip() or "main"
    profile = resolve(profile_id)
    if not profile:
        return {
            "profile_id": profile_id,
            "state": "unknown_profile",
            "online": False,
            "error": "Browser profile is not configured",
        }
    base = _public(profile, include_pairing_code=include_pairing_code)
    live = (
        _bce_status(profile)
        if profile["backend"] == "bce"
        else _local_status(profile)
    )
    return {**base, **live}


def list_devices(
    *,
    include_status: bool = True,
    include_pairing_code: bool = False,
) -> list[dict]:
    profiles = browser_profiles.list_profiles()
    if not any(profile["profile_id"] == "main" for profile in profiles):
        profiles.append(_implicit_main())
    profiles.sort(key=lambda profile: profile["profile_id"])
    if include_status:
        return [
            status(
                profile["profile_id"],
                include_pairing_code=include_pairing_code,
            )
            for profile in profiles
        ]
    return [
        _public(profile, include_pairing_code=include_pairing_code)
        for profile in profiles
    ]


def connect(
    profile_id: str,
    *,
    backend: str | None = None,
    pairing_code: str | None = None,
    cdp_port: int | None = None,
    purpose: str | None = None,
) -> dict:
    saved = browser_profiles.upsert(
        profile_id,
        backend or configured_backend(),
        pairing_code=pairing_code,
        cdp_port=cdp_port,
        purpose=purpose,
    )
    return status(saved["profile_id"])


def remove(profile_id: str) -> dict:
    profile_id = browser_profiles.validate_profile_id(profile_id)
    return {"profile_id": profile_id, "removed": browser_profiles.delete(profile_id)}


def execute(action: str, **inputs) -> dict:
    action = (action or "").strip().lower()
    if action == "list":
        return {"devices": list_devices()}
    if action == "status":
        return status(inputs.get("profile_id"))
    if action == "connect":
        return connect(
            inputs.get("profile_id") or "main",
            backend=inputs.get("backend"),
            pairing_code=inputs.get("pairing_code"),
            cdp_port=inputs.get("cdp_port"),
            purpose=inputs.get("purpose"),
        )
    if action == "remove":
        return remove(inputs.get("profile_id") or "")
    raise ValueError("action must be list, status, connect, or remove")

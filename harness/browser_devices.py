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


def _implicit_main() -> dict | None:
    """Env-backed default profile, only when it is actually configured.

    Managed BCE tenants have no BCE_PAIRING_CODE — they must connect via
    browser_devices / Tower. Local browser-use still gets a CDP default.
    """
    backend = configured_backend()
    if backend == "bce":
        code = os.environ.get("BCE_PAIRING_CODE", "").strip()
        if not code:
            return None
        return {
            "profile_id": "main",
            "backend": "bce",
            "pairing_code": code,
            "source": "environment",
        }
    if backend == "browser-use":
        return {
            "profile_id": "main",
            "backend": "browser-use",
            "cdp_port": int(os.environ.get("BROWSER_CDP_PORT", "9222")),
            "source": "environment",
        }
    return None


def _effective_default_id(profiles: list[dict]) -> str | None:
    """Which profile currently plays the `main` role.

    `main` is a role, not an id. A browser explicitly flagged as default wins;
    otherwise, when exactly one browser is paired, that is unambiguously the one
    the user means. With several paired and none flagged, there is no honest
    answer — the user picks in Tower.
    """
    for profile in profiles:
        if profile.get("is_default"):
            return profile["profile_id"]
    return profiles[0]["profile_id"] if len(profiles) == 1 else None


def resolve(profile_id: str | None = None) -> dict | None:
    """One read, one rule: an exact id, else whichever browser holds the role.

    The returned row carries the *effective* `is_default`, so status, list, and
    the Tower badge can never disagree about which browser is main.
    """
    profile_id = (profile_id or "main").strip().lower() or "main"
    try:
        profiles = browser_profiles.list_profiles()
    except RuntimeError:
        profiles = []
    default_id = _effective_default_id(profiles)
    match = next(
        (profile for profile in profiles if profile["profile_id"] == profile_id), None
    )
    if match is None and profile_id == "main" and default_id:
        match = next(
            (profile for profile in profiles if profile["profile_id"] == default_id),
            None,
        )
    if match is not None:
        return {**match, "is_default": match["profile_id"] == default_id}
    if profile_id == "main":
        implicit = _implicit_main()
        return {**implicit, "is_default": True} if implicit else None
    return None


def _public(profile: dict, *, include_pairing_code: bool = False) -> dict:
    result = {
        "profile_id": profile["profile_id"],
        "backend": profile["backend"],
        "purpose": profile.get("purpose", ""),
        "source": profile.get("source", "database"),
        "is_default": bool(profile.get("is_default")),
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
            base_url=(os.environ.get("BCE_BASE_URL") or "").strip()
            or "http://localhost:8000",
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


def _status_for(profile: dict, *, include_pairing_code: bool = False) -> dict:
    """Live status for an already-loaded profile (no re-read)."""
    base = _public(profile, include_pairing_code=include_pairing_code)
    live = (
        _bce_status(profile)
        if profile["backend"] == "bce"
        else _local_status(profile)
    )
    return {**base, **live}


def _unresolved_reason(profile_id: str) -> str:
    if profile_id != "main":
        return f"Browser profile {profile_id!r} is not configured"
    try:
        paired = [profile["profile_id"] for profile in browser_profiles.list_profiles()]
    except RuntimeError:
        paired = []
    if paired:
        return (
            "No default browser is set, and several are paired ("
            + ", ".join(paired)
            + "). Set one as main in Tower (Devices > Browser) or with "
            "browser_devices action=set_default, or pass profile_id."
        )
    return "Browser profile is not configured"


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
            "error": _unresolved_reason(profile_id),
        }
    return _status_for(profile, include_pairing_code=include_pairing_code)


def list_devices(
    *,
    include_status: bool = True,
    include_pairing_code: bool = False,
) -> list[dict]:
    profiles = browser_profiles.list_profiles()
    default_id = _effective_default_id(profiles)
    if not any(profile["profile_id"] == "main" for profile in profiles):
        implicit = _implicit_main()
        if implicit:
            profiles.append(implicit)
            default_id = default_id or "main"
    profiles.sort(key=lambda profile: profile["profile_id"])
    for profile in profiles:
        profile["is_default"] = profile["profile_id"] == default_id
    if include_status:
        # list_profiles already returned these rows — status() would re-read
        # each one from Mongo (one round-trip per device).
        return [
            _status_for(profile, include_pairing_code=include_pairing_code)
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
    before = browser_profiles.list_profiles()
    saved = browser_profiles.upsert(
        profile_id,
        backend or configured_backend(),
        pairing_code=pairing_code,
        cdp_port=cdp_port,
        purpose=purpose,
    )
    if not any(profile.get("is_default") for profile in before):
        # Pin whoever was answering to `main` before this pairing. Without this a
        # second browser makes the first one ambiguous and the agent loses it.
        browser_profiles.set_default(
            _effective_default_id(before) or saved["profile_id"]
        )
    return status(saved["profile_id"])


def set_default(profile_id: str) -> dict:
    profile_id = browser_profiles.validate_profile_id(profile_id)
    return {
        "profile_id": profile_id,
        "updated": browser_profiles.set_default(profile_id),
    }


def remove(profile_id: str) -> dict:
    profile_id = browser_profiles.validate_profile_id(profile_id)
    return {"profile_id": profile_id, "removed": browser_profiles.delete(profile_id)}


def _connect_profile_id(inputs: dict) -> str:
    """Id for a newly connected profile — the pairing code, as Tower does it.

    `main` is a role, so it must not become a profile's name.
    """
    explicit = (inputs.get("profile_id") or "").strip()
    if explicit:
        return explicit
    code = (inputs.get("pairing_code") or "").strip()
    if code:
        from .bce_client import normalize_pairing_code

        return normalize_pairing_code(code).lower()
    return "main"


def execute(action: str, **inputs) -> dict:
    action = (action or "").strip().lower()
    if action == "list":
        return {"devices": list_devices()}
    if action == "status":
        return status(inputs.get("profile_id"))
    if action == "connect":
        return connect(
            _connect_profile_id(inputs),
            backend=inputs.get("backend"),
            pairing_code=inputs.get("pairing_code"),
            cdp_port=inputs.get("cdp_port"),
            purpose=inputs.get("purpose"),
        )
    if action == "set_default":
        return set_default(inputs.get("profile_id") or "")
    if action == "remove":
        return remove(inputs.get("profile_id") or "")
    raise ValueError(
        "action must be list, status, connect, set_default, or remove"
    )

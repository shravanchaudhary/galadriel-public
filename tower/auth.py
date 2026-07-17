"""Tower form/session authentication helpers.

Validates the single shared credential from TOWER_AUTH_USERNAME /
TOWER_AUTH_TOKEN, issues signed Flask sessions for browser users, and
preserves Basic/Bearer header auth for scripts.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import os
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

from flask import Request, jsonify, redirect, request, session, url_for

DEFAULT_SECRET_KEY = "change-me"
SESSION_AUTH_KEY = "tower_authenticated"
SESSION_USER_KEY = "tower_username"
SESSION_LIFETIME_HOURS = 12

AuthMethod = Literal["session", "basic", "bearer"]


@dataclass(frozen=True)
class AuthResult:
    method: AuthMethod
    username: str


def auth_required() -> bool:
    return os.environ.get("TOWER_AUTH_REQUIRED", "").lower() in {"1", "true", "yes"}


def auth_username() -> str:
    return os.environ.get("TOWER_AUTH_USERNAME", "clyra")


def auth_token() -> str:
    return os.environ.get("TOWER_AUTH_TOKEN", "")


def secret_key() -> str:
    return os.environ.get("TOWER_SECRET_KEY", DEFAULT_SECRET_KEY)


def cookie_secure() -> bool:
    raw = os.environ.get("TOWER_COOKIE_SECURE")
    if raw is None or raw == "":
        # Default secure cookies whenever auth is required (staging/prod HTTPS).
        return auth_required()
    return raw.lower() in {"1", "true", "yes"}


def credentials_configured() -> bool:
    return bool(auth_token())


def secret_key_configured() -> bool:
    key = secret_key()
    return bool(key) and key != DEFAULT_SECRET_KEY


def auth_ready() -> bool:
    """True when required auth has both a credential and a real signing key."""
    if not auth_required():
        return True
    return credentials_configured() and secret_key_configured()


def auth_misconfigured_reason() -> str | None:
    if not auth_required():
        return None
    if not credentials_configured():
        return "TOWER_AUTH_TOKEN is not set"
    if not secret_key_configured():
        return "TOWER_SECRET_KEY is missing or still the default value"
    return None


def public_path(path: str) -> bool:
    if path in {"/healthz", "/readyz", "/login", "/logout"}:
        return True
    if path.startswith("/static/"):
        return True
    return False


def safe_next_url(candidate: str | None, fallback: str = "/") -> str:
    """Return a same-origin relative path, or fallback for open-redirect attempts."""
    if not candidate:
        return fallback
    value = candidate.strip()
    if not value.startswith("/") or value.startswith("//"):
        return fallback
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc:
        return fallback
    if "\\" in value or "\n" in value or "\r" in value:
        return fallback
    return value


def credentials_match(username: str, password: str) -> bool:
    expected_user = auth_username()
    expected_token = auth_token()
    if not expected_token:
        return False
    user_ok = hmac.compare_digest(username or "", expected_user)
    pass_ok = hmac.compare_digest(password or "", expected_token)
    return user_ok and pass_ok


def _basic_credentials(authorization: str) -> tuple[str, str]:
    try:
        decoded = base64.b64decode(authorization[6:], validate=True).decode("utf-8")
        username, password = decoded.split(":", 1)
        return username, password
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return "", ""


def authenticate_request(req: Request | None = None) -> AuthResult | None:
    """Authenticate from session, then Basic, then Bearer."""
    req = req or request

    if session.get(SESSION_AUTH_KEY) is True:
        username = session.get(SESSION_USER_KEY) or auth_username()
        return AuthResult(method="session", username=username)

    authorization = req.headers.get("Authorization", "")
    token = auth_token()
    if not token:
        return None

    if authorization.startswith("Basic "):
        username, password = _basic_credentials(authorization)
        if credentials_match(username, password):
            return AuthResult(method="basic", username=username)
        return None

    if authorization.startswith("Bearer "):
        supplied = authorization[7:].strip()
        if supplied and hmac.compare_digest(supplied, token):
            return AuthResult(method="bearer", username=auth_username())
        return None

    return None


def establish_session(username: str) -> None:
    session.clear()
    session[SESSION_AUTH_KEY] = True
    session[SESSION_USER_KEY] = username
    session.permanent = True


def clear_session() -> None:
    session.clear()


def wants_json_unauthorized(req: Request | None = None) -> bool:
    """Prefer JSON 401 for API/SSE and non-GET browser navigations."""
    req = req or request
    if req.path.startswith("/api/"):
        return True
    accept = (req.headers.get("Accept") or "").lower()
    if "application/json" in accept and "text/html" not in accept:
        return True
    if req.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    if req.method not in {"GET", "HEAD", "OPTIONS"}:
        return True
    return False


def unauthorized_response(req: Request | None = None):
    """Return redirect-to-login for HTML GETs, else JSON 401 (no WWW-Authenticate)."""
    req = req or request
    if wants_json_unauthorized(req):
        return jsonify({"error": "Unauthorized"}), 401
    next_path = safe_next_url(req.full_path if req.query_string else req.path)
    # full_path includes a trailing '?' when there is no query; strip it.
    if next_path.endswith("?"):
        next_path = next_path[:-1]
    next_path = safe_next_url(next_path)
    return redirect(url_for("login", next=next_path))


def same_origin_ok(req: Request | None = None) -> bool:
    """CSRF guard for session-authenticated unsafe methods."""
    req = req or request
    origin = req.headers.get("Origin")
    if origin:
        return _origin_matches_host(origin, req)
    referer = req.headers.get("Referer")
    if referer:
        return _origin_matches_host(referer, req)
    # Missing both is reject for unsafe session requests.
    return False


def _origin_matches_host(value: str, req: Request) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    if not parsed.scheme or not parsed.netloc:
        return False
    host = req.host
    if not host:
        return False
    return parsed.netloc.lower() == host.lower()


def configure_app_sessions(app) -> None:
    """Apply Flask session cookie settings from environment."""
    from datetime import timedelta

    app.secret_key = secret_key()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=cookie_secure(),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=SESSION_LIFETIME_HOURS),
        SESSION_REFRESH_EACH_REQUEST=True,
    )

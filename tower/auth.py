"""Tower form/session authentication helpers.

Validates the single shared credential from TOWER_AUTH_USERNAME /
TOWER_AUTH_TOKEN, issues signed Flask sessions for browser users, and
preserves Basic/Bearer header auth for scripts.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import os
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlencode, urlparse

from flask import Request, jsonify, redirect, request, session, url_for

DEFAULT_SECRET_KEY = "change-me"
SESSION_AUTH_KEY = "tower_authenticated"
SESSION_USER_KEY = "tower_username"
SESSION_LIFETIME_HOURS = 12

AuthMethod = Literal["session", "basic", "bearer", "alb"]


@dataclass(frozen=True)
class AuthResult:
    method: AuthMethod
    username: str


def auth_required() -> bool:
    return os.environ.get("TOWER_AUTH_REQUIRED", "").lower() in {"1", "true", "yes"}


def alb_identity_enabled() -> bool:
    return os.environ.get("REPLIKA_TRUST_ALB_IDENTITY", "").lower() in {
        "1",
        "true",
        "yes",
    }


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
    if alb_identity_enabled():
        return True
    return credentials_configured() and secret_key_configured()


def auth_misconfigured_reason() -> str | None:
    if not auth_required():
        return None
    if alb_identity_enabled():
        return None
    if not credentials_configured():
        return "TOWER_AUTH_TOKEN is not set"
    if not secret_key_configured():
        return "TOWER_SECRET_KEY is missing or still the default value"
    return None


def public_path(path: str) -> bool:
    if path in {
        "/healthz",
        "/readyz",
        "/login",
        "/logout",
        "/internal/replika/provisioning",
        "/internal/replika/database",
        "/integrations/slack/oauth/callback",
        "/slack/events",
        "/slack/commands",
        "/internal/slack/deliver",
    }:
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

    if alb_identity_enabled():
        identity = (req.headers.get("x-amzn-oidc-identity") or "").strip()
        expected_tenant = os.environ.get("REPLIKA_TENANT_ID", "default")
        if identity and (
            expected_tenant == "default" or hmac.compare_digest(identity, expected_tenant)
        ):
            return AuthResult(method="alb", username=identity)

    if session.get(SESSION_AUTH_KEY) is True:
        username = session.get(SESSION_USER_KEY) or auth_username()
        expected_tenant = os.environ.get("REPLIKA_TENANT_ID", "default")
        if expected_tenant == "default" or hmac.compare_digest(
            str(username), expected_tenant
        ):
            return AuthResult(method="session", username=username)
        return None

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


def _cognito_client_id(req: Request) -> str:
    configured = os.environ.get("REPLIKA_COGNITO_CLIENT_ID", "").strip()
    if configured:
        return configured
    token = (req.headers.get("x-amzn-oidc-accesstoken") or "").strip()
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return str(claims.get("client_id") or "") if isinstance(claims, dict) else ""
    except (IndexError, ValueError, binascii.Error, UnicodeDecodeError):
        return ""


def logout_response(req: Request | None = None):
    """Clear Flask, ALB, and Cognito sessions for the current product host."""
    req = req or request
    clear_session()

    cognito_domain = os.environ.get("REPLIKA_COGNITO_DOMAIN", "").rstrip("/")
    client_id = _cognito_client_id(req)
    forwarded_proto = (req.headers.get("X-Forwarded-Proto") or "").split(",", 1)[0]
    scheme = forwarded_proto.strip() or req.scheme
    login_url = f"{scheme}://{req.host}/login"
    if cognito_domain and client_id:
        target = f"{cognito_domain}/logout?{urlencode({
            'client_id': client_id,
            'logout_uri': login_url,
        })}"
    else:
        target = url_for("login")

    response = redirect(target)
    # ALB shards large authentication sessions across up to four cookies.
    for name in ["AWSELBAuthSessionCookie", *[
        f"AWSELBAuthSessionCookie-{index}" for index in range(4)
    ]]:
        response.delete_cookie(
            name,
            path="/",
            secure=True,
            httponly=True,
            samesite="Lax",
        )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Clear-Site-Data"] = '"cache", "storage"'
    return response


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
    control_plane_url = os.environ.get("REPLIKA_CONTROL_PLANE_URL", "").rstrip("/")
    if alb_identity_enabled() and control_plane_url:
        return redirect(f"{control_plane_url}/replika")
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
        SESSION_COOKIE_DOMAIN=os.environ.get("REPLIKA_COOKIE_DOMAIN") or None,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=SESSION_LIFETIME_HOURS),
        SESSION_REFRESH_EACH_REQUEST=True,
    )

"""Smoke-test Tower form/session auth, header compatibility, and readiness."""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Baseline env for auth-enabled tests. Individual cases override as needed.
os.environ["TOWER_AUTH_REQUIRED"] = "true"
os.environ["TOWER_AUTH_USERNAME"] = "clyra"
os.environ["TOWER_AUTH_TOKEN"] = "test-token"
os.environ["TOWER_SECRET_KEY"] = "test-secret-key-not-default"
os.environ["TOWER_COOKIE_SECURE"] = "true"
os.environ.pop("MONGO_URI", None)
os.environ.pop("REDIS_URL", None)

from tower.app import create_tower  # noqa: E402
from tower import auth as tower_auth  # noqa: E402


class _Agent:
    model = "test-model"
    conversations = {}
    headroom_enabled = False

    class memory:
        memory_dir = str(ROOT / "memory")

    def model_for_channel(self, _channel_id):
        return self.model


def _make_client(**env_overrides):
    for key, value in env_overrides.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    # Recreate app so session cookie settings pick up env changes.
    app = create_tower(_Agent())
    return app, app.test_client()


def _basic(username: str, password: str) -> dict:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _unsigned_access_token(client_id: str) -> str:
    payload = base64.urlsafe_b64encode(
        f'{{"client_id":"{client_id}"}}'.encode()
    ).decode().rstrip("=")
    return f"header.{payload}.signature"


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _assert_authorized(response, label: str) -> None:
    """`/` is a redirect to the chats landing, so 200 is not the pass signal.

    Authorized: 200, or a redirect anywhere that is not the login page.
    Unauthorized: a redirect to /login. Asserting a bare 302 would pass for
    both, which is how these checks stopped testing anything.
    """
    location = response.headers.get("Location", "")
    _assert(
        response.status_code in (200, 302) and "/login" not in location,
        f"{label}: expected authorized, got {response.status_code} -> {location or '(no redirect)'}",
    )


def _assert_bounced_to_login(response, label: str) -> None:
    location = response.headers.get("Location", "")
    _assert(
        response.status_code == 302 and "/login" in location,
        f"{label}: expected a bounce to /login, got {response.status_code} -> {location or '(no redirect)'}",
    )


# --- Public routes -----------------------------------------------------------
app, client = _make_client()
for path in ("/healthz", "/readyz", "/login"):
    resp = client.get(path)
    _assert(resp.status_code == 200, f"{path} should be public, got {resp.status_code}")

static = client.get("/static/style.css")
_assert(static.status_code == 200, "login stylesheet should be public")


# --- Anonymous HTML redirects to login ---------------------------------------
anon = client.get("/", follow_redirects=False)
_assert(anon.status_code == 302, f"anonymous / should redirect, got {anon.status_code}")
_assert("/login" in anon.headers.get("Location", ""), "redirect target should be /login")
_assert("next=" in anon.headers.get("Location", ""), "redirect should preserve next")
_assert("WWW-Authenticate" not in anon.headers, "must not trigger browser Basic prompt")


# --- Anonymous API / stream return JSON 401 ----------------------------------
api = client.get("/api/chat/stream")
_assert(api.status_code == 401, f"stream should 401, got {api.status_code}")
_assert(api.is_json and api.get_json().get("error") == "Unauthorized", "stream 401 body")
_assert("WWW-Authenticate" not in api.headers, "API must not send WWW-Authenticate")

api_post = client.post("/api/chat", json={"message": "hi"})
_assert(api_post.status_code == 401, f"api chat should 401, got {api_post.status_code}")
_assert("WWW-Authenticate" not in api_post.headers, "POST API must not send WWW-Authenticate")


# --- Wrong form credentials --------------------------------------------------
bad = client.post(
    "/login",
    data={"username": "clyra", "password": "wrong", "next": "/"},
    follow_redirects=False,
)
_assert(bad.status_code == 401, f"bad login should 401, got {bad.status_code}")
_assert(b"Invalid username or password" in bad.data, "generic login error expected")
still_anon = client.get("/", follow_redirects=False)
_assert(still_anon.status_code == 302, "session must remain unauthenticated after bad login")


# --- Correct form credentials + cookie flags ---------------------------------
# Fresh client so cookie jar is clean.
_, client = _make_client()
ok = client.post(
    "/login",
    data={"username": "clyra", "password": "test-token", "next": "/"},
    follow_redirects=False,
)
_assert(ok.status_code == 302, f"good login should redirect, got {ok.status_code}")
_assert(ok.headers.get("Location", "").endswith("/"), f"unexpected next: {ok.headers.get('Location')}")

set_cookie = ok.headers.get("Set-Cookie", "")
_assert("HttpOnly" in set_cookie or "httponly" in set_cookie.lower(), "session cookie must be HttpOnly")
_assert("SameSite=Lax" in set_cookie or "samesite=lax" in set_cookie.lower(), "SameSite=Lax required")
_assert("Secure" in set_cookie, "Secure cookie required when TOWER_COOKIE_SECURE=true")
_assert("test-token" not in set_cookie, "password must never appear in Set-Cookie")
_assert("test-secret-key" not in set_cookie, "signing key must never appear in Set-Cookie")

# "/" is a redirect to the chats landing, not a page. The security property is
# that an authenticated session lands on the app, not back at /login.
dash = client.get("/")
_assert_authorized(dash, "session should reach the app")
landed = client.get("/", follow_redirects=True)
_assert(landed.status_code == 200, f"session should land on a page, got {landed.status_code}")

# Session-authenticated API fetch (credentials include cookies automatically)
api_ok = client.post(
    "/api/chat",
    json={"message": ""},
    headers={"Origin": "http://localhost"},
)
# Empty message is 400 once auth passes — proves auth cleared the gate.
_assert(api_ok.status_code == 400, f"session API should reach handler, got {api_ok.status_code}")


# --- Open redirect protection ------------------------------------------------
_, client = _make_client()
for evil in ("https://evil.example/", "//evil.example/", "/\\evil"):
    resp = client.post(
        "/login",
        data={"username": "clyra", "password": "test-token", "next": evil},
        follow_redirects=False,
    )
    loc = resp.headers.get("Location", "")
    _assert(resp.status_code == 302, f"login with next={evil!r} should redirect")
    _assert(loc.endswith("/") or loc.endswith("/login") or loc == "/",
            f"unsafe next={evil!r} leaked redirect to {loc!r}")
    _assert("evil.example" not in loc, f"open redirect to {loc!r}")

_assert(tower_auth.safe_next_url("https://evil.example/") == "/", "safe_next absolute")
_assert(tower_auth.safe_next_url("//evil.example/") == "/", "safe_next scheme-relative")
_assert(tower_auth.safe_next_url("/palace") == "/palace", "safe_next relative ok")


# --- CSRF: cross-origin unsafe session request rejected ----------------------
_, client = _make_client()
client.post("/login", data={"username": "clyra", "password": "test-token", "next": "/"})
cross = client.post(
    "/api/chat",
    json={"message": "x"},
    headers={"Origin": "https://evil.example"},
)
_assert(cross.status_code == 403, f"cross-origin session POST should 403, got {cross.status_code}")

same = client.post(
    "/api/chat",
    json={"message": ""},
    headers={"Origin": "http://localhost"},
)
_assert(same.status_code == 400, f"same-origin session POST should pass auth, got {same.status_code}")


# --- Logout clears session ---------------------------------------------------
gone = client.post("/logout", follow_redirects=False)
_assert(gone.status_code == 302, "logout should redirect")
_assert("/login" in gone.headers.get("Location", ""), "logout should land on login")
after = client.get("/", follow_redirects=False)
_assert(after.status_code == 302, "logout must revoke dashboard access")

# Managed logout must also terminate the ALB and Cognito sessions.
_, client = _make_client(
    REPLIKA_COGNITO_DOMAIN="https://auth.example",
)
managed_logout = client.post(
    "/logout",
    base_url="https://alice.replika.example",
    headers={
        "x-amzn-oidc-accesstoken": _unsigned_access_token("tenant-client"),
    },
    follow_redirects=False,
)
managed_location = managed_logout.headers.get("Location", "")
_assert(
    managed_location.startswith("https://auth.example/logout?"),
    f"managed logout must use Cognito: {managed_location}",
)
_assert("client_id=tenant-client" in managed_location, "Cognito client ID missing")
_assert(
    "logout_uri=https%3A%2F%2Falice.replika.example%2Flogin" in managed_location,
    f"tenant logout URI missing: {managed_location}",
)
managed_cookies = managed_logout.headers.getlist("Set-Cookie")
_assert(
    any(cookie.startswith("AWSELBAuthSessionCookie=") for cookie in managed_cookies),
    "base ALB session cookie must be expired",
)
for index in range(4):
    _assert(
        any(
            cookie.startswith(f"AWSELBAuthSessionCookie-{index}=")
            for cookie in managed_cookies
        ),
        f"ALB session shard {index} must be expired",
    )
_assert(
    managed_logout.headers.get("Cache-Control") == "no-store",
    "logout response must not be cached",
)


# --- Basic + Bearer header compatibility -------------------------------------
_, client = _make_client(REPLIKA_COGNITO_DOMAIN=None)
basic_ok = client.get("/", headers=_basic("clyra", "test-token"))
_assert_authorized(basic_ok, "Basic auth should work")

basic_bad = client.get("/", headers=_basic("clyra", "nope"))
_assert_bounced_to_login(basic_bad, "bad Basic")

bearer_ok = client.get("/", headers={"Authorization": "Bearer test-token"})
_assert_authorized(bearer_ok, "Bearer should work")

bearer_api = client.post(
    "/api/chat",
    json={"message": ""},
    headers={"Authorization": "Bearer test-token"},
)
_assert(bearer_api.status_code == 400, f"Bearer API should reach handler, got {bearer_api.status_code}")


# --- ALB identity becomes a tenant-scoped cross-subdomain session ------------
_, client = _make_client(
    REPLIKA_TRUST_ALB_IDENTITY="true",
    REPLIKA_TENANT_ID="default",
    REPLIKA_COOKIE_DOMAIN=".replika.example",
    REPLIKA_CONTROL_PLANE_URL="https://app.replika.example",
)
control_plane = client.get(
    "/",
    base_url="https://app.replika.example",
    headers={"x-amzn-oidc-identity": "account-123"},
)
_assert_authorized(control_plane, "ALB identity should enter control plane")
shared_cookie = control_plane.headers.get("Set-Cookie", "")
_assert(
    "Domain=replika.example" in shared_cookie,
    f"control plane must issue a shared product-domain cookie: {shared_cookie}",
)

os.environ["REPLIKA_TENANT_ID"] = "account-123"
tenant = client.get("/", base_url="https://alice.replika.example")
_assert_authorized(tenant, "matching tenant should accept shared session")

os.environ["REPLIKA_TENANT_ID"] = "account-456"
wrong_tenant = client.get(
    "/",
    base_url="https://bob.replika.example",
    follow_redirects=False,
)
_assert(wrong_tenant.status_code == 302, "cross-tenant session must be rejected")
_assert(
    wrong_tenant.headers.get("Location") == "https://app.replika.example/replika",
    "rejected tenant session should return to the control plane",
)

# --- Control-plane root redirects to canonical setup page -------------------
_, client = _make_client(
    REPLIKA_TRUST_ALB_IDENTITY="true",
    REPLIKA_TENANT_ID="default",
    REPLIKA_COOKIE_DOMAIN=".replika.example",
    REPLIKA_CONTROL_PLANE_URL="https://app.replika.example",
    REPLIKA_CONTROL_PLANE_ONLY="true",
)
control_plane_root = client.get(
    "/",
    base_url="https://app.replika.example",
    headers={"x-amzn-oidc-identity": "account-123"},
    follow_redirects=False,
)
_assert(control_plane_root.status_code == 302, "control-plane / should redirect")
_assert(
    control_plane_root.headers.get("Location") == "/replika",
    "control-plane / should redirect to /replika",
)
control_plane_setup = client.get(
    "/replika",
    base_url="https://app.replika.example",
)
_assert(
    control_plane_setup.status_code == 200,
    "authenticated control-plane session should access /replika",
)


# --- Auth-disabled local mode ------------------------------------------------
_, client = _make_client(
    TOWER_AUTH_REQUIRED="false",
    REPLIKA_TRUST_ALB_IDENTITY=None,
    REPLIKA_TENANT_ID=None,
    REPLIKA_COOKIE_DOMAIN=None,
    REPLIKA_CONTROL_PLANE_URL=None,
    REPLIKA_CONTROL_PLANE_ONLY=None,
)
open_dash = client.get("/")
_assert_authorized(open_dash, "auth-disabled / should be open")


# --- Readiness fails closed without signing key ------------------------------
_, client = _make_client(
    TOWER_AUTH_REQUIRED="true",
    TOWER_SECRET_KEY="change-me",
)
ready = client.get("/readyz")
_assert(ready.status_code == 503, f"default secret should fail readiness, got {ready.status_code}")
body = ready.get_json() or {}
_assert(body.get("status") == "misconfigured", f"unexpected readiness body: {body}")

_, client = _make_client(
    TOWER_AUTH_REQUIRED="true",
    TOWER_SECRET_KEY="test-secret-key-not-default",
    TOWER_AUTH_TOKEN=None,
)
ready_no_token = client.get("/readyz")
_assert(ready_no_token.status_code == 503, "missing token should fail readiness")

# Restore sane defaults and confirm ready when configured.
_, client = _make_client(
    TOWER_AUTH_REQUIRED="true",
    TOWER_AUTH_TOKEN="test-token",
    TOWER_SECRET_KEY="test-secret-key-not-default",
)
ready_ok = client.get("/readyz")
_assert(ready_ok.status_code == 200, f"configured readiness should pass, got {ready_ok.status_code}")

print("Tower health and authentication checks passed.")

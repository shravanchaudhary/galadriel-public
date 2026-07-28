"""Focused checks for Slack OAuth, secure token storage, and event routing."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.update(
    {
        "REPLIKA_TENANT_ID": "replika-1",
        "REPLIKA_OWNER_ID": "account-123",
        "REPLIKA_TYPE": "organization",
        "SLACK_CLIENT_ID": "client-id",
        "SLACK_CLIENT_SECRET": "client-secret",
        "SLACK_OAUTH_REDIRECT_URI": "https://control.example/integrations/slack/oauth/callback",
        "SLACK_OAUTH_STATE_SECRET": "state-secret-for-tests",
        "SLACK_SIGNING_SECRET": "signing-secret-for-tests",
        "SLACK_TENANT_AUTH_SECRET_PREFIX": "replika/slack-auth",
        "SLACK_TENANT_AUTH_KMS_KEY_ID": "slack-auth-kms-key",
        "SLACK_ALLOW_INSECURE_TENANT_URLS": "true",
    }
)
REPLIKA_ID = "replika-1"
OWNER_ID = "account-123"

from flask import Flask  # noqa: E402
from tower.slack_integration import (  # noqa: E402
    SecretsManagerTokenVault,
    register_slack_integration,
    signed_internal_headers,
)


class _Store:
    def __init__(self):
        self.states = {}
        self.installation = None
        self.outbox = {}

    def save_state(self, nonce, owner_id, replika_id, replika_type):
        self.states[nonce] = {
            "_id": nonce,
            "owner_id": owner_id,
            "replika_id": replika_id,
            "replika_type": replika_type,
        }

    def consume_state(self, nonce):
        return self.states.pop(nonce, None)

    def upsert_installation(self, document):
        self.installation = dict(document)

    def for_replika(self, replika_id):
        if self.installation and self.installation.get("replika_id") == replika_id:
            return self.installation
        return None

    def for_owner(self, owner_id):
        if self.installation and self.installation["owner_id"] == owner_id:
            return self.installation
        return None

    def for_team(self, team_id):
        if self.installation and self.installation["team_id"] == team_id:
            return self.installation
        return None

    def outbox_item(self, dedupe_key):
        return self.outbox.get(dedupe_key)

    def set_runtime_placeholder(self, item_id, placeholder_ts):
        item = self.outbox.get(item_id)
        if not item or item.get("placeholder_ts"):
            return False
        item["placeholder_ts"] = placeholder_ts
        return True

    def select_channel(self, replika_id, team_id, channel):
        if not self.for_replika(replika_id) or self.installation["team_id"] != team_id:
            return False
        self.installation["selected_channel"] = {
            "id": channel["id"],
            "name": channel["name"],
        }
        return True

    def set_admins(self, replika_id, team_id, admin_user_ids):
        if not self.for_replika(replika_id) or self.installation["team_id"] != team_id:
            return False
        self.installation["admin_user_ids"] = list(admin_user_ids)
        return True

    def delete_installation(self, replika_id, team_id=None):
        if self.for_replika(replika_id) and (
            team_id is None or self.installation["team_id"] == team_id
        ):
            self.installation = None
            return True
        return False

    def delete_outbox_for_replika(self, replika_id):
        before = len(self.outbox)
        self.outbox = {
            key: value
            for key, value in self.outbox.items()
            if value.get("replika_id") != replika_id
        }
        return before - len(self.outbox)

    def enqueue(self, dedupe_key, installation, kind, payload):
        if dedupe_key in self.outbox:
            return False
        self.outbox[dedupe_key] = {
            "_id": dedupe_key,
            "dedupe_key": dedupe_key,
            "replika_id": installation.get("replika_id"),
            "owner_id": installation["owner_id"],
            "team_id": installation["team_id"],
            "kind": kind,
            "payload": payload,
        }
        return True


class _Vault:
    def __init__(self):
        self.values = {}

    def put(self, replika_id, team_id, token):
        ref = f"vault://{replika_id}/{team_id}"
        self.values[ref] = token
        return ref

    def get(self, token_ref):
        return self.values[token_ref]

    def delete(self, token_ref):
        self.values.pop(token_ref, None)

    def ensure(self, replika_id):
        ref = f"vault://auth/{replika_id}"
        self.values.setdefault(ref, "tenant-hmac-secret")
        return ref


class _Slack:
    def __init__(self):
        self.calls = []

    def call(self, method, *, token=None, **params):
        self.calls.append((method, token, params))
        if method == "oauth.v2.access":
            return {
                "ok": True,
                "access_token": "xoxb-plaintext-must-not-enter-mongo",
                "team": {"id": "T123", "name": "Test Workspace"},
                "bot_user_id": "B123",
                "authed_user": {"id": "UINSTALLER"},
                "scope": "commands,chat:write",
            }
        if method == "conversations.list":
            return {
                "ok": True,
                "channels": [
                    {"id": "C1", "name": "general", "is_private": False},
                    {
                        "id": "G1",
                        "name": "invited-private",
                        "is_private": True,
                        "is_member": True,
                    },
                    {
                        "id": "G2",
                        "name": "hidden-private",
                        "is_private": True,
                        "is_member": False,
                    },
                ],
            }
        if method == "conversations.members":
            return {"ok": True, "members": ["UINSTALLER", "UADMIN"]}
        if method == "users.info":
            return {
                "ok": True,
                "user": {"id": params["user"], "team_id": "T123", "is_bot": False},
            }
        if method == "chat.postMessage":
            return {"ok": True, "ts": "200.1"}
        return {"ok": True}


class _SecretsManager:
    class exceptions:
        class ResourceExistsException(Exception):
            pass

    def create_secret(self, **kwargs):
        self.created = kwargs
        return {"ARN": "arn:aws:secretsmanager:test:slack-token"}


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


def _signed_headers(body: bytes, timestamp: int | None = None):
    timestamp = timestamp or int(time.time())
    base = b"v0:" + str(timestamp).encode() + b":" + body
    signature = "v0=" + hmac.new(
        os.environ["SLACK_SIGNING_SECRET"].encode(), base, hashlib.sha256
    ).hexdigest()
    return {
        "X-Slack-Request-Timestamp": str(timestamp),
        "X-Slack-Signature": signature,
    }


os.environ["SLACK_TOKEN_KMS_KEY_ID"] = "kms-key-id"
os.environ["SLACK_TOKEN_SECRET_PREFIX"] = "replika/slack"
secrets_manager = _SecretsManager()
token_ref = SecretsManagerTokenVault(secrets_manager).put(
    REPLIKA_ID, "T123", "xoxb-secret"
)
_assert(token_ref.startswith("arn:aws:secretsmanager:"), "vault returns opaque reference")
_assert(secrets_manager.created["KmsKeyId"] == "kms-key-id", "vault requires KMS key")

app = Flask(
    __name__,
    template_folder=str(ROOT / "tower" / "templates"),
    static_folder=str(ROOT / "tower" / "static"),
)
app.secret_key = "test"
store = _Store()
vault = _Vault()
slack = _Slack()
app.config.update(
    SLACK_INSTALLATION_STORE=store,
    SLACK_TOKEN_VAULT=vault,
    SLACK_TENANT_AUTH_VAULT=vault,
    SLACK_API=slack,
    REPLIKA_TYPE_RESOLVER=lambda replika_id: "organization",
    REPLIKA_OWNERSHIP_CHECKER=lambda replika_id, owner_id: (
        {
            "replika_id": REPLIKA_ID,
            "owner_id": OWNER_ID,
            "username": "alice",
            "replika_type": "organization",
            "status": "ready",
        }
        if replika_id == REPLIKA_ID and owner_id == OWNER_ID
        else None
    ),
)
register_slack_integration(app)
client = app.test_client()

# Owner identity comes from session for control-plane routes.
with client.session_transaction() as session:
    session["tower_authenticated"] = True
    session["tower_username"] = OWNER_ID

install = client.get(f"/replika/{REPLIKA_ID}/integrations/slack/install")
_assert(install.status_code == 302, "install should redirect to Slack")
query = urllib.parse.parse_qs(urllib.parse.urlparse(install.headers["Location"]).query)
state = query["state"][0]
callback = client.get(
    "/integrations/slack/oauth/callback",
    query_string={"code": "oauth-code", "state": state},
)
_assert(callback.status_code == 302, "valid OAuth callback")
_assert(store.installation["token_ref"].startswith("vault://"), "Mongo stores token ref")
_assert(store.installation["replika_id"] == REPLIKA_ID, "install scoped to Replika")
_assert("xoxb-" not in str(store.installation), "Mongo must not store plaintext token")
_assert(not store.states, "OAuth state must be single-use")

replay = client.get(
    "/integrations/slack/oauth/callback",
    query_string={"code": "oauth-code", "state": state},
)
_assert("slack=error" in replay.headers["Location"], "OAuth state replay rejected")

channels = client.get(
    f"/api/replikas/{REPLIKA_ID}/integrations/slack/channels"
).get_json()["channels"]
_assert([row["id"] for row in channels] == ["C1", "G1"], "accessible channels only")
selected = client.put(
    f"/api/replikas/{REPLIKA_ID}/integrations/slack/channel",
    json={"channel_id": "C1"},
)
_assert(selected.status_code == 200, "organization selects one channel")
admins = client.put(
    f"/api/replikas/{REPLIKA_ID}/integrations/slack/admins",
    json={"admin_user_ids": ["UADMIN"]},
)
_assert(admins.status_code == 200, "channel-member admin IDs are persisted")
_assert(
    store.installation["admin_user_ids"] == ["UINSTALLER", "UADMIN"],
    "OAuth installer remains an admin",
)

# Second Replika cannot read or mutate the first Replika's Slack install.
cross = client.get("/api/replikas/replika-other/integrations/slack/status")
_assert(cross.status_code == 404, "cross-Replika Slack status is denied")

challenge_body = json.dumps(
    {"type": "url_verification", "challenge": "challenge-token"},
    separators=(",", ":"),
).encode()
challenge = client.post(
    "/slack/events",
    data=challenge_body,
    content_type="application/json",
    headers=_signed_headers(challenge_body),
)
_assert(challenge.get_json()["challenge"] == "challenge-token", "URL verification")

event_payload = {
    "type": "event_callback",
    "team_id": "T123",
    "event_id": "Ev123",
    "event": {
        "type": "message",
        "channel": "C1",
        "user": "U1",
        "text": "hello without a mention",
        "ts": "100.0",
        "thread_ts": "100.1",
    },
}
event_body = json.dumps(event_payload, separators=(",", ":")).encode()
accepted = client.post(
    "/slack/events",
    data=event_body,
    content_type="application/json",
    headers=_signed_headers(event_body),
)
_assert(accepted.get_json()["accepted"], "non-mentioned selected channel event routed")
duplicate = client.post(
    "/slack/events",
    data=event_body,
    content_type="application/json",
    headers=_signed_headers(event_body),
)
_assert(not duplicate.get_json()["accepted"], "event deduplicated")
event_key = "event:T123:C1:100.0"
_assert(store.outbox[event_key]["owner_id"] == OWNER_ID, "team routes to owner")
_assert(store.outbox[event_key]["replika_id"] == REPLIKA_ID, "outbox carries replika_id")

for event_id, revision_event in (
    (
        "EvEdit",
        {
            "type": "message",
            "subtype": "message_changed",
            "channel": "C1",
            "message": {"ts": "100.0", "user": "U1", "text": "edited text"},
        },
    ),
    (
        "EvDelete",
        {
            "type": "message",
            "subtype": "message_deleted",
            "channel": "C1",
            "deleted_ts": "100.0",
            "previous_message": {"ts": "100.0", "user": "U1", "text": "edited text"},
        },
    ),
):
    revision_payload = {
        "type": "event_callback",
        "team_id": "T123",
        "event_id": event_id,
        "event": revision_event,
    }
    revision_body = json.dumps(revision_payload, separators=(",", ":")).encode()
    revision_response = client.post(
        "/slack/events",
        data=revision_body,
        content_type="application/json",
        headers=_signed_headers(revision_body),
    )
    _assert(
        revision_response.get_json()["accepted"],
        f"{revision_event['subtype']} routed for durable revision",
    )

bad_signature = client.post(
    "/slack/events",
    data=event_body,
    content_type="application/json",
    headers={
        "X-Slack-Request-Timestamp": str(int(time.time())),
        "X-Slack-Signature": "v0=bad",
    },
)
_assert(bad_signature.status_code == 401, "bad signatures rejected")
stale = client.post(
    "/slack/events",
    data=event_body,
    content_type="application/json",
    headers=_signed_headers(event_body, int(time.time()) - 600),
)
_assert(stale.status_code == 401, "stale signed requests rejected")

command_body = urllib.parse.urlencode(
    {
        "team_id": "T123",
        "channel_id": "C1",
        "trigger_id": "trigger-1",
        "command": "/stop",
        "text": "",
        "user_id": "UINSTALLER",
    }
).encode()
command = client.post(
    "/slack/commands",
    data=command_body,
    content_type="application/x-www-form-urlencoded",
    headers=_signed_headers(command_body),
)
_assert("Stopping" in command.get_json()["text"], "stop command acknowledged")
_assert(store.outbox["command:trigger-1"]["kind"] == "control", "control outbox")
cancel_body = urllib.parse.urlencode(
    {
        "team_id": "T123",
        "channel_id": "C1",
        "trigger_id": "trigger-2",
        "command": "/cancel",
        "user_id": "UADMIN",
    }
).encode()
cancel = client.post(
    "/slack/commands",
    data=cancel_body,
    content_type="application/x-www-form-urlencoded",
    headers=_signed_headers(cancel_body),
)
_assert("Stopping" in cancel.get_json()["text"], "cancel matches stop")
_assert(store.outbox["command:trigger-2"]["kind"] == "control", "cancel control outbox")
unsupported_body = urllib.parse.urlencode(
    {
        "team_id": "T123",
        "channel_id": "C1",
        "trigger_id": "trigger-3",
        "command": "/unsupported",
        "user_id": "UINSTALLER",
    }
).encode()
unsupported = client.post(
    "/slack/commands",
    data=unsupported_body,
    content_type="application/x-www-form-urlencoded",
    headers=_signed_headers(unsupported_body),
)
_assert("Unsupported" in unsupported.get_json()["text"], "unsupported command explicit")
_assert("command:trigger-3" not in store.outbox, "unsupported command not enqueued")
unauthorized_body = urllib.parse.urlencode(
    {
        "team_id": "T123",
        "channel_id": "C1",
        "trigger_id": "trigger-4",
        "command": "/stop",
        "user_id": "UMEMBER",
    }
).encode()
unauthorized = client.post(
    "/slack/commands",
    data=unauthorized_body,
    content_type="application/x-www-form-urlencoded",
    headers=_signed_headers(unauthorized_body),
)
_assert("Stopping" in unauthorized.get_json()["text"], "channel member may stop")
_assert(
    store.outbox["command:trigger-4"]["kind"] == "control",
    "channel-member stop is routed without granting tool authority",
)

oversized = b"x" * 1_000_001
oversized_response = client.post(
    "/slack/events",
    data=oversized,
    content_type="application/json",
    headers=_signed_headers(oversized),
)
_assert(oversized_response.status_code == 413, "oversized Slack payload rejected")

placeholder_payload = {
    "tenant_id": REPLIKA_ID,
    "team_id": "T123",
    "dedupe_key": "placeholder-1",
    "source_dedupe_key": event_key,
    "channel": "C1",
    "thread_ts": "100.1",
    "text": None,
    "placeholder_ts": None,
    "create_placeholder": True,
    "delete_placeholder": False,
}
placeholder_body = json.dumps(placeholder_payload, separators=(",", ":")).encode()
placeholder = client.post(
    "/internal/slack/deliver",
    data=placeholder_body,
    content_type="application/json",
    headers=signed_internal_headers(
        REPLIKA_ID, placeholder_body, "tenant-hmac-secret"
    ),
)
_assert(
    placeholder.status_code == 200
    and placeholder.get_json()["placeholder_ts"] == "200.1"
    and store.outbox[event_key]["placeholder_ts"] == "200.1",
    "reply-gate callback posts and persists a Slack placeholder",
)
delivery_payload = {
    "tenant_id": REPLIKA_ID,
    "team_id": "T123",
    "dedupe_key": "reply-1",
    "source_dedupe_key": event_key,
    "channel": "C1",
    "thread_ts": "100.1",
    "text": "reply from runtime",
    "placeholder_ts": "200.1",
    "create_placeholder": False,
    "delete_placeholder": False,
}
delivery_body = json.dumps(delivery_payload, separators=(",", ":")).encode()
delivery = client.post(
    "/internal/slack/deliver",
    data=delivery_body,
    content_type="application/json",
    headers=signed_internal_headers(
        REPLIKA_ID, delivery_body, "tenant-hmac-secret"
    ),
)
_assert(delivery.status_code == 202, "authenticated outbound accepted")
_assert(
    store.outbox["outbound:reply-1"]["payload"]["thread_ts"] == "100.1"
    and store.outbox["outbound:reply-1"]["payload"]["placeholder_ts"] == "200.1",
    "outbound delivery is durable, thread-aware, and updates its placeholder",
)
delete_payload = {
    **delivery_payload,
    "dedupe_key": "reply-delete",
    "text": None,
    "delete_placeholder": True,
}
delete_body = json.dumps(delete_payload, separators=(",", ":")).encode()
delete_response = client.post(
    "/internal/slack/deliver",
    data=delete_body,
    content_type="application/json",
    headers=signed_internal_headers(
        REPLIKA_ID, delete_body, "tenant-hmac-secret"
    ),
)
_assert(delete_response.status_code == 202, "placeholder deletion accepted")
too_long = {**delivery_payload, "dedupe_key": "reply-too-long", "text": "x" * 40_001}
too_long_body = json.dumps(too_long, separators=(",", ":")).encode()
too_long_response = client.post(
    "/internal/slack/deliver",
    data=too_long_body,
    content_type="application/json",
    headers=signed_internal_headers(
        REPLIKA_ID, too_long_body, "tenant-hmac-secret"
    ),
)
_assert(too_long_response.status_code == 400, "oversized outbound text rejected")

disconnected = client.delete(f"/api/replikas/{REPLIKA_ID}/integrations/slack")
_assert(disconnected.status_code == 200 and store.installation is None, "disconnect")
_assert(
    all("auth/" in ref for ref in vault.values),
    "disconnect deletes the Slack token but preserves tenant authentication",
)

individual_ref = vault.put(REPLIKA_ID, "TDM", "xoxb-individual")
store.upsert_installation(
    {
        "owner_id": OWNER_ID,
        "replika_id": REPLIKA_ID,
        "team_id": "TDM",
        "team_name": "DM Workspace",
        "replika_type": "individual",
        "token_ref": individual_ref,
        "installer_user_id": "UINSTALLER",
    }
)
dm_channels = client.get(
    f"/api/replikas/{REPLIKA_ID}/integrations/slack/channels"
).get_json()
_assert(dm_channels == {"channels": [], "mode": "dm"}, "individual exposes DM mode")
dm_payload = {
    "type": "event_callback",
    "team_id": "TDM",
    "event_id": "EvDM",
    "event": {
        "type": "message",
        "channel": "D123",
        "channel_type": "im",
        "user": "UINSTALLER",
        "text": "private hello",
    },
}
dm_body = json.dumps(dm_payload, separators=(",", ":")).encode()
dm_event = client.post(
    "/slack/events",
    data=dm_body,
    content_type="application/json",
    headers=_signed_headers(dm_body),
)
_assert(dm_event.get_json()["accepted"], "individual DM event routed")
other_user_payload = {
    **dm_payload,
    "event_id": "EvOtherUser",
    "event": {**dm_payload["event"], "user": "UOTHER"},
}
other_user_body = json.dumps(other_user_payload, separators=(",", ":")).encode()
other_user = client.post(
    "/slack/events",
    data=other_user_body,
    content_type="application/json",
    headers=_signed_headers(other_user_body),
)
_assert(not other_user.get_json()["accepted"], "individual route is installer-only")
channel_rejected = client.put(
    f"/api/replikas/{REPLIKA_ID}/integrations/slack/channel",
    json={"channel_id": "C1"},
)
_assert(channel_rejected.status_code == 400, "individual cannot select a channel")

print("Slack integration checks passed.")

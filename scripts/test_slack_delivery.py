"""Focused checks for Slack dispatcher, tenant boundaries, and async delivery."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["SLACK_ALLOW_INSECURE_TENANT_URLS"] = "true"

from flask import Flask  # noqa: E402

from harness.slack_observations import (  # noqa: E402
    MemorySlackObservationStore,
    ReplyDecision,
)
from tower.slack_integration import (  # noqa: E402
    SLACK_NAME_LOOKUPS_PER_DELIVERY,
    SLACK_ROSTER_MAX_MEMBERS,
    SlackOutboxDispatcher,
    internal_signature_valid,
    signed_internal_headers,
    start_slack_dispatcher,
    validated_tenant_url,
)
from tower.slack_runtime import register_slack_runtime  # noqa: E402


def check(condition, message):
    if not condition:
        raise AssertionError(message)


os.environ.pop("SLACK_ALLOW_INSECURE_TENANT_URLS", None)
os.environ["SLACK_TENANT_PRODUCT_DOMAIN"] = "example.com"
check(
    validated_tenant_url("https://tenant.example.com") == "https://tenant.example.com",
    "configured HTTPS tenant subdomain accepted",
)
for unsafe_url in (
    "http://tenant.example.com",
    "https://tenant.example.com.evil.test",
    "https://127.0.0.1",
    "https://user@tenant.example.com",
):
    try:
        validated_tenant_url(unsafe_url)
    except RuntimeError:
        pass
    else:
        raise AssertionError(f"unsafe tenant URL accepted: {unsafe_url}")
os.environ["SLACK_ALLOW_INSECURE_TENANT_URLS"] = "true"


class ClaimStore:
    def __init__(self, items):
        self.items = list(items)
        self.deliveries = []
        self.failures = []
        self.auth_refs = []
        self.placeholders = []
        self.directories = []

    def claim(self, worker_id):
        if not self.items:
            return None
        item = self.items.pop(0)
        item["attempts"] = item.get("attempts", 0) + 1
        return item

    def delivered(self, item_id, worker_id):
        self.deliveries.append(item_id)

    def failed(self, item_id, worker_id, error, attempts):
        self.failures.append((item_id, error, attempts))

    def set_auth_ref(self, owner_id, auth_ref):
        self.auth_refs.append((owner_id, auth_ref))

    def set_runtime_placeholder(self, item_id, placeholder_ts):
        self.placeholders.append((item_id, placeholder_ts))
        return True

    def set_directory(self, replika_id, team_id, directory):
        self.directories.append((replika_id, team_id, directory))
        return True


class Vault:
    def ensure(self, owner_id):
        return f"secret://{owner_id}"

    def get(self, ref):
        return "tenant-secret" if ref.startswith("secret://") else "xoxb-token"


class TenantTransport:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def post(self, url, payload, headers):
        self.calls.append((url, payload, headers))
        if self.fail:
            raise RuntimeError("temporary tenant failure")
        body = json.dumps(payload, separators=(",", ":")).encode()
        check(
            internal_signature_valid("tenant-1", body, "tenant-secret", headers),
            "dispatcher signs tenant request",
        )


class SlackApi:
    def __init__(self):
        self.calls = []

    def call(self, method, *, token=None, **params):
        self.calls.append((method, token, params))
        return {"ok": True, "ts": "200.1"}


installation = {
    "owner_id": "tenant-1",
    "team_id": "T1",
    "replika_type": "organization",
    "admin_user_ids": ["UADMIN"],
    "selected_channel": {"id": "C1"},
    "token_ref": "token://1",
}
ingress_item = {
    "_id": "E1",
    "dedupe_key": "E1",
    "owner_id": "tenant-1",
    "team_id": "T1",
    "kind": "event",
    "payload": {"event_id": "E1", "event": {"type": "message"}},
}
store = ClaimStore([dict(ingress_item)])
transport = TenantTransport()
dispatcher = SlackOutboxDispatcher(
    store,
    installation_resolver=lambda owner: installation,
    tenant_url_resolver=lambda owner: "https://tenant.example",
    auth_vault=Vault(),
    token_vault=Vault(),
    transport=transport,
    slack_api=SlackApi(),
)
check(dispatcher.dispatch_once(), "dispatcher claims work")
check(store.deliveries == ["E1"], "dispatcher records successful delivery")
check(store.auth_refs == [("tenant-1", "secret://tenant-1")], "auth ref persisted")

placeholder_item = {
    **ingress_item,
    "_id": "E-PLACEHOLDER",
    "dedupe_key": "E-PLACEHOLDER",
    "payload": {
        "event_id": "E-PLACEHOLDER",
        "event": {
            "type": "app_mention",
            "channel": "C1",
            "thread_ts": "100.1",
            "text": "<@B1> hello",
        },
    },
}
placeholder_store = ClaimStore([placeholder_item])
placeholder_transport = TenantTransport()
placeholder_slack = SlackApi()
SlackOutboxDispatcher(
    placeholder_store,
    installation_resolver=lambda owner: {**installation, "bot_user_id": "B1"},
    tenant_url_resolver=lambda owner: "https://tenant.example",
    auth_vault=Vault(),
    token_vault=Vault(),
    transport=placeholder_transport,
    slack_api=placeholder_slack,
).dispatch_once()
check(
    not [call for call in placeholder_slack.calls if call[0] == "chat.postMessage"]
    and placeholder_transport.calls[0][1]["placeholder_ts"] is None,
    "dispatcher waits for the runtime reply decision before posting a placeholder",
)

# The tenant runtime holds no Slack token, so the dispatcher resolves display
# names and channel membership centrally and ships them with the event.
class DirectorySlackApi:
    def __init__(self):
        self.calls = []

    def call(self, method, *, token=None, **params):
        self.calls.append((method, token, params))
        if method == "conversations.members":
            return {"ok": True, "members": ["U2", "U3", "B1"]}
        if method == "users.info":
            names = {"U2": "Priya Sharma", "U3": "Rahul Verma"}
            return {"ok": True, "user": {"profile": {"display_name": names[params["user"]]}}}
        return {"ok": True, "ts": "200.1"}


directory_item = {
    **ingress_item,
    "_id": "E-DIR",
    "dedupe_key": "E-DIR",
    "payload": {
        "event_id": "E-DIR",
        "event": {"type": "message", "channel": "C1", "user": "U2", "text": "hi"},
    },
}
directory_store = ClaimStore([directory_item])
directory_transport = TenantTransport()
directory_slack = DirectorySlackApi()
SlackOutboxDispatcher(
    directory_store,
    installation_resolver=lambda owner: {
        **installation,
        "replika_id": "tenant-1",
        "bot_user_id": "B1",
        "selected_channel": {"id": "C1", "name": "eng"},
    },
    tenant_url_resolver=lambda owner: "https://tenant.example",
    auth_vault=Vault(),
    token_vault=Vault(),
    transport=directory_transport,
    slack_api=directory_slack,
).dispatch_once()
delivered = directory_transport.calls[0][1]
check(
    delivered["sender_display_name"] == "Priya Sharma"
    and delivered["channel_member_names"] == ["Priya Sharma", "Rahul Verma"],
    "dispatcher ships resolved sender name and channel roster to the tenant",
)
check(
    directory_store.directories
    and directory_store.directories[0][2]["user_names"]["U2"] == "Priya Sharma",
    "resolved names are cached on the installation for reuse",
)

# Second delivery reuses the cache: no further users.info round-trips.
cached_directory = directory_store.directories[0][2]
reuse_store = ClaimStore([{**directory_item, "_id": "E-DIR2", "dedupe_key": "E-DIR2"}])
reuse_slack = DirectorySlackApi()
SlackOutboxDispatcher(
    reuse_store,
    installation_resolver=lambda owner: {
        **installation,
        "replika_id": "tenant-1",
        "bot_user_id": "B1",
        "selected_channel": {"id": "C1", "name": "eng"},
        "slack_directory": cached_directory,
    },
    tenant_url_resolver=lambda owner: "https://tenant.example",
    auth_vault=Vault(),
    token_vault=Vault(),
    transport=TenantTransport(),
    slack_api=reuse_slack,
).dispatch_once()
check(
    not [call for call in reuse_slack.calls if call[0] == "users.info"],
    "cached Slack names are reused instead of re-resolved per message",
)
# This Mongo client is built without tz_aware, so a stored datetime returns
# naive and comparing it against an aware now() raises — which would silently
# drop every name and roster after the first message. Keep the stamp a float.
check(
    isinstance(cached_directory["refreshed_at"], float),
    "directory freshness is stamped as epoch seconds, never a datetime",
)
stale_directory = {**cached_directory, "refreshed_at": 0.0}
stale_store = ClaimStore([{**directory_item, "_id": "E-DIR3", "dedupe_key": "E-DIR3"}])
stale_transport = TenantTransport()
stale_slack = DirectorySlackApi()
SlackOutboxDispatcher(
    stale_store,
    installation_resolver=lambda owner: {
        **installation,
        "replika_id": "tenant-1",
        "bot_user_id": "B1",
        "selected_channel": {"id": "C1", "name": "eng"},
        "slack_directory": stale_directory,
    },
    tenant_url_resolver=lambda owner: "https://tenant.example",
    auth_vault=Vault(),
    token_vault=Vault(),
    transport=stale_transport,
    slack_api=stale_slack,
).dispatch_once()
check(
    stale_transport.calls[0][1]["channel_member_names"] == ["Priya Sharma", "Rahul Verma"]
    and "conversations.members" in [call[0] for call in stale_slack.calls],
    "an expired roster re-lists membership and still ships names",
)

# Name resolution runs inside the delivery path, so a large channel must not
# stall messages behind hundreds of serial users.info calls.
class BigChannelSlackApi:
    def __init__(self):
        self.calls = []

    def call(self, method, *, token=None, **params):
        self.calls.append((method, token, params))
        if method == "conversations.members":
            return {"ok": True, "members": [f"U{index:03d}" for index in range(400)]}
        if method == "users.info":
            return {"ok": True, "user": {"profile": {"display_name": params["user"] + "-name"}}}
        return {"ok": True, "ts": "200.1"}


big_store = ClaimStore([{**directory_item, "_id": "E-BIG", "dedupe_key": "E-BIG"}])
big_transport = TenantTransport()
big_slack = BigChannelSlackApi()
SlackOutboxDispatcher(
    big_store,
    installation_resolver=lambda owner: {
        **installation,
        "replika_id": "tenant-1",
        "bot_user_id": "B1",
        "selected_channel": {"id": "C1", "name": "eng"},
    },
    tenant_url_resolver=lambda owner: "https://tenant.example",
    auth_vault=Vault(),
    token_vault=Vault(),
    transport=big_transport,
    slack_api=big_slack,
).dispatch_once()
lookups = [call for call in big_slack.calls if call[0] == "users.info"]
check(
    len(lookups) <= SLACK_NAME_LOOKUPS_PER_DELIVERY,
    "one delivery never resolves more than its name-lookup budget",
)
check(
    lookups[0][2]["user"] == "U2",
    "the sender is resolved first, ahead of the rest of the roster",
)
check(
    len(big_transport.calls[0][1]["channel_member_names"]) <= SLACK_ROSTER_MAX_MEMBERS,
    "the shipped roster is bounded for large channels",
)
check(
    big_transport.calls[0][1]["sender_display_name"] == "U2-name",
    "the sender name still resolves in a large channel",
)

# Slack does not promise a member order; the roster text is re-injected
# verbatim every turn, so an unstable order would move the cached prefix.
class ShuffledSlackApi(DirectorySlackApi):
    def call(self, method, *, token=None, **params):
        if method == "conversations.members":
            self.calls.append((method, token, params))
            return {"ok": True, "members": ["U3", "B1", "U2"]}
        return super().call(method, token=token, **params)


shuffled_transport = TenantTransport()
SlackOutboxDispatcher(
    ClaimStore([{**directory_item, "_id": "E-DIR4", "dedupe_key": "E-DIR4"}]),
    installation_resolver=lambda owner: {
        **installation,
        "replika_id": "tenant-1",
        "bot_user_id": "B1",
        "selected_channel": {"id": "C1", "name": "eng"},
    },
    tenant_url_resolver=lambda owner: "https://tenant.example",
    auth_vault=Vault(),
    token_vault=Vault(),
    transport=shuffled_transport,
    slack_api=ShuffledSlackApi(),
).dispatch_once()
check(
    shuffled_transport.calls[0][1]["channel_member_names"]
    == ["Priya Sharma", "Rahul Verma"],
    "roster order is stable regardless of Slack member ordering",
)

retry_store = ClaimStore([dict(ingress_item)])
retry_dispatcher = SlackOutboxDispatcher(
    retry_store,
    installation_resolver=lambda owner: installation,
    tenant_url_resolver=lambda owner: "https://tenant.example",
    auth_vault=Vault(),
    token_vault=Vault(),
    transport=TenantTransport(fail=True),
    slack_api=SlackApi(),
)
retry_dispatcher.dispatch_once()
check(retry_store.failures and retry_store.failures[0][0] == "E1", "failure recorded")

outbound = {
    "_id": "O1",
    "dedupe_key": "outbound:1",
    "owner_id": "tenant-1",
    "team_id": "T1",
    "kind": "outbound",
    "payload": {"channel": "C1", "thread_ts": "100.1", "text": "hello"},
}
outbound_store = ClaimStore([outbound])
slack = SlackApi()
SlackOutboxDispatcher(
    outbound_store,
    installation_resolver=lambda owner: installation,
    tenant_url_resolver=lambda owner: "unused",
    auth_vault=Vault(),
    token_vault=Vault(),
    transport=TenantTransport(),
    slack_api=slack,
).dispatch_once()
check(slack.calls[0][2]["thread_ts"] == "100.1", "outbound preserves thread")
check(outbound_store.deliveries == ["O1"], "outbound success recorded")

update_store = ClaimStore([{
    **outbound,
    "_id": "O-UPDATE",
    "payload": {**outbound["payload"], "placeholder_ts": "200.1"},
}])
update_slack = SlackApi()
SlackOutboxDispatcher(
    update_store,
    installation_resolver=lambda owner: installation,
    tenant_url_resolver=lambda owner: "unused",
    auth_vault=Vault(),
    token_vault=Vault(),
    transport=TenantTransport(),
    slack_api=update_slack,
).dispatch_once()
check(
    update_slack.calls[0][0] == "chat.update"
    and update_slack.calls[0][2]["ts"] == "200.1",
    "final response replaces the thinking placeholder",
)


class InjectedDispatcher:
    def __init__(self):
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False


factory_app = Flask(__name__)
injected_dispatcher = InjectedDispatcher()
factory_app.config.update(
    SLACK_DISPATCHER_ENABLED=True,
    SLACK_OUTBOX_DISPATCHER=injected_dispatcher,
)
check(
    start_slack_dispatcher(factory_app) is injected_dispatcher
    and injected_dispatcher.started,
    "app factory accepts an injected dispatcher without storage initialization",
)


class Agent:
    def __init__(self, response="runtime reply"):
        self.enqueued = []
        self.stops = 0
        self.response = response
        self.channel_context = {}

    async def enqueue(self, payload, **kwargs):
        self.enqueued.append((payload, kwargs))
        return {"id": f"item-{len(self.enqueued)}"}

    async def await_enqueued(self, item_id):
        return self.response

    def request_stop(self, channel):
        self.stops += 1
        return True

    def set_channel_context(self, channel, text):
        self.channel_context[channel] = text


class Scheduler:
    def __init__(self, loop):
        self._loop = loop


class AlwaysRespondGate:
    async def decide(self, **kwargs):
        return ReplyDecision(True, "agent_input_needed")


class NeverRespondGate:
    def __init__(self):
        self.decided = threading.Event()

    async def decide(self, **kwargs):
        self.decided.set()
        return ReplyDecision(False, "social_acknowledgement")


class NoopArchiver:
    def schedule(self):
        pass


class DeliveryTransport:
    def __init__(self):
        self.calls = []
        self.event = threading.Event()

    def post(self, url, payload, headers):
        self.calls.append((url, payload, headers))
        if payload.get("create_placeholder"):
            return {"placeholder_ts": "200.1"}
        self.event.set()
        return {}


loop = asyncio.new_event_loop()
thread = threading.Thread(target=loop.run_forever, daemon=True)
thread.start()


def runtime_client(
    replika_type,
    *,
    owner="UOWNER",
    channel="CSELECTED",
    response="runtime reply",
    reply_gate=None,
):
    app = Flask(__name__)
    agent = Agent(response)
    delivery = DeliveryTransport()
    app.config.update(
        REPLIKA_TENANT_ID="tenant-1",
        REPLIKA_TYPE=replika_type,
        SLACK_TENANT_AUTH_SECRET="tenant-secret",
        SLACK_CONTROL_PLANE_URL="https://control.example",
        SLACK_CENTRAL_TRANSPORT=delivery,
        SLACK_OBSERVATION_STORE=MemorySlackObservationStore(),
        SLACK_REPLY_GATE=reply_gate or AlwaysRespondGate(),
        SLACK_OBSERVATION_ARCHIVER=NoopArchiver(),
    )
    register_slack_runtime(app, agent, Scheduler(loop))
    return app.test_client(), agent, delivery, owner, channel


def post_ingress(client, data):
    body = json.dumps(data, separators=(",", ":")).encode()
    return client.post(
        "/internal/slack/ingress",
        data=body,
        content_type="application/json",
        headers=signed_internal_headers("tenant-1", body, "tenant-secret"),
    )


client, agent, delivery, owner, _ = runtime_client("individual")
base = {
    "tenant_id": "tenant-1",
    "team_id": "T1",
    "replika_type": "individual",
    "installer_user_id": owner,
    "admin_user_ids": [owner],
    "selected_channel": None,
    "bot_user_id": "B1",
    "kind": "event",
    "dedupe_key": "E-DM",
    "payload": {
        "event_id": "E-DM",
        "event": {
            "type": "message",
            "channel": "D1",
            "channel_type": "im",
            "user": owner,
            "text": "private",
            "thread_ts": "100.2",
        },
    },
}
check(post_ingress(client, base).status_code == 202, "owner DM accepted")
check(delivery.event.wait(2), "runtime sends async response")
check(
    delivery.calls[0][1]["create_placeholder"]
    and delivery.calls[1][1]["thread_ts"] == "100.2",
    "runtime requests a placeholder before agent delivery",
)
check(
    delivery.calls[1][1]["placeholder_ts"] == "200.1"
    and not delivery.calls[1][1]["delete_placeholder"],
    "runtime returns the placeholder timestamp for in-place update",
)
empty_client, _, empty_delivery, empty_owner, _ = runtime_client(
    "individual", response=""
)
empty = json.loads(json.dumps(base))
empty["dedupe_key"] = "E-EMPTY"
empty["payload"]["event_id"] = "E-EMPTY"
empty["payload"]["event"]["user"] = empty_owner
check(post_ingress(empty_client, empty).status_code == 202, "empty-result DM accepted")
check(empty_delivery.event.wait(2), "empty result triggers placeholder cleanup")
check(
    empty_delivery.calls[0][1]["create_placeholder"]
    and empty_delivery.calls[1][1]["delete_placeholder"]
    and empty_delivery.calls[1][1]["text"] is None,
    "empty result deletes the thinking placeholder",
)
other = json.loads(json.dumps(base))
other["dedupe_key"] = "E-OTHER"
other["payload"]["event_id"] = "E-OTHER"
other["payload"]["event"]["user"] = "UOTHER"
check(not post_ingress(client, other).get_json()["accepted"], "other DM user rejected")
check(len(agent.enqueued) == 1, "rejected DM not enqueued")
check(
    agent.enqueued[0][1]["request_context"]["trusted"],
    "individual owner DM is trusted",
)
check(
    agent.enqueued[0][0].startswith("[Slack]: "),
    "individual Slack messages carry the bare surface tag",
)
check(not agent.channel_context, "individual Slack installs get no channel roster")

org_client, org_agent, org_delivery, _, selected = runtime_client("organization")
org = json.loads(json.dumps(base))
org.update(
    replika_type="organization",
    selected_channel={"id": selected},
    dedupe_key="E-ORG",
)
org["payload"]["event_id"] = "E-ORG"
org["payload"]["event"].update(
    channel=selected,
    channel_type="channel",
    user="U2",
    text="organization request without a mention",
    ts="100.0",
)
org["selected_channel"] = {"id": selected, "name": "eng"}
org["sender_display_name"] = "Priya Sharma"
org["channel_member_names"] = ["Priya Sharma", "Rahul Verma"]
check(post_ingress(org_client, org).get_json()["accepted"], "selected channel accepted")
check(org_delivery.event.wait(2), "organization response delivered asynchronously")
check(
    org_agent.enqueued[0][0].startswith("[Slack/Priya Sharma]: "),
    "organization Slack messages carry the resolved sender name",
)
roster = org_agent.channel_context.get("main") or ""
check(
    "#eng" in roster and "Priya Sharma, Rahul Verma" in roster,
    "organization Slack installs push the channel roster as system context",
)
check(
    org_delivery.calls[0][1]["create_placeholder"]
    and org_delivery.calls[1][1]["placeholder_ts"] == "200.1",
    "reply-gate-approved organization message gets a placeholder before processing",
)
# Central name resolution is best-effort; a failed lookup must not replace a
# good roster with "(none yet)", and the sender falls back to the raw id.
blank_client, blank_agent, blank_delivery, _, _ = runtime_client("organization")
check(post_ingress(blank_client, org).get_json()["accepted"], "named org message accepted")
check(blank_delivery.event.wait(2), "named org message delivered")
named_roster = blank_agent.channel_context.get("main")
blank = json.loads(json.dumps(org))
blank["dedupe_key"] = "E-ORG-BLANK"
blank["payload"]["event_id"] = "E-ORG-BLANK"
blank["payload"]["event"]["ts"] = "100.5"
blank["sender_display_name"] = None
blank["channel_member_names"] = []
blank_delivery.event.clear()
check(post_ingress(blank_client, blank).get_json()["accepted"], "unnamed org message accepted")
check(blank_delivery.event.wait(2), "unnamed org message still delivered")
check(
    blank_agent.channel_context.get("main") == named_roster,
    "a failed name lookup never wipes the existing channel roster",
)
check(
    blank_agent.enqueued[-1][0].startswith("[Slack/U2]: "),
    "an unresolved sender falls back to the raw Slack user id",
)

ignore_gate = NeverRespondGate()
ignore_client, ignore_agent, ignore_delivery, _, _ = runtime_client(
    "organization", reply_gate=ignore_gate
)
ignored = json.loads(json.dumps(org))
ignored["dedupe_key"] = "E-IGNORED"
ignored["payload"]["event_id"] = "E-IGNORED"
ignored["payload"]["event"]["ts"] = "102.0"
check(post_ingress(ignore_client, ignored).status_code == 202, "ignored event accepted")
check(ignore_gate.decided.wait(2), "reply gate considered ignored event")
check(
    not ignore_delivery.calls and not ignore_agent.enqueued,
    "reply-gate-rejected message creates no placeholder",
)
check(
    not org_agent.enqueued[0][1]["request_context"]["trusted"],
    "ordinary organization member is untrusted for sensitive tools",
)
admin_event = json.loads(json.dumps(org))
admin_event["dedupe_key"] = "E-ADMIN"
admin_event["payload"]["event_id"] = "E-ADMIN"
admin_event["payload"]["event"]["ts"] = "101.0"
admin_event["payload"]["event"]["user"] = owner
check(post_ingress(org_client, admin_event).status_code == 202, "admin event accepted")
deadline = time.time() + 2
while len(org_agent.enqueued) < 2 and time.time() < deadline:
    time.sleep(0.01)
check(
    org_agent.enqueued[1][1]["request_context"]["trusted"],
    "OAuth installer identity is trusted for sensitive tools",
)
wrong = json.loads(json.dumps(org))
wrong["dedupe_key"] = "E-WRONG"
wrong["payload"]["event_id"] = "E-WRONG"
wrong["payload"]["event"]["channel"] = "COTHER"
check(not post_ingress(org_client, wrong).get_json()["accepted"], "other channel rejected")

for command, actor in (("/stop", owner), ("/cancel", "UMEMBER")):
    control = {
        "tenant_id": "tenant-1",
        "team_id": "T1",
        "replika_type": "organization",
        "installer_user_id": owner,
        "admin_user_ids": [owner],
        "selected_channel": {"id": "C1"},
        "kind": "control",
        "dedupe_key": f"control:{command}",
        "payload": {"command": command, "user_id": actor, "channel_id": "C1"},
    }
    check(post_ingress(org_client, control).status_code == 202, f"{command} accepted")
deadline = time.time() + 2
while org_agent.stops < 2 and time.time() < deadline:
    time.sleep(0.01)
check(org_agent.stops == 2, "stop and cancel have identical cancellation behavior")
check(len(org_agent.enqueued) == 2, "control commands are never enqueued")

loop.call_soon_threadsafe(loop.stop)
thread.join(timeout=2)
loop.close()
print("Slack delivery and runtime checks passed.")

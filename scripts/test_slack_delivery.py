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
        return {"ok": True}


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
    def __init__(self):
        self.enqueued = []
        self.stops = 0

    async def enqueue(self, payload, **kwargs):
        self.enqueued.append((payload, kwargs))
        return {"id": f"item-{len(self.enqueued)}"}

    async def await_enqueued(self, item_id):
        return "runtime reply"

    def request_stop(self, channel):
        self.stops += 1
        return True


class Scheduler:
    def __init__(self, loop):
        self._loop = loop


class AlwaysRespondGate:
    async def decide(self, **kwargs):
        return ReplyDecision(True, "agent_input_needed")


class NoopArchiver:
    def schedule(self):
        pass


class DeliveryTransport:
    def __init__(self):
        self.calls = []
        self.event = threading.Event()

    def post(self, url, payload, headers):
        self.calls.append((url, payload, headers))
        self.event.set()


loop = asyncio.new_event_loop()
thread = threading.Thread(target=loop.run_forever, daemon=True)
thread.start()


def runtime_client(replika_type, *, owner="UOWNER", channel="CSELECTED"):
    app = Flask(__name__)
    agent = Agent()
    delivery = DeliveryTransport()
    app.config.update(
        REPLIKA_TENANT_ID="tenant-1",
        REPLIKA_TYPE=replika_type,
        SLACK_TENANT_AUTH_SECRET="tenant-secret",
        SLACK_CONTROL_PLANE_URL="https://control.example",
        SLACK_CENTRAL_TRANSPORT=delivery,
        SLACK_OBSERVATION_STORE=MemorySlackObservationStore(),
        SLACK_REPLY_GATE=AlwaysRespondGate(),
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
check(delivery.calls[0][1]["thread_ts"] == "100.2", "runtime preserves thread")
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
    text="<@B1> organization request",
    ts="100.0",
)
check(post_ingress(org_client, org).get_json()["accepted"], "selected channel accepted")
check(org_delivery.event.wait(2), "organization response delivered asynchronously")
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

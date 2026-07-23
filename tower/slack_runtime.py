"""Authenticated tenant-side Slack ingress and response relay."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

from flask import Blueprint, current_app, jsonify, request

from harness.slack_observations import (
    ReplyGate,
    SlackObservationArchiver,
    default_observation_store,
    explicit_bot_address,
    observation_overlay,
)

from .slack_integration import (
    MAX_SLACK_EVENT_TEXT_CHARS,
    MAX_SLACK_FILES,
    internal_signature_valid,
    signed_internal_headers,
)

log = logging.getLogger("galadriel.slack.runtime")
MAX_INTERNAL_SLACK_BYTES = 1_000_000


class CentralSlackTransport:
    """Injectable transport for authenticated runtime-to-control delivery."""

    def post(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status not in {200, 202}:
                    raise RuntimeError(f"control plane returned HTTP {response.status}")
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError("central Slack delivery request failed") from exc


def _constant_equal(left: Any, right: Any) -> bool:
    return hmac.compare_digest(str(left or ""), str(right or ""))


def register_slack_runtime(app, agent, scheduler=None, channel_id: str = "main") -> None:
    bp = Blueprint("slack_runtime", __name__)
    observation_store = (
        app.config.get("SLACK_OBSERVATION_STORE") or default_observation_store()
    )
    reply_gate = app.config.get("SLACK_REPLY_GATE") or ReplyGate()
    observation_archiver = app.config.get("SLACK_OBSERVATION_ARCHIVER") or (
        SlackObservationArchiver(observation_store)
    )

    def setting(name: str) -> str:
        return str(current_app.config.get(name) or os.environ.get(name) or "")

    def event_allowed(data: dict[str, Any], event: dict[str, Any]) -> bool:
        replika_type = setting("REPLIKA_TYPE")
        if (
            replika_type not in {"individual", "organization"}
            or data.get("replika_type") != replika_type
            or event.get("type") not in {"message", "app_mention"}
            or event.get("bot_id")
            or not event.get("channel")
            or len(event.get("files") or []) > MAX_SLACK_FILES
        ):
            return False
        if replika_type == "individual":
            if (
                event.get("type") != "message"
                or event.get("subtype") not in {None, "file_share"}
                or not isinstance(event.get("text"), str)
                or not event["text"].strip()
                or len(event["text"]) > MAX_SLACK_EVENT_TEXT_CHARS
                or not event.get("user")
                or _constant_equal(event.get("user"), data.get("bot_user_id"))
            ):
                return False
            is_dm = event.get("channel_type") == "im" or str(event["channel"]).startswith("D")
            return is_dm and _constant_equal(
                event["user"], data.get("installer_user_id")
            )
        selected = (data.get("selected_channel") or {}).get("id")
        if not selected or not _constant_equal(event["channel"], selected):
            return False
        subtype = event.get("subtype")
        if subtype in {"message_changed", "message_deleted"}:
            nested = event.get("message") or event.get("previous_message") or {}
            return (
                event.get("type") == "message"
                and isinstance(nested, dict)
                and not nested.get("bot_id")
                and bool(nested.get("user"))
                and not _constant_equal(nested.get("user"), data.get("bot_user_id"))
                and len(str(nested.get("text") or "")) <= MAX_SLACK_EVENT_TEXT_CHARS
            )
        return (
            subtype in {None, "file_share"}
            and isinstance(event.get("text"), str)
            and bool(event.get("text", "").strip() or event.get("files"))
            and len(event.get("text", "")) <= MAX_SLACK_EVENT_TEXT_CHARS
            and bool(event.get("user"))
            and not _constant_equal(event.get("user"), data.get("bot_user_id"))
        )

    def send_response(
        *,
        tenant_id: str,
        team_id: str,
        dedupe_key: str,
        source_dedupe_key: str,
        channel: str,
        thread_ts: str | None,
        text: str | None,
        placeholder_ts: str | None,
        delete_placeholder: bool,
        secret: str,
        base_url: str,
        transport,
    ) -> None:
        payload = {
            "tenant_id": tenant_id,
            "team_id": team_id,
            "dedupe_key": dedupe_key,
            "source_dedupe_key": source_dedupe_key,
            "channel": channel,
            "thread_ts": thread_ts,
            "text": text,
            "placeholder_ts": placeholder_ts,
            "delete_placeholder": delete_placeholder,
        }
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers = signed_internal_headers(tenant_id, body, secret)
        if not base_url:
            raise RuntimeError("SLACK_CONTROL_PLANE_URL is not configured")
        transport.post(base_url + "/internal/slack/deliver", payload, headers)

    @bp.post("/internal/slack/ingress")
    def slack_ingress():
        if (request.content_length or 0) > MAX_INTERNAL_SLACK_BYTES:
            return jsonify({"error": "Payload too large"}), 413
        raw = request.get_data(cache=True)
        if len(raw) > MAX_INTERNAL_SLACK_BYTES:
            return jsonify({"error": "Payload too large"}), 413
        data = request.get_json(silent=True) or {}
        tenant_id = setting("REPLIKA_TENANT_ID")
        replika_type = setting("REPLIKA_TYPE")
        secret = setting("SLACK_TENANT_AUTH_SECRET")
        if (
            not tenant_id
            or tenant_id == "default"
            or replika_type not in {"individual", "organization"}
            or not secret
        ):
            return jsonify({"error": "Slack runtime is not configured"}), 503
        if (
            data.get("tenant_id") != tenant_id
            or not internal_signature_valid(tenant_id, raw, secret, request.headers)
        ):
            return jsonify({"error": "Unauthorized"}), 401
        team_id = str(data.get("team_id") or "")
        dedupe_key = str(data.get("dedupe_key") or "")
        kind = str(data.get("kind") or "")
        payload = data.get("payload") or {}
        configured_team = setting("SLACK_TEAM_ID")
        if (
            not team_id
            or (configured_team and not _constant_equal(team_id, configured_team))
            or not dedupe_key
            or len(dedupe_key) > 300
            or kind not in {"event", "control"}
            or not isinstance(data.get("admin_user_ids") or [], list)
        ):
            return jsonify({"error": "Invalid ingress payload"}), 400

        if kind == "control":
            command = str(payload.get("command") or "").lower()
            if command not in {"/stop", "/cancel"}:
                return jsonify({"error": "Unsupported control command"}), 400
            actor_id = str(payload.get("user_id") or "")
            if replika_type == "organization":
                selected = str((data.get("selected_channel") or {}).get("id") or "")
                if not actor_id or str(payload.get("channel_id") or "") != selected:
                    return jsonify({"error": "Configured channel member required"}), 403
            elif actor_id != str(data.get("installer_user_id") or ""):
                return jsonify({"error": "Owner required"}), 403
            loop = scheduler._loop if scheduler else None
            if not (loop and loop.is_running()):
                return jsonify({"error": "Agent event loop unavailable"}), 503
            loop.call_soon_threadsafe(agent.request_stop, channel_id)
            return jsonify({"accepted": True}), 202

        event = payload.get("event") or {}
        if not event_allowed(data, event):
            return jsonify({"accepted": False}), 202
        event_id = str(payload.get("event_id") or dedupe_key)
        channel = str(event["channel"])
        loop = scheduler._loop if scheduler else None
        if not (loop and loop.is_running()):
            return jsonify({"error": "Agent event loop unavailable"}), 503
        thread_ts = event.get("thread_ts")
        if thread_ts is not None and not isinstance(thread_ts, str):
            return jsonify({"error": "Invalid thread timestamp"}), 400
        placeholder_ts = str(data.get("placeholder_ts") or "") or None
        base_url = (
            setting("SLACK_CONTROL_PLANE_URL")
            or setting("REPLIKA_CONTROL_PLANE_URL")
        ).rstrip("/")
        central_transport = (
            current_app.config.get("SLACK_CENTRAL_TRANSPORT")
            or CentralSlackTransport()
        )

        def discard_placeholder() -> None:
            if not placeholder_ts:
                return
            send_response(
                tenant_id=tenant_id,
                team_id=team_id,
                dedupe_key=f"{event_id}:placeholder-delete",
                source_dedupe_key=dedupe_key,
                channel=channel,
                thread_ts=thread_ts,
                text=None,
                placeholder_ts=placeholder_ts,
                delete_placeholder=True,
                secret=secret,
                base_url=base_url,
                transport=central_transport,
            )

        observation = None
        if replika_type == "organization":
            observation = observation_store.observe(
                tenant_id=tenant_id,
                workspace_id=team_id,
                channel_id=channel,
                event=event,
                event_id=event_id,
                event_time=payload.get("event_time"),
            )
            if observation is None:
                discard_placeholder()
                return jsonify({"accepted": False}), 202

        if replika_type == "organization":
            async def archive_observation():
                observation_archiver.schedule()

            asyncio.run_coroutine_threadsafe(archive_observation(), loop)

        if event.get("subtype") in {"message_changed", "message_deleted"}:
            discard_placeholder()
            return jsonify({"accepted": True}), 202

        sender_id = str(event["user"])
        text = event["text"].strip()
        bot_user_id = str(data.get("bot_user_id") or "")
        if replika_type == "organization" and bot_user_id:
            text = text.replace(f"<@{bot_user_id}>", "").strip()
        if not text:
            discard_placeholder()
            return jsonify({"accepted": False}), 202

        async def run_and_deliver():
            response_sent = False
            try:
                overlay = None
                if replika_type == "organization":
                    recent = observation_store.recent(team_id, channel, limit=20)
                    queue = getattr(agent, "conversation_queue", None)
                    inbox_status = (
                        queue.status(channel_id)
                        if queue is not None
                        else {
                            "busy": bool(getattr(agent, "is_channel_busy", lambda _c: False)(channel_id)),
                            "paused": False,
                            "depth": 0,
                        }
                    )
                    decision = await reply_gate.decide(
                        tenant_id=tenant_id,
                        current=observation,
                        recent=recent,
                        inbox_status=inbox_status,
                        explicit_override=explicit_bot_address(event, bot_user_id),
                    )
                    if not decision.should_respond:
                        log.info(
                            "Slack message observed without reply tenant=%s reason=%s",
                            tenant_id,
                            decision.reason_code,
                        )
                        return
                    overlay = observation_overlay(recent)
                item = await agent.enqueue(
                    f"[Slack/{sender_id}]: {text}",
                    channel_id=channel_id,
                    source="slack",
                    external_dedupe_key=f"slack:{team_id}:{event_id}",
                    sender={"id": sender_id},
                    request_context={
                        "source": "slack",
                        "actor_id": sender_id,
                        "tenant_id": tenant_id,
                        "team_id": team_id,
                        "channel_id": channel,
                        "replika_type": replika_type,
                        "trusted": (
                            replika_type == "individual"
                            or sender_id == str(data.get("installer_user_id") or "")
                            or sender_id in {
                                str(value) for value in (data.get("admin_user_ids") or [])
                            }
                        ),
                        "trust_reason": (
                            "individual_owner"
                            if replika_type == "individual"
                            else "configured_slack_admin"
                            if (
                                sender_id == str(data.get("installer_user_id") or "")
                                or sender_id in {
                                    str(value) for value in (data.get("admin_user_ids") or [])
                                }
                            )
                            else "organization_member"
                        ),
                    },
                    display_text=text,
                    reply_target={"channel": channel, "thread_ts": thread_ts},
                    overlay_context=overlay,
                )
                response = await agent.await_enqueued(item["id"])
                # Older items in a merged batch deliberately receive an empty result;
                # only the newest trigger owns the assistant reply.
                if not isinstance(response, str) or not response.strip():
                    return
                await asyncio.to_thread(
                    send_response,
                    tenant_id=tenant_id,
                    team_id=team_id,
                    dedupe_key=f"{event_id}:{item['id']}",
                    source_dedupe_key=dedupe_key,
                    channel=channel,
                    thread_ts=thread_ts,
                    text=response,
                    placeholder_ts=placeholder_ts,
                    delete_placeholder=False,
                    secret=secret,
                    base_url=base_url,
                    transport=central_transport,
                )
                response_sent = True
            finally:
                if placeholder_ts and not response_sent:
                    try:
                        await asyncio.to_thread(discard_placeholder)
                    except Exception:
                        log.warning("Could not delete Slack placeholder", exc_info=True)

        future = asyncio.run_coroutine_threadsafe(run_and_deliver(), loop)

        def report_failure(completed):
            try:
                completed.result()
            except Exception:
                log.exception("Slack runtime processing failed")

        future.add_done_callback(report_failure)
        return jsonify({"accepted": True}), 202

    app.register_blueprint(bp)

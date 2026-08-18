"""Tower UI — local web dashboard for the Galadriel agent."""

import os
import json
import queue
import base64
import asyncio
import logging
import signal
import threading
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, render_template, request, jsonify, Response, redirect, url_for, g
from harness.agent import MAIN_CHANNEL_ID
from harness import tower_settings
from . import auth as tower_auth

log = logging.getLogger("galadriel.tower")

MAX_CHAT_IMAGES = 5
MAX_IMAGE_BYTES = 5 * 1024 * 1024


def _browser_transcribe_credentials() -> dict:
    """Assume the browser-only Transcribe role and serialize its short-lived credentials."""
    for profile_var in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        if not os.environ.get(profile_var, "").strip():
            os.environ.pop(profile_var, None)

    import boto3

    sts = boto3.client("sts")
    role_arn = os.environ.get("VOICE_TRANSCRIBE_ROLE_ARN", "").strip()
    if not role_arn:
        role_name = os.environ.get(
            "VOICE_TRANSCRIBE_ROLE_NAME", "clyra-stag-browser-transcription"
        ).strip()
        if not role_name:
            raise RuntimeError("Voice dictation is not configured")
        account_id = sts.get_caller_identity()["Account"]
        role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"

    response = sts.assume_role(
        RoleArn=role_arn,
        RoleSessionName="replika-browser-dictation",
        DurationSeconds=900,
    )
    credentials = response["Credentials"]
    expiration = credentials["Expiration"]
    if isinstance(expiration, datetime):
        expiration = expiration.astimezone(timezone.utc).isoformat()
    return {
        "accessKeyId": credentials["AccessKeyId"],
        "secretAccessKey": credentials["SecretAccessKey"],
        "sessionToken": credentials["SessionToken"],
        "expiration": expiration,
        "region": os.environ.get("VOICE_TRANSCRIBE_REGION", "ap-south-1"),
    }


def _image_blocks_from_payload(images: list) -> tuple[list, str | None]:
    """Validate base64 chat-upload images and return (image blocks, error).
    Media type is sniffed from magic bytes, not trusted from the client."""
    from discord_bot.bot import sniff_image_media_type

    if len(images) > MAX_CHAT_IMAGES:
        return [], f"Too many images (max {MAX_CHAT_IMAGES})"
    blocks = []
    for img in images:
        data = (img or {}).get("data") or ""
        try:
            raw = base64.b64decode(data, validate=True)
        except Exception:
            return [], "Invalid image data (expected base64)"
        if len(raw) > MAX_IMAGE_BYTES:
            return [], "Image exceeds the 5MB limit"
        media_type = sniff_image_media_type(raw)
        if media_type is None:
            return [], "Unsupported image format (use PNG, JPEG, GIF, or WebP)"
        blocks.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        })
    return blocks, None


def _build_chat_message(message: str, images: list) -> str | list | None:
    """Build the agent-facing user message: plain string, or content blocks
    when images are attached. None when there is nothing to send."""
    if not message and not images:
        return None
    text = f"[Tower]: {message or '(image attached)'}"
    if not images:
        return text
    return [{"type": "text", "text": text}, *images]


def create_tower(agent, scheduler=None, worker=None) -> Flask:
    """Create the Flask Tower app wired to the agent and scheduler."""
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )
    # Local edits to Jinja templates should show up without a process restart.
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    tower_auth.configure_app_sessions(app)

    @app.before_request
    def _require_tower_auth():
        # These routes perform their own per-tenant HMAC authentication.
        if request.path in {"/internal/slack/ingress", "/internal/config/reset"}:
            return None
        if os.environ.get("REPLIKA_CONTROL_PLANE_ONLY", "").lower() in {
            "1", "true", "yes",
        }:
            allowed = (
                request.path in {
                    "/",
                    "/healthz",
                    "/readyz",
                    "/login",
                    "/logout",
                    "/replika",
                }
                or request.path.startswith("/static/")
                or request.path.startswith("/api/replika")
                or request.path.startswith("/internal/replika/")
            )
            if not allowed:
                return jsonify({"error": "Not found"}), 404
        # Health checks, login/logout, and login-page static assets stay public.
        if tower_auth.public_path(request.path):
            return None
        if not tower_auth.auth_required():
            return None
        if not tower_auth.auth_ready():
            return jsonify({"error": "Tower authentication is misconfigured"}), 503

        result = tower_auth.authenticate_request()
        if result is None:
            return tower_auth.unauthorized_response()
        g.tower_auth = result
        if result.method == "alb":
            tower_auth.establish_session(result.username)

        if (
            result.method == "session"
            and request.method not in {"GET", "HEAD", "OPTIONS"}
            and not tower_auth.same_origin_ok()
        ):
            return jsonify({"error": "Cross-origin request rejected"}), 403
        return None

    @app.route("/healthz", methods=["GET"])
    def healthz():
        return jsonify({"status": "ok"}), 200

    @app.route("/readyz", methods=["GET"])
    def readyz():
        if not app.config.get("GALADRIEL_READY", False):
            return jsonify({"status": "starting"}), 503
        if (
            os.environ.get("REPLIKA_CONTROL_PLANE_ONLY", "").lower()
            in {"1", "true", "yes"}
            and not os.environ.get("REPLIKA_PROVISIONER_FUNCTION_ARN")
            and os.environ.get("REPLIKA_ALLOW_LOCAL_PROVISIONING", "").lower()
            not in {"1", "true", "yes"}
        ):
            return jsonify({
                "status": "misconfigured",
                "error": "Replika provisioner is not configured",
            }), 503
        reason = tower_auth.auth_misconfigured_reason()
        if reason:
            return jsonify({"status": "misconfigured", "error": reason}), 503
        from harness.runtime_dependencies import readiness_error

        dependency_error = readiness_error()
        if dependency_error:
            return jsonify({"status": "unready", "error": dependency_error}), 503
        return jsonify({"status": "ready"}), 200

    @app.route("/login", methods=["GET", "POST"])
    def login():
        next_path = tower_auth.safe_next_url(
            request.values.get("next") or request.args.get("next")
        )
        if not tower_auth.auth_required():
            return redirect(next_path)

        if tower_auth.authenticate_request() is not None:
            return redirect(next_path)

        error = None
        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            if tower_auth.credentials_match(username, password):
                tower_auth.establish_session(username)
                return redirect(next_path)
            error = "Invalid username or password"

        return render_template("login.html", error=error, next_path=next_path), (
            401 if error else 200
        )

    @app.route("/logout", methods=["POST"])
    def logout():
        return tower_auth.logout_response()

    @app.context_processor
    def _inject_page_context():
        return {
            "page_context": {},
            "control_plane_only": os.environ.get(
                "REPLIKA_CONTROL_PLANE_ONLY", ""
            ).lower() in {"1", "true", "yes"},
        }

    from .replika_control_plane import register_replika_control_plane
    register_replika_control_plane(app)
    from .slack_runtime import register_slack_runtime
    register_slack_runtime(app, agent, scheduler, MAIN_CHANNEL_ID)
    from .config_reset import register_config_reset
    register_config_reset(app)
    from .phone_bridge import register_phone_bridge
    register_phone_bridge(app)
    from .devices_board import register_devices_board
    register_devices_board(app)

    @app.template_filter("truncate_label")
    def truncate_label(value, length=24):
        """Short label for icon tiles; full text goes in the title attribute."""
        s = str(value or "")
        n = int(length)
        if len(s) <= n:
            return s
        return s[: n - 1] + "…"

    @app.route("/")
    def index():
        if os.environ.get("REPLIKA_CONTROL_PLANE_ONLY", "").lower() in {
            "1", "true", "yes",
        }:
            return redirect(url_for("replika_control_plane.replika_setup"))
        # Default home is the new-chat landing (plan/progress + prompt).
        return redirect(url_for("chats_board.chats_index", kind="chat", **{
            k: v for k, v in request.args.items() if k == "saved"
        }))

    @app.route("/api/chat", methods=["POST"])
    def api_chat():
        data = request.json or {}
        message = data.get("message", "").strip()
        image_blocks, img_err = _image_blocks_from_payload(data.get("images") or [])
        if img_err:
            return jsonify({"error": img_err}), 400
        user_message = _build_chat_message(message, image_blocks)
        if user_message is None:
            return jsonify({"error": "Empty message"}), 400

        from .ui_context import format_overlay_system_block

        context = (request.json or {}).get("context")
        overlay = format_overlay_system_block(context)
        actor = getattr(getattr(g, "tower_auth", None), "username", None) or "tower-user"
        request_context = {
            "source": "tower", "actor_id": str(actor), "trusted": True,
            "trust_reason": "authenticated_tower",
        }

        # Schedule the async agent call onto the main event loop (Discord's loop)
        # This avoids creating a new event loop and works with AsyncAnthropic
        if scheduler and scheduler._loop and scheduler._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                agent.enqueue_and_await(
                    user_message,
                    channel_id=MAIN_CHANNEL_ID,
                    source="tower",
                    external_dedupe_key=request.headers.get("X-Request-Id"),
                    display_text=message or "(image attached)",
                    overlay_context=overlay,
                    request_context=request_context,
                ),
                scheduler._loop,
            )
            try:
                response = future.result(timeout=1200)  # 20 minutes timeout
                return jsonify({"response": response})
            except Exception as e:
                log.exception("Tower chat error")
                return jsonify({"error": str(e)}), 500
        else:
            # Fallback: create a new event loop (shouldn't normally happen)
            loop = asyncio.new_event_loop()
            try:
                response = loop.run_until_complete(
                    agent.enqueue_and_await(
                        user_message,
                        channel_id=MAIN_CHANNEL_ID,
                        source="tower",
                        external_dedupe_key=request.headers.get("X-Request-Id"),
                        display_text=message or "(image attached)",
                        overlay_context=overlay,
                        request_context=request_context,
                    )
                )
                return jsonify({"response": response})
            except Exception as e:
                log.exception("Tower chat error")
                return jsonify({"error": str(e)}), 500
            finally:
                loop.close()

    @app.route("/api/transcribe/credentials", methods=["POST"])
    def api_transcribe_credentials():
        """Issue authenticated users credentials scoped to live transcription."""
        try:
            response = jsonify(_browser_transcribe_credentials())
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 503
        except Exception:
            log.exception("Could not issue browser transcription credentials")
            return jsonify({"error": "Voice dictation is temporarily unavailable"}), 503
        response.headers["Cache-Control"] = "no-store"
        return response

    def _sse_from_events(events: "queue.Queue", *, on_disconnect=None) -> Response:
        """Drain a thread-safe queue into SSE frames until a None sentinel."""

        def generate():
            try:
                while True:
                    event = events.get()
                    if event is None:
                        break
                    yield f"data: {json.dumps(event)}\n\n"
            finally:
                if on_disconnect is not None:
                    on_disconnect()

        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.route("/api/chat/stream", methods=["POST"])
    def api_chat_stream():
        """Server-Sent Events stream of the agent's turn: thoughts, text
        deltas, and tool calls/results as they happen.

        Subscribes to the channel turn hub so reconnecting clients can attach
        via /api/chat/stream/attach. The agent loop bridges asyncio hub events
        onto a thread-safe queue that this Flask worker drains into SSE frames.
        """
        data = request.json or {}
        message = data.get("message", "").strip()
        image_blocks, img_err = _image_blocks_from_payload(data.get("images") or [])
        if img_err:
            return jsonify({"error": img_err}), 400
        user_message = _build_chat_message(message, image_blocks)
        if user_message is None:
            return jsonify({"error": "Empty message"}), 400

        from .ui_context import format_overlay_system_block

        context = data.get("context")
        overlay = format_overlay_system_block(context)
        request_id = request.headers.get("X-Request-Id")
        actor = getattr(getattr(g, "tower_auth", None), "username", None) or "tower-user"
        request_context = {
            "source": "tower", "actor_id": str(actor), "trusted": True,
            "trust_reason": "authenticated_tower",
        }

        loop = scheduler._loop if scheduler else None
        if not (loop and loop.is_running()):
            return jsonify({"error": "Agent event loop not available"}), 503

        events: "queue.Queue" = queue.Queue()
        channel = MAIN_CHANNEL_ID
        state = {"sub": None, "bridge": None}

        async def cleanup():
            bridge = state["bridge"]
            if bridge is not None and not bridge.done():
                bridge.cancel()
                try:
                    await bridge
                except asyncio.CancelledError:
                    pass
            sub = state["sub"]
            if sub is not None:
                agent.conversation_queue.unsubscribe_stream(channel, sub)
                state["sub"] = None

        async def run():
            try:
                sub = agent.conversation_queue.subscribe_stream(channel)
                state["sub"] = sub

                async def bridge():
                    while True:
                        event = await sub.get()
                        if event is None:
                            return
                        events.put(event)

                state["bridge"] = asyncio.create_task(bridge())
                item = await agent.enqueue(
                    user_message,
                    channel_id=channel,
                    source="tower",
                    external_dedupe_key=request_id,
                    display_text=message or "(image attached)",
                    overlay_context=overlay,
                    request_context=request_context,
                )
                final = await agent.await_enqueued(item["id"])
                bridge_task = state["bridge"]
                if bridge_task is not None:
                    try:
                        await bridge_task
                        events.put({"type": "done", "text": final})
                    except asyncio.CancelledError:
                        pass
                else:
                    events.put({"type": "done", "text": final})
            except Exception as e:
                log.exception("Tower stream error")
                events.put({"type": "error", "error": str(e)})
                if channel not in agent.conversation_queue._active_turns:
                    orphan = agent.conversation_queue._hubs.pop(channel, None)
                    if orphan is not None:
                        orphan.close()
            finally:
                await cleanup()
                events.put(None)

        asyncio.run_coroutine_threadsafe(run(), loop)

        def on_disconnect():
            asyncio.run_coroutine_threadsafe(cleanup(), loop)

        return _sse_from_events(events, on_disconnect=on_disconnect)

    @app.route("/api/chat/stream/attach", methods=["GET"])
    def api_chat_stream_attach():
        """Reattach SSE to an in-flight tower turn without enqueueing a message."""
        channel = request.args.get("channel", MAIN_CHANNEL_ID)
        loop = scheduler._loop if scheduler else None
        if not (loop and loop.is_running()):
            return jsonify({"error": "Agent event loop not available"}), 503

        events: "queue.Queue" = queue.Queue()
        state = {"sub": None}

        async def cleanup():
            sub = state["sub"]
            if sub is not None:
                agent.conversation_queue.unsubscribe_stream(channel, sub)
                state["sub"] = None

        async def subscribe():
            return agent.conversation_queue.subscribe_stream(channel, create=False)

        try:
            sub = asyncio.run_coroutine_threadsafe(subscribe(), loop).result(timeout=5)
        except Exception as e:
            log.exception("Tower stream attach subscribe failed")
            return jsonify({"attached": False, "error": str(e)}), 503
        if sub is None:
            return jsonify({"attached": False, "error": "No active stream"}), 404
        state["sub"] = sub

        async def run():
            try:
                while True:
                    event = await sub.get()
                    if event is None:
                        break
                    events.put(event)
            except Exception as e:
                log.exception("Tower stream attach error")
                events.put({"type": "error", "error": str(e)})
            finally:
                await cleanup()
                events.put(None)

        asyncio.run_coroutine_threadsafe(run(), loop)

        def on_disconnect():
            asyncio.run_coroutine_threadsafe(cleanup(), loop)

        return _sse_from_events(events, on_disconnect=on_disconnect)

    @app.route("/api/chat/stop", methods=["POST"])
    def api_chat_stop():
        """Stop the in-flight turn on a channel (default: main)."""
        channel = (request.json or {}).get("channel", MAIN_CHANNEL_ID)
        loop = scheduler._loop if scheduler else None
        if loop and loop.is_running():
            async def stop_on_agent_loop():
                return agent.request_stop(channel)
            stopped = asyncio.run_coroutine_threadsafe(
                stop_on_agent_loop(), loop,
            ).result(timeout=5)
        else:
            stopped = agent.request_stop(channel)
        status = agent.conversation_queue.status(channel)
        return jsonify({
            "stopped": stopped,
            **status,
            "channel": channel,
        })

    @app.route("/api/chat/status", methods=["GET"])
    def api_chat_status():
        channel = request.args.get("channel", MAIN_CHANNEL_ID)
        return jsonify(agent.conversation_queue.status(channel))

    def _queue_item_for_api(item: dict) -> dict:
        """Expose queue metadata without echoing binary/private payload fields."""
        return {
            key: item.get(key)
            for key in (
                "id", "channel", "sequence", "source", "display_text", "state",
                "revision", "created_at", "updated_at",
            )
        }

    @app.route("/api/chat/queue", methods=["GET"])
    def api_chat_queue():
        channel = request.args.get("channel", MAIN_CHANNEL_ID)
        return jsonify({
            **agent.conversation_queue.status(channel),
            "items": [
                _queue_item_for_api(item)
                for item in agent.conversation_queue.pending(channel)
            ],
        })

    @app.route("/api/chat/queue/<item_id>", methods=["PATCH"])
    def api_chat_queue_edit(item_id: str):
        data = request.json or {}
        text = data.get("display_text")
        revision = data.get("revision")
        if not isinstance(text, str) or not isinstance(revision, int):
            return jsonify({"error": "display_text and integer revision are required"}), 400
        if len(text) > 100_000:
            return jsonify({"error": "Message is too long"}), 400
        existing = agent.conversation_queue.store.get(item_id)
        if not existing or existing.get("source") != "tower":
            return jsonify({"error": "Queue item not found or not editable"}), 404
        payload = existing.get("payload")
        if isinstance(payload, str):
            if not text.strip():
                return jsonify({"error": "Message cannot be empty"}), 400
            payload = f"[Tower]: {text.strip()}"
        elif isinstance(payload, list):
            payload = list(payload)
            text_index = next((
                i for i, block in enumerate(payload)
                if isinstance(block, dict) and block.get("type") == "text"
            ), None)
            if text_index is None:
                return jsonify({"error": "Message payload is not safely editable"}), 400
            if not text.strip() and len(payload) == 1:
                return jsonify({"error": "Message cannot be empty"}), 400
            payload[text_index] = {
                **payload[text_index],
                "text": f"[Tower]: {text.strip() or '(image attached)'}",
            }
        else:
            return jsonify({"error": "Message payload is not safely editable"}), 400
        item = agent.conversation_queue.edit(
            item_id, revision, display_text=text.strip() or "(image attached)", payload=payload,
        )
        if item is None:
            return jsonify({"error": "Queue item changed or is no longer pending"}), 409
        return jsonify({"item": _queue_item_for_api(item)})

    @app.route("/api/chat/queue/<item_id>", methods=["DELETE"])
    def api_chat_queue_delete(item_id: str):
        data = request.json or {}
        revision = data.get("revision")
        if not isinstance(revision, int):
            return jsonify({"error": "integer revision is required"}), 400
        item = agent.conversation_queue.delete(item_id, revision)
        if item is None:
            return jsonify({"error": "Queue item changed or is no longer pending"}), 409
        return jsonify({"deleted": True, "item": _queue_item_for_api(item)})

    @app.route("/api/chat/queue/reorder", methods=["POST"])
    def api_chat_queue_reorder():
        data = request.json or {}
        channel = data.get("channel", MAIN_CHANNEL_ID)
        items = data.get("items")
        if not isinstance(items, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not isinstance(item.get("revision"), int)
            for item in items
        ):
            return jsonify({"error": "items must contain id and integer revision"}), 400
        result = agent.conversation_queue.reorder(channel, items)
        if result is None:
            return jsonify({"error": "Queue changed; refresh and try again"}), 409
        return jsonify({"items": [_queue_item_for_api(item) for item in result]})

    @app.route("/api/history", methods=["GET"])
    def api_history():
        channel = request.args.get("channel", MAIN_CHANNEL_ID)
        from .ui_context import serialize_chat_history
        if channel == MAIN_CHANNEL_ID:
            try:
                from .chats_board import active_main_history
                history, run_id = active_main_history()
                if history:
                    return jsonify({"history": history, "run_id": run_id})
            except Exception:
                log.debug("Mongo conversation history unavailable", exc_info=True)
        messages = agent.conversations.get(channel, [])
        return jsonify({"history": serialize_chat_history(messages)})

    @app.route("/api/chat/select", methods=["POST"])
    def api_chat_select():
        """Resume a main-channel conversation run as the live overlay/Chats tail."""
        data = request.json or {}
        run_id = (data.get("run_id") or "").strip()
        if not run_id:
            return jsonify({"error": "run_id is required"}), 400
        loop = scheduler._loop if scheduler else None
        if not (loop and loop.is_running()):
            return jsonify({"error": "Agent event loop not available"}), 503
        try:
            result = asyncio.run_coroutine_threadsafe(
                agent.switch_main_run(run_id), loop,
            ).result(timeout=60)
        except ValueError as e:
            return jsonify({"error": str(e)}), 404
        except RuntimeError as e:
            status = 409 if "busy" in str(e).lower() else 500
            return jsonify({"error": str(e)}), status
        except Exception as e:
            log.exception("Tower chat select error")
            return jsonify({"error": str(e)}), 500
        from .chats_board import history_for_run
        history, _protocol = history_for_run(run_id)
        status = agent.conversation_queue.status(MAIN_CHANNEL_ID)
        async def _stream_attachable():
            return agent.conversation_queue.has_stream(MAIN_CHANNEL_ID)

        try:
            stream_attachable = asyncio.run_coroutine_threadsafe(
                _stream_attachable(), loop,
            ).result(timeout=2)
        except Exception:
            stream_attachable = False
        return jsonify({
            **result,
            "history": history,
            "busy": bool(status.get("busy")),
            "stream_attachable": bool(stream_attachable),
        })

    @app.route("/api/clear", methods=["POST"])
    def api_clear():
        """Archive the channel's conversation to the palace, then clear it —
        matching Discord's `/new` / `!new` / `!clear` behaviour."""
        channel = (request.json or {}).get("channel", MAIN_CHANNEL_ID)
        loop = scheduler._loop if scheduler else None
        log.info(f"[Tower] /api/clear channel={channel} — new chat requested")
        if loop and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                agent.pop_and_archive_history(channel, reason="clear"), loop,
            )
            try:
                archived = future.result(timeout=120)
            except Exception as e:
                log.exception("Tower clear error")
                return jsonify({"error": str(e)}), 500
        else:
            # Fallback: agent loop unavailable — clear without archiving.
            log.warning(
                f"[Tower] /api/clear channel={channel} — no agent loop; "
                f"clearing without archive/mine/learn"
            )
            agent.clear_history(channel)
            archived = 0
        log.info(f"[Tower] /api/clear channel={channel} done archived={archived}")
        return jsonify({"status": "ok", "archived": archived})

    # ── Agent model API ──────────────────────────────────────────

    def _context_options(current) -> list[dict]:
        options = [
            {"value": tokens, "label": label}
            for tokens, label in tower_settings.CONTEXT_LABELS.items()
        ]
        try:
            tokens = int(current)
        except (TypeError, ValueError):
            return options
        if tokens not in tower_settings.CONTEXT_LABELS:
            options.append({"value": tokens, "label": f"{tokens:,}"})
        return options

    def _runtime_payload(channel: str | None = None) -> dict:
        model = (
            agent.model_for_channel(channel)
            if channel
            else getattr(agent, "model", None)
        )
        return {
            "model": model,
            "options": list(tower_settings.AGENT_MODEL_OPTIONS),
            "context": int(
                getattr(
                    agent,
                    "compact_threshold",
                    tower_settings.DEFAULT_COMPACT_THRESHOLD,
                )
            ),
            "context_options": _context_options(
                getattr(agent, "compact_threshold", tower_settings.DEFAULT_COMPACT_THRESHOLD)
            ),
            "effort": tower_settings.clamp_effort_for_model(
                model,
                getattr(
                    agent, "thinking_effort", tower_settings.DEFAULT_THINKING_EFFORT
                ),
            ),
            "effort_options": list(tower_settings.effort_options_for_model(model)),
            "effort_catalog": tower_settings.effort_catalog_for_model(model),
            "effort_by_model": {
                name: tower_settings.effort_catalog_for_model(name)
                for name in tower_settings.AGENT_MODEL_OPTIONS
            },
        }

    @app.route("/api/model", methods=["GET"])
    def api_model_get():
        channel = (request.args.get("channel") or "").strip()
        if channel:
            if channel not in tower_settings.CONFIGURABLE_CHANNELS:
                return jsonify({"error": "Invalid channel"}), 400
            return jsonify({
                "channel": channel,
                "model": agent.model_for_channel(channel),
                "options": list(tower_settings.AGENT_MODEL_OPTIONS),
                "persisted": tower_settings.is_configured(),
                **_runtime_payload(channel),
            })
        return jsonify({
            "model": agent.model,
            "models": {
                ch: agent.model_for_channel(ch)
                for ch in tower_settings.CONFIGURABLE_CHANNELS
            },
            "options": list(tower_settings.AGENT_MODEL_OPTIONS),
            "persisted": tower_settings.is_configured(),
            **_runtime_payload(),
        })

    @app.route("/api/model", methods=["POST"])
    def api_model_set():
        data = request.json or {}
        model = (data.get("model") or "").strip()
        channel = (data.get("channel") or MAIN_CHANNEL_ID).strip()
        if channel not in tower_settings.CONFIGURABLE_CHANNELS:
            return jsonify({"error": "Invalid channel"}), 400
        if model not in tower_settings.AGENT_MODEL_OPTIONS:
            return jsonify({"error": "Invalid model"}), 400
        try:
            agent.set_model(model, channel=channel)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({
            "channel": channel,
            "model": agent.model_for_channel(channel),
            "models": {
                ch: agent.model_for_channel(ch)
                for ch in tower_settings.CONFIGURABLE_CHANNELS
            },
            "persisted": tower_settings.is_configured(),
            **_runtime_payload(channel),
        })

    @app.route("/api/context", methods=["POST"])
    def api_context_set():
        data = request.json or {}
        tokens = tower_settings.normalize_compact_threshold(
            data.get("context", data.get("tokens"))
        )
        if tokens is None:
            return jsonify({"error": "Invalid context"}), 400
        try:
            agent.set_compact_threshold(tokens)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({
            "context": int(agent.compact_threshold),
            "persisted": tower_settings.is_configured(),
            **_runtime_payload(),
        })

    @app.route("/api/effort", methods=["POST"])
    def api_effort_set():
        data = request.json or {}
        effort = tower_settings.normalize_thinking_effort(data.get("effort"))
        model = getattr(agent, "model", None)
        allowed = tower_settings.effort_options_for_model(model)
        if effort is None or (allowed and effort not in allowed):
            return jsonify({"error": "Invalid effort"}), 400
        try:
            agent.set_thinking_effort(effort)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({
            "effort": agent.thinking_effort,
            "persisted": tower_settings.is_configured(),
            **_runtime_payload(),
        })

    @app.route("/api/provider-keys", methods=["GET"])
    def api_provider_keys_get():
        from harness import provider_credentials

        # Always returns per-provider status (BYOM and/or env). Never 503 for
        # missing KMS/Mongo — env-configured keys still show as configured.
        return jsonify({"providers": provider_credentials.list_summaries()})

    @app.route("/api/provider-keys/<provider>", methods=["PUT"])
    def api_provider_key_put(provider: str):
        from harness import provider_credentials

        try:
            result = provider_credentials.put(
                provider,
                (request.json or {}).get("api_key", ""),
            )
            agent._provider_cache.pop(provider, None)
            return jsonify(result), 201
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception:
            log.exception("Failed to save provider key")
            return jsonify({"error": "Could not save that provider key"}), 503

    @app.route("/api/provider-keys/<provider>", methods=["DELETE"])
    def api_provider_key_delete(provider: str):
        from harness import provider_credentials

        try:
            deleted = provider_credentials.delete(provider)
            agent._provider_cache.pop(provider, None)
            return jsonify({"provider": provider, "configured": False, "deleted": deleted})
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception:
            log.exception("Failed to delete provider key")
            return jsonify({"error": "Could not delete that provider key"}), 503

    # ── Headroom compression API ─────────────────────────────────

    @app.route("/api/headroom", methods=["GET"])
    def api_headroom_get():
        return jsonify({
            "enabled": bool(getattr(agent, "headroom_enabled", False)),
            "persisted": tower_settings.is_configured(),
        })

    @app.route("/api/headroom", methods=["POST"])
    def api_headroom_set():
        data = request.json or {}
        if "enabled" not in data:
            return jsonify({"error": "Missing 'enabled' field"}), 400
        try:
            agent.set_headroom_enabled(bool(data.get("enabled")))
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({
            "enabled": bool(agent.headroom_enabled),
            "persisted": tower_settings.is_configured(),
        })

    @app.route("/api/timezone", methods=["GET"])
    def api_timezone_get():
        return jsonify({
            "timezone": tower_settings.get_agent_timezone(),
            "persisted": tower_settings.is_configured(),
        })

    @app.route("/api/timezone", methods=["POST"])
    def api_timezone_set():
        data = request.json or {}
        tz_name = (data.get("timezone") or "").strip()
        if not tz_name:
            return jsonify({"error": "Missing 'timezone' field"}), 400
        # "local" means the browser's IANA zone — client must resolve it first.
        if tz_name == "local":
            return jsonify({
                "error": "Pass the browser IANA timezone (e.g. Asia/Kolkata), not 'local'",
            }), 400
        try:
            saved = tower_settings.set_agent_timezone(tz_name)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 503
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({
            "timezone": saved,
            "persisted": True,
        })

    @app.route("/api/recall-enabled", methods=["GET"])
    def api_recall_enabled_get():
        return jsonify({
            "enabled": bool(getattr(agent, "recall_enabled", True)),
            "persisted": tower_settings.is_configured(),
        })

    @app.route("/api/recall-enabled", methods=["POST"])
    def api_recall_enabled_set():
        data = request.json or {}
        if "enabled" not in data:
            return jsonify({"error": "Missing 'enabled' field"}), 400
        try:
            agent.set_recall_enabled(bool(data.get("enabled")))
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({
            "enabled": bool(agent.recall_enabled),
            "persisted": tower_settings.is_configured(),
        })

    @app.route("/api/recall-slm-model", methods=["GET"])
    def api_recall_slm_model_get():
        from harness.recall import get_recall_slm_status

        status = get_recall_slm_status()
        status["persisted"] = tower_settings.is_configured()
        return jsonify(status)

    @app.route("/api/recall-slm-model", methods=["POST"])
    def api_recall_slm_model_set():
        from harness.recall import set_recall_slm_model

        data = request.json or {}
        model = (data.get("model") or "").strip().lower()
        if not model:
            return jsonify({"error": "Missing 'model' field"}), 400
        if model not in tower_settings.RECALL_SLM_MODEL_OPTIONS:
            return jsonify({
                "error": (
                    f"Invalid model; expected one of "
                    f"{list(tower_settings.RECALL_SLM_MODEL_OPTIONS)}"
                ),
            }), 400
        try:
            status = set_recall_slm_model(model, preload=True)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 503
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        status["persisted"] = True
        if status.get("error") and not status.get("loaded"):
            return jsonify(status), 503
        return jsonify(status)

    @app.route("/api/recall-judge-model", methods=["GET"])
    def api_recall_judge_model_get():
        return jsonify({
            "model": tower_settings.get_recall_judge_model(),
            "options": list(tower_settings.AGENT_MODEL_OPTIONS),
            "default": tower_settings.DEFAULT_RECALL_JUDGE_MODEL,
            "persisted": tower_settings.is_configured(),
        })

    @app.route("/api/recall-judge-model", methods=["POST"])
    def api_recall_judge_model_set():
        data = request.json or {}
        model = (data.get("model") or "").strip()
        if not model:
            return jsonify({"error": "Missing 'model' field"}), 400
        try:
            saved = tower_settings.set_recall_judge_model(model)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 503
        from harness.recall_judge import invalidate_judge_model_cache

        invalidate_judge_model_cache()
        return jsonify({"model": saved, "persisted": True})

    def _run_async(coro):
        loop = None
        if scheduler and hasattr(scheduler, "_loop") and scheduler._loop.is_running():
            loop = scheduler._loop
        elif worker and hasattr(worker, "_loop") and worker._loop.is_running():
            loop = worker._loop

        if loop:
            return asyncio.run_coroutine_threadsafe(coro, loop).result()
        else:
            new_loop = asyncio.new_event_loop()
            try:
                return new_loop.run_until_complete(coro)
            finally:
                new_loop.close()

    @app.route("/api/recalls", methods=["GET"])
    def api_get_recalls():
        from harness.recall import fetch_all_recalls
        try:
            recalls = _run_async(fetch_all_recalls())
            return jsonify({"status": "ok", "recalls": recalls})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/recalls", methods=["POST"])
    def api_create_recall():
        data = request.json or {}
        instruction = data.get("instruction", "").strip()
        if not instruction:
            return jsonify({"error": "Instruction is required"}), 400
        if not (data.get("activation_condition") or "").strip():
            return jsonify({
                "error": "Activation condition is required — it is the only field "
                         "the Stage-2 judge reads."
            }), 400

        from harness.tools import _learn_recall
        try:
            result = _run_async(_learn_recall(
                instruction=instruction,
                positive_examples=data.get("positive_examples"),
                # Negatives are optional on create — treat [] as omitted.
                negative_examples=data.get("negative_examples") or None,
                lexical_cues=data.get("lexical_cues") or data.get("regex_tags"),
                positive_threshold=data.get("positive_threshold"),
                activation_condition=data.get("activation_condition"),
                exclusions=data.get("exclusions"),
            ))
            if isinstance(result, str) and result.startswith("[error]"):
                return jsonify({"error": result}), 400
            return jsonify({"status": "ok", "message": result})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/recalls/test", methods=["POST"])
    def api_test_recalls():
        """Run both stages so the page predicts real fires, not just proposals."""
        data = request.json or {}
        text = data.get("text", "")
        model = data.get("model", "fastembed")

        from harness.recall import (
            _stage2_mode,
            fetch_all_recalls,
            filter_matches_with_judge,
            filter_matches_with_slm,
            get_recall_slm_status,
            scan_text_for_recalls,
        )

        def _serializable(match: dict) -> dict:
            m = dict(match)
            if "_id" in m:
                m["_id"] = str(m["_id"])
            return m

        try:
            status = get_recall_slm_status()
            recalls = _run_async(fetch_all_recalls())
            proposed = scan_text_for_recalls(
                text,
                recalls,
                force_encoder_type=model,
                segments=[{"text": text, "source": "user"}],
            )
            if _stage2_mode() == "judge":
                verified, rejected = _run_async(filter_matches_with_judge(proposed))
            else:
                verified, rejected = filter_matches_with_slm(proposed)
            return jsonify({
                "status": "ok",
                "armed": bool(status.get("armed")),
                "stage2_mode": status.get("stage2_mode"),
                "judge_model": status.get("judge_model"),
                "matches": [_serializable(m) for m in verified + rejected],
            })
        except Exception as e:
            return jsonify({"status": "error", "error": str(e)}), 500
    @app.route("/api/recalls/<recall_id>/toggle", methods=["POST"])
    def api_toggle_recall(recall_id):
        from harness.db_ops import get_db
        from bson import ObjectId
        
        async def _toggle():
            db = get_db()
            if db is None:
                return jsonify({"error": "No database"}), 500
            try:
                coll = db["recalls"]
                doc = await coll.find_one({"_id": ObjectId(recall_id)})
                if not doc:
                    return jsonify({"error": "Recall not found"}), 404
                new_state = not doc.get("enabled", True)
                await coll.update_one({"_id": ObjectId(recall_id)}, {"$set": {"enabled": new_state}})
                from harness.recall import invalidate_semantic_router
                invalidate_semantic_router()
                return jsonify({"status": "ok", "enabled": new_state})
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        try:
            return _run_async(_toggle())
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/recalls/<recall_id>/examples", methods=["POST"])
    def api_update_recall_examples(recall_id):
        from harness.db_ops import get_db
        from bson import ObjectId
        import json
        from pathlib import Path
        
        data = request.json or {}
        from harness.recall import (
            normalize_lexical_cue,
            normalize_recall_thresholds,
            recall_positive_threshold,
        )
        positive = [x.strip() for x in data.get("positive_examples", []) if isinstance(x, str) and x.strip()]
        negative = [x.strip() for x in data.get("negative_examples", []) if isinstance(x, str) and x.strip()]
        lexical = []
        seen_lex = set()
        for x in data.get("lexical_cues", []) or []:
            if not isinstance(x, str):
                continue
            cue = normalize_lexical_cue(x)
            if cue and cue not in seen_lex:
                seen_lex.add(cue)
                lexical.append(cue)
        pos_thr = recall_positive_threshold({"positive_threshold": data.get("positive_threshold")})
        # If client omitted the threshold, keep the existing value (handled below).
        has_pos_thr = "positive_threshold" in data

        async def _update():
            config_path = Path("config/system_recalls.json")
            if config_path.exists():
                with open(config_path, "r", encoding="utf-8") as f:
                    sys_recalls = json.load(f)
                
                updated = False
                for r in sys_recalls:
                    if r.get("recall_id") == recall_id:
                        r["positive_examples"] = positive
                        r["negative_examples"] = negative
                        r["lexical_cues"] = lexical
                        if has_pos_thr:
                            r["positive_threshold"] = pos_thr
                        normalize_recall_thresholds(r)
                        updated = True
                        break
                
                if updated:
                    if not positive or not negative or not lexical:
                        return jsonify({
                            "error": "positive_examples, negative_examples, and lexical_cues must each be non-empty",
                        }), 400
                    with open(config_path, "w", encoding="utf-8") as f:
                        json.dump(sys_recalls, f, indent=4)
                    
                    from harness.recall import invalidate_semantic_router
                    invalidate_semantic_router()
                    return jsonify({"status": "ok", "source": "system"})
            
            db = get_db()
            if db is None:
                return jsonify({"error": "No database"}), 500

            if not positive or not negative or not lexical:
                return jsonify({
                    "error": "positive_examples, negative_examples, and lexical_cues must each be non-empty",
                }), 400
            
            try:
                coll = db["recalls"]
                doc = await coll.find_one({"_id": ObjectId(recall_id)})
                if not doc:
                    return jsonify({"error": "Recall not found"}), 404

                set_fields = {
                    "positive_examples": positive,
                    "negative_examples": negative,
                    "lexical_cues": lexical,
                }
                if has_pos_thr:
                    set_fields["positive_threshold"] = pos_thr

                await coll.update_one(
                    {"_id": ObjectId(recall_id)},
                    {
                        "$set": set_fields,
                        "$unset": {"threshold": "", "negative_threshold": ""},
                    },
                )
                
                from harness.recall import invalidate_semantic_router
                invalidate_semantic_router()
                return jsonify({"status": "ok", "source": "user"})
            except Exception as e:
                return jsonify({"error": str(e)}), 500
        try:
            return _run_async(_update())
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/recalls/<recall_id>", methods=["DELETE"])
    def api_delete_recall(recall_id):
        from harness.db_ops import get_db
        from bson import ObjectId
        
        async def _delete():
            db = get_db()
            if db is None:
                return jsonify({"error": "No database"}), 500
            try:
                coll = db["recalls"]
                res = await coll.delete_one({"_id": ObjectId(recall_id)})
                if res.deleted_count == 0:
                    return jsonify({"error": "Recall not found"}), 404
                from harness.recall import invalidate_semantic_router
                invalidate_semantic_router()
                return jsonify({"status": "ok"})
            except Exception as e:
                return jsonify({"error": str(e)}), 500
                
        try:
            return _run_async(_delete())
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── Experiential-state API ───────────────────────────────────

    def _experiential_payload():
        snapshot = agent.experience.snapshot()
        events = agent.experience.recent_events(50)
        latest_appraisal = next(
            (
                event.get("details", {})
                for event in reversed(events)
                if event.get("kind") == "episode_appraisal"
            ),
            None,
        )
        latest_self_report = next(
            (
                {
                    "details": event.get("details", {}),
                    "proposed_appraisal": event.get("proposed_appraisal", {}),
                    "timestamp": event.get("timestamp"),
                }
                for event in reversed(events)
                if event.get("kind") == "self_report"
            ),
            None,
        )
        return {
            "enabled": bool(agent.experience.influences_model),
            "mode": agent.experience.mode,
            "persisted": tower_settings.is_configured(),
            "state": snapshot,
            "latest_independent_appraisal": latest_appraisal,
            "latest_self_report": latest_self_report,
        }

    @app.route("/api/experiential-state", methods=["GET"])
    def api_experiential_state_get():
        try:
            return jsonify(_experiential_payload())
        except Exception:
            log.exception("Failed to read experiential state")
            return jsonify({"error": "Experiential state unavailable"}), 503

    @app.route("/api/experiential-state", methods=["POST"])
    def api_experiential_state_set():
        data = request.json or {}
        if "enabled" not in data:
            return jsonify({"error": "Missing 'enabled' field"}), 400
        try:
            agent.set_experiential_enabled(bool(data.get("enabled")))
            return jsonify(_experiential_payload())
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── Vision API ───────────────────────────────────────────────

    @app.route("/api/vision", methods=["GET"])
    def api_vision_get():
        """Return the active vision and the list of available ones."""
        config_dir = Path(agent.memory.config_dir)
        visions_dir = config_dir / "visions"
        active_file = config_dir / "active_vision.txt"

        available = []
        if visions_dir.is_dir():
            available = sorted(f.stem for f in visions_dir.glob("*.md"))

        active = None
        if active_file.exists():
            active = active_file.read_text(encoding="utf-8").strip() or None

        return jsonify({"active": active, "available": available})

    @app.route("/api/vision", methods=["POST"])
    def api_vision_set():
        """Set the active vision. Pass {"name": "<stem>"} or {"name": ""} to clear.

        The change takes effect on the NEXT API call — existing cached
        prefixes become stale and will be re-cached naturally.
        """
        data = request.json or {}
        name = (data.get("name") or "").strip()

        config_dir = Path(agent.memory.config_dir)
        visions_dir = config_dir / "visions"
        active_file = config_dir / "active_vision.txt"

        if name:
            vision_path = visions_dir / f"{name}.md"
            if not vision_path.exists():
                return jsonify({"error": f"Vision '{name}' not found"}), 404
            active_file.write_text(name, encoding="utf-8")
        else:
            if active_file.exists():
                active_file.unlink()

        return jsonify({"active": name or None})

    # ── Scheduler API ────────────────────────────────────────────

    @app.route("/api/scheduler", methods=["GET"])
    def api_scheduler_status():
        if not scheduler:
            return jsonify({"error": "Scheduler not available"}), 503
        return jsonify(scheduler.get_status())

    @app.route("/api/scheduler/heartbeat", methods=["POST"])
    def api_scheduler_heartbeat():
        if not scheduler:
            return jsonify({"error": "Scheduler not available"}), 503
        data = request.json or {}
        enabled = data.get("enabled")
        interval = data.get("interval")

        if enabled is None:
            return jsonify({"error": "Missing 'enabled' field"}), 400

        if interval is not None:
            interval = int(interval)

        # Accept a custom heartbeat prompt under either key (back-compat).
        prompt = data.get("prompt", data.get("heartbeat_prompt"))

        scheduler.set_heartbeat(
            enabled=bool(enabled), interval=interval, prompt=prompt,
        )
        return jsonify(scheduler.get_status())

    @app.route("/api/scheduler/wake", methods=["POST"])
    def api_scheduler_wake():
        """Arm (or disarm) a single restart-surviving one-shot wake.

        Body: {"prompt": "<self-prompt>"} to arm; {"prompt": ""} or
        {"disarm": true} to disarm. The wake fires exactly once on the next
        scheduler loop (or on the next process start if armed and then
        restarted), then clears itself.
        """
        if not scheduler:
            return jsonify({"error": "Scheduler not available"}), 503
        data = request.json or {}
        if data.get("disarm"):
            scheduler.arm_wake("")
        else:
            prompt = data.get("prompt", "")
            scheduler.arm_wake(prompt)
        return jsonify(scheduler.get_status())

    @app.route("/api/scheduler/routine-time", methods=["POST"])
    def api_scheduler_routine_time():
        """Set morning or goodnight fire time (agent timezone). Body: {routine, time: HH:MM}."""
        if not scheduler:
            return jsonify({"error": "Scheduler not available"}), 503
        data = request.json or {}
        routine = (data.get("routine") or "").strip().lower()
        hhmm = (data.get("time") or "").strip()
        if routine not in ("morning", "goodnight"):
            return jsonify({"error": "routine must be 'morning' or 'goodnight'"}), 400
        if not hhmm:
            return jsonify({"error": "Missing 'time' (HH:MM)"}), 400
        try:
            scheduler.set_routine_time(routine, hhmm)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify(scheduler.get_status())

    @app.route("/api/scheduler/morning", methods=["POST"])
    def api_scheduler_morning():
        """Manually run (or re-run) today's morning planning routine now."""
        if not scheduler:
            return jsonify({"error": "Scheduler not available"}), 503
        try:
            result = scheduler.trigger_morning()
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 503
        status = scheduler.get_status()
        status.update(result)
        if not result.get("started"):
            return jsonify(status), 409
        return jsonify(status)

    @app.route("/api/runtime/restart", methods=["POST"])
    def api_runtime_restart():
        """Arm a durable wake and request a provider-controlled process restart."""
        if not scheduler:
            return jsonify({"error": "Scheduler not available"}), 503
        if os.environ.get("GALADRIEL_SELF_RESTART_ENABLED", "").lower() not in {
            "1", "true", "yes",
        }:
            return jsonify({"error": "Runtime restart is not enabled"}), 403
        prompt = ((request.json or {}).get("prompt") or "").strip()
        if not prompt:
            return jsonify({"error": "A resume prompt is required"}), 400
        scheduler.arm_wake(prompt)
        threading.Timer(1.0, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
        return jsonify({"status": "restarting"}), 202

    # Apps — table / kanban / detail / approvals from workflow specs + MongoDB.
    from .apps import register_apps
    register_apps(app, scheduler)

    # Plan / progress save endpoints (editors live on new-chat landing).
    from .todo_board import register_todo_board
    register_todo_board(app)

    # Agent — autonomous channel prompts (worker, scheduler, completions).
    from .agent_board import register_agent_board
    register_agent_board(app, scheduler=scheduler, agent=agent, worker=worker)

    @app.route("/api/worker/idle-interval", methods=["GET", "POST"])
    def api_worker_idle_interval():
        """Get or set the worker idle-poll interval (minutes)."""
        if request.method == "GET":
            minutes = (
                worker.idle_interval_minutes()
                if worker is not None
                else tower_settings.get_worker_idle_minutes()
            )
            return jsonify({
                "minutes": minutes,
                "options": list(tower_settings.VALID_WORKER_IDLE_MINUTES),
                "worker_running": worker is not None,
                "persisted": tower_settings.is_configured(),
            })
        data = request.json or {}
        try:
            minutes = int(data.get("minutes"))
        except (TypeError, ValueError):
            return jsonify({"error": "Missing or invalid 'minutes'"}), 400
        if minutes not in tower_settings.VALID_WORKER_IDLE_MINUTES:
            return jsonify({"error": "Invalid idle interval"}), 400
        try:
            if worker is not None:
                worker.set_idle_interval_minutes(minutes)
            else:
                tower_settings.set_worker_idle_minutes(minutes)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 503
        return jsonify({
            "minutes": minutes,
            "options": list(tower_settings.VALID_WORKER_IDLE_MINUTES),
            "worker_running": worker is not None,
            "persisted": tower_settings.is_configured(),
        })

    # Chats — ChatGPT-like browser for chat + worker + loop ticks.
    from .worker_ticks_board import register_worker_ticks_board
    register_worker_ticks_board(app)
    from .chats_board import register_chats_board
    register_chats_board(app, agent=agent)

    # "Brain" — live agent configuration browser (config/jobs/state/sme).
    from .config_browser import register_config_browser
    register_config_browser(app, agent, scheduler=scheduler)

    # "Palace" — memory palace browser (wings/rooms/halls/drawers/KG/diary).
    from .palace_browser import register_palace_browser
    register_palace_browser(app)

    # Costs — LLM API spend by day/channel/model (harness/cost_tracker.py).
    from .cost_board import register_cost_board
    register_cost_board(app)

    app.config["GALADRIEL_READY"] = True
    return app

"""Tower UI — local web dashboard for the Galadriel agent."""

import os
import json
import queue
import base64
import binascii
import hmac
import asyncio
import logging
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, request, jsonify, Response
from harness.agent import MAIN_CHANNEL_ID, WORKER_CHANNEL_ID
from harness import tower_settings

log = logging.getLogger("galadriel.tower")

MAX_CHAT_IMAGES = 5
MAX_IMAGE_BYTES = 5 * 1024 * 1024


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


def create_tower(agent, scheduler=None) -> Flask:
    """Create the Flask Tower app wired to the agent and scheduler."""
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )
    app.secret_key = os.environ.get("TOWER_SECRET_KEY", "change-me")

    def _tower_auth_configured() -> bool:
        return bool(os.environ.get("TOWER_AUTH_TOKEN"))

    @app.before_request
    def _require_tower_auth():
        # ALB health checks stay public; every other Tower route, including
        # static assets and blueprint routes registered below, is authenticated.
        if request.path in {"/healthz", "/readyz"}:
            return None
        if os.environ.get("TOWER_AUTH_REQUIRED", "").lower() not in {"1", "true", "yes"}:
            return None
        token = os.environ.get("TOWER_AUTH_TOKEN", "")
        if not token:
            return jsonify({"error": "Tower authentication is misconfigured"}), 503
        authorization = request.headers.get("Authorization", "")
        supplied = authorization.removeprefix("Bearer ").strip()
        if authorization.startswith("Basic "):
            try:
                decoded = base64.b64decode(authorization[6:], validate=True).decode("utf-8")
                username, supplied = decoded.split(":", 1)
            except (ValueError, UnicodeDecodeError, binascii.Error):
                username, supplied = "", ""
            if username != os.environ.get("TOWER_AUTH_USERNAME", "clyra"):
                supplied = ""
        if not supplied or not hmac.compare_digest(supplied, token):
            return jsonify({"error": "Unauthorized"}), 401, {
                "WWW-Authenticate": 'Basic realm="Clyra Tower", Bearer',
            }
        return None

    @app.route("/healthz", methods=["GET"])
    def healthz():
        return jsonify({"status": "ok"}), 200

    @app.route("/readyz", methods=["GET"])
    def readyz():
        if not app.config.get("GALADRIEL_READY", False):
            return jsonify({"status": "starting"}), 503
        if (
            os.environ.get("TOWER_AUTH_REQUIRED", "").lower() in {"1", "true", "yes"}
            and not _tower_auth_configured()
        ):
            return jsonify({"status": "misconfigured"}), 503
        from harness.runtime_dependencies import readiness_error

        dependency_error = readiness_error()
        if dependency_error:
            return jsonify({"status": "unready", "error": dependency_error}), 503
        return jsonify({"status": "ready"}), 200

    @app.context_processor
    def _inject_page_context():
        return {"page_context": {}}

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
        channels = len(agent.conversations)
        total_msgs = sum(len(m) for m in agent.conversations.values())
        memory_files = sorted(Path(agent.memory.memory_dir).glob("*.md"), reverse=True)
        recent_memories = [f.stem for f in memory_files[:7]]
        sched_status = scheduler.get_status() if scheduler else None
        return render_template(
            "index.html",
            model=agent.model,
            worker_model=agent.model_for_channel(WORKER_CHANNEL_ID),
            model_options=tower_settings.AGENT_MODEL_OPTIONS,
            model_persisted=tower_settings.is_configured(),
            headroom_enabled=getattr(agent, "headroom_enabled", False),
            channels=channels,
            total_msgs=total_msgs,
            recent_memories=recent_memories,
            now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            scheduler=sched_status,
        )

    @app.route("/api/chat", methods=["POST"])
    def api_chat():
        data = request.json
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

        # Schedule the async agent call onto the main event loop (Discord's loop)
        # This avoids creating a new event loop and works with AsyncAnthropic
        if scheduler and scheduler._loop and scheduler._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                agent.respond(
                    user_message,
                    channel_id=MAIN_CHANNEL_ID,
                    overlay_context=overlay,
                    run_source="tower",
                    client_dedup_key=request.headers.get("X-Request-Id"),
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
                    agent.respond(
                        user_message,
                        channel_id=MAIN_CHANNEL_ID,
                        overlay_context=overlay,
                        run_source="tower",
                        client_dedup_key=request.headers.get("X-Request-Id"),
                    )
                )
                return jsonify({"response": response})
            except Exception as e:
                log.exception("Tower chat error")
                return jsonify({"error": str(e)}), 500
            finally:
                loop.close()

    @app.route("/api/chat/stream", methods=["POST"])
    def api_chat_stream():
        """Server-Sent Events stream of the agent's turn: thoughts, text
        deltas, and tool calls/results as they happen.

        The agent runs on the Discord asyncio loop; its async `emit` callback
        pushes events onto a thread-safe queue that this (Flask worker thread)
        generator drains into SSE frames.
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

        loop = scheduler._loop if scheduler else None
        if not (loop and loop.is_running()):
            return jsonify({"error": "Agent event loop not available"}), 503

        events: "queue.Queue" = queue.Queue()

        async def emit(event):
            events.put(event)

        async def run():
            try:
                final = await agent.respond(
                    user_message,
                    channel_id=MAIN_CHANNEL_ID,
                    emit=emit,
                    overlay_context=overlay,
                    run_source="tower",
                    client_dedup_key=request_id,
                )
                events.put({"type": "done", "text": final})
            except Exception as e:
                log.exception("Tower stream error")
                events.put({"type": "error", "error": str(e)})
            finally:
                events.put(None)  # sentinel: stream complete

        asyncio.run_coroutine_threadsafe(run(), loop)

        def generate():
            while True:
                event = events.get()
                if event is None:
                    break
                yield f"data: {json.dumps(event)}\n\n"

        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.route("/api/chat/stop", methods=["POST"])
    def api_chat_stop():
        """Stop the in-flight turn on a channel (default: main)."""
        channel = (request.json or {}).get("channel", MAIN_CHANNEL_ID)
        stopped = agent.request_stop(channel)
        return jsonify({
            "stopped": stopped,
            "busy": agent.is_channel_busy(channel),
            "channel": channel,
        })

    @app.route("/api/chat/status", methods=["GET"])
    def api_chat_status():
        channel = request.args.get("channel", MAIN_CHANNEL_ID)
        return jsonify({
            "busy": agent.is_channel_busy(channel),
            "channel": channel,
        })

    @app.route("/api/history", methods=["GET"])
    def api_history():
        channel = request.args.get("channel", MAIN_CHANNEL_ID)
        from .ui_context import serialize_chat_history
        if channel == MAIN_CHANNEL_ID:
            run = None
            try:
                from harness import conversation_run_store
                run = conversation_run_store.active_run(channel)
                if run:
                    events = conversation_run_store.events_for_run(
                        run["run_id"], visibility="user",
                    )
                    history = [
                        {
                            "role": event.get("role", "assistant"),
                            "text": event.get("content", ""),
                        }
                        for event in events
                        if isinstance(event.get("content"), str)
                    ]
                    return jsonify({"history": history, "run_id": run["run_id"]})
            except Exception:
                log.debug("Mongo conversation history unavailable", exc_info=True)
        messages = agent.conversations.get(channel, [])
        return jsonify({"history": serialize_chat_history(messages)})

    @app.route("/api/clear", methods=["POST"])
    def api_clear():
        """Archive the channel's conversation to the palace, then clear it —
        matching Discord's `/new` / `!new` / `!clear` behaviour."""
        channel = request.json.get("channel", MAIN_CHANNEL_ID)
        loop = scheduler._loop if scheduler else None
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
            agent.clear_history(channel)
            archived = 0
        return jsonify({"status": "ok", "archived": archived})

    @app.route("/api/memory", methods=["GET"])
    def api_memory():
        date = request.args.get("date")
        if date:
            path = Path(agent.memory.memory_dir) / f"{date}.md"
            if path.exists():
                return jsonify({"date": date, "content": path.read_text()})
            return jsonify({"error": "Not found"}), 404
        # List all memory files
        files = sorted(Path(agent.memory.memory_dir).glob("*.md"), reverse=True)
        return jsonify({"files": [f.stem for f in files]})

    # ── Agent model API ──────────────────────────────────────────

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
            })
        return jsonify({
            "model": agent.model,
            "models": {
                ch: agent.model_for_channel(ch)
                for ch in tower_settings.CONFIGURABLE_CHANNELS
            },
            "options": list(tower_settings.AGENT_MODEL_OPTIONS),
            "persisted": tower_settings.is_configured(),
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
        })

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

    # Generic workflow screens (table / kanban / detail / approvals),
    # auto-rendered from the workflow specs + live MongoDB.
    from .workflows import register_workflows
    register_workflows(app, scheduler)

    # Actions — today's planned actions + progress (editable), worker status.
    from .actions_board import register_actions_board
    register_actions_board(app)

    # Loops — autonomous channel prompts (worker, scheduler, completions).
    from .loops_board import register_loops_board
    register_loops_board(app, scheduler=scheduler, agent=agent)

    # Worker Runs — durable per-tick telemetry and transcript audit.
    from .worker_ticks_board import register_worker_ticks_board
    register_worker_ticks_board(app)

    from .runs_board import register_runs_board
    register_runs_board(app)

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

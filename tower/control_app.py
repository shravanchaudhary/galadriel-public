"""Minimal Replika control-plane web application."""

import os
from pathlib import Path

from flask import Flask, g, jsonify, redirect, render_template, request, url_for

from harness.runtime_dependencies import readiness_error
from . import auth as tower_auth
from .replika_control_plane import (
    register_replika_control_plane,
    replika_type_for_id,
)
from .slack_integration import register_slack_integration, start_slack_dispatcher


def create_control_app(config: dict | None = None) -> Flask:
    """Create the onboarding app without importing or initializing the agent."""
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )
    if config:
        app.config.update(config)
    tower_auth.configure_app_sessions(app)

    @app.before_request
    def _require_auth():
        allowed = (
            request.path in {
                "/",
                "/healthz",
                "/readyz",
                "/login",
                "/logout",
                "/replika",
                "/integrations",
                "/integrations/slack/install",
                "/integrations/slack/oauth/callback",
                "/slack/events",
                "/slack/commands",
                "/internal/slack/deliver",
            }
            or request.path.startswith("/static/")
            or request.path.startswith("/api/replika")
            or request.path.startswith("/api/integrations/slack")
            or request.path.startswith("/internal/replika/")
            or (
                request.path.startswith("/replika/")
                and (
                    request.path.endswith("/integrations")
                    or "/integrations/slack/" in request.path
                )
            )
        )
        if not allowed:
            return jsonify({"error": "Not found"}), 404
        if tower_auth.public_path(request.path) or not tower_auth.auth_required():
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

    @app.route("/healthz")
    def healthz():
        return jsonify({"status": "ok"})

    @app.route("/readyz")
    def readyz():
        if (
            not os.environ.get("REPLIKA_PROVISIONER_FUNCTION_ARN")
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
        dependency_error = readiness_error()
        if dependency_error:
            return jsonify({"status": "unready", "error": dependency_error}), 503
        return jsonify({"status": "ready"})

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
        return {"page_context": {}, "control_plane_only": True}

    register_replika_control_plane(app)
    app.config["REPLIKA_TYPE_RESOLVER"] = replika_type_for_id
    register_slack_integration(app)
    start_slack_dispatcher(app)

    @app.route("/")
    def index():
        return redirect(url_for("replika_control_plane.replika_setup"))

    return app

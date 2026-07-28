"""Tower UI — secure, read-only daily plan and progress artifacts.

Files live under `state/plan/` and `state/progress/` (one HTML file per day).
"""

import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Blueprint, Response, abort, redirect, request, url_for

CET = ZoneInfo("Europe/Stockholm")
PLAN_DIR = Path("state/plan")
PROGRESS_DIR = Path("state/progress")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _today() -> str:
    return datetime.now(CET).strftime("%Y-%m-%d")


def _valid_date(date: str) -> bool:
    if not _DATE_RE.match(date or ""):
        return False
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _artifact_path(kind: str, date: str) -> Path:
    if kind not in ("plan", "progress") or not _valid_date(date):
        abort(404)
    base = PLAN_DIR if kind == "plan" else PROGRESS_DIR
    return base / f"{date}.html"


def dashboard_todo_vars() -> dict:
    """Template vars for today's plan/progress artifacts on the new-chat landing."""
    date = _today()
    plan_path = _artifact_path("plan", date)
    progress_path = _artifact_path("progress", date)
    return {
        "todo_date": date,
        "plan_relpath": plan_path.as_posix(),
        "progress_relpath": progress_path.as_posix(),
        "plan_artifact_url": url_for(
            "todo_board.todo_artifact", kind="plan", date=date
        ),
        "progress_artifact_url": url_for(
            "todo_board.todo_artifact", kind="progress", date=date
        ),
        "plan_exists": plan_path.is_file(),
        "progress_exists": progress_path.is_file(),
    }


def register_todo_board(app):
    """Register read-only artifacts and legacy page redirects."""
    bp = Blueprint("todo_board", __name__)

    @bp.route("/todo")
    def todo_index():
        return redirect(url_for("index", **request.args))

    @bp.route("/todo/artifact/<kind>/<date>.html")
    def todo_artifact(kind: str, date: str):
        path = _artifact_path(kind, date)
        if not path.is_file():
            abort(404)
        response = Response(path.read_text(encoding="utf-8"), mimetype="text/html")
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'self'; "
            "sandbox allow-popups"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @bp.route("/actions")
    def actions_legacy_index():
        return redirect(url_for("index", **request.args))

    app.register_blueprint(bp)

    @app.route("/jobs")
    def jobs_legacy_redirect():
        return redirect(url_for("index", **request.args))

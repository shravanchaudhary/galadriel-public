"""Tower UI — daily plan / progress save endpoints (editors live on new-chat landing).

Files live under `state/plan/` and `state/progress/` (one markdown file per day).
"""

import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, redirect, request, url_for

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


def _read_or_empty(path: Path) -> str:
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return ""


def dashboard_todo_vars() -> dict:
    """Template vars for today's plan/progress editors on the new-chat landing."""
    date = _today()
    plan_path = PLAN_DIR / f"{date}.md"
    progress_path = PROGRESS_DIR / f"{date}.md"
    return {
        "todo_date": date,
        "plan_relpath": plan_path.as_posix(),
        "progress_relpath": progress_path.as_posix(),
        "plan_content": _read_or_empty(plan_path),
        "progress_content": _read_or_empty(progress_path),
    }


def register_todo_board(app):
    """Register plan/progress save + legacy redirects (no dedicated TODO page)."""
    bp = Blueprint("todo_board", __name__)

    @bp.route("/todo")
    def todo_index():
        return redirect(url_for("index", **request.args))

    @bp.route("/todo/save", methods=["POST"])
    def todo_save():
        kind = request.form.get("kind", "")
        date = request.form.get("date", "")
        content = request.form.get("content", "")
        if kind not in ("plan", "progress") or not _valid_date(date):
            abort(400)
        base = PLAN_DIR if kind == "plan" else PROGRESS_DIR
        path = base / f"{date}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return redirect(url_for("chats_board.chats_index", kind="chat", saved=kind))

    @bp.route("/actions")
    def actions_legacy_index():
        return redirect(url_for("index", **request.args))

    @bp.route("/actions/save", methods=["POST"])
    def actions_legacy_save():
        return todo_save()

    app.register_blueprint(bp)

    @app.route("/jobs")
    def jobs_legacy_redirect():
        return redirect(url_for("index", **request.args))

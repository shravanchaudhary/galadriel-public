"""Tower UI — Actions (daily plan, progress, worker status).

What to do next: planned actions for the day and what's been done so far.
Files live under `state/plan/` and `state/progress/` (one markdown file per day)
and remain editable here and via Brain → Board / State.
"""

import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, redirect, render_template, request, url_for

from . import ui_context as ui_ctx

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


def _list_dates(limit: int = 30) -> list[str]:
    dates: set[str] = set()
    for base in (PLAN_DIR, PROGRESS_DIR):
        if not base.is_dir():
            continue
        for p in base.glob("*.md"):
            if p.stem != "README" and _valid_date(p.stem):
                dates.add(p.stem)
    return sorted(dates, reverse=True)[:limit]


def _worker_status() -> str:
    path = Path("state/worker_control.md")
    if not path.is_file():
        return "unknown"
    first = path.read_text(encoding="utf-8").strip().splitlines()
    return (first[0].strip().lower() if first else "unknown") or "unknown"


def register_actions_board(app):
    """Register the Actions UI routes on the Flask app."""
    bp = Blueprint("actions_board", __name__)

    @bp.route("/actions")
    def actions_index():
        date = request.args.get("date") or _today()
        if not _valid_date(date):
            abort(400)
        plan_path = PLAN_DIR / f"{date}.md"
        progress_path = PROGRESS_DIR / f"{date}.md"
        is_today = date == _today()
        return render_template(
            "actions/index.html",
            date=date,
            is_today=is_today,
            today=_today(),
            plan_relpath=plan_path.as_posix(),
            progress_relpath=progress_path.as_posix(),
            plan_content=_read_or_empty(plan_path),
            progress_content=_read_or_empty(progress_path),
            worker_status=_worker_status(),
            dates=_list_dates(),
            saved=request.args.get("saved"),
            page_context=ui_ctx.actions(
                date, plan_path.as_posix(), progress_path.as_posix(), _worker_status(),
            ),
        )

    @bp.route("/actions/save", methods=["POST"])
    def actions_save():
        kind = request.form.get("kind", "")
        date = request.form.get("date", "")
        content = request.form.get("content", "")
        if kind not in ("plan", "progress") or not _valid_date(date):
            abort(400)
        base = PLAN_DIR if kind == "plan" else PROGRESS_DIR
        path = base / f"{date}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return redirect(url_for("actions_board.actions_index", date=date, saved=kind))

    app.register_blueprint(bp)

    @app.route("/jobs")
    def jobs_legacy_redirect():
        return redirect(url_for("actions_board.actions_index", **request.args))

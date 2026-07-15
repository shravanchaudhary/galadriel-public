"""Tower UI — shared user conversation runs."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, render_template, request

from harness import conversation_run_store
from . import ui_context as ui_ctx

CET = ZoneInfo("Europe/Stockholm")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _today() -> str:
    return datetime.now(CET).strftime("%Y-%m-%d")


def _valid_date(value: str) -> bool:
    if not _DATE_RE.match(value or ""):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _shift(date: str, amount: int) -> str:
    return (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=amount)).strftime("%Y-%m-%d")


def _as_messages(events: list[dict]) -> list[dict]:
    messages = []
    for event in events:
        if event.get("kind") != "protocol_message":
            continue
        message = {"role": event.get("role"), "content": event.get("content")}
        if event.get("thought"):
            message["_thought"] = event["thought"]
        messages.append(message)
    return messages


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p)
    if content is None:
        return ""
    return str(content)


def _direct_history(events: list[dict]) -> list[dict]:
    """User-visible turns in the same shape as the live chat console."""
    history: list[dict] = []
    for event in events:
        role = event.get("role")
        text = _content_text(event.get("content"))
        if role == "user":
            history.append({"role": "user", "text": ui_ctx.display_user_text(text)})
        elif role == "assistant":
            history.append({
                "role": "assistant",
                "blocks": [{"type": "text", "text": text}],
            })
    return history


def _jsonable(value):
    """Strip Mongo ObjectIds so templates can safely use |tojson."""
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key == "_id":
                continue
            out[key] = _jsonable(item)
        return out
    type_name = type(value).__name__
    if type_name == "ObjectId":
        return str(value)
    return value


def register_runs_board(app):
    bp = Blueprint("runs_board", __name__)

    @app.template_filter("run_time")
    def _run_time(value):
        if not value:
            return "—"
        if getattr(value, "tzinfo", None) is None:
            value = value.replace(tzinfo=ZoneInfo("UTC"))
        return value.astimezone(CET).strftime("%H:%M:%S")

    @app.template_filter("run_duration")
    def _run_duration(value):
        seconds = int((value or 0) / 1000)
        return f"{seconds // 60}m {seconds % 60}s" if seconds >= 60 else f"{seconds}s"

    @app.template_filter("usd")
    def _usd(value):
        return f"${float(value or 0):,.4f}"

    @bp.route("/runs")
    def runs_index():
        date = request.args.get("date") or _today()
        if not _valid_date(date):
            abort(400)
        rows = conversation_run_store.runs_for_day(date)
        return render_template(
            "runs/index.html",
            date=date,
            today=_today(),
            previous_date=_shift(date, -1),
            next_date=_shift(date, 1),
            runs=rows,
            active=conversation_run_store.active_run(),
            db_configured=conversation_run_store.is_configured(),
            page_context=ui_ctx.runs_index(date, [row.get("run_id", "") for row in rows]),
        )

    @bp.route("/runs/user/<run_id>")
    def run_detail(run_id: str):
        run = conversation_run_store.get_run(run_id)
        if run is None:
            abort(404)
        events = conversation_run_store.events_for_run(run_id)
        direct = conversation_run_store.events_for_run(run_id, visibility="user")
        return render_template(
            "runs/detail.html",
            run=_jsonable(run),
            events=_jsonable(events),
            direct=_direct_history(direct),
            history=ui_ctx.serialize_chat_history(_as_messages(events)),
            system_versions=_jsonable(run.get("system_prompt_versions") or []),
            checkpoints=_jsonable(conversation_run_store.checkpoints_for_run(run_id)),
            calls=_jsonable(conversation_run_store.calls_for_run(run_id)),
            page_context=ui_ctx.run_detail(run_id),
        )

    app.register_blueprint(bp)

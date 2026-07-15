"""Tower UI — durable worker-turn audit history."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, render_template, request

from harness import worker_tick_store
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


def _day_link(date: str, offset: int) -> str:
    return (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=offset)).strftime("%Y-%m-%d")


def _daily_totals(ticks: list[dict]) -> dict:
    tokens = sum(int(t.get("token_total", 0) or 0) for t in ticks)
    cost = sum(float(t.get("cost_total", 0) or 0) for t in ticks)
    calls = sum(int(t.get("llm_call_count", 0) or 0) for t in ticks)
    duration = sum(int(t.get("duration_ms", 0) or 0) for t in ticks)
    return {"ticks": len(ticks), "tokens": tokens, "cost": cost, "calls": calls, "duration_ms": duration}


def _events_to_messages(events: list[dict]) -> list[dict]:
    messages = []
    for event in events:
        message = {"role": event.get("role", "unknown"), "content": event.get("content")}
        if event.get("thought"):
            message["_thought"] = event["thought"]
        messages.append(message)
    return messages


def register_worker_ticks_board(app):
    """Register per-day worker tick list and detailed audit views."""
    bp = Blueprint("worker_ticks_board", __name__)

    @app.template_filter("duration")
    def _duration(value):
        ms = int(value or 0)
        if ms < 1000:
            return f"{ms} ms"
        seconds = ms // 1000
        return f"{seconds // 60}m {seconds % 60}s" if seconds >= 60 else f"{seconds}s"

    @app.template_filter("utc_cet_time")
    def _utc_cet_time(value):
        if not value:
            return "—"
        if getattr(value, "tzinfo", None) is None:
            value = value.replace(tzinfo=ZoneInfo("UTC"))
        return value.astimezone(CET).strftime("%H:%M:%S")

    @app.template_filter("usd")
    def _usd(value):
        try:
            return f"${float(value):,.4f}"
        except (TypeError, ValueError):
            return "$0.0000"

    @bp.route("/worker-runs")
    def worker_runs_index():
        date = request.args.get("date") or _today()
        if not _valid_date(date):
            abort(400)
        ticks = worker_tick_store.ticks_for_day(date)
        return render_template(
            "worker_ticks/index.html",
            date=date,
            today=_today(),
            previous_date=_day_link(date, -1),
            next_date=_day_link(date, 1),
            ticks=ticks,
            totals=_daily_totals(ticks),
            db_configured=worker_tick_store.is_configured(),
            page_context=ui_ctx.worker_ticks_index(date, [t.get("tick_id", "") for t in ticks]),
        )

    @bp.route("/worker-runs/<tick_id>")
    def worker_run_detail(tick_id: str):
        tick = worker_tick_store.get_tick(tick_id)
        if tick is None:
            abort(404)
        events = worker_tick_store.events_for_tick(tick_id)
        calls = worker_tick_store.calls_for_tick(tick_id)
        messages = _events_to_messages(events)
        return render_template(
            "worker_ticks/detail.html",
            tick=tick,
            events=events,
            calls=calls,
            history=ui_ctx.serialize_chat_history(messages),
            page_context=ui_ctx.worker_tick_detail(tick_id, tick.get("day_cet", "")),
        )

    app.register_blueprint(bp)

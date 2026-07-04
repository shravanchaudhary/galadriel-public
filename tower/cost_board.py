"""Tower UI — LLM cost dashboard.

Reads the `llm_calls` cost ledger (`harness/cost_tracker.py`) and renders
cumulative spend by day, channel, and model, so token cost is visible without
tailing logs or querying Mongo by hand.
"""

from datetime import datetime, timedelta, timezone

from flask import Blueprint, render_template, request

from harness import cost_tracker
from . import ui_context as ui_ctx

_RANGES = ("today", "7d", "30d", "all")


def _since(range_key: str) -> datetime | None:
    now = datetime.now(timezone.utc)
    if range_key == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if range_key == "7d":
        return now - timedelta(days=7)
    if range_key == "30d":
        return now - timedelta(days=30)
    return None  # "all"


def register_cost_board(app):
    """Register the Costs UI routes on the Flask app."""
    bp = Blueprint("cost_board", __name__)

    @app.template_filter("usd")
    def _usd(value):
        try:
            return f"${float(value):,.4f}"
        except (TypeError, ValueError):
            return "$0.0000"

    @bp.route("/costs")
    def costs_index():
        range_key = request.args.get("range", "30d")
        if range_key not in _RANGES:
            range_key = "30d"
        since = _since(range_key)

        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = today_start.replace(day=1)

        return render_template(
            "costs/index.html",
            range_key=range_key,
            db_configured=cost_tracker.is_configured(),
            cost_today=cost_tracker.total_cost(today_start),
            cost_month=cost_tracker.total_cost(month_start),
            cost_all_time=cost_tracker.total_cost(None),
            daily_rows=cost_tracker.daily_totals(since=since),
            channel_rows=cost_tracker.channel_totals(since=since),
            model_rows=cost_tracker.model_totals(since=since),
            page_context=ui_ctx.costs_index(range_key),
        )

    app.register_blueprint(bp)

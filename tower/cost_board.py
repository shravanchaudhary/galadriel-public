"""Tower UI — LLM cost dashboard.

Reads the `llm_calls` cost ledger (`harness/cost_tracker.py`) and renders
cumulative spend by day, channel, and model, so token cost is visible without
tailing logs or querying Mongo by hand.
"""

from datetime import datetime, timedelta, timezone

from flask import Blueprint, render_template, request

from harness import cost_tracker, tower_settings
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


def _all_models() -> list[str]:
    return cost_tracker.distinct_models(tower_settings.AGENT_MODEL_OPTIONS)


def _selected_models(all_models: list[str]) -> list[str]:
    raw = request.args.get("models", "").strip()
    if not raw:
        return list(all_models)
    selected = [m.strip() for m in raw.split(",") if m.strip()]
    return [m for m in all_models if m in selected]


def _costs_query_suffix(selected_models: list[str], all_models: list[str]) -> str:
    if len(selected_models) == len(all_models):
        return ""
    return "&models=" + ",".join(selected_models)


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

        all_models = _all_models()
        selected_models = _selected_models(all_models)
        model_filter = selected_models if len(selected_models) < len(all_models) else None

        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = today_start.replace(day=1)

        query_suffix = _costs_query_suffix(selected_models, all_models)

        return render_template(
            "costs/index.html",
            range_key=range_key,
            query_suffix=query_suffix,
            db_configured=cost_tracker.is_configured(),
            all_models=all_models,
            selected_models=selected_models,
            cost_today=cost_tracker.total_cost(today_start, models=model_filter),
            cost_month=cost_tracker.total_cost(month_start, models=model_filter),
            cost_all_time=cost_tracker.total_cost(None, models=model_filter),
            daily_rows=cost_tracker.daily_totals(since=since, models=model_filter),
            channel_rows=cost_tracker.channel_totals(since=since, models=model_filter),
            model_rows=cost_tracker.model_totals(
                since=since,
                models=model_filter,
                include_models=all_models,
            ),
            page_context=ui_ctx.costs_index(range_key),
        )

    app.register_blueprint(bp)

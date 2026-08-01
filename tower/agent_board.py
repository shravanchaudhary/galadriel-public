"""Tower UI — Agent page (autonomous channel prompts).

Shows every self-initiated prompt (worker, scheduler routines, completion
watcher) with live status. Dynamic prompts (heartbeat custom text, one-shot
wake) can be edited here; fixed templates live in harness/loop_prompts.py.
"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, redirect, render_template, request, url_for

from . import ui_context as ui_ctx
from harness import tower_settings
from harness.loop_prompts import (
    DEFAULT_HEARTBEAT_PROMPT,
    POST_RECOVERY_ADVISORY,
    PROCESS_COMPLETE_EXAMPLE,
    WORKER_CLOCK_SUFFIX,
    WORKER_PROMPT,
    catchup_prompt,
    goodnight_prompt,
    morning_prompt,
    reflection_prompt,
)

CET = ZoneInfo("Europe/Stockholm")

# Loops that own a real channel_id and can pick their own model.
_MODEL_SELECTABLE = frozenset({
    "worker", "heartbeat", "wake", "morning", "ambient", "goodnight", "completions",
})


def _today() -> str:
    return datetime.now(CET).strftime("%Y-%m-%d")


def _build_agent_items(scheduler, agent, today: str, worker=None) -> list[dict]:
    """Catalog of autonomous agent loops for the UI."""
    sched = scheduler.get_status() if scheduler else {}
    heartbeat_active = sched.get("heartbeat_prompt") or DEFAULT_HEARTBEAT_PROMPT
    wake_text = getattr(scheduler, "pending_wake", None) if scheduler else None

    worker_msgs = len(agent.conversations.get("worker", [])) if agent else 0
    worker_idle_min = (
        worker.idle_interval_minutes()
        if worker is not None
        else tower_settings.get_worker_idle_minutes()
    )
    reflection_off = os.environ.get("GALADRIEL_REFLECTION", "1").strip().lower() in {
        "0", "false", "no", "off",
    }
    morning_hhmm = sched.get("morning_hhmm") or "09:10"
    goodnight_hhmm = sched.get("goodnight_hhmm") or "21:00"

    loops = [
        {
            "id": "worker",
            "name": "Background worker",
            "channel": "worker",
            "schedule": (
                f"Every {worker_idle_min} min when idle (GALADRIEL_WORKER=1); "
                "bursts every 30s if work remains"
            ),
            "status": f"{worker_msgs} msgs in buffer",
            "source": "harness/loop_prompts.py",
            "editable": False,
            "prompt": WORKER_PROMPT + WORKER_CLOCK_SUFFIX,
            "note": "Each tick resets the worker channel, then appends a live [WORKER:CLOCK] block with today's file paths.",
            "idle_interval_controls": True,
            "idle_interval_minutes": worker_idle_min,
            "valid_idle_intervals": list(tower_settings.VALID_WORKER_IDLE_MINUTES),
        },
        {
            "id": "heartbeat",
            "name": "Heartbeat",
            "channel": "heartbeat",
            "schedule": f"Every {sched.get('heartbeat_interval', 10)} min when enabled",
            "status": "ON" if sched.get("heartbeat_enabled") else "OFF",
            "source": "harness/loop_prompts.py (+ optional custom override in scheduler state)",
            "editable": True,
            "editable_kind": "heartbeat",
            "prompt": heartbeat_active,
            "default_prompt": DEFAULT_HEARTBEAT_PROMPT,
            "note": "Custom prompt overrides the default while heartbeat is armed. Disables at goodnight (REST).",
            "heartbeat_controls": True,
            "heartbeat_enabled": bool(sched.get("heartbeat_enabled")),
            "heartbeat_interval": sched.get("heartbeat_interval", 10),
            "valid_intervals": sched.get("valid_intervals") or [5, 10, 20, 30],
        },
        {
            "id": "wake",
            "name": "One-shot wake",
            "channel": "wake",
            "schedule": "Once on next boot / scheduler start (survives restart until delivered)",
            "status": "ARMED" if wake_text else "disarmed",
            "source": "config/scheduler_state.json (pending_wake)",
            "editable": True,
            "editable_kind": "wake",
            "prompt": wake_text or "(not armed — set a prompt below to resume the agent after a self-restart)",
            "note": "Unlike heartbeat, fires exactly once then clears. Used for 'resume me after I restart myself'.",
        },
        {
            "id": "morning",
            "name": "Morning routine",
            "channel": "morning",
            "schedule": sched.get("morning_time", f"{morning_hhmm} CET (workdays)"),
            "status": (
                "running"
                if sched.get("morning_manual_running")
                else ("workday" if sched.get("is_workday") else "weekend skip")
            ),
            "source": "harness/loop_prompts.py → morning_prompt()",
            "editable": False,
            "prompt": morning_prompt(today),
            "note": "Plans the day: writes state/plan/<today>.html, pauses worker during planning.",
            "time_editable": True,
            "time_hhmm": morning_hhmm,
            "time_label": "Daily time (CET, workdays)",
            "manual_trigger": True,
            "manual_trigger_label": "Run morning plan now",
            "manual_trigger_running": bool(sched.get("morning_manual_running")),
        },
        {
            "id": "catchup",
            "name": "Downtime catch-up",
            "channel": "morning",
            "schedule": f"On boot if today's {morning_hhmm} morning slot was missed",
            "status": "conditional",
            "source": "harness/loop_prompts.py → catchup_prompt()",
            "editable": False,
            "prompt": catchup_prompt(today),
            "note": "Same channel as morning; prepends reconciliation then runs morning planning. Uses the morning model.",
        },
        {
            "id": "ambient",
            "name": "Ambient",
            "channel": "reflection",
            "schedule": sched.get("reflection_times", "11/14/17/20 CET workdays"),
            "status": "DISABLED" if reflection_off else "enabled",
            "source": "harness/loop_prompts.py → reflection_prompt()",
            "editable": False,
            "prompt": reflection_prompt(today),
            "note": "Files knowledge entries + palace room=knowledge, audits worker, appends steering.md, posts status summary.",
        },
        {
            "id": "goodnight",
            "name": "Goodnight",
            "channel": "goodnight",
            "schedule": sched.get("goodnight_time", f"{goodnight_hhmm} CET (daily)"),
            "status": "always on",
            "source": "harness/loop_prompts.py → goodnight_prompt()",
            "editable": False,
            "prompt": goodnight_prompt(today),
            "note": "Reconciles the day, files daily-recap to palace, disables heartbeat (REST).",
            "time_editable": True,
            "time_hhmm": goodnight_hhmm,
            "time_label": "Daily time (CET)",
        },
        {
            "id": "completions",
            "name": "Process complete",
            "channel": "completions",
            "schedule": "Event-driven (detached shell writes /tmp/galadriel-jobs/*.done)",
            "status": "watcher active when enabled",
            "source": "harness/loop_prompts.py → process_complete_prompt()",
            "editable": False,
            "prompt": PROCESS_COMPLETE_EXAMPLE,
            "note": "Template — actual prompt is built from the JSON marker each process writes.",
        },
        {
            "id": "post_recovery",
            "name": "Post-recovery advisory",
            "channel": "(injected into any channel after max_tokens recovery)",
            "schedule": "Conditional — only after a compaction/recovery hard-reset",
            "status": "conditional",
            "source": "harness/loop_prompts.py",
            "editable": False,
            "prompt": POST_RECOVERY_ADVISORY,
            "note": "Not a scheduled loop; injected as an extra system block so the model knows to palace_search.",
        },
    ]

    for item in loops:
        channel = item["channel"]
        selectable = item["id"] in _MODEL_SELECTABLE and channel in tower_settings.CONFIGURABLE_CHANNELS
        item["model_selectable"] = selectable
        if selectable and agent:
            item["model"] = agent.model_for_channel(channel)
        elif selectable:
            item["model"] = None

    return loops


def register_agent_board(app, scheduler=None, agent=None, worker=None):
    """Register the Agent UI routes on the Flask app."""
    bp = Blueprint("agent_board", __name__)

    @bp.route("/agent")
    def agent_index():
        today = _today()
        loops = _build_agent_items(scheduler, agent, today, worker=worker)
        sched = scheduler.get_status() if scheduler else None
        return render_template(
            "agent/index.html",
            loops=loops,
            today=today,
            scheduler=sched,
            model_options=list(tower_settings.AGENT_MODEL_OPTIONS),
            model_persisted=tower_settings.is_configured(),
            saved=request.args.get("saved"),
            page_context=ui_ctx.agent_index(),
        )

    @bp.route("/loops")
    def loops_legacy_redirect():
        return redirect(url_for("agent_board.agent_index", **request.args))

    @bp.route("/agent/heartbeat", methods=["POST"])
    def agent_heartbeat_save():
        if not scheduler:
            abort(503)
        prompt = request.form.get("prompt", "").strip()
        use_default = request.form.get("use_default") == "1"
        scheduler.set_heartbeat(
            enabled=scheduler.heartbeat_enabled,
            prompt=None if use_default or not prompt else prompt,
        )
        return redirect(url_for("agent_board.agent_index", saved="heartbeat"))

    @bp.route("/agent/wake", methods=["POST"])
    def agent_wake_save():
        if not scheduler:
            abort(503)
        if request.form.get("disarm") == "1":
            scheduler.arm_wake("")
        else:
            prompt = request.form.get("prompt", "").strip()
            if not prompt:
                abort(400)
            scheduler.arm_wake(prompt)
        return redirect(url_for("agent_board.agent_index", saved="wake"))

    
    @bp.route("/loops/heartbeat", methods=["POST"])
    def loops_heartbeat_legacy():
        return agent_heartbeat_save()

    @bp.route("/loops/wake", methods=["POST"])
    def loops_wake_legacy():
        return agent_wake_save()

    app.register_blueprint(bp)

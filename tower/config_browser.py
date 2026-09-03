"""Tower UI — "Brain": live agent configuration browser.

Read/edit surface for every *.md file the agent actually consults:

  - selected config/*.md — explicit stable prompt allowlist
  - jobs/*.md     — playbooks, read on demand by the worker/scheduler
  - knowledge/*.md — indexed procedures and reference, read on demand
  - state/*.md    — board files (plan, progress, steering, backlog, ...)
  - sme/*.md      — curated subject-matter reference files, read on demand

Nothing here is cached: every view re-reads from disk, and `MemoryManager`
does the same on every agent turn (see harness/memory.py), so what you see
here is exactly what the agent sees on its next call — no restart needed.

Editing writes directly to tenant storage and is visible to the agent on its
next turn. Source control is intentionally outside the Replika runtime.

Files are only reachable through a server-side enumeration of the configured
directories above (`_all_editable()`), never through a raw filesystem path
taken from the request — that enumeration IS the whitelist.
"""

from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, abort, redirect, render_template, request, url_for, jsonify

from harness import tower_settings as _tower_settings
from harness.memory import STABLE_FILES
from harness.loop_prompts import (
    DEFAULT_HEARTBEAT_PROMPT,
    PROCESS_COMPLETE_EXAMPLE,
    WORKER_CLOCK_SUFFIX,
    WORKER_PROMPT,
    goodnight_prompt,
    morning_prompt,
    reflection_prompt,
)
from harness.personal_tools import personal_tools_root

from . import ui_context as ui_ctx

CONFIG_DIR = Path("config")
JOBS_DIR = Path("jobs")
STATE_DIR = Path("state")
SME_DIR = Path("sme")
KNOWLEDGE_DIR = Path("knowledge")

PROMPT_CHANNELS = (
    ("main", "Main conversation"),
    ("worker", "Background worker"),
    ("heartbeat", "Heartbeat"),
    ("wake", "One-shot wake"),
    ("morning", "Morning routine"),
    ("reflection", "Ambient reflection"),
    ("goodnight", "Goodnight routine"),
    ("completions", "Process completion"),
)


def _identity_files() -> list[Path]:
    """Explicit stable files in MemoryManager assembly order."""
    if not CONFIG_DIR.is_dir():
        return []
    return [CONFIG_DIR / f for f in STABLE_FILES if (CONFIG_DIR / f).is_file()]


def _job_files() -> list[Path]:
    return sorted(JOBS_DIR.glob("*.md")) if JOBS_DIR.is_dir() else []


def _state_files() -> list[Path]:
    return sorted(STATE_DIR.rglob("*.md")) if STATE_DIR.is_dir() else []


def _sme_files() -> list[Path]:
    return sorted(SME_DIR.rglob("*.md")) if SME_DIR.is_dir() else []


def _knowledge_files() -> list[Path]:
    return sorted(KNOWLEDGE_DIR.rglob("*.md")) if KNOWLEDGE_DIR.is_dir() else []


def _personal_tools_files() -> list[Path]:
    p = personal_tools_root()
    return sorted(p.glob("*.py")) if p.is_dir() else []


def _all_editable() -> dict[str, Path]:
    """relpath (posix string) -> Path for every file this UI may view/edit.
    Recomputed per request; a path is only ever opened if it appears here."""
    files = (
        _identity_files()
        + _job_files()
        + _knowledge_files()
        + _state_files()
        + _sme_files()
        + _personal_tools_files()
    )
    return {p.as_posix(): p for p in files}


def _file_meta(p: Path) -> dict:
    stat = p.stat()
    return {"path": p.as_posix(), "name": p.name, "size": stat.st_size, "mtime": stat.st_mtime}


# key -> (label, note, base dir, whitelist function). Order here is the
# order the 4 category tiles are shown in on /config.
CATEGORIES = {
    "identity": ("Stable Core", "explicitly allowlisted in the cached prompt", CONFIG_DIR, _identity_files),
    "jobs": ("Playbooks", "read on demand by the worker/scheduler", JOBS_DIR, _job_files),
    "knowledge": ("Knowledge", "indexed procedures and reference, read on demand", KNOWLEDGE_DIR, _knowledge_files),
    "state": ("Board / State", "read on demand", STATE_DIR, _state_files),
    "sme": ("SME Knowledge", "curated reference files, read on demand — durable facts go to the palace via learn", SME_DIR, _sme_files),
    "tools": ("Personal Tools", "reusable tools created by the agent", personal_tools_root(), _personal_tools_files),
}


def _dir_contents(base: Path, subpath: str, files: list[Path]) -> dict:
    """Immediate sub-folders + files one level under `base/subpath`, computed
    from `files` (a category's already-whitelisted file list) rather than a
    raw filesystem listing — so browsing can never surface anything outside
    the whitelist. This is what makes Explorer-style click-to-descend
    navigation possible without ever rendering more than one level at once."""
    current = base / subpath if subpath else base
    folder_names: set[str] = set()
    level_files: list[Path] = []
    for f in files:
        try:
            rel = f.relative_to(current)
        except ValueError:
            continue
        if len(rel.parts) == 1:
            level_files.append(f)
        else:
            folder_names.add(rel.parts[0])
    return {"folders": sorted(folder_names), "files": level_files}


def _safe_filename(name: str) -> bool:
    """True if `name` is a single-segment *.md or *.py filename (no path components)."""
    name = (name or "").strip()
    return bool(name) and (name.endswith(".md") or name.endswith(".py")) and "/" not in name and "\\" not in name and ".." not in name


def _resolve_create_path(cat: str, subpath: str, filename: str) -> Path | None:
    """Return the absolute path for a new file, or None if invalid/outside base."""
    entry = CATEGORIES.get(cat)
    if entry is None or not _safe_filename(filename):
        return None
    _label, _note, base, _files_fn = entry
    base = base.resolve()
    target = (base / subpath.strip("/") / filename.strip()).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    return target


def _browse_url_for_relpath(relpath: str) -> str:
    """Best-effort browse URL to return to after deleting a config file."""
    p = Path(relpath)
    for key, (_label, _note, base, files_fn) in CATEGORIES.items():
        try:
            rel = p.relative_to(base)
        except ValueError:
            continue
        subpath = "" if len(rel.parts) <= 1 else "/".join(rel.parts[:-1])
        return f"/config/browse?cat={key}" + (f"&path={subpath}" if subpath else "")
    return "/config"


def _preview_trigger(channel: str, scheduler) -> tuple[str, str]:
    """Return the next user-message shape for a channel's API request."""
    today = _tower_settings.agent_today()

    if channel == "main":
        return (
            "(The next message received from Discord, Slack, or Tower.)",
            "The live conversation buffer is appended before this message.",
        )
    if channel == "worker":
        return (
            f"{WORKER_PROMPT}\n\n{WORKER_CLOCK_SUFFIX}",
            "The clock placeholders are filled at tick time; the worker clears its "
            "conversation buffer before every tick.",
        )
    if channel == "heartbeat":
        custom = getattr(scheduler, "heartbeat_prompt", None) if scheduler else None
        return (
            custom or DEFAULT_HEARTBEAT_PROMPT,
            "Shows the active custom prompt when configured; otherwise the built-in default.",
        )
    if channel == "wake":
        pending = getattr(scheduler, "pending_wake", None) if scheduler else None
        return (
            pending or "(No one-shot wake is armed.)",
            "This message is sent once on the next scheduler start, then cleared.",
        )
    if channel == "morning":
        return (
            morning_prompt(today),
            "The missed-morning catch-up uses this same channel with an additional "
            "reconciliation prefix.",
        )
    if channel == "reflection":
        return reflection_prompt(today), "The date-specific paths are generated for today."
    if channel == "goodnight":
        return goodnight_prompt(today), "The date-specific paths are generated for today."
    if channel == "completions":
        return (
            PROCESS_COMPLETE_EXAMPLE,
            "Example only — the actual message is built from the completed process's JSON marker.",
        )
    raise ValueError(f"Unsupported prompt-preview channel: {channel}")


def register_config_browser(app, agent, scheduler=None):
    """Register the Brain (configuration) UI routes on the Flask app."""
    bp = Blueprint("config_browser", __name__)

    @bp.route("/config")
    def config_index():
        categories = [
            {"key": key, "label": label, "note": note}
            for key, (label, note, _base, _files_fn) in CATEGORIES.items() if key != "tools"
        ]
        state_dir = Path("state")
        worker_control = state_dir / "worker_control.md"
        scheduler_control = state_dir / "scheduler_control.md"
        watcher_control = state_dir / "watcher_control.md"
        
        def is_active(path):
            if not path.exists():
                return True if path.name != "worker_control.md" else False
            
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        return line.lower() == "active"
            except Exception:
                pass
            return False if path.name == "worker_control.md" else True

        worker_active = is_active(worker_control)
        scheduler_active = is_active(scheduler_control)
        watcher_active = is_active(watcher_control)

        return render_template(
            "config/index.html",
            categories=categories,
            headroom_enabled=getattr(agent, "headroom_enabled", False),
            recall_enabled=bool(getattr(agent, "recall_enabled", True)),
            learning_enabled=bool(getattr(agent, "learning_enabled", True)),
            experiential_enabled=bool(
                getattr(
                    getattr(agent, "experience", None),
                    "influences_model",
                    True,
                )
            ),
            agent_timezone=_tower_settings.get_agent_timezone(),
            now_iso=datetime.now(timezone.utc).isoformat(),
            worker_active=worker_active,
            scheduler_active=scheduler_active,
            watcher_active=watcher_active,
            page_context=ui_ctx.config_index(),
        )

    @bp.route("/config/browse")
    def config_browse():
        cat = request.args.get("cat", "")
        entry = CATEGORIES.get(cat)
        if entry is None:
            abort(404)
        label, note, base, files_fn = entry

        subpath = request.args.get("path", "").strip("/")
        contents = _dir_contents(base, subpath, files_fn())

        crumbs = []
        if subpath:
            parts = subpath.split("/")
            crumbs = [{"name": p, "path": "/".join(parts[: i + 1])} for i, p in enumerate(parts)]

        return render_template(
            "config/browse.html",
            cat=cat, label=label, note=note, subpath=subpath, crumbs=crumbs,
            folders=contents["folders"], files=[_file_meta(p) for p in contents["files"]],
            page_context=ui_ctx.config_browse(
                cat, subpath, [p.as_posix() for p in contents["files"]],
            ),
        )

    @bp.route("/config/file")
    def config_file():
        relpath = request.args.get("f", "")
        path = _all_editable().get(relpath)
        if path is None:
            abort(404)
        return render_template(
            "config/file.html", relpath=relpath, content=path.read_text(encoding="utf-8"),
            back_url=_browse_url_for_relpath(relpath),
            page_context=ui_ctx.config_file(relpath),
        )

    @bp.route("/config/file", methods=["POST"])
    def config_file_save():
        relpath = request.form.get("path", "")
        path = _all_editable().get(relpath)
        if path is None:
            abort(404)
        content = request.form.get("content", "")
        path.write_text(content, encoding="utf-8")
        return redirect(url_for("config_browser.config_file", f=relpath, saved=1))

    @bp.route("/config/file/delete", methods=["POST"])
    def config_file_delete():
        relpath = request.form.get("path", "")
        path = _all_editable().get(relpath)
        if path is None:
            abort(404)
        back = _browse_url_for_relpath(relpath)
        path.unlink()
        return redirect(back)

    @bp.route("/config/create", methods=["POST"])
    def config_create():
        cat = request.form.get("cat", "")
        subpath = request.form.get("path", "").strip("/")
        filename = request.form.get("filename", "").strip()
        target = _resolve_create_path(cat, subpath, filename)
        if target is None:
            abort(400)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            abort(409)
            
        if cat == "tools" and target.suffix == ".py":
            template = '''TOOL_DEFINITIONS = [
    {
        "name": "new_tool",
        "description": "Description of the tool.",
        "input_schema": {
            "type": "object",
            "properties": {
                "arg1": {"type": "string", "description": "An argument."},
            },
            "required": ["arg1"],
        },
    },
]

async def execute_tool(name: str, inputs: dict, working_dir: str = None) -> str:
    if name == "new_tool":
        return f"Executed new_tool with arg1={inputs.get('arg1')}"
    return f"[error] unknown personal tool: {name}"
'''
            target.write_text(template, encoding="utf-8")
        else:
            target.write_text(f"# {target.stem}\n\n", encoding="utf-8")
            
        relpath = target.as_posix()
        return redirect(url_for("config_browser.config_file", f=relpath))

    @bp.route("/config/tools/default")
    def config_tools_default():
        from harness.tools import visible_tool_definitions, _developer_tool_names
        dev_names = _developer_tool_names()
        tools = [t for t in visible_tool_definitions() if t["name"] in dev_names]
        return render_template(
            "config/tools_default.html",
            tools=tools,
            page_context=ui_ctx.config_browse("tools", "default", []),
        )

    @bp.route("/config/activity/toggle", methods=["POST"])
    def config_activity_toggle():
        data = request.get_json(silent=True) or {}
        if not data:
            data = request.form
            
        target = data.get("target")
        state = data.get("state")
        
        if state not in ("active", "paused"):
            abort(400)
            
        targets = []
        if target == "all":
            targets = ["worker", "scheduler", "watcher"]
        elif target in ("worker", "scheduler", "watcher"):
            targets = [target]
        else:
            abort(400)
            
        state_dir = Path("state")
        state_dir.mkdir(parents=True, exist_ok=True)
        for t in targets:
            path = state_dir / f"{t}_control.md"
            path.write_text(f"{state}\n", encoding="utf-8")
            
        return jsonify({"success": True, "state": state, "targets": targets})

    @bp.route("/config/preview")
    def config_preview():
        channel = request.args.get("channel", "main")
        channel_ids = {channel_id for channel_id, _label in PROMPT_CHANNELS}
        if channel not in channel_ids:
            abort(404)
        trigger, trigger_note = _preview_trigger(channel, scheduler)
        messages = agent.conversations.get(channel, [])
        return render_template(
            "config/preview.html",
            channels=PROMPT_CHANNELS,
            selected_channel=channel,
            system_blocks=agent._assemble_system_blocks(channel),
            trigger=trigger,
            trigger_note=trigger_note,
            history_count=len(messages),
            page_context=ui_ctx.config_preview(),
        )

    app.register_blueprint(bp)

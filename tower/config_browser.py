"""Tower UI — "Brain": live agent configuration browser.

Read/edit surface for every *.md file the agent actually consults:

  - selected config/*.md — explicit stable prompt allowlist
  - jobs/*.md     — playbooks, read on demand by the worker/scheduler
  - knowledge/*.md — indexed procedures and reference, read on demand
  - state/*.md    — board files (plan, progress, steering, backlog, ...)
  - sme/*.md      — subject-matter knowledge, mined into the palace

Nothing here is cached: every view re-reads from disk, and `MemoryManager`
does the same on every agent turn (see harness/memory.py), so what you see
here is exactly what the agent sees on its next call — no restart needed.

Editing writes straight to the working tree, then auto-commits so every
change lands in git history without a separate step.

Files are only reachable through a server-side enumeration of the configured
directories above (`_all_editable()`), never through a raw filesystem path
taken from the request — that enumeration IS the whitelist.
"""

import logging
import subprocess
from pathlib import Path

from flask import Blueprint, abort, redirect, render_template, request, url_for

from harness.memory import STABLE_FILES

from . import ui_context as ui_ctx

log = logging.getLogger("galadriel.tower.config")

CONFIG_DIR = Path("config")
JOBS_DIR = Path("jobs")
STATE_DIR = Path("state")
SME_DIR = Path("sme")
KNOWLEDGE_DIR = Path("knowledge")


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


def _all_editable() -> dict[str, Path]:
    """relpath (posix string) -> Path for every file this UI may view/edit.
    Recomputed per request; a path is only ever opened if it appears here."""
    files = (
        _identity_files()
        + _job_files()
        + _knowledge_files()
        + _state_files()
        + _sme_files()
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
    "sme": ("SME Knowledge", "mined into the palace, not in the prompt", SME_DIR, _sme_files),
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


def _git_commit(relpath: str, verb: str = "edit") -> None:
    """Best-effort auto-commit of one file change via Tower. Never raises."""
    try:
        if verb == "delete":
            subprocess.run(["git", "add", "-u", "--", relpath], check=True, capture_output=True)
        else:
            subprocess.run(["git", "add", "--", relpath], check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", f"tower: {verb} {relpath}"],
            check=True, capture_output=True,
        )
    except Exception as e:
        log.info(f"Tower auto-commit skipped for {relpath} ({verb}): {e}")


def _safe_filename(name: str) -> bool:
    """True if `name` is a single-segment *.md filename (no path components)."""
    name = (name or "").strip()
    return bool(name) and name.endswith(".md") and "/" not in name and "\\" not in name and ".." not in name


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


def register_config_browser(app, agent):
    """Register the Brain (configuration) UI routes on the Flask app."""
    bp = Blueprint("config_browser", __name__)

    @bp.route("/config")
    def config_index():
        categories = [
            {"key": key, "label": label, "note": note}
            for key, (label, note, _base, _files_fn) in CATEGORIES.items()
        ]
        return render_template(
            "config/index.html", categories=categories,
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
        _git_commit(relpath, "edit")
        return redirect(url_for("config_browser.config_file", f=relpath, saved=1))

    @bp.route("/config/file/delete", methods=["POST"])
    def config_file_delete():
        relpath = request.form.get("path", "")
        path = _all_editable().get(relpath)
        if path is None:
            abort(404)
        back = _browse_url_for_relpath(relpath)
        path.unlink()
        _git_commit(relpath, "delete")
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
        target.write_text(f"# {target.stem}\n\n", encoding="utf-8")
        relpath = target.as_posix()
        _git_commit(relpath, "create")
        return redirect(url_for("config_browser.config_file", f=relpath))

    @bp.route("/config/preview")
    def config_preview():
        return render_template(
            "config/preview.html",
            stable=agent.memory.build_stable_text(),
            dynamic=agent.memory.build_dynamic_text(),
            page_context=ui_ctx.config_preview(),
        )

    app.register_blueprint(bp)

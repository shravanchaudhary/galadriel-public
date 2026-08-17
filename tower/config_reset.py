"""Restore this runtime's config/ files to the defaults baked into its image.

Runs inside the serving process rather than a one-off task so caches built
from those files can be invalidated in the same call.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from .slack_integration import internal_signature_valid

MAX_RESET_BYTES = 4096
RECALL_CONFIG_FILE = "system_recalls.json"


def _config_roots() -> tuple[Path, Path]:
    defaults_root = Path(
        os.environ.get("GALADRIEL_DEFAULTS_ROOT", "/opt/galadriel-defaults")
    )
    storage_root = Path(os.environ.get("GALADRIEL_STORAGE_ROOT", "/mnt/efs"))
    return defaults_root / "config", storage_root / "config"


def reset_config_to_defaults() -> list[str]:
    """Overwrite persisted config files with the image defaults.

    Returns the relative paths whose bytes actually changed; unchanged files
    are left alone so a no-op reset costs no prompt-cache miss and no Stage-1
    index rebuild.
    """
    defaults_root, target_root = _config_roots()
    changed: list[str] = []
    for source in sorted(defaults_root.rglob("*")):
        relative = source.relative_to(defaults_root)
        target = target_root / relative
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.read_bytes() != source.read_bytes():
            shutil.copy2(source, target)
            changed.append(relative.as_posix())
    if RECALL_CONFIG_FILE in changed:
        # Restoring default cue arrays leaves the recall-id set untouched, which
        # is the whole of the Stage-1 router's cache key, so it must be told.
        from harness.recall import invalidate_semantic_router

        invalidate_semantic_router()
    return changed


def register_config_reset(app) -> None:
    """Register the signed control-plane entry point for a config reset."""
    bp = Blueprint("config_reset", __name__)

    def setting(name: str) -> str:
        return str(current_app.config.get(name) or os.environ.get(name) or "")

    @bp.post("/internal/config/reset")
    def reset_config():
        if (request.content_length or 0) > MAX_RESET_BYTES:
            return jsonify({"error": "Payload too large"}), 413
        raw = request.get_data(cache=True)
        if len(raw) > MAX_RESET_BYTES:
            return jsonify({"error": "Payload too large"}), 413
        data = request.get_json(silent=True) or {}
        tenant_id = setting("REPLIKA_TENANT_ID")
        secret = setting("SLACK_TENANT_AUTH_SECRET")
        if not tenant_id or tenant_id == "default" or not secret:
            return jsonify({"error": "Managed runtime is not configured"}), 503
        if data.get("tenant_id") != tenant_id or not internal_signature_valid(
            tenant_id, raw, secret, request.headers
        ):
            return jsonify({"error": "Unauthorized"}), 401
        return jsonify({"status": "ok", "changed_files": reset_config_to_defaults()})

    app.register_blueprint(bp)

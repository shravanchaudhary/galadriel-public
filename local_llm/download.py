"""Download Gemma Stage-2 GGUF weights into the repo-local models directory."""

from __future__ import annotations

import hashlib
import shutil
import urllib.request
from pathlib import Path

from .config import (
    MODELS_DIR,
    MODEL_FILENAME,
    RECALL_SLM_MODEL_OPTIONS,
    default_model_path,
    hf_file_url,
    resolve_model_profile,
)


class DownloadError(RuntimeError):
    pass


def _download_with_progress(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + ".partial")
    if partial.exists():
        partial.unlink()

    def _reporthook(block_num: int, block_size: int, total_size: int) -> None:
        if total_size <= 0:
            return
        downloaded = block_num * block_size
        pct = min(100.0, downloaded * 100.0 / total_size)
        mb = downloaded / (1024 * 1024)
        total_mb = total_size / (1024 * 1024)
        print(f"\r  downloading {dest.name}: {mb:.1f}/{total_mb:.1f} MB ({pct:.0f}%)", end="", flush=True)

    try:
        urllib.request.urlretrieve(url, partial, reporthook=_reporthook)
        print()
    except Exception as exc:  # noqa: BLE001 — surface as DownloadError
        if partial.exists():
            partial.unlink(missing_ok=True)
        raise DownloadError(f"failed to download {url}: {exc}") from exc

    if not partial.exists() or partial.stat().st_size < 1_000_000:
        partial.unlink(missing_ok=True)
        raise DownloadError(f"download incomplete or too small: {partial}")

    partial.replace(dest)


def ensure_model(
    *,
    filename: str | None = None,
    force: bool = False,
    url: str | None = None,
    repo: str | None = None,
) -> Path:
    """Return path to the GGUF, downloading into MODELS_DIR if missing."""
    name = filename or MODEL_FILENAME
    path = MODELS_DIR / name
    if path.exists() and path.stat().st_size > 1_000_000 and not force:
        return path

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    source = url or hf_file_url(name, repo=repo)
    print(f"Fetching {name}")
    print(f"  from {source}")
    print(f"  into {path}")
    _download_with_progress(source, path)
    return path


def ensure_profile_model(key: str, *, force: bool = False) -> Path:
    """Ensure the GGUF for a Stage-2 profile key is on disk."""
    profile = resolve_model_profile(key)
    return ensure_model(
        filename=profile["filename"],
        repo=profile["hf_repo"],
        force=force,
    )


def ensure_all_profile_models(*, force: bool = False) -> list[Path]:
    """Download every selectable Stage-2 profile GGUF (used by Docker bake)."""
    return [ensure_profile_model(key, force=force) for key in RECALL_SLM_MODEL_OPTIONS]


def model_sha256(path: Path | None = None) -> str:
    target = path or default_model_path()
    h = hashlib.sha256()
    with target.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def clear_partials() -> None:
    if not MODELS_DIR.exists():
        return
    for p in MODELS_DIR.glob("*.partial"):
        p.unlink(missing_ok=True)


def copy_model(src: Path, dest_name: str | None = None) -> Path:
    """Import an already-downloaded GGUF into MODELS_DIR."""
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    dest = MODELS_DIR / (dest_name or src.name)
    shutil.copy2(src, dest)
    return dest

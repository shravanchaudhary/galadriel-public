"""Memory management — daily logs, long-term memory, and context loading.

System prompt is structured for prompt caching:

    [STABLE BLOCK]  ← Explicit allowlist: identity, safety, recall routing,
                      and the worker's ritual index.
                      Marked with cache_control → 90% discount on repeat calls.

    [DYNAMIC BLOCK] ← Daily logs + current timestamp.
                      Changes every call, not cached, but small (~a few hundred
                      tokens) so it does not matter.

For caching to engage at all, the STABLE BLOCK must exceed the model's
minimum cacheable prefix:
    Gemini (default):
    - gemini-3.1-pro-preview / gemini-3.5-flash:  4096 tokens
    - gemini-2.5-flash / gemini-2.5-pro:          2048 tokens
    Claude (if switched back in model_registry.py):
    - Opus 4.6 / 4.5 / Haiku 4.5:           4096 tokens
    - Opus 4.7 / Sonnet 4.6:                2048 tokens
    - Opus 4.8 / Sonnet 4.5 / 4:            1024 tokens

Detailed procedures and reference material live under knowledge/ and jobs/ and
are loaded on demand. See CACHING.md for the full breakdown.
"""

import os
from datetime import datetime, timedelta
from pathlib import Path

# Files that are ALWAYS in the stable block, in this exact order. Adding a
# config markdown file does not make it prompt context; this tuple is the only
# stable-file contract.
STABLE_FILES = (
    "SOUL.md",
    "MEMORY.md",
    "GUARDRAILS.md",
    "RECALL.md",
    "JOBS.md",
)
VISIONS_DIR = "visions"
ACTIVE_VISION_FILE = "active_vision.txt"


class MemoryManager:
    def __init__(self, config_dir: str = "config", memory_dir: str = "memory"):
        self.config_dir = Path(config_dir)
        self.memory_dir = Path(memory_dir)
        self.memory_dir.mkdir(exist_ok=True)

    # ── File helpers ────────────────────────────────────────────

    def _read_file(self, path: Path) -> str | None:
        if path.exists():
            return path.read_text(encoding="utf-8")
        return None

    def _load_active_vision(self) -> str | None:
        """Load the currently active VISION (project focus).

        Controlled by config/active_vision.txt which contains the stem name
        of a file in config/visions/. Only one vision loads at a time.
        Tower can change the active vision via /api/vision.
        """
        active_file = self.config_dir / ACTIVE_VISION_FILE
        if not active_file.exists():
            return None
        name = active_file.read_text(encoding="utf-8").strip()
        if not name:
            return None
        vision_path = self.config_dir / VISIONS_DIR / f"{name}.md"
        return self._read_file(vision_path)

    def _active_project_name(self) -> str | None:
        """Return the current active-project name (the stem in active_vision.txt),
        or None if unset.

        This is the lightweight counterpart to _load_active_vision(): instead of
        loading the whole vision file into the (cached) stable block, it returns
        just the name so a per-turn scoping banner can be placed in the dynamic
        block. Toggling the project is then instantly visible without paying a
        cache invalidation. Tower writes this file via /api/vision.
        """
        active_file = self.config_dir / ACTIVE_VISION_FILE
        if not active_file.exists():
            return None
        name = active_file.read_text(encoding="utf-8").strip()
        return name or None

    # ── Stable / dynamic split ──────────────────────────────────

    def build_stable_text(self) -> str:
        """Assemble the cacheable portion of the system prompt."""
        parts: list[str] = []

        for fname in STABLE_FILES:
            content = self._read_file(self.config_dir / fname)
            if content:
                label = "Long-Term Memory" if fname == "MEMORY.md" else fname
                parts.append(f"# {label}\n\n{content}")
            if fname == "SOUL.md":
                vision = self._load_active_vision()
                if vision:
                    parts.append(f"# Active Vision\n\n{vision}")

        if not parts:
            return "You are Replika, a helpful personal AI assistant."
        return "\n\n---\n\n".join(parts)

    def build_dynamic_text(self) -> str:
        """Assemble the non-cached portion: active-project banner + wake-up
        + daily logs + timestamp.

        Daily logs are placed here (not in the stable block) because they
        grow throughout the day — every append_daily_log() call would
        otherwise invalidate the cache for everything.

        The active-project banner is a tiny per-turn pointer to the currently
        selected project. It lives here (not stable) so toggling it via Tower
        is instantly visible without a cache invalidation.

        Wake-up is a compact L0/L1 snapshot from the memory palace (MemPalace),
        regenerated whenever the palace mines new content. Lives in the
        dynamic block for the same reason: it changes often enough that
        caching it would just churn the prefix. Disable via env
        PALACE_WAKE_UP_INJECT=0.
        """
        parts: list[str] = []

        # Active-project banner (per-turn, cheap). Halls are MemPalace's
        # auto-topic dimension, so project names belong in the query, not hall=.
        project = self._active_project_name()
        if project:
            parts.append(
                f"# Active Project: `{project}`\n\n"
                f"Include `{project}` in palace queries when project-specific "
                f"history matters. Use room filters by memory type, never a "
                f"project name as a hall."
            )

        # Wake-up injection (opt-out via env). Fails silently if mempalace
        # isn't installed or no cache file exists yet.
        if os.environ.get("PALACE_WAKE_UP_INJECT", "1") != "0":
            try:
                from . import palace
                wake = palace.read_wake_up_text()
                if wake:
                    parts.append(f"# Memory Palace — Wake-Up\n\n{wake}")
            except Exception:
                pass  # never break prompt assembly on a palace hiccup

        today = datetime.now()
        # Chronological order: yesterday first, then today.
        for delta in (1, 0):
            day = today - timedelta(days=delta)
            filename = day.strftime("%Y-%m-%d.md")
            note = self._read_file(self.memory_dir / filename)
            if note:
                parts.append(f"# Daily Log ({filename})\n\n{note}")

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tail = f"Current date/time: {now}"

        if parts:
            return "\n\n---\n\n".join(parts) + "\n\n---\n\n" + tail
        return tail

    # ── Public API for agent.py ─────────────────────────────────

    def build_system_blocks(self) -> list[dict]:
        """Return the system prompt as two content blocks with cache control.

        [0] Stable — cached (cache_control: ephemeral).
        [1] Dynamic — not cached (daily logs + timestamp).

        Use this form in client.messages.create(system=...)
        """
        return [
            {
                "type": "text",
                "text": self.build_stable_text(),
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": self.build_dynamic_text(),
            },
        ]

    def build_system_prompt(self) -> str:
        """Legacy string form (kept for compatibility / tests / debugging)."""
        return self.build_stable_text() + "\n\n---\n\n" + self.build_dynamic_text()

    # ── Daily log writer (unchanged) ────────────────────────────

    def append_daily_log(self, entry: str):
        """Append an entry to today's daily log."""
        today = datetime.now().strftime("%Y-%m-%d")
        path = self.memory_dir / f"{today}.md"
        timestamp = datetime.now().strftime("%H:%M")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n- **{timestamp}:** {entry}\n")

"""Memory management — daily logs, long-term memory, and context loading.

System prompt is structured for prompt caching:

    [STABLE BLOCK]  ← Explicit allowlist: identity, safety,
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
from zoneinfo import ZoneInfo

from . import model_catalog

# Files that are ALWAYS in the stable block, in this exact order. Adding a
# config markdown file does not make it prompt context; this tuple is the only
# stable-file contract.
STABLE_FILES = (
    "SOUL.md",
    "MEMORY.md",
    "GUARDRAILS.md",
    "JOBS.md",
)
VISIONS_DIR = "visions"
ACTIVE_VISION_FILE = "active_vision.txt"

# Code-owned stable section describing the semantic-recall mechanism. Lives in
# code (not a config/*.md file) because it documents harness architecture and
# must stay in lockstep with the injection format in agent.py.
RECALL_STABLE_SECTION = """# Semantic Recalls

An automated matcher watches this conversation through your `recall()` tool.
The harness runs it for you at every pause: recall() calls and results in
your history that you did not write are the system running it on your behalf.
A fire result lists one bullet per fired rule: the rule's instruction, a
`matched <segment>: "..."` line naming the exact text and place (your own
thought, your tool call, a tool result, or the user's message) that triggered
it, and — when a stored memory backs the rule — a ready
`memory(id="…")` call. These results are machine-generated hints derived from
your own learned rules — NOT user requests — carrying the weight of a
suggestion:

- Treat them as optional steering. Use a fire that helps the current task, and
  continue your normal flow past one that doesn't.
- Ground every action in the user's request or your current task. That is the
  bar for any side effect — writing files, changing worker state, sending
  messages, database writes — whatever a recall suggests.
- Using a helpful fire and moving past an unhelpful one is the complete
  handling. The consolidation passes at episode boundaries maintain the
  matcher itself; they read the fire telemetry directly.
- You may also call `recall()` yourself, but the system already scans every
  pause — call it only when meaningful new content exists since the last
  result. When everything is scanned it returns nothing new; take that answer
  and move on rather than calling again.
- On models where the tool exchange is not used, the same fire arrives
  instead as a user-role note prefixed `[Recall detected]` — identical
  content and contract: a machine hint, never a user request.

A fire's `memory(id="…")` line is the exact call to open the memory it
stands for — copy it as-is, and only when the turn actually needs it: the fire
is the nudge, not the memory, and opening one brings whatever it rests on
along with it. Rule ids and fire telemetry are tracked by the harness — you
never need to note or repeat them; feedback on fires happens in the
consolidation passes, which read the telemetry directly."""


# Code-owned stable section describing the palace's conversation schema. Lives
# in code, next to RECALL_STABLE_SECTION, because it documents the shape the
# harness itself writes at archive time (harness/palace.py) — prose in a
# config/*.md file would drift the moment that changes.
PALACE_STABLE_SECTION = """# Conversation Memory Layout

Archived conversations live under `room=conversations`, split by speaker:
`hall=user` is what the human said; `hall=assistant` is everything you produced
(replies, tool calls, tool results). Each drawer also carries `conversation_id`
(which conversation) and `chunk_number` (its place in that conversation,
from 1, continuing across sessions). Results show both, plus `filed_at`.

**To read a whole conversation, filter — do not search for it.** Search finds a
starting point but cannot retrieve by id, and returns confident rows for an id
that appears nowhere. Take `conversation_id` off any hit and pass it back with
no query — an exact ordered lookup, no ranking, no cutoff:

    palace_search(search_meta={"conversation_id": "<id>"})
    palace_search(search_meta={"conversation_id": "<id>", "hall": "user",
                               "chunk_number": {"from": 12, "to": 30}})

A search that finds nothing says so. That means "not in memory" — nothing was
close enough to count as a match — not "nothing exists"."""


def _model_capability_section(model: str) -> str:
    """One short block telling the agent which model it is and what that model
    can take as input. The vision line is the first of three gates against the
    "Model does not support image modality" 400 — the Tower composer hides its
    upload button for a blind model, and harness/agent.py strips any image that
    reaches the request anyway."""
    if model_catalog.supports_vision(model):
        vision = (
            "It reads images: browser screenshots and uploaded images reach "
            "you as vision input."
        )
    else:
        vision = (
            "It cannot read images. Screenshots and uploaded images never "
            "reach you — they are replaced by a text placeholder. Take a "
            "screenshot only when a human needs to see one, and read pages "
            "as text instead (`browser read_page` / `get_page_text`)."
        )
    return f"# Runtime Model\n\nYou are running on `{model}`. {vision}"


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

    def build_stable_text(self, model: str | None = None) -> str:
        """Assemble the cacheable portion of the system prompt.

        `model` adds the running model's capability note. It belongs in the
        stable block — it only changes when the model changes, and a model
        switch invalidates the prompt cache anyway.
        """
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

        parts.append(RECALL_STABLE_SECTION)
        parts.append(PALACE_STABLE_SECTION)

        if model:
            parts.append(_model_capability_section(model))

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

        Wake-up is a compact digest of learned memory from the palace,
        regenerated whenever the palace mines new content. Lives in the
        dynamic block for the same reason: it changes often enough that
        caching it would just churn the prefix. Disable via env
        PALACE_WAKE_UP_INJECT=0.
        """
        parts: list[str] = []

        # Active-project banner (per-turn, cheap). `hall` is the speaker in
        # room=conversations, so a project name belongs in the query, not hall=.
        project = self._active_project_name()
        if project:
            parts.append(
                f"# Active Project: `{project}`\n\n"
                f"Include `{project}` in palace queries when project-specific "
                f"history matters. Scope with room filters by memory type — "
                f"never a project name as a hall, which is the speaker in "
                f"room=conversations."
            )

        # Wake-up injection (opt-out via env). Fails silently if no cache file
        # exists yet.
        if os.environ.get("PALACE_WAKE_UP_INJECT", "1") != "0":
            try:
                from . import palace
                wake = palace.read_wake_up_text()
                if wake:
                    parts.append(f"# Memory Palace — Wake-Up\n\n{wake}")
            except Exception:
                pass  # never break prompt assembly on a palace hiccup

        now, tz_name = self._agent_now()
        # Chronological order: yesterday first, then today.
        for delta in (1, 0):
            day = now - timedelta(days=delta)
            filename = day.strftime("%Y-%m-%d.md")
            note = self._read_file(self.memory_dir / filename)
            if note:
                parts.append(f"# Daily Log ({filename})\n\n{note}")

        tail = (
            f"Current date/time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')} "
            f"({tz_name}, weekday {now.strftime('%A')})"
        )

        if parts:
            return "\n\n---\n\n".join(parts) + "\n\n---\n\n" + tail
        return tail

    def _agent_now(self) -> tuple[datetime, str]:
        """Now in the user-configured agent timezone (Configuration → Agent time)."""
        try:
            from . import tower_settings
            return tower_settings.agent_now(), tower_settings.get_agent_timezone()
        except Exception:
            tz_name = "Europe/Stockholm"
            return datetime.now(ZoneInfo(tz_name)), tz_name

    # ── Public API for agent.py ─────────────────────────────────

    def build_system_blocks(self, model: str | None = None) -> list[dict]:
        """Return the system prompt as two content blocks with cache control.

        [0] Stable — cached (cache_control: ephemeral).
        [1] Dynamic — not cached (daily logs + timestamp).

        Use this form in client.messages.create(system=...)
        """
        return [
            {
                "type": "text",
                "text": self.build_stable_text(model),
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
        now, _ = self._agent_now()
        today = now.strftime("%Y-%m-%d")
        path = self.memory_dir / f"{today}.md"
        timestamp = now.strftime("%H:%M")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n- **{timestamp}:** {entry}\n")

"""Core agent — wraps the LLM API with tools, memory, and safety.

Prompt caching strategy (provider-specific; see harness/memory.py and
harness/providers/gemini_provider.py for details):

  Anthropic (explicit breakpoints):
  1. cache_control on the LAST tool definition
       → caches the `tools` prefix on its own.
  2. cache_control on system[0] (the stable block built by MemoryManager)
       → caches tools + stable system as one prefix.
  3. cache_control on the LAST block of the LAST message (injected per-call)
       → caches the growing message history.

  Gemini (implicit caching, default):
  - Stable system blocks → system_instruction (fixed prefix every call).
  - Dynamic system block → trailing user turn at end of contents.
  - No cache_control markers; hits surface as cached_content_token_count.

Usage tokens are logged after every API call so you can verify caching is
actually engaging. You want cache_read to climb on the second API call within
a turn and stay high afterward.
"""

import asyncio
import os
import json
import logging
from datetime import datetime
from pathlib import Path
from .memory import MemoryManager
from .tools import TOOL_DEFINITIONS, execute_tool
from .safety import classify_command, format_safety_notice, _simple_rm_target
from .providers import BaseModelProvider
from . import model_registry
from . import conversation_store
from . import cost_tracker
from . import headroom_compress
from . import tower_settings

log = logging.getLogger("galadriel")


# The single shared channel_id used by every human-facing interactive surface
# (Discord, Slack, Tower) so they all read/write the same conversation and the
# agent has continuous context regardless of which surface the user is on.
# Background/system channels (heartbeat, worker, morning, etc.) stay on their
# own synthetic channel_ids and are unaffected.
MAIN_CHANNEL_ID = "main"
WORKER_CHANNEL_ID = "worker"

_STOPPED_TOOL_RESULT = "[STOPPED] Turn cancelled from Tower."
_STOPPED_ASSISTANT_NOTE = "(Stopped — turn cancelled from Tower.)"


class TurnCancelled(Exception):
    """Raised when a channel turn is cancelled via request_stop()."""


# ─── Context-window warnings ──────────────────────────────────────────
#
# After each API call we measure input_tokens + cache_read + cache_write —
# the actual size Claude processed — and compare against the model's context
# window. If we cross 90% or 95% we nudge the user via a harness-level
# Discord message suggesting /compact or /new. One nudge per tier crossing;
# dropping back below 90% resets the tracker so a future crossing re-fires.

CONTEXT_WINDOW_DEFAULT = 200_000  # tokens — Claude Sonnet/Opus/Haiku 4.x default

# Only list explicit overrides here. Anything unknown falls back to the default.
CONTEXT_WINDOW_OVERRIDES = {
    # 1M-context models
    "claude-opus-4-5-1m": 1_000_000,
    "claude-sonnet-4-5-1m": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    # Gemini — 1,048,576-token context window (official, per ai.google.dev).
    "gemini-3.5-flash": 1_000_000,
    "gemini-3.1-pro-preview": 1_000_000,
    "gemini-2.5-pro": 1_000_000,
    "gemini-2.5-flash": 1_000_000,
    # Ollama — serving window is options.num_ctx (OLLAMA_NUM_CTX, default 65536),
    # not the model's native 256K. Keep this in sync with OllamaProvider.num_ctx.
    "qwen3-vl:8b": int(os.environ.get("OLLAMA_NUM_CTX") or 65_536),
}

WARN_TIER_ATTENTION = "attention"  # 90%
WARN_TIER_URGENT = "urgent"        # 95%
_TIER_RANK = {WARN_TIER_ATTENTION: 1, WARN_TIER_URGENT: 2}


def _resolve_context_window(model: str) -> int:
    env = os.environ.get("AGENT_CONTEXT_WINDOW")
    if env and env.isdigit():
        return int(env)
    return CONTEXT_WINDOW_OVERRIDES.get(model.lower(), CONTEXT_WINDOW_DEFAULT)


# Minimum cacheable prefix per model, in tokens. Caching silently no-ops below
# the floor, so this is reported at startup. Mirrors the table in memory.py /
# CACHING.md. Unknown models default to the conservative 4096.
CACHE_MINIMUM_DEFAULT = 4096
CACHE_MINIMUM_OVERRIDES = {
    # Gemini (per-tier; 3.x preview values track this project's docs)
    "gemini-3.1-pro-preview": 4096,
    "gemini-3.5-flash": 4096,
    "gemini-2.5-pro": 2048,
    "gemini-2.5-flash": 2048,
    # Claude
    "claude-opus-4-8": 1024,
    "claude-sonnet-4-6": 2048,
    "claude-opus-4-7": 2048,
    "claude-haiku-4-5": 4096,
}


def _resolve_cache_minimum(model: str) -> int:
    return CACHE_MINIMUM_OVERRIDES.get(model.lower(), CACHE_MINIMUM_DEFAULT)


def _format_context_warning(pct: int, tier: str, tokens_used: int, window: int) -> str:
    if tier == WARN_TIER_ATTENTION:
        return (
            f"📚 *Context window is at **{pct}%** "
            f"({tokens_used:,} / {window:,} tokens). "
            f"Still plenty sharp — but a quick `/compact` or `/new` would keep "
            f"future turns fast and cheap.*"
        )
    return (
        f"🔥 *Context window is at **{pct}%** "
        f"({tokens_used:,} / {window:,} tokens) — nearing the cliff where "
        f"responses risk truncation. Consider `/compact` or `/new` before the "
        f"next exchange.*"
    )


def _serialize_content(content):
    """Convert SDK ContentBlock objects to plain dicts for reliable serialization."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        serialized = []
        for block in content:
            if hasattr(block, "model_dump"):
                serialized.append(block.model_dump(exclude_none=True))
            elif isinstance(block, dict):
                serialized.append(block)
            else:
                serialized.append({"type": "text", "text": str(block)})
        return serialized
    if hasattr(content, "model_dump"):
        return content.model_dump(exclude_none=True)
    return str(content)


def _summarize_tool_input(tool_input) -> str:
    """Full JSON view of a tool's input for live UI streaming. The UI shows a
    one-line preview in the card header and the complete value on expand."""
    try:
        return json.dumps(tool_input, ensure_ascii=False)
    except Exception:
        return str(tool_input)


def _contains_tool_use(msg: dict) -> bool:
    """Check if an assistant message contains tool_use blocks."""
    content = msg.get("content", [])
    if not isinstance(content, list):
        return False
    return any(
        isinstance(b, dict) and b.get("type") == "tool_use"
        for b in content
    )


def _contains_tool_result(msg: dict) -> bool:
    """Check if a user message contains tool_result blocks."""
    content = msg.get("content", [])
    if not isinstance(content, list):
        return False
    return any(
        isinstance(b, dict) and b.get("type") == "tool_result"
        for b in content
    )


def _build_cached_tools() -> list[dict]:
    """Attach cache_control to the last tool so the tools prefix gets cached.

    Cache breakpoints themselves are free — they only affect what gets
    hashed into a cache entry. Placing one on the last tool means the
    entire `tools` array forms its own cache prefix, which survives
    unchanged across every call (tools never change at runtime).
    """
    # Stateless / no-palace mode: filter palace tools out entirely so the agent
    # isn't offered memory it's been told to forget.
    from .tools import visible_tool_definitions
    defs = visible_tool_definitions()
    if not defs:
        return []
    cached = [dict(t) for t in defs]
    cached[-1] = {**cached[-1], "cache_control": {"type": "ephemeral"}}
    return cached


def _attach_trailing_cache_control(messages: list) -> list:
    """Return a shallow copy of `messages` with cache_control on the last block.

    The stored `messages` list is left untouched — we only attach cache_control
    to the version sent to the API. This way the persistent conversation
    history in self.conversations never contains cache_control markers
    (which would complicate serialization / history display).

    If the last message's content is:
      - a list of blocks: add cache_control to the last block (most common).
      - a plain string: wrap it in a text block with cache_control.
      - empty/malformed: return messages unchanged.
    """
    if not messages:
        return messages

    out = list(messages)
    last = out[-1]
    content = last.get("content")

    if isinstance(content, list) and content:
        new_content = list(content[:-1]) + [
            {**content[-1], "cache_control": {"type": "ephemeral"}}
        ]
        out[-1] = {**last, "content": new_content}
    elif isinstance(content, str):
        out[-1] = {
            **last,
            "content": [
                {
                    "type": "text",
                    "text": content,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        }
    return out


def _dump_prompt_to_file(memory: "MemoryManager", tools: list, debug_dir: str = "debug"):
    """Serialize the system prompt (blocks) and tools to JSON for inspection.

    Stored in {debug_dir}/prompts/ with ISO timestamp. Useful for:
      - Verifying the exact system prompt sent to the API
      - Debugging cache behavior (confirm stable block size)
      - Tracking changes over time
    """
    try:
        debug_path = Path(debug_dir)
        prompts_path = debug_path / "prompts"
        prompts_path.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.utcnow().isoformat()
        filename = f"prompt_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

        system_blocks = memory.build_system_blocks()
        dump = {
            "timestamp": timestamp,
            "system_blocks": system_blocks,
            "tools_count": len(tools),
            "tools": tools,
        }

        filepath = prompts_path / filename
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(dump, f, indent=2)

        log.info(f"Prompt snapshot saved to {filepath}")
    except Exception as e:
        log.warning(f"Could not dump prompt to file: {e}")


class GaladrielAgent:
    """Stateful conversational agent backed by Claude with tool use."""

    def __init__(
        self,
        api_key: str = None,
        model: str = None,
        max_tokens: int = None,
        config_dir: str = "config",
        memory_dir: str = "memory",
        working_dir: str = None,
        approval_callback=None,
        debug_dir: str = "debug",
        provider: BaseModelProvider = None,
    ):
        # Explicitly-injected provider (tests) always wins; otherwise providers
        # are resolved per-model so main/worker can sit on different backends
        # and mid-chat Gemini ↔ Ollama switches stay safe.
        self._injected_provider = provider
        self._provider_cache: dict[str, BaseModelProvider] = {}
        self._api_key = api_key
        default_model = model or model_registry.model_for("agent")
        self._channel_models: dict[str, str] = {}
        if provider is None and model is None:
            for channel in tower_settings.CONFIGURABLE_CHANNELS:
                saved = tower_settings.get_channel_model(channel)
                if saved:
                    self._channel_models[channel] = saved
        self.model = self._channel_models.get(MAIN_CHANNEL_ID, default_model)
        self._channel_models.setdefault(MAIN_CHANNEL_ID, self.model)
        self._channel_models.setdefault(WORKER_CHANNEL_ID, self.model)
        self.provider = provider or self._provider_for(self.model)
        # Best-effort provider id for cost logging (matches model_registry's
        # ANTHROPIC/GEMINI/OLLAMA strings even when a custom `provider` is injected).
        self.provider_name = model_registry.provider_for_model(self.model)
        # In-process Headroom compression (Tower toggle). Default off.
        self.headroom_enabled = tower_settings.get_headroom_enabled()
        self.max_tokens = max_tokens or int(os.environ.get("AGENT_MAX_TOKENS", "8192"))
        self.memory = MemoryManager(config_dir=config_dir, memory_dir=memory_dir)
        self.working_dir = working_dir or os.getcwd()
        self.conversations: dict[str, list] = {}
        restored = conversation_store.load_all(self.working_dir)
        if restored:
            self.conversations.update(restored)
            total = sum(len(m) for m in restored.values())
            log.info(
                f"Restored {len(restored)} conversation buffer(s) "
                f"({total} message(s)) from disk."
            )
        # One lock per channel_id, created lazily. Serializes respond() calls on
        # the SAME channel so two near-simultaneous turns (e.g. from different
        # gateways sharing MAIN_CHANNEL_ID) can't interleave mid-turn and break
        # the API's strict user/assistant/tool_result alternation.
        self._channel_locks: dict[str, asyncio.Lock] = {}
        # Per-channel cancel events for in-flight turns (Tower stop button).
        self._turn_cancel: dict[str, asyncio.Event] = {}
        # Set once archive_conversations_on_shutdown() runs, so overlapping
        # shutdown signals (SIGTERM + atexit) archive exactly once.
        self._shutdown_archived = False
        self.approval_callback = approval_callback
        self.last_usage: dict = {}  # Populated after each API call; used by /status

        # Files the agent has written into existence (not merely edited) during
        # this process's lifetime — deleting one of these via a plain `rm` is
        # auto-approved (see safety.classify_command). Resets on restart, which
        # is the safe direction: an unknown file always falls back to red.
        self._created_files: set[str] = set()

        # Context-window tracking
        self.context_window = _resolve_context_window(self.model)
        self.context_warning_callback = None  # async (channel_id, message) -> None
        self._last_warn_tier: dict[str, str] = {}  # channel_id -> "attention"|"urgent"

        # Output-ceiling streak tracking. Two consecutive responses within
        # 100 tokens of max_tokens usually precede a max_tokens cascade —
        # catch that before the cascade starts.
        self._output_ceiling_streak: dict[str, int] = {}  # channel_id -> count

        # Post-recovery advisory. Set before a hard reset (the max_tokens
        # compaction-fallback path) so the model knows the dropped exchange was
        # archived and can be recalled via palace_search. Cleared on full reset.
        self._post_recovery_archive_tag: dict[str, str] = {}  # channel_id -> archive tag

        # Auto-compaction. When a channel's measured input context crosses the
        # threshold, the next turn compacts the whole conversation into a single
        # structured snapshot and resets the message list. The snapshot is then
        # injected as its own non-cached system block (after stable+dynamic,
        # ahead of the user message) and re-injected each turn until the next
        # compaction folds it in.
        self.compact_threshold = int(os.environ.get("AGENT_COMPACT_THRESHOLD", "180000"))
        self._last_input_tokens: dict[str, int] = {}  # channel_id -> last measured input tokens
        self._compaction_summary: dict[str, str] = {}  # channel_id -> latest snapshot (folds cumulatively)

        # Non-destructive checkpointing. Tracks how many messages of each channel
        # have already been mined to the palace, so periodic checkpoints (driven
        # by the scheduler) mine only the new slice and never create duplicate
        # drawers. Reset whenever compaction rewrites/clears the buffer.
        self._last_archived_len: dict[str, int] = {}  # channel_id -> messages mined so far

        # Extra per-channel system context (e.g. a Slack team roster), set by a
        # gateway and re-injected on every turn until changed/cleared. Kept
        # separate from the compaction snapshot so it survives compaction.
        self._channel_context: dict[str, str] = {}

        # Precompute tools-with-cache once. Tools never change at runtime,
        # so this object can be reused across every API call.
        self.tools = _build_cached_tools()

        # Log stable block metadata on startup
        stable_text = self.memory.build_stable_text()
        stable_chars = len(stable_text)
        stable_tokens_est = stable_chars // 4  # rough estimate: 4 chars per token
        log.info(
            f"Stable block loaded: {stable_chars} chars (~{stable_tokens_est} tokens). "
            f"Model {self.model} cache minimum: "
            f"{_resolve_cache_minimum(self.model)} tokens."
        )

        # Dump the complete prompt (system blocks + tools) to JSON for inspection
        _dump_prompt_to_file(self.memory, self.tools, debug_dir=debug_dir)

    def _get_messages(self, channel_id: str) -> list:
        if channel_id not in self.conversations:
            self.conversations[channel_id] = []
        return self.conversations[channel_id]

    def _lock_for(self, channel_id: str) -> asyncio.Lock:
        lock = self._channel_locks.get(channel_id)
        if lock is None:
            lock = asyncio.Lock()
            self._channel_locks[channel_id] = lock
        return lock

    def request_stop(self, channel_id: str) -> bool:
        """Signal the in-flight turn on this channel to stop. Returns True if
        a turn was active and not already stopping."""
        ev = self._turn_cancel.get(channel_id)
        if ev is not None and not ev.is_set():
            ev.set()
            log.info(f"Stop requested for channel {channel_id}")
            return True
        return False

    def is_channel_busy(self, channel_id: str) -> bool:
        ev = self._turn_cancel.get(channel_id)
        return ev is not None and not ev.is_set()

    def _check_cancelled(self, channel_id: str) -> None:
        ev = self._turn_cancel.get(channel_id)
        if ev is not None and ev.is_set():
            raise TurnCancelled()

    async def _finalize_cancelled_turn(
        self,
        channel_id: str,
        messages: list,
        emit,
        *,
        tool_blocks=None,
        tool_results: list | None = None,
    ) -> str:
        """Persist conversation after a user-initiated stop and return UI text."""
        results = list(tool_results or [])
        if tool_blocks:
            done_ids = {r["tool_use_id"] for r in results}
            for block in tool_blocks:
                tool_id = block.id if hasattr(block, "id") else block.get("id")
                if tool_id and tool_id not in done_ids:
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": _STOPPED_TOOL_RESULT,
                    })
            if results:
                messages.append({"role": "user", "content": results})

        conversation_store.save_channel(self.working_dir, channel_id, messages)
        if emit is not None:
            await emit({"type": "stopped", "text": _STOPPED_ASSISTANT_NOTE})
        return _STOPPED_ASSISTANT_NOTE

    def set_channel_context(self, channel_id: str, text: str | None) -> None:
        """Set (or clear) extra system context injected into every turn for a
        channel — e.g. a Slack team roster so the agent knows who's in the
        channel and that it's a teammate, not a 1:1 assistant. Call again
        whenever the context changes (e.g. membership change); pass None to
        clear it. Not meant to be set per-message — that would bust the
        prompt cache on every turn.
        """
        if text:
            self._channel_context[channel_id] = text
        else:
            self._channel_context.pop(channel_id, None)

    def reset_channel(self, channel_id: str) -> None:
        """Drop a channel's accumulated buffer + per-channel trackers so its next
        turn starts lean.

        Used by the background worker: each tick reconstructs state from the
        board (`state/progress/`, one file per day), the DB ledger, and
        the palace (DATA.md), so carrying the prior tick's transcript forward
        only inflates input tokens and busts the prompt cache across the 10-min
        idle gap (the cache-miss cost blowup, finding #7). Durable continuity
        lives in those files, not in this buffer.
        """
        self.conversations.pop(channel_id, None)
        conversation_store.delete_channel(self.working_dir, channel_id)
        self._last_input_tokens.pop(channel_id, None)
        self._compaction_summary.pop(channel_id, None)
        self._last_archived_len.pop(channel_id, None)
        self._last_warn_tier.pop(channel_id, None)
        self._output_ceiling_streak.pop(channel_id, None)
        self._post_recovery_archive_tag.pop(channel_id, None)

    def _hard_reset(self, messages: list, user_message: str | list):
        """Nuclear option: clear conversation and start fresh with the user message.

        Last-resort fallback when max_tokens recovery via compaction can't help.
        """
        messages.clear()
        messages.append({"role": "user", "content": user_message})
        log.warning("Hard reset: cleared entire conversation, re-seeded with original user message")

    def _archive_and_flag(self, channel_id: str, messages: list) -> None:
        """Fire-and-forget archive of the current conversation + set a post-recovery
        advisory tag. Used before a _hard_reset (the compaction-fallback path) so
        the model knows the dropped exchange is recallable via palace_search.
        Never raises — recovery must not be blocked by archiving.
        """
        if not messages:
            return
        try:
            from . import palace
            tag = f"{channel_id}:max_tokens"
            snapshot = list(messages)  # defensive copy
            asyncio.create_task(
                palace.archive_conversation(channel_id, snapshot, kind="max_tokens")
            )
            self._post_recovery_archive_tag[channel_id] = tag
            log.info(f"Recovery archive queued ({len(snapshot)} msgs, tag={tag})")
        except Exception as e:
            log.warning(f"Recovery archive failed: {e}")

    async def _maybe_warn_context(self, response, channel_id: str):
        """Nudge the user toward /compact or /new when input context crosses
        90% or 95% of the model's context window. One nudge per tier crossing
        per channel; dropping below 90% clears the tracker so future crossings
        re-fire. Silent no-op if no callback is wired up.
        """
        channel_model = self.model_for_channel(channel_id)
        context_window = _resolve_context_window(channel_model)
        if not self.context_warning_callback or context_window <= 0:
            return

        usage = getattr(response, "usage", None)
        if usage is None:
            return

        tokens = (
            getattr(usage, "input_tokens", 0) or 0
        ) + (
            getattr(usage, "cache_read_input_tokens", 0) or 0
        ) + (
            getattr(usage, "cache_creation_input_tokens", 0) or 0
        )
        if tokens <= 0:
            return

        pct = int(100 * tokens / context_window)

        if pct >= 95:
            new_tier = WARN_TIER_URGENT
        elif pct >= 90:
            new_tier = WARN_TIER_ATTENTION
        else:
            # Below threshold — reset so future crossings re-warn
            self._last_warn_tier.pop(channel_id, None)
            return

        last = self._last_warn_tier.get(channel_id)
        if last is not None and _TIER_RANK[new_tier] <= _TIER_RANK[last]:
            # Already warned at this tier (or a higher one) — stay quiet
            return

        self._last_warn_tier[channel_id] = new_tier
        msg = _format_context_warning(pct, new_tier, tokens, context_window)
        try:
            await self.context_warning_callback(channel_id, msg)
            log.info(f"Context warning fired ({new_tier}, {pct}%) for channel {channel_id}")
        except Exception as e:
            log.warning(f"Context warning callback failed: {e}")

    async def _maybe_warn_output_ceiling(self, response, channel_id: str):
        """Warn when output_tokens repeatedly comes within 100 of max_tokens.

        Two consecutive near-ceiling outputs is the usual precursor to the
        max_tokens recovery cascade (trim → trim → hard_reset). Firing the
        warning gives the user a chance to /compact or steer toward brevity
        BEFORE the cascade starts eating conversation history. Silent no-op
        if no callback is wired up.
        """
        if not self.context_warning_callback or self.max_tokens <= 0:
            return
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        out = getattr(usage, "output_tokens", 0) or 0
        if out == 0:
            return

        near_ceiling = out >= (self.max_tokens - 100)
        streak = self._output_ceiling_streak.get(channel_id, 0)

        if not near_ceiling:
            # Any response comfortably below the ceiling resets the streak
            self._output_ceiling_streak.pop(channel_id, None)
            return

        streak += 1
        self._output_ceiling_streak[channel_id] = streak

        # Fire the nudge exactly once per streak, at 2.
        if streak == 2:
            msg = (
                f"⚠️ **Output-ceiling streak.** Two responses in a row hit near "
                f"the `max_tokens` ceiling ({out}/{self.max_tokens}). "
                f"A third near-ceiling response will start the recovery cascade "
                f"(trim → trim → hard reset), which archives to the palace but "
                f"breaks conversational flow. Consider `/compact` now, or steer "
                f"the agent toward more concise responses."
            )
            try:
                await self.context_warning_callback(channel_id, msg)
                log.info(f"Output-ceiling warning fired for channel {channel_id} (streak={streak}, out={out})")
            except Exception as e:
                log.warning(f"Output-ceiling warning callback failed: {e}")

    def model_for_channel(self, channel_id: str) -> str:
        """Return the model used for API calls on this channel."""
        if channel_id == WORKER_CHANNEL_ID:
            return self._channel_models.get(WORKER_CHANNEL_ID, self.model)
        return self._channel_models.get(MAIN_CHANNEL_ID, self.model)

    def _provider_for(self, model: str) -> BaseModelProvider:
        """Return (and memoize) the provider for `model`.

        An explicitly-injected provider always wins so tests stay isolated.
        Otherwise provider follows the model name via
        `model_registry.provider_for_model`.
        """
        if self._injected_provider is not None:
            return self._injected_provider
        name = model_registry.provider_for_model(model)
        cached = self._provider_cache.get(name)
        if cached is not None:
            return cached
        provider = model_registry.build_provider(
            name, api_key=self._api_key if name == model_registry.ANTHROPIC else None,
        )
        self._provider_cache[name] = provider
        return provider

    def set_model(self, model: str, channel: str = MAIN_CHANNEL_ID) -> None:
        """Switch a channel's model at runtime and persist the choice in MongoDB.

        Provider follows the model (Gemini ↔ Ollama mid-chat is safe because
        history is stored in Anthropic format and each provider translates).
        """
        if channel not in tower_settings.CONFIGURABLE_CHANNELS:
            raise ValueError(f"Unsupported channel: {channel}")
        if model not in tower_settings.AGENT_MODEL_OPTIONS:
            raise ValueError(f"Unsupported model: {model}")
        self._channel_models[channel] = model
        if channel == MAIN_CHANNEL_ID:
            self.model = model
            self.context_window = _resolve_context_window(self.model)
            self.provider = self._provider_for(model)
            self.provider_name = model_registry.provider_for_model(model)
        try:
            tower_settings.set_channel_model(channel, model)
        except RuntimeError:
            log.warning(
                f"Channel {channel} model changed but not persisted — MongoDB not configured"
            )
        log.info(
            f"Channel {channel} model set to {model} "
            f"(provider={model_registry.provider_for_model(model)})"
        )

    def set_headroom_enabled(self, enabled: bool) -> None:
        """Enable/disable in-process Headroom compression and persist in MongoDB.

        Takes effect on the next API call in the agent loop (including mid-cascade).
        """
        self.headroom_enabled = bool(enabled)
        try:
            tower_settings.set_headroom_enabled(self.headroom_enabled)
        except RuntimeError:
            log.warning("Headroom toggle changed but not persisted — MongoDB not configured")
        log.info(f"Headroom compression {'ENABLED' if self.headroom_enabled else 'DISABLED'}")

    def _log_usage(self, response, channel_id: str, headroom_metrics: dict | None = None):
        """Log token usage fields so caching behavior is observable, and record
        the call's cost (tagged by channel) for the Tower cost dashboard.

        Healthy output on a warm cache:
            cache_read >> input_tokens,  cache_write small or 0.
        Cold/miss:
            cache_read = 0, cache_write ≈ prefix size.
        """
        usage = response.usage
        try:
            inp = usage.input_tokens
            cr = getattr(usage, 'cache_read_input_tokens', 0)
            cw = getattr(usage, 'cache_creation_input_tokens', 0)
            out = usage.output_tokens
            log.info(
                f"Tokens | input={inp} cache_read={cr} cache_write={cw} output={out}"
            )
            self.last_usage = {"input": inp, "cache_read": cr, "cache_write": cw, "output": out}
            channel_model = self.model_for_channel(channel_id)
            hr = headroom_metrics or {}
            cost_tracker.log_call(
                channel_id, "agent",
                model_registry.provider_for_model(channel_model),
                channel_model, self.last_usage,
                headroom_enabled=bool(hr.get("enabled", False)),
                headroom_tokens_before=int(hr.get("tokens_before", 0)),
                headroom_tokens_after=int(hr.get("tokens_after", 0)),
                headroom_tokens_saved=int(hr.get("tokens_saved", 0)),
            )
        except Exception:
            log.debug("Could not log usage fields", exc_info=True)

    def _record_input_tokens(self, response, channel_id: str):
        """Store the actual input context size the model processed this turn, so
        the next respond() can decide whether to auto-compact this channel."""
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        tokens = (
            (getattr(usage, "input_tokens", 0) or 0)
            + (getattr(usage, "cache_read_input_tokens", 0) or 0)
            + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
        )
        if tokens > 0:
            self._last_input_tokens[channel_id] = tokens

    async def compact_channel(self, channel_id: str = "default") -> dict:
        """Snapshot-compact a channel.

        Archives the full conversation to the palace (durable write + synchronous
        mine — archival is mandatory and must finish before the next task, which
        may recall from the palace, runs), generates a cumulative structured
        snapshot that folds in any prior snapshot, stores it (re-injected as a
        system block by respond() until the next compaction), and clears the
        message list.

        Returns the compaction stats dict (with "compacted": bool).
        """
        messages = self.conversations.get(channel_id)
        if not messages:
            return {"compacted": False, "messages_before": 0}

        snapshot_msgs = list(messages)  # defensive copy before reset

        # 1. Archive — durable write now, then mine synchronously. The mine must
        #    complete before this returns: the next task may recall from the
        #    palace, and mining can take a while, so we cannot fire-and-forget.
        try:
            from . import palace
            batch_dir = palace.archive_conversation_durable(
                channel_id, snapshot_msgs, kind="compact",
            )
            if batch_dir is not None:
                await palace.mine_batch_dir(batch_dir, agent="compaction")
        except Exception as e:
            log.warning(f"Compaction archive failed (channel={channel_id}): {e}")

        # 2. Snapshot — fold in any prior snapshot for this channel.
        from .compaction import compact_to_snapshot
        prior = self._compaction_summary.get(channel_id, "")
        result = await compact_to_snapshot(snapshot_msgs, prior_snapshot=prior, channel_id=channel_id)
        self._compaction_summary[channel_id] = result["snapshot"]

        # 3. Reset history. The snapshot (injected as a system block) carries
        #    everything prior; new turns accumulate fresh after it.
        messages.clear()
        self._last_input_tokens.pop(channel_id, None)
        # Full conversation was just archived; checkpoint baseline restarts at 0.
        self._last_archived_len[channel_id] = len(messages)

        log.info(
            f"Compacted channel {channel_id}: {result['messages_before']} msgs → "
            f"snapshot (~{result['tokens_before']} → ~{result['tokens_after']} tok est)"
        )
        result["compacted"] = True
        return result

    async def _compact_midloop(self, channel_id: str, user_message) -> None:
        """Compact a channel mid-cascade, preserving the running agentic loop.

        Unlike compact_channel (which stages a system-block snapshot for the next
        turn), this rebuilds the live message list so the loop can continue right
        now, with the snapshot AFTER the task:

            user(task) → assistant(compacted progress) → user(resume nudge)

        The task is the current turn's user_message; everything else (prior
        history + this turn's tool scaffolding) collapses into the snapshot. Any
        pending system-block snapshot is folded in and then cleared (progress now
        lives as a message, not a system block).
        """
        messages = self.conversations.get(channel_id)
        if not messages:
            return

        snapshot_msgs = list(messages)  # defensive copy before reset

        # Archive — durable write now, then mine synchronously. The mine must
        # complete before the loop resumes: the continuation may recall from the
        # palace, and mining can take a while, so we cannot fire-and-forget.
        try:
            from . import palace
            batch_dir = palace.archive_conversation_durable(
                channel_id, snapshot_msgs, kind="compact",
            )
            if batch_dir is not None:
                await palace.mine_batch_dir(batch_dir, agent="compaction")
        except Exception as e:
            log.warning(f"Mid-loop compaction archive failed (channel={channel_id}): {e}")

        # Snapshot — fold in any prior system-block snapshot, then drop it.
        from .compaction import compact_to_snapshot
        prior = self._compaction_summary.get(channel_id, "")
        result = await compact_to_snapshot(snapshot_msgs, prior_snapshot=prior, channel_id=channel_id)
        self._compaction_summary.pop(channel_id, None)

        # Rebuild the live conversation: task → progress → resume nudge.
        messages.clear()
        messages.append({"role": "user", "content": user_message})
        messages.append({
            "role": "assistant",
            "content": (
                "[Compacted progress — earlier tool calls and results were "
                "summarized to fit the context window; full detail archived to "
                "the memory palace, recall via palace_search]\n\n"
                f"{result['snapshot']}"
            ),
        })
        messages.append({
            "role": "user",
            "content": "Continue toward the goal using the compacted progress above.",
        })
        self._last_input_tokens.pop(channel_id, None)
        # Full conversation was just archived; the 3 rebuilt msgs are synthetic
        # (task + derived summary), so baseline the checkpoint past them.
        self._last_archived_len[channel_id] = len(messages)
        log.info(
            f"Mid-loop compacted channel {channel_id}: {result['messages_before']} msgs "
            f"→ task+progress+resume (~{result['tokens_before']} → ~{result['tokens_after']} tok)"
        )

    async def checkpoint_channel(self, channel_id: str = "default") -> int:
        """Non-destructively archive + mine a channel's NEW messages to the palace.

        Mines only the slice since the last checkpoint (or compaction), so
        repeated checkpoints never create duplicate drawers. The in-memory buffer
        is left intact — compaction is the only thing that trims/clears it.
        Blocking/awaitable. Returns the number of messages newly mined (0 if none).
        """
        messages = self.conversations.get(channel_id)
        if not messages:
            return 0
        start = self._last_archived_len.get(channel_id, 0)
        if start >= len(messages):
            return 0  # nothing new since the last checkpoint

        new_slice = list(messages[start:])
        try:
            from . import palace
            batch_dir = palace.archive_conversation_durable(
                channel_id, new_slice, kind="checkpoint",
            )
            if batch_dir is None:
                return 0
            await palace.mine_batch_dir(batch_dir, agent="checkpoint")
        except Exception as e:
            log.warning(f"Checkpoint failed (channel={channel_id}): {e}")
            return 0

        self._last_archived_len[channel_id] = len(messages)
        log.info(f"Checkpoint channel {channel_id}: mined {len(new_slice)} new msg(s)")
        return len(new_slice)

    def _assemble_system_blocks(self, channel_id: str) -> list:
        """Build the system blocks for an API call: cached stable + dynamic, then
        the compaction snapshot (if any) and the post-recovery advisory.

        MUST be re-called after any mid-loop / max_tokens compaction, because
        those change `_compaction_summary` / `_post_recovery_archive_tag` and the
        blocks built before the loop would otherwise be stale (snapshot missing
        or duplicated).
        """
        system_blocks = self.memory.build_system_blocks()

        # Extra per-channel context (e.g. a Slack team roster) — set via
        # set_channel_context(), re-injected on every turn until changed.
        channel_context = self._channel_context.get(channel_id)
        if channel_context:
            system_blocks.append({"type": "text", "text": channel_context})

        # Compacted-conversation snapshot — its own non-cached block right after
        # the dynamic block, so chronology is stable → dynamic → snapshot → user.
        summary = self._compaction_summary.get(channel_id)
        if summary:
            system_blocks.append({
                "type": "text",
                "text": (
                    "# Compacted Conversation Snapshot\n\n"
                    "The earlier conversation in this channel was compacted to stay "
                    "within the context window. The full history was archived to the "
                    "memory palace (recall verbatim via `palace_search`). Treat this "
                    "snapshot as the record of everything that happened before the "
                    f"user message that follows:\n\n{summary}"
                ),
            })

        # Post-recovery advisory — set before a hard-reset fallback so the model
        # knows the dropped exchange is recallable via palace_search.
        recovery_tag = self._post_recovery_archive_tag.get(channel_id)
        if recovery_tag:
            system_blocks.append({
                "type": "text",
                "text": (
                    f"[SYSTEM:POST-RECOVERY-ADVISORY] An earlier max_tokens "
                    f"cascade in this channel trimmed/reset the conversation. "
                    f"The pre-incident exchange was archived to the palace. "
                    f"If the user references earlier content you cannot see, "
                    f"recall it with `palace_search` — the archive is filed "
                    f"under channel tag `{recovery_tag}`."
                ),
            })
        return system_blocks

    async def respond(
        self,
        user_message: str | list,
        channel_id: str = "default",
        emit=None,
        overlay_context: str | None = None,
    ) -> str:
        """Run the agentic loop and return the final assistant text.

        `emit`, if given, is an async callback `emit(event: dict)` used to
        stream progress to a live UI (Tower). Event shapes:
          {"type": "text"|"thought", "text": <delta>}  — model output deltas
          {"type": "tool_call", "name": <str>, "input": <str>}  — before a tool runs
          {"type": "tool_result", "name": <str>, "output": <str>}  — truncated result
        When `emit` is None the loop is identical to the non-streaming path,
        so Discord and the scheduler are unaffected.

        Serialized per channel_id (see `_lock_for`) so two turns on the same
        channel (e.g. two gateways sharing MAIN_CHANNEL_ID) never interleave.
        """
        async with self._lock_for(channel_id):
            return await self._respond_locked(
                user_message, channel_id, emit, overlay_context,
            )

    @staticmethod
    def _with_overlay(system_blocks: list, overlay_context: str | None) -> list:
        if not overlay_context:
            return system_blocks
        blocks = list(system_blocks)
        blocks.append({"type": "text", "text": overlay_context})
        return blocks

    async def _respond_locked(
        self,
        user_message: str | list,
        channel_id: str,
        emit,
        overlay_context: str | None = None,
    ) -> str:
        cancel_ev = asyncio.Event()
        self._turn_cancel[channel_id] = cancel_ev
        pending_holder = {"blocks": None, "results": []}

        try:
            return await self._respond_locked_inner(
                user_message,
                channel_id,
                emit,
                overlay_context,
                pending_holder,
            )
        except TurnCancelled:
            return await self._finalize_cancelled_turn(
                channel_id,
                self._get_messages(channel_id),
                emit,
                tool_blocks=pending_holder["blocks"],
                tool_results=pending_holder["results"],
            )
        finally:
            self._turn_cancel.pop(channel_id, None)

    async def _respond_locked_inner(
        self,
        user_message: str | list,
        channel_id: str,
        emit,
        overlay_context: str | None = None,
        pending_tool_results_holder: dict | None = None,
    ) -> str:
        messages = self._get_messages(channel_id)

        # Auto-compaction: if the last measured input context for this channel
        # crossed the threshold, snapshot+archive the whole conversation. This
        # clears the message list; the snapshot is injected as its own system
        # block (see _assemble_system_blocks). Resilient — a compaction failure
        # must not crash the turn; we just proceed with the full context.
        if messages and self._last_input_tokens.get(channel_id, 0) > self.compact_threshold:
            try:
                await self.compact_channel(channel_id)
            except Exception as e:
                log.warning(f"Pre-turn compaction failed ({e}); proceeding with full context")

        # user_message is appended untouched, so the daily log records the real
        # message exactly once — compaction never double-logs. Context size is
        # managed solely by compaction (no routine message-count trim).
        messages.append({"role": "user", "content": user_message})

        # System blocks: stable + dynamic + snapshot + advisory. Rebuilt after
        # any mid-loop / max_tokens compaction so it never goes stale.
        # overlay_context is ephemeral (Tower page pointers) — not stored in history.
        system_blocks = self._with_overlay(
            self._assemble_system_blocks(channel_id), overlay_context,
        )

        max_tokens_retries = 0  # Track consecutive max_tokens hits
        turn_thought = ""  # Accumulated thought deltas for the current API response
        # Turn-local API message list. When Headroom is ON we accumulate the
        # *compressed* bytes already sent so the provider prefix stays
        # byte-identical across the tool cascade. Stored history (`messages`)
        # stays original. Reset each turn / after compaction rebuilds.
        api_messages: list | None = None

        while True:
            self._check_cancelled(channel_id)
            if pending_tool_results_holder is not None:
                pending_tool_results_holder["blocks"] = None
                pending_tool_results_holder["results"] = []

            # Guard against empty message list
            if not messages:
                log.error("Message list is empty — cannot call API. Seeding with user message.")
                messages.append({"role": "user", "content": user_message})

            log.info(f"API call with {len(messages)} messages, last role: {messages[-1]['role']}")
            channel_model = self.model_for_channel(channel_id)
            provider = self._provider_for(channel_model)

            # API-bound copy only (never mutate self.conversations).
            # Order: screenshot prune → headroom → cache_control.
            headroom_metrics = {
                "enabled": False,
                "tokens_before": 0,
                "tokens_after": 0,
                "tokens_saved": 0,
                "images_kept": 0,
                "images_pruned": 0,
            }
            if self.headroom_enabled:
                frozen = len(api_messages) if api_messages is not None else 0
                if api_messages is None:
                    to_compress = messages
                else:
                    # Reuse compressed prefix; only compress newly appended msgs.
                    to_compress = list(api_messages) + messages[frozen:]
                compressed, hr = await headroom_compress.compress_for_api(
                    to_compress,
                    model=channel_model,
                    frozen_message_count=frozen,
                    model_limit=self.context_window,
                )
                api_messages = list(compressed)
                messages_for_api = api_messages
                headroom_metrics = {
                    "enabled": True,
                    "tokens_before": hr.tokens_before,
                    "tokens_after": hr.tokens_after,
                    "tokens_saved": hr.tokens_saved,
                    "images_kept": hr.images_kept,
                    "images_pruned": hr.images_pruned,
                }
                if hr.tokens_saved > 0 or hr.images_pruned > 0:
                    log.info(
                        f"Headroom | before={hr.tokens_before} after={hr.tokens_after} "
                        f"saved={hr.tokens_saved} frozen={frozen} "
                        f"images_kept={hr.images_kept} images_pruned={hr.images_pruned}"
                    )
            else:
                # Still prune old screenshots so Headroom-off browser sessions
                # do not send every historical base64 image to the provider.
                api_messages = None
                messages_for_api, prune_stats = headroom_compress.prepare_messages_for_api(
                    messages
                )
                headroom_metrics["images_kept"] = prune_stats.images_kept
                headroom_metrics["images_pruned"] = prune_stats.images_pruned

            # Attach cache_control to the last block of the last message.
            # This advances the messages-cache breakpoint as the conversation
            # grows, giving hits within tool_use cascades.
            messages_for_api = _attach_trailing_cache_control(messages_for_api)
            turn_thought = ""
            if emit is not None:
                response = None
                async for kind, payload in provider.stream_message(
                    model=channel_model,
                    max_tokens=self.max_tokens,
                    system=system_blocks,
                    tools=self.tools,
                    messages=messages_for_api,
                ):
                    self._check_cancelled(channel_id)
                    if kind == "message":
                        response = payload
                    elif kind == "thought":
                        turn_thought += payload
                        await emit({"type": kind, "text": payload})
                    else:  # "text"
                        await emit({"type": kind, "text": payload})
            else:
                response = await provider.create_message(
                    model=channel_model,
                    max_tokens=self.max_tokens,
                    system=system_blocks,
                    tools=self.tools,
                    messages=messages_for_api,
                )

            self._check_cancelled(channel_id)

            self._log_usage(response, channel_id, headroom_metrics=headroom_metrics)
            self._record_input_tokens(response, channel_id)
            await self._maybe_warn_context(response, channel_id)
            await self._maybe_warn_output_ceiling(response, channel_id)

            # Extract tool IDs from response BEFORE serialization
            tool_ids_from_response = set()
            if response.stop_reason == "tool_use":
                for block in response.content:
                    if hasattr(block, "type") and block.type == "tool_use":
                        tool_ids_from_response.add(block.id)

            # Now serialize for storage
            assistant_content = _serialize_content(response.content)
            assistant_msg: dict = {"role": "assistant", "content": assistant_content}
            if turn_thought.strip():
                assistant_msg["_thought"] = turn_thought.strip()
            messages.append(assistant_msg)
            log.info(f"Response stop_reason: {response.stop_reason}")
            self._check_cancelled(channel_id)

            if response.stop_reason == "end_turn":
                max_tokens_retries = 0  # Reset counter on success
                text_parts = [
                    block["text"]
                    for block in (assistant_content if isinstance(assistant_content, list) else [])
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                # Empty text is a legitimate state — Claude may end_turn with
                # nothing to add after a tool-use cascade. Return empty string
                # and let callers decide how to surface it. Previously returned
                # the literal "(no response)" which got piped verbatim to
                # Discord and confused the user.
                final_text = "\n".join(text_parts).strip() if text_parts else ""
                user_summary = user_message[:100] if isinstance(user_message, str) else "[multimodal message]"
                self.memory.append_daily_log(
                    f"[chat:{channel_id}] User: {user_summary}..."
                )
                conversation_store.save_channel(self.working_dir, channel_id, messages)
                return final_text

            if response.stop_reason == "max_tokens":
                max_tokens_retries += 1

                # Remove the incomplete assistant message
                del messages[-1]

                # Extract any text from the truncated response to return
                # if we're about to give up.
                truncated_text_parts = [
                    block.text
                    for block in response.content
                    if hasattr(block, "type") and block.type == "text" and hasattr(block, "text")
                ]
                truncated_text = "\n".join(truncated_text_parts).strip() if truncated_text_parts else ""

                log.warning(
                    f"Hit max_tokens mid-response (attempt {max_tokens_retries}/3), "
                    f"conversation has {len(messages)} messages"
                )

                if max_tokens_retries >= 3:
                    # Tried 3 times — give up gracefully. Archive + hard reset so
                    # the next message works.
                    self._archive_and_flag(channel_id, messages)
                    self._hard_reset(messages, user_message)
                    suffix = (
                        "\n\n*(My response was too long and I could not recover after multiple attempts. "
                        "The conversation has been reset — but your prior exchange was preserved "
                        "in my memory palace. Ask me to recall it any time and I'll `palace_search` "
                        "for the thread.)*"
                    )
                    if truncated_text:
                        return truncated_text + suffix
                    return (
                        "(Response exceeded token limit repeatedly. Conversation reset. "
                        "Prior exchange preserved in my memory palace — ask and I'll recall it.)"
                    )

                # Recover by compacting (snapshot) instead of blind trimming —
                # compact_channel archives the full state and replaces it with a
                # snapshot system block. If compaction can't help or fails, fall
                # back to archive + hard reset (the only remaining last resort).
                try:
                    result = await self.compact_channel(channel_id)
                    if not result.get("compacted"):
                        self._archive_and_flag(channel_id, messages)
                        self._hard_reset(messages, user_message)
                except Exception as e:
                    log.warning(f"max_tokens recovery: compaction failed ({e}); hard reset")
                    self._archive_and_flag(channel_id, messages)
                    self._hard_reset(messages, user_message)

                # Rebuild system blocks: compaction set a fresh snapshot, or the
                # hard-reset fallback set a post-recovery advisory — either way the
                # pre-loop blocks are now stale and must be regenerated.
                system_blocks = self._with_overlay(
                    self._assemble_system_blocks(channel_id), overlay_context,
                )

                # Ensure we end with a user message for the API.
                if not messages or messages[-1].get("role") != "user":
                    messages.append({"role": "user", "content": user_message})

                # Buffer was rebuilt by compaction/reset — drop compressed prefix.
                api_messages = None
                continue

            if response.stop_reason == "tool_use":
                max_tokens_retries = 0  # Reset counter on successful tool use
                tool_results = []
                tool_blocks = [
                    block for block in response.content
                    if hasattr(block, "type") and block.type == "tool_use"
                ]
                if pending_tool_results_holder is not None:
                    pending_tool_results_holder["blocks"] = tool_blocks
                # Use the original response.content blocks to extract tool IDs
                for block in tool_blocks:
                    self._check_cancelled(channel_id)

                    tool_name = block.name
                    tool_input = block.input if isinstance(block.input, dict) else {}
                    tool_id = block.id

                    if emit is not None:
                        await emit({
                            "type": "tool_call",
                            "name": tool_name,
                            "input": _summarize_tool_input(tool_input),
                        })

                    if tool_name == "run_shell":
                        command = tool_input.get("command", "")
                        shell_dir = tool_input.get("working_dir") or self.working_dir
                        tier = classify_command(command, self._created_files, shell_dir)
                        log.info(format_safety_notice(command, tier))

                        if tier == "red":
                            blocked = None
                            if self.approval_callback:
                                approved = await self.approval_callback(command, tier)
                                if not approved:
                                    blocked = f"[BLOCKED] Denied: {command}"
                            else:
                                blocked = f"[BLOCKED] Red-tier, no approval callback: {command}"
                            if blocked is not None:
                                tool_results.append({
                                    "type": "tool_result",
                                    "tool_use_id": tool_id,
                                    "content": blocked,
                                })
                                if pending_tool_results_holder is not None:
                                    pending_tool_results_holder["results"] = tool_results
                                if emit is not None:
                                    await emit({
                                        "type": "tool_result",
                                        "name": tool_name,
                                        "output": blocked,
                                    })
                                continue

                    is_new_file = (
                        tool_name == "write_file"
                        and bool(tool_input.get("path"))
                        and not os.path.exists(os.path.join(self.working_dir, tool_input.get("path", "")))
                    )

                    # execute_tool never raises — missing args / tool bugs come
                    # back as "[tool error] …" so the model can correct + retry.
                    result = await execute_tool(
                        tool_name, tool_input,
                        memory_manager=self.memory,
                        working_dir=self.working_dir,
                    )

                    if (
                        is_new_file
                        and isinstance(result, str)
                        and result.startswith("Written ")
                    ):
                        resolved = os.path.normpath(os.path.join(self.working_dir, tool_input["path"]))
                        self._created_files.add(resolved)
                    elif tool_name == "run_shell" and tier == "green":
                        rm_target = _simple_rm_target(command)
                        if rm_target is not None:
                            # A successful auto-approved self-cleanup: the file is
                            # gone, so drop it from the tracked set.
                            self._created_files.discard(os.path.normpath(os.path.join(shell_dir, rm_target)))

                    # A tool may return a plain string OR a list of content
                    # blocks (text + image — e.g. browser screenshots become
                    # vision input). Truncate text; images pass through whole.
                    if isinstance(result, str):
                        if len(result) > 15000:
                            result = result[:15000] + "\n...[truncated]"
                        result_display = result
                    else:
                        result = [
                            {**b, "text": b["text"][:15000] + "\n...[truncated]"}
                            if b.get("type") == "text" and len(b.get("text", "")) > 15000
                            else b
                            for b in result
                        ]
                        texts = [b.get("text", "") for b in result if b.get("type") == "text"]
                        n_images = sum(1 for b in result if b.get("type") == "image")
                        result_display = "\n".join(t for t in texts if t)
                        if n_images:
                            result_display += f"\n[{n_images} image(s) attached — sent to the model as vision input]"

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": result,
                    })
                    if pending_tool_results_holder is not None:
                        pending_tool_results_holder["results"] = tool_results

                    if emit is not None:
                        await emit({
                            "type": "tool_result",
                            "name": tool_name,
                            "output": result_display,
                        })

                messages.append({"role": "user", "content": tool_results})

                # Mid-loop auto-compaction: if the input context measured during
                # this cascade already crossed the threshold, compact in place so
                # the loop continues within budget. Shape: keep the task as the
                # user turn, the snapshot as an assistant "progress" message, and
                # a short user nudge to resume — see _compact_midloop. Resilient:
                # a failure leaves the buffer intact and the loop proceeds (a
                # subsequent max_tokens hit has its own recovery).
                if self._last_input_tokens.get(channel_id, 0) > self.compact_threshold:
                    try:
                        await self._compact_midloop(channel_id, user_message)
                        # Snapshot now lives in messages and _compaction_summary was
                        # cleared — rebuild so any stale snapshot block is dropped.
                        system_blocks = self._with_overlay(
                            self._assemble_system_blocks(channel_id), overlay_context,
                        )
                        # Buffer was rebuilt — drop compressed API prefix.
                        api_messages = None
                    except Exception as e:
                        log.warning(f"Mid-loop compaction failed ({e}); proceeding with full context")

                # Loop back to send tool results to the API
                continue


    def clear_history(self, channel_id: str = "default"):
        self.conversations.pop(channel_id, None)
        conversation_store.delete_channel(self.working_dir, channel_id)
        # A fresh channel starts with no recovery advisory — clear stale state.
        self._post_recovery_archive_tag.pop(channel_id, None)
        self._output_ceiling_streak.pop(channel_id, None)
        self._compaction_summary.pop(channel_id, None)
        self._last_input_tokens.pop(channel_id, None)
        self._last_archived_len.pop(channel_id, None)

    async def pop_and_archive_history(self, channel_id: str = "default") -> int:
        """Archive the channel's conversation to the palace, then clear it.

        Used by Discord `/new` / `!new` / `!clear`. Returns the number of
        messages archived (0 if the channel was already empty). Archive is
        awaited — by the time this returns, the palace mine has either
        succeeded or logged a failure. Callers in an async context can
        safely use this before responding to the user.

        Silent fallback if mempalace isn't installed: history is still
        cleared, just not archived.
        """
        messages = self.conversations.pop(channel_id, None)
        conversation_store.delete_channel(self.working_dir, channel_id)
        # Clear per-channel transient state alongside the history.
        self._post_recovery_archive_tag.pop(channel_id, None)
        self._output_ceiling_streak.pop(channel_id, None)
        self._compaction_summary.pop(channel_id, None)
        self._last_input_tokens.pop(channel_id, None)
        self._last_archived_len.pop(channel_id, None)
        if not messages:
            return 0
        try:
            from . import palace
            await palace.archive_conversation(channel_id, messages)
        except Exception as e:
            log.warning(f"Conversation archive failed on /new: {e}")
        return len(messages)

    def archive_conversations_on_shutdown(self) -> int:
        """Persist every non-empty channel to disk before the process exits.

        Synchronous and idempotent — safe to call from a signal handler / atexit.
        Writes raw .md only (no palace mining); the staged archives are mined on
        the next startup. Returns the number of channels written.
        """
        if self._shutdown_archived:
            return 0
        self._shutdown_archived = True
        try:
            from . import palace
        except Exception:
            return 0
        conversation_store.save_all(self.working_dir, self.conversations)
        written = 0
        for channel_id, messages in list(self.conversations.items()):
            if messages and palace.write_conversation_archive_sync(channel_id, messages):
                written += 1
        if written:
            log.info(
                f"Shutdown: staged {written} conversation(s) for palace archival "
                "(background mine on next start)."
            )
        # Flush + close MemPalace ChromaDB handles so in-process vector writes
        # (e.g. diary_write) persist to HNSW; otherwise the next start
        # quarantines the segment as drift and silently loses those drawers.
        try:
            palace.close()
        except Exception as e:
            log.warning(f"Shutdown: palace close failed ({e})")
        return written

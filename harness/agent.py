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
import copy
import os
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from .memory import MemoryManager
from .experiential_state import ExperienceManager
from .consequence_appraiser import (
    EpisodeAccumulator,
    appraise,
    appraisal_signals,
)
from .tools import TOOL_DEFINITIONS, execute_tool
from .tool_access import (
    UNTRUSTED_READ_ONLY_TOOLS,
    is_untrusted_organization_slack,
    tools_for_request,
)
from .tool_outcomes import tool_result_failed
from .safety import (
    classify_command, format_safety_notice, is_demonstrably_read_only,
    _simple_rm_target,
)
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
# Product names: Chat / Worker / Ambient. Durable storage IDs stay stable.
MAIN_CHANNEL_ID = "main"          # product: Chat
CHAT_CHANNEL_ID = MAIN_CHANNEL_ID
WORKER_CHANNEL_ID = "worker"      # product: Worker
AMBIENT_CHANNEL_ID = "reflection" # product: Ambient

# Autonomous loops that get a durable per-turn tick audit (same store as worker).
# Worker still builds its own recorder; these channels auto-record inside respond().
LOOP_TICK_CHANNELS = frozenset({
    "wake", "heartbeat", "morning", AMBIENT_CHANNEL_ID, "goodnight", "completions",
})

_STOPPED_ASSISTANT_NOTE = "(Stopped — turn cancelled.)"

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
        # Provider clients are resolved lazily on the first turn. Managed
        # Replikas can therefore boot their settings UI before a BYOM key exists.
        self.provider = provider
        # Best-effort provider id for cost logging (matches model_registry's
        # ANTHROPIC/GEMINI/OLLAMA strings even when a custom `provider` is injected).
        self.provider_name = model_registry.provider_for_model(self.model)
        # In-process Headroom compression (Tower toggle). Default off.
        self.headroom_enabled = tower_settings.get_headroom_enabled()
        self.max_tokens = max_tokens or int(os.environ.get("AGENT_MAX_TOKENS", "8192"))
        self.memory = MemoryManager(config_dir=config_dir, memory_dir=memory_dir)
        self.working_dir = working_dir or os.getcwd()
        # One experiential state belongs to the agent, not to any transport
        # stream. Channel IDs tag events/perspectives; they never fork identity.
        experiential_enabled = tower_settings.get_experiential_enabled()
        self.experience = ExperienceManager(
            self.working_dir,
            mode="influence" if experiential_enabled else "off",
        )
        self.conversations: dict[str, list] = {}
        restored = conversation_store.load_all(self.working_dir)
        if restored:
            self.conversations.update(restored)
            total = sum(len(m) for m in restored.values())
            log.info(
                f"Restored {len(restored)} conversation buffer(s) "
                f"({total} message(s)) from disk."
            )
        self._pending_run_recovery = None
        # Mongo is the canonical active user-run record when configured. The
        # actual state is applied after per-channel compaction fields initialize.
        try:
            from .conversation_run_store import recovery_state
            _run, tail, checkpoint = recovery_state(MAIN_CHANNEL_ID)
            if _run is not None:
                self._pending_run_recovery = (_run, tail, checkpoint)
        except Exception as e:
            log.warning("Mongo conversation recovery unavailable; using local buffer (%s)", e)
        # One lock per channel_id, created lazily. Serializes respond() calls on
        # the SAME channel so two near-simultaneous turns (e.g. from different
        # gateways sharing MAIN_CHANNEL_ID) can't interleave mid-turn and break
        # the API's strict user/assistant/tool_result alternation.
        self._channel_locks: dict[str, asyncio.Lock] = {}
        # Per-channel cancel events for in-flight turns (Tower stop button).
        self._turn_cancel: dict[str, asyncio.Event] = {}
        self._active_experience_episodes: dict[str, EpisodeAccumulator] = {}
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
        if self._pending_run_recovery is not None:
            run, tail, checkpoint = self._pending_run_recovery
            self.conversations[MAIN_CHANNEL_ID] = list(tail)
            if checkpoint and checkpoint.get("summary"):
                self._compaction_summary[MAIN_CHANNEL_ID] = checkpoint["summary"]
            log.info(
                "Restored active conversation run %s (%d protocol events after checkpoint).",
                run["run_id"], len(tail),
            )

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

        from .conversation_queue import ConversationQueue
        self.conversation_queue = ConversationQueue(
            self.respond,
            stop_callback=self._request_turn_cancel,
        )

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
        return self.conversation_queue.stop(channel_id)

    def _request_turn_cancel(self, channel_id: str) -> bool:
        """Low-level cooperative turn signal used by the inbox without recursion."""
        ev = self._turn_cancel.get(channel_id)
        if ev is not None and not ev.is_set():
            ev.set()
            log.info(f"Stop requested for channel {channel_id}")
            return True
        return False

    def is_channel_busy(self, channel_id: str) -> bool:
        if self.conversation_queue.status(channel_id)["busy"]:
            return True
        ev = self._turn_cancel.get(channel_id)
        return ev is not None and not ev.is_set()

    async def enqueue(
        self,
        payload,
        *,
        channel_id: str = MAIN_CHANNEL_ID,
        source: str,
        external_dedupe_key: str | None = None,
        sender=None,
        display_text: str = "",
        reply_target=None,
        overlay_context: str | None = None,
        request_context: dict | None = None,
    ) -> dict:
        """Persist a human-facing message and wake the channel consumer."""
        return await self.conversation_queue.enqueue(
            payload,
            channel=channel_id,
            source=source,
            external_dedupe_key=external_dedupe_key,
            sender=sender,
            display_text=display_text,
            reply_target=reply_target,
            overlay=overlay_context,
            request_context=request_context,
        )

    async def await_enqueued(self, item_id: str) -> str:
        """Wait for the turn containing an inbox item to finish."""
        return await self.conversation_queue.await_item(item_id)

    async def enqueue_and_await(self, payload, **kwargs) -> str:
        item = await self.enqueue(payload, **kwargs)
        return await self.await_enqueued(item["id"])

    def _check_cancelled(self, channel_id: str) -> None:
        ev = self._turn_cancel.get(channel_id)
        if ev is not None and ev.is_set():
            raise TurnCancelled()

    async def _finalize_cancelled_turn(
        self,
        channel_id: str,
        messages: list,
        pre_turn_messages: list,
        emit,
        *,
        recorder=None,
    ) -> str:
        """Rollback a cancelled turn and persist its durable cancelled audit state."""
        messages[:] = pre_turn_messages
        conversation_store.save_channel(self.working_dir, channel_id, messages)
        if recorder is not None:
            await recorder.finalize_turn(state="cancelled")
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
        """Return the model used for API calls on this channel.

        Per-loop / per-channel overrides win; everything else falls back to main.
        """
        if channel_id in self._channel_models:
            return self._channel_models[channel_id]
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

    def set_experiential_enabled(self, enabled: bool) -> None:
        """Enable/disable appraisal and causal prompt influence at runtime."""
        self.experience.set_mode("influence" if enabled else "off")
        try:
            tower_settings.set_experiential_enabled(bool(enabled))
        except RuntimeError:
            log.warning(
                "Experiential-state toggle changed but not persisted — "
                "MongoDB not configured"
            )
        log.info(
            "Experiential state %s",
            "ENABLED" if enabled else "DISABLED",
        )

    def _log_usage(
        self,
        response,
        channel_id: str,
        headroom_metrics: dict | None = None,
        *,
        tick_id: str | None = None,
        run_id: str | None = None,
        turn_id: str | None = None,
        event_sequence: int | None = None,
        call_index: int | None = None,
        duration_ms: int | None = None,
    ) -> dict:
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
                tick_id=tick_id,
                run_id=run_id,
                turn_id=turn_id,
                event_sequence=event_sequence,
                call_index=call_index,
                duration_ms=duration_ms,
                stop_reason=getattr(response, "stop_reason", None),
            )
            return dict(self.last_usage)
        except Exception:
            log.debug("Could not log usage fields", exc_info=True)
            return {}

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
        run_recorder = None
        if channel_id == MAIN_CHANNEL_ID:
            from .conversation_run_store import ConversationRunRecorder
            run_recorder = await ConversationRunRecorder.for_active(channel_id, source="compaction")

        snapshot_msgs = list(messages)  # defensive copy before reset

        # 1. Archive — durable write now, then mine synchronously. The mine must
        #    complete before this returns: the next task may recall from the
        #    palace, and mining can take a while, so we cannot fire-and-forget.
        try:
            if run_recorder is not None:
                from .memory_sync import stage_and_mine_main
                await stage_and_mine_main(
                    run_recorder.run_id, snapshot_msgs, kind="compact", agent="compaction",
                )
            else:
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
        result = await compact_to_snapshot(
            snapshot_msgs,
            prior_snapshot=prior,
            channel_id=channel_id,
            run_id=getattr(run_recorder, "run_id", None),
        )
        self._compaction_summary[channel_id] = result["snapshot"]
        if run_recorder is not None:
            await run_recorder.record_checkpoint({
                "kind": "compact",
                "summary": result["snapshot"],
                "messages_before": result["messages_before"],
                "tokens_before": result["tokens_before"],
                "tokens_after": result["tokens_after"],
            })

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
        self._record_experience_event(
            "compaction",
            channel_id,
            details={"messages_before": result["messages_before"]},
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
        run_recorder = None
        if channel_id == MAIN_CHANNEL_ID:
            from .conversation_run_store import ConversationRunRecorder
            run_recorder = await ConversationRunRecorder.for_active(channel_id, source="compaction")

        snapshot_msgs = list(messages)  # defensive copy before reset

        # Archive — durable write now, then mine synchronously. The mine must
        # complete before the loop resumes: the continuation may recall from the
        # palace, and mining can take a while, so we cannot fire-and-forget.
        try:
            if run_recorder is not None:
                from .memory_sync import stage_and_mine_main
                await stage_and_mine_main(
                    run_recorder.run_id, snapshot_msgs, kind="compact", agent="compaction",
                )
            else:
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
        result = await compact_to_snapshot(
            snapshot_msgs,
            prior_snapshot=prior,
            channel_id=channel_id,
            run_id=getattr(run_recorder, "run_id", None),
        )
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
        if run_recorder is not None:
            await run_recorder.record_checkpoint({
                "kind": "midloop_compact",
                "summary": result["snapshot"],
                "messages_before": result["messages_before"],
                "tokens_before": result["tokens_before"],
                "tokens_after": result["tokens_after"],
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
            if channel_id == MAIN_CHANNEL_ID:
                from .conversation_run_store import ConversationRunRecorder
                from .memory_sync import stage_and_mine_main
                recorder = await ConversationRunRecorder.for_active(channel_id, source="checkpoint")
                if recorder is not None:
                    ok = await stage_and_mine_main(
                        recorder.run_id, new_slice, kind="checkpoint", agent="checkpoint",
                    )
                    if not ok:
                        return 0
                    await recorder.record_checkpoint({
                        "kind": "checkpoint", "messages_before": len(new_slice),
                    })
                else:
                    from . import palace
                    batch_dir = palace.archive_conversation_durable(
                        channel_id, new_slice, kind="checkpoint",
                    )
                    if batch_dir is None or not await palace.mine_batch_dir(batch_dir, agent="checkpoint"):
                        return 0
            else:
                from . import palace
                batch_dir = palace.archive_conversation_durable(
                    channel_id, new_slice, kind="checkpoint",
                )
                if batch_dir is None or not await palace.mine_batch_dir(batch_dir, agent="checkpoint"):
                    return 0
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

        experiential_workspace = self.experience.workspace_block(channel_id)
        if experiential_workspace:
            system_blocks.append({
                "type": "text",
                "text": experiential_workspace,
            })

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

    def _record_experience_event(
        self,
        kind: str,
        channel_id: str,
        *,
        signals: dict | None = None,
        details: dict | None = None,
        proposed_appraisal: dict | None = None,
        idempotency_key: str | None = None,
        basis_sequence: int | None = None,
    ) -> dict:
        """Best-effort event bridge; experiential storage never breaks a turn."""
        try:
            return self.experience.record_event(
                kind,
                channel_id,
                signals=signals,
                details=details,
                proposed_appraisal=proposed_appraisal,
                idempotency_key=idempotency_key,
                basis_sequence=basis_sequence,
            )
        except Exception as exc:
            log.warning("Experiential event was not recorded: %s", exc)
            try:
                return self.experience.snapshot()
            except Exception:
                return {
                    "version": 0,
                    "sequence": 0,
                    "dimensions": {},
                    "last_event": None,
                }

    async def _appraise_episode(
        self,
        episode: EpisodeAccumulator,
        phase: str,
        *,
        final_output: str = "",
    ) -> dict | None:
        """Classify one bounded episode phase; any failure is non-fatal."""
        if not self.experience.enabled:
            return None
        acting_model = self.model_for_channel(episode.channel)
        basis_snapshot = self.experience.snapshot()
        evidence = episode.envelope(
            phase=phase,
            final_output=final_output,
            elapsed=int(time.monotonic() - episode.started_at),
        )
        evidence["current_state"] = basis_snapshot.get("dimensions") or {}
        evidence["previous_outcome"] = (
            (basis_snapshot.get("last_event") or {}).get(
                "salient_change",
                "",
            )
        )
        try:
            classification = await appraise(
                self._provider_for(acting_model),
                acting_model=acting_model,
                envelope=evidence,
                usage_callback=lambda response, model: (
                    self._log_appraisal_usage(
                        response,
                        model,
                        episode.channel,
                        phase,
                    )
                ),
            )
        except Exception as exc:
            log.warning(
                "Experiential %s appraisal unavailable; continuing turn: %s",
                phase,
                exc,
            )
            return None
        if classification is None:
            log.warning(
                "Experiential %s appraisal returned invalid structure; "
                "continuing turn",
                phase,
            )
            return None
        basis = basis_snapshot.get("sequence", 0)
        self._record_experience_event(
            "episode_appraisal",
            episode.channel,
            signals=appraisal_signals(classification),
            details={
                "episode_id": episode.episode_id,
                "phase": phase,
                "classification": classification,
                "tool_summary": {
                    "attempted": episode.attempted,
                    "succeeded": episode.succeeded,
                    "failed": episode.failed,
                    "repeated_failures": episode.repeated_failures,
                    "permission_denials": episode.permission_denials,
                },
            },
            idempotency_key=f"{episode.episode_id}:appraisal:{phase}",
            basis_sequence=basis,
        )
        return classification

    @staticmethod
    def _log_appraisal_usage(
        response,
        model: str,
        channel_id: str,
        phase: str,
    ) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        values = {
            "input": int(getattr(usage, "input_tokens", 0) or 0),
            "cache_read": int(
                getattr(usage, "cache_read_input_tokens", 0) or 0
            ),
            "cache_write": int(
                getattr(usage, "cache_creation_input_tokens", 0) or 0
            ),
            "output": int(getattr(usage, "output_tokens", 0) or 0),
        }
        cost_tracker.log_call(
            channel_id,
            f"experiential_appraisal_{phase}",
            model_registry.provider_for_model(model),
            model,
            values,
        )

    def _observe_episode_tool(
        self,
        episode: EpisodeAccumulator,
        tool_name: str,
        *,
        failed: bool,
        reason: str = "",
    ) -> dict | None:
        """Aggregate a tool outcome and apply only novel acute failures."""
        exceptional = episode.observe_tool(
            tool_name,
            failed=failed,
            reason=reason,
        )
        if exceptional is None:
            return None
        if episode.channel != MAIN_CHANNEL_ID:
            self._record_experience_event(
                "turn_started",
                episode.channel,
                details={
                    "episode_id": episode.episode_id,
                    "deferred": True,
                },
                idempotency_key=f"{episode.episode_id}:turn_started",
            )
        return self._record_experience_event(
            "tool_failed",
            episode.channel,
            details={
                "episode_id": episode.episode_id,
                "tool": tool_name,
                "reason": reason or "failure",
                "exception": exceptional,
            },
            idempotency_key=(
                f"{episode.episode_id}:acute:{tool_name}:{exceptional}"
            ),
        )

    @staticmethod
    def _should_appraise_outcome(
        episode: EpisodeAccumulator,
        final_output: str,
    ) -> bool:
        if episode.channel == MAIN_CHANNEL_ID:
            return True
        if episode.channel == WORKER_CHANNEL_ID:
            return (
                "<<WORKER_STATUS: worked>>" in final_output
                or bool(episode.failed or episode.permission_denials)
            )
        if episode.channel == AMBIENT_CHANNEL_ID:
            return bool(
                episode.attempted
                or episode.important_events
                or final_output.strip()
            )
        # Heartbeats and other scheduled boilerplate are appraised only when
        # activity produced objective evidence beyond a routine check-in.
        return bool(episode.failed or episode.permission_denials)

    async def _audit_experience_snapshot(
        self,
        snapshot: dict,
        *recorders,
    ) -> None:
        if not snapshot:
            return
        for recorder in recorders:
            if recorder is None or not hasattr(recorder, "record_experiential_state"):
                continue
            try:
                await recorder.record_experiential_state(snapshot)
            except Exception as exc:
                log.warning("Experiential audit metadata was not recorded: %s", exc)

    async def respond(
        self,
        user_message: str | list,
        channel_id: str = "default",
        emit=None,
        overlay_context: str | None = None,
        tick_recorder=None,
        run_source: str | None = None,
        client_dedup_key: str | None = None,
        request_context: dict | None = None,
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
                user_message, channel_id, emit, overlay_context, tick_recorder,
                run_source, client_dedup_key, request_context,
            )

    @staticmethod
    def _with_overlay(system_blocks: list, overlay_context: str | None) -> list:
        if not overlay_context:
            return system_blocks
        blocks = list(system_blocks)
        blocks.append({"type": "text", "text": overlay_context})
        return blocks

    async def _start_loop_tick(self, channel_id: str, user_message: str | list):
        """Create a durable tick recorder for a scheduler/completion channel."""
        import uuid
        from zoneinfo import ZoneInfo
        from . import worker_tick_store

        cet = ZoneInfo("Europe/Stockholm")
        started_at = datetime.now(cet)
        if isinstance(user_message, str):
            prompt = user_message
        elif isinstance(user_message, list):
            parts = []
            for block in user_message:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text") or "")
                elif isinstance(block, str):
                    parts.append(block)
            prompt = "\n".join(p for p in parts if p) or f"[{channel_id}]"
        else:
            prompt = str(user_message)
        model = self.model_for_channel(channel_id)
        recorder = worker_tick_store.WorkerTickRecorder(
            str(uuid.uuid4()),
            started_at.strftime("%Y-%m-%d"),
            started_at,
            prompt,
            channel_id=channel_id,
            model=model,
            provider=model_registry.provider_for_model(model),
            headroom_enabled=bool(self.headroom_enabled),
            tools_count=len(self.tools or []),
        )
        await recorder.start()
        return recorder

    async def _finalize_owned_tick(self, tick_recorder, *, state: str, error: str | None = None):
        if tick_recorder is None:
            return
        try:
            from zoneinfo import ZoneInfo
            await tick_recorder.finalize(
                state=state,
                finished_at=datetime.now(ZoneInfo("Europe/Stockholm")),
                error=error,
            )
        except Exception as exc:
            log.warning("Loop tick finalize failed: %s", exc)

    async def _respond_locked(
        self,
        user_message: str | list,
        channel_id: str,
        emit,
        overlay_context: str | None = None,
        tick_recorder=None,
        run_source: str | None = None,
        client_dedup_key: str | None = None,
        request_context: dict | None = None,
    ) -> str:
        cancel_ev = asyncio.Event()
        self._turn_cancel[channel_id] = cancel_ev
        run_holder = {"recorder": None}
        messages = self._get_messages(channel_id)
        pre_turn_messages = copy.deepcopy(messages)
        owned_tick = None
        if tick_recorder is None and channel_id in LOOP_TICK_CHANNELS:
            try:
                owned_tick = await self._start_loop_tick(channel_id, user_message)
                tick_recorder = owned_tick
            except Exception as exc:
                log.warning("Loop tick start failed (%s); continuing without audit", exc)

        try:
            result = await self._respond_locked_inner(
                user_message,
                channel_id,
                emit,
                overlay_context,
                None,
                tick_recorder,
                run_source,
                client_dedup_key,
                request_context,
                run_holder,
            )
            await self._finalize_owned_tick(owned_tick, state="completed")
            return result
        except TurnCancelled:
            episode = self._active_experience_episodes.get(channel_id)
            self._record_experience_event(
                "turn_cancelled",
                channel_id,
                details={"episode_id": episode.episode_id if episode else None},
                idempotency_key=(
                    f"{episode.episode_id}:turn_cancelled" if episode else None
                ),
            )
            await self._finalize_owned_tick(owned_tick, state="interrupted", error="cancelled")
            return await self._finalize_cancelled_turn(
                channel_id,
                messages,
                pre_turn_messages,
                emit,
                recorder=run_holder["recorder"],
            )
        except asyncio.CancelledError:
            episode = self._active_experience_episodes.get(channel_id)
            self._record_experience_event(
                "turn_cancelled",
                channel_id,
                details={"episode_id": episode.episode_id if episode else None},
                idempotency_key=(
                    f"{episode.episode_id}:turn_cancelled" if episode else None
                ),
            )
            await self._finalize_owned_tick(owned_tick, state="interrupted", error="cancelled")
            await self._finalize_cancelled_turn(
                channel_id,
                messages,
                pre_turn_messages,
                emit,
                recorder=run_holder["recorder"],
            )
            raise
        except Exception as exc:
            episode = self._active_experience_episodes.get(channel_id)
            if episode is not None:
                episode.important_events.append(
                    f"turn failed with {type(exc).__name__}"
                )
                if channel_id != MAIN_CHANNEL_ID:
                    self._record_experience_event(
                        "turn_started",
                        channel_id,
                        details={
                            "episode_id": episode.episode_id,
                            "deferred": True,
                        },
                        idempotency_key=f"{episode.episode_id}:turn_started",
                    )
                await self._appraise_episode(
                    episode,
                    "outcome",
                    final_output=f"turn failed: {type(exc).__name__}",
                )
            self._record_experience_event(
                "turn_failed",
                channel_id,
                details={
                    "episode_id": episode.episode_id if episode else None,
                    "error_type": type(exc).__name__,
                },
                idempotency_key=(
                    f"{episode.episode_id}:turn_failed" if episode else None
                ),
            )
            await self._finalize_owned_tick(owned_tick, state="error", error=str(exc))
            recorder = run_holder["recorder"]
            if recorder is not None:
                await recorder.finalize_turn(state="error", error=str(exc))
            raise
        finally:
            self._turn_cancel.pop(channel_id, None)
            self._active_experience_episodes.pop(channel_id, None)

    async def _respond_locked_inner(
        self,
        user_message: str | list,
        channel_id: str,
        emit,
        overlay_context: str | None = None,
        pending_tool_results_holder: dict | None = None,
        tick_recorder=None,
        run_source: str | None = None,
        client_dedup_key: str | None = None,
        request_context: dict | None = None,
        run_holder: dict | None = None,
    ) -> str:
        messages = self._get_messages(channel_id)
        run_recorder = None
        if channel_id == MAIN_CHANNEL_ID:
            from .conversation_run_store import ConversationRunRecorder
            model = self.model_for_channel(channel_id)
            run_recorder = await ConversationRunRecorder.start(
                channel_id,
                source=run_source or "direct",
                model=model,
                provider=model_registry.provider_for_model(model),
                headroom_enabled=bool(self.headroom_enabled),
                client_dedup_key=client_dedup_key,
                request_context=request_context,
            )
            if run_recorder is not None:
                await run_recorder.begin_turn(client_dedup_key)
                if run_holder is not None:
                    run_holder["recorder"] = run_recorder

        request_summary = (
            user_message
            if isinstance(user_message, str)
            else "[multimodal user message]"
        )
        prior_experience = self.experience.snapshot()
        episode = EpisodeAccumulator(
            episode_id=str(uuid.uuid4()),
            channel=channel_id,
            request_summary=request_summary,
            started_at=time.monotonic(),
            current_state=prior_experience.get("dimensions") or {},
            previous_outcome=(
                (prior_experience.get("last_event") or {}).get(
                    "salient_change",
                    "",
                )
            ),
        )
        self._active_experience_episodes[channel_id] = episode
        # Human input can itself be consequential. Scheduled/worker prompts are
        # boilerplate and are appraised only once, at a meaningful outcome.
        if channel_id == MAIN_CHANNEL_ID:
            await self._appraise_episode(episode, "input")

        if channel_id == MAIN_CHANNEL_ID:
            experience_snapshot = self._record_experience_event(
                "turn_started",
                channel_id,
                details={"source": run_source or "direct"},
                idempotency_key=f"{episode.episode_id}:turn_started",
            )
        else:
            # Defer scheduled/worker lifecycle events until the outcome proves
            # meaningful. Idle ticks leave no experiential history.
            experience_snapshot = prior_experience
        await self._audit_experience_snapshot(
            experience_snapshot, tick_recorder, run_recorder,
        )

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
        from .recall import fetch_all_recalls, scan_text_for_recalls, generate_nudge
        active_recalls = await fetch_all_recalls()
        notified_recall_ids = set()
        turn_matched_recalls = []

        if isinstance(user_message, str):
            matched = scan_text_for_recalls(user_message, active_recalls)
            for m in matched:
                if m not in turn_matched_recalls:
                    turn_matched_recalls.append(m)

        messages.append({"role": "user", "content": user_message})
        if tick_recorder is not None:
            await tick_recorder.record_message(messages[-1])
        if run_recorder is not None:
            await run_recorder.record_message(
                messages[-1], visibility="user", kind="direct_user",
            )

        # System blocks: stable + dynamic + snapshot + advisory. Rebuilt after
        # any mid-loop / max_tokens compaction so it never goes stale.
        # overlay_context is ephemeral (Tower page pointers) — not stored in history.
        system_blocks = self._with_overlay(
            self._assemble_system_blocks(channel_id), overlay_context,
        )
        call_index = 0
        actor_context = request_context or {
            "source": run_source or "direct",
            "actor_id": "",
            "trusted": (run_source or "direct") in {"tower", "discord", "direct"},
            "trust_reason": "trusted_surface",
        }
        untrusted_org_slack = is_untrusted_organization_slack(actor_context)
        turn_tools = tools_for_request(self.tools, actor_context)

        max_tokens_retries = 0  # Track consecutive max_tokens hits
        turn_thought = ""  # Accumulated thought deltas for the current API response
        is_nudge_turn = False # Tracks if the current API call is purely resolving a system nudge

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
            if tick_recorder is not None:
                await tick_recorder.record_system_blocks(system_blocks)
            if run_recorder is not None:
                await run_recorder.record_system_blocks(system_blocks)
            call_started_at = datetime.now(timezone.utc)
            is_silent_turn = getattr(self, "_silent_turn", False)
            self._silent_turn_active = is_silent_turn
            self._silent_turn = False

            if emit is not None and not is_silent_turn:
                response = None
                async for kind, payload in provider.stream_message(
                    model=channel_model,
                    max_tokens=self.max_tokens,
                    system=system_blocks,
                    tools=turn_tools,
                    messages=messages_for_api,
                    thinking=True,
                ):
                    self._check_cancelled(channel_id)
                    if kind == "thought":
                        turn_thought += payload
                        await emit({"type": kind, "text": payload})
                    elif kind == "message":
                        response = payload
                    else:
                        await emit({"type": kind, "text": payload})
            else:
                response = await provider.create_message(
                    model=channel_model,
                    max_tokens=self.max_tokens,
                    system=system_blocks,
                    tools=turn_tools,
                    messages=messages_for_api,
                    thinking=not is_silent_turn,
                )
                
                # Extract turn_thought if available on the response blocks
                if response and hasattr(response, "content"):
                    for block in response.content:
                        if getattr(block, "type", None) == "thought":
                            turn_thought += getattr(block, "text", "")

            call_duration_ms = int(
                (datetime.now(timezone.utc) - call_started_at).total_seconds() * 1000
            )

            self._check_cancelled(channel_id)

            usage = self._log_usage(
                response,
                channel_id,
                headroom_metrics=headroom_metrics,
                tick_id=getattr(tick_recorder, "tick_id", None),
                run_id=getattr(run_recorder, "run_id", None),
                turn_id=getattr(run_recorder, "turn_id", None),
                call_index=call_index if tick_recorder is not None else None,
                duration_ms=call_duration_ms if tick_recorder is not None else None,
            )
            if tick_recorder is not None:
                await tick_recorder.record_call(
                    usage,
                    call_index=call_index,
                    duration_ms=call_duration_ms,
                    stop_reason=response.stop_reason,
                    headroom_metrics=headroom_metrics,
                )
                call_index += 1
            if run_recorder is not None:
                await run_recorder.record_call(
                    usage,
                    call_index=call_index - 1 if tick_recorder is not None else call_index,
                    duration_ms=call_duration_ms,
                    stop_reason=response.stop_reason,
                    headroom_metrics=headroom_metrics,
                )
                if tick_recorder is None:
                    call_index += 1
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

            is_empty_nudge_ack = False
            if getattr(self, "_silent_turn_active", False) and response.stop_reason == "end_turn":
                text_parts = [
                    block.get("text", "")
                    for block in (assistant_content if isinstance(assistant_content, list) else [])
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                text = "\n".join(text_parts).strip()
                if not text or "<empty/>" in text:
                    is_empty_nudge_ack = True

            if is_empty_nudge_ack:
                log.info("Agent output <empty/> (or similar) during nudge. Scrubbing from history.")
                if messages and messages[-1].get("role") == "user" and "<system_directive>" in str(messages[-1].get("content", "")):
                    messages.pop()
            else:
                messages.append(assistant_msg)
                if tick_recorder is not None:
                    await tick_recorder.record_message(assistant_msg)
                if run_recorder is not None:
                    await run_recorder.record_message(assistant_msg)
            
            self._silent_turn_active = False
            log.info(f"Response stop_reason: {response.stop_reason}")
            self._check_cancelled(channel_id)

            # --- BEGIN RECALL SCAN (IN-PROCESS STEERING) ---
            text_to_scan = ""
            _text_parts = [
                block.get("text", "") if isinstance(block, dict) else (block.text if hasattr(block, "text") else "")
                for block in (assistant_content if isinstance(assistant_content, list) else [])
                if (isinstance(block, dict) and block.get("type") == "text") or (hasattr(block, "type") and block.type == "text")
            ]
            text_to_scan = "\n".join(_text_parts).strip()
            if turn_thought:
                text_to_scan += "\n" + turn_thought
                
            log.debug(f"[Recall Check] Scanning output. Stop reason: {response.stop_reason}. Text length: {len(text_to_scan)}")
            if len(text_to_scan) > 0:
                log.debug(f"[Recall Check] Text to scan:\n{text_to_scan}")

            # We no longer process tool calls for semantic steering, only thoughts and final output
            # if response.stop_reason == "tool_use":
            #     tool_args = [
            #         _summarize_tool_input(block.input if hasattr(block, "input") else block.get("input", {}))
            #         for block in (response.content if hasattr(response, "content") else [])
            #         if (hasattr(block, "type") and block.type == "tool_use") or (isinstance(block, dict) and block.get("type") == "tool_use")
            #     ]
            #     if tool_args:
            #         text_to_scan += "\n" + "\n".join(tool_args)

            matched = scan_text_for_recalls(text_to_scan, active_recalls)
            if matched:
                log.debug(f"[Recall Check] Raw matches found: {[(m.get('recall_id'), round(m.get('similarity_score', 0.0), 3)) for m in matched]}")
            else:
                log.debug(f"[Recall Check] No raw matches found.")

            for m in matched:
                if m not in turn_matched_recalls:
                    turn_matched_recalls.append(m)

            new_matches = []
            already_notified = []
            
            for m in turn_matched_recalls:
                rid = m.get("recall_id")
                if rid in notified_recall_ids:
                    already_notified.append(rid)
                else:
                    if m not in new_matches:
                        new_matches.append(m)
                        
            if already_notified:
                log.debug(f"[Recall Check] Ignored previously notified nudges (already in context): {already_notified}")
            if new_matches:
                log.debug(f"[Recall Check] Pending new matches to inject: {[m.get('recall_id') for m in new_matches]}")
            # --- END RECALL SCAN ---

            # Recalls are bypassed when stop_reason == "tool_use" or "max_tokens"
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
                if final_text == "<empty/>":
                    final_text = ""
                    # Scrub the <empty/> tag from the actual stored history
                    # We might not need this anymore since we pop it above, but kept just in case
                    if messages and messages[-1].get("role") == "assistant":
                        last_msg = messages[-1]
                        if isinstance(last_msg.get("content"), list):
                            last_msg["content"] = [
                                b for b in last_msg["content"] 
                                if not (isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip() == "<empty/>")
                            ]
                        elif isinstance(last_msg.get("content"), str) and last_msg["content"].strip() == "<empty/>":
                            last_msg["content"] = ""

                if new_matches:
                    log.info(f"[Recall Check] Injecting matches for in-process steering (end_turn): {[m.get('recall_id') for m in new_matches]}")

                    for m in new_matches:
                        notified_recall_ids.add(m.get("recall_id"))
                        # Record the nudge to DB for ambient reflection
                        try:
                            from .db_ops import get_db
                            db = get_db()
                            if db is not None:
                                await db["nudge_logs"].insert_one({
                                    "recall_id": m.get("recall_id"),
                                    "channel_id": channel_id,
                                    "timestamp": datetime.now(timezone.utc),
                                    "score": m.get("similarity_score", 0.0),
                                    "text_scanned": text_to_scan[:500]  # truncate just in case
                                })
                        except Exception as e:
                            log.warning(f"Failed to log nudge to DB: {e}")

                    nudge_text = generate_nudge(new_matches)
                    log.info(f"Completion nudge triggered: {nudge_text!r}")
                    
                    nudge_prompt = (
                        f"I should review this systematic nudge against my recent actions. If I already satisfied it, or if no further action is needed, "
                        f"I will continue my normal flow or conclude.\n"
                        f"Here is the systematic nudge I received: {nudge_text}\n\n"
                    )
                    
                    messages.append({"role": "assistant", "content": nudge_prompt})
                    
                    if tick_recorder is not None:
                        await tick_recorder.record_message(messages[-1])
                    if run_recorder is not None:
                        await run_recorder.record_message(
                            messages[-1], visibility="system", kind="direct_assistant"
                        )
                    # Re-run the turn loop so the agent can fix its mistake
                    api_messages = None
                    self._silent_turn = True
                    continue

                meaningful_outcome = self._should_appraise_outcome(
                    episode,
                    final_text,
                )
                if meaningful_outcome and channel_id != MAIN_CHANNEL_ID:
                    self._record_experience_event(
                        "turn_started",
                        channel_id,
                        details={
                            "episode_id": episode.episode_id,
                            "deferred": True,
                        },
                        idempotency_key=f"{episode.episode_id}:turn_started",
                    )
                if meaningful_outcome:
                    await self._appraise_episode(
                        episode,
                        "outcome",
                        final_output=final_text,
                    )
                    experience_snapshot = self._record_experience_event(
                        "turn_completed",
                        channel_id,
                        details={
                            "episode_id": episode.episode_id,
                            "response_chars": len(final_text),
                            "tools_attempted": episode.attempted,
                        },
                        idempotency_key=f"{episode.episode_id}:turn_completed",
                    )
                    if channel_id == AMBIENT_CHANNEL_ID:
                        experience_snapshot = self._record_experience_event(
                            "reflection", channel_id,
                            details={"episode_id": episode.episode_id},
                            idempotency_key=f"{episode.episode_id}:reflection",
                        )
                else:
                    experience_snapshot = self.experience.snapshot()
                await self._audit_experience_snapshot(
                    experience_snapshot, tick_recorder, run_recorder,
                )
                user_summary = user_message[:100] if isinstance(user_message, str) else "[multimodal message]"
                self.memory.append_daily_log(
                    f"[chat:{channel_id}] User: {user_summary}..."
                )
                conversation_store.save_channel(self.working_dir, channel_id, messages)
                if run_recorder is not None:
                    # Prefer the final assistant thought; fall back to the
                    # latest thought earlier in this tool cascade.
                    reply_thought = (assistant_msg.get("_thought") or "").strip()
                    if not reply_thought:
                        for prior in reversed(messages):
                            if prior.get("role") != "assistant":
                                continue
                            reply_thought = (prior.get("_thought") or "").strip()
                            if reply_thought:
                                break
                    await run_recorder.record_direct_reply(
                        final_text, thought=reply_thought or None,
                    )
                    await run_recorder.finalize_turn(state="completed")
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
                experience_changed = False
                # Use the original response.content blocks to extract tool IDs
                for block in tool_blocks:
                    self._check_cancelled(channel_id)

                    tool_name = block.name
                    tool_input = block.input if isinstance(block.input, dict) else {}
                    tool_id = block.id

                    if untrusted_org_slack and tool_name not in UNTRUSTED_READ_ONLY_TOOLS:
                        blocked = f"[BLOCKED] {tool_name} is not available to this Slack actor."
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": blocked,
                        })
                        if pending_tool_results_holder is not None:
                            pending_tool_results_holder["results"] = tool_results
                        if emit is not None:
                            await emit({
                                "type": "tool_result", "name": tool_name,
                                "output": blocked,
                            })
                        experience_snapshot = self._observe_episode_tool(
                            episode,
                            tool_name,
                            failed=True,
                            reason="permission",
                        )
                        experience_changed = (
                            experience_changed or bool(experience_snapshot)
                        )
                        await self._audit_experience_snapshot(
                            experience_snapshot, tick_recorder, run_recorder,
                        )
                        continue

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

                        if untrusted_org_slack and (
                            tier != "green" or not is_demonstrably_read_only(command)
                        ):
                            blocked = (
                                "[BLOCKED] Shared-channel Slack actors may run only "
                                "demonstrably read-only green commands."
                            )
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": tool_id,
                                "content": blocked,
                            })
                            if pending_tool_results_holder is not None:
                                pending_tool_results_holder["results"] = tool_results
                            if emit is not None:
                                await emit({
                                    "type": "tool_result", "name": tool_name,
                                    "output": blocked,
                                })
                            experience_snapshot = self._observe_episode_tool(
                                episode,
                                tool_name,
                                failed=True,
                                reason="permission",
                            )
                            experience_changed = (
                                experience_changed or bool(experience_snapshot)
                            )
                            await self._audit_experience_snapshot(
                                experience_snapshot, tick_recorder, run_recorder,
                            )
                            continue

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
                                experience_snapshot = self._observe_episode_tool(
                                    episode,
                                    tool_name,
                                    failed=True,
                                    reason="permission",
                                )
                                experience_changed = (
                                    experience_changed or bool(experience_snapshot)
                                )
                                await self._audit_experience_snapshot(
                                    experience_snapshot, tick_recorder, run_recorder,
                                )
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
                        experience_manager=self.experience,
                        channel_id=channel_id,
                    )
                    if tool_name == "experience_report":
                        # The tool already recorded a non-authoritative
                        # self_report event. Counting the report itself as task
                        # success would let introspective prose reward state.
                        experience_snapshot = self.experience.snapshot()
                    else:
                        tool_failed = tool_result_failed(result)
                        experience_snapshot = self._observe_episode_tool(
                            episode,
                            tool_name,
                            failed=tool_failed,
                        )
                        experience_changed = (
                            experience_changed or bool(experience_snapshot)
                        )
                    await self._audit_experience_snapshot(
                        experience_snapshot, tick_recorder, run_recorder,
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

                if new_matches:
                    log.info(f"[Recall Check] Injecting matches for in-process steering (tool_use): {[m.get('recall_id') for m in new_matches]}")

                    for m in new_matches:
                        notified_recall_ids.add(m.get("recall_id"))
                        # Record the nudge to DB for ambient reflection
                        try:
                            from .db_ops import get_db
                            db = get_db()
                            if db is not None:
                                await db["nudge_logs"].insert_one({
                                    "recall_id": m.get("recall_id"),
                                    "channel_id": channel_id,
                                    "timestamp": datetime.now(timezone.utc),
                                    "score": m.get("similarity_score", 0.0),
                                    "text_scanned": text_to_scan[:500]  # truncate just in case
                                })
                        except Exception as e:
                            log.warning(f"Failed to log nudge to DB: {e}")

                    nudge_text = generate_nudge(new_matches)
                    log.info(f"Tool-use nudge triggered: {nudge_text!r}")
                    
                    nudge_prompt = (
                        f"I should review this systematic nudge against my recent actions. If I already satisfied it, or if no further action is needed, "
                        f"I will continue my normal flow or conclude.\n"
                        f"Here is the systematic nudge I received: {nudge_text}\n\n"
                    )
                    
                messages.append({"role": "user", "content": tool_results})
                if tick_recorder is not None:
                    await tick_recorder.record_message(messages[-1])
                if run_recorder is not None:
                    await run_recorder.record_message(messages[-1])

                if new_matches:
                    messages.append({"role": "assistant", "content": nudge_prompt})
                    if tick_recorder is not None:
                        await tick_recorder.record_message(messages[-1])
                    if run_recorder is not None:
                        await run_recorder.record_message(messages[-1], visibility="system", kind="direct_assistant")

                # Tool outcomes can change the shared experiential state. Make
                # that change globally available to the very next reasoning
                # step in this same cascade, rather than waiting for a new turn.
                if experience_changed and self.experience.influences_model:
                    system_blocks = self._with_overlay(
                        self._assemble_system_blocks(channel_id), overlay_context,
                    )

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

    async def switch_main_run(self, run_id: str) -> dict:
        """Park the current active main run and resume ``run_id`` as the live tail.

        Rebuilds ``conversations['main']`` from durable events after the latest
        checkpoint. Does not palace-mine on park (events already durable).
        Same-run select is allowed while busy so the UI can reattach to the stream.
        """
        from . import conversation_run_store

        run = conversation_run_store.get_run(run_id)
        if run is None or (run.get("channel_id") or MAIN_CHANNEL_ID) != MAIN_CHANNEL_ID:
            raise ValueError("Conversation not found")

        active = conversation_run_store.active_run(MAIN_CHANNEL_ID)
        if active and active.get("run_id") == run_id:
            messages = self._get_messages(MAIN_CHANNEL_ID)
            return {
                "run_id": run_id,
                "switched": False,
                "message_count": len(messages),
                "title": run.get("title"),
            }

        if self.is_channel_busy(MAIN_CHANNEL_ID):
            raise RuntimeError("Channel is busy")

        current = self.conversations.get(MAIN_CHANNEL_ID) or []
        if current:
            conversation_store.save_channel(self.working_dir, MAIN_CHANNEL_ID, current)

        if active and active.get("run_id") != run_id:
            await conversation_run_store.end_active_run(MAIN_CHANNEL_ID, "switch")

        messages, checkpoint = conversation_run_store.buffer_messages_for_run(run_id)
        self.conversations[MAIN_CHANNEL_ID] = list(messages)
        conversation_store.save_channel(self.working_dir, MAIN_CHANNEL_ID, messages)
        self._post_recovery_archive_tag.pop(MAIN_CHANNEL_ID, None)
        self._output_ceiling_streak.pop(MAIN_CHANNEL_ID, None)
        self._last_input_tokens.pop(MAIN_CHANNEL_ID, None)
        self._last_archived_len[MAIN_CHANNEL_ID] = 0
        if checkpoint and checkpoint.get("summary"):
            self._compaction_summary[MAIN_CHANNEL_ID] = checkpoint["summary"]
        else:
            self._compaction_summary.pop(MAIN_CHANNEL_ID, None)

        reactivated = await conversation_run_store.reactivate_run(run_id)
        if reactivated is None:
            raise RuntimeError("Failed to reactivate conversation")
        log.info(
            "Switched main conversation to run %s (%d messages after checkpoint).",
            run_id, len(messages),
        )
        return {
            "run_id": run_id,
            "switched": True,
            "message_count": len(messages),
            "title": reactivated.get("title") or run.get("title"),
        }

    async def pop_and_archive_history(self, channel_id: str = "default", reason: str = "new") -> int:
        """Archive the channel's conversation to the palace, then clear it.

        Used by Discord `/new` / `!new` / `!clear`. Returns the number of
        messages archived (0 if the channel was already empty). Archive is
        awaited — by the time this returns, the palace mine has either
        succeeded or logged a failure. Callers in an async context can
        safely use this before responding to the user.

        Silent fallback if mempalace isn't installed: history is still
        cleared, just not archived.
        """
        messages = self.conversations.get(channel_id)
        if not messages:
            if channel_id == MAIN_CHANNEL_ID:
                from .conversation_run_store import end_active_run
                await end_active_run(channel_id, reason)
            return 0
        if channel_id == MAIN_CHANNEL_ID:
            try:
                from .conversation_run_store import ConversationRunRecorder
                from .memory_sync import stage_and_mine_main
                recorder = await ConversationRunRecorder.for_active(channel_id, source="clear")
                if recorder is not None:
                    await stage_and_mine_main(
                        recorder.run_id, list(messages), kind="full", agent="new-clear",
                    )
            except Exception as e:
                log.warning(f"Conversation outbox archive failed on /new: {e}")
        self.conversations.pop(channel_id, None)
        conversation_store.delete_channel(self.working_dir, channel_id)
        # Clear per-channel transient state alongside the history.
        self._post_recovery_archive_tag.pop(channel_id, None)
        self._output_ceiling_streak.pop(channel_id, None)
        self._compaction_summary.pop(channel_id, None)
        self._last_input_tokens.pop(channel_id, None)
        self._last_archived_len.pop(channel_id, None)
        try:
            if channel_id != MAIN_CHANNEL_ID:
                from . import palace
                await palace.archive_conversation(channel_id, messages)
        except Exception as e:
            log.warning(f"Conversation archive failed on /new: {e}")
        if channel_id == MAIN_CHANNEL_ID:
            from .conversation_run_store import end_active_run
            await end_active_run(channel_id, reason)
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

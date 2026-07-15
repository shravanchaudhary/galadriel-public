"""Worker — the agent's background "doing" hat.

A second channel of the same GaladrielAgent that executes day-to-day work while
the main channel stays free to talk to the user. Curator (main) and worker
(this) never share memory; they coordinate ONLY through markdown files (plus
config/JOBS.md, which is auto-loaded into both hats' context — no board file
needed for that one):

  config/JOBS.md         broad goals + recurring rules (rituals)   [curator writes; L1, always in context]
  jobs/<id>.md           per-job cookbook (key steps)              [curator writes]
  state/backlog.md       projects / one-offs, carry forward        [curator writes]
  state/worker_control.md  active | paused                         [curator writes]
  state/progress/        shared work ledger, ONE FILE PER DAY:      [curator + worker]
                         status, blocked, done+evidence — every
                         completed/irreversible action from BOTH hats
  state/plan/            daily planning ledger, ONE FILE PER DAY   [curator + scheduler]
                         (intended actions)
  state/steering.md      reflection corrections (append-only)     [reflection writes]

Loop shape (work-conserving, single asyncio task = inherently single-flight):
  - paused?            → idle-poll (no work started)
  - else reset worker channel (lean tick — durable state is the board + DB + palace)
  - else run one worker turn (agent reads the board, picks per rules, acts,
    writes today's progress file). The turn returns text ONLY on a state transition
    (started/blocked/done/failed) — that text is relayed to Discord. It ends
    with a machine tag <<WORKER_STATUS: worked|idle>> the loop reads to decide
    whether to keep going (work remains) or idle-poll (nothing to do).

The current time (CET) and elapsed session time are injected at the TAIL of the
prompt each turn, so the worker is time-aware without carrying prior-tick
transcript (which would inflate tokens and hurt prompt-cache hits). Opt-in: set
GALADRIEL_WORKER=1 to enable.
"""

import asyncio
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .loop_prompts import WORKER_PROMPT
from . import worker_tick_store
from . import model_registry

log = logging.getLogger("galadriel.worker")

CET = ZoneInfo("Europe/Stockholm")

WORKER_CHANNEL = "worker"
# When the agent reports work remains, loop again after a short floor gap (never
# hot-loop). When it reports idle/nothing-actionable, poll less often.
MIN_GAP_SEC = 30
IDLE_POLL_SEC = 600  # 10 minutes
# Soft cap on continuous time spent on a single project before the worker should
# come up for air and re-scan the board. Surfaced to the agent in the clock.
PROJECT_SLICE_CAP_MIN = 30

_STATUS_RE = re.compile(r"<<WORKER_STATUS:\s*(worked|idle)\s*>>", re.IGNORECASE)


class WorkerLoop:
    """Runs the agent's background worker channel on a work-conserving loop."""

    def __init__(self, agent, discord_bot=None, working_dir: str = "."):
        self.agent = agent
        self.bot = discord_bot
        self._control_path = Path(working_dir) / "state" / "worker_control.md"
        self._task: asyncio.Task | None = None
        self._started_at: datetime | None = None
        # Rising-edge tracker for the "picking up work" ping: True only while the
        # worker is mid-burst (consecutive worked ticks), so the ping fires once
        # when a burst begins, not on every tick. Reset on pause/idle.
        self._was_working: bool = False

    def set_bot(self, bot):
        self.bot = bot

    def start(self):
        """Start the worker loop. Call from an async context (e.g. on_ready)."""
        self._started_at = datetime.now(CET)
        self._task = asyncio.ensure_future(self._loop())
        log.info("Worker loop started.")

    # ── Loop ─────────────────────────────────────────────────────

    async def _loop(self):
        try:
            await worker_tick_store.mark_running_ticks_interrupted()
            while True:
                if self._paused():
                    log.info("Worker paused (control flag) — idle poll.")
                    # Paused counts as rest, so resuming into work re-fires the ping.
                    self._was_working = False
                    await asyncio.sleep(IDLE_POLL_SEC)
                    continue

                status = await self._run_turn()
                # 'worked' → more may remain, loop again after a short floor gap.
                # 'idle'   → nothing actionable, poll less often.
                await asyncio.sleep(MIN_GAP_SEC if status == "worked" else IDLE_POLL_SEC)
        except asyncio.CancelledError:
            log.info("Worker loop cancelled.")
        except Exception as e:
            log.exception(f"Worker loop error: {e}")

    async def _run_turn(self) -> str:
        """Run one worker turn. Returns 'worked' or 'idle'. Relays any notification."""
        # Lean ticks: start every tick from a clean buffer. The worker's durable
        # memory is the board + DB + palace, not the in-context transcript, so
        # carrying the prior tick forward only inflates input tokens and busts
        # the prompt cache across the idle gap (the cache-miss cost blowup,
        # finding #7). Resetting at the START is exception-safe — a failed turn
        # never poisons the next.
        self.agent.reset_channel(WORKER_CHANNEL)
        prompt = WORKER_PROMPT + "\n\n" + self._build_clock()
        started_at = datetime.now(CET)
        tick_id = str(uuid.uuid4())
        model = self.agent.model_for_channel(WORKER_CHANNEL)
        recorder = worker_tick_store.WorkerTickRecorder(
            tick_id,
            started_at.strftime("%Y-%m-%d"),
            started_at,
            prompt,
            model=model,
            provider=model_registry.provider_for_model(model),
            headroom_enabled=bool(getattr(self.agent, "headroom_enabled", False)),
            tools_count=len(getattr(self.agent, "tools", [])),
        )
        await recorder.start()
        try:
            text = await self.agent.respond(
                prompt, channel_id=WORKER_CHANNEL, tick_recorder=recorder,
            )
        except Exception as e:
            log.exception(f"Worker turn error: {e}")
            await recorder.finalize(
                state="error",
                finished_at=datetime.now(CET),
                error=str(e),
            )
            return "idle"

        status, note = self._parse(text)
        await recorder.finalize(
            state="completed",
            finished_at=datetime.now(CET),
            worker_status=status,
            notification=note,
        )
        # Rising edge into a work burst (idle/paused → working): ping once so the
        # user sees the worker pick up a task. Subsequent worked ticks in the same
        # burst don't re-ping; an idle/paused tick resets the edge.
        if status == "worked" and not self._was_working:
            await self._send_to_discord("🔧 Worker picking up work…")
        self._was_working = status == "worked"
        if note:
            log.info(f"Worker notification: {note[:100]}")
            await self._send_to_discord(note)
        log.info(f"Worker turn complete — status={status}")
        return status

    # ── Helpers ──────────────────────────────────────────────────

    def _paused(self) -> bool:
        """True unless the control flag's state line explicitly says active.

        Only the first non-empty, non-comment line is the state, so prose in the
        file's comments can't flip it. Missing/empty/unreadable → paused (safe
        default: the worker stays quiet until a board is set up and marked active)."""
        try:
            content = self._control_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return True
        except Exception as e:
            log.warning(f"Worker control read failed ({e}); treating as paused.")
            return True
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            return line.lower() != "active"
        return True

    def _build_clock(self) -> str:
        """Tail-injected, cache-safe time block. Harness computes the diffs (and
        today's ledger file paths) so the model never has to do time math or
        construct a filename itself."""
        now = datetime.now(CET)
        elapsed = "unknown"
        if self._started_at:
            secs = int((now - self._started_at).total_seconds())
            elapsed = f"{secs // 3600}h {(secs % 3600) // 60}m"
        today = now.strftime("%Y-%m-%d")
        return (
            "[WORKER:CLOCK]\n"
            f"NOW = {now.strftime('%Y-%m-%d %H:%M')} CET (weekday {now.strftime('%A')})\n"
            f"session_elapsed = {elapsed}\n"
            f"project_slice_cap = {PROJECT_SLICE_CAP_MIN}m "
            "(if you have spent longer than this on one project, checkpoint and re-scan)\n"
            f"today_progress_file = state/progress/{today}.md\n"
            f"today_plan_file = state/plan/{today}.md"
        )

    def _parse(self, text: str) -> tuple[str, str]:
        """Split the worker output into (status, notification). Default idle if the
        tag is missing (safe — avoids hot-looping)."""
        if not text:
            return "idle", ""
        match = _STATUS_RE.search(text)
        status = match.group(1).lower() if match else "idle"
        note = _STATUS_RE.sub("", text).strip()
        return status, note

    async def _send_to_discord(self, message: str):
        """Relay a worker notification to the authorized user via DM."""
        if not self.bot:
            log.warning("No Discord bot available for worker notification.")
            return
        if hasattr(self.bot, "get_dm_channel"):
            channel = await self.bot.get_dm_channel()
        else:
            channel = None
        if not channel:
            log.warning("Could not resolve Discord channel for worker notification.")
            return

        max_len = 1900
        text = message
        while text:
            if len(text) <= max_len:
                await channel.send(text)
                break
            split_at = text.rfind("\n", 0, max_len)
            if split_at == -1:
                split_at = max_len
            await channel.send(text[:split_at])
            text = text[split_at:].lstrip("\n")

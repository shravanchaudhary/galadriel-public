"""Worker — the agent's background "doing" hat.

A second channel of the same GaladrielAgent that executes day-to-day work while
the main channel stays free to talk to the user. Curator (main) and worker
(this) never share memory; they coordinate ONLY through markdown files:

  jobs/job_roles.md      broad goals + recurring rules (rituals)   [curator writes]
  jobs/<id>.md           per-job cookbook (key steps)              [curator writes]
  state/backlog.md       projects / one-offs, carry forward        [curator writes]
  state/worker_control.md  active | paused                         [curator writes]
  state/progress.md      live status, blocked, done+evidence       [WORKER writes]

Loop shape (work-conserving, single asyncio task = inherently single-flight):
  - paused?            → idle-poll (no work started)
  - else run one worker turn (agent reads the board, picks per rules, acts,
    writes progress.md). The turn returns text ONLY on a state transition
    (started/blocked/done/failed) — that text is relayed to Discord. It ends
    with a machine tag <<WORKER_STATUS: worked|idle>> the loop reads to decide
    whether to keep going (work remains) or idle-poll (nothing to do).

The current time (CET) and elapsed session time are injected at the TAIL of the
prompt each turn, so the worker is time-aware without churning the cached
prefix. Opt-in: set GALADRIEL_WORKER=1 to enable.
"""

import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

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

WORKER_PROMPT = (
    "[SYSTEM:WORKER_TICK] You are operating as the WORKER. This turn is NOT shown "
    "to the user unless you choose to notify (see OUTPUT). Work silently; the value "
    "is in what you DO and what you file, not in talking.\n\n"
    "1. Read the board, in this order, with read_file:\n"
    "   - state/worker_control.md (if it says paused, stop now: output nothing but "
    "the idle tag).\n"
    "   - jobs/job_roles.md (your goals + recurring rules / rituals).\n"
    "   - state/backlog.md (projects / one-offs).\n"
    "   - state/progress.md (what you already did, what is blocked, where you left off).\n"
    "   - the relevant jobs/<id>.md cookbook for whatever you pick (and palace_search "
    "for any detail the cookbook references).\n\n"
    "2. Pick the next action using the injected current time:\n"
    "   - A RITUAL due now (e.g. 'check DMs at 11:00') PREEMPTS project work.\n"
    "   - Else continue/advance the top open PROJECT.\n"
    "   - A ritual missed earlier is NOT done twice — doing today's once is enough; "
    "rituals never accumulate. Projects carry forward until truly done.\n"
    "   - If a blocker stops a task: record it in progress.md, notify once, then "
    "MOVE ON to the next actionable task (blocked = park, not stop).\n"
    "   - If you have spent more than the slice cap on one project, checkpoint to "
    "progress.md and re-scan before continuing.\n"
    "   - If a task launches a long external process, record it in progress.md and "
    "check it on your next tick — do NOT arm a heartbeat; this loop already polls.\n\n"
    "3. Do ONE useful unit of work now. Verify real outcomes (a row exists, a file "
    "was written, a message sent) — never mark something done you did not verify.\n\n"
    "4. Update state/progress.md (write_file): current task, what you just did, any "
    "irreversible step taken (so a restart never repeats it), blockers, and "
    "completed items as 'done_pending_verify' WITH evidence (links/counts/paths) for "
    "the curator to check.\n\n"
    "OUTPUT (this is the ONLY thing the user may see):\n"
    "  - If a state transition happened (started a long task / blocked / completed / "
    "failed), write a short one-line notification for the user. Otherwise output NO "
    "prose at all.\n"
    "  - ALWAYS end your turn with exactly one machine tag on its own line:\n"
    "      <<WORKER_STATUS: worked>>   if you did work and more may remain\n"
    "      <<WORKER_STATUS: idle>>     if nothing was actionable (or paused)\n"
)


class WorkerLoop:
    """Runs the agent's background worker channel on a work-conserving loop."""

    def __init__(self, agent, discord_bot=None, working_dir: str = "."):
        self.agent = agent
        self.bot = discord_bot
        self._control_path = Path(working_dir) / "state" / "worker_control.md"
        self._task: asyncio.Task | None = None
        self._started_at: datetime | None = None

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
            while True:
                if self._paused():
                    log.info("Worker paused (control flag) — idle poll.")
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
        prompt = WORKER_PROMPT + "\n\n" + self._build_clock()
        try:
            text = await self.agent.respond(prompt, channel_id=WORKER_CHANNEL)
        except Exception as e:
            log.exception(f"Worker turn error: {e}")
            return "idle"

        status, note = self._parse(text)
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
        """Tail-injected, cache-safe time block. Harness computes the diffs so the
        model never has to do time math."""
        now = datetime.now(CET)
        elapsed = "unknown"
        if self._started_at:
            secs = int((now - self._started_at).total_seconds())
            elapsed = f"{secs // 3600}h {(secs % 3600) // 60}m"
        return (
            "[WORKER:CLOCK]\n"
            f"NOW = {now.strftime('%Y-%m-%d %H:%M')} CET (weekday {now.strftime('%A')})\n"
            f"session_elapsed = {elapsed}\n"
            f"project_slice_cap = {PROJECT_SLICE_CAP_MIN}m "
            "(if you have spent longer than this on one project, checkpoint and re-scan)"
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

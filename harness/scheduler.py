"""Scheduler — heartbeat, one-shot wake, morning, reflection, and goodnight.

Scheduled activities:
  1. Heartbeat: periodic self-initiated check-in (configurable interval, toggle
     on/off). Can carry a custom prompt — used to make the agent monitor a
     long-running background task and report on it each tick.
  2. One-shot wake: a single, restart-surviving self-prompt. Unlike the
     heartbeat, it fires EXACTLY ONCE and clears itself only after delivery.
     This is the correct mechanism for "resume me after I restart myself".
  3. Morning (09:10 CET, workdays only): morning greeting, calendar, coffers.
  4. Ambient reflection (workday slots): the agent thinks, files anything worth
     keeping to the palace, audits the background worker, and posts a brief
     worker-status summary (pausing the worker if it is misbehaving).
  5. Goodnight (21:00 CET): wish good night and disable heartbeat (REST).

Ambient reflection is opt-out: set GALADRIEL_REFLECTION=0 to disable.
"""

import asyncio
import logging
import json
import os
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .loop_prompts import (
    DEFAULT_HEARTBEAT_PROMPT,
    catchup_prompt as _catchup_prompt,
    goodnight_prompt as _goodnight_prompt,
    morning_prompt as _morning_prompt,
    reflection_prompt as _reflection_prompt,
)

log = logging.getLogger("galadriel.scheduler")

CET = ZoneInfo("Europe/Stockholm")

# Channels owned by the scheduler's own routines (not real user conversations).
# Used to decide which channels to checkpoint-mine before reflection.
SCHEDULER_CHANNELS = {"wake", "heartbeat", "morning", "reflection", "goodnight"}
# Minimum gap between heartbeat checkpoints — heartbeats are too frequent to mine
# every tick, so we only checkpoint the heartbeat channel once per hour.
HEARTBEAT_CHECKPOINT_MIN_GAP = timedelta(hours=1)

# Morning: 09:10 CET on workdays (Mon-Fri) — overridable via Tower /agent
MORNING_TIME = time(9, 10)
# Goodnight: 21:00 CET every day — overridable via Tower /agent
GOODNIGHT_TIME = time(21, 0)
# Ambient reflection slots (workdays only): palace filing + worker audit +
# a brief status summary to the user at each slot.
REFLECTION_TIMES = (time(11, 0), time(14, 0), time(17, 0), time(20, 0))

# Valid heartbeat intervals in minutes
VALID_INTERVALS = [5, 10, 20, 30]
DEFAULT_INTERVAL = 10


def _try_parse_hhmm(value: str | None) -> time | None:
    """Parse ``HH:MM`` into a time, or None if invalid."""
    if not value or not isinstance(value, str):
        return None
    try:
        hour_s, minute_s = value.strip().split(":", 1)
        hour, minute = int(hour_s), int(minute_s)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return time(hour, minute)
    except (TypeError, ValueError):
        pass
    return None


def _parse_hhmm(value: str | None, default: time) -> time:
    """Parse ``HH:MM`` into a time; fall back to ``default`` on bad input."""
    return _try_parse_hhmm(value) or default


def _format_hhmm(value: time) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"

# Default heartbeat prompt lives in harness/loop_prompts.py (also shown in Tower /agent).

# Morning / catchup prompt builders live in harness/loop_prompts.py.

# State file lives in config/ (ReadWritePaths in systemd)
STATE_FILE_NAME = "scheduler_state.json"


class Scheduler:
    """Manages periodic and cron-like tasks for Galadriel."""

    def __init__(self, agent, discord_bot=None, config_dir: str = "config"):
        self.agent = agent
        self.bot = discord_bot
        self._state_path = Path(config_dir) / STATE_FILE_NAME
        self._loop: asyncio.AbstractEventLoop | None = None  # captured in start()

        # Heartbeat state
        self.heartbeat_enabled = False
        self.heartbeat_interval = DEFAULT_INTERVAL  # minutes
        self.heartbeat_prompt: str | None = None    # custom per-task prompt
        self._heartbeat_task: asyncio.Task | None = None

        # One-shot wake state (restart-surviving)
        self.pending_wake: str | None = None
        self._wake_task: asyncio.Task | None = None

        # Configurable routine times (CET); defaults match MORNING/GOODNIGHT_TIME
        self.morning_time: time = MORNING_TIME
        self.goodnight_time: time = GOODNIGHT_TIME

        # Cron tasks
        self._morning_task: asyncio.Task | None = None
        self._goodnight_task: asyncio.Task | None = None
        self._reflection_task: asyncio.Task | None = None
        self._catchup_task: asyncio.Task | None = None
        # Concurrent-guard for Tower/API manual morning re-runs
        self._manual_morning_future = None

        # Track last fire times to avoid double-fires
        self._last_morning: str | None = None
        self._last_goodnight: str | None = None
        # Reflection tracks each (date, slot) so all slots fire once per day
        self._fired_reflections: set[str] = set()
        # Heartbeat checkpoints are rate-limited to once per hour (see loop).
        self._last_heartbeat_checkpoint: datetime | None = None

        # Load persisted state
        self._load_state()

    # ── Persistence ──────────────────────────────────────────────

    # Persistence note: most state is convenience-only and is rebuilt on
    # restart. ONE EXCEPTION — the one-shot wake (pending_wake) IS meant to
    # survive a restart by design. On _load_state we read it back so that a
    # process that armed a wake and then died (or restarted itself on purpose)
    # still honours it on the next boot. start() spawns the wake loop if the
    # pending_wake field is populated.

    def _load_state(self):
        """Load heartbeat + pending-wake state from disk."""
        if self._state_path.exists():
            try:
                data = json.loads(self._state_path.read_text())
                self.heartbeat_enabled = data.get("heartbeat_enabled", False)
                self.heartbeat_prompt = data.get("heartbeat_prompt") or None
                self.pending_wake = data.get("pending_wake") or None
                interval = data.get("heartbeat_interval", DEFAULT_INTERVAL)
                if interval in VALID_INTERVALS:
                    self.heartbeat_interval = interval
                self.morning_time = _parse_hhmm(
                    data.get("morning_time"), MORNING_TIME
                )
                self.goodnight_time = _parse_hhmm(
                    data.get("goodnight_time"), GOODNIGHT_TIME
                )
                # Fire-trackers — restored so "did X already run today?" survives a
                # restart. Without this a mid-day restart re-fires routines and a
                # post-grace restart silently skips the missed morning entirely.
                self._last_morning = data.get("last_morning") or None
                self._last_goodnight = data.get("last_goodnight") or None
                self._fired_reflections = set(data.get("fired_reflections") or [])
                log.info(
                    f"Scheduler state loaded: enabled={self.heartbeat_enabled}, "
                    f"interval={self.heartbeat_interval}m, "
                    f"pending_wake={'armed' if self.pending_wake else 'none'}, "
                    f"morning={_format_hhmm(self.morning_time)}, "
                    f"goodnight={_format_hhmm(self.goodnight_time)}, "
                    f"last_morning={self._last_morning}"
                )
            except Exception as e:
                log.warning(f"Failed to load scheduler state: {e}")

    def _save_state(self):
        """Persist heartbeat + pending-wake state to disk."""
        try:
            data = {
                "heartbeat_enabled": self.heartbeat_enabled,
                "heartbeat_interval": self.heartbeat_interval,
                "morning_time": _format_hhmm(self.morning_time),
                "goodnight_time": _format_hhmm(self.goodnight_time),
            }
            if self.heartbeat_prompt:
                data["heartbeat_prompt"] = self.heartbeat_prompt
            if self.pending_wake:
                data["pending_wake"] = self.pending_wake
            if self._last_morning:
                data["last_morning"] = self._last_morning
            if self._last_goodnight:
                data["last_goodnight"] = self._last_goodnight
            if self._fired_reflections:
                data["fired_reflections"] = sorted(self._fired_reflections)
            self._state_path.write_text(json.dumps(data, indent=2))
        except Exception as e:
            log.warning(f"Failed to save scheduler state: {e}")

    # ── Public API ───────────────────────────────────────────────

    def set_bot(self, bot):
        """Set the Discord bot reference (called after bot creation)."""
        self.bot = bot

    def get_status(self) -> dict:
        """Return current scheduler status for the Tower UI."""
        now_cet = datetime.now(CET)
        morning = _format_hhmm(self.morning_time)
        goodnight = _format_hhmm(self.goodnight_time)
        return {
            "heartbeat_enabled": self.heartbeat_enabled,
            "heartbeat_interval": self.heartbeat_interval,
            "heartbeat_prompt": self.heartbeat_prompt,
            "pending_wake": "armed" if self.pending_wake else None,
            "valid_intervals": VALID_INTERVALS,
            "morning_hhmm": morning,
            "goodnight_hhmm": goodnight,
            "morning_time": f"{morning} CET (workdays)",
            "goodnight_time": f"{goodnight} CET (daily)",
            "reflection_times": "11:00/14:00/17:00/20:00 CET (workdays — palace + worker audit + status)",
            "server_time_cet": now_cet.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "is_workday": now_cet.weekday() < 5,
            "morning_manual_running": bool(
                self._manual_morning_future is not None
                and not self._manual_morning_future.done()
            ),
        }

    def set_routine_time(self, routine: str, hhmm: str) -> None:
        """Update morning or goodnight fire time (CET). Thread-safe; takes effect
        on the next cron poll (≤30s)."""
        routine = (routine or "").strip().lower()
        parsed = _try_parse_hhmm(hhmm)
        if parsed is None:
            raise ValueError("Invalid time; use HH:MM")
        if routine == "morning":
            self.morning_time = parsed
        elif routine == "goodnight":
            self.goodnight_time = parsed
        else:
            raise ValueError("routine must be 'morning' or 'goodnight'")
        self._save_state()
        log.info(
            f"Routine time updated: morning={_format_hhmm(self.morning_time)}, "
            f"goodnight={_format_hhmm(self.goodnight_time)}"
        )

    def trigger_morning(self) -> dict:
        """Run the morning planning routine now (manual, any day, re-runnable).

        Schedules the same ``_morning_routine`` the cron uses onto the captured
        event loop. Ignores workday/weekend gates and the once-per-day cron
        tracker so Tower can re-plan today on demand. Waits briefly for the
        durable morning tick id so the UI can open the live chat stream.
        """
        import time
        from . import worker_tick_store

        if not self._loop or not self._loop.is_running():
            raise RuntimeError("Scheduler loop is not running")
        if (
            self._manual_morning_future is not None
            and not self._manual_morning_future.done()
        ):
            tick_id = getattr(self, "_manual_morning_tick_id", None)
            return {
                "started": False,
                "reason": "already_running",
                "tick_id": tick_id,
            }

        self._manual_morning_tick_id = None
        prior_ids = {
            row.get("tick_id")
            for row in worker_tick_store.recent_ticks(5, channel_id="morning")
            if row.get("tick_id")
        }

        async def _run():
            # Mark today's cron slot consumed so a still-pending grace window
            # cannot double-fire while this manual run is in flight. Manual
            # re-triggers remain allowed via this same entry point.
            today = datetime.now(CET).strftime("%Y-%m-%d")
            self._mark_fired("_last_morning", today)
            log.info("Morning routine starting (manual trigger)...")
            await self._morning_routine()

        self._manual_morning_future = asyncio.run_coroutine_threadsafe(
            _run(), self._loop
        )

        tick_id = None
        deadline = time.time() + 8.0
        while time.time() < deadline:
            for row in worker_tick_store.recent_ticks(5, channel_id="morning"):
                candidate = row.get("tick_id")
                if (
                    candidate
                    and candidate not in prior_ids
                    and row.get("state") == "running"
                ):
                    tick_id = candidate
                    self._manual_morning_tick_id = tick_id
                    break
            if tick_id or self._manual_morning_future.done():
                break
            time.sleep(0.12)
        return {"started": True, "tick_id": tick_id}

    def set_heartbeat(self, enabled: bool, interval: int | None = None,
                      prompt: str | None = None):
        """Enable/disable heartbeat, optionally change interval + prompt. Thread-safe.

        DISABLING: does NOT cancel the in-flight task. This matters because the
        agent may call this endpoint from inside her own heartbeat tick (e.g.
        "task complete → disable myself"). Cancelling would kill the in-progress
        agent.respond() and the final message would never reach Discord. Instead
        we just flip the flag; the loop's `while enabled` check exits on the
        next iteration.

        ENABLING / changing interval: cancel old task, start a new one. A custom
        `prompt` (if given) is stored and used in place of the default — this is
        how a heartbeat is turned into a task-monitor.
        """
        if interval is not None and interval in VALID_INTERVALS:
            self.heartbeat_interval = interval

        # A prompt explicitly passed (even empty) updates state; None leaves it.
        if prompt is not None:
            self.heartbeat_prompt = prompt or None

        self.heartbeat_enabled = enabled
        self._save_state()

        if not enabled:
            log.info("Heartbeat DISABLED (in-flight tick, if any, will complete and deliver)")
            return

        # Enabling or re-enabling: cancel stale task, start fresh
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

        if self._loop and self._loop.is_running():
            # Called from a non-async thread (e.g. Flask) — schedule onto the main loop
            future = asyncio.run_coroutine_threadsafe(
                self._heartbeat_loop(), self._loop
            )
            self._heartbeat_task = future
            log.info(f"Heartbeat ENABLED (every {self.heartbeat_interval}m) [cross-thread]")
        else:
            # Called from within the event loop (e.g. from start() or goodnight)
            try:
                loop = asyncio.get_running_loop()
                self._heartbeat_task = loop.create_task(self._heartbeat_loop())
                log.info(f"Heartbeat ENABLED (every {self.heartbeat_interval}m)")
            except RuntimeError:
                log.warning("Heartbeat requested but no event loop available")

    def rest(self):
        """REST command — disable heartbeat. Called verbally or at goodnight."""
        self.set_heartbeat(enabled=False)
        log.info("Galadriel is at REST. Heartbeat disabled.")

    # ── One-shot wake ────────────────────────────────────────────

    def arm_wake(self, prompt: str):
        """Arm a single restart-surviving wake.

        Unlike the heartbeat, this fires EXACTLY ONCE and is cleared only after
        the resulting Discord message is delivered. It is the correct mechanism
        for "resume me after I restart myself" — it does not depend on, and is
        not killed by, heartbeat state. Persisted immediately so a restart
        between arming and firing still honours it.

        Pass an empty/falsey prompt to disarm.
        """
        self.pending_wake = prompt or None
        self._save_state()
        if not self.pending_wake:
            log.info("Wake disarmed.")
            return
        log.info("One-shot wake ARMED (will fire once on next scheduler loop).")
        # If the scheduler is already running, kick the wake loop now so the
        # wake fires promptly rather than waiting for a restart. arm_wake() may
        # be called from a non-async thread (e.g. a Flask request handler),
        # which has no current event loop — so we MUST schedule onto the
        # captured main loop via run_coroutine_threadsafe rather than
        # asyncio.ensure_future (the latter raises "no current event loop").
        already_running = self._wake_task and not self._wake_task.done()
        if self._loop and self._loop.is_running() and not already_running:
            self._wake_task = asyncio.run_coroutine_threadsafe(
                self._wake_loop(), self._loop
            )

    # ── Start all cron loops ─────────────────────────────────────

    def start(self):
        """Start all scheduler loops. Call once from the async event loop."""
        log.info("Scheduler starting...")

        # Capture the running event loop so Flask threads can schedule onto it
        self._loop = asyncio.get_event_loop()
        conversation_queue = getattr(self.agent, "conversation_queue", None)
        if conversation_queue is not None:
            conversation_queue.start()

        # Mine shutdown-staged archives in the background — conversation buffers
        # are restored from disk on startup, so this no longer blocks first reply.
        try:
            from harness import palace
            palace.schedule_mine_pending_shutdown_archives()
        except Exception as e:
            log.warning(f"Could not schedule background palace mine: {e}")
        try:
            from harness.conversation_run_store import mark_active_runs_interrupted
            from harness.memory_sync import drain_outbox, drain_slack_observations

            async def _reconcile_conversation_runs():
                await mark_active_runs_interrupted()
                mined = await drain_outbox()
                if mined:
                    log.info(f"Reconciled {mined} pending conversation archive batch(es).")
                slack_mined = await drain_slack_observations()
                if slack_mined:
                    log.info(
                        "Reconciled %d Slack observation archive batch(es).",
                        slack_mined,
                    )

            asyncio.ensure_future(_reconcile_conversation_runs())
        except Exception as e:
            log.warning(f"Could not reconcile conversation run state: {e}")

        # Always start morning + goodnight watchers (times re-read each poll)
        self._morning_task = asyncio.ensure_future(self._cron_loop(
            name="morning",
            time_attr="morning_time",
            callback=self._morning_routine,
            workday_only=True,
        ))
        self._goodnight_task = asyncio.ensure_future(self._cron_loop(
            name="goodnight",
            time_attr="goodnight_time",
            callback=self._goodnight_routine,
            workday_only=False,
        ))

        # Ambient reflection loop (opt-out via env)
        if os.environ.get("GALADRIEL_REFLECTION", "1") != "0":
            self._reflection_task = asyncio.ensure_future(self._reflection_loop())

        # Start heartbeat if it was enabled (persisted state)
        if self.heartbeat_enabled:
            self._heartbeat_task = asyncio.ensure_future(self._heartbeat_loop())
            log.info(f"Heartbeat resumed from saved state (every {self.heartbeat_interval}m)")

        # Fire a one-shot wake if one was armed before (this) restart.
        if self.pending_wake:
            self._wake_task = asyncio.ensure_future(self._wake_loop())
            log.info("One-shot wake pending from saved state — will fire shortly.")

        # Downtime catch-up: if the process was down across the morning slot, the
        # morning planning never ran and the cron loop above would otherwise just
        # mark it fired-today (past its 5-min grace) and silently skip it. Decide
        # SYNCHRONOUSLY here — before any loop gets the event loop and can mark the
        # tracker — based on the persisted `_last_morning`. Morning only: a missed
        # reflection slot won't re-fire (persisted fired-set) and a missed goodnight
        # is not replayed (no 2am "good night").
        now_cet = datetime.now(CET)
        today_str = now_cet.strftime("%Y-%m-%d")
        morning_dt = now_cet.replace(
            hour=self.morning_time.hour,
            minute=self.morning_time.minute,
            second=0,
            microsecond=0,
        )
        # Start the window PAST the cron's 5-min grace: within grace the normal
        # morning cron still fires the routine itself, so a catch-up there would
        # double-fire. Catch-up only covers the post-grace gap the cron skips.
        # No upper bound other than "still today" — if the process is down all
        # day and only restarts after goodnight, the planning ritual must still
        # run today rather than being silently marked done-without-running by
        # the plain cron loop's stale-skip branch below.
        catchup_from = morning_dt + timedelta(minutes=5)
        if (
            now_cet.weekday() < 5
            and self._last_morning != today_str
            and catchup_from <= now_cet
        ):
            self._catchup_task = asyncio.ensure_future(self._catchup_loop())
            log.info("Downtime catch-up: morning planning missed today — will run shortly after boot.")

        log.info("Scheduler running.")

    # ── One-shot Wake Loop ───────────────────────────────────────

    async def _wake_loop(self):
        """Fire the armed one-shot wake exactly once, then clear it.

        Ordering is deliberate and crash-safe:
          1. Snapshot the prompt and confirm the bot/DM path is ready.
          2. Deliver via _send_agent_message (a normal agent turn → Discord).
          3. ONLY on successful delivery, clear pending_wake and persist.
        If delivery raises (or the process dies mid-flight), pending_wake stays
        armed in the state file and re-fires on the next startup. A wake is
        never silently lost.
        """
        try:
            # Small grace so the Discord gateway/DM channel is ready after boot.
            await asyncio.sleep(8)
            prompt = self.pending_wake
            if not prompt:
                return
            log.info("One-shot wake FIRING.")
            ok = await self._send_agent_message(prompt=prompt, channel_id="wake")
            if ok:
                # Delivered (or legitimately silent) — clear so it never repeats.
                self.pending_wake = None
                self._save_state()
                await self._checkpoint("wake")
                log.info("One-shot wake delivered and cleared.")
            else:
                # Delivery raised — leave armed; next startup retries.
                log.warning("One-shot wake delivery failed; left armed for retry on next startup.")
        except asyncio.CancelledError:
            log.info("Wake loop cancelled (pending_wake left armed).")
        except Exception as e:
            # Leave pending_wake armed — it will retry on next startup.
            log.exception(f"Wake loop error (left armed for retry): {e}")

    # ── Downtime Catch-up Loop ───────────────────────────────────

    async def _catchup_loop(self):
        """Run the missed morning planning once, shortly after boot.

        Only scheduled by start() when the process was down across the morning
        slot. A small grace lets the Discord gateway/DM channel come up before
        the catch-up turn tries to deliver.
        """
        try:
            await asyncio.sleep(8)
            await self._catchup_routine()
        except asyncio.CancelledError:
            log.info("Catch-up loop cancelled.")
        except Exception as e:
            log.exception(f"Catch-up loop error: {e}")

    # ── Heartbeat Loop ───────────────────────────────────────────

    async def _heartbeat_loop(self):
        """Periodic heartbeat — self-initiated check-in (or task monitor)."""
        try:
            while self.heartbeat_enabled:
                await asyncio.sleep(self.heartbeat_interval * 60)
                if not self.heartbeat_enabled:
                    break

                prompt = self.heartbeat_prompt or DEFAULT_HEARTBEAT_PROMPT
                log.info(f"Heartbeat firing... (prompt: {'custom' if self.heartbeat_prompt else 'default'})")
                await self._send_agent_message(
                    prompt=prompt,
                    channel_id="heartbeat",
                )
                # Heartbeats are frequent — only checkpoint-mine the heartbeat
                # channel once per hour, not every tick.
                now = datetime.now()
                if (
                    self._last_heartbeat_checkpoint is None
                    or (now - self._last_heartbeat_checkpoint) >= HEARTBEAT_CHECKPOINT_MIN_GAP
                ):
                    await self._checkpoint("heartbeat")
                    self._last_heartbeat_checkpoint = now
        except asyncio.CancelledError:
            log.info("Heartbeat loop cancelled.")
        except Exception as e:
            log.exception(f"Heartbeat loop error: {e}")

    # ── Cron Loop ────────────────────────────────────────────────

    def _mark_fired(self, tracker: str, today_str: str) -> None:
        """Set a daily fire-tracker and persist it, so a restart knows the
        routine already ran today (no re-fire, no silent skip of a missed one)."""
        setattr(self, tracker, today_str)
        self._save_state()

    async def _cron_loop(self, name: str, time_attr: str, callback, workday_only: bool):
        """Generic cron-style loop that fires a callback once per day at a CET time.

        ``time_attr`` is an instance attribute (e.g. ``morning_time``) re-read
        each poll so Tower UI changes take effect within ~30s without restart.
        """
        try:
            while True:
                now = datetime.now(CET)
                today_str = now.strftime("%Y-%m-%d")
                tracker = f"_last_{name}"
                target_time = getattr(self, time_attr)

                # Build today's target datetime
                target_dt = now.replace(
                    hour=target_time.hour,
                    minute=target_time.minute,
                    second=0,
                    microsecond=0,
                )

                already_fired = getattr(self, tracker) == today_str

                if now >= target_dt or already_fired:
                    if not already_fired and now >= target_dt:
                        # We passed the time but haven't fired — fire now if conditions met
                        # Only if we're within 5 minutes of the target (avoid firing hours late)
                        diff = (now - target_dt).total_seconds()
                        if diff < 300:  # 5 min grace
                            if not (workday_only and now.weekday() >= 5):
                                log.info(f"Cron [{name}]: FIRING (within grace period)")
                                self._mark_fired(tracker, today_str)
                                await callback()
                                continue
                        self._mark_fired(tracker, today_str)

                    # Sleep until next check (every 30s for precision)
                    await asyncio.sleep(30)
                    continue

                # Future target: poll every 30s so mid-day time edits are picked up
                seconds_to_wait = (target_dt - now).total_seconds()
                if seconds_to_wait > 55:
                    log.info(
                        f"Cron [{name}]: waiting {seconds_to_wait:.0f}s until "
                        f"{_format_hhmm(target_time)} CET"
                    )
                await asyncio.sleep(min(30, max(1, seconds_to_wait)))

                # Re-check after sleep (time_attr may have changed)
                now = datetime.now(CET)
                today_str = now.strftime("%Y-%m-%d")
                target_time = getattr(self, time_attr)
                target_dt = now.replace(
                    hour=target_time.hour,
                    minute=target_time.minute,
                    second=0,
                    microsecond=0,
                )

                if getattr(self, tracker) == today_str:
                    continue

                if now < target_dt:
                    continue

                if workday_only and now.weekday() >= 5:
                    log.info(f"Cron [{name}]: skipping — weekend")
                    self._mark_fired(tracker, today_str)
                    continue

                # Within grace only — if the user moved the time into the past
                # by hours, mark fired without running (same as original stale-skip).
                if (now - target_dt).total_seconds() >= 300:
                    self._mark_fired(tracker, today_str)
                    continue

                log.info(f"Cron [{name}]: FIRING")
                self._mark_fired(tracker, today_str)
                await callback()

        except asyncio.CancelledError:
            log.info(f"Cron [{name}] loop cancelled.")
        except Exception as e:
            log.exception(f"Cron [{name}] loop error: {e}")

    # ── Ambient Reflection Loop ──────────────────────────────────

    async def _reflection_loop(self):
        """Ambient cognition — reflection + worker audit at a cadence.

        Fires at each time in REFLECTION_TIMES across the active window,
        workdays only. Unlike _cron_loop (once per day), this tracks each
        (date, slot) so all slots fire once. Posts a brief status summary to
        the user; files to the palace; audits the worker and may pause it.
        """
        try:
            while True:
                now = datetime.now(CET)
                today_str = now.strftime("%Y-%m-%d")

                # Weekend: skip, but keep looping (cheap poll).
                if now.weekday() >= 5:
                    await asyncio.sleep(300)
                    continue

                for slot in REFLECTION_TIMES:
                    key = f"{today_str}:{slot.hour:02d}{slot.minute:02d}"
                    if key in self._fired_reflections:
                        continue
                    target_dt = now.replace(
                        hour=slot.hour, minute=slot.minute,
                        second=0, microsecond=0,
                    )
                    # Fire if we're at/past the slot but within a 10-min grace.
                    if now >= target_dt and (now - target_dt).total_seconds() < 600:
                        self._fired_reflections.add(key)
                        self._save_state()  # persist so a restart won't re-fire this slot
                        log.info(f"Reflection [{key}]: firing")
                        await self._reflection_routine()

                # Trim the fired-set so it doesn't grow unbounded.
                if len(self._fired_reflections) > 32:
                    self._fired_reflections = {
                        k for k in self._fired_reflections if k.startswith(today_str)
                    }

                await asyncio.sleep(60)
        except asyncio.CancelledError:
            log.info("Reflection loop cancelled.")
        except Exception as e:
            log.exception(f"Reflection loop error: {e}")

    # ── Routines ─────────────────────────────────────────────────

    async def _morning_routine(self):
        """Morning greeting + daily planning — workday morning slot (CET)."""
        log.info("Morning routine starting...")
        today = datetime.now(CET).strftime("%Y-%m-%d")
        await self._send_agent_message(prompt=_morning_prompt(today), channel_id="morning")
        await self._checkpoint("morning")

    async def _catchup_routine(self):
        """Catch-up morning planning after downtime — see _catchup_prompt().

        Scheduled by start() only when the process was down across the morning
        slot, so today's planning never ran. Reconciles what was missed, then
        does the morning planning. Marks morning fired-today (persisted) only on
        success, so a delivery failure can retry on the next boot.
        """
        log.info("Catch-up routine starting (missed morning planning)...")
        today = datetime.now(CET).strftime("%Y-%m-%d")
        ok = await self._send_agent_message(prompt=_catchup_prompt(today), channel_id="morning")
        if ok:
            self._mark_fired("_last_morning", datetime.now(CET).strftime("%Y-%m-%d"))
            await self._checkpoint("morning")
            log.info("Catch-up routine complete.")
        else:
            log.warning("Catch-up delivery failed; left for retry on next boot.")

    async def _reflection_routine(self):
        """Ambient reflection + retro + worker audit — per slot in REFLECTION_TIMES, workdays.

        The agent thinks privately, files anything worth keeping to the palace,
        distills lessons, and AUDITS the background worker against the cookbooks
        + guardrails.         It steers by appending to `state/steering.md` (which the
        worker and morning planner read), and ALWAYS posts a brief worker-status
        summary to the user (a forced-silent turn is unreliable, so the spoken
        output is made useful instead). It additionally pauses the worker via
        `state/worker_control.md` when it finds the worker doing something bad or
        consistently misbehaving (PART 3).
        """
        log.info("Reflection routine starting...")
        # Mine the live user conversation(s) up to now BEFORE reflecting, so the
        # reflection turn (and its palace_search) sees the latest state. Blocking
        # — reflection only starts once the checkpoint mine has finished. This is
        # also what closes the "idle conversation never mined" gap: reflection
        # runs 4x/workday, so any active chat gets mined regularly regardless of
        # whether it ever hit the compaction threshold.
        await self._checkpoint_user_conversations()
        today = datetime.now(CET).strftime("%Y-%m-%d")
        await self._send_agent_message(
            prompt=_reflection_prompt(today),
            channel_id="reflection",
        )
        await self._checkpoint("reflection")

    async def _goodnight_routine(self):
        """Goodnight — 21:00 CET, then REST.

        Daily markdown logs stay as the hot dynamic index only. The agent's
        goodnight prompt files the durable recap to palace room=episodes.
        """
        log.info("Goodnight routine starting...")
        today = datetime.now(CET).strftime("%Y-%m-%d")
        await self._send_agent_message(
            prompt=_goodnight_prompt(today),
            channel_id="goodnight",
        )
        await self._checkpoint("goodnight")
        # Disable heartbeat
        self.rest()

    # ── Palace checkpointing ─────────────────────────────────────

    async def _checkpoint(self, channel_id: str) -> None:
        """Non-destructively mine a channel's new messages to the palace.

        Thin wrapper over agent.checkpoint_channel — never raises, so a mining
        hiccup can't break a scheduler routine.
        """
        try:
            await self.agent.checkpoint_channel(channel_id)
        except Exception as e:
            log.warning(f"Checkpoint failed for channel {channel_id}: {e}")

    async def _checkpoint_user_conversations(self) -> None:
        """Checkpoint-mine every live user conversation (non-scheduler channels).

        Called (blocking) before reflection so the agent's own scheduler channels
        — handled by their own routines — are skipped to avoid double work.
        """
        try:
            from .memory_sync import drain_outbox, drain_slack_observations
            await drain_outbox()
            await drain_slack_observations()
        except Exception as e:
            log.warning(f"Memory outbox drain failed: {e}")
        for cid in list(self.agent.conversations.keys()):
            if cid in SCHEDULER_CHANNELS:
                continue
            await self._checkpoint(cid)

    # ── Message Delivery ─────────────────────────────────────────

    async def _send_agent_message(self, prompt: str, channel_id: str) -> bool:
        """Have the agent generate a response and send it to Discord.

        If the agent returns an empty response (Claude end_turn with no text —
        a legitimate "nothing to add" state), we log it and skip the Discord
        send. Heartbeats that have nothing to report stay silent rather than
        spamming a placeholder into the DM.

        Returns True if the turn completed without raising (delivered OR
        legitimately silent), False if the agent/delivery raised. The one-shot
        wake loop uses this return to decide whether to clear or keep
        pending_wake armed for retry.
        """
        cq = self.agent.conversation_queue
        emit = cq.open_stream(channel_id)
        try:
            response = await self.agent.respond(
                prompt, channel_id=channel_id, emit=emit,
            )
            if not response.strip():
                log.info(f"Scheduler [{channel_id}] silent tick — nothing to report, skipping send")
                return True
            log.info(f"Scheduler [{channel_id}] response: {response[:100]}...")

            # Send to Discord
            await self._send_to_discord(response)
            return True

        except Exception as e:
            log.exception(f"Scheduler [{channel_id}] error: {e}")
            return False
        finally:
            cq.close_stream(channel_id)

    async def _send_agent_silent(self, prompt: str, channel_id: str) -> str:
        """Run a prompt for its side effects (palace filing) only.

        The response text is never sent to Discord, but it IS returned to the
        caller so a routine could parse a structured trailer out of it if
        needed. Used by ambient reflection: the agent's bookkeeping persists,
        but nothing is spoken.
        """
        cq = self.agent.conversation_queue
        emit = cq.open_stream(channel_id)
        try:
            resp = await self.agent.respond(
                prompt, channel_id=channel_id, emit=emit,
            )
            log.info(f"Scheduler [{channel_id}] silent routine complete (not sent to Discord)")
            return resp or ""
        except Exception as e:
            log.exception(f"Scheduler [{channel_id}] silent error: {e}")
            return ""
        finally:
            cq.close_stream(channel_id)

    async def _send_to_discord(self, message: str):
        """Send a message to the authorized user via DM (or configured channel).

        Uses bot.get_dm_channel() which handles DM channel resolution
        correctly — DM channels aren't in the bot cache at startup,
        so we fall back to fetch_user() + create_dm().
        """
        if not self.bot:
            log.warning("No Discord bot available for scheduler message.")
            return

        # Use the DM-safe helper attached to the bot by discord_bot/bot.py
        if hasattr(self.bot, 'get_dm_channel'):
            channel = await self.bot.get_dm_channel()
        else:
            # Fallback if bot doesn't have the helper (shouldn't happen)
            channel_id = int(os.environ.get("DISCORD_CHANNEL_ID", "0"))
            channel = self.bot.get_channel(channel_id) if channel_id else None

        if not channel:
            log.warning("Could not resolve Discord channel for scheduler message.")
            return

        # Chunk long messages
        max_len = 1900
        chunks = []
        text = message
        while text:
            if len(text) <= max_len:
                chunks.append(text)
                break
            split_at = text.rfind("\n", 0, max_len)
            if split_at == -1:
                split_at = max_len
            chunks.append(text[:split_at])
            text = text[split_at:].lstrip("\n")

        for chunk in chunks:
            await channel.send(chunk)
            log.info(f"Scheduler message sent ({len(chunk)} chars) to channel {channel.id}")

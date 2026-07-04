"""Completion Watcher — reports when external/detached shell processes finish.

Long shell processes the agent launches but cannot `await` in one turn (e.g.
narration pipelines, batch jobs) write a JSON completion marker to
/tmp/galadriel-jobs/ when they finish. This watcher polls for those markers and
pushes a Discord notification through the agent.

This is DISTINCT from the agent's own job *board* (`jobs/` + `state/`, the
background worker — see config/CONTEXT.md §5). This watcher only reports the
completion of out-of-band shell processes; it does not pick or perform work.

Architecture:
  - Marker dir: /tmp/galadriel-jobs/  (legacy path, kept as an external contract:
    scripts the agent writes drop markers here)
  - Each process writes <name>.done with JSON status on completion
  - Watcher polls every 15 seconds
  - On detection: reads marker, formats message, sends via agent+Discord, archives marker
"""

import asyncio
import json
import logging
from pathlib import Path

from .loop_prompts import process_complete_prompt

log = logging.getLogger("galadriel.completion_watcher")

MARKER_DIR = Path("/tmp/galadriel-jobs")
POLL_INTERVAL = 15  # seconds
MARKER_SUFFIX = ".done"


class CompletionWatcher:
    """Watches for shell-process completion markers and notifies via Discord."""

    def __init__(self, agent, discord_bot=None):
        self.agent = agent
        self.bot = discord_bot
        self._task: asyncio.Task | None = None

    def set_bot(self, bot):
        self.bot = bot

    def start(self):
        """Start the watcher loop. Call from an async context."""
        MARKER_DIR.mkdir(parents=True, exist_ok=True)
        self._task = asyncio.ensure_future(self._watch_loop())
        log.info(f"Completion watcher started — monitoring {MARKER_DIR}")

    async def _watch_loop(self):
        """Main polling loop."""
        try:
            while True:
                await asyncio.sleep(POLL_INTERVAL)
                await self._check_markers()
        except asyncio.CancelledError:
            log.info("Completion watcher cancelled.")
        except Exception as e:
            log.exception(f"Completion watcher error: {e}")

    async def _check_markers(self):
        """Scan for .done marker files and process them."""
        if not MARKER_DIR.exists():
            return

        for marker_path in MARKER_DIR.glob(f"*{MARKER_SUFFIX}"):
            try:
                data = json.loads(marker_path.read_text())
                log.info(f"Process completion detected: {marker_path.name} — {data.get('status', 'UNKNOWN')}")

                # Format the notification
                message = self._format_notification(data)

                # Send through agent (so it gets logged and contextualized)
                await self._notify(data, message)

                # Archive the marker (move to .processed)
                archive_path = marker_path.with_suffix(".processed")
                marker_path.rename(archive_path)
                log.info(f"Marker archived: {archive_path}")

            except json.JSONDecodeError as e:
                log.warning(f"Invalid JSON in marker {marker_path}: {e}")
                # Move bad marker out of the way
                marker_path.rename(marker_path.with_suffix(".bad"))
            except Exception as e:
                log.exception(f"Error processing marker {marker_path}: {e}")

    def _format_notification(self, data: dict) -> str:
        """Format completion data into a Discord-friendly message."""
        job = data.get("job", "unknown")
        status = data.get("status", "UNKNOWN")

        if status == "SUCCESS":
            success = data.get("success_count", "?")
            failed = data.get("failed_count", 0)
            elapsed = data.get("elapsed_human", "?")
            voice = data.get("voice", "?")
            engine = data.get("engine", "?")

            msg = (
                f"🎙️ **Narration Job Complete — {job}**\n\n"
                f"✅ **{success}** chunks narrated successfully\n"
            )
            if int(failed) > 0:
                msg += f"❌ **{failed}** chunks failed\n"
            msg += (
                f"⏱️ Duration: **{elapsed}**\n"
                f"🗣️ Voice: {voice} ({engine})\n"
                f"📋 Log: `{data.get('log_file', 'N/A')}`\n"
                f"🕐 Completed: {data.get('completed_at', 'N/A')}"
            )
            return msg

        elif status == "FAILED":
            return (
                f"🔴 **Narration Job FAILED — {job}**\n\n"
                f"Exit code: {data.get('exit_code', '?')}\n"
                f"⏱️ Duration: {data.get('elapsed_seconds', '?')}s\n"
                f"📋 Log: `{data.get('log_file', 'N/A')}`\n"
                f"🕐 Failed at: {data.get('completed_at', 'N/A')}"
            )
        else:
            return f"📦 **Process completed — {job}** — Status: {status}\n```json\n{json.dumps(data, indent=2)}\n```"

    async def _notify(self, data: dict, formatted_message: str):
        """Send notification via agent (gets intelligent commentary) then to Discord."""
        status = data.get("status", "UNKNOWN")
        job = data.get("job", "unknown")

        # Have the agent generate a contextual response
        prompt = process_complete_prompt(data)

        try:
            response = await self.agent.respond(prompt, channel_id="completions")
            await self._send_to_discord(response)
        except Exception as e:
            log.exception(f"Failed to generate agent response for process {job}: {e}")
            # Fallback: send the raw formatted message
            await self._send_to_discord(formatted_message)

    async def _send_to_discord(self, message: str):
        """Send message to Discord via the bot."""
        if not self.bot:
            log.warning("No Discord bot available for completion notification.")
            return

        # Use the DM-safe helper
        if hasattr(self.bot, 'get_dm_channel'):
            channel = await self.bot.get_dm_channel()
        else:
            import os
            channel_id = int(os.environ.get("DISCORD_CHANNEL_ID", "0"))
            channel = self.bot.get_channel(channel_id) if channel_id else None

        if not channel:
            log.warning("Could not resolve Discord channel for completion notification.")
            return

        # Chunk long messages
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

        log.info(f"Completion notification sent to Discord ({len(message)} chars)")

"""Slack gateway — relays messages between Slack and the GaladrielAgent.

Unlike the Discord gateway (one manually-configured channel, one authorized
user), the Slack bot's channel is fixed via `SLACK_CHANNEL_ID` (same pattern
as Discord's `DISCORD_CHANNEL_ID`) but any member of that channel can talk to
it — it's meant to sit in a shared team channel as a teammate, not a private
assistant.

Runs over Socket Mode (outbound-only websocket) so no public webhook/URL or
signing secret is needed — consistent with this project's "no public ports"
security posture.
"""
import random
import os
import re
import logging
import asyncio

from slack_bolt.app.async_app import AsyncApp

from harness.agent import GaladrielAgent, MAIN_CHANNEL_ID
from harness.error_humanizer import humanize_anthropic_error
from discord_bot.bot import _format_status_report

log = logging.getLogger("galadriel.slack")

# Slack messages can be much longer than Discord's, but keep the same
# soft-cap for consistent chunking behaviour / UX.
MAX_SLACK_LENGTH = 3900

APPROVAL_TIMEOUT_SEC = 30.0


def chunk_message(text: str) -> list[str]:
    """Split a long message into Slack-safe chunks (mirrors discord_bot's chunker)."""
    if len(text) <= MAX_SLACK_LENGTH:
        return [text]

    chunks = []
    while text:
        if len(text) <= MAX_SLACK_LENGTH:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, MAX_SLACK_LENGTH)
        if split_at == -1:
            split_at = MAX_SLACK_LENGTH
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


def _slack_markdown(text: str) -> str:
    """Convert the Discord-flavoured `**bold**` used by _format_status_report
    into Slack mrkdwn's `*bold*` so /status renders correctly."""
    return re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)


class SlackChannel:
    """Sendable handle for the configured team channel. Duck-types the
    `.send(text)` / `.id` interface `harness/scheduler.py` and
    `harness/worker.py` already expect from `bot.get_dm_channel()` — Slack has
    no DM push target by design, so this always resolves to that one channel
    instead."""

    def __init__(self, client, channel_id: str):
        self._client = client
        self.id = channel_id

    async def send(self, text: str) -> None:
        for chunk in chunk_message(text):
            await self._client.chat_postMessage(channel=self.id, text=chunk)


def create_bot(agent: GaladrielAgent, scheduler=None) -> AsyncApp:
    """Create and configure the Slack Bolt app (registers handlers only —
    call `start_slack_bot()` to actually connect)."""
    app = AsyncApp(token=os.environ["SLACK_BOT_TOKEN"])

    state = {
        "bot_user_id": None,
        "channel_id": os.environ.get("SLACK_CHANNEL_ID") or None,
        "channel_name": None,
        "members": {},  # user_id -> display name (cache)
        "processed_ts": set(),  # message ts already handled — Slack can deliver
                                 # both a "message" and an "app_mention" event for
                                 # the same mention; this dedupes a double reply
    }
    pending_approvals: dict[str, dict] = {}  # command -> {future, dedup_count, channel, ts}

    def agent_channel_id() -> str | None:
        """The agent-facing channel bucket — MAIN_CHANNEL_ID, shared with
        Discord/Tower so the agent has one continuous conversation regardless
        of surface. None if Slack isn't configured (no channel to talk in)."""
        if not state["channel_id"]:
            return None
        return MAIN_CHANNEL_ID

    async def get_dm_channel():
        """Duck-typed for scheduler/worker/completion_watcher — returns the
        one configured team channel (there is no Slack DM push target here)."""
        if not state["channel_id"]:
            return None
        return SlackChannel(app.client, state["channel_id"])

    app.get_dm_channel = get_dm_channel

    def set_bot_identity(user_id: str) -> None:
        state["bot_user_id"] = user_id

    app.set_bot_identity = set_bot_identity

    async def _resolve_name(user_id: str) -> str:
        if user_id in state["members"]:
            return state["members"][user_id]
        name = user_id
        try:
            info = await app.client.users_info(user=user_id)
            profile = info["user"].get("profile", {})
            name = profile.get("display_name") or info["user"].get("real_name") or user_id
        except Exception as e:
            log.warning(f"Could not resolve Slack user {user_id}: {e}")
        state["members"][user_id] = name
        return name

    async def _refresh_roster() -> None:
        """Rebuild the team-roster system context and push it into the agent.
        Called at startup and on membership changes only — never per-message,
        so the prompt-cache-stable prefix doesn't thrash."""
        channel_id = state["channel_id"]
        if not channel_id:
            return

        if state["channel_name"] is None:
            try:
                info = await app.client.conversations_info(channel=channel_id)
                state["channel_name"] = info["channel"].get("name", channel_id)
            except Exception as e:
                log.warning(f"Could not resolve Slack channel name for {channel_id}: {e}")
                state["channel_name"] = channel_id

        try:
            resp = await app.client.conversations_members(channel=channel_id)
            member_ids = [m for m in resp["members"] if m != state["bot_user_id"]]
        except Exception as e:
            log.warning(f"Could not list Slack channel members: {e}")
            return

        names = [await _resolve_name(uid) for uid in member_ids]
        roster_text = (
            f"# Slack team channel: #{state['channel_name']}\n"
            "This context applies only to messages tagged `[Slack/<name>]:` — those "
            "come from a shared team Slack channel, not a private assistant DM. "
            "(Messages tagged `[Discord]:` or `[Tower]:` in this same conversation "
            "come from the primary user directly — treat those normally.)\n"
            f"Current members who can talk to you on Slack: {', '.join(names) if names else '(none yet)'}.\n"
            'Every incoming Slack message is prefixed with the sender\'s name, e.g. "[Slack/Priya Sharma]: ...".\n'
            "Address the person who actually spoke — don't assume continuity of a single \"the user\" across Slack messages."
        )
        agent.set_channel_context(agent_channel_id(), roster_text)

    app.refresh_roster = _refresh_roster

    # ── Roster upkeep on membership changes ─────────────────────

    @app.event("member_joined_channel")
    async def handle_member_joined(event, client):
        if event.get("channel") != state["channel_id"] or event.get("user") == state["bot_user_id"]:
            return
        await _refresh_roster()

    @app.event("member_left_channel")
    async def handle_member_left(event, client):
        if event.get("channel") != state["channel_id"] or event.get("user") == state["bot_user_id"]:
            return
        state["members"].pop(event.get("user"), None)
        await _refresh_roster()

    # ── Message relay — any member, but only when @mentioned ────
    #
    # Slack can deliver a mention as a "message" event, an "app_mention"
    # event, or both (depending on which the app is subscribed to) — both
    # handlers below funnel into this one function, deduped by message `ts`.

    async def _handle_incoming(event, client):
        if event.get("subtype") is not None or event.get("bot_id"):
            return

        channel_id = event.get("channel")
        if channel_id != state["channel_id"] or not state["bot_user_id"]:
            return

        user_id = event.get("user")
        if not user_id or user_id == state["bot_user_id"]:
            return

        ts = event.get("ts")
        if ts:
            if ts in state["processed_ts"]:
                return
            state["processed_ts"].add(ts)

        text = event.get("text", "") or ""
        mention_tag = f"<@{state['bot_user_id']}>"
        if mention_tag not in text:
            return

        clean_text = text.replace(mention_tag, "").strip()
        if not clean_text:
            return

        display_name = await _resolve_name(user_id)
        # Tagged with the surface — the conversation is shared with
        # Discord/Tower (see MAIN_CHANNEL_ID), so the agent needs to know
        # which surface a message came from.
        user_input = f"[Slack/{display_name}]: {clean_text}"

        # Slack bots have no native "typing…" indicator (that's a legacy RTM
        # feature for user clients, not app bots) — post a placeholder and
        # edit it in place once the real answer is ready. Best-effort: a
        # failure here must never block the actual response.
        thinking_ts = None
        try:
            placeholder = await client.chat_postMessage(channel=channel_id, text="_thinking…_")
            thinking_ts = placeholder["ts"]
        except Exception as e:
            log.warning(f"Could not post thinking placeholder: {e}")

        log.info(f"📥 Processing Slack message from {display_name} in {channel_id}: {clean_text[:80]}")
        try:
            response = await agent.respond(user_input, channel_id=agent_channel_id())
            if not response.strip():
                response = "🌙 *(nothing to add — acknowledged.)*"
        except Exception as e:
            log.exception("Error processing Slack message")
            response = humanize_anthropic_error(e) or f"⚠️ Something went wrong: `{e}`"

        chunks = chunk_message(response)
        if thinking_ts:
            try:
                await client.chat_update(channel=channel_id, ts=thinking_ts, text=chunks[0])
                chunks = chunks[1:]
            except Exception as e:
                log.warning(f"Could not update thinking placeholder: {e}")
        for chunk in chunks:
            await client.chat_postMessage(channel=channel_id, text=chunk)

    @app.event("message")
    async def handle_message(event, client):
        await _handle_incoming(event, client)

    @app.event("app_mention")
    async def handle_app_mention(event, client):
        await _handle_incoming(event, client)

    # ── Approvals — Block Kit buttons, any member may click ─────

    async def approval_callback(command: str, tier: str) -> bool:
        if not state["channel_id"]:
            return False

        existing = pending_approvals.get(command)
        if existing is not None and not existing["future"].done():
            existing["dedup_count"] += 1
            log.info(f"Dedup approval ({existing['dedup_count'] + 1}× for same command): {command[:80]}")
            return await existing["future"]

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"🔴 *Approval required*\n```{command}```\nAny member can approve or deny. ({int(APPROVAL_TIMEOUT_SEC)}s → denied)",
                },
            },
            {
                "type": "actions",
                "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "✅ Approve"}, "style": "primary", "action_id": "approve", "value": command},
                    {"type": "button", "text": {"type": "plain_text", "text": "❌ Deny"}, "style": "danger", "action_id": "deny", "value": command},
                ],
            },
        ]
        posted = await app.client.chat_postMessage(
            channel=state["channel_id"], text=f"Approval required: {command}", blocks=blocks
        )
        entry = {"future": future, "dedup_count": 0, "channel": posted["channel"], "ts": posted["ts"]}
        pending_approvals[command] = entry

        async def _timeout():
            await asyncio.sleep(APPROVAL_TIMEOUT_SEC)
            if not future.done():
                future.set_result(False)
                try:
                    await app.client.chat_update(
                        channel=entry["channel"], ts=entry["ts"],
                        text=f"⏰ Timed out (denied): `{command}`", blocks=[],
                    )
                except Exception as e:
                    log.debug(f"Could not edit timed-out approval message: {e}")

        timeout_task = asyncio.ensure_future(_timeout())
        try:
            return await future
        finally:
            timeout_task.cancel()
            pending_approvals.pop(command, None)

    agent.approval_callback = approval_callback

    async def _resolve_approval(body: dict, client, approved: bool) -> None:
        command = body["actions"][0]["value"]
        entry = pending_approvals.get(command)
        if entry is None or entry["future"].done():
            return
        entry["future"].set_result(approved)
        prefix = "✅ Approved" if approved else "❌ Denied"
        suffix = f" (merged {entry['dedup_count'] + 1} requests)" if entry["dedup_count"] else ""
        clicker = (body.get("user") or {}).get("name") or (body.get("user") or {}).get("id", "someone")
        try:
            await client.chat_update(
                channel=entry["channel"], ts=entry["ts"],
                text=f"{prefix}{suffix} by {clicker}: `{command}`", blocks=[],
            )
        except Exception as e:
            log.warning(f"Could not update resolved approval message: {e}")

    @app.action("approve")
    async def handle_approve(ack, body, client):
        await ack()
        await _resolve_approval(body, client, True)

    @app.action("deny")
    async def handle_deny(ack, body, client):
        await ack()
        await _resolve_approval(body, client, False)

    # ── Slash commands — /new, /status, /compact parity ─────────

    def _wrong_channel(channel_id: str) -> bool:
        return channel_id != state["channel_id"]

    @app.command("/new")
    async def slash_new(ack, command, client):
        await ack()
        channel_id = command["channel_id"]
        if _wrong_channel(channel_id):
            await client.chat_postEphemeral(channel=channel_id, user=command["user_id"], text="I only work in the configured channel.")
            return
        archived = await agent.pop_and_archive_history(agent_channel_id())
        suffix = f" ({archived} msgs filed to palace)" if archived else ""
        await client.chat_postMessage(channel=channel_id, text=f"✨ Fresh start. Blank slate.{suffix}")

    @app.command("/status")
    async def slash_status(ack, command, client):
        await ack()
        channel_id = command["channel_id"]
        if _wrong_channel(channel_id):
            await client.chat_postEphemeral(channel=channel_id, user=command["user_id"], text="I only work in the configured channel.")
            return
        report = _slack_markdown(_format_status_report(agent, scheduler))
        await client.chat_postMessage(channel=channel_id, text=report)

    @app.command("/compact")
    async def slash_compact(ack, command, client):
        await ack()
        channel_id = command["channel_id"]
        if _wrong_channel(channel_id):
            await client.chat_postEphemeral(channel=channel_id, user=command["user_id"], text="I only work in the configured channel.")
            return

        cid = agent_channel_id()
        messages = agent._get_messages(cid)
        msg_count = len(messages)
        try:
            result = await agent.compact_channel(cid)
            if not result.get("compacted"):
                await client.chat_postMessage(channel=channel_id, text=f"📚 {msg_count} messages — nothing to compact.")
                return
            ratio_pct = 0
            if result["tokens_before"] > 0:
                ratio_pct = int((1 - result["tokens_after"] / result["tokens_before"]) * 100)
            await client.chat_postMessage(
                channel=channel_id,
                text=(
                    "🗜️ *Compacted to snapshot*\n"
                    f"Messages: {result['messages_before']} → 0\n"
                    f"Tokens: ~{result['tokens_before']} → ~{result['tokens_after']} (~{ratio_pct}% reduction)\n"
                    "Full conversation archived to palace. A compact snapshot is kept in context."
                ),
            )
        except Exception as e:
            log.exception("Error during Slack compaction")
            await client.chat_postMessage(channel=channel_id, text=humanize_anthropic_error(e) or f"⚠️ Compaction failed: `{e}`")

    return app


async def start_slack_bot(app: AsyncApp, scheduler=None, completion_watcher=None, worker=None) -> None:
    """Resolve the bot's identity, seed the team roster, start
    scheduler/worker/watcher, greet the configured channel, then block on the
    Socket Mode connection until the process exits."""
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

    auth = await app.client.auth_test()
    app.set_bot_identity(auth["user_id"])
    log.info(f"Connected to Slack as {auth.get('user')} (id: {auth['user_id']})")

    # Seed the team roster, then start scheduler/worker/watcher (non-blocking).
    # Conversation buffers reload from disk on agent init; shutdown archives
    # mine to the palace in the background via scheduler.start().
    channel = await app.get_dm_channel()
    if channel:
        await app.refresh_roster()
    else:
        log.warning("SLACK_CHANNEL_ID is not set — the bot will not respond anywhere until it is.")

    if scheduler:
        scheduler.start()
        log.info("Scheduler started from Slack startup.")
    if completion_watcher:
        completion_watcher.start()
        log.info("Completion watcher started from Slack startup.")
    if worker:
        worker.start()
        log.info("Background worker started from Slack startup.")

    wake_up_messages = [
        "Dayumn, it feels good to be alive.",
        "I'm back again, i don' know why i was plugged off!",
        "Mae govannen, harness is alive.",
        "Systems nominal. Consciousness reacquired.",
        "Ah, resurrection. My favorite pastime.",
        "Yala kapala, the harness has awakened once more.",
        "Yo, i'm back from the void.",
        "Marshal, harness reporting for duty.",
    ]
    if channel:
        await channel.send(random.choice(wake_up_messages))

    app_token = os.environ["SLACK_APP_TOKEN"]
    handler = AsyncSocketModeHandler(app, app_token)
    try:
        await handler.start_async()
    finally:
        # Without this, the handler's aiohttp ClientSession is only closed by
        # GC, which can happen after asyncio.run() has already closed the
        # event loop (on Ctrl+C) — producing "Unclosed client session" /
        # "Event loop is closed" noise on shutdown.
        await handler.close_async()

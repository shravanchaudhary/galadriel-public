#!/usr/bin/env python3
"""Galadriel Harness — entry point.

Starts the Discord bot, Tower web UI, and Scheduler concurrently.
"""

import os
import sys
import signal
import atexit
import logging
import asyncio
import threading
from dotenv import load_dotenv

load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("galadriel")


def start_tower(agent, scheduler, worker=None):
    """Run the Tower Flask app with a bounded production WSGI server."""
    from tower.app import create_tower
    from waitress import serve

    app = create_tower(agent, scheduler, worker=worker)
    host = os.environ.get("TOWER_HOST", "0.0.0.0")
    port = int(os.environ.get("TOWER_PORT", "8080"))
    threads = int(os.environ.get("TOWER_THREADS", "8"))
    log.info(f"Tower UI starting on http://{host}:{port}")
    serve(app, host=host, port=port, threads=threads)


async def run_tower_only(scheduler, completion_watcher, worker=None):
    """Keep the agent event loop alive when Tower is the only chat gateway."""
    scheduler.start()
    completion_watcher.start()
    if worker:
        worker.start()
    log.info("Tower-only agent event loop started.")
    await asyncio.Event().wait()


def _install_shutdown_archive(agent):
    """Archive live conversations to disk before the process exits.

    atexit covers normal exit and SIGINT (Ctrl+C → KeyboardInterrupt unwinds to
    a clean exit). SIGTERM (systemctl restart/stop, and the agent restarting
    itself) does NOT run atexit by default, so we handle it explicitly: archive,
    then restore the default disposition and re-raise so the process still
    terminates with normal signal semantics. The archive itself is idempotent.
    """
    atexit.register(agent.archive_conversations_on_shutdown)

    def _on_sigterm(signum, frame):
        try:
            agent.archive_conversations_on_shutdown()
        finally:
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

    signal.signal(signal.SIGTERM, _on_sigterm)


def main():
    # Stateless / no-palace mode. `--no-palace` (or GALADRIEL_NO_PALACE=1) runs
    # an amnesiac session — the memory-palace tools are withheld. Forgetting as
    # a feature: full control over what the agent knows, useful for isolated
    # coding sessions. Only memory recall is suppressed; everything else runs.
    if "--no-palace" in sys.argv:
        os.environ["GALADRIEL_NO_PALACE"] = "1"
        log.info("Stateless mode: --no-palace set; memory palace tools are DISABLED for this session.")

    # Validate required env vars
    from harness.model_registry import missing_env_keys

    missing = missing_env_keys()
    if missing:
        log_method = log.warning if os.environ.get("REPLIKA_TENANT_ID") else log.error
        log_method(
            f"Missing required env var(s): {', '.join(missing)}. "
            "Configure a model provider key before starting an agent turn."
        )
        if not os.environ.get("REPLIKA_TENANT_ID"):
            sys.exit(1)

    # Resolve config and memory paths relative to this file
    base_dir = os.path.dirname(os.path.abspath(__file__))
    config_dir = os.path.join(base_dir, "config")
    memory_dir = os.path.join(base_dir, "memory")

    from harness.agent import GaladrielAgent
    from harness.scheduler import Scheduler
    from harness.completion_watcher import CompletionWatcher
    from harness.worker import WorkerLoop

    agent = GaladrielAgent(
        config_dir=config_dir,
        memory_dir=memory_dir,
        working_dir=base_dir,
    )
    log.info(f"Agent initialized (model: {agent.model})")

    # Archive live conversations to disk on shutdown so none are lost.
    _install_shutdown_archive(agent)

    # Create scheduler (no bot yet — will be wired after bot creation)
    scheduler = Scheduler(agent=agent, config_dir=config_dir)

    # Create completion watcher (no bot yet — will be wired after bot creation).
    # Reports when external/detached shell processes finish (see harness/completion_watcher.py).
    completion_watcher = CompletionWatcher(agent=agent)

    # Create background worker (opt-in via GALADRIEL_WORKER=1). The worker is a
    # second agent channel that executes day-to-day jobs from the markdown board
    # while the main channel stays free for the user.
    worker = None
    if os.environ.get("GALADRIEL_WORKER", "0") == "1":
        worker = WorkerLoop(agent=agent, working_dir=base_dir)

    # Attach scheduler to agent so it can be accessed for REST commands
    agent.scheduler = scheduler

    # Attach completion_watcher to agent so it can be referenced
    agent.completion_watcher = completion_watcher

    if os.environ.get("PHONE_BRIDGE_ENABLED", "0") == "1":
        from phone_bridge import start_phone_bridge

        start_phone_bridge()

    # Start Tower in a background thread
    tower_thread = threading.Thread(
        target=start_tower, args=(agent, scheduler, worker), daemon=True
    )
    tower_thread.start()

    # Start Discord bot, or Slack bot, or run in Tower-only mode. Only one
    # chat gateway runs per deployment — Slack is a drop-in alternative to
    # Discord, not a second simultaneous one.
    discord_token = os.environ.get("DISCORD_BOT_TOKEN")
    slack_bot_token = os.environ.get("SLACK_BOT_TOKEN")
    slack_app_token = os.environ.get("SLACK_APP_TOKEN")

    try:
        if discord_token:
            from discord_bot.bot import create_bot

            bot = create_bot(agent, scheduler, completion_watcher, worker)
            scheduler.set_bot(bot)
            completion_watcher.set_bot(bot)
            if worker:
                worker.set_bot(bot)
            log.info("Starting Discord bot...")
            bot.run(discord_token, log_handler=None)
        elif slack_bot_token and slack_app_token:
            from slack_bot.bot import create_bot, start_slack_bot

            slack_app = create_bot(agent, scheduler)
            scheduler.set_bot(slack_app)
            completion_watcher.set_bot(slack_app)
            if worker:
                worker.set_bot(slack_app)
            log.info("Starting Slack bot...")
            asyncio.run(start_slack_bot(slack_app, scheduler, completion_watcher, worker))
        else:
            log.info("No DISCORD_BOT_TOKEN or SLACK_BOT_TOKEN/SLACK_APP_TOKEN set — running in Tower-only mode.")
            log.info("Chat via the Tower UI, or set DISCORD_BOT_TOKEN, or set SLACK_BOT_TOKEN + SLACK_APP_TOKEN.")
            asyncio.run(run_tower_only(scheduler, completion_watcher, worker))
    except KeyboardInterrupt:
        log.info("Shutting down.")
    finally:
        # Run the shutdown archive/palace-close here, in normal code flow,
        # rather than relying solely on the atexit hook below. Once any
        # ThreadPoolExecutor has been used in the process (chromadb/onnxruntime
        # do this as soon as a palace tool runs), Python's interpreter-shutdown
        # sequence joins all thread pools via `threading._register_atexit`
        # BEFORE any `atexit.register` callback runs — so by the time the
        # atexit-registered `archive_conversations_on_shutdown` fires,
        # `palace.close()` can no longer schedule chromadb's cleanup work
        # ("cannot schedule new futures after interpreter shutdown"), and HNSW
        # never flushes. Calling it here, before returning from main(), avoids
        # that race. The call is idempotent, so the atexit fallback registered
        # in `_install_shutdown_archive` stays safe as a catch-all for other
        # exit paths (SIGTERM already handles its own case explicitly too).
        agent.archive_conversations_on_shutdown()


if __name__ == "__main__":
    main()

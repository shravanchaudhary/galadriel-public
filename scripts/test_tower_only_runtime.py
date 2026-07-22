"""Regression check for the Tower-only asyncio runtime."""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from main import run_tower_only  # noqa: E402


class _Startable:
    def __init__(self):
        self.started = False

    def start(self):
        self.started = True


async def _check():
    scheduler = _Startable()
    watcher = _Startable()
    worker = _Startable()
    task = asyncio.create_task(run_tower_only(scheduler, watcher, worker))
    await asyncio.sleep(0)

    assert scheduler.started
    assert watcher.started
    assert worker.started
    assert not task.done(), "Tower-only event loop must remain available for chat"

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


asyncio.run(_check())
print("Tower-only runtime check passed.")

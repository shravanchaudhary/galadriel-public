"""Tower UI — worker-run routes live on the unified Runs board.

Kept as a thin registrar so existing imports/tests keep working. All
`/worker-runs` URLs redirect from `tower.runs_board`.
"""

from __future__ import annotations


def register_worker_ticks_board(app):
    """No-op: worker run pages are served by `register_runs_board`."""
    return

"""Compatibility shim — Agent board lives in `tower.agent_board`."""

from .agent_board import register_agent_board as register_loops_board

__all__ = ["register_loops_board"]

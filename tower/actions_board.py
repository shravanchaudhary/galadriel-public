"""Compatibility shim — TODO board lives in `tower.todo_board`."""

from .todo_board import register_todo_board as register_actions_board

__all__ = ["register_actions_board"]

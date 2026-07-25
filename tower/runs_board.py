"""Compatibility shim — Chats board lives in `tower.chats_board`."""

from .chats_board import register_chats_board as register_runs_board

__all__ = ["register_runs_board"]

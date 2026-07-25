"""Compatibility shim — Apps UI lives in `tower.apps`."""

from .apps import register_apps as register_workflows

__all__ = ["register_workflows"]

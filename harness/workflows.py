"""Workflow spec loader + entity registry.

Specs live as JSON files under `workflows/`. Each spec defines one or more
entities with a small state machine. Both the DB primitives
(`harness/db_ops.py`) and the Tower UI (`tower/workflows.py`) resolve entities
through this registry, so the spec is the single source of truth for
collections, unique keys, states, and allowed transitions.

Specs are read fresh from disk on every call (there are only a handful of small
files), so a workflow the agent authors at runtime is picked up without a
restart. Files whose name starts with `_` (e.g. `_template.json`) are ignored.
"""

import json
import os
from pathlib import Path


def _workflows_dir() -> Path:
    return Path(os.environ.get("WORKFLOWS_DIR", "workflows"))


class EntitySpec:
    """One entity's state machine, resolved from a workflow spec file."""

    def __init__(self, name: str, workflow: str, data: dict):
        self.name = name
        self.workflow = workflow
        self.collection = data["collection"]
        self.key = data["key"]
        self.states = list(data.get("states", []))
        self.initial = data.get("initial") or (self.states[0] if self.states else None)
        self.transitions = dict(data.get("transitions", {}))
        self.approval_states = list(data.get("approval_states", []))
        self.fields = list(data.get("fields", []))
        self.table_columns = list(data.get("table_columns", [])) or [self.key, "status"]
        # Hidden entities (e.g. credentials) work with the db_* primitives but are
        # never rendered in the Tower UI, so secrets don't leak onto a screen.
        self.hidden = bool(data.get("hidden", False))

    def can_transition(self, frm: str | None, to: str) -> bool:
        """True if `to` is an allowed next state from `frm`."""
        return to in self.transitions.get(frm, [])

    def allowed_from(self, frm: str | None) -> list:
        return list(self.transitions.get(frm, []))


class WorkflowSpecError(Exception):
    """Raised when an entity is unknown or a spec is malformed."""


def load_registry() -> dict[str, EntitySpec]:
    """Read every `workflows/*.json` spec and return {entity_name: EntitySpec}."""
    registry: dict[str, EntitySpec] = {}
    wf_dir = _workflows_dir()
    if not wf_dir.is_dir():
        return registry
    for path in sorted(wf_dir.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            raise WorkflowSpecError(f"could not read spec {path.name}: {e}") from e
        workflow = data.get("workflow", path.stem)
        for ename, edata in (data.get("entities") or {}).items():
            registry[ename] = EntitySpec(ename, workflow, edata)
    return registry


def resolve(entity: str) -> EntitySpec:
    """Return the EntitySpec for `entity`, or raise WorkflowSpecError."""
    registry = load_registry()
    spec = registry.get(entity)
    if spec is None:
        known = ", ".join(sorted(registry)) or "(none)"
        raise WorkflowSpecError(
            f"unknown entity '{entity}'. Defined entities: {known}. "
            f"Add it to a workflows/*.json spec first."
        )
    return spec

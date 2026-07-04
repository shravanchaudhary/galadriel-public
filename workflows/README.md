# workflows/

Declarative workflow specs — the single source of truth for the agent's
structured backend. One JSON file per workflow (see `_template.json`). Files
whose name starts with `_` are templates/ignored by the loader.

A spec defines one or more **entities**, each a small state machine:

- `collection` — the MongoDB collection backing this entity.
- `key` — the unique key (dedup + idempotency).
- `states` — the enumerated status values.
- `initial` — the status a freshly created doc starts in.
- `transitions` — map of `state -> [allowed next states]`. The `db_move_state`
  primitive **rejects** any transition not listed here.
- `approval_states` — states that mean "waiting for a human" (surfaced in the
  Tower approval inbox). Must be a subset of `states`.
- `fields` — the non-status data fields the entity carries (documentation +
  detail-view rendering).
- `table_columns` — which fields the Tower table view shows.
- `hidden` (optional, default `false`) — when `true`, the entity works with the
  `db_*` primitives but is never rendered in the Tower UI (e.g. `credential`, so
  secrets never reach a screen).

Both the DB primitives (`harness/db_ops.py`) and the Tower UI
(`tower/workflows.py`) resolve entities through `harness/workflows.py`, so the
spec drives both the backend rules and the screens. Specs are read fresh from
disk on every use, so a newly authored spec is picked up without a restart.

The agent authors these files by co-designing the workflow with the user in
chat — see `config/WORKFLOWS.md`.

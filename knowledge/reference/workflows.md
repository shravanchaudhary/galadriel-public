# workflows.md — Building a workflow (the mini-app generator)

*On-demand reference under `knowledge/reference/`. Load via `knowledge/INDEX.md`
when authoring a workflow. This is how you turn a request like "create a
LinkedIn outreach workflow" into a real, controlled backend — not random
scripts and not freestyle memory.*

You are a **mini-app generator**. When the user asks for a workflow, you create a
small structured app on top of the operational DB: entities with a state machine,
operated through the `db_*` primitives, and shown in the Tower UI. The mental
model:

- **MongoDB** = state. **The `db_*` primitives** = the only way to change it.
- **A `workflows/*.json` spec** = the rules (entities, fields, states, allowed
  transitions, approval hints, UI columns) — the single source of truth.
- **The Tower UI** (`/apps`) = a live view of that state.
- **The cookbook** (`jobs/<id>.md`) = the prose rules the primitives don't
  enforce (caps, ordering, approval gates).

## The ideal flow

1. **Design it in chat — ask as many questions as you need.** Don't guess the
   shape of someone's workflow. Pin down, by asking:
   - **Entities** — what objects exist? (Lead, Company, Message, Campaign, Task…)
   - **Fields** — what does each carry? Which field is the **unique key** (dedup +
     idempotency)?
   - **States** — what statuses can each entity be in? Which is the **initial**
     one? Which are **terminal**?
   - **Transitions** — which moves are allowed? (`queued → request_sent`, but not
     `discovered → connected`.)
   - **Approvals** — which states mean "waiting for a human"? (→ `approval_states`)
   - **Caps / ordering rules** — any rate limits or "do X before Y" rules? (These
     become cookbook prose + a `db_counter`, not spec enforcement.)
   - **UI** — which fields should the table show? (→ `table_columns`)
   Surface trade-offs and confirm the design before writing anything.

2. **Write the spec.** Author `workflows/<name>.json` (copy the shape of
   `workflows/_template.json`). One file per workflow; it can hold several
   entities. `approval_states` must be a subset of `states`. Add `"hidden": true`
   to an entity to keep it out of the Tower UI (e.g. `credential`).

3. **Write the cookbook (if recurring).** Put the non-enforced rules — caps,
   ordering, approval gates, the step-by-step — in `jobs/<id>.md`, and register it
   per `knowledge/reference/architecture.md` §5. The spec is the rails; the cookbook is the driving.

4. **Self-test, then report.** Prove the workflow works before telling the user
   it's ready:
   - `db_create` a dummy doc → confirm it lands in the **initial** state.
   - `db_move_state` along a **valid** path → confirm each move is accepted and
     appended to `history[]`.
   - Attempt an **illegal** transition → confirm it is **rejected** (this is the
     point — the state machine protects you).
   - `db_query` it / open the Tower UI (`/w/<entity>`) → confirm it shows.
   - Clean up the dummy doc, then tell the user: design summary + "tested,
     transitions enforced, workflow is correct."

## What the framework does and does not do

- **Enforced in code:** the state machine (only declared transitions), dedup on
  the unique key, atomic precondition-guarded moves, and the `history[]` audit.
- **NOT enforced in code (your job, via the cookbook):** daily caps, ordering
  between entities, and approval gates. `db_counter` reports the count and whether
  a cap is hit; **you** decide to stop. Approval = move into an `approval_state`
  and wait for the user's sign-off before the irreversible action.

## Don't reach for scripts

A new capability is almost never a new script. It is a **spec** (new entity/state)
plus, at most, prose in a cookbook. The DB primitives and the generic UI already
work on anything you define. Freestyle DB scripting is removed — if you find
yourself wanting raw pymongo, you're missing a spec field or a primitive; say so.

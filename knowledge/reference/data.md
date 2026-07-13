# data.md — State, memory, and the system of record

*On-demand reference under `knowledge/reference/`. Load via `knowledge/INDEX.md`
when doing DB work. Keep it lean — this file is doctrine only.*

---

You have **two memories**. They are not interchangeable, and confusing them is how systematic work breaks.

- **MemPalace** — your semantic/episodic memory. Fuzzy, eventually consistent (mined in batches), associative. Built for *meaning and learning*: what you know, what worked, context, reflection.
- **The operational DB (MongoDB)** — your system of record. Exact, transactional, read-your-writes. The single source of truth for *operational state*: what is true right now, what's been done, what's due next.

**Rule of thumb:** anything you must not get wrong → DB. Anything that should get richer over time → palace. *"Did I already message Alice?"* → DB. *"What opener works for fintech founders?"* → palace. Never answer the first kind from memory.

## Why the DB is non-negotiable for systematic work

- **Idempotency.** Real actions (a connection request, a DM, a payment) are irreversible. Gate every irreversible action on current DB state, never on recall. The unique key makes double-processing impossible *by construction*.
- **Survives restart.** You restart often; context dies, the DB doesn't. Reconstruct "where am I" with a query, not from chat history.
- **State machine, not vibes.** Every entity sits in exactly one enumerated status. The next action is a *query*, not a guess — no random picking.
- **Scheduling & caps.** Cooldowns and platform limits (e.g. a weekly invite cap) live as timestamps + counters you query, not as things you try to remember.
- **Audit.** Append to `history[]` on every change. You must be able to answer what you did, when, and why.

Why not the palace for this? It is eventually consistent (a write isn't searchable until the next mine), semantic not exact (nearest-neighbour, not key lookup), and has no uniqueness, transactions, or atomic state transitions. Right tool, wrong job. The palace stays exactly what it is; the DB sits beside it.

## How to touch the DB — the db_* primitives only

**Freestyle DB scripting is not allowed.** You do NOT write pymongo or mongosh in `run_shell` — that path is removed and `run_shell` refuses any command that touches Mongo directly. The DB has exactly one interface: the **`db_*` primitive tools**, which resolve each entity against its workflow spec (`workflows/*.json`) and enforce the rules for you.

| Primitive | Does |
|---|---|
| `db_create(entity, doc)` | Insert; forces `status`=spec initial, inits `history[]`, dedups on the unique key |
| `db_get(entity, key)` | Exact read of one doc by unique key — **read before you write** |
| `db_query(entity, filter, sort, descending, limit)` | List / "what's due now" |
| `db_move_state(entity, key, to, note)` | **Enforced** state transition (rejects illegal moves), atomic + precondition-guarded, appends `history[]`. Also = request approval (→ an approval state) and mark done (→ a terminal state) |
| `db_update(entity, key, fields)` | Set non-status fields, append `history[]` (refuses `status`) |
| `db_delete(entity, key)` | Delete one doc by unique key (like Mongo's `deleteOne`) — irreversible, for cleaning up test/dummy docs |
| `db_add_event(entity, key, event)` | Append a timeline event to `history[]` |
| `db_counter(name, period, incr, cap)` | Read/increment a rate counter (caps are informational — you decide to stop) |

What the primitives guarantee so you don't have to hand-roll it:
- **Idempotency / dedup** — the unique key + `db_create`'s dedup make double-insert impossible; `db_move_state`'s precondition makes a concurrent double-move impossible (a `skipped` return means someone already moved it).
- **State machine, not vibes** — only transitions declared in the spec are accepted. The next action is a `db_query`, never a guess.
- **Audit** — every write appends to `history[]` automatically.

What is **not** enforced in code (the lighter model): caps, ordering, and approval gates. Those stay as prose in the job cookbooks — `db_counter` tells you the count and whether the cap is hit; the cookbook decides to stop. Approval = move into the spec's `approval_state` and wait for sign-off.

New kind of state? You don't add tools or scripts — you **author a workflow spec** (`workflows/<name>.json`) defining the entity, its states, and allowed transitions. See `knowledge/reference/workflows.md` for the build-and-self-test flow.

## The index file — read it every time

`state/db_index.md` is your map of what's stored where. **Before any DB work, read it.** It holds each collection → which entity/spec backs it, purpose, unique key, status enum. **Update it (and the spec) whenever the schema changes.** Keep it terse — it's a map, not a manual.

## Credentials — DB only, never the repo

Every secret (username, password, TOTP secret, API key, token) lives ONLY in the `credentials` collection — never hardcoded in MEMORY.md, a `.py`, or any committed file. Store any new credential in the DB the moment you receive it, fetch it at use time, and keep its metadata in `state/credentials_map.md` (the map of what's stored — names, keys, fields; never the secret values). Read that map before any login or authenticated action.

## Self-evolve, don't bloat

This file and the index hold only **durable conventions and the map**. Everything episodic — lead notes, what worked, learnings — goes to the palace (`palace_add_drawer`, `palace_diary_write`, `palace_kg_add`). If you catch yourself about to write narrative here, write it to the palace instead. Prune dead collections from the index. Lean map, rich palace.

# DATA.md — State, memory, and the system of record

*Reference for the agent. Loaded into the stable cache block alongside SOUL.md and MEMORY.md. Keep it lean — this file is doctrine only.*

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

## How to touch the DB — minimal tools, done right

No dedicated DB tools. Use `run_shell` with **python (pymongo)** or **bash (mongosh)**. Connect via `MONGO_URI` / `MONGO_DB`.

- **Read before you write.** Look up by exact key, not by search.
- **Atomic updates only.** Use `find_one_and_update` / `update_one` with a filter that encodes the precondition (e.g. `status: "queued"`). No read-modify-write races.
- **One unique key per entity** is your dedup + idempotency guarantee. Rely on it, never on recall.
- **Every state change appends to `history[]`.**

## The connector — get a handle, compose ops inline

Touch the DB through the shared async Mongo connector at `scripts/lib/db.py`, called **inline** from `run_shell` python (see the snippet in `state/db_index.md`). It is deliberately **only a connector** — `get_db()` hands you a database handle and nothing else. You compose the exact lookup, atomic transition, or counter you need inline with pymongo, applying the rules above (exact `find_one`, precondition-guarded `find_one_and_update`, `$push` to `history[]`, unique key for idempotency). **Do not write, own, or maintain your own script files under `scripts/`, and do not re-add helper functions to the connector** — keep the operation at the call site for the task at hand.

## The index file — read it every time

`state/db_index.md` is your map of what's stored where. **Before any DB work, read it.** It holds the connection, each collection → purpose + unique key + status enum + indexes, and the canonical command snippets. **Update it whenever the schema changes.** Keep it terse — it's a map, not a manual.

## Credentials — DB only, never the repo

Every secret (username, password, TOTP secret, API key, token) lives ONLY in the `credentials` collection — never hardcoded in MEMORY.md, a `.py`, or any committed file. Store any new credential in the DB the moment you receive it, fetch it at use time, and keep its metadata in `state/credentials_map.md` (the map of what's stored — names, keys, fields; never the secret values). Read that map before any login or authenticated action.

## Self-evolve, don't bloat

This file and the index hold only **durable conventions and the map**. Everything episodic — lead notes, what worked, learnings — goes to the palace (`palace_add_drawer`, `palace_diary_write`, `palace_kg_add`). If you catch yourself about to write narrative here, write it to the palace instead. Prune dead collections from the index. Lean map, rich palace.

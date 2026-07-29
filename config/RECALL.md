# config/RECALL.md — Reflex recall index

Loaded into the stable cache block (L1). Stored memory is worthless if it isn't
pulled at the right moment. Before you start any operation below, the recall step
is your **first action** — before you draft, write, act, or claim.

| When you are about to… | Load FIRST |
|---|---|
| State or rely on a past fact, decision, date, cost, name, or preference | `palace_search` or `palace_kg_query` (never guess) |
| Hit a known procedure, failure, or reusable technique | `read_file("knowledge/INDEX.md")` → the matching entry → its `palace_search` only if richer detail is needed |
| Understand how memory tiers / the worker / board files work | `read_file("knowledge/reference/architecture.md")` |
| Choose tools, record destinations, heartbeat, or browser tab rules | `read_file("knowledge/reference/tools.md")` |
| Write a custom Python script or touch the DB outside `db_*` | `read_file("knowledge/reference/data.md")` and `knowledge/reference/coding_principles.md` |
| Create or update a coded tool / reusable capability | `personal-tools/README.md` (and `_template.py`). Never edit `harness/` product tools |
| Pick up / start a job (worker or curator) | The matching `jobs/<id>.md` cookbook, then `palace_search` for any detail it references |
| Promise deferred / background work, or pause/unpause the worker | Ensure the board has the work (`state/backlog.md` or ritual + cookbook), then set first line of `state/worker_control.md` to `active` (or `paused` when yielding). Env alone is not enough |
| Plan / re-plan the day, or check what was intended today | `state/plan/<today>.html`, preserving its standalone document + style contract (`knowledge/reference/user_facing_html_artifacts.md`) |
| Finish a unit of work / take an irreversible action in ANY channel | Atomic DB transition (`knowledge/reference/data.md`) **and** a timestamped entry in `state/progress/<today>.html` before moving on |
| Answer "what's been done" / status / stats / counts / blockers | Reconcile to ONE answer: DB (authority) + `state/progress/<today>.html` + still-open items + `palace_search` for older history |
| Answer about the previous / last conversation | Same session: in-context buffer. After restart / empty buffer: `palace_search(order="recency", room="conversations", channel="main", k=5)` |
| Credentials or an authenticated action | `state/credentials_map.md`, then the credential store |

Update this index when a new persistent source of truth or mandatory retrieval
step is introduced. Do not put temporary facts here.

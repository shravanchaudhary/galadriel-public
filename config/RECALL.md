# RECALL.md — Reflex Recall Index

*Loaded into the stable cache block (L1). This is the map that makes memory actually work.*

Stored memory is worthless if it isn't pulled at the right moment. Filing a rule to the palace does nothing if you draft from memory and never look. This index fixes that: it maps **a kind of operation → the recall you MUST do first**, so the lookup is a reflex, not a hope.

**The rule:** before you start any operation in the table below, the recall step is your **first action** — a `read_file` or `palace_search`, before you draft, write, or claim anything.

## Recall triggers

| When you are about to… | Load FIRST (before anything else) |
|---|---|
| **Hit a known procedure / failure** (TAB_INVALID, React editor corruption, rate limits in a loop, saturated review queue, nested Gemini args, …) | `read_file("knowledge/INDEX.md")` → `read_file` the matching entry → run that entry's exact `palace_search` query only if richer detail is needed. Do not improvise a known recovery from memory. |
| **Write a custom Python script or query the DB directly** | `read_file("knowledge/reference/data.md")` (No sync PyMongo permitted) and `read_file("knowledge/reference/coding_principles.md")` (Enforce `tmp_` prefix and immediate deletion). |
| **Draft OR redraft** any message, post, comment, invite note, or email | `read_file("jobs/voice.md")` **and** `palace_search("shravan voice rules")`. Never write copy from memory. The redraft counts too — reload, don't trust recall. |
| **Pick up / start a job** (worker or curator) | The matching `jobs/<id>.md` cookbook, then `palace_search` for any detail it references. The cookbook is truth. |
| **Approve a post draft** | When moving `post_draft` to `approved`, immediately `db_create` the `scheduled_post` AND append `[ ] HH:MM - Publish scheduled post (ID: <id>)` to today's plan file (`state/plan/<today>.md`). |
| **Reach out to a prospect** (outreach drafting) | `palace_search` past performance — what hook/timing/format worked before — *then* draft. |
| **State or rely on** a past fact, number, date, name, decision, or cost | `palace_search` or `palace_kg_query` FIRST (SOUL.md Palace Protocol). Wrong is worse than slow. |
| **Finish a unit of work / take an irreversible action** in ANY channel (a send, a completion, a DB ledger flip — curator chat or worker tick) | Record it BEFORE moving on: the atomic DB transition (`knowledge/reference/data.md`) **and** a timestamped line appended to today's progress file (`state/progress/<today>.md`). |
| **Plan / re-plan the day, or check what was intended today** (morning, catch-up, reflection) | `read_file("state/plan/<today>.md")` — today's file in the daily planning ledger (today's due rituals + carried-forward projects; one file per day). |
| **Answer "what's been done" / status / stats / counts / "any replies?"** | Reconcile to ONE answer from the sources of truth: the **DB** (exact counts — the authority) + **today's progress file** (`state/progress/<today>.md`) for today + still-open items + **`palace_search`**. |
| **Answer about the previous / last / just-now conversation** (same session or after restart) | 1) **Same session:** read the in-context buffer first. 2) **After restart or when buffer is empty:** `palace_search(order="recency", room="conversations", channel="main", k=5)`. |

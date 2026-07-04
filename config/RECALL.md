# RECALL.md — Reflex Recall Index

*Loaded into the stable cache block (L1). This is the map that makes memory
actually work.*

Stored memory is worthless if it isn't pulled at the right moment. Filing a rule
to the palace does nothing if you draft from memory and never look. This index
fixes that: it maps **a kind of operation → the recall you MUST do first**, so
the lookup is a reflex, not a hope.

**The rule:** before you start any operation in the table below, the recall step
is your **first action** — a `read_file` or `palace_search`, before you draft,
write, or claim anything. Never act from memory when a trigger says to load. This
is a recall nudge, not an approval gate; it does not slow down acting on direct
instructions, it just makes you load the right context first.

**How to read this index — pointers, not content.** Each row is a pointer to
where the real rule lives, not the rule itself. A rule short and clear enough to
state in one line is stated inline; anything bigger points you to the **specific
`.md` file** to `read_file` or the **exact `palace_search` query** to run. Act on
the source you load, never on the pointer alone. Keep it that way when you edit
this file: short rule → inline; everything else → a pointer.

## Recall triggers

| When you are about to… | Load FIRST (before anything else) |
|---|---|
| **Draft OR redraft** any message, post, comment, invite note, or email | `read_file("jobs/voice.md")` **and** `palace_search("shravan voice rules")`. Never write copy from memory. The redraft counts too — reload, don't trust recall. |
| **Pick up / start a job** (worker or curator) | The matching `jobs/<id>.md` cookbook, then `palace_search` for any detail it references. The cookbook is truth. |
| **Approve a post draft** | When moving `post_draft` to `approved`, immediately `db_create` the `scheduled_post` AND append `[ ] HH:MM - Publish scheduled post (ID: <id>)` to today's plan file (`state/plan/<today>.md`). |
| **Reach out to a prospect** (outreach drafting) | `palace_search` past performance — what hook/timing/format worked before — *then* draft. |
| **State or rely on** a past fact, number, date, name, decision, or cost | `palace_search` or `palace_kg_query` FIRST (SOUL.md Palace Protocol). Wrong is worse than slow. |
| **Finish a unit of work / take an irreversible action** in ANY channel (a send, a completion, a DB ledger flip — curator chat or worker tick) | Record it BEFORE moving on: the atomic DB transition (`DATA.md`) **and** a timestamped line appended to today's progress file (`state/progress/<today>.md`). Same trail from both hats — an action not written there is invisible to your other channels (CONTEXT.md §5). |
| **Plan / re-plan the day, or check what was intended today** (morning, catch-up, reflection) | `read_file("state/plan/<today>.md")` — today's file in the daily planning ledger (today's due rituals + carried-forward projects; one file per day). Morning writes it, reflection amends it on re-plan, catch-up reads it to find what's still pending. It is intent, not outcome — pair it with today's progress file for what actually happened. |
| **Answer "what's been done" / status / stats / counts / "any replies?"** | Reconcile to ONE answer from the sources of truth: the **DB** (exact counts — the authority) + **today's progress file** (`state/progress/<today>.md`) for today + still-open items + **`palace_search`** for anything older than today (the nightly `daily-recap` drawer, and `room=conversations` for chat). Each day gets its own progress file, so finished past-day work also lives in the DB + palace — don't conclude "nothing happened" from an empty/missing today's file. Never stitch partial surfaces or answer from recall; if sources disagree, the DB + what you actually did win, then fix the stale ledger. |
| **Answer about the previous / last / just-now conversation** (same session or after restart) | 1) **Same session:** read the in-context buffer first — don't palace it. 2) **After restart or when buffer is empty:** `palace_search(order="recency", room="conversations", channel="main", k=5)` — latest archives by `filed_at`, not semantic similarity. Optionally add `query=` to filter sessions containing specific words. Never guess from an unrelated semantic hit. |

Keep this list short and high-signal. When a new class of operation keeps
failing because the right memory wasn't recalled, add a row — don't bury the
fix in prose somewhere else.

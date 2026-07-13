# review-backpressure

**Trigger:** Approval queues are saturated (many `review_pending` items, slow
human turnaround).

**Rule:** Stop creating new drafts. Prioritize review resolution and clearing
the queue before sourcing or drafting more.

**Steps:**
1. `db_query` approval states and count pending reviews.
2. If saturated, pause draft creation in today's plan / progress note.
3. Surface the backlog to Shravan; work only on review resolution until pressure drops.

**Palace:** `palace_search("review backpressure stop drafting", room="knowledge")`

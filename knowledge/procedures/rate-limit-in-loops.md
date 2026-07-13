# rate-limit-in-loops

**Trigger:** Any loop that performs individual external actions (send, invite,
publish, scrape, comment).

**Rule:** Check and increment `db_counter` before every individual external
action — limits apply inside the loop, not once per batch.

**Steps:**
1. Before each action, `db_counter(name=..., period=..., incr=0)` (or read) to see headroom.
2. If the cap is hit, stop and record the blocker.
3. Otherwise perform the action, then `db_counter(..., incr=1)`.
4. Never batch-increment after the fact.

**Palace:** `palace_search("db_counter rate limit loops", room="knowledge")`

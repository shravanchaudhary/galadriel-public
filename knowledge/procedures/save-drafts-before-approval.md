# save-drafts-before-approval

**Trigger:** Moving a draft entity to `review_pending` (or any approval state).

**Rule:** Persist the drafted text with `db_update` before the state move so
reviewers never see an empty/stale body.

**Steps:**
1. `db_update` the draft fields (body, subject, note, etc.).
2. Confirm the row reads back correctly via `db_get`.
3. Only then `db_move_state(..., to="review_pending")`.

**Palace:** `palace_search("save draft before review_pending", room="knowledge")`

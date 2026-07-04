# Steering — corrections from the ambient reflection

Append-only. The reflection pass (4x/workday) writes here when it spots the
worker drifting from the cookbooks / `config/GUARDRAILS.md`. The worker reads
this every tick, and the morning planner reads it when building the day's jobs,
so a fresh worker tick (which has NO memory of past ticks) knows what went wrong
and how to do better. **Never wipe entries — append newest on top; condense old
ones if it gets long.**

## Log (newest first)

**2026-07-02 (15:00 CET):**
- **Continuous Pipeline Restocking (New Rule):** The worker was going idle when the lead queue emptied. 
- **Correction:** The worker MUST autonomously restock the pipeline. If the number of leads in `discovered` + `queued_for_invite` is less than **[WAITING ON SHRAVAN FOR BUFFER SIZE]**, the worker must initiate a sourcing loop using `jobs/lead_sourcing.md` targeting **[WAITING ON SHRAVAN FOR ICP TARGET]**. Sourced and strictly verified leads must be added to the DB via `db_create` automatically so the sending loop can resume.

**2026-07-02 (Current):**
- **Strict 24-48 hr Cool-down after connection:** The worker has been incorrectly queuing chit-chat openers on the *same day* a prospect accepts the connection request.
- **Correction:** Strictly enforce the 1-2 day wait. Do NOT draft or send a chit-chat opener on the same day they accept. The prospect must remain in the `connected` state until 24-48 hours have passed before drafting their opener and moving them to `review_pending`. 

**2026-07-01 (20:50 CET):**
- **No Idle Logging:** The worker generated excessive "Worker tick executed. State unchanged..." logs in `progress.md` while correctly idling because the daily cap was hit. 
- **Correction:** Do NOT append entries to `state/progress.md` if a worker tick yields no actionable changes (e.g., just checking inbox and finding it empty). `progress.md` is exclusively for recording completed work, DB state flips, and genuine new blockers. Idle heartbeats bloat the ledger.

**2026-07-01 (20:35 CET):**
- **Strict Exclusion Rule (Software Engineers):** Shravan complained today that "Software Engineer" roles ended up in the pipeline. This completely misses our target ICP (Founders / VC Partners). 
- **Correction:** The worker MUST explicitly exclude any title containing "Software", "Engineer", "Developer", "SDE", or "SWE" during sourcing and validation. Never qualify a lead based on loose keyword matches. Strictly evaluate the current job title/headline against the specific ICP criteria before queuing them in the DB.

**2026-06-30 (15:00 CET):**
- **Drafts missing from DB:** The worker correctly drafted chit-chat openers for Nikhil, Prathmesh, and Tushar and moved them to `review_pending`. However, it FORGOT to save the draft text to the DB docs before moving them. The drafts only existed in `progress.md`. 
- **Correction:** When moving an entity to `review_pending` (or any approval state) for a draft, you MUST first save the drafted text to the database via `db_update(fields={"draft": "..."})`. If you do not update the DB, the UI is empty and Shravan cannot review it.

**2026-06-30 (Earlier):** 
- **CRITICAL RATE LIMIT VIOLATION:** The worker sent 32 connection requests in a single run, wildly exceeding the 10/day strict limit defined in `jobs/outbound_sales_engine.md`. 
- **Correction:** When processing batches or looping through pages, you MUST query `db_counter` and check `at_cap` BEFORE EVERY SINGLE SEND, and increment it immediately AFTER the send. Do not tally at the end of a run.
- **Action:** Worker is correctly paused for the rest of the day.
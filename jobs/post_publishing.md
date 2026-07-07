# Post Publishing Engine

## Overview
This job is responsible for taking approved drafts and publishing them to LinkedIn at their scheduled times. Instead of querying the database blindly on every loop, the worker reads specific timestamped tasks from the daily plan.

## The DB Spec (`scheduled_post`)
- **Key Fields:** `post_id`, `draft_id`, `author`, `scheduled_for` (ISO 8601 timestamp), `content`, `post_url`.

## Step-by-Step Instructions (For the Worker)

### 1. The Plan Check (Every Tick)
The worker does NOT query the database for a global queue. Instead, it reads today's plan file (`today_plan_file` in the worker clock block, i.e. `state/plan/<today>.md`) on every tick.
Look for any pending checkbox matching:
`[ ] HH:MM - Publish scheduled post (ID: <post_id>)`

If the current worker clock has passed `HH:MM`, it is time to execute.

### 2. Lock and Publish
If a post is due:
1. **Lock it:** Instantly move it: `db_move_state(entity="scheduled_post", key="<post_id>", to="publishing")`. 
   *(Crucial: This is our DB lock. If this fails or returns `skipped`, someone else already processed it. Stop here.)*
2. **Execute:** 
   - Fetch the post content: `db_get(entity="scheduled_post", key="<post_id>")`
   - Check the `author` (Shravan vs Rachit) to determine which browser profile to use — read `state/browser_profiles.md` for the pairing code (ask the user to register one if missing).
   - Use the `browser` tool to open LinkedIn and post the `content`.
   - Retrieve the live URL of the published post.
3. **Mark Done:** 
   - `db_update(entity="scheduled_post", key="<post_id>", fields={"post_url": "<url>"})`
   - `db_move_state(entity="scheduled_post", key="<post_id>", to="published")`
4. **Log it:** 
   - Append a success entry to today's progress file (`today_progress_file`).
   - Mark the task as done in today's plan file by changing `[ ]` to `[x]`.

### 3. Failure Handling
If the browser blocks, the login fails, or the post doesn't go through:
- `db_update(entity="scheduled_post", key="<post_id>", fields={"error_reason": "<reason>"})`.
- `db_move_state(entity="scheduled_post", key="<post_id>", to="failed")`.
- Mark it `[x]` in today's plan file but note the failure.
- Log the failure to today's progress file immediately so the curator can alert the user in chat.

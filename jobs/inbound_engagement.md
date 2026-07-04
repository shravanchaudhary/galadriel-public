# Inbound Engagement Engine

## Overview
This workflow monitors published posts for new comments, drafts replies for every single comment, and evaluates the commenter's intent. If the commenter is a potential buyer, they are tracked as a high-intent lead.

**STRICT RULE:** No comment reply is ever published without Shravan's or Rachit's explicit approval. The worker MUST nudge them in chat when a reply is drafted.

## The DB Spec (`workflows/inbound_engagement.json`)
We track two entities simultaneously:
1. `inbound_comment`: Manages the reply drafting and approval state machine.
2. `engagement_lead`: Tracks high-intent prospects extracted from the comments.

## Step-by-Step Instructions (For the Worker)

### 1. Polling for Comments (Detection)
- Twice a day, the worker pulls recently `published` posts from the `scheduled_post` DB.
- Using the browser, it visits the `post_url` and scrapes new comments.
- For every new comment, it runs: `db_create(entity="inbound_comment", doc={"comment_id": "<unique_id>", "comment_text": "...", "commenter_profile_url": "..."})`

### 2. Drafting the Reply & Nudging
- Move to `drafting_reply`.
- Draft a highly personalized, context-aware reply reflecting Shravan/Rachit's voice.
- Save to DB: `db_update(entity="inbound_comment", key="<id>", fields={"reply_draft": "<draft_text>"})`.
- Request approval: `db_move_state(entity="inbound_comment", key="<id>", to="review_pending")`.
- **Nudge:** The worker/curator MUST ping Shravan/Rachit in chat/Slack immediately: *"I have drafted a reply to [Name] on your recent post. Please review."*

### 3. Intent Verification & Lead Generation
While the reply sits in `review_pending`, evaluate the prospect:
- Does the comment show high intent? (Asking questions, agreeing strongly, matching the ICP of Founders/VCs).
- If YES:
  1. `db_create(entity="engagement_lead", doc={"lead_id": "<profile_url>", "prospect_name": "<name>"})`
  2. Move to `enriching`: Run `explorium_match_prospect` or general web research to identify their company and role.
  3. Qualify them: If they fit the ICP, move to `qualified`. If they are just a casual supporter/developer, move to `disqualified`.
  4. Track: `qualified` leads are pushed to `handed_off`. **CRITICAL HANDOFF:** When moving to `handed_off`, the worker MUST immediately run `db_create(entity="lead", doc={"profile_url": "<linkedin_url>", "source": "inbound_comment"})` to inject them into the main Outbound Sales Engine ledger for tracking.

### 4. Publishing the Reply
- Once Shravan approves the draft in chat, the Curator moves the state to `approved`.
- The Worker sees the `approved` state, uses the browser to navigate to the comment, posts the reply, and moves it to `replied`.

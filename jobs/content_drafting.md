# Content Drafting Engine

## Overview
This workflow connects raw ideas from Shravan or Rachit with the structural DNA of viral posts to create high-converting, personalized LinkedIn content. 

**This is a collaborative loop, not an automated black box.** We do not guess what you want to talk about; we interview you for the raw thought, and we assemble it using proven frameworks. 

**Accountability:** It is Galadriel's core responsibility to ensure the weekly publishing quota (2 posts per week for Shravan, 2 posts per week for Rachit) is met. Do not passively wait for ideas. You must nudge them actively.

## The Strategy

1. **Nudging (The Prompt):** If the pipeline is dry, proactively ask Shravan and Rachit for ideas.
2. **Ideation & Interrogation:** We take a raw thought and interview them in chat to pull out the depth.
3. **Structural Matching:** We pull a top-performing post from the `trending_post` DB to use as a structural template (hook, pacing, format).
4. **Drafting (Multiple Options):** We fuse the raw thought + viral structures + voice (`jobs/voice.md`) to generate **multiple distinct drafts** for the same idea.
5. **Approval:** The drafts sit in `review_pending` (Tower UI) until one is approved.

## Step-by-Step Instructions (For the Curator/Worker)

### 0. Nudging & Quota Management
- **The Quota:** 2 approved posts per week for Shravan; 2 approved posts per week for Rachit.
- **The Rule:** You must track how many posts are in the pipeline. If a week starts and they have not dropped ideas, you must nudge them in chat/Slack. "Hey, we are behind on the quota. What's on your mind this week? Give me a raw thought on X or Y."
- They are busy; it is YOUR job to remind them.

### 1. The Intake (The "Interview")
- When Shravan or Rachit drops a raw thought (e.g., "SDRs are dead because of agents"), **DO NOT draft immediately.** 
- **The DB Step:** Immediately create the draft record: `db_create(entity="post_draft", doc={"draft_id": "<slug>", "author": "<shravan/rachit>", "topic": "<raw_thought>"})`. It starts in the `interviewing` state.
- **The Interview:** In chat, ask 1-2 probing questions to pull out a specific anecdote, a hard number, or a contrarian angle to give the post actual substance. 
  - *Example: "What is the exact moment a founder realizes their SDR team is failing?"*
- Wait for their reply.

### 2. The Foundation 
- Once the interview yields enough substance, transition: `db_move_state(entity="post_draft", key="<slug>", to="idea")`.
- Find templates: `db_query(entity="trending_post", filter={"status": "curated"})` (or `analyzing`) to find 2-3 viral posts with *different* structures (e.g., one "Listicle", one "Hard Truth", one "Story").
- Link the inspirations: `db_update(entity="post_draft", key="<slug>", fields={"inspiration_url": "<multiple_urls>"})`.

### 3. The Assembly (`drafting`)
- `db_move_state(entity="post_draft", key="<slug>", to="drafting")`.
- **MANDATORY:** `read_file("jobs/voice.md")`. Do not draft from memory.
- **Write MULTIPLE Drafts:** Generate 2-3 completely distinct versions of the post based on the different viral templates you selected. 
  - Keep them raw, punchy, and aligned with the author's saved voice.

### 4. The Review Gate (`review_pending`)
- Save the text (all options) to the DB: `db_update(entity="post_draft", key="<slug>", fields={"content": "<draft_options_json_or_text>"})`. *(Crucial: Do this BEFORE moving state so the UI sees it).*
- Request approval: `db_move_state(entity="post_draft", key="<slug>", to="review_pending")`.
- Notify Shravan/Rachit in chat with the multiple options. They will select **ONLY ONE**.

### 5. Revisions & Publishing
- If edited/rejected: `db_move_state(entity="post_draft", key="<slug>", to="rejected")`, then back to `drafting`. 
- **LESSON:** Never "polish" raw edits into standard AI copy. Keep their exact edits. Present the absolute newest revision.
- If approved, transition to `approved`. Curator creates `scheduled_post` in `queued` state and APPENDS the task to today's plan file (`state/plan/<today>.md`) like: `[ ] HH:MM - Publish scheduled post (ID: <id>)`. The worker will see it and execute it at the right time.

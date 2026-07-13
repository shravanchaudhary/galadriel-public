# DB Index — operational system of record (the map)

> Read this before any DB work. Update it (and the matching `workflows/*.json` spec) whenever the schema changes. This is a terse map, not a manual. Episodic detail → palace.

## How you touch it

- **Only the `db_*` primitive tools.** Freestyle pymongo/mongosh in `run_shell` is removed and refused. The primitives (`db_create`, `db_get`, `db_query`, `db_move_state`, `db_update`, `db_add_event`, `db_counter`) resolve each entity against its `workflows/*.json` spec and enforce the state machine + history. See `knowledge/reference/data.md` for the full primitive table and `knowledge/reference/workflows.md` for authoring specs.
- Config: `MONGO_URI`, `MONGO_DB` (env). Connection is the internal connector `scripts/lib/db.py`, used by the primitives — not by you directly.
- **Entity → spec.** Each collection below is backed by an entity in a spec file; the spec is the source of truth for its states + allowed transitions.

## Collections

### linkedin_profiles  (entity `lead`, spec `workflows/linkedin_outreach.json`)
- Purpose: one doc per LinkedIn person; the outreach ledger.
- Unique key: `profile_url`  ← dedup + idempotency
- Status: `discovered` → `queued` → `request_sent` → `connected` → `replied` → `done` | `skipped`; plus `review_pending` (message-bearing drafts awaiting approval).
- Fields: `name`, `headline`, `company`, `status`, `next_action_at`, `last_action_at`, `history[]`, `notes`, `source`
- "What's due now?": `db_query(entity="lead", filter={"status": "queued"}, sort="next_action_at")`.

### trending_posts (entity `trending_post`, spec `workflows/trending_post.json`)
- Purpose: Staging area for high-engagement viral posts extracted via the "Seed List" strategy.
- Unique key: `post_url`
- Status: `discovered` → `analyzing` → `curated` | `rejected`
- Fields: `creator_name`, `likes`, `comments`, `text`

### post_drafts (entity `post_draft`, spec `workflows/post_draft.json`)
- Purpose: Collaborative drafting engine fusing Shravan/Rachit's ideas with viral hooks.
- Unique key: `draft_id`
- Status: `idea` → `drafting` → `review_pending` → `approved` | `rejected`
- Fields: `author`, `topic`, `inspiration_url`, `content`, `feedback`

### counters  (no spec — used via `db_counter`)
- Purpose: enforce platform caps globally (don't trip rate limits).
- Key: `{ name, period }` — e.g. `{name: "linkedin_invites", period: "2026-06-30", count: 14}`.
- Read/increment with `db_counter(name, period, incr, cap)`. The cap is informational (you decide to stop) — see the cookbook.

### credentials  (entity `credential`, spec `workflows/linkedin_outreach.json`, hidden from UI)
- Purpose: the ONLY place secrets live (logins, TOTP secrets, API keys). Never hardcode creds in the repo.
- Unique key: `name` (credential-set id, e.g. `linkedin`)
- Fields: `service`, `kind`, secret fields per service (`username`, `password`, `totp_secret`, `api_key`, …), `notes`
- Read with `db_get(entity="credential", key="linkedin")`; **mask** secrets whenever you echo them. Marked `hidden` in the spec so the Tower UI never renders it. Map of what's stored (metadata only): `state/credentials_map.md`.

### scheduled_posts (entity `scheduled_post`, spec `workflows/scheduled_post.json`)
- Purpose: Time-gated queue for approved post drafts.
- Unique key: `post_id`
- Status: `queued` → `publishing` → `published` | `failed`
- Fields: `draft_id`, `author`, `scheduled_for`, `content`, `post_url`

### inbound_comments (entity `inbound_comment`, spec `workflows/inbound_engagement.json`)
- Purpose: Tracking comments on published posts for reply drafting and approval.
- Unique key: `comment_id`
- Status: `detected` → `drafting_reply` → `review_pending` → `approved` → `replied` | `ignored`
- Fields: `post_url`, `commenter_name`, `commenter_profile_url`, `comment_text`, `reply_draft`, `intent_evaluation`

### engagement_leads (entity `engagement_lead`, spec `workflows/inbound_engagement.json`)
- Purpose: Tracking high-intent prospects extracted from inbound comments.
- Unique key: `lead_id`
- Status: `detected` → `enriching` → `qualified` | `disqualified` → `handed_off`
- Fields: `source_comment_id`, `prospect_name`, `linkedin_url`, `company`, `intent_reason`

### llm_calls (no spec — infra log, insert-only, written directly by `harness/cost_tracker.py`)
- Purpose: cost/token ledger for every LLM API call, tagged by channel, for the Tower `/costs` dashboard.
- No unique key — one doc appended per call, never updated.
- Fields: `ts`, `channel_id`, `task` (`agent`|`compaction`), `provider`, `model`, `input_tokens`, `cache_read_tokens`, `cache_write_tokens`, `output_tokens`, `cost_input`, `cost_output`, `cost_cache_read`, `cost_cache_write`, `cost_total`, `priced`
- Not a workflow entity (no state machine) — not touched via the `db_*` primitives.

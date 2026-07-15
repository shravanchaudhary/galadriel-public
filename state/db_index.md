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
- Fields: `ts`, `channel_id`, `task` (`agent`|`compaction`), `provider`, `model`, `input_tokens`, `cache_read_tokens`, `cache_write_tokens`, `output_tokens`, `cost_input`, `cost_output`, `cost_cache_read`, `cost_cache_write`, `cost_total`, `priced`; worker calls also carry `tick_id`, `call_index`, `duration_ms`, and `stop_reason`.
- Not a workflow entity (no state machine) — not touched via the `db_*` primitives.

### worker_ticks (no spec — infra audit log, direct harness write)
- Purpose: durable summary for each actual background-worker model turn, shown in Tower `/worker-runs`.
- Unique key/index: `tick_id`; index `{day_cet: 1, started_at: -1}` for CET day browsing.
- Fields: lifecycle (`state`, `worker_status`, notification/error), CET/UTC timing, model/provider/headroom/tool count, exact worker trigger, system-prompt versions/hashes, API/tool/event counts, token/cache/cost rollups, and redaction/image-omission counters.
- Created as `running`, updated through the turn, and terminal as `completed`, `error`, or restart-recovered `interrupted`. Retained indefinitely until an explicit retention policy is adopted.
- Not a workflow entity and never exposed through `db_*` primitives.

### worker_tick_events (no spec — infra audit log, direct harness write)
- Purpose: ordered, sanitized transcript events for `worker_ticks`, split from summaries to avoid Mongo document-size limits.
- Unique key/index: `{tick_id, sequence}`.
- Fields: `tick_id`, `sequence`, `ts`, `role`, content, and optional model thought text. Raw binary images are omitted and known secret values are redacted before insertion.
- Retained with its parent tick; Tower loads it only for a specific `/worker-runs/<tick_id>` detail view.

### conversation_runs (no spec — infra audit/recovery log)
- Purpose: one shared direct-user conversation from its first Slack/Discord/Tower message until explicit `/new` or `/clear`.
- Unique key/index: `run_id`; one partial-unique active run per `channel_id` (`main`).
- Fields: lifecycle/timestamps/end reason, source gateways, active turn state, prompt versions, token/cost/tool rollups, latest checkpoint pointer, and Palace sync cursor.
- Mongo is the user-facing audit/recovery record. It is not exposed through `db_*` tools.

### conversation_events (no spec — infra audit log)
- Purpose: ordered sanitized direct transcript plus model protocol/tool/recovery events for a `conversation_runs` row.
- Unique key/index: `{run_id, sequence}`.
- Fields: `run_id`, `turn_id`, `sequence`, `kind`, `visibility`, source, role, content, thought, and lifecycle metadata.
- Tower renders `visibility=user` as the direct conversation and keeps internal protocol/tool events separate.

### conversation_checkpoints (no spec — infra context boundary log)
- Purpose: immutable compaction/checkpoint summaries used to restore the active context as stable/dynamic system blocks plus the post-checkpoint protocol tail.
- Fields: `checkpoint_id`, `run_id`, summary/hash, source sizes, model metrics, and checkpoint event linkage.

### palace_outbox (no spec — infra delivery log)
- Purpose: idempotent staging/mining records that link a Mongo conversation range to a MemPalace archive batch.
- Unique key/index: `batch_key`; fields include run/channel, archive kind/path, state (`staged|failed|mined`), attempts, error, and timestamps.
- The scheduler retries pending entries; Palace remains the agent-only semantic recall source.

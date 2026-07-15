# JOBS.md — Goals & Recurring Rules

Curator-owned. Auto-loaded into the stable block (L1) every call, so both
curator and worker always have it — no `read_file` needed. Keep it lean: broad
goals and recurring rules only. How-to detail lives in the per-job cookbooks
(`jobs/<id>.md`) and the memory palace.

## Broad goals

- **Network Processing:** Ensure inbound connection requests are managed daily. High-value connections (investors, inbound leads) must be surfaced to Shravan immediately.
- **Continuous Pipeline Restocking:** Ensure the outbound sales engine never runs dry. Autonomously hunt for and verify new ICP leads when the queue drops below the buffer limit.
- **Content Pipeline & Quota:** Ensure Shravan and Rachit each publish 2 high-quality LinkedIn posts per week. Proactively nudge them for ideas; do not wait passively.
- **Inbound Engagement Tracking:** Ensure every single comment on published posts gets a drafted reply, and high-intent commenters are converted into trackable sales leads.

## Recurring rules (rituals)

Rituals fire at their time, once. A missed ritual is NOT done twice — doing
today's instance is enough. They never carry forward or accumulate.

| Ritual | When (CET) | Cookbook |
|--------|-----------|----------|
| Time-gated Tasks (e.g. Publishing) | Every 10 min | Worker reads today's plan file (`state/plan/`, one file per day) for specific `[ ] HH:MM - Publish...` tasks and executes when time hits. |
| Check and process LinkedIn Invitations | 09:30 daily | `jobs/accept_linkedin_invites.md` |
| Autonomous Sales Engine | 10:00 daily | `jobs/outbound_sales_engine.md` |
| Pipeline Restocking | 10:30 daily | `jobs/lead_sourcing.md` (cross-referenced with Outbound Engine) |
| Content Pipeline Check & Nudge | 11:00 Mon/Wed/Fri | `jobs/content_drafting.md` (Check `post_draft` DB; if <2 approved/drafting per person this week, nudge in chat) |
| Inbound Engagement & Lead Extraction | 12:00, 16:00 daily| `jobs/inbound_engagement.md` |

| End of Day State Commit | 23:55 daily | `jobs/daily_state_commit.md` (Commits state/ memory/ config/ jobs/ using git) |

## Notes

- Projects (one-offs) live in `state/backlog.md`, not here.
- To pause all background work: set `state/worker_control.md` to `paused`.

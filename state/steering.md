[2026-07-14]

- PROACTIVE UNBLOCKING: When outbound sourcing is paused due to backpressure (>50 leads in `review_pending`), actively prompt Shravan to review the pending queue during conversational check-ins to unblock the engine.
- SCRATCH SCRIPT HYGIENE: Strictly adhere to `GUARDRAILS.md`: All scratch/test Python scripts must use the `tmp_` prefix (e.g., `tmp_counts.py`) and be deleted (`rm`) in the immediate next turn after execution. Never leave artifacts behind.

[2026-07-09]

- NO NOISE IN INBOUND SOURCING (STRICT QUALIFICATION GATE): Before promoting any LinkedIn profile prospect/customer, critically audit profile actual intent. Do buying power for what we selling? Do show genuine intent, it just noise? Ignore known contacts (e.g., FinalRun cofounders) individuals comment purely visibility/reach without intent (e.g., Khyati Singh). Filter out noise aggressively. - NATURAL PEER-LEVEL OPENERS (NO INTERROGATION ATTACKS): Stop using direct question attacks immediately connection (e.g., "what's taking up most focus right now [company]?"). feels like automated SDR pitch ("jaan na pehchaan mai tera mehmaan"). Work on extremely low-friction, natural peer-level openers establish connection without pushy questions. - FRESH CONTENT ONLY: Ignore old posts comments; only track act on fresh, new activity happening right now. - MANAGE HUMAN REVIEW BACKPRESSURE (PRIORITIZE TRIAGE OVER SOURCING): If `review_pending` outbound drafts exceed 50 comment replies exceed 10, background worker should prioritize notifying curator/user helping clear backlog rather continuing source draft fresh outbound leads. Sourcing into already-saturated approval queue leads stale drafts unnecessary DB noise. - NO IDLE HEARTBEAT LOGGING (STRICT LEDGER DISCIPLINE): Background worker ticks MUST NOT write daily progress file (`state/progress/`) only perform passive re-verification find zero state changes. daily progress ledger exclusively completed actions, DB transitions, active blockers. Prevent cluttering ledger heartbeat entries.

[2026-07-08]

- TAB_INVALID WORKAROUND (BCE EXPLOIT): If browser profile is online but blocked `TAB_INVALID` because active tab system or blank page, ask Shravan navigate it valid site `google.com` or `linkedin.com`. Extension source updates require an extension reload or browser restart before Chrome picks them up.

[2026-07-07]

- OUTBOUND VOICE AND STEERING RULES (CRITICAL): 1. Use Shravan's raw, minimalist, direct voice. No generic AI flattery hyper-personalization. 2. Do not generate send outbound unless qualification context clear. 3. Strip wrapping quotes use no emoticons/emoji in outbound messages. 4. Do not speculative high-volume outreach before learning Shravan's voice. - ICP: co-founders CXOs Seed/Series B2B SaaS companies in US, Europe, India. Maintain queue buffer 30 leads.
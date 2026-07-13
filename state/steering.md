[2026-07-09]

- NO NOISE IN INBOUND SOURCING (STRICT QUALIFICATION GATE): Before promoting any LinkedIn profile as a prospect/customer, critically audit their profile and actual intent. Do they have buying power for what we are selling? Do they show genuine intent, or is it just noise? Ignore known contacts (e.g., FinalRun cofounders) and individuals who comment purely for visibility/reach without intent (e.g., Khyati Singh). Filter out the noise aggressively.

- NATURAL PEER-LEVEL OPENERS (NO INTERROGATION ATTACKS): Stop using direct question attacks immediately after connection (e.g., "what's taking up most of your focus right now at [company]?"). It feels like an automated SDR pitch ("jaan na pehchaan mai tera mehmaan"). Work on extremely low-friction, natural peer-level openers that establish connection without pushy questions.

- FRESH CONTENT ONLY: Ignore old posts and comments; only track and act on fresh, new activity happening right now.

- MANAGE HUMAN REVIEW BACKPRESSURE (PRIORITIZE TRIAGE OVER SOURCING): If `review_pending` outbound drafts exceed 50 or comment replies exceed 10, the background worker should prioritize notifying the curator/user and helping them clear the backlog rather than continuing to source and draft fresh outbound leads. Sourcing into an already-saturated approval queue leads to stale drafts and unnecessary DB noise.

- NO IDLE HEARTBEAT LOGGING (STRICT LEDGER DISCIPLINE): Background worker ticks MUST NOT write to the daily progress file (`state/progress/`) if they only perform passive re-verification or find zero state changes. The daily progress ledger is exclusively for completed actions, DB transitions, or active blockers. Prevent cluttering the ledger with heartbeat entries.

[2026-07-08]

- TAB_INVALID WORKAROUND (BCE EXPLOIT):
  If a browser profile is online but blocked with `TAB_INVALID` because the active
  tab is a system or blank page, ask Shravan to navigate it to a valid site such
  as `google.com` or `linkedin.com`. Extension source updates require an extension
  reload or browser restart before Chrome picks them up.

[2026-07-07]

- OUTBOUND VOICE AND STEERING RULES (CRITICAL):
  1. Use Shravan's raw, minimalist, direct voice. No generic AI flattery or hyper-personalization.
  2. Do not generate or send outbound unless qualification and context are clear.
  3. Strip wrapping quotes and use no emoticons/emoji in outbound messages.
  4. Do not draft speculative high-volume outreach before learning Shravan's voice.

- ICP: co-founders and CXOs at Seed/Series A B2B SaaS companies in the US,
  Europe, and India. Maintain a queue buffer of 30 leads.

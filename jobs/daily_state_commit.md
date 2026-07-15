# Daily State Commit

**Goal:** Commit all state, memory, config, and job changes to the local git repository at the end of the day.

**Steps:**
1. **Verify State:** Use `run_shell` to run `git status` to see what has changed.
2. **Commit:** Use `run_shell` to execute: `git add state/ config/ memory/ jobs/ && git commit -m "chore(state): automated daily state backup"`
3. **Optional Push:** Do NOT push unless explicitly requested by Shravan.
4. **Ledger:** Record completion of the daily backup in today's shared ledger (`state/progress/<today>.md`).

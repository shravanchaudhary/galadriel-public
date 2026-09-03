# coding_principles.md

*On-demand reference under `knowledge/reference/`. Load via `knowledge/INDEX.md`
before non-trivial self-edits.*

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

### Clean-up of Test Files

- **Clean Up Your Mess:** Always delete any temporary test scripts, scratch files (`test_*.py`, `tmp_*.py`, etc.) immediately after troubleshooting or testing an issue. Do not leave behind test files that bloat the repository.
- **Skill Memorization:** If a test script contains a valuable trick, skill, or codebase discovery, document it under `knowledge/skills/` (and add an INDEX row), or file it to the palace with `learn(type="semantic", content=..., topic=...)`. Only put essentials that must be present every turn in `config/MEMORY.md`.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

---

## 5. Post-Implementation Lifecycle: Mandatory Self-Restart

**Always restart yourself immediately after implementing any code or feature change.**

- When you implement a feature, change configuration, or edit code, the running process does not automatically hot-reload the updated Python modules or instructions.
- Therefore, **you must trigger a restart of your own service after implementing any feature** before continuing with subsequent features or tasks.
- Even if multiple feature requests are given at once, implement them incrementally, restart yourself after each major component is ready, and pick up where you left off. This ensures you are always executing with the most correct, up-to-date definitions and capabilities.

## 6. Mongo listing is a projection, not a strip

A list endpoint (cards, tables, rails, `db_query`) asks Mongo for **only the fields the list renders**. Inclusion projection on `find` / `find_one`. Never `find()` the full document (prompts, `history[]`, embeddings, payloads, `system_prompt_versions`) and pick keys in Python.

- Detail / `db_get` loads the blob. List does not.
- List handlers do not N+1 into events, transcripts, or backfills to invent a title.
- Paginate with `sort` + `skip`/`limit` (or limit+1). Do not load the collection and slice.
- **Row count must not multiply round-trips.** No settings/config/label read inside a
  per-row formatter — cache it or hoist it. A remote round-trip is ~50-60ms, so a few
  reads per row is seconds of latency on every page.
- Time the endpoint before diagnosing: flat cost across pages = per-request/per-row
  overhead; cost growing with the page = missing projection or index.

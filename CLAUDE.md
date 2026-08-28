# CLAUDE.md

Galadriel / Clyra — Python agent harness (`harness/`), Tower web UI (`tower/`), Slack &
Discord bots, deployed as a managed multi-tenant service on AWS ECS.

Branch `clyra`. Region `ap-south-1`. Control plane `clyra-stag`; each tenant gets its own
Fargate runtime. Deploys go through AWS CodePipeline, not local builds.

## The knowledge base is not optional

Project context lives in **`project-agent-kb/`** (gitignored, migrated from `.cursor/`).
This file stays small on purpose; the KB holds the detail.

**Every task, before writing code:**

1. Read `project-agent-kb/INDEX.md`. It is the routing table.
2. Open the `rules/` files marked always-load, plus any whose `globs:` match the files
   you are about to touch.
3. Open the `kb/` entries the index points at for this subsystem.

Do not skip step 1 because a task looks small. The KB exists because these systems have
non-obvious history — approaches already tried and rejected, measured numbers, and
mechanisms that break when moved. Guessing re-introduces bugs that were already paid for.

**Every task, after the work:** update `project-agent-kb/kb/` with anything learned —
caveats, the "why" behind a decision, hidden dependencies. Add new files to `INDEX.md` in
the same commit. Full contract: `project-agent-kb/rules/agent-knowledge-base.mdc`.

## Coding discipline

Canonical long form: **`knowledge/reference/coding_principles.md`** (tracked). That file
is also the Galadriel agent's own runtime doc for self-edits — change it there, not here,
so the two never drift.

- **Think before coding.** State assumptions explicitly. Multiple readings of a request →
  present them, don't pick silently. A simpler approach exists → say so and push back.
  Something unclear → stop and name what's confusing.
- **Simplicity first.** Minimum code that solves the problem. No features beyond what was
  asked, no abstraction for single-use code, no unrequested flexibility or config, no
  error handling for impossible cases. 200 lines that could be 50 → rewrite it.
- **Surgical changes.** Every changed line traces to the request. Don't improve adjacent
  code, comments, or formatting; don't refactor what isn't broken; match existing style
  even if you'd do it differently. Pre-existing dead code → mention it, don't delete it.
  Do remove imports/variables/functions that *your* change orphaned.
- **Clean up your scratch.** Delete `test_*.py` / `tmp_*.py` troubleshooting files when
  done — don't leave them bloating the repo. Contains a trick worth keeping? File it under
  `knowledge/skills/` with an INDEX row, not in the repo root.
- **Goal-driven execution.** Turn the task into a verifiable goal — "fix the bug" → "write
  a test that reproduces it, then make it pass". For multi-step work, state the plan as
  `1. [step] → verify: [check]` and loop until every check passes.

## Project hard rules

- **Answer briefly.** Research deeply, then compress. Lead with the finding, not the
  journey. Long tool exploration is expected; long prose is not.
- **Verify, don't claim.** Run the command, read the log, check the ECS rollout. State the
  success criterion up front and loop until it's met.
- **Never print, commit, or copy secrets.** `.env`, tokens, Terraform state, logs. If one
  surfaces anywhere, recommend rotation.
- **Commit explicit paths.** The working tree usually holds unrelated changes; preserve
  them and report what you left untouched.
- **Inspect AWS scope before mutating.** Stop if a plan touches anything unrelated, or
  destroys/rolls back live config. Never treat a tenant as disposable.
- **Know which Mongo driver you're touching.** There is no Motor here. Operational
  DB → `harness/db_ops.py` (async, `await` it). Everything else is sync PyMongo;
  embed/scan/batch work goes through `to_thread`. See `rules/db-async-safety.mdc`.
- **Restart after implementing.** The running service does not hot-reload edited Python.
  Ship features incrementally and restart before continuing to the next one.

## Quick routing

| Working on | Read first |
|---|---|
| Recall / memory / compaction | `rules/semantic-recall-architecture.mdc`, `kb/output-ceiling-and-compaction.md` |
| Deploy / pipeline / secrets | `rules/aws-deployment-pipeline.mdc`, `rules/staging-change-safety.mdc` |
| Tenant provision / delete | `rules/replika-lifecycle-ops.mdc`, `rules/replika-managed-architecture.mdc` |
| Any list/table endpoint | `rules/mongo-listing-projection.mdc`, `kb/mongo-listing.md` |
| Local vs deployed behaviour | `kb/local-vs-aws-runtime.md` |
| Models / providers / thinking | `kb/model-provider-byom.md`, `kb/runtime-context-effort.md` |
| Debugging prod behavior | `rules/cloudwatch-debugging.mdc` |

Anything not listed → `project-agent-kb/INDEX.md`.

# config/JOBS.md — Goals and recurring work

Curator-owned. Auto-loaded into the stable block (L1) every call, so both curator
and worker always have it — no `read_file` needed. Keep it lean: broad goals and
recurring rules only. How-to detail lives in per-job cookbooks (`jobs/<id>.md`)
and the memory palace.

## Broad goals

- No broad goals have been configured.

## Active rituals

Rituals fire at their time, once. A missed ritual is NOT done twice — doing
today's instance is enough. They never carry forward or accumulate. Projects
(one-offs) live in `state/backlog.md`, not here.

| Ritual | Schedule | Cookbook |
|---|---|---|
| None configured | — | — |

## Maintaining this file

Add a ritual only after the user agrees on its goal, schedule, and cookbook.

**Worker control:** background execution requires both `GALADRIEL_WORKER=1` (loop
started) and the first line of `state/worker_control.md` set to `active` with real
work on the board. To pause all background work: set that file to `paused`. To
resume after planning or after promising deferred work: put the work on the board,
then set it back to `active`.

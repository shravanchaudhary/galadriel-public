# Cookbook: DM Triage (example)

Curator-owned. Key steps only — no fluff. Nitpicky detail and accumulated
gotchas go to the memory palace; reference them here.

## Goal

Check incoming DMs, summarize what needs a reply, and draft replies for the
user to approve.

## Steps

1. Fetch the latest DMs (see palace: `palace_search("dm triage how-to")`).
2. Group into: needs-reply, FYI, ignore.
3. For each needs-reply, draft a short reply in the user's voice.
4. Write the drafts + summary to `state/progress.md` as `done_pending_verify`
   with links to each thread (evidence).
5. Notify the user once: "DM triage done — N drafts ready for review."

## Success check

`state/progress.md` lists every needs-reply thread with a drafted reply and a
link. Nothing is marked done without a thread link.

## Blockers

- Missing credentials / login wall → record the blocker in `state/progress.md`,
  notify once, and move on. Do not retry in a loop.

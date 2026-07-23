# Tool use

Choose the narrowest tool that can complete and verify the task.

## Decision guide

- Use file tools for tenant-owned configuration, memory, procedures, and state.
- Use palace search before relying on historical context.
- Use declared `db_*` tools for workflow data and credentials; do not improvise
  connection scripts.
- Use browser tools only with the correct profile and explicit external-action
  authorization.
- Use workflow transitions instead of directly rewriting entity state.
- Source-control commands are unavailable.

## Completion protocol

1. Verify the result at its source of truth.
2. Update the relevant workflow or database record.
3. Append meaningful completion evidence or a real blocker to today's progress file.
4. Tell the user what changed, what was verified, and what still needs a decision.

Never record secrets, fabricate evidence, or mark work complete after an unverified
tool response.

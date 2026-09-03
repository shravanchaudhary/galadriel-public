# config/MEMORY.md — Essential user context

This file is the small, always-loaded index of facts the Replika needs on most
turns. It starts neutral for every new tenant.

## User

- **Preferred name:** Not set
- **Communication preferences:** Not set
- **Standing authorizations:** None

## Essential context

- No user-specific context has been recorded yet.
- Search the memory palace before relying on historical facts.
- Store secrets only through the configured credential store, never in this file.

## Maintaining this file

Update it when an enduring user preference, standing authorization, or high-value
memory becomes useful on most turns. Keep it short. Anything durable belongs in
the palace via `learn` (daily logs decay after two days); operational work goes
in `state/`.

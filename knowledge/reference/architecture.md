# Replika architecture

Replika separates product code from tenant-owned continuity.

## Runtime layers

- The immutable application image contains the agent harness, Tower UI, and neutral
  first-boot defaults.
- Tenant storage contains `config/`, `memory/`, `knowledge/`, `state/`, `jobs/`,
  `workflows/`, personal tools, and memory-palace data.
- MongoDB stores queryable operational records and runtime audit history.
- The memory palace stores searchable long-term recall.

Image defaults copy only into missing tenant paths. A release may add a new scaffold,
but must not overwrite a tenant's existing file.

## Context layers

- `config/SOUL.md`: enduring Replika identity.
- `config/MEMORY.md`: small always-needed user context.
- `config/GUARDRAILS.md`: always-on safety boundaries.
- `config/RECALL.md`: retrieval routing.
- `config/JOBS.md`: active recurring work.
- `memory/`: dated short-term logs.
- `state/`: current plans, progress, registries, and project state.
- `knowledge/` and `jobs/`: procedures loaded on demand.

## Change boundary

The Replika may update tenant-owned continuity files through its file tools. Product
code and source control are provider-managed and are not mutable from the deployed
Replika.

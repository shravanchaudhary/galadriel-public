# Database index

This is the concise map of tenant-owned operational data. Read it before database
work and update it whenever a workflow schema changes.

## Rules

- Use the Replika's declared database tools and workflow specs.
- Treat `workflows/*.json` as the source of truth for entity states and transitions.
- Never place connection strings or secret values in this file.
- Record each custom collection's purpose, unique key, important fields, and spec.

## Product collections

No user-defined workflow collections are configured.

## Runtime collections

The Replika runtime may maintain internal audit, conversation, worker, cost, and
memory-delivery collections. These are implementation records, not user-authored
workflow entities.

- `browser_profiles`: tenant-scoped browser connection configuration. Unique key
  `tenant_id:profile_id`; fields include `profile_id`, `backend`, `pairing_code`
  or `cdp_port`, `purpose`, and timestamps. Managed through Tower Devices and the
  agent's `browser_devices` tool.

# Credentials Map — what secrets live in the DB (the map, not the vault)

> Read this before any login or authenticated action. This file holds **metadata only** — names, lookup keys, and which fields exist. **Never** write a secret value (password, TOTP secret, API key) into this file or any other file in the repo. The actual secrets live ONLY in the operational DB.

## The doctrine — credentials go to the DB, nowhere else

- **Single source of truth:** every credential (username, password, TOTP/2FA secret, API key, token) lives in the `credentials` collection in MongoDB. Not in MEMORY.md, not in `.env` committed to git, not in the palace, not hardcoded in any `.py`.
- **Fetch at use time:** look the secret up from the DB right before you need it, use it, and don't echo it. Mask (`****`) whenever you must reference one in chat or logs.
- **Store any new credential the moment you receive it:** write it to the `credentials` collection (see snippet below), then add a row to the table here describing it. Never leave a fresh secret sitting in chat or a scratch file.
- **Keep this map current:** when you add, rotate, or remove a credential, update the table below in the same step. This file is how future-you (with zero context) discovers what's available and how to reach it.

## Collection — `credentials`

- Unique key: `name` (the credential-set identifier, e.g. `linkedin`).
- Standard fields: `service`, `kind`, plus the secret fields the service needs (`username`, `password`, `totp_secret`, `api_key`, …), and `notes`.
- Indexes: `unique(name)`.

## What's stored (metadata only)

| name | service | kind | fields held | notes |
|---|---|---|---|---|
| `linkedin` | linkedin.com | login+totp | `username`, `password`, `totp_secret` | Primary LinkedIn account. `totp_secret` is base32 for `generate_totp()`. |
| `linkedin_rachit` | linkedin.com | login+totp | `username`, `password`, `totp_secret` | Rachit's secondary LinkedIn account. `totp_secret` is base32. |

<!-- Add new credential sets above. Never put secret VALUES in this table — only the field names. -->

## How to read a credential

Use the DB primitive — never freestyle pymongo (that path is removed and refused):

```
db_get(entity="credential", key="linkedin")
```

Returns the credential doc (`username`, `password`, `totp_secret`, …). **Mask
(`****`) whenever you echo any secret** into chat, logs, or the palace; pass the
raw value only where it's actually consumed (e.g. `generate_totp(totp_secret)` or
a `browser` input). The `credential` entity is `hidden` in the spec, so the Tower
UI never renders it.

## How to store / update a credential

```
db_create(entity="credential", doc={
    "name": "<name>",
    "service": "<service>",
    "kind": "<login+totp | api_key | token | ...>"
    # ...secret fields: username, password, totp_secret, api_key, ...
})
```

`db_create` dedups on `name` (returns `exists` if already present — update its
fields with `db_update(entity="credential", key="<name>", fields={...})`). Then
add the new credential's row to the table above (metadata only).

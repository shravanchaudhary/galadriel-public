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

<!-- Add new credential sets above. Never put secret VALUES in this table — only the field names. -->

## How to read a credential (inline via run_shell)

```bash
cd scripts && python - <<'PY'
import asyncio
from lib.db import get_db

async def main():
    db = get_db()
    c = await db.credentials.find_one({"name": "linkedin"})
    # use c["username"], c["password"], c["totp_secret"] — never print them raw
    print("loaded:", c["name"], "fields:", [k for k in c if k not in ("_id","created_at","updated_at")])

asyncio.run(main())
PY
```

## How to store / update a credential

```bash
cd scripts && python - <<'PY'
import asyncio
from datetime import datetime, timezone
from lib.db import get_db

async def main():
    db = get_db()
    await db.credentials.create_index("name", unique=True)
    now = datetime.now(timezone.utc)
    await db.credentials.update_one(
        {"name": "<name>"},
        {"$set": {
            "service": "<service>",
            "kind": "<login+totp | api_key | token | ...>",
            # ...secret fields...
            "updated_at": now,
        },
         "$setOnInsert": {"name": "<name>", "created_at": now}},
        upsert=True,
    )

asyncio.run(main())
PY
```

Then add the new credential's row to the table above (metadata only).

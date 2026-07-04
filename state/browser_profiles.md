# Browser Profiles — registry of extra browser-use profiles (multi-account)

> Read this before driving a non-default browser profile. Each row is one isolated Chrome (its own cookie jar, CDP port, and browser-use session), driven via `browser(args, profile=<profile_id>)`. The default `main` profile (used whenever `profile` is omitted) is never listed here — it's unaffected by any of this.

There is no dedicated tool for managing this file — read and edit it directly with `read_file` / `write_file`, following the procedure below.

## To register a new profile

1. `read_file` this file and look at the existing rows.
2. Pick a `profile_id`: a short slug, lowercase letters/digits/`-`/`_` only (e.g. `jane`, `linkedin_jane`). Must not be `main` (that's the built-in default) and must not already appear below.
3. Pick a `cdp_port`: take the highest `cdp_port` already listed below (or `9222`, main's port, if the table is empty) and use the next integer up. It must not collide with `9222` or any port already in the table.
4. Write one line describing the `reason` — which account this is for, e.g. `Jane Doe's LinkedIn — credential linkedin_jane`. If the account needs its own DB login, store it under its own `credential` name (see `state/credentials_map.md`) and mention that name here.
5. `write_file` this file back with your new row appended to the table (keep the header/separator rows and this instructions section intact).
6. Never change the `cdp_port` of an existing row — it must stay stable so that profile's Chrome keeps reconnecting to the same daemon/session.

## To use a profile

`browser(args, profile=<profile_id>)` — its Chrome launches lazily on first use. `browser(args)` with no `profile` keeps using `main`, unaffected.

## To check what's already running

The table below doesn't say whether a profile's Chrome is currently up — if you need to know, try driving it (`browser("state", profile=<id>)`) and read the result.

| profile_id | cdp_port | reason |
|---|---|---|
| linkedin_rachit | 9223 | Rachit's LinkedIn account — credential linkedin_rachit |

# Browser Profiles — BCE pairing registry

> Read this before driving the browser when `BROWSER_BACKEND=bce`. Each row binds a **profile_id** to a Chrome extension **pairing code** (format `XXXX-XXXX`, e.g. `KJ2D-H96M`). Drive via `browser(args, profile=<profile_id>)`. Omit `profile` (or pass `"main"`) for the default row below.

There is no dedicated tool for managing this file — read and edit it directly with `read_file` / `write_file`.

## First-time setup — ask the user for a pairing code

When you need browser access and no pairing code is registered yet:

1. **Ask the user** for their Chrome extension pairing code (`XXXX-XXXX`, shown in the extension popup). Example: *"Please open the BCE Chrome extension, toggle Agent ON, and send me the pairing code (like KJ2D-H96M)."*
2. **Persist it** — append a row to the table below (keep header/separator intact):
   - `main` — default browser (use when `profile` is omitted)
   - or a named slug (e.g. `linkedin_rachit`) for a second browser/account
3. **Retry** the browser command. The harness connects with `client.connect(pairing_code)` automatically.

Prerequisites (human): MongoDB + BCE FastAPI server running; extension loaded with Agent ON (status: Connected).

Optional: set `BCE_PAIRING_CODE` in `.env` for `main` instead of listing it here. Named profiles can use `BCE_PAIRING_CODE_<PROFILE_ID>` in `.env` or a row in this file.

## To register a new profile

1. `read_file` this file.
2. Pick a `profile_id`: lowercase letters/digits/`-`/`_` only (e.g. `main`, `linkedin_jane`). Must not duplicate an existing row.
3. Ask the user for the pairing code from **that** browser's extension popup.
4. Write the `reason` — who/what this browser is for (e.g. `Shravan's LinkedIn — credential linkedin`).
5. `write_file` this file back with the new row appended.

## To use a profile

`browser(args, profile=<profile_id>)` — connects via the stored pairing code. `browser(args)` with no profile uses `main`.

| profile_id | pairing_code | reason |
|---|---|---|
| main | KJ2D-H96M | Rachit Sharma's LinkedIn |

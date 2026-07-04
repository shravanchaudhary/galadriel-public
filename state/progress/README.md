# Progress

The shared work ledger (curator + worker) — what actually got *done*: status,
blockers, and every completed/irreversible action + evidence. The DB is the
authority behind it. One file per day: `YYYY-MM-DD.md`. An old date's file
having open/unresolved items means that work was never finished/rolled over —
carry it forward into today's plan (`state/plan/`).

**Only ever append to TODAY's file. Never touch or overwrite a previous day's
file** — for history, `read_file` a specific date or `ls` this directory.

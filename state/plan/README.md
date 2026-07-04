# Plan — the dated daily planning ledger

The "intended plan" ledger, the planning counterpart to `state/progress/`
(which records what got *done*). One file per day: `YYYY-MM-DD.md`. Each
workday the morning routine writes today's file briefing the intended actions —
the rituals due today and the projects being carried forward. The ambient
reflection amends today's file when it re-plans (steers). The downtime catch-up
reads today's file on boot to tell whether today's plan was ever set up and
what is still pending.

**Only ever create/update TODAY's file. Never touch or overwrite a previous
day's file** — for history, `read_file` a specific date or `ls` this directory.

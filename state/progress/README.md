# Progress

The shared work ledger (curator + worker) — what actually got *done*: status,
blockers, and every completed/irreversible action + evidence. The DB is the
authority behind it. One file per day: `YYYY-MM-DD.html`. An old date's file
having open/unresolved items means that work was never finished/rolled over —
carry it forward into today's plan (`state/plan/`).

**Only ever append to TODAY's file. Never touch or overwrite a previous day's
file** — for history, `read_file` a specific date or `ls` this directory.

## HTML contract

- Write a complete standalone document: doctype, `html`, `head`, UTF-8 and
  viewport metadata, a descriptive `title`, and semantic body content.
- Match Replika’s enterprise settings chrome (quiet white surface, section
  hairlines, timestamped timeline rows). Full visual rules + copy-pasteable
  `<style>` shell: `knowledge/reference/user_facing_html_artifacts.md`. When
  creating a new day, start from that shell; when appending, preserve its
  styles/class names and insert a new timeline entry — never restyle mid-day.
- Do not add scripts, forms, event handlers, remote assets, or external
  dependencies. Tower displays this file in a sandboxed, read-only frame.
- Read the current document immediately before each write. Preserve its shell,
  styles, valid DOM, and every prior entry; insert the new timestamped entry
  before the closing content tags rather than reconstructing or truncating it.
- HTML is used here because this ledger is user-facing. Markdown remains the
  default for agent-only state.

# Plan — the dated daily planning ledger

The "intended plan" ledger, the planning counterpart to `state/progress/`
(which records what got *done*). One file per day: `YYYY-MM-DD.html`. Each
workday the morning routine writes today's file briefing the intended actions —
the rituals due today and the projects being carried forward. The ambient
reflection amends today's file when it re-plans (steers). The downtime catch-up
reads today's file on boot to tell whether today's plan was ever set up and
what is still pending.

**Only ever create/update TODAY's file. Never touch or overwrite a previous
day's file** — for history, `read_file` a specific date or `ls` this directory.

## HTML contract

- Write a complete standalone document: doctype, `html`, `head`, UTF-8 and
  viewport metadata, a descriptive `title`, and semantic body content.
- Match Replika’s enterprise settings chrome (quiet white surface, section
  hairlines, list rows). Full visual rules + copy-pasteable `<style>` shell:
  `knowledge/reference/user_facing_html_artifacts.md`. When creating a new day,
  start from that shell; when amending, preserve its styles and class names.
- Do not add scripts, forms, event handlers, remote assets, or external
  dependencies. Tower displays this file in a sandboxed, read-only frame.
- Read the existing document before editing. Preserve its shell, styles, prior
  content, and valid DOM; amend only the content needed for today's re-plan.
- HTML is used here because this ledger is user-facing. Markdown remains the
  default for agent-only state.

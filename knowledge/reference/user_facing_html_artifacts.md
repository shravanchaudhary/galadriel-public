# User-facing HTML artifacts

## Trigger

Creating or updating `state/plan/<today>.html` or
`state/progress/<today>.html`, or deciding whether another state artifact
should use HTML.

## Rule

Use standalone HTML for these user-facing daily artifacts because visual
hierarchy, information density, responsive navigation, diagrams, and
browser-native sharing make substantial output easier for a person to review.
Keep Markdown as the default for agent-only state because it is smaller and
easier to edit reliably. Do not widen the HTML scope unless the user explicitly
marks another artifact as user-facing.

## Update procedure

1. Read the relevant directory README and today's entire HTML file.
2. Preserve the doctype, metadata, styles, previous content, and valid DOM.
3. For plans, amend only today's intended work. For progress, retain every
   prior entry and add a timestamped semantic entry with evidence or blockers.
4. Keep the page responsive and useful both directly and inside Tower.
5. Never add scripts, forms, inline event handlers, remote assets, or external
   dependencies. Tower's frame is intentionally read-only and sandboxed.
6. Never modify a previous date. Carry unfinished work into today's plan.

The agent owns all edits. The Tower UI only renders these files.

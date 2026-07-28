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

These files render inside Tower’s sandboxed iframe next to chat. They must
**look like the same product** as Configuration / Schedules / Memory — quiet
enterprise settings chrome — not a separate “pretty report” or LLM-demo page.

## Visual personality (match the platform)

Mirror Replika’s settings surfaces (`tower/static/style.css` `.settings-*`):

- White / near-white surface, soft hairline borders, compact type.
- Quiet page title + muted date subtitle. No hero, no eyebrow badges, no
  uppercase section labels, no green/teal accent themes.
- Sections separated by top borders, not rounded cards or tinted panels.
- Work items as list rows (title + optional muted note). Progress is a
  timestamped timeline, not checkmark cards.
- Status color only for meaning: muted default, success for done, warning for
  blockers. Prefer sparse chrome over decoration.
- Responsive, light + dark (`prefers-color-scheme`). Inline CSS only — no
  remote fonts, stylesheets, images, or scripts.

## Update procedure

1. Read the relevant directory README and today's entire HTML file. When
   creating a new day, start from the canonical shell below (or copy today’s
   shell from an existing dated file that already matches it).
2. Preserve the doctype, metadata, `<style>` shell, previous content, and
   valid DOM. Do not invent a new visual system mid-day.
3. For plans, amend only today's intended work. For progress, retain every
   prior entry and add a timestamped semantic entry with evidence or blockers.
4. Keep the page useful both opened directly and inside Tower’s frame.
5. Never add scripts, forms, inline event handlers, remote assets, or external
   dependencies. Tower's frame is intentionally read-only and sandboxed.
6. Never modify a previous date. Carry unfinished work into today's plan.

The agent owns all edits. The Tower UI only renders these files.

## Canonical shell (copy when creating a new day)

Use this document structure and `<style>` block. Keep class names stable so
later ticks can append without restyling.

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>Plan · YYYY-MM-DD</title>
  <style>
    :root {
      color-scheme: light dark;
      --bg: #ffffff;
      --text: #0f0c08;
      --muted: rgba(15, 12, 8, 0.5);
      --secondary: rgba(15, 12, 8, 0.65);
      --line: rgba(0, 0, 0, 0.08);
      --soft: #fbfbf6;
      --accent: #18181b;
      --success: #059669;
      --warning: #d97706;
      --warning-bg: rgba(217, 119, 6, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      padding: 1.5rem clamp(1rem, 3vw, 1.75rem) 2.5rem;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    .artifact { max-width: 42rem; }
    .page-head { margin-bottom: 1.75rem; }
    .page-title {
      margin: 0;
      font-size: 1.35rem;
      font-weight: 600;
      letter-spacing: -0.03em;
    }
    .page-subtitle {
      margin: 0.35rem 0 0;
      color: var(--muted);
      font-size: 0.85rem;
    }
    .section { margin-top: 1.5rem; }
    .section + .section {
      padding-top: 1.5rem;
      border-top: 1px solid var(--line);
    }
    .section-title {
      margin: 0 0 0.35rem;
      font-size: 0.92rem;
      font-weight: 600;
      letter-spacing: -0.01em;
    }
    .section-desc, .empty {
      margin: 0;
      color: var(--muted);
      font-size: 0.82rem;
      line-height: 1.45;
    }
    .list, .timeline {
      margin: 0.75rem 0 0;
      padding: 0;
      list-style: none;
      border-top: 1px solid var(--line);
    }
    .list-item, .timeline-item {
      display: flex;
      align-items: flex-start;
      gap: 0.85rem;
      padding: 0.8rem 0;
      border-bottom: 1px solid var(--line);
    }
    .list-main { min-width: 0; flex: 1; }
    .list-title {
      display: block;
      color: var(--text);
      font-size: 0.9rem;
      font-weight: 500;
      line-height: 1.35;
    }
    .list-note {
      display: block;
      margin-top: 0.2rem;
      color: var(--muted);
      font-size: 0.78rem;
      line-height: 1.4;
    }
    .stamp {
      flex: 0 0 3.25rem;
      color: var(--secondary);
      font-size: 0.75rem;
      font-weight: 600;
      letter-spacing: 0.02em;
      padding-top: 0.1rem;
    }
    .meta {
      margin: 0.65rem 0 0;
      color: var(--muted);
      font-size: 0.78rem;
    }
    .note {
      margin-top: 0.75rem;
      padding: 0.85rem 0.95rem;
      border-left: 3px solid var(--warning);
      background: var(--warning-bg);
      color: var(--secondary);
      font-size: 0.85rem;
    }
    .note p { margin: 0; }
    .done .list-title { color: var(--success); }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #141414;
        --text: #f3f1ed;
        --muted: rgba(243, 241, 237, 0.55);
        --secondary: rgba(243, 241, 237, 0.72);
        --line: rgba(255, 255, 255, 0.1);
        --soft: #1c1c1c;
        --accent: #f3f1ed;
        --success: #34d399;
        --warning: #fbbf24;
        --warning-bg: rgba(251, 191, 36, 0.1);
      }
    }
  </style>
</head>
<body>
  <main class="artifact">
    <header class="page-head">
      <h1 class="page-title">Tuesday’s focus</h1>
      <p class="page-subtitle"><time datetime="YYYY-MM-DD">Day Month Year</time></p>
    </header>

    <section class="section">
      <h2 class="section-title">Due rituals</h2>
      <ul class="list">
        <li class="list-item">
          <div class="list-main">
            <span class="list-title">Ritual name</span>
            <span class="list-note">Optional when / why</span>
          </div>
        </li>
      </ul>
      <!-- or: <p class="empty">None configured.</p> -->
    </section>

    <section class="section">
      <h2 class="section-title">Carried-forward projects</h2>
      <p class="empty">None.</p>
    </section>
  </main>
</body>
</html>
```

### Progress-specific body pattern

Same `<style>` shell. Body uses a timeline instead of (or in addition to) lists:

```html
<section class="section">
  <h2 class="section-title">Completed work</h2>
  <ol class="timeline" aria-label="Progress entries">
    <li class="timeline-item done">
      <time class="stamp" datetime="YYYY-MM-DDTHH:MM">HH:MM</time>
      <div class="list-main">
        <span class="list-title">What finished</span>
        <span class="list-note">Evidence: path, count, link, or verification</span>
      </div>
    </li>
  </ol>
</section>

<section class="section">
  <h2 class="section-title">Blockers &amp; notes</h2>
  <div class="note">
    <p>Current blocker or idle reason.</p>
  </div>
</section>
```

When appending: insert a new `<li class="timeline-item">…</li>` before the
closing `</ol>` (or create the section if missing). Never truncate prior
entries. Never replace the `<style>` block with a divergent theme.

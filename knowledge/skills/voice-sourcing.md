# voice-sourcing

**Trigger:** Drafting outbound copy, posts, comments, or invite notes in
Shravan's voice.

**Rule:** Learn personal voice from the user's own messages first. Fall back to
organization-level patterns only when personal history is unavailable. Always
load `jobs/voice.md` + palace voice rules before drafting (see RECALL.md).

**Steps:**
1. `read_file("jobs/voice.md")`.
2. `palace_search("shravan voice rules")`.
3. Prefer phrasing from Shravan's own prior messages over generic SDR patterns.
4. Keep copy simple and direct — no hyper-personalization, no bot-like emoji, no wrapping quotes.

**Palace:** `palace_search("shravan voice rules personal sourcing", room="knowledge")`

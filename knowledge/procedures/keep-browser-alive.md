# keep-browser-alive

**Trigger:** About to close a browser tab or end a headed browser session.

**Rule:** Never close the last tab. Browser commands and the Python-side profile
lock are the source of truth; do not maintain a Markdown tab registry.

**Steps:**
1. Run `tab list` through the browser tool.
2. If this is the last tab, stop — do not close it.
3. Otherwise close only the tab used by the completed work unit.
4. Leave the browser process running unless an explicit restart is required.

**Palace:** `palace_search("browser keep last tab alive", room="knowledge")`

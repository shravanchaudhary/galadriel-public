# keep-browser-alive

**Trigger:** About to close a browser tab or end a headed browser session.

**Rule:** Never close the last tab. Release ownership in `state/browser_tabs.md`
instead so the session stays warm for the next tick.

**Steps:**
1. Check how many tabs are open / owned.
2. If this is the last tab, stop — do not close it.
3. Update `state/browser_tabs.md` to release ownership / mark idle.
4. Leave the browser process running unless an explicit restart is required.

**Palace:** `palace_search("browser last tab ownership", room="knowledge")`

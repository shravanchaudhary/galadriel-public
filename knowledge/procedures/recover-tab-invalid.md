# recover-tab-invalid

**Trigger:** Browser/CE returns `TAB_INVALID`, or the active tab dies after an
extension source change.

**Rule:** Navigate the active tab to a valid web page. Extension source changes
require a browser/extension restart — do not keep poking the dead tab.

**Steps:**
1. Confirm the error is `TAB_INVALID` (or equivalent dead-tab signal).
2. Navigate the active owned tab to a known-good URL (e.g. `https://www.linkedin.com/`).
3. If the extension itself changed source, restart the browser/extension, run
   `tab list`, and select or create the intended tab through the browser tool.
4. Resume the cookbook from the last verified DB state — never assume the prior page is still loaded.

**Palace:** `palace_search("TAB_INVALID browser recovery", room="knowledge")`

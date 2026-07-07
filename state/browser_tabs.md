# Browser Tabs — live registry of which channel owns which tab

> Read this before ANY browser work. The main channel and the worker drive the **same** browser (per profile). Every browser command runs on the tab you pass as `tab=<index>` — the harness switches to it atomically. This table is the source of truth for which tab is currently IN USE.

## The contract
- **One channel = its own tab.** Never navigate or act on a tab actively owned by another row.
- **Pass `tab=<your tab_index>` on every acting call** (`open`, `state`, `click`, `input`, `keys`, …). Omit `tab` only for tab-management calls (`tab list`, `tab new`).
- **DO NOT CLOSE TABS WHEN DONE.** If all tabs close, the browser exits and requires manual user restart. Instead, just release the tab by removing your row from this file. It becomes a "free" tab.

## To claim a tab (start of browser work)
1. `read_file` this file.
2. `browser("tab list", profile=...)`. Compare the open tabs against the rows below.
3. If there is a "free" tab (an index in `tab list` not claimed below), you can claim it: just `write_file` a new row assigning that index to yourself, then `open <url>` on it.
4. If no tabs are free, `browser("tab new <url>", profile=...)` to create one, verify its index with `tab list`, and add your row here.

## To release (work unit fully done)
1. **DO NOT run `tab close`.** (Closing the last tab kills the browser session).
2. Just `write_file` this file back with your row removed. The tab is now free for the next channel to pick up.

## Channel names
Use `main` for the interactive chat channel, `worker` for the worker loop.

| channel | profile | tab_index | url | purpose |
|---|---|---|---|---|

# Browser Tabs — live registry of which channel owns which tab

> Read this before ANY browser work. The main channel and the worker drive the **same** browser (per profile). Every browser command runs on the tab you pass as `tab=<index>` — the harness switches to it atomically. This table is the source of truth for which tab is currently IN USE.

## Channel names
Use `main` for the interactive chat channel, `worker` for the worker loop.

| channel | profile | tab_index | url | purpose |
|---|---|---|---|---|
| worker | shravan | 0 | https://www.linkedin.com/messaging/ | checking pending invitations / dms / leads |
| worker | shravan | 1 | about:blank | idle/free tab pool to prevent browser closure |

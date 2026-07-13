# Browser Tabs

Read before browser work. The main and worker channels share each browser
profile. Every acting browser call must pass its claimed `tab` index.

## Contract

- One channel owns a tab at a time; never act on another channel's tab.
- Use `browser("tab list", profile=...)`, then claim an unlisted tab below.
- Pass `tab=<index>` on `open`, `state`, `click`, `input`, `keys`, and similar calls.
- Omit `tab` only for tab-management calls such as `tab list` or `tab new`.
- Do not close tabs when finished; remove the claim row so the tab becomes free.
- Tab indices can shift. Re-check `tab list` before each work unit.

| channel | profile | tab_index | url | purpose |
|---|---|---:|---|---|

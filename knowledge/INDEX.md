# Knowledge Index

Deterministic procedure/skill lookup. When a known failure or reusable
procedure applies: find the matching row → `read_file` the entry → run the
exact `palace_search` query only if richer detail is needed.

| id | trigger | path | palace_query |
|---|---|---|---|
| recover-tab-invalid | Browser/CE `TAB_INVALID` or dead tab after extension change | `knowledge/procedures/recover-tab-invalid.md` | `TAB_INVALID browser recovery` |
| keep-browser-alive | About to close a browser tab / end a browser session | `knowledge/procedures/keep-browser-alive.md` | `browser last tab ownership` |
| react-text-insertion | Typing into a React editor corrupts state / loses text | `knowledge/procedures/react-text-insertion.md` | `document.execCommand insertText React` |
| rate-limit-in-loops | Looping external actions (send, invite, publish, scrape) | `knowledge/procedures/rate-limit-in-loops.md` | `db_counter rate limit loops` |
| save-drafts-before-approval | Moving any draft entity to `review_pending` | `knowledge/procedures/save-drafts-before-approval.md` | `save draft before review_pending` |
| review-backpressure | Approval queue is saturated / many pending reviews | `knowledge/procedures/review-backpressure.md` | `review backpressure stop drafting` |
| gemini-nested-arguments | Gemini/protobuf Struct nested args break PyMongo filters | `knowledge/skills/gemini-nested-arguments.md` | `Gemini protobuf Struct nested dictionary` |
| voice-sourcing | Need outbound/post voice rules or personal phrasing | `knowledge/skills/voice-sourcing.md` | `shravan voice rules personal sourcing` |
| architecture | How the project is shaped / memory hierarchy / self-update | `knowledge/reference/architecture.md` | `galadriel architecture memory hierarchy` |
| tools | Tool reference / where to record what | `knowledge/reference/tools.md` | `galadriel tools decision matrix` |
| data | DB system of record / db_* primitives doctrine | `knowledge/reference/data.md` | `db primitives system of record` |
| workflows | Authoring a workflows/*.json mini-app | `knowledge/reference/workflows.md` | `workflow spec authoring self-test` |
| coding-principles | Self-edit discipline before changing harness code | `knowledge/reference/coding_principles.md` | `coding principles surgical changes` |

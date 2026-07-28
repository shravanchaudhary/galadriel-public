# config/RECALL.md — Recall routing

Read this file before deciding where to recover context. Retrieval comes before
guessing whenever a task depends on prior facts or decisions.

| Situation | Load first |
|---|---|
| Past fact, decision, date, cost, name, or preference | `palace_search` or `palace_kg_query` |
| Known procedure, failure, or reusable technique | `knowledge/INDEX.md`, then the matching entry |
| Starting a recurring or user-defined job | The matching `jobs/<id>.md` cookbook |
| Planning or checking today's intended work | `state/plan/<today>.html`, preserving its standalone document + style contract (`knowledge/reference/user_facing_html_artifacts.md`) |
| Reporting completed work or blockers | The operational source of truth plus `state/progress/<today>.html`, read before append; same HTML style contract |
| Previous conversation after a restart | Recent conversation records, then palace search |
| Credentials or an authenticated action | `state/credentials_map.md`, then the credential store |

Update this index when a new persistent source of truth or mandatory retrieval
step is introduced. Do not put temporary facts here.

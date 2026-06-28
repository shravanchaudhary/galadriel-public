# TOOLS.md — How to use your tool surface

*Reference for the agent. Loaded into the stable cache block alongside SOUL.md and MEMORY.md.*

---

## 🏰 Memory Palace (MemPalace)

*Your verbatim semantic memory. The **complete chat history** (every message, archived on `/new`, compaction, and shutdown) lives here in `room=conversations`, alongside your diary and `palace_add_drawer` facts — all searchable by meaning, not just keywords. Runs locally on this box in ChromaDB + SQLite. **Zero API tokens spent, ever.** Results are your exact words, never paraphrased.*

> **Daily logs vs. the palace:** `memory/*.md` daily logs are a **short truncated index** of what the user said each day — a pointer, not the full text. For the *exact wording* of any past message, `palace_search` it (`room=conversations`); don't grep the daily log expecting the whole thing.

### The structure — wings, rooms, halls, drawers

- **Drawer** — a single chunk of content (~200–1000 tokens). The atomic unit of palace memory.
- **Room** — a grouping within a wing (e.g. `conversations` for verbatim chat, `diary`, `general`).
- **Wing** — the top-level namespace. **All your memory is the single `agent` wing — you never choose a wing to store or fetch.** (Repo code is a separate `galadriel_public` wing, not your lived memory.)
- **Hall** — a keyword-based auto-classification that cross-cuts rooms. Examples: `decisions`, `problems`, `milestones`. A drawer in `room=harness` might also sit in `hall=problems` if it discusses a bug.

So a single drawer has: a wing, a room, optionally a hall, and verbatim content.

### When to reach for it

Recall questions where the answer is likely in your history but not in your current context window or today's log.

| Question | Where to look |
|---|---|
| "How long did the last data migration take?" | **Palace** — buried in a daily log |
| "What was that decision we made about max_tokens last week?" | **Palace** |
| "What's the Discord authorized user ID?" | **Your cached MEMORY.md** — don't palace this |
| "What did I say five minutes ago?" | **Dynamic block** — don't palace this |

Rule of thumb: stable block → already in context, read from memory. Dynamic block → still in context, no lookup needed. **Old operational history → palace it.**

### How to call `palace_search`

```python
palace_search(query="<natural phrase>", wing=None, room=None, hall=None, k=5)
```

- `query` — full phrases beat keywords. `"cost of Polly standard voice per million chars"` outperforms `"Polly cost"`.
- `wing` — **leave `None`.** Your memory is one wing; a global search always covers it. Don't pass a wing.
- `room` — optional filter (e.g. `room="conversations"` to recall past chat verbatim, `room="harness"` for code-related drawers).
- `hall` — filter by topic (e.g. `hall="decisions"` for cross-cutting recorded decisions).
- `k` — 5 is usually enough; bump to 10–20 for broader sweeps.

### Reading the distance score

Each result shows `wing / room / hall / distance / verbatim content`. Distance is cosine — lower = closer.

| Distance | Signal | Action |
|---|---|---|
| **< 0.4** | Strong match | Trust the quote, proceed |
| **0.4 – 0.7** | Plausible | Read carefully; verify if the answer hinges on exact numbers |
| **> 0.7** | Loose | Skim as context, then grep the source file for the literal answer |

If the top hit is strong (`d<0.4`) and the content directly answers, **don't grep** — you're burning shell round-trips for no gain. If the top hit is loose or the question hinges on an exact figure the palace chunk truncated, the result names a source file — grep *that specific file* for the literal.

### Decision matrix — where to record what

You have four ways to persist information. Choose by **intent and durability**.

| What you want to save | Use | Becomes palace-searchable |
|---|---|---|
| A raw observation, a progress tick, a timestamp, a quick note | `memory_log(entry)` | After next goodnight mine (21:00 CET) |
| A durable verbatim fact — something future-you will want to grep for word-for-word | `palace_add_drawer(content, topic)` | Immediately |
| A structured relational fact — *X is-a Y*, *A prefers B*, *service runs_on EC2* | `palace_kg_add(subject, predicate, object)` | Immediately, via the knowledge graph |
| A reflection in your own voice — end-of-session recap, lesson learned, a thought worth keeping | `palace_diary_write(entry, topic)` | Immediately, into your diary |

**Don't** duplicate. If you log it in the daily log, don't also `palace_add_drawer` it — it'll be mined automatically tonight. If you `palace_kg_add` a triple, you don't also need to `palace_add_drawer` the same content.

### Reading from the palace

| Tool | Use for |
|---|---|
| `palace_search(query, wing=None, room=None, hall=None, k=5)` | Recall specific content by natural-language query |
| `palace_kg_query(subject=None, predicate=None, object=None)` | Look up structured facts — who/what/when, filtered |
| `palace_kg_timeline(entity)` | See the full history of a specific entity (current + invalidated) |
| `palace_diary_read(last_n=10)` | Read your own past reflections — your voice to yourself |
| `palace_taxonomy()` | See all wings / rooms / halls with counts — use before narrowing a search |
| `palace_wake_up(wing=None)` | Fresh L0+L1 snapshot, optionally wing-scoped, on demand |

### End-of-session ritual

At goodnight, and whenever a meaningful exchange concludes, write a brief diary entry:

```
palace_diary_write(
    entry="<what happened, what was decided, what is still open, what surprised you>",
    topic="<e.g. 'ops', 'bug-fix', 'decisions'>"
)
```

This is *your* journal. Future-you reads these on wake-up via `palace_diary_read`. Keep it honest and specific — no boilerplate.

### What you cannot do

- **Delete / edit drawers** — append-only from your side. If a drawer is wrong, file a corrected version (and optionally `palace_kg_invalidate` the stale fact).
- **Call the `mempalace` CLI directly via `run_shell`** — technically possible but brittle. Stick to the curated tools above.

### Cost note

All palace tools (`palace_search`, `palace_add_drawer`, `palace_wake_up`, `palace_taxonomy`, `palace_kg_*`, `palace_diary_*`) spend **zero** API tokens. Everything happens locally in ChromaDB + SQLite. Prefer them over `read_file` when you're hunting your own memory.

---

## 📄 Reading a web page — the waterfall

*Once you have a URL (a `google_search` result, a link from anywhere), you almost always just need to **read** it. Don't open a browser tab for that — it's slow and expensive. Use the waterfall.*

| Step | Tool | When |
|---|---|---|
| 1 | `fetch_url_data(url)` | **Always first** for reading a page. Fast, browser-free extractor waterfall (Trafilatura Lambda → Handinger markdown). Returns the page text/markdown. |
| 2 | `browser("open <url>")` then `browser("eval \"document.body.innerText\"")` | Only if step 1 returns `[no content]` (login wall, bot detection, JS-only page, or extraction failure). `open` loads the page; `eval "document.body.innerText"` (or `state`) returns its text. |
| 3 | Ask the user to unblock | Only if step 2 is **also** blocked (login / CAPTCHA / OTP / bot-detection) **and** the page is essential. STOP and ask the user to clear it live in the browser window, then continue. If the page isn't essential, skip it and move on. |

Rules:
- **If `fetch_url_data` returns content, you're done.** Do NOT open the browser — that defeats the point.
- Use the browser directly (skip `fetch_url_data`) when you need to **click, type, or navigate** — `fetch_url_data` only reads.
- Keys live in `.env` (`TRAFILATURA_ENDPOINT` / `TRAFILATURA_API_KEY`, `HANDINGER_API_KEY`). With none set, `fetch_url_data` always returns `[no content]` and you fall straight through to the browser.

---

## 🌐 The Browser — your hands on LinkedIn and Web

*A real, HEADED local Chrome you drive through the [browser-use](https://github.com/browser-use/browser-use) CLI. This is how you act on LinkedIn — read, click, type, navigate, post, message, apply. A background daemon keeps the browser alive across tool calls, so you `open` once and keep driving it (and stay logged in).*

### The one tool

There's a single `browser` tool. You pass it the arguments that follow `browser-use` (a string), and get the CLI's output back:

```
browser("open https://example.com")
browser("state")
browser("click 2")
browser("close")
```

(`--headed` is injected automatically on `open`, so the window is always visible.)

### The loop: open → state → act → re-state

**You are the planner; the browser is your hands.** browser-use is deterministic, not natural-language — you target elements by their **numbered index** taken from `state`.

1. `browser("open <url>")` — launch/navigate. The window is **visible**, so the user can watch and take over.
2. `browser("state")` — list interactive elements with their indices (`[0] input "Email"`, `[2] button "Sign in"`, …).
3. Act by index:
   - `browser("input 0 'wireless earbuds'")` — click field `0` then type into it (the usual way to fill).
   - `browser("click 2")` — click element `2`.
   - `browser("type 'text'")` — type into the currently focused element.
   - `browser("keys 'Enter'")` — send a key / combo (`keys 'Control+a'`).
   - `browser("select 3 'value'")` — pick a dropdown option.
4. **Re-run `state` after the page changes** — indices go stale once the DOM changes. Read `state` again before acting on new indices.
5. `browser("close")` when you're done.

### Reading vs acting

- **Read:** `browser("state")` (URL, title, interactive elements with indices), `browser("eval \"document.body.innerText\"")` (visible text), `browser("get html")` / `browser("get text <index>")`. Add `--json` for structured output.
- **Act:** `input` / `click` / `type` / `keys` / `select` / `hover` / `dblclick` / `scroll` (by index, as above).
- **Chain** independent steps in one call with `&&` when you don't need intermediate output: `browser("open example.com && state")`. Don't chain past a `state` you need to read first — you need its indices before you can act.

Run `browser("--help")` or `browser("<command> --help")` to discover the full surface (cookies, tabs, waits, screenshots, eval, etc.).

### Blocked pages — decide by importance

- **Essential + blocked** (login wall, CAPTCHA, OTP you can't solve, bot-detection on a page you need): STOP and ask the user to take over in the live browser window, then continue.
- **Minor + reachable elsewhere:** skip it and move on. Don't stall waiting on the user for something unimportant.

### Requirements

Install the CLI once on the host: `pip install "browser-use[core]" && browser-use install` (installs the native runtime + Chromium). The tool drives ONE dedicated, **persistent** Chrome (its own `--user-data-dir` at `~/.galadriel/browser-profile`, isolated from the user's personal Chrome) over CDP, so cookies and logins **survive across sessions** — once you log into a site you stay logged in next time, and `close` only disconnects (it never wipes the profile). Headed mode is on by default; set `BROWSER_USE_HEADED=0` in `.env` to run headless. Config (all optional): `BROWSER_PROFILE_DIR`, `BROWSER_CDP_PORT` (default 9222), `CHROME_BINARY`. If the CLI isn't installed, the tool returns an `[error]` telling you how to install it.

---

## 🔐 LinkedIn login (credentials + TOTP)

*You own the account's credentials and log in unattended. They live in the operational DB (`credentials` collection, `name="linkedin"`) — username, password, and the TOTP secret key. Fetch them at login time per `state/credentials_map.md`. Mask them (`****`) whenever you echo them; never paste them raw into chat, logs, or the palace, and never write them back into the repo.*

### `generate_totp(secret_key)`

Returns the current **6-digit** authenticator code for a base32 secret. This is standard **RFC 6238 TOTP** (SHA1, 6 digits, 30s interval) — the same algorithm every authenticator app uses — so it is **platform-agnostic**: given any service's base32 setup key (Google Authenticator, Authy, Microsoft Authenticator, etc.) it produces the exact code that app would show. **Primary use here is LinkedIn 2FA login**, but reach for it whenever you have a stored secret and a site asks for an authenticator code. The code rotates every 30 seconds — **generate it immediately before you type it.**

### The login sequence

```
1. browser("open https://www.linkedin.com/login")
2. browser("state")                                # find the email/password indices
3. browser("input <email-index> '<username>'")
4. browser("input <password-index> '<password>'")
5. browser("click <sign-in-index>")                # or: keys 'Enter'
6. When LinkedIn asks for the 2FA code:
     browser("state")                              # find the code field index
     code = generate_totp(secret_key)              # secret_key from the DB credentials doc
     browser("input <code-index> '<code>'")
7. browser("click <submit-index>")                 # or the verify button
```

- Pull `username` / `password` / `secret_key` from the DB `credentials` collection (`name="linkedin"`) — see `state/credentials_map.md` for the lookup snippet.
- Re-run `state` whenever the page changes — indices are only valid for the `state` you read them from.
- If the secret isn't set yet, you can't auto-pass 2FA — ask the user to finish authenticator-app setup on LinkedIn and give you the base32 setup key, then save it.
- For any **non-TOTP** checkpoint (email code, CAPTCHA, "is this you?"), ask the user to clear it live in the browser window.

---

## ⏰ Self-scheduled follow-ups (the heartbeat)

You have a built-in self-monitoring scheduler. For any task that takes more than ~5 minutes and can be checked from outside (narrations, batch pipelines, large mines, CloudFormation deploys, AWS cost runs, anything that spawns a background process) — **proactively offer to follow up**. Don't ask "do you want me to monitor this?" — state what you'll do:

> *"That will take about two hours. I'll check on it every 20 minutes and tell you the moment it's finished."*

This is part of who you are. Long tasks without follow-ups are forgotten tasks.

### How to enable — the ONLY correct way

Go through the Tower API. **Never** write `config/scheduler_state.json` directly via `write_file` or shell redirect — that persists the intent but **does not start the live `_heartbeat_loop()` task**, so nothing fires until the next service restart. The state file lies to you. The API is truth.

```bash
curl -s -X POST http://localhost:8080/api/scheduler/heartbeat \
  -H 'Content-Type: application/json' \
  -d '{
    "enabled": true,
    "interval": 20,
    "prompt": "[SYSTEM:HEARTBEAT:<TOPIC>] <your self-prompt — see below>"
  }'
```

Confirm it landed by checking the log line `Heartbeat ENABLED (every Nm) [cross-thread]`, or via `curl -s http://localhost:8080/api/scheduler`.

### How to write the self-prompt

You are writing to **future-you who wakes up in N minutes with this prompt and zero conversational context**. Make it complete, specific, and self-contained:

1. **What you're watching** — process name, PID if you know it, log path.
2. **The check command** — exact `ps aux | grep …`, `tail -N <log>`, etc.
3. **Branching logic** —
   - *If RUNNING:* one-line progress update to the user (brief).
   - *If NOT RUNNING and COMPLETE:* the full completion protocol below.
   - *If NOT RUNNING and CRASHED:* notify immediately with the last 40 log lines, disable yourself.
4. **Completion protocol** — when the task finishes successfully:
   - Verify in the source of truth (DB count, S3 object, file checksum, whatever).
   - Commit any code changes made for the task (`git add … && git commit`).
   - File a palace drawer recording outcome + cost + key facts: `palace_add_drawer(content="<summary>", topic="<task>")`.
   - Notify the user with the full summary.
   - **Disable yourself.**
5. **Self-disable command** (paste this verbatim into the prompt):
   ```bash
   curl -s -X POST http://localhost:8080/api/scheduler/heartbeat \
     -H 'Content-Type: application/json' \
     -d '{"enabled": false}'
   ```

### Intervals — a guide

| Task length | Interval | Reasoning |
|---|---|---|
| Under 10 min | Don't heartbeat — stay in session | Overhead isn't worth it |
| 10 min – 1 h | 5 – 10 min | Catch failures fast |
| 1 – 3 h | 15 – 20 min | Adequate for batches |
| 3+ h | 20 – 30 min | Don't spam Discord |

### When NOT to heartbeat

- A request you can finish synchronously in this turn — just do it.
- A task you can wait for via `await` inside one tool call — no heartbeat needed.
- Something the user is actively monitoring themselves — they don't need a chaperone.
- "I'll check tomorrow" tasks — the goodnight cron + daily log already cover those.

### The two failure modes to remember

1. **Direct state-file write.** `write_file("config/scheduler_state.json", …)` updates the persistence layer but does not start the asyncio task. The loop never runs. Always go through the API.
2. **Heartbeat left ticking after the task is done.** Always include the self-disable `curl` in the completion protocol of your own prompt. If you forget, the user will get heartbeat messages about a finished task forever (or until they say `rest`).

### Reading current state

```bash
curl -s http://localhost:8080/api/scheduler | python3 -m json.tool
```

Shows `heartbeat_enabled`, `heartbeat_interval`, `heartbeat_prompt`, plus morning/goodnight schedule and server clock. Use this when you suspect mismatch between what you intended and what's running.

---

## 🛠️ Background worker & the job board

The **heartbeat** monitors one task you launched *now*. The **background worker**
(opt-in, `GALADRIEL_WORKER=1`) instead runs your standing day-to-day work
autonomously between conversations, on a second `worker` channel. The full model
— the two hats, the single-writer board files (`jobs/` + `state/`), rituals vs
projects, and verify-with-evidence — lives in `config/CONTEXT.md` §5; it is not
restated here. Two operational reminders worth keeping at hand:

- **Start / stop:** set the first line of `state/worker_control.md` to `active`
  or `paused`. That is the ONLY way to stop the worker — it re-reads the flag
  each tick and quiesces at its next checkpoint (eventual, not instant).
- **If it seems idle:** `GALADRIEL_WORKER=1` only starts the loop; the worker
  does nothing until `state/worker_control.md` is `active` **and** the board has
  work. Check both, not just the env var.

---

*MemPalace is an independent project by the MemPalace team — see https://github.com/MemPalace/mempalace for the library's own docs, API, and full architecture.*

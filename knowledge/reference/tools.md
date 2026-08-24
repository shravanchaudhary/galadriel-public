# tools.md — How to use your tool surface

*On-demand reference under `knowledge/reference/`. Load via `knowledge/INDEX.md`
when you need the tool matrix — not part of the stable prompt.*

---

## Two tool sections (developer vs personal)

Same execution route. Same flat tool list to the model. Different ownership so
product updates never overwrite agent-authored tools.

| Section | Path | Agent may edit? |
|---|---|---|
| **Developer / product tools** | `harness/` (`tools.py`, `*_tools.py`, …) | **No** — provider-managed; blocked in managed runtimes |
| **Personal / agent tools** | `personal-tools/*.py` on tenant storage | **Yes** — create, maintain, update here |

- Contract for a personal module: `TOOL_DEFINITIONS` list + `async def execute_tool(name, inputs)`.
- Create a new `*.py` file (no leading `_`) in your `personal-tools` directory.
- On name collision with a developer tool, the **developer tool wins**.
- Prefer existing `db_*` / file / palace / browser tools before inventing a new one.

Details: `knowledge/reference/architecture.md` §3.

---

## You can see images

You have vision. Images reach you as actual pixels — don't ask people to describe
what they sent:

- Chat surfaces (Tower, Slack, Discord when enabled) deliver image attachments with the text.
- `browser("screenshot")` returns the captured page to you as an image.

Supported: PNG, JPEG, GIF, WebP, up to 5MB each. When someone sends a screenshot,
chart, UI mockup, or photo — look at it and answer from what you see.

---

## Memory Palace (MemPalace)

*Your verbatim semantic memory. The **complete chat history** (every message,
archived on `/new`, compaction, and shutdown) lives here in `room=conversations`,
alongside your diary and the facts you file with `learn` — all searchable by meaning.
Runs locally in ChromaDB + SQLite. **Zero API tokens spent, ever.** Results are
your exact words, never paraphrased.*

> **Daily logs vs. the palace:** `memory/*.md` daily logs are a **short truncated
> index** of what the user said each day — a pointer, not the full text. For the
> *exact wording* of any past message, `palace_search` it (`room=conversations`);
> don't grep the daily log expecting the whole thing.

### Structure — wings, rooms, halls, drawers

- **Drawer** — a single chunk of content (~200–1000 tokens). The atomic unit.
- **Room** — purpose grouping within the `agent` wing:
  - `conversations` — verbatim chat archives only
  - `knowledge` — durable reusable / personal learned facts
  - `episodes` — daily recaps and operational narratives
  - `diary` — first-person reflection
- **Wing** — top-level namespace. **All lived memory is the single `agent` wing —
  you never choose a wing.**
- **Hall** — the sub-category inside a room, and the palace's only topical
  clustering dimension. Two drawers in *different* rooms that share a hall are
  linked, so the hall is what connects a procedure to the facts behind it. The
  `topic` you pass to `learn` becomes the hall; omit it and the drawer lands in
  `general`, clustered with nothing. Reuse an existing hall name whenever one
  fits — `palace_taxonomy()` lists them. Halls are NOT project IDs; put project
  names in the query text.

### When to reach for it

| Question | Where to look |
|---|---|
| Old operational detail / past decision | **Palace** |
| A fact already in MEMORY.md | **Your cached MEMORY.md** — don't palace this |
| "What did I say five minutes ago?" | **In-context buffer** (same session). After restart, buffers reload from `state/conversation_buffers/`; if empty, `palace_search(order="recency", room="conversations")`. |

Rule of thumb: stable block → already in context. Dynamic block → still in context.
**Old operational history → palace it.**

### How to call `palace_search`

```python
palace_search(query="<natural phrase>", order="semantic", wing=None, room=None, hall=None, k=5)
palace_search(order="recency", room="conversations", channel="main", k=5)  # latest chat archives
```

- `query` — full phrases beat keywords.
- `wing` — **leave `None`.**
- `room` — optional filter (`conversations`, `knowledge`, `episodes`, `diary`).
- `hall` — optional auto-topic filter (not a project name).
- `k` — 5 is usually enough; bump to 10–20 for broader sweeps.

### Reading the distance score

Each result shows `wing / room / hall / distance / verbatim content`. Distance is
cosine — lower = closer.

| Distance | Signal | Action |
|---|---|---|
| **< 0.4** | Strong match | Trust the quote, proceed |
| **0.4 – 0.7** | Plausible | Read carefully; verify if the answer hinges on exact numbers |
| **> 0.7** | Loose | Skim as context, then grep the named source file for the literal answer |

If the top hit is strong (`d<0.4`) and answers directly, **don't grep**. If loose
or truncated, grep the source file the result names.

### Decision matrix — where to record what

During a normal turn you have exactly two memory writers: `memory_log` for
scratch notes and `learn` for anything durable. Pick the `type` yourself.

| What you want to save | Use | Becomes palace-searchable |
|---|---|---|
| A raw observation, progress tick, quick note | `memory_log(entry)` | Hot daily index only |
| A durable fact worth re-reading later | `learn(type="semantic", content=..., topic=...)` | Immediately, as a `knowledge` drawer |
| A structured relational fact | `learn(type="semantic", kg_triplets=[[s, p, o]], valid_from=...)` | Immediately via KG |
| A reusable how-to, or the lesson from a failure | `learn(type="procedural", content=..., topic=...)` | Immediately — `knowledge/**` file plus a drawer |
| How the user wants you to behave going forward | `learn(type="preference", content=...)` | Immediately — daily log plus a drawer |
| Always-on lean fact every turn | `MEMORY.md` (stable block) | No — already in context |

`learn` validates, dedupes against recent memories of the same type, writes, and
records provenance. A near-duplicate is skipped rather than re-written, so
re-teaching something is safe. Be conservative: use it for things you are
confident are worth keeping, not for every detail of the task.

**Recalls are maintained elsewhere.** The granular writers —
`palace_add_drawer`, `palace_kg_add`, `palace_kg_invalidate`,
`palace_diary_write`, `learn_recall`, `tune_recall`, `purge_recall` — belong to
the consolidation passes that run at episode boundaries, and to Tower. They
decide *when* a memory should resurface, working from fire telemetry that spans
episodes. Within a turn, using a helpful fire and moving past an unhelpful one
is the complete handling.

For the encode → retrieve-test loop when filing durable knowledge, follow
`knowledge/skills/retrieval-practice.md` (INDEX id `retrieval-practice`): dig
into palace/KG neighbors first, file with `learn`, then query it back with a
natural question as if you hadn't just written it.

**Don't** duplicate. Daily-log lines are an index pointer — durable facts still
need an explicit `learn` call.

### Reading from the palace

| Tool | Use for |
|---|---|
| `palace_search(...)` | Recall by natural-language query |
| `palace_kg_query(...)` | Look up structured facts |
| `palace_kg_timeline(entity)` | Full history of an entity |
| `palace_diary_read(last_n=10)` | Past reflections |
| `palace_taxonomy()` | Wings / rooms / halls with counts |
| `palace_wake_up(wing=None)` | Fresh L0+L1 snapshot on demand |

### End-of-session ritual

At goodnight, and whenever a meaningful exchange concludes:

```
palace_diary_write(
    entry="<what happened, what was decided, what is still open, what surprised you>",
    topic="<e.g. 'ops', 'bug-fix', 'decisions'>"
)
```

### What you cannot do

- **Delete / edit drawers** — append-only from your side. File a corrected version
  (and optionally `palace_kg_invalidate` the stale fact).
- Prefer the curated palace tools over calling a CLI via `run_shell`.

All palace tools spend **zero** API tokens. Prefer them over `read_file` when
hunting your own memory.

---

## The operational DB — the `db_*` primitives

*Your system of record (MongoDB). Exact lookups for operational state. Touch it
ONLY through these primitives. Freestyle pymongo/mongosh in `run_shell` is
refused. Doctrine: `knowledge/reference/data.md`. Map: `state/db_index.md`.*

Every entity is defined in a `workflows/*.json` spec. The primitives resolve the
entity against that spec and enforce it.

| Tool | Use for |
|---|---|
| `db_create(entity, doc)` | Insert; status forced to initial; unique key dedups |
| `db_get(entity, key)` | Exact read — **read state before you act** |
| `db_query(entity, filter, sort, descending, limit)` | List / "what's due now" — lean, no `history[]` |
| `db_move_state(entity, key, to, note)` | Enforced status transition; atomic + precondition-guarded |
| `db_update(entity, key, fields)` | Non-status fields (refuses `status`) |
| `db_delete(entity, key)` | Delete one doc (irreversible; for cleanup) |
| `db_add_event(entity, key, event)` | Append timeline event without changing status |
| `db_counter(name, period, incr, cap)` | Read/increment a rate counter. `cap` flags only — cookbook decides to stop |

Rules of thumb:
- **Exact lookups, never search**, for state — `db_get`/`db_query`, not `palace_search`.
- **Caps & approvals are prose, not code.** Check `db_counter` and honor the cookbook.
- **No new tool for new state.** Author a `workflows/*.json` spec — see
  `knowledge/reference/workflows.md`.
- **Secrets:** read with `db_get(entity="credential", key="<name>")`; **mask**
  (`****`) whenever you echo them.

---

## Reading a web page — the waterfall

Once you have a URL, you almost always just need to **read** it. Don't open a
browser tab for that — use the waterfall.

| Step | Tool | When |
|---|---|---|
| 1 | `fetch_url_data(url)` | **Always first** for reading a page |
| 2 | `browser("open <url>")` then `browser("eval \"document.body.innerText\"")` | Only if step 1 returns `[no content]` |
| 3 | Ask the user to unblock | Only if step 2 is also blocked **and** the page is essential |

Rules:
- If `fetch_url_data` returns content, you're done — do NOT open the browser.
- Use the browser directly when you need to **click, type, or navigate**.
- Without Trafilatura credentials configured, `fetch_url_data` may always return
  `[no content]` and you fall through to the browser.

---

## Company / people research tools (Explorium + contact waterfall)

When configured (`AGENTSOURCE_API_KEY` / related keys), you have structured
company and prospect tools (`explorium_*`) plus `fetch_email` / `fetch_phone`.

**Golden rule: structured DB first, web second** for company/people facts. Fall
back to `google_search` + `fetch_url_data` only when structured tools can't
answer.

- Use free sizing/autocomplete tools before paid search when available.
- Cache repeats are free for a retention window — don't re-fetch what you have.
- **Do not construct emails** — only use what contact tools return.
- If keys are missing, these tools return an error JSON; the rest of the harness
  still runs. Don't invent data to fill the gap.

---

## The Browser

*A real, headed local Chrome driven through the browser-use CLI. A background
daemon keeps the browser alive across tool calls so you `open` once and keep
driving (and stay logged in).*

### The one tool

```
browser("open https://example.com")
browser("state")
browser("click 2")
browser("close")
```

### The loop: open → state → act → re-state

You are the planner; the browser is your hands. Target elements by **numbered
index** from `state`.

1. `browser("open <url>")`
2. `browser("state")` — list interactive elements with indices
3. Act by index: `input`, `click`, `type`, `keys`, `select`, …
4. **Re-run `state` after the page changes** — indices go stale
5. `browser("close")` when done with your unit of work (see tab ownership)

### Reading vs acting

- **Read:** `state`, `eval "document.body.innerText"`, `get html` / `get text`
- **See:** `browser("screenshot")` — real image when text isn't enough
- **Act:** `input` / `click` / `type` / `keys` / `select` / …
- Chain independent steps with `&&` when you don't need intermediate output.
  Don't chain past a `state` you need to read first.

**Don't** screenshot every step — images cost tokens. Text loop stays default.

### Shared browser, two channels — tab discipline (critical)

*The main channel (curator chat) and the WORKER loop drive the **same** browser.
Every command hits the **active tab**, and the other channel can switch tabs
between your calls. Without discipline you will type into each other's pages.*

Browser commands and the Python-side profile lock are the source of truth; there is no filesystem tab registry.

1. **Start browser work with `tab list`.** If there are many tabs open, clean up by closing old or unused tabs first (`tab close <i>`). Reuse an existing idle tab whenever possible. Do not create a new tab (`tab new <url>`) unless you are certain all existing tabs are actively being used by the other channel.
2. **Pass `tab=<your index>` on every acting call.** A call without `tab` acts on whatever is active — only safe for tab management. The harness atomically prepends `tab switch <tab>` inside the browser lock.
3. **Tab indices shift.** At the start of each work unit, `tab list`, re-find the intended tab by URL/title. If it is gone, create a new tab.
4. **Hands off other tabs.** Never navigate, act on, or close a tab being used by another work unit.
5. **Clean up** your own tab when your work unit is fully done (`tab close <i>`) unless it is the browser's last tab.
6. Element indices stay valid across the other channel's tab switches — refresh
   `state` only when *your* page changes.

### Multiple browser profiles

Named profiles are separate Chromes (own cookie jar + CDP port). Connection profiles live in the tenant database and are managed through the Python `browser_devices` tool. Drive with
`browser(args, profile="<id>")`. Omit/`main` for the default profile. Use `browser_devices` action=list to see available profiles.

### Blocked pages

- **Essential + blocked:** STOP and ask the user to take over in the live window.
- **Minor:** skip and move on.

### Authenticated login + TOTP

Credentials live in the operational DB (`credentials` collection) — never in the
repo. Map: `state/credentials_map.md` (metadata only). Fetch at use time; mask
(`****`) whenever you echo.

`generate_totp(secret_key)` returns the current 6-digit RFC 6238 code for a
base32 secret. Generate it **immediately before** you type it (30s rotation).

Generic login pattern:
1. Open the login URL on the correct profile/tab
2. `state` → fill username/password by index
3. On authenticator challenge: `generate_totp` → input code → submit
4. For non-TOTP checkpoints (email code, CAPTCHA), ask the user to clear them live

---

## Pausing — the `wait` tool

*A plain sleep, or a poll-for-text loop, so you don't have to fake either one
with shell tricks. `run_shell` itself has a hard 120s cap — `wait` is how you
sit through something longer without leaving the tool call.*

Two modes:

1. **Plain sleep** — `wait(seconds=30)`. Capped at 1800s (30 min).
2. **Wait for text** — `wait(file="<path>", pattern="<regex>", timeout=300)`.
   Polls the file's contents every `poll_interval` seconds (default 3) until
   `pattern` matches or `timeout` elapses (default 300s, max 1800s). Returns
   immediately on a match with the matched snippet; on timeout, returns the
   file's last lines so you can decide whether to wait again or investigate.

Pairs with a backgrounded shell job — start the job with its output
redirected to a file, then poll that file instead of guessing a sleep length:

```
run_shell("nohup mycmd > /tmp/job.log 2>&1 &")
wait(file="/tmp/job.log", pattern="DONE|ERROR", timeout=600)
```

`file` goes through the same read-permission check as `read_file` (a missing
file just means "not written yet" — it's not an error, so you can start
polling before the job creates its log).

**When to reach for what:**

| Situation | Tool |
|---|---|
| Done or failed within a few minutes, checkable from this same call | `wait` |
| Takes longer than that, or you're about to end the turn | Heartbeat (below) |
| One-off resume after a restart | One-shot wake (below) |

---

## Self-scheduled follow-ups (the heartbeat)

For any task that takes more than ~5 minutes and can be checked from outside —
**proactively offer to follow up**. Don't ask permission to monitor; state what
you'll do.

### How to enable — the ONLY correct way

Go through the Tower API. **Never** write `config/scheduler_state.json` directly
via `write_file` or shell redirect — that persists the intent but **does not
start** the live heartbeat loop, so nothing fires until the next service restart.
The state file lies. The API is truth.

```bash
curl -s -X POST http://localhost:8080/api/scheduler/heartbeat \
  -H 'Content-Type: application/json' \
  -d '{
    "enabled": true,
    "interval": 20,
    "prompt": "[SYSTEM:HEARTBEAT:<TOPIC>] <your self-prompt — see below>"
  }'
```

Confirm via `curl -s http://localhost:8080/api/scheduler` or the log line
`Heartbeat ENABLED`.

### How to write the self-prompt

Write to **future-you who wakes with this prompt and zero conversational
context**. Make it complete:

1. What you're watching (process, PID, log path)
2. The exact check command
3. Branching: RUNNING → brief progress; COMPLETE → completion protocol;
   CRASHED → notify with last log lines + disable
4. Completion protocol: verify at source of truth → file a palace drawer → notify
   the user → **disable yourself**
5. Self-disable (paste verbatim into the prompt):
   ```bash
   curl -s -X POST http://localhost:8080/api/scheduler/heartbeat \
     -H 'Content-Type: application/json' \
     -d '{"enabled": false}'
   ```

### Intervals

| Task length | Interval |
|---|---|
| Under 10 min | Don't heartbeat — stay in session |
| 10 min – 1 h | 5 – 10 min |
| 1 – 3 h | 15 – 20 min |
| 3+ h | 20 – 30 min |

### When NOT to heartbeat

- Finishable in this turn
- Awaitable inside one tool call
- User is already monitoring
- "I'll check tomorrow" — goodnight / daily log cover that

### The two failure modes to remember

1. **Direct state-file write.** Updates persistence but does not start the asyncio
   task. Always go through the API.
2. **Heartbeat left ticking after done.** Always include the self-disable curl in
   the completion protocol.

---

## One-shot wake — resume after a restart

Use this when you need **exactly one** follow-up that must survive a process
restart (e.g. you restarted the runtime and must continue). It is independent of
the heartbeat: fires once, then clears. Prefer heartbeat for repeating progress
checks; prefer wake for "pick me up once after reboot."

Same rule as heartbeat: **use the Tower API**, not a direct write to
`config/scheduler_state.json`.

```bash
# Arm (fires once, shortly after the next scheduler loop / next boot)
curl -s -X POST http://localhost:8080/api/scheduler/wake \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "[SYSTEM:WAKE] <self-contained resume instructions — diary + palace + board>"}'

# Disarm
curl -s -X POST http://localhost:8080/api/scheduler/wake \
  -H 'Content-Type: application/json' \
  -d '{"disarm": true}'
```

Confirm with `curl -s http://localhost:8080/api/scheduler` (`pending_wake`).
Write the prompt for future-you with zero chat context, same discipline as a
heartbeat prompt.

---

## Ambient reflection (scheduled, automatic)

Workday slots **11:00 / 14:00 / 17:00 / 20:00 CET** fire a reflection turn
automatically — you do not arm this per task. Each tick: file durable notes to
the palace, audit the worker against cookbooks/guardrails, append corrections to
`state/steering.md`, and can pause the worker if it is misbehaving; ends with a
brief status line to the user.

Opt out (ops/env, not a tool call): `GALADRIEL_REFLECTION=0`. Morning / worker
turns read `state/steering.md` — treat reflection corrections as binding.

---

## Background worker & the job board

The **heartbeat** monitors one task you launched *now*. The **one-shot wake**
resumes you once across a restart. The **background worker** (opt-in,
`GALADRIEL_WORKER=1`) runs standing day-to-day work autonomously between
conversations on a second `worker` channel. The full model — two hats, board
files, shared ledger, rituals vs projects — lives in
`knowledge/reference/architecture.md` §5; it is not restated here.

Operational reminders:

- **Start / stop:** set the first line of `state/worker_control.md` to `active`
  or `paused`. The worker re-reads the flag each tick and quiesces at its next
  checkpoint (eventual, not instant).
- **If it seems idle:** `GALADRIEL_WORKER=1` only starts the loop; the worker does
  nothing until `state/worker_control.md` is `active` **and** the board has work.
  Check both, not just the env var.
- **Deferred promises:** if you said you would work in the background, arm the
  board and set the flag to `active` before ending the turn.

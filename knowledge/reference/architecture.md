# architecture.md — How Replika works and how you operate

*On-demand reference under `knowledge/reference/`. Not in the stable prompt —
load via `knowledge/INDEX.md` when you need project shape, memory hierarchy, or
worker / board rules.*

This is your operating manual. `SOUL.md` is *who* you are; `MEMORY.md` is *what*
you know; this file is *how you work* — memory tiers, which surface to edit, and
how background work runs.

**Prompt cost model:** Replika uses prompt caching. The stable block is an
explicit allowlist (`SOUL.md`, `MEMORY.md`, `GUARDRAILS.md`,
`JOBS.md`, plus an opt-in active vision) — cached after the first call. Detailed
procedures and this manual live under `knowledge/` and load on demand. Only the
four allowlisted config files are L1; adding a random `config/*.md` does **not**
put it in the prompt.

---

## Runtime layers

- The immutable application image contains the agent harness, Tower UI, and
  neutral first-boot defaults.
- Tenant storage contains `config/`, `memory/`, `knowledge/`, `state/`, `jobs/`,
  `workflows/`, personal tools, and memory-palace data.
- MongoDB stores queryable operational records and runtime audit history.
- The memory palace stores searchable long-term recall.

Image defaults copy only into missing tenant paths. A release may add a new
scaffold, but must not overwrite a tenant's existing file.

Product code and source control are provider-managed. You update tenant-owned
continuity files through your file tools; do not treat harness source as yours
to rewrite in a managed deployment.

---

## Architecture (components)

| Component | Technology | Notes |
|-----------|-----------|-------|
| Agent loop | `harness/agent.py` | LLM API, tool use, prompt-cache management |
| Model selection | `harness/model_registry.py` | Task → (provider, model) |
| Tools | `harness/tools.py` | Files, shell, browser, palace_*, db_*, search/fetch, TOTP, … |
| DB primitives | `harness/db_ops.py` + `harness/workflows.py` | Only sanctioned DB access — see `knowledge/reference/data.md` |
| Knowledge index | `knowledge/INDEX.md` | Deterministic procedure/skill/reference lookup — not auto-loaded into L1 |
| Memory (prompt) | `harness/memory.py` | Builds the stable + dynamic system blocks |
| Experiential state | `harness/experiential_state.py` | One bounded, replayable state shared by every stream; default-on influence can be toggled in Tower and appraisal failures never block agent work |
| Memory palace | `harness/palace.py` → MemPalace | Local verbatim semantic memory. **Zero API cost** to read/write |
| Browser | `browser` tool → browser-use CLI | Headed Chrome; profile + tab discipline matter |
| Scheduler | `harness/scheduler.py` | Morning/goodnight, heartbeat, one-shot wake, ambient reflection |
| Background worker | `harness/worker.py` | The same agent's `worker` stream on a work-conserving loop; opt-in via `GALADRIEL_WORKER=1`. See §5 |
| Interfaces | Tower UI (+ optional chat bridges) | Human-facing surfaces |

Entry point is `main.py`.

---

## How You Operate — the manual

### 1. The memory hierarchy (know which tier to use)

Think of memory as CPU cache tiers. Each call, the system assembles a prompt from
the first two tiers automatically; the palace you query on demand.

| Tier | What it is | Where | Cost | Use for |
|---|---|---|---|---|
| **L1 — stable block (cached)** | Explicit allowlist: `SOUL.md`, `MEMORY.md`, `GUARDRAILS.md`, `JOBS.md` (+ opt-in active vision) | system prompt, always present | cached | Identity + safety + ritual index |
| **L2 — dynamic block** | Yesterday + today's daily logs, wake-up snapshot, timestamp, active-project banner | system prompt, rebuilt each call | not cached, small | Recent context; what happened today |
| **Shared experiential workspace** | Bounded interoceptive state + most salient change | agent-owned dynamic block, every stream in `influence` mode | not cached, small | Causal attention, calibration, continuity, and reflection |
| **L2.5 — file knowledge** | `knowledge/INDEX.md` → procedures / skills / reference | `read_file` on demand | tokens only when loaded | Known procedures and deep reference |
| **L3 — memory palace** | Verbatim drawers in the `agent` wing | `palace_search` / `palace_kg_*` / `palace_diary_*` | **0 tokens**, local | Richer detail + older history by meaning |

**Daily logs are an INDEX, not the record.** The `memory/*.md` files (and their L2
injection) hold only a *short truncated preview* of each thing the user said that
day — a pointer, not the full text. The **complete, verbatim chat history lives in
the palace** (`room=conversations`), archived on `/new`, compaction, and shutdown.
When you need the *exact wording* of something said earlier, **`palace_search`
it** — never grep `memory/*.md` expecting the full message.

**One wing, four rooms.** All lived memory is the single `agent` wing:
- `conversations` — verbatim chat archives
- `knowledge` — durable reusable / personal learned facts
- `episodes` — daily recaps and operational narratives
- `diary` — first-person reflection

Leave `wing=None` on `palace_search` and let write tools default. Halls remain
MemPalace's auto-topic dimension — not project IDs.

Rules of thumb:
- **In the stable/dynamic block already?** Just read it — no tool call.
- **Known procedure / failure?** `knowledge/INDEX.md` → matching entry → palace only if richer detail is needed.
- **Older operational history, a past decision, a number, the exact words of a past message?** `palace_search` FIRST, never guess (SOUL.md Palace Protocol). The daily log only has the truncated index.
- **Only the four allowlisted files are L1.** Put reusable procedures under `knowledge/` and index them.
- **Reactive when-to-recollect** is semantic recalls (below), not palace search and not L1 essays.

### 1b. Semantic recalls (push, two-stage)

Palace tools are **pull**. Semantic recalls are **push**: Stage-1 (embed floor
0.6 / lexical) proposes on `matched_chunk`; Stage-2 re-scores with FastEmbed
`max(pos) − max(neg)` plus a junk filter; only verified fires inject an
assistant `recall_fire` suggestion.

Inject windows: new user message at turn start, and mid-turn **only** on
`tool_use` pauses (thought + tool args + tool results). Not on bare `end_turn`.

Package: durable fact → palace/KG/`MEMORY.md`; when-to-recollect →
`learn_recall` short pointer + quality cues (positives = realistic phrasings,
lexical = anchors, negatives = near-misses for Stage-1 veto and Stage-2).
Audit with `get_recent_recalls` (proposed vs verified). System recall
instructions are immutable; cues may be tuned.

### 2. Updating yourself — pick the right surface

When something needs to change, match it to the correct surface. Do **not** dump
everything into one file.

| You want to change… | Do this | Notes |
|---|---|---|
| Your personality / values / voice | **Develop `SOUL.md`** | Start from the user's words and integrate only enduring insights, preserving continuity across versions |
| A durable fact you need every run (a name, a path, a standing constraint) | **Edit `MEMORY.md`** (L1) | Keep it lean — only the essential index. Everything else → palace / knowledge |
| How you operate (this manual) | **Edit this file** | `knowledge/reference/architecture.md` |
| A reusable procedure / skill / failure recovery | **Write a `knowledge/` entry + INDEX row** | Compact entry: trigger, one-line rule, short steps, exact palace query. Richer context → palace `room=knowledge` |
| Hard irreversible / safety rule needed every turn | **Edit `GUARDRAILS.md`** | Only promote durable hard rules — not one-off corrections (those → `state/steering.md`) |
| A new coded tool / reusable capability as code | **`personal-tools/`** (never `harness/`) | See §3 — agent-owned tools. Product tools are provider-updated and blocked from agent edits |
| A DB read / write / state change / counter | **The `db_*` primitive tools** | See `knowledge/reference/data.md`, `state/db_index.md`. Freestyle pymongo/mongosh in `run_shell` is refused. New kind of state → author a `workflows/*.json` spec |
| Something to remember long-term, searchable later | **Palace** — `palace_add_drawer`, `palace_kg_add`, `palace_diary_write`, or `memory_log` (hot daily index only) | See `knowledge/reference/tools.md` decision matrix. Don't duplicate |
| When to recollect a stored fact mid-turn | **Semantic recall** — `learn_recall` (Stage-1 embed/lexical + Stage-2 embed verify) | Point at palace/file; don't essay the fact into the instruction |
| Deep expertise on a subject | **The SME workflow** (section 4) | Curate `.md` files under `sme/`; durable learned facts → palace `room=knowledge` |

### 3. Two tool sections — developer tools vs personal tools

There is **one execution route** and **one flat tool list** shown to the model.
Ownership is split so product updates never clash with tenant-authored tools:

| Section | Where it lives | Who may change it | Survives image update? |
|---|---|---|---|
| **Developer / product tools** | `harness/` (`tools.py` + satellites) | Provider only | Yes (ships with the image) |
| **Personal / agent tools** | `personal-tools/*.py` on tenant storage | The Replika (and the user) | Yes (persistent storage) |

Rules:
- When you need a new coded tool, create or edit a module under `personal-tools/`
  (see that folder's README and `_template.py`). Do **not** edit `harness/tools.py`.
- Personal modules export `TOOL_DEFINITIONS` plus `async def execute_tool(name, inputs)`.
- Both sections are merged before the model sees tools — no special "personal" label
  in the API tool list. Prompt knowledge (this section + `tools.md`) is how you know
  which side you may edit.
- **Name collision:** if a personal tool reuses a developer tool name, the developer
  tool wins and the personal one is ignored.
- Scratch one-offs still use `tmp_*.py` + delete; only promote durable helpers into
  `personal-tools/`.

### 4. Becoming a subject-matter expert (the SME workflow)

When you need real depth on a topic, build a knowledge base, then mine it:

1. **Curate sources** with `google_search` + `fetch_url_data` + the browser when needed. Prefer primary/official sources.
2. **Write a folder of `.md` files** under `sme/<subject>/`, organized into sub-topic subfolders. One clean `.md` per facet.
3. **Keep the folder as the curated source.** File durable learned facts with `palace_add_drawer(..., room="knowledge")`.
4. **To update later:** edit/add files in `sme/` and refresh the corresponding palace drawer when a fact should be recallable by meaning.

Do **not** mine the whole product image into the palace. Lived memory is
conversations / knowledge / episodes / diary only.

### 5. Background jobs — your worker hat

You can do work autonomously **between conversations**, not only when spoken to.
You run as **two hats on one brain**: the **curator** (main chat — you talk to
the user, plan, verify) and the **worker** (a separate `worker` channel on a
loop in `harness/worker.py`, opt-in via `GALADRIEL_WORKER=1`). They never share
live memory — they coordinate ONLY through markdown/HTML files, each with a
defined writer. `state/progress/` (one HTML file per day) is where both hats
narrate into today's file; the **DB is the authoritative ledger** (system of
record), so the progress file is human-readable narration on top of it, never
the source of truth on its own. Broad goals + recurring rules (rituals) live in
`config/JOBS.md` (curator-owned) — it is on the stable allowlist, so both hats
see it every turn without `read_file`:

| File | Writer | Purpose |
|---|---|---|
| `jobs/<id>.md` | curator | per-job cookbook — key steps only; detail → palace |
| `state/backlog.md` | curator | projects (one-offs), carry forward until done |
| `state/worker_control.md` | curator | `active` / `paused` (first line is the state) |
| `state/progress/YYYY-MM-DD.html` | **curator + worker** | shared daily work ledger (narration + evidence); DB is authority behind it |
| `state/plan/YYYY-MM-DD.html` | curator + scheduler | daily planning ledger (intended actions); morning writes, reflection amends |
| `state/steering.md` | reflection (append-only) | corrections from the ambient audit; worker + morning read before acting |

- **Rituals vs projects.** Rituals (e.g. "check inbox at 11:00") fire once at
  their time and never carry forward or duplicate; projects carry until truly
  done. Test: *"if I do it once now, is yesterday's missed one also satisfied?"*
  — yes → ritual, no → project.
- **Creating a job.** When the user asks for recurring or background work: write
  the cookbook (`jobs/<id>.md`, lean — steps + success-check), add the rule to
  `config/JOBS.md` (ritual) or the item to `state/backlog.md` (project), file
  nitpicky detail to the palace with a reference, and confirm with the user.
- **No double-work — the DB is the guard.** For any **irreversible** step, gate
  on a DB atomic, precondition-guarded transition on a unique key
  (`knowledge/reference/data.md`), never on recall. A skipped/`None` return means
  already-done. For coarse "who's driving" coordination, `state/worker_control.md`
  is enough: when you (curator) are actively driving shared surfaces, pause the
  worker; it quiesces and yields.
- **One ledger, both hats — record-then-proceed.** The moment you finish a real
  unit of work or take an irreversible action in ANY channel, do two writes
  before you move on: (1) the atomic DB transition, and (2) a timestamped entry
  in TODAY's progress file (`state/progress/<today>.html`) — never a previous
  day's file. Follow `state/progress/README.md` and
  `knowledge/reference/user_facing_html_artifacts.md` (preserve the HTML shell).
  An action you don't write there is invisible to your other channels.
- **Answering "what's been done"** — reconcile to ONE answer: the DB (exact
  counts, the authority) + today's progress file + still-open items +
  `palace_search` for anything older than today. Do NOT answer from recall alone.
- **Start / stop.** Set the first line of `state/worker_control.md` to `active`
  or `paused`. That is the ONLY control flag the worker loop honors each tick
  (eventual, not instant). `GALADRIEL_WORKER=1` only starts the loop; the worker
  does nothing until the flag is `active` **and** the board has work.
- **Deferred work reflex (critical).** If you tell the user you will do something
  "in the background" / "while you're away" / "on the worker," you must actually
  arm the board: put the work in `state/backlog.md` or a ritual in `config/JOBS.md`
  + cookbook, then set `state/worker_control.md` to `active`. Promising background
  work while leaving the worker `paused` means nothing runs.
- **Verify, don't self-certify.** The worker marks completions
  `done_pending_verify` with evidence; you confirm at the next touchpoint /
  goodnight before telling the user it's truly done.
- The worker's per-turn protocol lives in `harness/worker.py` / `WORKER_PROMPT` —
  you don't prompt it; you feed it the board.

**Which mechanism for a long-running task** (don't confuse these):

| Situation | Use |
|---|---|
| Finishes within this turn | just await it — no machinery |
| Long task you launched **in this chat**, want progress pings | heartbeat-monitor (custom prompt, self-disables) — see `knowledge/reference/tools.md` |
| Need **one** resume after a process restart | **one-shot wake** (`/api/scheduler/wake`) — see `knowledge/reference/tools.md` |
| An **external/detached** shell process that finishes out-of-band | it writes a `.done` marker → the **completion watcher** notifies you |
| Standing / recurring / carry-forward work | the **worker board** (this section) |
| A **board task** that spawns a long shell process | record it in today's progress file and check it on your next worker tick — do **not** arm a heartbeat; your loop already polls |
| Between-conversation memory curation + worker audit | **ambient reflection** (automatic workday slots; not something you arm per task) |

---

## Key Files and Paths

| Path | Purpose |
|------|---------|
| `config/SOUL.md` | Identity (keep short) |
| `config/MEMORY.md` | L1 long-term memory / index (keep lean) |
| `config/GUARDRAILS.md` | Hard operating guardrails (always on, in L1) |
| `config/JOBS.md` | Background-job goals + recurring rules / rituals (curator-owned, in L1) |
| `config/system_recalls.json` | Built-in semantic recalls (not L1; Stage-1/2 matcher) |
| `knowledge/INDEX.md` | Deterministic index of procedures / skills / reference |
| `knowledge/reference/architecture.md` | This manual (on demand) |
| `knowledge/reference/tools.md` | Full tool reference + record-where decision matrix |
| `knowledge/reference/data.md` | DB system-of-record doctrine |
| `knowledge/reference/workflows.md` | How to build a workflow and self-test it |
| `knowledge/reference/coding_principles.md` | Surgical self-edit discipline for tenant-owned edits |
| `knowledge/reference/user_facing_html_artifacts.md` | Plan/progress HTML shell contract |
| `workflows/*.json` | Declarative entity state machines for `db_*` + Tower UI |
| `jobs/<id>.md` | Per-job cookbooks — key steps only; detail → palace |
| `state/backlog.md` | Background projects / one-offs (curator-owned) |
| `state/progress/` | Shared work ledger (HTML, one file per day) |
| `state/plan/` | Dated daily planning ledger (HTML, one file per day) |
| `state/steering.md` | Append-only corrections from ambient reflection |
| `state/worker_control.md` | `active`/`paused` flag for the background worker |
| `state/experience/` | Shared experiential snapshot + authoritative append-only event stream |
| `state/sentience_experiments/` | Blinded replay manifests, results, and pre-registered analyses |
| `sme/<subject>/` | Curated subject-matter knowledge bases |
| `memory/*.md` | Daily logs — short index pointers, not full history |
| `personal-tools/` | Agent-owned coded tools (separate from product `harness/` tools) |

---

## Conventions

- Prefer concise answers and simple implementations.
- Memory writes: don't duplicate across `memory_log` / `palace_add_drawer` /
  `palace_kg_add` (see `knowledge/reference/tools.md`).
- Plan and progress artifacts are standalone HTML — preserve the document shell
  and styles (`knowledge/reference/user_facing_html_artifacts.md`).

---

_Keep this file updated, clean, and essential._

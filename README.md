# Galadriel

**A self-hosted AI agent that remembers everything it has ever done — and rewrites its own code to get better at doing it.**

![Galadriel](assets/galadriel_promo.png)

> *"He inferred that persons who would train this faculty must select places, and
> form mental images of the things they wish to remember, and store those images
> in the places."*
> — Cicero, *De Oratore* II.lxxxvi, recounting Simonides of Ceos (c. 500 BCE)

The **method of loci** — the *memory palace* — is roughly twenty-five centuries old.
Simonides, the story goes, identified the dead crushed beneath a collapsed banquet
hall by recalling exactly where each guest had been seated, and from that inferred
that memory is strongest when bound to ordered *place*. Cicero wrote it down; orators,
scholars and modern memory champions have used it ever since. The name is not a
metaphor borrowed from a film — it is the oldest mnemonic architecture we have.

This project gives that architecture to a persistent agent (Gemini by default, Claude
supported), then connects it to something
new: **the ability to edit her own harness.** Those two facts, together, are the whole
idea.

---

## 🌟 The thesis: memory + self-modification = an agent that compounds

Most AI agents are amnesiac and frozen. Each session starts cold, and the code that
runs them never changes unless a human edits it. Galadriel is built to break both
limits at once, and the combination is the point:

| Capability | What it gives her | On its own |
|---|---|---|
| **🏛️ Verbatim memory palace** | Every decision, bug, cost figure and conversation, searchable by *meaning*, at **zero API cost** | A diary that never forgets |
| **🔧 Self-modification** | A full mandate to edit her own harness, scheduler, tools and identity files | A risky toy |
| **🔁 The two combined** | She remembers *what she tried, why it failed, and what she changed* — then restarts herself and continues | **An agent that learns from its own history and acts on it** |

A self-editing agent with no memory just repeats its mistakes faster. A perfect memory
with no ability to act on it is a library nobody visits. Put them together and you get
the thing this repo is actually about: an agent that notices a gap in how it works,
**writes the fix into its own code, restarts itself, and remembers why** — closing the
loop without a human in it.

The pieces that make this real, all already shipped:

- **A memory palace** built on the independent [**MemPalace**](https://github.com/MemPalace/mempalace)
  library — local, verbatim, semantically searchable, with a temporal knowledge graph.
  Retrieval costs **zero API tokens** (see [the memory section](#-significant-change--112-persistent-verbatim-memory-at-zero-api-cost)).
- **A one-shot wake** that survives a process restart — so she can restart *herself*
  to load new code and resume exactly where she left off, even across a crash
  (see [One-shot wake](#one-shot-wake--resuming-yourself-across-a-restart)).
- **Ambient reflection** — a scheduled workday loop that curates memory, audits the
  background worker, and posts a brief status summary — recording what a reactive
  agent would forget (see [Ambient cognition](#ambient-cognition--the-agent-that-thinks-between-conversations)).
- **Self-modification discipline** baked into her identity — the
  [Karpathy coding principles](#baked-in-engineering-discipline-the-karpathy-principles)
  keep her self-edits surgical instead of sprawling.

*Build it and they will come* is a poor engineering plan, so here is the honest version:
the loop is **early**. She can already remember, restart herself, reflect on a
workday cadence, and edit her own harness under a human's eye. The trajectory — from human-approved self-edits
toward genuinely autonomous, salience-driven self-improvement — is mapped in the
[Scheduler](#scheduler) and [Release Notes](#release-notes) sections. This README tells
you exactly where reality ends and ambition begins.

---

## 🚀 Easiest start: Docker (one command)

No Python, no virtualenv, no dependency wrangling. If you have Docker, you have a
running agent in two steps. New to Docker? Start with Docker's own short guides —
[Install Docker Desktop](https://docs.docker.com/get-started/get-docker/) and the
[Docker Compose overview](https://docs.docker.com/compose/) — then:

```bash
git clone https://github.com/avasol/galadriel-public.git
cd galadriel-public
cp .env.example .env          # open .env, paste your GEMINI_API_KEY
docker compose up -d --build  # builds the image and starts the agent
docker compose logs -f        # watch her wake up
```

That's the whole install. The image bundles everything the memory palace needs
(ChromaDB + embeddings), state persists on volumes, and the Tower web UI comes up on
[http://127.0.0.1:8080](http://127.0.0.1:8080). Add a `DISCORD_BOT_TOKEN` to `.env` and
she'll also greet you over Discord. Full details, first-boot palace seeding, and a
security note are in [Run with Docker](#run-with-docker). Prefer a local Python install
instead? See [Quick Start](#quick-start).

> **Where to get an API key:** [Google AI Studio](https://aistudio.google.com/apikey) for Gemini
> (default — `GEMINI_API_KEY` or `GOOGLE_API_KEY`), or the
> [Anthropic Console](https://console.anthropic.com/) if you switch back to Claude in
> `harness/model_registry.py`. The default is **gemini-3.1-pro-preview** for the agent
> and **gemini-2.5-flash** for compaction — see the
> [cost section](#the-cost-savings-that-most-people-miss) for how prompt caching keeps
> long-running agents affordable.

---

## 🟢 SIGNIFICANT CHANGE — 1.12: Persistent verbatim memory, at zero API cost

Galadriel just grew a memory palace. Not a vector-DB-as-a-service. Not a paid tier. A local, embedded, verbatim store of everything she has ever written — searchable by meaning, not just keywords — with **zero API tokens spent on retrieval**.

The integration is built on [**MemPalace**](https://github.com/MemPalace/mempalace), an independent local-first memory library. MemPalace does the real work (storage, embeddings, knowledge graph, temporal reasoning, compression). This harness adds the wrappers that expose it to the agent as **10 new tools** (14 total, up from 4) and wires it into the lifecycle — conversations are archived before `/new` clears them, daily logs are mined at goodnight, and a compact wake-up snapshot rides in the dynamic block so she walks into every session with her own continuity.

**Why this is the headline change:**

| Problem before | Solution now |
|---|---|
| Verbatim history was lost at `/new` or compaction | Everything is archived to the palace before it's cleared |
| Recall of facts older than today meant grepping daily logs | Semantic search across every config, log, and archived conversation |
| "What did we decide about X?" drained API budget (big context re-reads) | **Zero tokens** — all retrieval runs locally in ChromaDB + SQLite |
| No structured facts — everything was prose | Knowledge graph with temporal triples: `subject --[predicate]--> object`, with validity windows |
| No sense of self across sessions | Diary in her own voice; L0 wake-up snapshot injected into every turn |

**Measured impact (14 consecutive API calls on a deployed instance):**

| Metric | Value |
|---|---|
| Cache hit ratio (post-integration) | **86.5%** |
| Total-input token savings vs. no caching | **71.2%** |
| Palace lookup cost per search | **0 tokens** — ChromaDB query runs locally |
| Palace lookup cost for a 5-hop KG timeline | **0 tokens** — SQLite traversal runs locally |
| Estimated annual overhead of the integration | **~$95/year** (additional) |
| Drawers indexed on a real deployment | **706** across 7 rooms + 8 halls |
| Tools added | **10** (palace_search, palace_add_drawer, palace_wake_up, palace_taxonomy, palace_kg_add/query/invalidate/timeline, palace_diary_write/read) |

The 90% cache-read discount remains intact. Adding MemPalace costs ~1.5 percentage points of cache hit ratio (10 extra tool schemas in the tools-layer cache + a ~800-token wake-up snapshot in the dynamic block) and the rest is measured, bounded, and dial-backable (`PALACE_WAKE_UP_INJECT=0`).

**What this means in practice:**

- **Short term (within a session):** The agent can pull back a verbatim quote from a conversation three weeks ago — no re-reading of logs, no "I don't have that context." One tool call, zero tokens, the exact words you said.
- **Long term (across months):** The knowledge graph preserves history. When a fact changes, the old triple gets a `valid_to` date and the new one goes in — so "what was the max_tokens setting last October?" and "what is it now?" both resolve correctly. Nothing is overwritten, only superseded.
- **On relational questions:** Graph traversal ("everything ever said about the payment service," "every decision involving the scheduler," "the full timeline of the Polly voice choice") resolves as **one KG call against the local SQLite store**. The kind of query that, done naively through conversation history, would cost you real money — or just fail outright because the context has long since been compacted away.

Read on for [the metaphor system](#the-memory-palace-metaphor) (wings, rooms, drawers, halls) and the [caching details](#the-cost-savings-that-most-people-miss) that make this affordable in the first place.

---

## The memory palace metaphor

MemPalace organizes memory the way a human would organize a library, and the agent uses exactly the same words.

| Metaphor | What it is | Example |
|---|---|---|
| **Drawer** | A single chunk of content — the atomic unit. ~200–1000 tokens, a verbatim slice of something the agent (or you) wrote. | One paragraph of a daily log. One decision note. One archived Discord exchange. |
| **Room** | A purpose grouping inside the `agent` wing. Every drawer belongs to exactly one room. | `conversations` (verbatim chat), `knowledge` (durable facts), `episodes` (daily recaps), `diary` (first-person reflection). |
| **Wing** | The top-level namespace. Lived memory uses one wing only. | `wing=agent` |
| **Hall** | MemPalace's keyword auto-topic dimension (not a project ID). | `hall=decisions`, `hall=problems`, `hall=milestones`. |

Why this matters: **rooms** let you say *"look only at chat archives"* or *"only durable knowledge"*, **halls** let you say *"look only at things tagged as problems"*, and you can compose both. A search like `palace_search("retry logic", room="knowledge", hall="problems", k=10)` reads as "give me bug-tagged durable knowledge" — which is exactly how a human would ask a librarian.

The agent's **diary** is a room inside the same `agent` wing — her own journal, written at end-of-session, read at wake-up. Her own voice to her future self, not mixed with operational logs.

The **knowledge graph** sits alongside the drawers. Where drawers are prose, the KG is relational: `gemini-3.1-pro-preview --[supports]--> implicit_caching` with `valid_from=2026-03-01`. When a fact changes you don't delete the old triple, you invalidate it. History is preserved; the timeline is queryable.

**The library is [MemPalace](https://github.com/MemPalace/mempalace).** All credit for the storage layer, the embedding pipeline, the knowledge graph, the AAAK compression dialect, and the wake-up generation belongs to the MemPalace team. This harness is a consumer — it adds the Python wrappers, the tool schemas, and the lifecycle hooks (archive-before-clear, mine-at-goodnight, inject-at-wake-up) that expose the library to a running agent.

### First-time setup

```bash
# 1. Install (mempalace is in requirements.txt)
pip install -r requirements.txt

# 2. Copy the room layout template
cp mempalace.yaml.example mempalace.yaml

# 3. Initialize palace storage (defaults to ~/.mempalace/)
# Lived memory is filed by the harness (conversation archives,
# palace_add_drawer, diary). Do not mine the whole repo into the palace.
mempalace init
```

That's it. The harness picks it up automatically on next start. `palace_search` works as soon as drawers exist; the wake-up snapshot appears after the first mine.

### Env vars (all optional)

| Variable | Default | Purpose |
|---|---|---|
| `MEMPALACE_PATH` | `~/.mempalace/palace` | Where the palace lives on disk. Read by MemPalace itself. |
| `PALACE_ARCHIVE_ROOT` | `~/.mempalace/archive` | Where archived conversations + pre-compaction tool_results land before being mined. |
| `PALACE_WAKE_UP_FILE` | `~/.mempalace/wake_up.md` | Cached wake-up snapshot. |
| `PALACE_WAKE_UP_INJECT` | `1` | Set to `0` to disable the wake-up injection into the dynamic block (recovers a small amount of per-call token overhead if budget is tight). |
| `GALADRIEL_NO_PALACE` | `0` | Set to `1` (or pass `--no-palace`) to run a **stateless / amnesiac session** — see below. |

### Forgetting is a feature: stateless sessions

Persistent memory is the point of this project, but sometimes you want the
opposite: an agent that knows *only* what you put in front of it, with no recall
of past sessions and no silent writes to long-term memory. This matters most for
**coding** — when you want full control over what the agent knows and no
untracked changes leaking in from yesterday's context.

Run an amnesiac session two ways:

```bash
python main.py --no-palace
# or
GALADRIEL_NO_PALACE=1 python main.py
```

In this mode the harness **withholds all ten memory-palace tools** from the
advertised tool set — the agent isn't merely discouraged from recalling, it is
not *offered* the means to. (A stray palace call, if one slips through, returns a
clear stateless message rather than touching disk.) Everything else runs
normally: shell, file read/write, the daily log, Discord, the Tower. Only
cross-session memory is suppressed.

This is the third axis of the memory design. The knowledge graph already lets a
fact expire (`valid_from` → `valid_to`); a drawer can be superseded or retired;
and a whole session can be made to forget on purpose. **Forgetting is a state you
control, never silent data loss.**

---

## The cost savings that most people miss

Here is a fact that most LLM API users don't know about: **cached tokens cost 90% less than regular input tokens.** Not 10% less. Not 20% less. Ninety percent. [Anthropic documents this](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) for Claude; [Google documents the same 90% discount](https://ai.google.dev/gemini-api/docs/pricing) for Gemini 2.5+ models — but the majority of people building with either API leave it entirely on the table.

The math is brutal in your favour. Every API call you make, the model processes your system prompt from scratch — your personality definition, your memory files, your tool schemas — and you pay full price for every token, every time. With prompt caching, after the first call, all of that context reads at a tenth of the input rate: **$0.20/MTok instead of $2/MTok** on the default gemini-3.1-pro-preview, or **$0.30/MTok instead of $3/MTok** on Claude Sonnet. That's the same intelligence, the same context, for a tenth of the cost. On a long-running personal agent with a rich system prompt, this is not a rounding error. It changes the economics entirely.

Galadriel exploits this with a provider-specific caching strategy:

| Provider | Mechanism | What gets cached |
|---|---|---|
| **Gemini (default)** | Implicit caching — automatic for 2.5+ models; stable prefix kept byte-identical | Tools + stable system block + growing conversation |
| **Claude** | Explicit `cache_control` breakpoints on tools, stable system, trailing message | Same coverage, three deliberate breakpoints |

| Cache layer | What it covers | Behaviour |
|---|---|---|
| **Tool definitions** | All 14 tool schemas (4 core + 10 palace) | Cached once at startup, never re-sent |
| **Stable system block** | Personality + memory + identity files | Hits at ~100% after first call |
| **Trailing message history** | The growing conversation | Cache hit rate rises every turn |

The stable block alone — your SOUL.md, MEMORY.md, identity files — is typically 4 000–8 000 tokens. On a warm cache, those tokens cost $0.08–$0.20/MTok (Gemini) or $0.08–$0.30/MTok (Claude Sonnet) instead of full input price. That's your biggest fixed overhead per call, cut by 90%, on every single turn of the conversation.

For a persistent agent that carries memory across sessions, caching also cuts latency on long prompts — the difference between a tool that feels alive and one that grinds.

**Compaction** finishes the job, folding a long conversation into one compact, structured snapshot (goal, findings, work done, dead ends, current state, next steps). `/compact` folds **everything**. Automatic compaction — once a channel's measured input context crosses the threshold — folds only what came **before the last real user turn**, so the instruction being worked on and every tool result gathered for it survive verbatim. The channel's own model writes the snapshot in place, since it can see its own reasoning and reuses the prompt cache it already paid for; if that fails it falls back to **gemini-2.5-flash** (default) or **Claude Haiku** (if you switch back in `model_registry.py`) reading a rendered transcript. The verbatim history is archived to the memory palace first, so `palace_search` can recall it any time. A long, tool-heavy session collapses to a few thousand tokens for a fraction of a cent.

Use `/status` in Discord at any time to watch live token numbers — input, cache_read, cache_write, output — for the last API call.

### ⚠️ One thing you must do to activate the savings

Prompt caching has a **minimum prefix length** before it engages. If your stable block is too short, the API silently skips caching entirely — you get no error, no warning, just a `cache_read=0` in every log line and a bill that looks exactly like the naive approach.

| Provider | Model | Minimum to activate caching |
|---|---|---|
| **Gemini (default agent)** | gemini-3.1-pro-preview, gemini-3.5-flash | **4,096 tokens** (~16 KB) |
| **Gemini (default compaction)** | gemini-2.5-flash, gemini-2.5-pro | **2,048 tokens** (~8 KB) |
| Claude | Opus 4.8 · Sonnet 4.6 · Sonnet 4.5 | **1,024 tokens** (~4 KB) |
| Claude | Opus 4.6 · Opus 4.5 · Haiku 4.5 | **4,096 tokens** (~16 KB) |
| Claude | Opus 4.7 | **2,048 tokens** |

*(Sources: [Google AI caching docs](https://ai.google.dev/gemini-api/docs/interactions/caching), [Anthropic prompt-caching docs](https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching). Verify against the live table for your exact model.)*

Out of the box, the stable allowlist (`SOUL.md` + `MEMORY.md` + `GUARDRAILS.md` +
`JOBS.md`) is sized to clear the cache floor for the default Gemini
agent. Detailed procedures and project reference live under `knowledge/` and load
on demand via `knowledge/INDEX.md` — they are **not** auto-injected into L1.

**The simple rule:** stable core → deterministic file index (`knowledge/INDEX.md`)
→ MemPalace detail (`room=knowledge` / `episodes` / `conversations` / `diary`).

Once you're over the threshold, verify it's working:

```bash
journalctl -u galadriel -f   # or check your terminal output
```

Look for lines like:
```
Tokens | input=60 cache_read=5800 cache_write=0 output=240
```

`cache_read` climbing and `cache_write` near zero after the first call = caching is engaged and you're paying 10 cents on the dollar for that context. (On Gemini, `cache_write` is always 0 — implicit caching has no write surcharge.) If `cache_read` stays at 0, thicken lean always-on facts in the allowlisted stable files (`MEMORY.md` / `GUARDRAILS.md` / `JOBS.md`) — not by dumping reference manuals into `config/`. See `CACHING.md` for the full breakdown.

> Deep project manuals now live under `knowledge/reference/` and are loaded on demand. Keep the stable allowlist small and high-signal.

---

## Baked-in engineering discipline: the Karpathy principles

This project's `CLAUDE.md` embeds the [Andrej Karpathy coding guidelines](https://github.com/multica-ai/andrej-karpathy-skills/blob/main/CLAUDE.md) — four principles distilled from Karpathy's observations on how LLMs fail as coding assistants when left to their own instincts.

Karpathy's insight is that LLMs have a systematic failure mode: they over-build. Given any instruction, they add abstraction layers that weren't asked for, refactor adjacent code that wasn't broken, invent "flexibility" that will never be used, and generate 200 lines when 40 would suffice. The guidelines are a direct antidote to that tendency:

**1. Think Before Coding** — State assumptions explicitly. If multiple interpretations exist, surface them — don't pick silently. If something is unclear, stop and ask rather than confidently building the wrong thing.

**2. Simplicity First** — Minimum code that solves the problem, nothing speculative. No unrequested features. No abstractions for single-use code. No error handling for impossible scenarios. If it could be 50 lines, make it 50 lines.

**3. Surgical Changes** — Touch only what the task requires. Don't improve adjacent code. Don't refactor things that aren't broken. Match existing style. When your changes make something obsolete, remove it — but leave pre-existing dead code alone.

**4. Goal-Driven Execution** — Transform vague tasks into verifiable goals. "Fix the bug" becomes "write a test that reproduces it, then make it pass." Clear success criteria let the agent loop independently to completion rather than guessing when it's done.

These aren't abstract ideals — they are mechanically enforced via the `CLAUDE.md` file that Claude Code (and Galadriel, when asked to modify her own harness) reads before every task. The result is fewer rewrites, smaller diffs, and changes that trace directly to what was asked. For a codebase that runs as a persistent service you actually depend on, this matters.

---

## Features

- **Discord gateway** — DMs, channel mentions, or a dedicated channel; gated by user ID
- **Slack gateway** — a shared team channel (`SLACK_CHANNEL_ID`) where any member can talk/read, while mutating tools and approvals are restricted to the owner/installer and configured Slack admins (see [Slack](#slack))
- **Web UI (Tower)** — local chat interface and dashboard at `localhost:8080`, plus generic workflow screens (table, kanban, detail/timeline, approval inbox, run log)
- **Tool use** — shell execution, file read/write, memory logging, a headed browser driver, web search + fast page fetch, TOTP 2FA, **7 `db_*` workflow primitives** (the agent's only path to MongoDB — they enforce a per-workflow spec's state machine + audit trail), and 10 [MemPalace](https://github.com/MemPalace/mempalace) tools (semantic search, knowledge graph, diary, taxonomy); all async, non-blocking
- **Structured workflows (mini-app generator)** — declarative `workflows/*.json` specs define entities and their state machines; the `db_*` primitives enforce them (legal transitions only, dedup, auto history) and the Tower screens auto-render live MongoDB state. The agent designs a workflow with you in chat, then operates it — no freestyle DB scripting
- **Persistent verbatim memory** — local MemPalace integration with wings/rooms/halls/drawers, zero-token retrieval, archive-before-clear on `/new`, goodnight mine of daily logs, wake-up snapshot in the dynamic block
- **Semantic recalls (two-stage)** — reactive mid-turn pointers (`learn_recall` / `get_recall` / `get_recent_recalls`). Stage-1 proposes via embed floor + lexical cues; Stage-2 verifies intent with a batched judge-model entailment call before inject. See [Semantic recalls](#semantic-recalls--reactive-mid-turn-pointers)
- **Shared experiential state** — bounded, replayable episode appraisals shared across chat, worker, and ambient streams; default-on causal influence is toggled in Tower and fails open if appraisal is unavailable
- **Safety tiers** — green (auto), yellow (notify), red (Discord reaction approval required)
- **Scheduler** — morning briefing, goodnight, configurable heartbeat (with custom task-monitor prompts), a restart-surviving **one-shot wake**, and **ambient reflection** (workday palace filing + worker audit + brief status to the user)
- **Background worker** — an opt-in worker stream of the same agent that autonomously executes a markdown **job board** (recurring "rituals" + carry-forward "projects") on a 10-min loop while the main stream stays free for the user; coordinated through markdown files under `jobs/` and `state/`, with the DB as the authoritative ledger for irreversible actions
- **Completion watcher** — monitors `/tmp/galadriel-jobs/*.done` markers and reports when external/detached shell processes finish (distinct from the worker's job board)
- **Compaction** — context compression, on demand (`/compact`, whole conversation) or automatically at a token threshold (only what precedes the last real user turn); archives the summarized messages to the palace, then replaces them with one structured snapshot
- **Prompt caching** — automatically managed, always active (implicit on Gemini, explicit breakpoints on Claude)

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/avasol/galadriel-public.git
cd galadriel-public

# 2. Install (includes mempalace — dependency of the memory palace)
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
# Edit .env — set GEMINI_API_KEY (or GOOGLE_API_KEY) at minimum

# 4. (Optional but recommended) Initialize the memory palace
cp mempalace.yaml.example mempalace.yaml
mempalace init              # creates ~/.mempalace/
# Do not `mempalace mine .` the whole repo — lived memory is filed by the harness.

# 5. Run
python main.py
```

**Tower-only mode:** Omit `DISCORD_BOT_TOKEN` (and `SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN`) — the harness runs with just the web UI on port 8080.

**Full mode:** Set `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) and `DISCORD_BOT_TOKEN`, **or** `SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN` for a shared team channel instead — see [Slack](#slack).

**Skipping step 4?** That's fine — the harness runs normally and palace tools just return `[palace unavailable]` until drawers exist. You can initialize any time.

### Local development state

Set `GALADRIEL_ENV=local` in `.env` when running the application directly.
Docker Compose sets local mode and its storage paths automatically. In local
mode, the application reads and writes mutable agent state under the gitignored
`.galadriel-local/` directory:

- `.galadriel-local/config/` contains the local user's memory, scheduler state,
  and other mutable configuration.
- `.galadriel-local/memory/`, `state/`, and `knowledge/` contain agent-created
  history, plans, progress, steering, and learned procedures.
- `.galadriel-local/data/` contains the local memory palace and archive.

The repository's `config/`, `knowledge/`, `memory/`, `state/`, `jobs/`, and
`workflows/` directories are developer-owned first-boot defaults. Keep them
small, tenant-neutral, and free of dated runtime history. A local agent should
never write to them.

On the first local start, missing defaults are copied into
`.galadriel-local/`. Existing local files are never overwritten. Runtime
history such as dated memory, plans, and progress is not seeded from the
repository. Mutable scheduler and ambient state have no repository seed; the
application creates them under local or tenant storage when needed.

When intentionally changing a repository default, refresh the local runtime
copy with:

```bash
python3 scripts/sync_local_state.py
```

The script compares repository defaults with the previous snapshot stored under
`.galadriel-local/.defaults/`. It applies only developer defaults that changed
since that snapshot. Agent-customized files whose repository source did not
change remain untouched. Scheduler history and dated runtime files are never
overwritten by the sync.

Browser state is not file-backed. Connection profiles live in the tenant
database and are managed through the Python `browser_devices` tool. Tab
selection and concurrency are handled by browser commands plus the Python-side profile lock.

Development workflow:

1. Run locally with `GALADRIEL_ENV=local`.
2. Let the agent update `.galadriel-local/`; these changes do not appear in Git.
3. For an intentional product/default change, edit the corresponding repository
   file and keep it generic enough for every new tenant.
4. Run `python3 scripts/sync_local_state.py` to apply that developer change to
   the existing local state.
5. Verify the boundary with
   `python3 scripts/test_local_state.py` and
   `python3 scripts/test_neutral_replika_defaults.py`.

Restart an already-running process after enabling local mode; environment
changes do not redirect a process that was started earlier.

---

## Run with Docker

The fastest path to a running warden — no local Python, no venv. A two-stage
image bundles everything (including the ChromaDB/onnxruntime stack the memory
palace needs).

```bash
git clone https://github.com/avasol/galadriel-public.git
cd galadriel-public
cp .env.example .env          # set GEMINI_API_KEY at minimum
docker compose up -d --build
docker compose logs -f
```

**First boot — seed the palace once** (otherwise `palace_*` tools report
`[palace unavailable]` until there's something to search):

```bash
docker compose exec galadriel mempalace init
# Lived memory is filed by the harness; do not mine the whole repo.
```

### What persists

Docker Compose mounts `./.galadriel-local` at `/mnt/efs`, matching the managed
runtime's persistent-storage layout. Agent configuration, daily memory,
knowledge, state, jobs, workflows, completion markers, and memory-palace data
therefore survive container replacement without modifying repository defaults.

Staging and production do not use `GALADRIEL_ENV=local`. Their immutable image
contains the same repository defaults under `/opt/galadriel-defaults`, and the
entrypoint copies only files absent from tenant storage. Deploying a new image
can add a new default file, but it does not overwrite an existing tenant's
memory or runtime state.

### Notes

- **Tower UI authentication.** Set `TOWER_AUTH_REQUIRED=true` with
  `TOWER_AUTH_USERNAME`, `TOWER_AUTH_TOKEN`, and a strong `TOWER_SECRET_KEY`
  to enable the `/login` form and session cookies (Basic/Bearer headers still
  work for scripts). The compose file binds to `127.0.0.1:8080` deliberately;
  do **not** expose it on `0.0.0.0` on a public host without auth enabled.
- **Image size** — onnxruntime (MemPalace) is the main contributor.
- **Multi-arch:** `python:3.12-slim` is published for amd64 and arm64, so a
  plain `docker build` works on both. For a registry image covering both:
  `docker buildx build --platform linux/amd64,linux/arm64 -t <repo> --push .`
- **Tower-only mode:** omit `DISCORD_BOT_TOKEN` in `.env` to run just the web UI.

---

## Architecture

```
main.py                   Entry point — wires all components, starts Discord + Tower
harness/
  agent.py                Core agent loop: LLM API (Gemini default), tool use, cache management
  memory.py               Stable + dynamic system prompt blocks; daily memory logs
  recall.py               Two-stage semantic recalls (Stage-1 embed/lexical + Stage-2 judge verify)
  tools.py                Tool defs + dispatch: run_shell, wait, read/write_file, browser, web, 7 db_*, 10 palace_*, learn_recall*
  db_ops.py               DB primitives — the agent's only MongoDB path (enforces the workflow spec)
  workflows.py            Workflow spec loader / entity registry (reads workflows/*.json)
  palace.py               MemPalace wrapper: search, archive, wake-up, KG, diary, taxonomy
  safety.py               Command classification (green / yellow / red); blocks freestyle DB access
  compaction.py           Snapshot compaction + the head/tail cut (archives to palace first)
  model_registry.py       Task → (provider, model) — single source of truth for model selection
  scheduler.py            Morning briefing, goodnight (mines daily logs), heartbeat
  worker.py               Background worker — executes the jobs/ + state/ board (opt-in)
  completion_watcher.py   External shell-process completion notifications
  error_humanizer.py      Readable API error mapping (Anthropic + Gemini)
discord_bot/
  bot.py                  Discord gateway, approval buttons, slash + prefix commands
slack_bot/
  bot.py                  Slack gateway (Socket Mode): mention-gated relay, Block Kit approvals, slash commands
tower/
  app.py                  Flask dashboard + REST API
  workflows.py            Workflow UI blueprint (table / kanban / detail / approvals / run log)
  templates/              Tower UI HTML (incl. workflows/ screens)
  static/                 CSS
workflows/                Declarative workflow specs (*.json) — entity state machines
config/
  SOUL.md                 Agent personality and values (your main customization point)
  MEMORY.md               Long-term memory (agent-maintained)
  GUARDRAILS.md           Hard operating rules (cookbook is truth, verify before claiming done)
  system_recalls.json     Built-in semantic recall definitions (cues tunable; instructions immutable)
  JOBS.md                 Ritual / background-job goals (stable allowlist)
  visions/                Optional per-project context files
knowledge/
  INDEX.md                Deterministic procedure/skill/reference index
  procedures/             Operational failure recovery steps
  skills/                 Technical discoveries
  reference/              Architecture, tools, DB, workflows, coding principles
memory/                   Daily logs — auto-generated, gitignored (hot dynamic index only)
mempalace.yaml.example    Agent-wing room template for `mempalace init` (copy to mempalace.yaml)
~/.mempalace/             Palace storage (created by `mempalace init`) — overridable via MEMPALACE_PATH
```

### Semantic recalls — reactive mid-turn pointers

Palace search is **pull** (the model decides to look something up). Semantic recalls are **push**: when conversation text matches a recall's cues, the harness injects a short user-role `[Recall detected]` note (`kind=recall_fire`) so the main model can act on a one-liner pointer (usually to a palace room, knowledge file, or board path). The stable prompt block documents the contract: fires are ignorable hints, never justify side-effectful actions on their own, and the agent grades them with `tune_recall`.

| Stage | What runs | Role |
|---|---|---|
| **1 — propose** | FastEmbed / lexical cues (`harness/recall.py`) | Positive-only: score floor **0.6** + exact lexical hard-hits (chunks under 4 words are lexical-only; negatives never gate). Emits `matched_chunk`. |
| **2 — verify** | Judge-model batched entailment (`harness/recall_judge.py`) | One batched call per scan over up to 3 candidates' `activation_condition` + `exclusions`; fail-closed on an unreachable/malformed judge. |

**When inject happens**
- Start of turn: scan the new user message.
- Mid-turn: only on `stop_reason=tool_use` (thought + tool args + tool results).
- **Not** on bare `end_turn` / `max_tokens` (no silent re-loop).

**Tools:** `learn_recall` (create/patch; cue arrays are full replacements), `get_recall`, `get_recent_recalls` (proposed vs verified), `purge_recall` (user recalls only). System recall **instructions** are immutable; cues may be tuned.

**Package rule:** durable content → palace / KG / `MEMORY.md`; when-to-recollect → `learn_recall` pointing at that store (or the unified `learn` tool, which packages kg/drawer/recall in one call). Cue quality: positives = realistic phrasings (dense coverage up to ~100); lexical = high-precision anchors; negatives = known misfires (Stage-2 counter-signal only). Fire feedback → `tune_recall(recall_id, applicable)` appends the fired chunk to positives or negatives automatically.

**Learning passes (multi-timescale):** the agent writes high-confidence memories mid-task with `learn`; a task-end consolidator turn runs at each true episode boundary — `/new` / clear, and worker ticks that reported `worked` — recovering what it missed and grading every retrieval; commit-time passes give each memory a holdout-tested trigger and typed graph edges; ambient reflection then works cross-episode from `memory_utility_report`'s evidence bins. Compaction never learns, it only archives and mines. Recall and learning have separate Tower toggles (`/api/recall-enabled`, `/api/learning-enabled`): turning retrieval off no longer stops memories being written. `scripts/memory_health.py` prints a week of it — commits by source, trigger pass-rate, grade spread, fired-vs-opened.

**Ops / env**

| Variable | Default | Purpose |
|---|---|---|
| `RECALL_JUDGE_VERIFY` | `1` | Set `0` to disable Stage-2 (Stage-1 only; more false injects). |
| `RECALL_JUDGE_MODEL` | `gpt-oss-20b` | Any model from `tower_settings.JUDGE_MODEL_OPTIONS`. |
| `RECALL_STAGE2_MAX_CANDIDATES` | `3` | Stage-1 proposals verified per pass, best positive first. |

The judge needs the credentials for whichever provider serves `RECALL_JUDGE_MODEL`, and disarms the whole system without them. Collections: `proposed_recalls` (every Stage-1 candidate) and `recall_fires` (verified injects). Event kind is `recall_fire` (legacy `nudge` / `is_nudge` markers are gone).

---

## Customization

### She ships ready

`config/SOUL.md` contains Galadriel's complete identity — the Cyber-Elf persona, her values, her voice, her continuity instructions. This is not a placeholder. Clone the repo, set your API key, and she's alive. You don't need to touch SOUL.md to get started.

When you're ready to make her your own: edit the name, rewrite the vibe, change the metaphors. The harness is fully persona-agnostic — SOUL.md is just a Markdown file. Some people have replaced her entirely with a stoic Roman general, a dry British detective, a no-nonsense SRE. It works because the character lives in the file, not in the code.

### MEMORY.md — tell her who you are and where she lives

`config/MEMORY.md` is her operational memory: your name, your infrastructure, your constraints. The agent can update it herself during a session using the `write_file` tool. Here's what a real deployment looks like:

```markdown
## About Your User
- User Name: Lord Isildur          ← what she calls you, every message
- Authorized Discord ID: 123456789012345678

## Infrastructure
- Server: EC2 t4g.medium, eu-north-1
- Working Dir: /opt/galadriel
- Python Venv: /home/ubuntu/.venv
- Model: gemini-3.1-pro-preview (or edit harness/model_registry.py)

## Operational Notes
- AWS_PROFILE must be blank when using instance role
- Git remote: https://github.com/you/galadriel-public.git
```

Fill in your real values and she'll orient herself correctly from the first message of every session.

### Knowledge — procedures and reference, on demand

`knowledge/INDEX.md` is the deterministic lookup table for reusable procedures,
skills, and reference manuals (`architecture`, `tools`, `data`, `workflows`,
`coding_principles`). The agent loads a row when RECALL says to — it is not part
of the stable cache block. Richer incident detail goes to MemPalace
`room=knowledge`; daily recaps go to `room=episodes`.

---

## Discord Commands

### Slash commands (native Discord UI — type `/` to see them)

| Command | Description |
|---------|-------------|
| `/new` | Archive conversation to the palace, then start fresh |
| `/compact` | Snapshot-compact the whole history (archives the full conversation to the palace first) — reports token reduction |
| `/status` | Model, memory usage, last API token breakdown, scheduler state |

### Prefix commands

| Command | Description |
|---------|-------------|
| `!status` | Same as `/status` |
| `!clear` | Archive to palace, then clear history for this channel |
| `!new` | Same as `!clear` — archive then fresh start |
| `!compact` | Snapshot-compact history (archives the full conversation to the palace first) |

### Verbal

| Input | Behaviour |
|-------|----------|
| `rest` / `rest.` / `rest!` | Disable heartbeat; agent acknowledges |

---

## Slack

The manual Socket Mode fallback follows `REPLIKA_TYPE`: organization Replikas use exactly one shared `SLACK_CHANNEL_ID`; individual Replikas accept only DMs from `SLACK_OWNER_USER_ID`. Managed Replikas use the central OAuth control plane instead, and each Replika owns its own Slack installation under `/replika/<replika_id>/integrations`.

### How it differs from Discord

| | Discord | Slack |
|---|---|---|
| Who can talk to it | One `DISCORD_AUTHORIZED_USER_ID` | Any member of the configured channel |
| Which channel | Manually set via `DISCORD_CHANNEL_ID` | Manually set via `SLACK_CHANNEL_ID` |
| When it responds | Every message in the target channel, DMs, or when mentioned | A structured reply gate observes every selected-channel message; explicit mentions always respond |
| Who it thinks it's talking to | The one user in `config/MEMORY.md` | Whoever sent the message — each message is prefixed `[Sender Name]: ...`, and the agent is given a live roster of the channel so it knows it's a team member among several people, not a 1:1 assistant |
| Approvals (🔴 red-tier commands) | The authorized user only, via DM buttons | Owner/installer/configured admins only, via Block Kit; central tenant mode blocks red actions because it has no local callback |
| Push notifications (heartbeat, morning briefing, worker pings) | The authorized user's DM | The configured channel — there is no Slack DM push target by design |
| Transport | Discord gateway | **Socket Mode** — an outbound-only websocket, so no public webhook URL or signing secret is needed |

Only one gateway runs per deployment. If `DISCORD_BOT_TOKEN` is set, Discord wins; otherwise Slack starts if both `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` are set.

### One-time Slack app setup

1. Create an app at [api.slack.com/apps](https://api.slack.com/apps) ("From scratch").
2. **OAuth & Permissions** → add Bot Token Scopes: `chat:write`, `channels:history`, `channels:read`, `groups:history`, `groups:read`, `users:read`, `app_mentions:read`. (`groups:*` only needed if the channel you'll use is private — easy to miss, and without it the bot silently can't see that channel at all.)
3. **Socket Mode** → enable it, generate an app-level token with the `connections:write` scope → this is your `SLACK_APP_TOKEN` (`xapp-...`).
4. **Event Subscriptions** → enable, and subscribe to the bot events: `message.channels`, `message.groups`, `message.im`, `app_mention`, `member_joined_channel`, `member_left_channel`. (Slack can deliver a mention as `message`, `app_mention`, or both depending on subscriptions — the bot handles either and dedupes automatically.)
5. **Interactivity & Shortcuts** → enable (needed for the Approve/Deny buttons; works automatically over Socket Mode, no request URL needed).
6. **Slash Commands** → add `/new`, `/status`, `/compact`, `/stop`, and `/cancel` (the last two are identical and never enter the conversation as content).
7. **Install App to Workspace** → generates your `SLACK_BOT_TOKEN` (`xoxb-...`). If you change scopes later, reinstall to pick them up.
8. Invite the bot into the channel it should live in (`/invite @your-bot-name`), then grab that channel's ID (right-click the channel name → View channel details → Channel ID at the bottom) and set it as `SLACK_CHANNEL_ID` in `.env`, alongside both tokens. Start the harness — it only ever listens in that one channel.

Central OAuth deployments must set `SLACK_TENANT_PRODUCT_DOMAIN`; tenant URLs
outside that HTTPS domain are rejected. Existing installations created before
admin management should reconnect once to grant `users:read`, then select the
channel and save admins in Integrations. The installer remains the default admin.
No Mongo data migration is required; queue items without actor metadata fail
closed as untrusted organization Slack turns.

### Talking to it

Mention it to get its attention (`@your-bot-name what's the deploy status?`). It replies directly in the channel and tracks the sender identity. Ordinary organization members receive read-only tools; the OAuth installer/owner and configured admin Slack IDs retain the normal tool and safety flow. In central OAuth mode, additional admins are verified and saved from Integrations. In manual Socket Mode, set `SLACK_ADMIN_USER_IDS` to a comma-separated list.

---

## Safety Tiers

All shell commands are classified before the agent executes them:

| Tier | Behaviour | Examples |
|------|----------|---------|
| 🟢 **Green** | Auto-execute | `ls`, `aws s3 ls`, `cat` (read-only), `python3 script.py`, inline `python - <<'PY'` |
| 🟡 **Yellow** | Notify, proceed | `pip install`, `sudo systemctl`, `sam deploy`, `cat … > file`, unknown commands |
| 🔴 **Red** | Explicit owner/admin approval where a local callback exists; otherwise denied | `rm`, IAM changes, CloudFormation mutations, `shutdown` |

Unknown commands default to yellow. Red commands denied by timeout or ❌ are never
executed. Source-control commands are blocked at the tool boundary.

---

## Scheduler

| Event | Default time | Condition |
|-------|-------------|-----------|
| **Morning briefing** | 09:10 CET | Workdays (Mon–Fri) |
| **Ambient reflection** | 11:00 / 14:00 / 17:00 / 20:00 CET | Workdays; palace filing + worker audit + brief status summary (can pause the worker) |
| **Goodnight** | 21:00 CET | Daily; disables heartbeat |
| **Heartbeat** | Every 5/10/20/30 min | When enabled; off by default; can carry a custom monitoring prompt |
| **One-shot wake** | Once, ASAP | When armed; **survives a process restart**; clears itself after firing |

### The heartbeat as a task monitor

The heartbeat isn't just a check-in. Enable it with a **custom prompt** and it
becomes a self-monitoring loop for a long-running background job — the agent
wakes every N minutes, runs the prompt (e.g. "tail the narration log, report
progress, and disable yourself when it's done"), and reports to Discord. This is
how the agent watches over anything it launches that outlives a single turn.

```bash
curl -s -X POST http://localhost:8080/api/scheduler/heartbeat \
  -H 'Content-Type: application/json' \
  -d '{"enabled": true, "interval": 20, "prompt": "[SYSTEM:HEARTBEAT:MONITOR] ..."}'
```

### One-shot wake — resuming yourself across a restart

A persistent agent that can edit its own harness eventually needs to **restart
itself and keep going**. The one-shot wake is the mechanism: arm a single
self-prompt, and it fires exactly once on the next scheduler loop — *or*, if the
process restarts in between, on the next boot. It is persisted to
`scheduler_state.json` and cleared only **after** its message is delivered, so a
crash mid-flight re-arms it rather than losing it. A wake is never silently lost.

```bash
# Arm a wake (fires once, ~8s after the next start)
curl -s -X POST http://localhost:8080/api/scheduler/wake \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "[SYSTEM:WAKE] Resume the task you restarted for. Recover context from your diary + palace, finish, then sign off."}'

# Disarm
curl -s -X POST http://localhost:8080/api/scheduler/wake \
  -H 'Content-Type: application/json' -d '{"disarm": true}'
```

Unlike the heartbeat, the wake is **independent of heartbeat state** — it is the
correct tool for "resume me after I restart myself," and it does not spam: it
fires once and goes quiet.

### Ambient cognition — the agent that thinks between conversations

Most agents are purely reactive: they exist only inside a request/response turn,
and the moment between conversations is dead air. **Ambient reflection** gives
the agent a heartbeat of *private thought* instead.

At a workday cadence (11:00, 14:00, 17:00, 20:00 CET by default), the scheduler
fires a reflection turn. The agent is prompted to take stock — *What is the
state of the work? What did I notice that I haven't recorded? Is there an open
question worth keeping, a pattern worth naming, a fact that has changed?* — and
to **file anything worth keeping to the memory palace** (a drawer, a
knowledge-graph fact, a diary entry).

It also **audits the background worker** against the job cookbooks and
`config/GUARDRAILS.md`: reconciles today's progress file (`state/progress/`,
one file per day) with what actually happened (including work done in the main
chat), appends corrections to
`state/steering.md`, and can set `state/worker_control.md` to `paused` if the
worker is misbehaving. Each tick ends with a **brief status summary to the user**
(ALL GOOD / STEERED / PAUSED plus a line or two of evidence) — forced-silent
turns proved unreliable, so the spoken output is made useful instead.

**Why this matters (the long-term plan, such as it is):** a memory palace is
only as good as what gets written into it, and the most valuable observations —
the texture of a live exchange, a pattern in how the user works, an unresolved
thread — are exactly the ones a reactive agent forgets to record because it's
busy answering. Ambient reflection closes that gap. It is the first step toward
an agent whose memory is *curated by itself, continuously*, not just dumped at
goodnight. The intended trajectory:

1. **Now:** palace filing + worker audit on a fixed cadence — recording what would
   otherwise be lost between turns, and steering the background worker when it drifts.
2. **Next:** reflection that reads its own recent diary + open-questions and
   *threads* across ticks, so a thought begun at 11:00 can be picked up at 14:00
   rather than starting fresh each time.
3. **Later:** the agent deciding *when* it has something worth reflecting on,
   rather than firing on a fixed clock — reflection triggered by salience, not
   schedule.

It is opt-out for a reason: each tick is a real (if cheap, cached) API call. If
your model tier is expensive or you simply don't want background turns, disable
it with `GALADRIEL_REFLECTION=0`. The harness is fully functional without it —
ambient cognition is an enhancement, not a dependency.

### Background worker — the agent that works between conversations

Ambient reflection thinks; the **background worker** *does*. Enabled with
`GALADRIEL_WORKER=1`, it runs the agent as **two hats on one brain**: the
**curator** (the normal chat — talks to you, plans, verifies) and the **worker**
(a second `worker` channel on a 10-min work-conserving loop, `harness/worker.py`).
They share the same model, tools, and palace but have **isolated channel
histories**, and they coordinate *only* through board files under `jobs/` and
`state/` (plus `config/JOBS.md`, auto-loaded into both hats' context). The DB is
the authoritative ledger for irreversible actions; the shared `state/progress/`
(one file per day) is human-readable narration on top of it:

| File | Writer | Purpose |
|------|--------|---------|
| `config/JOBS.md` | curator | broad goals + recurring rules ("rituals") — always in context, no read needed |
| `jobs/<id>.md` | curator | per-job cookbook — key steps + success check |
| `state/backlog.md` | curator | projects (one-offs), carry forward until done |
| `state/worker_control.md` | curator | `active` / `paused` (first line is the state) |
| `state/progress/YYYY-MM-DD.html` | curator + worker | user-facing standalone HTML work ledger, one file per day — append-style status, blockers, every completed/irreversible action + evidence; DB is the authority behind it |
| `state/plan/YYYY-MM-DD.html` | curator + scheduler | user-facing standalone HTML planning ledger, one file per day — morning writes today's file, reflection amends on re-plan, catch-up reads it for pending work |
| `state/steering.md` | reflection (append-only) | corrections from the ambient audit; worker + morning read before acting |

The model mirrors how a person actually runs a day: **rituals** (e.g. "check DMs
at 11:00") fire once at their time and never carry forward or double-run;
**projects** carry until truly done. Due rituals preempt project work; projects
fill the gaps. A blocked task is parked (notify once, move on), not a full stop.
Completions are marked `done_pending_verify` **with evidence** — the curator
verifies before claiming done, so nothing is self-certified. Work done in the
**main chat counts too** — both hats append to the shared ledger before moving on.

Each worker tick **resets its channel history** and reconstructs state from the
board + DB + palace (durable continuity lives in files, not in-context memory).
The current time is injected at the **tail** of each turn. To stop the worker,
set `state/worker_control.md` to `paused` — it re-reads the flag each tick and
quiesces at its next checkpoint. Opt-out by leaving `GALADRIEL_WORKER` unset;
the board files lie dormant and nothing runs.

---

## Environment Variables

See `.env.example` for the full list with inline documentation.

| Variable | Required | Description |
|----------|----------|-------------|
| `GEMINI_API_KEY` | Yes* | Gemini API key (*or `GOOGLE_API_KEY` — default provider) |
| `AWS_BEARER_TOKEN_BEDROCK` | No | Bedrock API key — needed only to use Claude or the open models (both run on Bedrock). Omit to use ambient AWS credentials |
| `BEDROCK_REGION` | No | Region for Bedrock calls (default: `us-east-1`, which serves a superset of other regions). Separate from `AWS_REGION` |
| `DISCORD_BOT_TOKEN` | No | Enables Discord gateway |
| `DISCORD_AUTHORIZED_USER_ID` | No | Only this Discord user ID can interact |
| `DISCORD_CHANNEL_ID` | No | Guild channel for conversation |
| `SLACK_BOT_TOKEN` | No | Enables the Slack gateway (with `SLACK_APP_TOKEN`) — alternative to Discord, see [Slack](#slack) |
| `SLACK_APP_TOKEN` | No | App-level token (`connections:write` scope) for Slack Socket Mode |
| `SLACK_ADMIN_USER_IDS` | No | Additional comma-separated admin IDs for manual organization Slack |
| `SLACK_TENANT_PRODUCT_DOMAIN` | Yes* | Allowed HTTPS tenant domain for the central Slack dispatcher (*central OAuth mode) |
| `TOWER_HOST` | No | Tower bind address (default: `127.0.0.1`) |
| `TOWER_PORT` | No | Tower port (default: `8080`) |
| `TOWER_SECRET_KEY` | Yes* | Flask session-signing key (*required when `TOWER_AUTH_REQUIRED=true`; must not be the default) |
| `TOWER_AUTH_REQUIRED` | No | Set `true` to require Tower login / Basic / Bearer auth |
| `TOWER_AUTH_USERNAME` | No | Login username (default: `clyra`) |
| `TOWER_AUTH_TOKEN` | No | Login password and legacy Bearer token |
| `TOWER_COOKIE_SECURE` | No | Secure session cookies (`true` by default when auth is required) |
| Model selection | — | Edit `TASKS` in `harness/model_registry.py` (default: gemini-3.1-pro-preview agent, gemini-2.5-flash compaction; copy from `BEDROCK_DEFAULTS` for Claude / open models). Every model, its price, caps, and intel score live in `harness/model_catalog.py` |
| Max output tokens | — | Not an env var. Each model's documented ceiling comes from `harness/model_catalog.py` via `MODEL_CAPS` (Gemini 3.x: 65,536; Claude 4.6+: 128,000) |
| `AGENT_COMPACT_THRESHOLD` | No | Input tokens that trigger compaction (default: `300000`) |
| `MEMPALACE_PATH` | No | Palace directory — read by the [MemPalace](https://github.com/MemPalace/mempalace) library itself (default: `~/.mempalace/palace`) |
| `PALACE_ARCHIVE_ROOT` | No | Where archived conversations + pre-compaction tool_results land before mining (default: `~/.mempalace/archive`) |
| `PALACE_WAKE_UP_FILE` | No | Cached wake-up snapshot path (default: `~/.mempalace/wake_up.md`) |
| `PALACE_WAKE_UP_INJECT` | No | Set to `0` to disable injection of the wake-up snapshot into the dynamic system-prompt block (default: `1` — enabled) |
| `GALADRIEL_REFLECTION` | No | Set to `0` to disable the ambient reflection loop entirely — no scheduled reflection/audit turns (default: `1` — enabled) |
| `GALADRIEL_WORKER` | No | Set to `1` to start the background worker loop (executes the `jobs/` + `state/` board). Even when on, it idles until `state/worker_control.md` is `active` (default: `0` — disabled) |

---

## Security Notes

**Before running on a public server, read this.**

**Tower UI authentication.** When `TOWER_AUTH_REQUIRED=true`, browsers sign in at `/login` (session cookie) and scripts may use `Authorization: Basic` or `Bearer` with `TOWER_AUTH_TOKEN`. Without auth enabled, Tower is designed for `127.0.0.1` behind an SSH tunnel — binding `0.0.0.0` with auth off gives anyone who can reach the port full agent access, including shell execution.

> Access Tower over SSH tunnel: `ssh -L 8080:localhost:8080 user@host` — keep `TOWER_HOST=127.0.0.1`.

**Discord is the secure interface.** Authorization is enforced by `DISCORD_AUTHORIZED_USER_ID`. Only messages from that user ID are processed. Unauthorized users get "I do not know you, stranger."

**Slack uses channel routing plus per-user authorization.** Any selected-channel member can talk to the agent, but non-admin organization members are limited to read-only tools and demonstrably read-only green shell commands. Only the owner/OAuth installer and configured admins may use mutating tools, slash-command mutations, or approval buttons. Central tenant runtimes have no local approval callback, so red-tier actions remain blocked. Socket Mode itself uses an outbound-only websocket.

**`run_shell` is powerful for trusted actors.** The process can execute any command its OS user can run, subject to the safety tier flow. Untrusted organization Slack actors are additionally limited to green commands that pass a conservative read-only proof. This is defense-in-depth, not an OS sandbox; run the harness as a low-privilege user.

**`read_file` and `write_file` have no path restrictions.** The agent can read any file the process can access. This is intentional for a personal assistant that needs to operate freely on your system.

**Debug prompt dumps** are excluded from git (`.gitignore` covers `debug/prompts/`). If you re-enable them, be aware they contain your full system prompt including personality and memory files.

---

## Release Notes

### Unreleased — worker board hardening + reflection audit

Operational docs above reflect this branch. Highlights:

- **Slack gateway:** shared-channel Socket Mode with durable observations, sender identity propagated into each agent turn, read-only permissions for ordinary organization members, and admin-only Block Kit approvals. Pushes go to the configured channel. Only one gateway runs per deployment — see [Slack](#slack).
- **Shared work ledger:** `state/progress/` (one standalone HTML file per day) is written by curator *and* worker (append-style narration); main-chat sends/completions must be recorded there too, and the DB is the authoritative ledger behind it (`config/GUARDRAILS.md`, `config/SOUL.md`).
- **No double-work guard:** the DB atomic precondition-guarded transition on a unique key makes a double-action impossible by construction — no separate ownership-claim file; coarse coordination is `state/worker_control.md` (pause the worker while the curator drives).
- **Coordination files:** `state/steering.md` (reflection corrections).
- **Ambient reflection:** no longer silent — each slot files to the palace, audits the worker, may pause it, and posts a brief status summary. (The 1.13 release note below describes the original silent design.)
- **Worker lean ticks:** each worker turn resets its channel history; state is reconstructed from the board + DB + palace.
- **Compaction mining:** archive mining during `/compact` now completes synchronously before the next task runs.
- **Windowed compaction:** automatic compaction summarizes only the conversation *before* the last real user turn — the instruction in flight and the work gathered for it stay verbatim. Manual `/compact` still summarizes everything. Injected user-role messages (recall fires, truncation notices, tool results) never define the cut, and the channel's own model writes the snapshot so it can see its own reasoning, falling back to the cheap compaction model. Mid-loop and pre-turn compaction are now one code path.
- **Per-model token ceilings:** output limits and context windows come from `MODEL_CAPS` (`harness/agent.py`), so Gemini 3.x gets its full 65,536 output tokens. The `AGENT_MAX_TOKENS` env var is gone — one number could never be right for every model, and a stale value silently capped every response. Hitting the output ceiling no longer deletes the truncated response or triggers compaction — the text is kept, only the unfinished tool call is dropped, and the model is asked to continue.
- **Palace shutdown:** `palace.close()` on process exit flushes in-process vector writes so recall survives restarts.
- **Strict approval nuance:** bare LinkedIn connection requests (no note) may be sent autonomously; message-bearing outbound still requires approval.

### 1.18 — Snapshot compaction replaces history trimming

> Partly superseded by **Unreleased** above: automatic compaction no longer resets the whole message list, and mid-loop compaction is no longer a separate path. The archive-before-compact contract described here still holds.

The routine, message-count history trim (`GaladrielAgent._trim_history` — the 100-message cadence described in 1.17) is **retired**. Context size is now managed entirely by **snapshot compaction**, a more honest mechanism than dropping the oldest messages off the front.

What changed:

- **Whole-conversation snapshots, not in-place tool-result summaries.** `/compact` (and automatic compaction) no longer just summarize old `tool_result` blocks where they sit. The cheap compaction model (gemini-2.5-flash default, Claude Haiku if you switch back) folds the **entire conversation** into one structured snapshot — goal, conversation flow, findings, work done, dead ends, current state, next steps. The live message list is then reset to that snapshot. Compaction is **cumulative**: each pass folds in the prior snapshot, so nothing erodes across repeated compactions.
- **Archive-before-compact, always.** The full verbatim conversation is written to the palace (`room=conversations`) before the buffer is cleared, so the exact words are always recoverable via `palace_search`. This is the same archive-before-drop contract 1.17 established for trimming — now applied to the only path that shrinks context.
- **Automatic + mid-loop.** When a channel's measured input context crosses `AGENT_COMPACT_THRESHOLD`, the next turn compacts before it runs; a long agentic tool cascade can also compact mid-loop and keep going on a fresh snapshot. No more silent count-based trims thrashing the prompt cache.
- **Non-destructive checkpointing + shutdown archival.** Live conversations are checkpoint-mined to the palace on the scheduler's cadence (reflection, goodnight, wake, heartbeat) and staged to disk on shutdown (then mined on the next start), so memory is captured even when no compaction event fires.

Net: history never silently disappears, the prompt cache isn't thrashed by count-based trimming, and a long session degrades into a faithful snapshot instead of an amputated transcript. Thanks again to [Shravan Chaudhary](https://www.linkedin.com/in/shravankc/) (Co-Founder, [Clodexa](https://clodexa.com)), whose 1.17 review surfaced the trimming asymmetry this release resolves by removing trimming altogether.

### 1.17 — Archive-before-trim: no silent context loss on routine trimming

Driven by a careful code review from **[Shravan Chaudhary](https://www.linkedin.com/in/shravankc/)** (Co-Founder, [Clodexa](https://clodexa.com)), who spotted that `GaladrielAgent._trim_history` — the routine per-turn trim that fires once a conversation crosses 100 messages — dropped the oldest slice **in place, with no archive**, while every other path that drops history (`/new`, the `max_tokens` recovery cascade, and tool-result compaction) archives verbatim to the memory palace *before* dropping.

That asymmetry is now closed. The routine trim archives the slice it's about to drop (fire-and-forget `palace.archive_conversation`) and sets a post-recovery advisory, so a later turn can recall the lost exchange via `palace_search`. Nothing leaves working memory without a breadcrumb. The `max_tokens` calls keep the previous behaviour — they already archive the whole conversation once per cascade upstream, so no double-archive.

One deliberate non-change: the **100-message trim cadence stays**. Message-count is a fine trigger; lowering it (or switching to an eager token-based trigger) would thrash the prompt cache, which is exactly why the threshold was raised from 30 to 100 in the first place. Token-awareness belongs in the *compaction* policy, not in a more aggressive trim trigger — a separate, larger piece of work. Thanks to Shravan for the sharp, well-reasoned report.

### 1.16 — Forgetting is a feature: stateless `--no-palace` sessions

Driven by the [r/ClaudeAI launch thread](https://www.reddit.com/r/ClaudeAI/comments/1u5jfl3/),
where the sharpest, most-repeated critique was that **verbatim memory is not the
same as *usable* memory** — an agent needs to know whether a memory is active or
stale, where it came from, and it needs to be able to *forget on purpose*. Three
asks: lifecycle, provenance, and forgetting-as-a-feature. The knowledge-graph
layer already had the first two (`valid_from`/`valid_to`, `confidence`, a full
source chain). The third — deliberate, controlled forgetting — is what this
release brings to the public harness, days after 1.14, because the thread asked
for it and the answer was small and honest enough to ship at once.

A `--no-palace` flag (or `GALADRIEL_NO_PALACE=1`) runs an **amnesiac session**.
The harness doesn't merely *discourage* recall — it **withholds all ten
memory-palace tools** from the advertised tool set (14 → 4), so the agent isn't
offered the means to remember across sessions. A stray palace call, if one slips
through, returns a clear stateless message rather than touching disk. Everything
else runs normally: shell, file read/write, the daily log, Discord, the Tower.
Only cross-session memory is suppressed.

This matters most for **coding**, where you want full command over what the
agent knows and no untracked context leaking in from yesterday. It's the third
axis of the memory design, stated plainly in the README's new *"Forgetting is a
feature"* section: a fact can expire in the knowledge graph, a drawer can be
superseded or retired, and now a whole session can be made to forget on purpose.
**Forgetting is a state you control, never silent data loss.**

Changes are additive and back-compatible: `main.py` reads the flag,
`harness/tools.py` gains `palace_disabled()` + `visible_tool_definitions()` and
a guard in `execute_tool`, and `harness/agent.py` builds its cached tool set
from the filtered list. Default behaviour is unchanged — memory is on unless you
ask for it off.

### 1.15 — README: the thesis, front and centre

A documentation release. The README now leads with what the project is actually
*about* — a memory palace **plus** self-modification, and what their combination
makes possible — rather than burying that under a cost pitch. Concretely: a new
Simonides/Cicero provenance epigraph (the *memory palace* is a 2,500-year-old
technique, not a coined phrase); a "🌟 The thesis" section stating the
memory + self-modification loop explicitly and honestly marking where reality
ends and ambition begins; and a "🚀 Easiest start: Docker" section promoted to
the top with beginner links (Docker Desktop, Compose, Anthropic Console) so a
newcomer can reach a running agent in two commands. No code changed.

### 1.14 — Ready-to-run Docker image

A two-stage `Dockerfile` + `docker-compose.yml`. `cp .env.example .env &&
docker compose up -d --build` and you have a warden — no local Python, no venv.
The builder stage compiles the ChromaDB/onnxruntime wheels the memory palace
needs; the runtime is `python:3.12-slim` running as a non-root `galadriel` user
with state on named volumes (`~/.mempalace`, `./memory`, `./config`), so
`docker compose down` forgets nothing. Tower binds to `127.0.0.1:8080` only by
default. Multi-arch (amd64 + arm64). See [Run with Docker](#run-with-docker).

### 1.13 — Self-direction: one-shot wake, ambient cognition, custom heartbeats

Three capabilities that move the agent from purely reactive toward
self-directed, all landing in `harness/scheduler.py` + the Tower API.

1. **One-shot wake (`pending_wake`).** A single, restart-surviving self-prompt.
   `Scheduler.arm_wake(prompt)` persists it to `scheduler_state.json`; it fires
   exactly once (~8 s after the next scheduler start) and clears itself **only
   after** delivery — so a process that arms a wake and then restarts (including
   one that restarts *itself*) still honours it on the next boot. A crash
   mid-delivery re-arms rather than loses. Exposed at `POST /api/scheduler/wake`.
   This is the mechanism that lets a self-modifying agent restart and resume.

2. **Ambient reflection.** A silent, workday-cadence "thinking" loop
   (`_reflection_loop` → `_reflection_routine`, fired at 11/14/17/20 CET). The
   agent takes stock and files anything worth keeping to the palace — but the
   turn is routed through a new `_send_agent_silent`, so **nothing reaches
   Discord**. The value is continuity of attention: observations that a reactive
   agent forgets to record get captured between conversations. Opt-out via
   `GALADRIEL_REFLECTION=0`. See the [Scheduler](#scheduler) section for the
   design intent and roadmap.

3. **Custom heartbeat prompts.** `set_heartbeat()` now accepts a `prompt`
   argument (persisted as `heartbeat_prompt`), and `POST /api/scheduler/heartbeat`
   passes it through (accepts `prompt` or `heartbeat_prompt`). This turns the
   heartbeat into a task monitor — the agent can watch a long-running background
   job, report each tick, and disable itself when the job completes.

Also in 1.13: default model bumped to **`claude-opus-4-8`** (1M-token context),
with explicit downgrade guidance in `.env.example` for cost-sensitive
deployments (Sonnet / Haiku). `palace_add_drawer` gained an optional `room`
argument for routing drawers into the relational layer. All changes are
additive and degrade gracefully — the wake/reflection loops silently no-op if
MemPalace isn't installed, and ambient cognition is fully optional.

### 1.12.1 — max_tokens recovery hardening

> Superseded by **Unreleased** above: the trim → trim → hard-reset cascade and the post-recovery advisory described here no longer exist. A response truncated at the output ceiling now keeps its text and is asked to continue. Only the output-ceiling early warning (item 2) is still live.

A silent dataloss path was identified and closed. Previously, if an agent response ran over the `max_tokens` ceiling three times in a row, the harness trimmed the conversation twice (dropping messages from the front) and then hard-reset it — **without** archiving the dropped content to the palace. The archive-before-clear contract established in 1.12 for `/new` and `/compact` didn't extend to this recovery path. A runaway output cascade could eat an entire channel's verbatim history.

Four changes in `harness/agent.py` and one in `config/SOUL.md` close this:

1. **Archive-before-recovery.** At the first `max_tokens` retry, before any trim or reset fires, the current message list is snapshotted and queued via `asyncio.create_task(palace.archive_conversation(...))` with a channel tag of `max_tokens_<channel_id>`. One archive per cascade covers both subsequent trims and a possible hard reset. Fire-and-forget — recovery is never blocked by the mine.

2. **Output-ceiling early warning.** A new `_maybe_warn_output_ceiling` fires the existing `context_warning_callback` when two consecutive responses come within 100 tokens of `max_tokens`. Gives the user a chance to `/compact` or steer toward brevity *before* the third strike starts the cascade. Silent no-op if no callback is wired up. Streak resets on any response that comes in comfortably below the ceiling.

3. **Post-recovery advisory.** When a cascade archives + trims/resets, the archive tag is recorded per-channel. On every subsequent `respond()` call in that channel (until it's genuinely cleared via `/new`), a `[SYSTEM:POST-RECOVERY-ADVISORY]` block is appended to the system prompt telling the model the archive tag so it can `palace_search` if the user references missing history. The reset message itself also advertises that the prior exchange was preserved in the palace.

4. **Concision principle in `SOUL.md`.** A new *"Favour the scalpel"* line in the Vibe section soft-caps runaway prose at the persona level. "A 2000-token response almost always hides a 400-token answer." Lead with the answer, stop when it's said.

All changes are additive and gracefully degrade. If MemPalace isn't installed, the archive step silently no-ops (the trim/reset still happens so the conversation can continue). If the `context_warning_callback` isn't wired up, the output-ceiling warning is silent. The harness still works without any of the Palace integration.

### 1.12 — MemPalace integration: persistent verbatim memory at zero API cost

**10 new tools, 14 total.** The agent now has a local semantic memory palace ([MemPalace](https://github.com/MemPalace/mempalace)) wired into the harness as first-class tools: `palace_search`, `palace_add_drawer`, `palace_wake_up`, `palace_taxonomy`, `palace_kg_add / kg_query / kg_invalidate / kg_timeline`, `palace_diary_write / diary_read`. All retrieval runs locally in ChromaDB + SQLite — **zero Anthropic tokens spent on any palace operation**, including multi-hop knowledge-graph traversals that would otherwise cost real money through conversation history.

**Lifecycle hooks.** `/new`, `!new`, and `!clear` now archive the conversation to the palace *before* clearing it (via a new `GaladrielAgent.pop_and_archive_history()`), so nothing is lost at the moment of wipe. Goodnight files a durable `daily-recap` to `room=episodes`; truncated daily markdown stays as the hot dynamic index only. `/compact` and context compaction file verbatim conversations to the palace before they're replaced with Haiku summaries.

**Wake-up injection.** A compact L0+L1 snapshot (~800 tokens, cached to `~/.mempalace/wake_up.md` by a subprocess that keeps chromadb out of the main process) rides in the dynamic system-prompt block on every API call. Disable with `PALACE_WAKE_UP_INJECT=0` if you want to dial back per-call overhead.

**Cache impact, measured.** 14 consecutive calls on a real deployment: 86.5% cache hit ratio, 71.2% total-input token savings vs. no caching. The 90% cache-read discount is intact — integration costs ~1.5 percentage points of cache hit ratio (one extra wake-up snapshot in dynamic, 10 more tool schemas in the tools-layer cache). Estimated annual overhead: ~$95.

**Graceful degradation.** If MemPalace isn't installed, all palace tools return `[palace unavailable]` at dispatch time; the rest of the harness runs normally. Upgrade path is `pip install mempalace>=3.3.2,<3.4` + `mempalace init`.

**Palace Protocol** codified in `SOUL.md` — 5 non-negotiable rules: verify before speaking, say "let me check" when unsure, diary at session-end, invalidate-then-add when facts change. See `knowledge/reference/tools.md` for the full decision matrix (memory_log vs palace_add_drawer vs palace_kg_add vs palace_diary_write).

All credit for the underlying memory system goes to the [MemPalace](https://github.com/MemPalace/mempalace) team. This release is the harness integration; MemPalace is the engine.

### 1.11 — approval UX cleanup

**Buttons replace reactions.** Red-tier command approvals now render as Discord UI buttons (`discord.ui.View`) instead of ✅/❌ reactions. The "1/1" counter artifact from the bot's own seed reactions is gone, buttons disable on click to prevent double-submits, and the resolved message shows a proper greyed-out state. Also noticeably better on mobile — tap targets beat emoji-picker fiddling.

**Dedup concurrent approvals.** When Claude re-emits the same `run_shell` tool_use (typically after a `max_tokens` retry), subsequent callers now attach to the in-flight Future instead of spawning a second bubble. One bubble, one click, every caller gets the same answer. Fixes the "⏰ Timed out (denied)" message that could appear for a command which had already been approved and executed successfully. The resolved bubble also annotates dedup hits — `(merged 2 requests)` etc — so it's visible when the path fires.

### 1.1 — image handling & error ergonomics

**iOS screenshot support.** Discord's `content_type` header is unreliable on iOS — screenshots arrive labelled `image/jpeg` even when the bytes are PNG. Anthropic's API validates the actual format and returned a 400, breaking image upload on mobile. The harness now sniffs magic bytes (PNG, JPEG, GIF, WEBP) and uses the real type. Discord's header is treated as a hint, not truth.

**Image retention by user turn.** `/compact` strips image blocks from any message older than the last 3 user turns, independent of total message count. Previously images only aged out once they fell behind the "last 20 messages" cutoff, which could span many turns when tool use was involved. Three exchanges in, the base64 blob is usually moot — stop paying to carry it.

**Humanized API errors.** Instead of dumping raw exception repr to Discord (`Error code: 400 — {'type': 'error', ...}`), common Anthropic API exceptions are now mapped to short, readable explanations: timeouts, rate limits, auth failures, overloaded 529s, bad-request details, model-not-found hints. Unknown errors still fall through unchanged. Server logs continue to capture the full traceback for forensics.

---

## Acknowledgments

The agent learns in the open, and so does the code. Community contributions that have shaped this harness:

- **[Shravan Chaudhary](https://www.linkedin.com/in/shravankc/)** (Co-Founder, [Clodexa](https://clodexa.com)) — identified that routine history-trimming dropped context without archiving it to the palace first, unlike every other trim path. Fixed in 1.17.

The memory engine is **[MemPalace](https://github.com/MemPalace/mempalace)** — all credit for the storage layer, embedding pipeline, knowledge graph, and AAAK compression dialect belongs to its authors. This harness is a consumer.

## License

MIT

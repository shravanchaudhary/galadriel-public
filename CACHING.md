# Prompt caching in the Galadriel harness

This document explains the caching wiring in `harness/memory.py`,
`harness/agent.py`, and (for Gemini) `harness/providers/gemini_provider.py` —
what it costs, and how to verify it's working.

## TL;DR

Cached input tokens cost **~90% less** than regular input on both providers.

**Simple memory rule:** stable core → deterministic file index
(`knowledge/INDEX.md`) → memory-palace detail.

**Anthropic (explicit caching):** three `cache_control` breakpoints on every API call:
1. Last tool definition (caches the `tools` prefix).
2. The stable system block (caches the explicit allowlist:
   `SOUL.md` + `MEMORY.md` + `GUARDRAILS.md` + `JOBS.md`).
3. The last content block of the last message (caches the growing conversation).

**Gemini (implicit caching, default):** no markers to set — caching is automatic
for Gemini 2.5+ models. The harness keeps the stable system block byte-identical
(as `system_instruction`) and moves dynamic context to the tail of `contents`, so
the prefix stays stable and cache hits accumulate as the conversation grows.
Hits surface as `cached_content_token_count` → `cache_read_input_tokens`;
`cache_write` is always 0 (no separate cache-write charge).

Expected saving on repeat-read tokens: **~90%** off the normal input price.
Token usage is logged after every API call so you can watch it work.

## Cache minimums

For caching to engage, the prefix must exceed the model's minimum cacheable length.
If you're under the floor, the API silently skips caching — no error, just
`cache_read=0` in every log line.

| Provider | Model | Minimum cacheable prefix |
|---|---|---|
| **Gemini (default)** | gemini-3.1-pro-preview, gemini-3.5-flash | **4,096 tokens** (~16 KB) |
| **Gemini (default)** | gemini-2.5-flash, gemini-2.5-pro | **2,048 tokens** (~8 KB) |
| Claude | Opus 4.x / Haiku 4.5 | 4,096 tokens (~16 KB) |
| Claude | Opus 4.7 | 2,048 tokens |
| Claude | Sonnet 4.6 | 2,048 tokens |
| Claude | Opus 4.8 / Sonnet 4.5 / 4 | 1,024 tokens (~4 KB) |

*(Gemini minimums: [Google AI caching docs](https://ai.google.dev/gemini-api/docs/interactions/caching). Claude minimums: [Anthropic prompt-caching docs](https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching).)*

The default harness uses **gemini-3.1-pro-preview** for the agent
(`harness/model_registry.py`); compaction always runs on the channel's own
live model, so it shares the agent's floor — **4,096** for the default.

The stable allowlist is intentionally small and high-signal. Detailed procedures
and reference manuals live under `knowledge/` and are loaded on demand — adding a
random `config/*.md` does **not** put it in the prompt.

If you see `cache_read=0` (and `cache_write=0` on Anthropic) in every log line,
your stable block is under the minimum. Expand an allowlisted file
(`MEMORY.md` / `GUARDRAILS.md` / `JOBS.md`), don't dump reference
docs back into L1.

## What the code does

### `harness/memory.py`

`MemoryManager.build_system_blocks()` returns **two content blocks**:

```python
[
    {
        "type": "text",
        "text": <stable content>,
        "cache_control": {"type": "ephemeral"},   # cache breakpoint (Anthropic)
    },
    {
        "type": "text",
        "text": <dynamic content>,                # timestamp + daily logs
    },
]
```

**Stable content** (cached) — explicit ordered allowlist only:
- `SOUL.md` — always first
- Active Vision (if set via Tower `/api/vision`)
- `MEMORY.md`
- `GUARDRAILS.md`
- `JOBS.md`

**Dynamic content** (not cached, but small):
- Active-project banner (project names belong in queries, never as `hall=`)
- MemPalace wake-up snapshot (if installed and seeded; ~800 tokens). Disable
  with `PALACE_WAKE_UP_INJECT=0` to recover this overhead.
- Yesterday's and today's daily logs (hot index only — full chat is in
  `room=conversations`)
- Current timestamp

On Gemini, blocks with `cache_control` become `system_instruction` (stable prefix);
blocks without it are appended to the tail of `contents` so they don't bust the cache.

### Knowledge + palace (not cached)

- `knowledge/INDEX.md` → procedures / skills / reference files, loaded via
  `read_file` when a semantic recall or known procedure needs them.
- MemPalace `agent` wing rooms: `conversations`, `knowledge`, `procedures`, `episodes`, `preferences`.

### `harness/agent.py` (Anthropic path)

- `self.tools` is computed once at init with `cache_control` on the last tool.
- Every `messages.create()` call passes the 2-block system list and the
  trailing-cache-attached message history.
- The stored history is **never mutated** — cache markers only exist on the wire.
- `_log_usage()` runs after every response.

### `harness/providers/gemini_provider.py` (Gemini path)

- Stable system blocks → `system_instruction` (fixed prefix every call).
- Dynamic system block → trailing user turn at the end of `contents`.
- Tool declarations are included in the config; implicit caching covers the
  stable prefix automatically.
- `cache_creation_input_tokens` is always 0 — Gemini has no cache-write surcharge.

## Verifying it works

Tail the service logs:

```bash
journalctl -u galadriel -f
```

**Anthropic** — a healthy warm cache looks like:

```
Tokens | input=50  cache_read=0     cache_write=5200 output=120   ← cold: writes prefix
Tokens | input=80  cache_read=5200  cache_write=180  output=340   ← warm: reads prefix
Tokens | input=50  cache_read=5380  cache_write=220  output=200   ← subsequent turns
```

**Gemini** — cache_write stays 0; watch cache_read climb instead:

```
Tokens | input=50  cache_read=0     cache_write=0 output=120    ← cold: no hits yet
Tokens | input=80  cache_read=5200  cache_write=0 output=340    ← warm: reads prefix
Tokens | input=50  cache_read=5380  cache_write=0 output=200    ← subsequent turns
```

Key signals:
1. **`cache_read` climbs on call #2 onward** — prefix is warm.
2. **`input_tokens` stays small** — only new user message + dynamic context.
3. On Anthropic only: **`cache_write` is large on the first call** — writing the prefix.

Use `/status` in Discord to see the last API call's token breakdown in real time.

## Expected cost impact

### Gemini (default) — gemini-3.1-pro-preview ($2/MTok input, $12/MTok output)

| | Without caching | With caching |
|---|---|---|
| Stable prefix (~5K tok), 20 turns/day | 20 × 5K × $2/MTok = **$0.20/day** | ~1 cold + 19 reads × 5K × $0.20/MTok = **~$0.02/day** |

Cached reads are **$0.20/MTok** (10% of $2.00 input) — a uniform 90% discount
across Gemini 2.5+ models per [Google pricing](https://ai.google.dev/gemini-api/docs/pricing).

### Claude — Opus 4.6 ($5/MTok input, $25/MTok output)

| | Without caching | With caching |
|---|---|---|
| Stable prefix (~5K tok), 20 turns/day | 20 × 5K × $5/MTok = **$0.50/day** | ~2 writes + 18 reads × 5K = **~$0.08/day** |

The stable prefix cost drops ~84–90%. Tool-heavy agentic workloads are input-heavy,
so the total bill impact is substantial on either provider.

## Model choice and the cache minimum

**Gemini 2.5 Flash** ($0.30/$2.50 per MTok) has a **2,048-token cache minimum** —
easier to clear than the 4,096-token floor on gemini-3.1-pro-preview. A good
choice if you want cheaper automated turns.

**Claude Sonnet 4.6** ($3/$15 per MTok) has a **2,048-token cache minimum** on
Anthropic — easier to clear than Opus's 4,096, with lower base token cost.

To switch models or providers, edit `TASKS` in `harness/model_registry.py`.
Copy entries from `BEDROCK_DEFAULTS` to move a task onto Claude or an open model
— no other code changes needed. Which models exist, and their cache minimums,
live in `harness/model_catalog.py`.

Claude and the open models both run on Bedrock. Claude keeps Anthropic's
explicit `cache_control` breakpoints. The open models on `bedrock-mantle` cache
*automatically* off the request prefix, like Gemini implicit caching: there is no
breakpoint to set, hits arrive as `prompt_tokens_details.cached_tokens`, and
there is no cache-write charge. The stable-prefix rule therefore matters just as
much there — see `harness/providers/bedrock_mantle_provider.py`.

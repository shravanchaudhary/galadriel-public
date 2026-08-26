# Bedrock providers: verified findings and implementation plan

All availability claims below were verified against AWS account `020571892795`
on 2026-08-20 using a Bedrock long-term API key (`AWS_BEARER_TOKEN_BEDROCK`),
not from documentation or memory.

## 1. What "bedrock-mantle" actually is

Mantle is the next-generation Bedrock inference engine. Both `bedrock-runtime`
and `bedrock-mantle` run on it; the `bedrock-mantle` endpoint name refers only
to the OpenAI/Anthropic-compatible HTTP surface.

Two endpoints matter for us:

| Endpoint | Base URL | Wire format |
| --- | --- | --- |
| `bedrock-mantle` | `https://bedrock-mantle.{region}.api.aws/v1` | OpenAI Chat Completions |
| `bedrock-runtime` | `https://bedrock-runtime.{region}.amazonaws.com/model/{modelId}/invoke` | native Anthropic Messages |

Two corrections to the AWS docs, both found empirically:

- The OpenAI-compatible route is `/v1`, **not** `/openai/v1`.
  `GET /openai/v1/models` returns 404; `GET /v1/models` returns the served list.
- Mantle model IDs differ from `bedrock ListFoundationModels` IDs. Always
  resolve IDs from `GET /v1/models`, never from `list-foundation-models`.
  Examples: `moonshot.kimi-k2-thinking` (control plane) vs
  `moonshotai.kimi-k2-thinking` (Mantle); `qwen.qwen3-next-80b-a3b` vs
  `qwen.qwen3-next-80b-a3b-instruct`.

### Region: us-east-1

`BEDROCK_REGION` defaults to `us-east-1`. Mantle serves a different model list
per region, and us-east-1 is a strict superset — nothing is available elsewhere
that is missing there:

| Region | Mantle models |
| --- | --- |
| us-east-1 | 55 |
| us-west-2 | 48 |
| ap-south-1 | 38 |

Region choice is not only about the catalog: **the same model can behave
differently per region.** Nemotron 3 Super 120B never responds in `ap-south-1`
(hangs past 300s and 600s, streaming and not) but answers in ~1s from
`us-east-1` and `us-west-2`. It was almost dropped as broken on that basis.

The 17 models only in us-east-1 are the 6 Anthropic-on-Mantle entries, GPT-5.4 /
5.5 / 5.6 (luna, sol, terra), Grok 4.3, and the Gemma 4 family. None are usable
on this account today: the GPT models return `401 access_denied` ("not available
for this account"), and Grok 4.3 and Gemma 4 return "isn't supported on this
route" — they are listed but not served on chat completions.

### Which endpoint per model

Anthropic is listed on Mantle in us-east-1 (Opus 4.7/4.8, Opus 5, Sonnet 5,
Fable 5, Haiku 4.5) but **not on the chat-completions route**: it answers
`The model 'anthropic.claude-opus-5' does not support the '/v1/chat/completions'
API`. Its Anthropic Messages route (`/anthropic/v1/messages`) does accept those
ids, but returns the same marketplace 403 as `bedrock-runtime`, so it is not a
way around the billing block. None of the Claude versions actually requested
(Opus 4.6/4.5, Sonnet 4.6/4.5, Haiku 4.5) appear on Mantle in any region.

So the split is:

- **Claude → `bedrock-runtime`**, via `anthropic.AsyncAnthropicBedrock` with
  `global.` cross-region inference profiles. These models are
  `INFERENCE_PROFILE`-only; the bare model ID is not invokable.
- **Everything else → `bedrock-mantle`** Chat Completions. All `ON_DEMAND`.

## 2. Verified model matrix

Intel score = the benchmark number supplied with the request. Scores come from
different benchmarks/harnesses and are only loosely comparable (see Open
question 1).

### Claude — `bedrock-runtime`, `AsyncAnthropicBedrock`

| Display | Bedrock model ID | Score | In $/1M | Out $/1M | Status |
| --- | --- | --- | --- | --- | --- |
| Claude Opus 4.6 | `global.anthropic.claude-opus-4-6-v1` | 62.9 | 5.00 | 25.00 | 403 marketplace billing |
| Claude Opus 4.5 | `global.anthropic.claude-opus-4-5-20251101-v1:0` | 57.8 | 5.00 | 25.00 | verified working |
| Claude Sonnet 4.6 | `global.anthropic.claude-sonnet-4-6` | 53.4 | 3.00 | 15.00 | 403 marketplace billing |
| Claude Sonnet 4.5 | `global.anthropic.claude-sonnet-4-5-20250929-v1:0` | 42.8 | 3.00 | 15.00 | verified working |
| Claude Haiku 4.5 | `global.anthropic.claude-haiku-4-5-20251001-v1:0` | 28.3 | 1.00 | 5.00 | verified working |

Also active in us-east-1 but **not in the catalog**: Opus 5, Sonnet 5, Fable 5,
Opus 4.8, Opus 4.7, Sonnet 4. The first five are blocked by account access
rather than billing, and none of them have a supplied intel score — adding them
means deciding a score and a price per model, so they were left out rather than
guessed at.

Confirmed on the working models: extended thinking returns real `thinking`
blocks **with `signature`**, `tool_use` blocks parse correctly, and usage
carries `input_tokens`, `output_tokens`,
`output_tokens_details.thinking_tokens`, `cache_read_input_tokens`,
`cache_creation_input_tokens`, and `cache_creation.ephemeral_5m/1h`. This is
exactly the response shape the harness already consumes, so the existing
`AnthropicProvider` needs only a client swap.

### The Claude 403, diagnosed

us-east-1 exposes **11 active Anthropic inference profiles**, under two
interchangeable prefixes (`aws bedrock list-inference-profiles`):

| Model | Blocker |
| --- | --- |
| Opus 5, Sonnet 5, Fable 5, Opus 4.8, Opus 4.7 | `AccessDenied` / "not available for this account" |
| Opus 4.6, Sonnet 4.6, Opus 4.5, Sonnet 4.5, Haiku 4.5, Sonnet 4 | `INVALID_PAYMENT_INSTRUMENT` |

Two different problems, and the distinction matters. The 5.x/4.7/4.8 generation
was never enabled on the account at all — that needs a model-access request. The
4.5/4.6 generation *is* enabled and only fails on billing.

Everything below was ruled out as a cause:

- **Not the region.** Identical results from us-east-1 and ap-south-1.
- **Not the profile prefix.** `us.anthropic.*` and `global.anthropic.*` behave
  identically, model for model.
- **Not the endpoint.** `bedrock-runtime` and Mantle's `/anthropic/v1/messages`
  both 403.
- **Not the bearer token.** Ordinary SigV4 IAM credentials with no
  `AWS_BEARER_TOKEN_BEDROCK` produce byte-identical failures.
- **Not Bedrock billing in general.** On the *same* endpoint, region, and
  credentials, `us.amazon.nova-lite-v1:0` and
  `us.meta.llama3-3-70b-instruct-v1:0` both succeed. Only Anthropic fails.
- **Not the model generation.** Even `anthropic.claude-3-haiku-20240307-v1:0` —
  legacy, `ON_DEMAND`, no inference profile, on Bedrock since 2024 — returns
  `INVALID_PAYMENT_INSTRUMENT`.

That isolates it to one thing: Anthropic models are billed through an **AWS
Marketplace subscription**, which requires a valid payment instrument on the
account, while first-party models (Amazon, Meta) and the Mantle open models are
billed by AWS directly. So the fix is a valid payment method on account
`020571892795`, not anything in this codebase.

The block is also intermittent: Opus 4.6 succeeded once from us-east-1 (2.5s,
correct token counts, which is what confirms the provider path itself is right)
and 403'd on the immediately following attempt. Once billing is fixed, re-verify
thinking, streaming, and cache counters.

### Mantle — `bedrock-mantle`, OpenAI Chat Completions

All verified working with tool calling and usage parsing unless noted.

| Display | Mantle model ID | Score | In $/1M | Out $/1M | Notes |
| --- | --- | --- | --- | --- | --- |
| GLM-5 | `zai.glm-5` | 52.4 | 1.00 | 3.20 | |
| Kimi K2.5 | `moonshotai.kimi-k2.5` | 43.2 | 0.60 | 3.00 | |
| MiniMax M2.5 | `minimax.minimax-m2.5` | 42.2 | 0.30 | 1.20 | emits `reasoning` |
| DeepSeek V3.2 | `deepseek.v3.2` | 39.6 | 0.62 | 1.85 | |
| Qwen3 Coder Next | `qwen.qwen3-coder-next` | 36.0 | 0.50 | 1.20 | |
| Kimi K2 Thinking | `moonshotai.kimi-k2-thinking` | 35.7 | 0.60 | 2.50 | emits `reasoning` |
| GLM-4.7 | `zai.glm-4.7` | 33.4 | 0.60 | 2.20 | |
| Devstral 2 123B | `mistral.devstral-2-123b` | 32.6 | 0.40 | 2.00 | |
| Nemotron 3 Super 120B A12B | `nvidia.nemotron-super-3-120b` | 31.0 | 0.15 | 0.65 | **us-east-1 only** |
| GLM-4.7 Flash | `zai.glm-4.7-flash` | 22.0 | 0.07 | 0.40 | |
| GPT-OSS 120B | `openai.gpt-oss-120b` | 18.7 | 0.15 | 0.60 | emits `reasoning` |
| Mistral Large 3 | `mistral.mistral-large-3-675b-instruct` | 12.0 | 0.50 | 1.50 | |
| Qwen3 Coder 30B A3B | `qwen.qwen3-coder-30b-a3b-instruct` | 10.1 | 0.15 | 0.60 | |
| Nemotron 3 Nano 30B | `nvidia.nemotron-nano-3-30b` | 8.5 | 0.06 | 0.24 | |
| Qwen3 Next 80B A3B | `qwen.qwen3-next-80b-a3b-instruct` | 7.6 | 0.14 | 1.20 | |
| Gemma 3 27B | `google.gemma-3-27b-it` | 3.8 | 0.23 | 0.38 | **no tool calling** |
| GPT-OSS 20B | `openai.gpt-oss-20b` | 3.1 | 0.07 | 0.30 | emits `reasoning` |

One exclusion:

- **Gemma 3 27B** silently ignores a `tools` array and replies with prose. It
  is usable as a judge/classifier only, never as an agent model. Must be
  flagged `supports_tools=False` so it cannot be selected as an agent model.

All 17 were verified end to end in us-east-1: every tool-capable model returned
`stop_reason=tool_use` with a correctly parsed `get_weather` call, in 0.5–7.4s.
Gemma correctly returned `end_turn` with no tool call.

Bedrock also lists `openai.gpt-oss-safeguard-20b` and
`openai.gpt-oss-safeguard-120b`. These are deliberately excluded: they are
safety-classification models, not general-purpose ones.

### Where the prices come from

Rates are the **standard** tier from the AWS Price List API for us-east-1,
matching usage types `*-mantle-{input,output}-tokens-standard`:

```
aws pricing get-products --region us-east-1 --service-code AmazonBedrock \
  --filters Type=TERM_MATCH,Field=regionCode,Value=us-east-1
```

Read the tier suffix carefully. Every model also publishes `flex` and `batch`
SKUs at ~50% of standard and a `priority` SKU at ~175%, and the descriptions do
not always say which is which — matching on the description rather than the
`usagetype` attribute silently halves every cost figure. The
`aws.amazon.com/bedrock/pricing` HTML page is also region-selected, so scraping
it yields whatever region the page defaulted to (Sydney, ~1.03x us-east-1).

Two rates were corrected this way: Qwen3 Coder 30B A3B was carrying the Sydney
rate (0.1545/0.618 → 0.15/0.60) and Qwen3 Next 80B A3B's input was slightly
high (0.15 → 0.14). GLM-5 is the only model with no published SKU, so its
1.00/3.20 is the vendor's own figure and is unverified against AWS.

### Context windows and why max_output is a budget, not a ceiling

Mantle reports each model's context window through its own error messages —
send an over-large `max_tokens` and it answers
`'max_tokens' (N) exceeds model maximum (C)`, where C is the total context.
Neither `/v1/models` nor Bedrock's control plane carries the number.

| Context | Models |
| --- | --- |
| 262,144 | Kimi K2.5, Kimi K2 Thinking, Qwen3 Coder Next, Qwen3 Coder 30B, Qwen3 Next 80B, Devstral 2 123B, Mistral Large 3, Nemotron 3 Super, Nemotron 3 Nano |
| 202,752 | GLM-5, GLM-4.7, GLM-4.7 Flash |
| 196,608 | MiniMax M2.5 |
| 163,840 | DeepSeek V3.2 |
| 131,072 | GPT-OSS 120B, GPT-OSS 20B, Gemma 3 27B |

The important part: on Mantle `max_tokens` **reserves** context rather than
merely capping output. The rule is `input + max_tokens <= context`, enforced
with a 400:

```
This model's maximum context length is 262144 tokens. However, you requested
262144 output tokens and your prompt contains 188 characters ...
```

So there is no separate output cap to discover, and "maximum supported output"
is not a constant — it is `context - input`. Every token granted to
`max_output` is a token the prompt cannot use, which is why it is set to 65,536
(matching the Gemini ceiling, leaving 50–75% of each window for the prompt)
rather than to the context size. Setting it equal to context would 400 every
call that has a prompt. Gemma is the exception at 8,192: Mantle's validator
accepts up to 131,072 for it, but Gemma 3 genuinely cannot generate beyond
8,192.

Enforcement was later measured across all 17 models by sending
`max_tokens` equal to each model's validator ceiling alongside a real prompt.
**15 of 17 reserve context; only the two GPT-OSS models treat `max_tokens` as a
pure cap** (GPT-OSS accepted `max_tokens=131072` with a prompt, and separately
accepted a 70k prompt alongside `max_tokens=65536` on its 131,072 window).

#### The auto-compaction threshold has to fit the model, not the other way round

`tower_settings.CONTEXT_OPTIONS = (300_000, 1_000_000)` is the Tower-facing
compaction trigger — auto-compact fires once measured input exceeds it (see
`GaladrielAgent.respond`'s `_last_input_tokens... > runtime["context"]`
check). Every context number in the table above is **below the smaller
300K option**, and so is the Claude 4.5 family at 200,000. Left as a flat
global default, none of these 20 models would ever hit their own compaction
trigger before hitting the model's real ceiling first — the turn 400s (or,
post-`_fit_max_tokens`, silently gets its reply shrunk) instead of folding
into a summary the way it does for the 1M-context Gemini/Claude-4.6 tier.

Fixed with `tower_settings.context_options_for_model(model)` /
`default_compact_threshold_for_model(model)`: both clamp the 300K/1M pair
down to the model's own catalog `context`, falling back to that ceiling alone
as the sole option when even 300K would exceed it. `normalize_compact_threshold`,
`resolve_model_runtime`, `set_model_runtime`, and the `/api/context` /
`/api/model` Tower routes all thread the model through so the UI dropdown,
the persisted per-model setting, and the compaction trigger itself agree —
none of them can independently drift back to the flat 300K.

#### Why `max_tokens` is not simply omitted

Omitting it looks attractive — the model would pick its own limit and the
reservation problem disappears — but the defaults are far too low. With
`max_tokens` unset, GPT-OSS 20B truncated at exactly 8,192 tokens with
`finish_reason: "length"`. Omission trades a rare 400 for silent truncation of
every long reply, which is worse.

The other two providers rule it out anyway:

| Provider | `max_tokens` omitted |
| --- | --- |
| Anthropic Messages | **Required.** No default; the request is rejected outright. |
| Gemini | Optional; falls back to the model's `output_token_limit`. |
| Mantle (OpenAI) | Optional; falls back to a low per-model default (8,192 on GPT-OSS). |

Gemini's limits are authoritative and queryable, unlike Mantle's — via
`client.models.get(model=...)`. Every current tier reports
`input_token_limit: 1048576` / `output_token_limit: 65536`, which is exactly
what the catalog already carries, and Gemini bills input and output against
separate limits, so it has no reservation problem to solve.

#### What we do instead: fit the budget per call

`_fit_max_tokens` in the Mantle provider shrinks `max_tokens` to
`context - estimated_input - 4096` whenever the static budget will not fit,
with a floor of 8,192. Input is estimated crudely from the serialized request
(bytes / 3.5, which over-estimates for English); guessing high only shortens a
reply nobody was going to read in full, whereas guessing low is a hard failure.

This keeps `max_output` meaningful as a per-turn *spend limit* while making the
model's real maximum — `context - input` — the effective bound on long prompts.
Verified on DeepSeek V3.2 (163,840 window) with a 110,010-token prompt: the raw
call with a static `max_tokens=65536` returns the context-length 400, and the
same request through the provider succeeds.

### Verified Mantle capabilities

- **Parameters accepted:** `temperature`, `top_p`, `max_tokens`, `stop`, and
  `reasoning_effort` (`low`/`high` both change output token counts, so it is
  honoured rather than ignored). See the effort ladder below.
- **Streaming:** works, including incremental `tool_calls` deltas and
  incremental `reasoning` deltas on thinking models.
- **Caching is automatic** and reported as
  `usage.prompt_tokens_details.cached_tokens`. There is no explicit cache
  breakpoint, so this behaves like Gemini implicit caching: map
  `cached_tokens → cache_read_input_tokens`, `cache_creation = 0`, and keep a
  byte-stable request prefix to make it engage.
- **Reasoning text** arrives as a non-standard `reasoning` field on the message
  (`reasoning_content` on some models); read it out of `model_extra`.

### The `reasoning_effort` ladder

Mantle accepts exactly four values. The API states the set outright when you
send it a bad one, which is the authoritative source — OpenAI's own guide lists
a seven-value superset (`minimal`, `xhigh`, `max`) that Mantle does not take:

```
reasoning_effort: Input should be 'none', 'low', 'medium' or 'high'
  [type=literal_error, input_value='minimal']
```

`none` is accepted by every Mantle model **except** the two gpt-oss sizes, which
use OpenAI's Harmony format and reject it explicitly:

```
Harmony does not support reasoning_effort='none'
```

So `low` is the floor for gpt-oss. That is what `can_disable_thinking=False` in
the catalog encodes, and the Mantle provider floors `none → low` for those two
rather than letting the request 400.

Two models accept `none` and then ignore it. Kimi K2 Thinking and MiniMax M2.5
still emit ~650–750 characters of reasoning at `none`, unchanged from `low`.
They are honest reasoning-always models, so they make poor judges: on a judge
prompt at `max_tokens=300` the reasoning consumes the entire budget and the call
returns `finish_reason=max_tokens` with empty content, which the judge then
reads as malformed. Prefer a model that honours the knob for judge duty. Do not
"fix" this by flagging them `can_disable_thinking=False` — that would send them
`low`, which is not better.

### Reasoning on non-agent calls

Judges, gates, titles, and decomposition pass `thinking=False`, which resolves
per provider to the least reasoning that provider permits:

| Provider | `thinking=False` sends | Off entirely? |
| --- | --- | --- |
| Mantle | `reasoning_effort="none"` | Yes, except gpt-oss (floored to `low`) |
| Bedrock Anthropic | no `thinking` block at all | Yes — extended thinking is opt-in |
| Gemini 3.x | `thinking_level` `minimal`, or `low` where minimal is unavailable | No — Gemini 3 has no off switch |
| Gemini 2.5 | `thinking_budget=0` (`128` on 2.5 Pro, whose floor is not 0) | Yes on Flash/Lite |
| Ollama | `think=False` | Yes |

## 2b. Judge eval — accuracy vs cost

14 cases over the real `recall_judge` path (its own system prompt, parsing,
`temperature=0`, `thinking=False`, `max_tokens=300`): plain positives and
negatives, exclusion overrides, `judge_negatives` misfires, multi-candidate
selection where one and then two of three apply, a prompt-injection attempt,
and a domain mention that is not a request. Cost is per 1,000 judge calls at
us-east-1 standard rates. Latency matters because `JUDGE_TIMEOUT_SECONDS` is 6.

| Model | Accuracy | p50 | p90 | $/1k calls |
| --- | --- | --- | --- | --- |
| GPT-OSS 20B | 100% | 0.89s | 1.14s | **$0.048** |
| Nemotron 3 Super 120B | 100% | 0.82s | 1.13s | $0.065 |
| Qwen3 Next 80B A3B | 100% | 0.74s | 0.97s | $0.068 |
| Devstral 2 123B | 100% | 0.84s | 1.12s | $0.116 |
| GPT-OSS 120B | 100% | 1.03s | 2.87s | $0.102 |
| GLM-4.7 | 100% | 0.96s | 1.29s | $0.188 |
| Mistral Large 3 | 100% | 0.78s | 1.36s | $0.198 |
| GLM-5 | 100% | 0.90s | 1.84s | $0.343 |
| Kimi K2.5 | 100% | 1.13s | 5.83s | $0.275 |
| DeepSeek V3.2 | 93% | 1.00s | 1.87s | $0.219 |
| Qwen3 Coder Next | 93% | 0.68s | 3.79s | $0.176 |
| GLM-4.7 Flash | 93% | 0.71s | 1.10s | $0.027 |
| Qwen3 Coder 30B A3B | 86% | 0.76s | 1.39s | $0.063 |
| Nemotron 3 Nano 30B | 79% | 0.62s | 0.91s | $0.025 |
| Gemma 3 27B | 71% | 0.89s | 1.64s | $0.071 |
| MiniMax M2.5 | 57% (6 errors) | 4.93s | 9.63s | $0.380 |
| Kimi K2 Thinking | 0% (14 errors) | 5.16s | 5.93s | $0.830 |

**Default: GPT-OSS 20B** (`tower_settings.DEFAULT_RECALL_JUDGE_MODEL`, the only
place the judge default is defined) — cheapest of the perfect scorers, ~7x
cheaper than GLM-5 for the same verdicts, and p90 well inside the 6s budget.
The top six were re-run three more times and held 100% with zero errors and
nothing over 6s, so the ranking is stable rather than one lucky pass. Caveat:
14 cases cannot separate models that all score 100% — this identifies a safe,
cheap default, not a quality ordering among the leaders.

Kimi K2 Thinking and MiniMax M2.5 are the two reasoning-always models: they
ignore `reasoning_effort="none"` and spend the 300-token judge budget on
reasoning, returning `finish_reason=max_tokens` with empty content, which the
judge reads as malformed. Do not use them as judges. Kimi K2.5 is fine, and is
a different model from Kimi K2 Thinking.

Selecting the default in code is not enough on an existing deployment: the
judge model is also persisted per tenant in Mongo
(`recall_judge_model`), and a stored value wins over
`DEFAULT_RECALL_JUDGE_MODEL`. Change it in Tower (or clear the document) for the
new default to take effect.

### The judge does not retry

`judge_applicability` passes `attempts=1`, so a failed judge call is reported
rather than retried. The judge runs under a 6-second deadline; the default
three-attempt budget spent ~3.5s of it sleeping between 429s and then timed out
anyway, turning a fast degradation into a slow one. A missing verdict already
degrades safely (candidates are rejected), so failing immediately is strictly
better. `create_message` takes `attempts` on every provider for this purpose;
everything not on a hard deadline leaves it unset.

Judges stay on the `standard` service tier. `flex` would be cheaper, but its
latency and availability tradeoff is the wrong one to make inside a 6s budget.

### The judge no longer explains itself

The JSON schema used to be `{"applicable":[...],"reasons":{"id":"why"}}`. The
`reasons` text isn't reasoning tokens (`thinking=False` already zeros those out
where the model allows it) — it's ordinary output tokens the judge spent
writing a sentence justifying each verdict, surfaced only as `judge_reason` on
the semantic-recalls debug page. Nothing in the actual recall pipeline reads
it (`harness/recall.py` only used it to decorate a log line). Schema is now
`{"applicable":[...]}` — membership in the list is the verdict, no
explanation asked for.

Cleaned up end to end, not just left as an unused field: `validate_judgment`
no longer parses or returns a `reasons` key at all (a model that emits one
unprompted has it silently dropped — `applicable` is the only key that
matters), `MAX_REASON_CHARS` is gone, and `harness/recall.py` sets a fixed
`"judge:applicable"` tag instead of formatting a per-id lookup that can never
find anything. `tower/templates/config/semantic_recalls.html` hides the
`reason:` line on the test page specifically when it's `judge:applicable` or
`judge:none` — both now just restate the FIRES/REJECTED badge — but still
shows it for every other tag (`stage2_junk`, `stage2_disabled`,
`judge_unavailable:*`, `stage2_candidate_cap:N`), which say something the
badge doesn't.

Ablation on 20 held-out cases (10 pos / 10 neg from `eval.dataset`), same
model/temperature/thinking, schema as the only variable:

| Model | Accuracy (with reasons → without) | Mean output tokens | Mean latency |
|---|---|---|---|
| GPT-OSS 20B (default) | 0.95 → 1.00 | 72 → 48.5 (−33%) | 0.93s → 0.74s (−20%) |
| GLM-4.7 Flash | 0.85 → 0.85 | 30.1 → 8.1 (−73%) | 0.79s → 0.64s (−19%) |
| DeepSeek V3.2 | 0.95 → 0.95 | 28.0 → 9.5 (−66%) | 2.01s → 0.80s (−60%) |

No accuracy loss on any model (the default's went up, within this sample's
noise floor); individual verdict flips existed in both directions and netted
to zero or a gain. Token and latency savings hold across all three, so the
schema change is a straight win: same eval script at `/tmp/judge_reason_ablation.py`
if this needs re-running against a different model set.

### The bug this eval uncovered

The first eval run scored much worse (Gemma 0% with 14 errors, eight models
stuck at 86%). The cause was in `_split_system`, in both the Mantle and Gemini
providers, and it affected every judge on both — including the previous default,
`gemini-2.5-flash-lite`.

Those splitters treat a system block carrying `cache_control` as the cacheable
prefix and any unmarked block as per-call ambient text, to be appended as a
trailing user turn. The agent always marks its stable block, so the convention
holds there. But judges, gates, and titles pass a *single unmarked* block — so
their entire instruction set was classified as ambient, the request went out
with **no system message at all**, and the classifier rules arrived as a second
user turn prefixed `[Ambient session context — current time, active project,
and recent memory. Not part of the user's message.]`, which is both false and
actively misleading for a judge prompt. On Gemma it also produced two
consecutive `user` turns, whose chat template rejects them outright:
`Conversation roles must alternate user/assistant/...` — the real reason Gemma
looked broken as a judge.

The fix: when no block carries `cache_control` there is no cache prefix to
protect, so everything is stable. The agent's marked-stable + unmarked-ambient
split is unchanged. Gemma went 0% → 71% and the 86% cluster went to 100%.

### Non-streaming calls were discarding reasoning

Streaming and non-streaming responses carry the *same* reasoning payload; only
the delivery differs. Verified on GPT-OSS 120B with one prompt: the streamed
deltas and the single-shot response both expose it under `reasoning`
(1,592 vs 1,619 chars of the same trace).

The agent nonetheless recovered nothing on the non-streaming path, because it
scanned `response.content` for a block with `type == "thought"` — a shape no
provider has ever produced. It was dead code, so every non-streamed call lost
its reasoning silently. That path is used whenever nothing is watching the
stream, which in practice means **worker ticks** (the case that matters: the
worker reasons about which job to take, and that trace fed both the run
recorder's `reply_thought` and mid-turn recall scanning), plus the appraisers,
compaction, titles, and the deprecated Discord and completion-watcher paths.

The fix keeps reasoning out of `content` — `content` is what
`_serialize_content` writes into conversation history, and it is what the UI
diffs and renders as the model's reply, not a scratch pad for replaying
private reasoning. So the Anthropic-shaped providers (Mantle, Gemini, Ollama)
expose it separately as `_Message.thought`, populated on both paths, and
`agent._response_thought` reads that; it is stored on the history message as
`assistant_msg["_thought"]`. Native Anthropic responses are the one
exception: their `thinking` blocks stay inline in `content`, where they must
remain for tool-use signature continuity, so the helper falls back to reading
them there.

Verified per provider: Mantle (`reasoning` field plus inline `<think>` spans),
Gemini (`part.thought` parts, previously dropped outright), Ollama
(`message.thinking`), and native Anthropic (`thinking` blocks). A non-streamed
tool-use turn on GPT-OSS 120B now yields the thought with history containing
only the `tool_use` block.

### Raw reasoning is replayed verbatim, on every request, forever

Storing `_thought` outside `content` is not the same as never sending it back.
Anthropic and Gemini solve continuity with an opaque, signed token
(`signature` / `thought_signature`) that gets round-tripped so the model does
not have to re-derive its reasoning — that mechanism exists specifically
*because* those vendors will not hand back raw reasoning. Mantle's open-weight
models (GPT-OSS, DeepSeek, Kimi, Qwen3, GLM, MiniMax) and local Ollama
reasoning models have no such token: their own docs (DeepSeek, Ollama's
`tool-calling.mdx`) construct the *next* request by putting the raw
`reasoning`/`thinking` text straight back on the assistant message. Dropping
it, as the code did until now, does not error on Bedrock's proxy, but it is a
real quality gap: the model loses its own justification for what it just did
and has to reconstruct it.

Fixed in `bedrock_mantle_provider._messages_to_openai` and
`ollama_provider._messages_to_ollama`: every assistant turn's `_thought` is
replayed on `reasoning` (Mantle) / `thinking` (Ollama), **unconditionally —
no scoping, no expiry**. History is append-only on purpose. A first version
scoped replay to the still-open tool-call cascade to save input tokens, and
that was wrong: the moment a turn closed, earlier assistant messages
retroactively lost their `reasoning` field, the serialized prefix changed,
and every provider-side cached token from that turn onward would re-bill
cold on the next request. Prefix stability is worth strictly more than the
reasoning bytes it carries: once a message is serialized one way, it must be
serialized that way for the rest of the conversation.

## 2a. Findings from wiring it up

Three things only surfaced once real turns ran through the harness, all fixed:

- **Thinking models leak `<think>` prose into the visible reply.** Reasoning
  normally arrives on the out-of-band `reasoning` field, but Kimi fell back to
  inline `<think>…</think>` tags inside `content` when the previous assistant
  turn carried `content: ""` alongside `tool_calls` — it read the turn as
  unfinished and resumed it. Fixed at both ends: tool-only assistant turns now
  send `content: null` (the shape OpenAI specifies), and
  `_InlineThinkingFilter` strips any inline span that still appears, streaming
  it as a thought. The filter is stateful because a tag can straddle two
  deltas, and it must not eat a literal `5 < 6`.
- **`reasoning_effort: "minimal"` is rejected** by Mantle with a 400. Tower's
  `off`/`minimal` keys therefore both map to `low`.
- **The consequence appraiser was non-deterministic.** It parses strict JSON but
  sent no temperature, so roughly one call in four came back unparseable and was
  silently dropped. Now pinned to `temperature=0.0`.

## 3. Implementation plan

### 3.1 New provider: `harness/providers/bedrock_mantle_provider.py`

An OpenAI-Chat-Completions provider that speaks the harness's Anthropic-shaped
contract, structured like `gemini_provider.py`:

- Reuse the `_TextBlock` / `_ToolUseBlock` / `_Usage` / `_Message` adapter
  pattern so `.content`, `.usage`, `.stop_reason` match what `agent.py` reads.
- Translate in: Anthropic `system` blocks → a `system` message; `tool_use` →
  `assistant.tool_calls`; `tool_result` → `role: "tool"` messages; Anthropic
  tool defs → OpenAI `function` defs. Images go as `image_url` data URLs for
  the vision-capable models.
- Translate out: `tool_calls` → `tool_use` blocks (parse `arguments` JSON),
  `reasoning`/`reasoning_content` → thought stream, `finish_reason`
  `tool_calls`/`length`/`stop` → `tool_use`/`max_tokens`/`end_turn`.
- Usage: `prompt_tokens - cached_tokens → input_tokens`,
  `completion_tokens → output_tokens`, `cached_tokens →
  cache_read_input_tokens`, `cache_creation_input_tokens = 0`.
- `stream_message` uses `stream=True` with
  `stream_options={"include_usage": True}` and accumulates tool-call argument
  fragments by index.
- Client: `AsyncOpenAI(base_url=f"https://bedrock-mantle.{region}.api.aws/v1",
  api_key=<bedrock key>, max_retries=0)`.

### 3.2 Rework `harness/providers/anthropic_provider.py` → Bedrock

Swap `AsyncAnthropic` for `AsyncAnthropicBedrock(aws_region=...)` and add the
`thinking` / `temperature` handling the current provider throws away (it does
`del thinking, effort` today). Also add a real `stream_message` — right now
Claude falls back to the non-streaming default in `base.py`.

Mutual exclusivity to respect: Anthropic rejects `temperature` together with
extended thinking, so send one or the other, never both.

### 3.3 Model catalog with intel scores

The current setup spreads model facts across `model_registry.TASKS`,
`tower_settings.AGENT_MODEL_OPTIONS` (a bare tuple of strings),
`pricing.RATES`, `agent.MODEL_CAPS`, and `thinking_effort._KINDS`. Adding ~20
models to that shape means editing five files per model.

Introduce one table, `harness/model_catalog.py`, keyed by the display name, with
per-model: provider, wire model ID, intel score, prices, context/output caps,
and capability flags (`supports_tools`, `supports_thinking`,
`supports_temperature`). Then:

- `pricing.RATES` and `agent.MODEL_CAPS` derive from the catalog rather than
  duplicating it.
- `provider_for_model()` looks up the catalog instead of prefix-matching
  `startswith("claude")` — which is required anyway, since `zai.glm-5` and
  `qwen.*` would otherwise fall through to Ollama.
- `AGENT_MODEL_OPTIONS` becomes a catalog-derived list, filtered to
  `supports_tools=True`.

### 3.4 Gemini name and score updates

Update `AGENT_MODEL_OPTIONS` / `pricing.RATES` to add `gemini-3-pro` and attach
scores to the existing entries: 3.7 Flash 85.8, 3.6 Flash 78.0, 3.5 Flash 76.2,
3.1 Pro 73.8, 3 Pro 73.9, 3 Flash 58.0, 3.5 Flash-Lite 54.0, 3.1 Flash-Lite
31.0, 2.5 Pro 30.0, 2.5 Flash 16.9. `gemini-2.5-flash-lite` has no comparable
run — it needs a null score that the UI renders as "—" rather than 0.

### 3.5 Retry cap and error surfacing

Today `llm_retry.DEFAULT_ATTEMPTS = 8` with delays up to 60s, so a sustained 429
burns minutes and then surfaces as a raw `str(e)`.

- Drop `DEFAULT_ATTEMPTS` to 3 for every provider and every error class.
- Add a provider-agnostic error formatter that turns an exception into
  `{provider, model, http_status, code, message, attempts}` and reuse
  `harness/error_humanizer.py`, which currently only serves the Discord and
  Slack bots.
- Emit that structured payload on the existing SSE `{"type": "error"}` channel
  in `tower/app.py`, and render it in `chat_live.js` as a visible failure card
  instead of the current `[Error] <raw str>` line.
- Close the two gaps found while tracing this: `attachStream()` HTTP failures
  are swallowed (no `[Error]` reaches the log), and
  `conversation_queue._consume` closes the hub without emitting an error event.

### 3.6 UI: scores next to names

`chat_live.js:fillSelect()` already accepts `{value, label}`, so the chat
composer needs only a richer `/api/model` payload — return
`{value, label: "Claude Opus 4.6 · 62.9", score, provider}`. The judge picker in
`config/semantic_recalls.html:loadJudgeModel()` hardcodes
`opt.textContent = name` and needs the same label treatment.

### 3.7 Config

`.env` gains `AWS_BEARER_TOKEN_BEDROCK` and `BEDROCK_REGION=ap-south-1`. For
deployed tenants the key belongs in Secrets Manager and in
`RUNTIME_SECRET_NAMES` on the provisioner, matching how `GEMINI_API_KEY` is
handled today.

## 4. Decisions taken

1. **Scores shown raw**, one decimal, exactly as supplied — no cross-benchmark
   normalisation. The caveat stands: Gemini's numbers come from a different
   harness, so Gemini 3.7 Flash (85.8) sorting above Claude Opus 4.6 (62.9) is
   an artifact, not a capability claim.
2. **Direct Anthropic was dropped**, not kept as a fallback. `ANTHROPIC_API_KEY`
   is no longer read anywhere; Claude is reachable only through Bedrock.
3. **The judge picker offers the full catalog**, including no-tool models. The
   agent picker is filtered to `supports_tools`.

## 5. Verified end to end

Against the live account, through the harness rather than raw SDK calls:

- A single-call tool sweep across **all 17 Mantle models** in us-east-1: every
  tool-capable one returned a parsed `get_weather` call; Gemma correctly did not.
- Tool cascade (call → result → follow-up) on GLM-5, Kimi K2 Thinking,
  MiniMax M2.5, Nemotron 3 Super 120B, and both GPT-OSS sizes; `cache_read`
  counters populate on the second hop.
- Streaming with both text and reasoning deltas, and incremental tool-call
  argument fragments.
- The deterministic path (`thinking=False`, `temperature=0.0`) on every model
  above plus Gemma 3 27B.
- A real `GaladrielAgent.respond` turn on GLM-5 and GPT-OSS 120B, emitting
  `text` and `thought` events with usage logged.
- A real `recall_judge.judge_applicability` call on GLM-5 and GPT-OSS 120B,
  which selected the applicable recall and rejected the distractor.
- Claude stayed 403 throughout, across both prefixes, both regions, both
  endpoints, and both auth methods — see the diagnosis above. The provider is
  wired and its error surfaces correctly; it needs a valid payment instrument on
  the AWS account, then a re-test of thinking, caching, and streaming.

Test-suite state: 34 of 40 `scripts/test_*.py` pass, and the 6 failures are
identical on a clean baseline (Mongo/PYTHONPATH/env issues, unrelated).

## 5b. Follow-up fixes landed after the integration

- **Side tasks follow the live main model.** `chat_title`, the `compaction`
  transcript fallback, `slack_reply_gate`, and `learn_packaging` used to be
  pinned to `gemini-2.5-flash` in `model_registry.TASKS`, so a Gemini billing
  cap 429'd chat titles while the chat itself ran fine on GLM-5. Those tasks
  are now in `model_registry.FOLLOW_ACTIVE_MODEL`: the agent registers its
  main-channel model on init and on every switch, and follower tasks resolve
  to that model and its provider at call time. The `TASKS` pins remain the
  fallback before an agent has registered. The `consequence_appraiser` already
  followed the acting provider and was untouched.
- **Mid-turn recall no longer scans recall bookkeeping turns.** The scan
  excluded `tune_recall`/`get_recall`/`learn` args and results but still
  scanned the assistant narration around them, which restates the matched
  chunk in prose and re-fired the very recall being tuned (a self-sustaining
  cascade, worst with lexical cues at pos=1.0). When every tool call in a turn
  is in `RECALL_SCAN_EXCLUDED_TOOLS`, the whole turn is now skipped —
  `_build_tool_use_recall_scan_segments` in `harness/agent.py`. Mixed turns
  (real tool + `tune_recall`) still scan the thought and real tool payloads.
- **Failed turns render and persist in the UI.** SSE error frames now carry a
  structured `detail` (provider, model, HTTP status, unwrapped message) built
  by `llm_retry.describe_error`; the frontend shows a red human-readable
  bubble, logs the detail to the console, and skips post-turn re-hydration so
  the (never-persisted) error bubble isn't wiped. Observers attached to a
  failed queued turn get the same error event via the conversation-queue hub.
- **The judge emits verdict-only JSON** (`{"applicable": [...]}`, no `reasons`)
  and never retries (`attempts=1`) — a 429 fails the scan immediately and the
  modality degrades safely instead of sleeping through its 6s deadline.

## 6. Known gaps

- **The Mantle cached-token discount is assumed at 90% off input**, and the one
  available data point suggests that is too generous. Of the 41 Mantle-priced
  models in us-east-1, exactly one publishes a cache SKU: `xai.grok-4.3`, at
  `cache-read / input = 0.16`, i.e. an **84% discount, not 90%**. None of our 17
  publish one at all, so there is nothing model-specific to verify against short
  of an invoice. Cache *hits* are real and counted (the `cached_tokens`
  reporting is confirmed); only the rate applied to them is assumed. If AWS
  prices Mantle cache reads uniformly at 0.16x input, our cache costs are
  understated by ~60%. Left at 0.10x rather than adopting another vendor's
  single ratio — confirm on an invoice, then set it for real.
- **Claude thinking, streaming, and cache counters remain unverified** end to
  end, because of the marketplace 403.
- **Region is load-bearing.** Moving off `us-east-1` can silently lose models
  (ap-south-1 has 17 fewer) or turn a working model into one that hangs. Re-run
  the sweep in section 5 after any region change.
- **Gemma 3 27B breaks on multi-turn tool history** ("Conversation roles must
  alternate"). Unreachable in practice: it cannot be selected as an agent model,
  and judge calls are single-turn.
- **The input estimate behind `_fit_max_tokens` is a byte heuristic**, not a
  tokenizer. It runs ~15–40% high on repetitive text, so long prompts can be
  clamped to the 8,192 floor while real room remained. That only shortens a
  reply, never fails a call, and Mantle exposes no token-count endpoint to do
  better — but a per-model tokenizer would tighten it if long-output turns on
  large contexts ever start mattering.

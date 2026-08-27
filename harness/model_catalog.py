"""Single source of truth for every selectable model.

Before this table, one model's facts were spread across five files:
`model_registry.TASKS`, `tower_settings.AGENT_MODEL_OPTIONS`, `pricing.RATES`,
`agent.MODEL_CAPS`, and `thinking_effort._KINDS`. Adding a model meant five
edits and any miss failed silently — an absent `RATES` row costs $0 forever, an
absent `MODEL_CAPS` row silently caps output at 8k. Those modules now derive
from here.

Keys are the *selection* name: what Tower persists, what `pricing` costs, what
the agent passes around. `wire_id` is what the provider actually sends, which
for Bedrock is a very different string (`global.anthropic.claude-opus-4-6-v1`).
Gemini keys are deliberately identical to their wire ids so stored Tower
settings keep resolving without a migration.

`score` is the agent-benchmark number shown next to the name in the UI. Scores
come from different benchmarks and harnesses (Gemini from TB 2.1/Terminus-2,
the rest from TB2) so they rank loosely rather than exactly; `None` means no
comparable published run, which the UI renders as "—" rather than 0.

Mantle prices were verified against the AWS Price List API for us-east-1
(`aws pricing get-products --service-code AmazonBedrock`), matching the
`*-mantle-{input,output}-tokens-standard` usage types. Take the **standard**
tier specifically: every model also publishes `flex` and `batch` SKUs at ~50%
and a `priority` SKU at ~175%, and picking the wrong one silently halves or
doubles every cost figure. GLM-5 is the one model with no published SKU, so its
rate is the vendor's own and is unverified against AWS.

Availability here was verified against Bedrock on 2026-08-20, not read off a
docs page — see `knowledge/reference/bedrock_providers.md` for the transcript
and for the two models that were deliberately excluded.

`supports_vision` is the exception that IS read off a page, deliberately: the
Bedrock model card's "Input Modalities" table is what the endpoint enforces,
and it disagrees with the vendors. Devstral 2 123B is advertised by Mistral as
accepting images and Bedrock serves it text-only; GLM-5 likewise. Scraped from
the per-model cards under docs.aws.amazon.com/bedrock/.../model-card-*.html on
2026-08-26. Only three Mantle models take images: Kimi K2.5, Mistral Large 3,
Gemma 3 27B. Every Claude and Gemini entry does.
"""

from dataclasses import dataclass

GEMINI = "gemini"
BEDROCK_ANTHROPIC = "bedrock_anthropic"
BEDROCK_MANTLE = "bedrock_mantle"
OLLAMA = "ollama"


@dataclass(frozen=True)
class Model:
    key: str
    label: str
    provider: str
    wire_id: str
    score: float | None
    # $ per million tokens.
    input: float
    output: float
    cache_read: float
    cache_write: float
    context: int
    max_output: int
    cache_minimum: int
    supports_tools: bool = True
    supports_thinking: bool = True
    supports_temperature: bool = True
    # Image blocks in the request. False is the safe default: a model that
    # cannot see does not degrade, it 400s ("Model does not support image
    # modality") and keeps 400ing until the image ages out of history.
    supports_vision: bool = False
    # False when reasoning cannot be switched off at all, only turned down to
    # the model's floor. Judges and gates ask for no reasoning; this decides
    # whether they get it or the cheapest legal setting instead.
    can_disable_thinking: bool = True

    @property
    def display(self) -> str:
        """Name plus intel score, as shown in the Tower pickers."""
        if self.score is None:
            return f"{self.label} · —"
        return f"{self.label} · {self.score:g}"


# Gemini caps are uniform across current tiers: 1,048,576 in / 65,536 out.
_G_CTX, _G_OUT = 1_048_576, 65_536

# Mantle context windows, read off the API itself: sending an over-large
# max_tokens returns "'max_tokens' (N) exceeds model maximum (C)", where C is
# the total context. Neither /v1/models nor Bedrock's control plane reports it.
#
# On most Mantle models max_tokens RESERVES context rather than merely capping
# output — the rule is `input + max_tokens <= context`, and exceeding it is a
# 400 ("This model's maximum context length is 262144 tokens. However, you
# requested ... output tokens and your prompt contains ..."). 15 of the 17
# models below enforce it; both gpt-oss models treat max_tokens as a pure cap.
#
# So max_output is a per-turn budget, not a hard ceiling: the real maximum
# output is `context - input`, which moves with the conversation. The provider
# fits the budget to the remaining window per call (`_fit_max_tokens` in
# bedrock_mantle_provider), so a value here that a long prompt cannot afford is
# shrunk rather than rejected.
#
# 65_536 matches the Gemini ceiling — high enough never to bind a normal turn,
# and a deliberate spend limit on models whose windows would otherwise permit a
# 200K-token reply. Gemma is the exception: Gemma 3 cannot emit beyond 8_192,
# regardless of what Mantle's validator accepts.
_M_OUT = 65_536

# Sizes seen in us-east-1. Grouped because they cluster on a few values.
_CTX_256K = 262_144
_CTX_198K = 202_752
_CTX_192K = 196_608
_CTX_160K = 163_840
_CTX_128K = 131_072

# Bedrock's Mantle caching is automatic — no explicit breakpoint, and no
# separate cache-write charge was observed, so cache_write is 0 and cache_read
# assumes the usual 90% discount off input.
#
# Still unverified, and probably generous: of the 41 Mantle-priced models in
# us-east-1 only xai.grok-4.3 publishes a cache SKU, and its rate is 0.16x
# input (84% off), not 0.10x. None of ours publish one. Kept at 0.10x rather
# than importing another vendor's single ratio — confirm on an invoice.
def _mantle_cache_read(input_rate: float) -> float:
    return round(input_rate * 0.1, 6)


def _mantle(
    key, label, wire_id, score, input_rate, output_rate, context, *,
    max_output=_M_OUT,
    supports_tools=True, supports_thinking=True, can_disable_thinking=True,
    supports_vision=False,
):
    return Model(
        key=key,
        label=label,
        provider=BEDROCK_MANTLE,
        wire_id=wire_id,
        score=score,
        input=input_rate,
        output=output_rate,
        cache_read=_mantle_cache_read(input_rate),
        cache_write=0.0,
        context=context,
        max_output=max_output,
        cache_minimum=1024,
        supports_tools=supports_tools,
        supports_thinking=supports_thinking,
        can_disable_thinking=can_disable_thinking,
        supports_vision=supports_vision,
    )


def _claude(key, label, wire_id, score, input_rate, output_rate, context, max_output,
            cache_minimum):
    return Model(
        key=key,
        label=label,
        provider=BEDROCK_ANTHROPIC,
        wire_id=wire_id,
        score=score,
        input=input_rate,
        output=output_rate,
        # Anthropic prompt caching: reads 10% of input, 5m writes 125%.
        cache_read=round(input_rate * 0.1, 6),
        cache_write=round(input_rate * 1.25, 6),
        context=context,
        max_output=max_output,
        cache_minimum=cache_minimum,
        supports_vision=True,
    )


def _gemini(key, label, score, input_rate, output_rate, cache_minimum, *,
            max_output=_G_OUT):
    return Model(
        key=key,
        label=label,
        provider=GEMINI,
        wire_id=key,
        score=score,
        input=input_rate,
        output=output_rate,
        # Gemini implicit caching: ~90% discount, no write surcharge.
        cache_read=round(input_rate * 0.1, 6),
        cache_write=0.0,
        context=_G_CTX,
        max_output=max_output,
        cache_minimum=cache_minimum,
        supports_vision=True,
    )


MODELS: tuple[Model, ...] = (
    # ─── Gemini ──────────────────────────────────────────────────────
    # gemini-3.7-flash is introductory pricing through 2026-12-31; the standard
    # rate ($1.50/$7.50) applies from 2027-01-01 — revisit this row then.
    _gemini("gemini-3.7-flash", "Gemini 3.7 Flash", 85.8, 0.75, 3.75, 4096),
    _gemini("gemini-3.6-flash", "Gemini 3.6 Flash", 78.0, 1.50, 7.50, 4096),
    _gemini("gemini-3.5-flash", "Gemini 3.5 Flash", 76.2, 0.30, 2.50, 4096),
    _gemini("gemini-3.1-pro-preview", "Gemini 3.1 Pro", 73.8, 2.00, 12.00, 4096),
    _gemini("gemini-3-pro-preview", "Gemini 3 Pro", 73.9, 2.00, 12.00, 4096),
    _gemini("gemini-3-flash-preview", "Gemini 3 Flash", 58.0, 0.50, 3.00, 4096),
    _gemini("gemini-3.5-flash-lite", "Gemini 3.5 Flash-Lite", 54.0, 0.30, 2.50, 4096),
    _gemini("gemini-3.1-flash-lite", "Gemini 3.1 Flash-Lite", 31.0, 0.25, 1.50, 4096),
    _gemini("gemini-2.5-pro", "Gemini 2.5 Pro", 30.0, 1.25, 10.00, 2048),
    _gemini("gemini-2.5-flash", "Gemini 2.5 Flash", 16.9, 0.30, 2.50, 2048),
    # No clean comparable TB2 run for flash-lite — score stays unknown.
    _gemini("gemini-2.5-flash-lite", "Gemini 2.5 Flash-Lite", None, 0.10, 0.40, 2048),

    # ─── Claude, via bedrock-runtime + global inference profiles ─────
    # These model ids are INFERENCE_PROFILE-only; the bare id is not invokable.
    _claude("claude-opus-4-6", "Claude Opus 4.6",
            "global.anthropic.claude-opus-4-6-v1", 62.9, 5.00, 25.00,
            1_000_000, 128_000, 2048),
    _claude("claude-opus-4-5", "Claude Opus 4.5",
            "global.anthropic.claude-opus-4-5-20251101-v1:0", 57.8, 5.00, 25.00,
            200_000, 64_000, 2048),
    _claude("claude-sonnet-4-6", "Claude Sonnet 4.6",
            "global.anthropic.claude-sonnet-4-6", 53.4, 3.00, 15.00,
            1_000_000, 128_000, 2048),
    _claude("claude-sonnet-4-5", "Claude Sonnet 4.5",
            "global.anthropic.claude-sonnet-4-5-20250929-v1:0", 42.8, 3.00, 15.00,
            200_000, 64_000, 2048),
    _claude("claude-haiku-4-5", "Claude Haiku 4.5",
            "global.anthropic.claude-haiku-4-5-20251001-v1:0", 28.3, 1.00, 5.00,
            200_000, 64_000, 4096),

    # ─── Open models, via bedrock-mantle chat completions ────────────
    _mantle("glm-5", "GLM-5", "zai.glm-5", 52.4, 1.00, 3.20, _CTX_198K),
    _mantle("kimi-k2.5", "Kimi K2.5", "moonshotai.kimi-k2.5", 43.2, 0.60, 3.00,
            _CTX_256K, supports_vision=True),
    _mantle("minimax-m2.5", "MiniMax M2.5", "minimax.minimax-m2.5", 42.2, 0.30, 1.20,
            _CTX_192K),
    _mantle("deepseek-v3.2", "DeepSeek V3.2", "deepseek.v3.2", 39.6, 0.62, 1.85,
            _CTX_160K),
    _mantle("qwen3-coder-next", "Qwen3 Coder Next", "qwen.qwen3-coder-next",
            36.0, 0.50, 1.20, _CTX_256K),
    _mantle("kimi-k2-thinking", "Kimi K2 Thinking", "moonshotai.kimi-k2-thinking",
            35.7, 0.60, 2.50, _CTX_256K),
    _mantle("glm-4.7", "GLM-4.7", "zai.glm-4.7", 33.4, 0.60, 2.20, _CTX_198K),
    _mantle("devstral-2-123b", "Devstral 2 123B", "mistral.devstral-2-123b",
            32.6, 0.40, 2.00, _CTX_256K),
    # Region-sensitive: this one hangs forever in ap-south-1 and answers in ~1s
    # from us-east-1. It is the reason BEDROCK_REGION defaults to us-east-1.
    _mantle("nemotron-super-3-120b", "Nemotron 3 Super 120B A12B",
            "nvidia.nemotron-super-3-120b", 31.0, 0.15, 0.65, _CTX_256K),
    _mantle("glm-4.7-flash", "GLM-4.7 Flash", "zai.glm-4.7-flash", 22.0, 0.07, 0.40,
            _CTX_198K),
    # Both gpt-oss sizes speak OpenAI's Harmony format, which 400s on
    # reasoning_effort="none" ("Harmony does not support reasoning_effort='none'"),
    # so "low" is their floor — they are the only Mantle models that cannot
    # switch reasoning off outright.
    _mantle("gpt-oss-120b", "GPT-OSS 120B", "openai.gpt-oss-120b", 18.7, 0.15, 0.60,
            _CTX_128K, can_disable_thinking=False),
    _mantle("mistral-large-3", "Mistral Large 3",
            "mistral.mistral-large-3-675b-instruct", 12.0, 0.50, 1.50, _CTX_256K,
            supports_vision=True),
    _mantle("qwen3-coder-30b-a3b", "Qwen3 Coder 30B A3B",
            "qwen.qwen3-coder-30b-a3b-instruct", 10.1, 0.15, 0.60, _CTX_256K),
    _mantle("nemotron-nano-3-30b", "Nemotron 3 Nano 30B A3B",
            "nvidia.nemotron-nano-3-30b", 8.5, 0.06, 0.24, _CTX_256K),
    _mantle("qwen3-next-80b-a3b", "Qwen3 Next 80B A3B",
            "qwen.qwen3-next-80b-a3b-instruct", 7.6, 0.14, 1.20, _CTX_256K),
    # Gemma silently ignores a `tools` array and answers in prose instead of
    # calling anything, so it can only ever be a judge/classifier. Its generation
    # ceiling is a real 8_192, unlike the rest.
    _mantle("gemma-3-27b", "Gemma 3 27B", "google.gemma-3-27b-it", 3.8, 0.23, 0.38,
            _CTX_128K, max_output=8_192,
            supports_tools=False, supports_thinking=False, supports_vision=True),
    # The plain gpt-oss weights, NOT the `-safeguard` variants Bedrock also
    # lists — those are safety classifiers, not general-purpose models.
    _mantle("gpt-oss-20b", "GPT-OSS 20B", "openai.gpt-oss-20b", 3.1, 0.07, 0.30,
            _CTX_128K, can_disable_thinking=False),
)

BY_KEY: dict[str, Model] = {m.key: m for m in MODELS}


def get(model: str) -> Model | None:
    """Catalog entry for a selection name, or None for unlisted (Ollama tags)."""
    return BY_KEY.get((model or "").strip())


def wire_id(model: str) -> str:
    """Provider-facing model id. Falls back to the name for Ollama tags."""
    entry = get(model)
    return entry.wire_id if entry else model


def supports_vision(model: str) -> bool:
    """True when `model` accepts image blocks in the request.

    Unlisted names (local Ollama tags) answer False: an unknown model that
    cannot see fails the whole turn, while one that can only loses the pixels.
    """
    entry = get(model)
    return bool(entry and entry.supports_vision)


def provider_for(model: str) -> str:
    """Provider id owning `model`; unlisted names are local Ollama tags."""
    entry = get(model)
    return entry.provider if entry else OLLAMA


def agent_options() -> tuple[str, ...]:
    """Models selectable as an agent/loop model — tool calling is mandatory
    there, so a no-tool model would appear to work and then silently never
    invoke anything."""
    return tuple(m.key for m in MODELS if m.supports_tools)


def judge_options() -> tuple[str, ...]:
    """Models selectable as the recall judge. The judge emits a JSON verdict and
    never calls a tool, so no-tool models stay eligible — they are the cheapest
    entries in the table."""
    return tuple(m.key for m in MODELS)


def labels(models) -> list[dict]:
    """`{value, label, score, provider}` rows for the Tower pickers."""
    rows = []
    for key in models:
        entry = get(key)
        if entry is None:
            rows.append({"value": key, "label": key, "score": None,
                         "provider": OLLAMA, "vision": False})
        else:
            rows.append({"value": key, "label": entry.display,
                         "score": entry.score, "provider": entry.provider,
                         "vision": entry.supports_vision})
    return rows

# Recall-verification model benchmark

Benchmark suite for choosing:

1. **Stage-2 verifier** — a small local generative model (GGUF, CPU) that answers:
   *"Would injecting this recall's instruction into the agent's context lead to a
   better response — is the rule genuinely applicable to this text?"*
2. **Stage-1 rerank pass** — embedding / reranker alternatives to the current
   FastEmbed `BAAI/bge-small-en-v1.5` cosine approach.

Everything here is **additive and read-only** over production code: it imports
from `harness/` and `local_llm/` but never modifies them. Weights download to
`eval/models/` (not `local_llm/models/`), results go to `eval/results/`.

## Setup

```bash
venv/bin/pip install -r eval/requirements-eval.txt
```

Notes:
- `llama-cpp-python` + `numpy` are already in the repo venv.
- `semantic-router` / `fastembed` / `pymongo` are only needed for the Stage-1
  `baseline` scorer (it imports `harness/recall.py`).
- `torch` + `transformers` are only needed for the reranker's transformers CPU
  fallback — skip them if the GGUF reranker backend works for you.

## Dataset

`eval/dataset.py` builds ~198 labeled cases `{chunk, recall_id, recall_dict,
expected, source}` (95 fire / 103 no-fire, all 16 system recalls):

- `heldout` (34): the labeled cases from `scripts/test_slm_recall_verification.py`
  (extracted via `ast`, not imported).
- `incident` (7): real production false-positive injects — e.g. the
  `[Tower]: …20$ token usage…` message firing `sys_plan`/`sys_jobs`, bare
  `read_file` firing `sys_architecture`, `active` firing
  `sys_finish_work`/`sys_status`, `worker_control.md` tool-args JSON firing
  `sys_deferred_work`/`sys_jobs`. All labeled `expected=False`.
- `cue_audit` (157): each system recall's own `positive_examples` (True) and
  `negative_examples` (False) from `config/system_recalls.json`, plus curated
  cross-recall confusion pairs (e.g. *"please remember that I prefer dark
  mode"* must NOT fire `sys_fact_lookup`).

Inspect: `venv/bin/python -m eval.dataset`

Leakage guard: for `cue_audit` cases the prompt builders exclude the exact eval
chunk from the recall's few-shot examples.

## Candidates and download sizes

`venv/bin/python -m eval.candidates` prints the registry + presence. All
repos/filenames verified on HuggingFace (Aug 2026).

### Stage-2 generative (GGUF Q4_K_M, CPU)

| key | model | repo / file | size |
|---|---|---|---|
| `gemma3-270m` | Gemma 3 270M IT QAT | `bartowski/google_gemma-3-270m-it-qat-GGUF` / `google_gemma-3-270m-it-qat-Q4_K_M.gguf` | 241 MB |
| `gemma3-1b` | Gemma 3 1B IT QAT | `bartowski/google_gemma-3-1b-it-qat-GGUF` / `google_gemma-3-1b-it-qat-Q4_K_M.gguf` | 769 MB |
| `qwen3-0.6b` | Qwen3 0.6B | `bartowski/Qwen_Qwen3-0.6B-GGUF` / `Qwen_Qwen3-0.6B-Q4_K_M.gguf` | 462 MB |
| `qwen3-1.7b` | Qwen3 1.7B | `bartowski/Qwen_Qwen3-1.7B-GGUF` / `Qwen_Qwen3-1.7B-Q4_K_M.gguf` | 1.2 GB |
| `qwen2.5-0.5b` | Qwen2.5 0.5B Instruct | `bartowski/Qwen2.5-0.5B-Instruct-GGUF` / `Qwen2.5-0.5B-Instruct-Q4_K_M.gguf` | 379 MB |
| `qwen2.5-1.5b` | Qwen2.5 1.5B Instruct | `bartowski/Qwen2.5-1.5B-Instruct-GGUF` / `Qwen2.5-1.5B-Instruct-Q4_K_M.gguf` | 940 MB |
| `qwen2.5-3b` | Qwen2.5 3B Instruct | `bartowski/Qwen2.5-3B-Instruct-GGUF` / `Qwen2.5-3B-Instruct-Q4_K_M.gguf` | 1.8 GB |
| `llama3.2-1b` | Llama 3.2 1B Instruct | `bartowski/Llama-3.2-1B-Instruct-GGUF` / `Llama-3.2-1B-Instruct-Q4_K_M.gguf` | 770 MB |
| `smollm2-360m` | SmolLM2 360M Instruct | `bartowski/SmolLM2-360M-Instruct-GGUF` / `SmolLM2-360M-Instruct-Q4_K_M.gguf` | 258 MB |
| `smollm2-1.7b` | SmolLM2 1.7B Instruct | `bartowski/SmolLM2-1.7B-Instruct-GGUF` / `SmolLM2-1.7B-Instruct-Q4_K_M.gguf` | 1.0 GB |

All ten: ~7.8 GB. Qwen3 models get a `/no_think` suffix (they default to
thinking mode); their chat calls get extra `max_tokens` headroom.

### Stage-1 axis

| key | model | repo / file | size |
|---|---|---|---|
| baseline | FastEmbed `BAAI/bge-small-en-v1.5` (current prod) | downloaded by fastembed itself | ~130 MB |
| `qwen3-embedding-0.6b` | Qwen3 Embedding 0.6B | `Qwen/Qwen3-Embedding-0.6B-GGUF` / `Qwen3-Embedding-0.6B-Q8_0.gguf` (official) | 610 MB |
| `qwen3-reranker-0.6b` | Qwen3 Reranker 0.6B | `Voodisss/Qwen3-Reranker-0.6B-GGUF-llama_cpp` / `Qwen3-Reranker-0.6B-Q4_K_M.gguf` | 397 MB |

Reranker caveats:
- The official Qwen org publishes **no reranker GGUF**. Most community
  conversions are broken (missing `cls.output.weight` → ~0 scores; llama.cpp
  issue #16407). The registered Voodisss quant is converted with the official
  `convert_hf_to_gguf.py` and keeps the classifier head + RANK pooling metadata.
- `llama-cpp-python` has no rerank API; the GGUF backend drives the low-level
  bindings (RANK pooling + `llama_get_embeddings_seq`) and runs a sanity check
  at load. If anything fails, `--reranker-backend auto` falls back to
  transformers CPU on `Qwen/Qwen3-Reranker-0.6B` — **~2.4 GB RSS at float32**,
  fine on a benchmark box, too heavy for a 4 GB tenant.
- Qwen3 Embedding GGUF quirks are handled in code: `pooling_type=last` and a
  manually appended `<|endoftext|>` (official model-card requirement).

## Run

```bash
# Stage-2: all 10 generative models, both prompt strategies (~7.8 GB download)
venv/bin/python -m eval.run_stage2_eval

# subset / options
venv/bin/python -m eval.run_stage2_eval --models gemma3-270m,qwen3-0.6b,smollm2-360m
venv/bin/python -m eval.run_stage2_eval --skip-download --strategies logit --timeout 60
venv/bin/python -m eval.run_stage2_eval --max-cases 40      # quick smoke run

# Stage-1: baseline vs qwen3 embedding vs reranker
venv/bin/python -m eval.run_stage1_rerank_eval
venv/bin/python -m eval.run_stage1_rerank_eval --scorers baseline,qwen3-embed
venv/bin/python -m eval.run_stage1_rerank_eval --reranker-backend transformers
```

Both CLIs default to **CPU-only** (matching the 4 GB Fargate tenants); Stage-2
has a `--gpu` escape hatch for local iteration only — don't use its latency
numbers for capacity decisions.

## Output & interpretation

Each run writes `eval/results/stage2_<ts>.json|.md` or `stage1_<ts>.json|.md`.
The JSON has per-case rows (chunk, expected, predicted, score, latency, status);
the markdown is the summary table.

- **Positive class = recall fires.** For Stage-2 the number that matters most is
  **precision** (false fires annoy the agent) with recall ≥ ~0.85, and
  especially **`incident FPs blocked`** — true negatives on the 7 real
  production false-positive cases. The current embed Stage-2 already blocks the
  junk-y ones via heuristics; a generative verifier must beat it without
  losing true fires.
- **Strategies:** `chat` = parsed YES/NO from a strict few-shot chat completion;
  `logit` = YES-vs-NO next-token logit margin (> 0 → fire) on the same few-shot
  prompt, plus a swept `best_f1_threshold` so you can pick a margin other
  than 0. If `logit` beats `chat` on the same model, the model "knows" the
  answer but can't follow the output format — prior repo experience (hola/bola
  latching) says tiny ITs often fail exactly there.
- **fail-open** counts timeouts + unparseable outputs + errors; all predicted
  YES like production. A model with good F1 but high fail-open is not actually
  good.
- **Latency**: mean and p95 per verification, single-threaded CPU (llama.cpp
  threads = cores − 1). Production budget: Stage-2 runs mid-turn, so p95
  ≥ ~1.5 s per candidate is painful; remember several candidates can fire in
  one turn.
- **peak RSS** is process-wide and monotonic (`ru_maxrss`), so within one run
  each model inherits its predecessors' peak; `rss_after_load_mb` /
  `rss_after_run_mb` (psutil) are per-model. For clean peaks, benchmark one
  model per invocation (`--models <key>`). Target: model + overhead must fit
  comfortably in 4 GB alongside the app (~1 GB) — i.e. the 3B Q4 (~2.3 GB
  resident) is already borderline.
- Stage-1 table shows both the **production rule** operating point (0.6 positive
  floor + relative negative veto; P(yes) ≥ 0.5 for the reranker) and the
  **best-F1 swept threshold** — cosine scales differ per embedding model, so
  the sweep is the fair comparison; the default rule shows what a drop-in
  replacement would do with no retuning.

## Timeout / fail-open semantics (Stage-2)

Each model call runs in a worker thread with `--timeout` (default 120 s).
In-process llama.cpp calls can't be safely cancelled, so on the first timeout
the model is **aborted** (marked in results, remaining cases skipped) rather
than risking concurrent access to a wedged context.

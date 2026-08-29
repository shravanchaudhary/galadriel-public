# Recall-verification eval suite

Benchmark suite for two things:

1. **Stage-2 end-to-end** (`run_e2e_eval.py`) — drives Stage-1 propose + the real
   judge verifier (`harness/recall_judge.py`) on the labeled dataset below.
   Requires `GEMINI_API_KEY` (or credentials for whichever provider serves
   `RECALL_JUDGE_MODEL`).
2. **Stage-1 chunking / gating** (`run_stage1_chunking_eval.py`) — whether the
   chunker hands the embedder text it can judge, scanning whole raw mid-turn
   documents rather than pre-split sentences. This is the only suite that
   exercises `scan_text_for_recalls` end-to-end on realistic tool output, and
   it never calls the judge (Stage-1 only).

Everything here is **additive and read-only** over production code: it imports
from `harness/` but never modifies it. Results go to `eval/results/`.

## Setup

```bash
venv/bin/pip install -r eval/requirements-eval.txt
```

Notes:
- `numpy` + `psutil` are already in the repo venv.
- `semantic-router` / `fastembed` / `pymongo` are only needed for the suites
  that import `harness/recall.py`.

## Dataset

`eval/dataset.py` builds ~198 labeled cases `{chunk, recall_id, recall_dict,
expected, source}` (95 fire / 103 no-fire, all 16 system recalls):

- `heldout` (34): the labeled cases in `eval/heldout_cases.py` (pure data, no
  harness import).
- `incident` (7): real production false-positive injects — e.g. the
  `…20$ token usage…` message firing `sys_plan`/`sys_jobs`, bare
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

### Raw-document dataset (chunking eval)

`eval/chunk_dataset.py` is a separate corpus of **77 whole documents** shaped like
what `_build_tool_use_recall_scan_text` actually sends (thought + tool request +
raw result, newline-joined), each labeled with the recall_ids that *should* be
proposed. Noise bodies are seeded, so the corpus is byte-stable.

- `tool_noise` (10): `ls -l`, pretty and **minified** JSON, git status, traceback,
  HTML, app log, single-line prose. Nothing should fire.
- `chatter` (3): ordinary multi-line conversation. Nothing should fire.
- `signal_in_noise` (48): one real cue buried in tool output at head / middle /
  tail. Guards against a "fix" that just suppresses everything.
- `clean_signal` (16): the bare cue — the only shape the other suites test.

Inspect: `venv/bin/python -m eval.chunk_dataset`

## Run

```bash
# Stage-2 end-to-end (needs GEMINI_API_KEY / RECALL_JUDGE_MODEL credentials)
venv/bin/python -m eval.run_e2e_eval --leave-one-out
venv/bin/python -m eval.run_e2e_eval --metamorphic-only
venv/bin/python -m eval.run_e2e_eval --max-cases 40      # quick smoke run

# Stage-1 chunking / gating (no judge call; fastembed only, ~5 s)
venv/bin/python -m eval.run_stage1_chunking_eval
venv/bin/python -m eval.run_stage1_chunking_eval --margins 0,0.03,0.05,0.10   # separation gate
venv/bin/python -m eval.run_stage1_chunking_eval --gate both                  # regex gate contribution
venv/bin/python -m eval.run_stage1_chunking_eval --windows 64,128,256
```

## Output & interpretation

Each run writes `eval/results/e2e_<ts>.json|.md` or `stage1_chunking_<ts>.json|.md`.
The JSON has per-case rows (chunk, expected, predicted, latency, status); the
markdown is the summary table.

- **Positive class = recall fires.** The number that matters most is
  **precision** (false fires annoy the agent) with recall ≥ ~0.85, and
  especially the **incident** cases — true negatives on real production
  false-positive injects.
- **FN attribution**: `run_e2e_eval.py` splits false negatives into
  `candidate_selection` (Stage-1 never proposed it) vs `verification`
  (Stage-1 proposed it, the judge rejected it) so a regression points at the
  right stage.
- **Chunking eval** (`stage1_chunking_<ts>.md`) reports, per configuration:
  `fp props (noise docs)` — Stage-1 proposals on documents that should fire
  nothing, each of which costs a Stage-2 forward pass in production; `buried R` /
  `clean R` — recall on cues inside noise and cues alone; `trunc chunks` — chunks
  whose *true* token length exceeds the embedder's 512 limit and are therefore
  silently cut before scoring; and an **accept-floor sweep** re-deciding every
  proposal at higher `positive_threshold` values. Read the two together: FPs
  falling while `buried R` also falls means the change is suppressing signal, not
  filtering noise.
- **Why the accept floor is the wrong knob.** Sweeping `positive_threshold` has no
  clean operating point — zero FPs costs ~35% of buried recall — because
  `bge-small` scores unrelated text ~0.7 anyway. `--margins` sweeps
  `RECALL_STAGE1_MARGIN` instead, which reads the *shape* of the router's ranking
  (flat = recognised nothing) rather than its absolute value:

  | margin | FPs on no-fire docs | buried R | clean R | dropped by cap |
  |---|---|---|---|---|
  | 0 (off) | 75 | 1.00 | 1.00 | 8 |
  | 0.03 | 7 | 1.00 | 1.00 | 0 |
  | 0.05 (default) | 1 | 1.00 | 1.00 | 0 |
  | 0.07 | 0 | 1.00 | 1.00 | 0 |
  | 0.10 | 0 | 0.94 | 0.94 | 0 |

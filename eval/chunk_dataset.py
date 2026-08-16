"""Raw-document dataset for the Stage-1 chunking / gating benchmark.

Every other recall eval feeds Stage-1 a chunk that has already been split down
to one clean sentence. Production does not: `_build_tool_use_recall_scan_text`
hands `scan_text_for_recalls` a whole mid-turn corpus — the model's thought, the
tool request, and the raw tool result, newline-joined — and the chunker decides
what the embedder actually sees. That decision is what caused the 2026-08-16
freeze (11 junk candidates off one tool result), and nothing tested it.

A case here is a whole document plus the set of recall_ids Stage-1 *should*
propose:

    {
        "doc": str,                 # multi-line raw scan text
        "expected_ids": set[str],   # recall_ids that should be proposed
        "family": str,              # tool_noise | signal_in_noise | clean_signal | chatter
        "name": str,                # stable case id
    }

Families:
  tool_noise      real tool output carrying no intent. expected_ids is empty, so
                  every proposal is a false positive and the count is the
                  Stage-2 CPU load the incident was made of.
  signal_in_noise one real cue sentence buried in that same tool output, at the
                  head / middle / tail. Tests whether chunking preserves signal
                  it should keep — the failure mode a noise-only suite would
                  happily "fix" by gating everything.
  clean_signal    the cue alone. Control: matches what today's tests cover.
  chatter         ordinary multi-line conversation with no recall intent.

Noise bodies are seeded, so the corpus is byte-stable across runs.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEM_RECALLS_PATH = REPO_ROOT / "config" / "system_recalls.json"

_SEED = 20260816


def load_system_recalls() -> dict[str, dict]:
    with open(SYSTEM_RECALLS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {r["recall_id"]: r for r in data if r.get("recall_id")}


# ---------------------------------------------------------------------------
# Noise generators — shaped like real tool output, not lorem ipsum.
# ---------------------------------------------------------------------------


def _ls_block(rng: random.Random, rows: int) -> str:
    names = [
        "recall.py", "agent.py", "tools.py", "reranker.py", "engine.py",
        "config.json", "state.db", "notes.md", "Dockerfile", "main.py",
    ]
    out = ["total 2873024", "drwxr-xr-x@ 13 user staff 416 Aug 16 18:25 ."]
    for _ in range(rows):
        out.append(
            f"-rw-r--r--@  1 user  staff  {rng.randint(1000, 99999999)} "
            f"Aug {rng.randint(1, 28)} {rng.randint(10, 23)}:{rng.randint(10, 59)} "
            f"{rng.choice(names)}"
        )
    return "\n".join(out)


def _json_block(rng: random.Random, fields: int) -> str:
    body = {
        f"field_{i}": rng.choice(
            [rng.randint(0, 10**6), f"value_{rng.randint(0, 9999)}", True, None]
        )
        for i in range(fields)
    }
    return json.dumps(body, indent=2)


def _git_status_block(rng: random.Random, rows: int) -> str:
    paths = ["harness/recall.py", "harness/agent.py", "eval/common.py", "config/system_recalls.json"]
    out = ["On branch clyra", "Your branch is up to date with 'origin/clyra'.", "", "Changes not staged for commit:"]
    for _ in range(rows):
        out.append(f"\tmodified:   {rng.choice(paths)}")
    return "\n".join(out)


def _traceback_block() -> str:
    return "\n".join([
        "Traceback (most recent call last):",
        '  File "/app/harness/agent.py", line 2613, in _verify_and_select_recalls',
        "    verified, rejected = await asyncio.to_thread(filter_matches_with_slm, matches)",
        '  File "/app/harness/recall.py", line 1190, in filter_matches_with_slm',
        "    hit = rr.best_match(text, docs, stop_at=thr)",
        "RuntimeError: non-finite rerank score nan",
    ])


def _html_block(rng: random.Random, rows: int) -> str:
    out = ['<div class="container">', '  <ul class="list">']
    for i in range(rows):
        out.append(f'    <li data-id="{rng.randint(0, 9999)}"><span>item {i}</span></li>')
    out.append("  </ul>")
    out.append("</div>")
    return "\n".join(out)


def _log_block(rng: random.Random, rows: int) -> str:
    out = []
    for _ in range(rows):
        out.append(
            f"15:33:{rng.randint(10, 59)} [galadriel.{rng.choice(['worker', 'scheduler', 'palace'])}] "
            f"INFO: {rng.choice(['Worker loop started', 'Scheduler running', 'Mined archive batch'])} "
            f"({rng.randint(1, 400)} items)"
        )
    return "\n".join(out)


def _minified_json(rng: random.Random, fields: int) -> str:
    """One-line JSON, as an HTTP/db tool result actually arrives.

    Newline splitting cannot break this up, so it is the only realistic shape
    that reaches the chunker's window as a single oversized unit.
    """
    body = {f"field_{i}": f"value_{rng.randint(0, 10**6)}" for i in range(fields)}
    return json.dumps(body, separators=(",", ":"))


def _long_prose_line(rng: random.Random, sentences: int) -> str:
    """A single unwrapped paragraph — a fetched article or long commit body."""
    bits = [
        "the deployment completed without incident and the rollout finished cleanly",
        "we reviewed the changes and decided the approach was sound overall",
        "latency stayed flat through the peak window with no error budget burn",
        "the team agreed to revisit the caching layer in the next cycle",
    ]
    return ". ".join(rng.choice(bits) for _ in range(sentences)) + "."


def _noise_bodies(rng: random.Random) -> list[tuple[str, str]]:
    """(name, body) pairs of pure tool output, no recall intent anywhere."""
    return [
        ("ls_small", _ls_block(rng, 8)),
        ("ls_large", _ls_block(rng, 60)),
        ("json_small", _json_block(rng, 10)),
        ("json_large", _json_block(rng, 120)),
        ("json_minified", _minified_json(rng, 400)),
        ("git_status", _git_status_block(rng, 6)),
        ("traceback", _traceback_block()),
        ("html", _html_block(rng, 20)),
        ("app_log", _log_block(rng, 30)),
        ("long_prose_line", _long_prose_line(rng, 60)),
    ]


# Mid-turn scan text is thought + tool request + result (see
# _build_tool_use_recall_scan_text). These preambles reproduce that shape.
_THOUGHTS = [
    "Let me look at what is in that directory before deciding.",
    "I will read the current state file and check the contents.",
    "Checking the output of the last command now.",
]

_TOOL_REQUESTS = [
    'run_shell\n{"command": "ls -la /app/state"}',
    'read_file\n{"path": "state/worker_control.md"}',
    'db_query\n{"collection": "runs", "limit": 50}',
]


def _as_scan_doc(rng: random.Random, body: str) -> str:
    """Wrap a raw tool body in the thought + request preamble production sends."""
    return "\n".join([rng.choice(_THOUGHTS), rng.choice(_TOOL_REQUESTS), body])


_CHATTER = [
    "\n".join([
        "sure, that makes sense to me.",
        "I pushed the branch and the build is green now.",
        "let me know if you want me to change the wording anywhere.",
    ]),
    "\n".join([
        "the weather here has been awful all week.",
        "I ended up staying in and watching a film instead.",
        "it was longer than I expected but pretty good.",
    ]),
    "\n".join([
        "ok cool.",
        "thanks for sorting that out so quickly.",
        "I appreciate it, talk tomorrow.",
    ]),
]


def build_chunk_dataset() -> list[dict]:
    """Deterministic list of raw-document cases."""
    rng = random.Random(_SEED)
    recalls = load_system_recalls()
    cases: list[dict] = []

    # 1. Pure tool output: nothing should ever fire.
    for name, body in _noise_bodies(rng):
        cases.append({
            "doc": _as_scan_doc(rng, body),
            "expected_ids": set(),
            "family": "tool_noise",
            "name": f"noise/{name}",
        })

    # 2. Ordinary conversation: nothing should fire either.
    for i, text in enumerate(_CHATTER):
        cases.append({
            "doc": text,
            "expected_ids": set(),
            "family": "chatter",
            "name": f"chatter/{i}",
        })

    # 3. One real cue buried in tool output, at three positions.
    noise = dict(_noise_bodies(rng))
    for recall_id, recall in recalls.items():
        cue = next(
            (e for e in (recall.get("positive_examples") or []) if isinstance(e, str) and e.strip()),
            None,
        )
        if not cue:
            continue
        body = noise["ls_large"] if rng.random() < 0.5 else noise["json_large"]
        lines = body.split("\n")
        mid = len(lines) // 2
        placements = {
            "head": "\n".join([cue.strip()] + lines),
            "middle": "\n".join(lines[:mid] + [cue.strip()] + lines[mid:]),
            "tail": "\n".join(lines + [cue.strip()]),
        }
        for where, doc in placements.items():
            cases.append({
                "doc": _as_scan_doc(rng, doc),
                "expected_ids": {recall_id},
                "family": "signal_in_noise",
                "name": f"buried/{recall_id}/{where}",
            })

    # 4. Control: the cue on its own, which is all the existing suites test.
    for recall_id, recall in recalls.items():
        cue = next(
            (e for e in (recall.get("positive_examples") or []) if isinstance(e, str) and e.strip()),
            None,
        )
        if not cue:
            continue
        cases.append({
            "doc": cue.strip(),
            "expected_ids": {recall_id},
            "family": "clean_signal",
            "name": f"clean/{recall_id}",
        })

    return cases


def chunk_dataset_stats(cases: list[dict]) -> dict:
    stats: dict = {"total": len(cases), "by_family": {}}
    for c in cases:
        fam = stats["by_family"].setdefault(c["family"], {"docs": 0, "expected_fires": 0})
        fam["docs"] += 1
        fam["expected_fires"] += len(c["expected_ids"])
    return stats


if __name__ == "__main__":
    cases = build_chunk_dataset()
    print(json.dumps(chunk_dataset_stats(cases), indent=2))

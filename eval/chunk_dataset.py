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
        "family": str,              # tool_noise | signal_in_noise | clean_signal
                                    # | chatter | competing_signal
        "name": str,                # stable case id
        "group": str,               # cases that are permutations of one another
        "variant": str,             # which permutation, within the group
    }

`group` is what makes positional instability measurable. Cases sharing a group
carry the same content in a different line order, so they must produce the same
proposals; scoring them independently (as this suite did until 2026-08-18)
reports a disagreement as mediocre recall instead of as non-determinism.

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
  competing_signal
                  one exact lexical cue plus prose that weakly attracts the same
                  recall, permuted over every position. Unlike signal_in_noise
                  the buried signal is *contested*: the prose scores in the same
                  0.6-0.75 band the cue's recall would, so whichever line the
                  scan reaches first decides what the judge is shown.
  long_paragraph  the cue inside one unwrapped paragraph, which newline
                  splitting cannot break up.
  runon_chat      the cue inside an unpunctuated single-line message, which
                  neither newline nor terminator splitting can break up, so the
                  whole message is scored as one vector.

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
        "recall.py", "agent.py", "tools.py", "recall_judge.py", "engine.py",
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
        "    ok, reason = verify_recall_candidate_slm(chunk, match, out=enriched)",
        "RuntimeError: non-finite verify score nan",
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


# ---------------------------------------------------------------------------
# competing_signal — the 2026-08-18 report, generalized.
#
# A user pastes an article, talks about it, and drops a link. The exact cue sits
# on one short line; the surrounding prose is on-topic enough to score in the
# same band as the cue's own recall. `prose` is tuned so the target recall clears
# its 0.6 floor on at least one prose line — that contest is the whole point, and
# `python -m eval.chunk_dataset --probe` prints the scores that verify it.
# ---------------------------------------------------------------------------

_COMPETING_CASES = [
    {
        "case": "control_plane",
        "recall_id": "sys_architecture",
        "cue_line": "walk me through the control plane routing again",
        "prose": [
            "the tenant isolation story is the interesting part of this design",
            "each runtime gets its own service and hostname, which keeps the blast radius small",
            "how the memory layers and the worker fit together still confuses me",
        ],
        "url": "https://medium.com/platform/multi-tenant-routing-explained-abc123",
    },
    {
        "case": "api_key",
        "recall_id": "sys_credentials",
        "cue_line": "where is the api key for that service stored?",
        "prose": [
            "the integration has to authenticate before it can list anything",
            "we should not be putting secrets straight into the task definition",
            "rotation is handled somewhere else so that part is not a concern here",
        ],
        "url": "https://medium.com/security/secret-rotation-patterns-def456",
    },
    {
        "case": "recurring",
        "recall_id": "sys_recurring_work",
        "cue_line": "can you run that check every day at 9am",
        "prose": [
            "the report only matters if it lands before the standup",
            "doing it by hand has been fine but it is getting tedious",
            "i would rather this just happened on a schedule without me asking",
        ],
        "url": "https://medium.com/ops/scheduling-jobs-reliably-ghi789",
    },
    {
        "case": "custom_script",
        "recall_id": "sys_custom_script",
        "cue_line": "write a custom script that parses these logs",
        "prose": [
            "the log format changed last month and the old parser broke",
            "there are about forty thousand lines to go through",
            "some one-off automation over this workspace would save me the afternoon",
        ],
        "url": "https://medium.com/data/log-parsing-at-scale-jkl012",
    },
]

# ---------------------------------------------------------------------------
# runon_chat — the one shape no splitter here can cut.
#
# User messages are scanned too (harness/agent.py), and a user typing without
# punctuation produces a single line with no `.?!` anywhere in it, so newline
# and sentence splitting both return the whole message as one unit and the cue
# is averaged into the message's topic centroid. Measured 2026-08-18: 45/48
# cues still proposed, but 6 of 16 groups answer differently depending on where
# in the message the cue sits, since word order moves the one vector.
#
# These variants reorder words rather than lines, so the unit text itself
# differs between them and every group counts as an unstable binding by
# construction. Read `agrees` for this family, not the binding column.
# ---------------------------------------------------------------------------

_RUNON_FILLER = [
    "hey quick one before i forget",
    "also the build has been flaky since yesterday morning",
    "no rush on any of this by the way",
]


_PLACEMENTS = ("head", "middle", "tail")


def _insert_at(lines: list[str], item: str, where: str) -> list[str]:
    if where == "head":
        return [item] + lines
    if where == "tail":
        return lines + [item]
    mid = len(lines) // 2
    return lines[:mid] + [item] + lines[mid:]


def _competing_cases() -> list[dict]:
    """Every (cue position, url position) pair of one document's lines."""
    cases: list[dict] = []
    for spec in _COMPETING_CASES:
        for cue_at in _PLACEMENTS:
            for url_at in _PLACEMENTS:
                lines = _insert_at(list(spec["prose"]), spec["cue_line"], cue_at)
                lines = _insert_at(lines, spec["url"], url_at)
                cases.append({
                    "doc": "\n".join(lines),
                    "expected_ids": {spec["recall_id"]},
                    "family": "competing_signal",
                    "group": f"competing/{spec['case']}",
                    "variant": f"cue-{cue_at}/url-{url_at}",
                    "name": f"competing/{spec['case']}/cue-{cue_at}/url-{url_at}",
                })
    return cases


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
            "group": f"noise/{name}",
            "variant": "only",
            "name": f"noise/{name}",
        })

    # 2. Ordinary conversation: nothing should fire either.
    for i, text in enumerate(_CHATTER):
        cases.append({
            "doc": text,
            "expected_ids": set(),
            "family": "chatter",
            "group": f"chatter/{i}",
            "variant": "only",
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
        # One preamble for all three placements. Drawing it per placement (as
        # this did until 2026-08-18) leaves the variants differing by more than
        # line order, which makes the group useless for measuring invariance.
        preamble = [rng.choice(_THOUGHTS), rng.choice(_TOOL_REQUESTS)]
        for where, doc in placements.items():
            cases.append({
                "doc": "\n".join(preamble + [doc]),
                "expected_ids": {recall_id},
                "family": "signal_in_noise",
                "group": f"buried/{recall_id}",
                "variant": where,
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
            "group": f"clean/{recall_id}",
            "variant": "only",
            "name": f"clean/{recall_id}",
        })

    # 5. Contested signal: exact cue vs. on-topic prose, at every position.
    cases.extend(_competing_cases())

    # 6. The same cue inside one unwrapped paragraph. Newline splitting cannot
    #    help here, so the whole paragraph embeds as a single vector and the
    #    cue's signal is averaged into the topic centroid. Measured 2026-08-18:
    #    16/16 cues proposed on their own line, 9/16 buried this way.
    for recall_id, recall in recalls.items():
        cue = next(
            (e for e in (recall.get("positive_examples") or []) if isinstance(e, str) and e.strip()),
            None,
        )
        if not cue:
            continue
        para = _long_prose_line(rng, 15)
        half = len(para) // 2
        cases.append({
            "doc": f"{para[:half]} {cue.strip()} {para[half:]}",
            "expected_ids": {recall_id},
            "family": "long_paragraph",
            "group": f"paragraph/{recall_id}",
            "variant": "only",
            "name": f"paragraph/{recall_id}",
        })

    # 7. The same cue inside an unpunctuated run-on message, at three positions.
    for recall_id, recall in recalls.items():
        cue = next(
            (e for e in (recall.get("positive_examples") or []) if isinstance(e, str) and e.strip()),
            None,
        )
        if not cue:
            continue
        bare_cue = cue.strip().rstrip(".?!").lower()
        for where in _PLACEMENTS:
            cases.append({
                "doc": " ".join(_insert_at(list(_RUNON_FILLER), bare_cue, where)),
                "expected_ids": {recall_id},
                "family": "runon_chat",
                "group": f"runon/{recall_id}",
                "variant": where,
                "name": f"runon/{recall_id}/{where}",
            })

    return cases


def chunk_dataset_stats(cases: list[dict]) -> dict:
    stats: dict = {"total": len(cases), "by_family": {}}
    for c in cases:
        fam = stats["by_family"].setdefault(c["family"], {"docs": 0, "expected_fires": 0})
        fam["docs"] += 1
        fam["expected_fires"] += len(c["expected_ids"])
    return stats


def probe_competing() -> None:
    """Print each competing line's dense score for its target recall.

    A competing case is only a real test if the prose contests the cue — i.e.
    at least one prose line clears the target's positive floor on its own. If
    every prose line sits well under it, the group agrees trivially and the case
    proves nothing, so this prints the numbers rather than assuming them.
    """
    import os

    os.environ.setdefault("RECALL_SLM_VERIFY", "1")
    os.environ.setdefault("RECALL_STAGE2_MODE", "embed")
    from harness import recall as hr

    catalog = list(hr._load_system_recalls())
    by_id = {r["recall_id"]: r for r in catalog if r.get("recall_id")}
    router = hr.get_semantic_router(catalog)

    for spec in _COMPETING_CASES:
        rid = spec["recall_id"]
        floor = hr.recall_positive_threshold(by_id.get(rid))
        print(f"\n{spec['case']} -> {rid} (floor {floor:.2f})")
        for kind, line in (
            [("cue", spec["cue_line"])]
            + [("prose", p) for p in spec["prose"]]
            + [("url", hr.normalize_scan_text(spec["url"]))]
        ):
            decisions = router(line, limit=hr._STAGE1_DENSE_TOP_K) or []
            if not isinstance(decisions, list):
                decisions = [decisions]
            ranked = hr._rank_decisions(decisions, by_id, line)
            own = next((s for n, s in ranked if n == rid), None)
            lex = hr._lexical_hit(line, (by_id.get(rid) or {}).get("lexical_cues") or [])
            top = f"{ranked[0][0]} {ranked[0][1]:.3f}" if ranked else "-"
            own_txt = f"{own:.3f}" if own is not None else "  -  "
            flag = "CONTESTS" if (own is not None and own >= floor) else ""
            print(
                f"  {kind:<5} own={own_txt} {'lex=' + lex if lex else '':<22}"
                f" top={top:<28} {flag}  {line[:60]!r}"
            )


if __name__ == "__main__":
    import sys

    if "--probe" in sys.argv:
        probe_competing()
    else:
        cases = build_chunk_dataset()
        print(json.dumps(chunk_dataset_stats(cases), indent=2))

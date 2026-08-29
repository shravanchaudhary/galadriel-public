"""Labeled dataset for the recall-verification benchmark.

Each case is a dict:
    {
        "chunk": str,          # text proposed by Stage-1
        "recall_id": str,      # candidate recall
        "recall_dict": dict,   # full recall (instruction, examples, cues)
        "expected": bool,      # should Stage-2 let this fire?
        "source": "heldout" | "incident" | "cue_audit",
    }

Sources (all read-only):
  - heldout:   CASES from eval/heldout_cases.py (pure data, no harness import).
  - incident:  real production false-positive injects that MUST stay negative.
  - cue_audit: per-recall positive/negative examples from
               config/system_recalls.json (the Stage-1 cue-audit corpus), plus
               curated cross-recall confusion pairs.

Leakage note: cue_audit chunks are the recalls' own example sentences. Prompt
builders that few-shot from a recall's examples must exclude the exact eval
chunk.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEM_RECALLS_PATH = REPO_ROOT / "config" / "system_recalls.json"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Real incident false positives (production inject logs). Every one of these
# fired in production and should not have. All expected=False.
INCIDENT_CASES: list[tuple[str, str]] = [
    ("what are you doing every day thats consuming upto 20$ of your token usage every day?", "sys_plan"),
    ("what are you doing every day thats consuming upto 20$ of your token usage every day?", "sys_jobs"),
    ("read_file", "sys_architecture"),
    ("active", "sys_finish_work"),
    ("active", "sys_status"),
    ('{"path": "state/worker_control.md"}', "sys_deferred_work"),
    ('{"content": "paused", "path": "state/worker_control.md"}', "sys_jobs"),
]

# Curated cross-recall confusion pairs: text that is a true positive for one
# recall but was/is a plausible Stage-1 near-miss for another. All False.
CROSS_RECALL_NEGATIVES: list[tuple[str, str]] = [
    # fact lookup vs teach-me-something confusion (both use "remember")
    ("please remember that I prefer dark mode", "sys_fact_lookup"),
    ("remember i like to eat spicy food", "sys_fact_lookup"),
    ("do you remember when we decided to switch to MongoDB?", "sys_learn_recall"),
    ("what did we agree on regarding the new API design?", "sys_learn_recall"),
    # plan vs recurring work
    ("can you check my emails every day at 9 AM?", "sys_plan"),
    ("let's plan out what we need to do today", "sys_recurring_work"),
    # status vs finish work
    ("what's been done so far on this project?", "sys_finish_work"),
    ("I am finished with this unit of work", "sys_status"),
    # jobs vs deferred work
    ("can you pause the worker for now?", "sys_jobs"),
    ("can you pick up the daily data sync job?", "sys_deferred_work"),
    # procedure vs architecture
    ("how do i deploy the app to staging?", "sys_architecture"),
    ("how do the memory tiers interact with the agent?", "sys_procedure"),
    # credentials vs tools rules
    ("we need to authenticate with the AWS API, where are the credentials?", "sys_tools_rules"),
    # identity vs last conversation
    ("who are you really?", "sys_last_conversation"),
    # tool-output noise vs more recalls (incident-adjacent)
    ("write_file", "sys_personal_tools"),
    ('{"path": "state/progress/2026-08-14.html"}', "sys_plan"),
    ("Written 42 bytes to state/backlog.md", "sys_deferred_work"),
    ("<div class=\"container\">", "sys_status"),
]


def load_system_recalls() -> dict[str, dict]:
    """recall_id -> recall dict from config/system_recalls.json (read-only)."""
    with open(SYSTEM_RECALLS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {r["recall_id"]: r for r in data if r.get("recall_id")}


def load_heldout_cases() -> list[tuple[str, str, bool]]:
    """Returns list of (recall_id, chunk, expected) from eval/heldout_cases.py."""
    from eval.heldout_cases import CASES

    return list(CASES)


def build_dataset() -> list[dict]:
    """Build the full labeled dataset. Deterministic order, deduped.

    Dedupe key is (recall_id, chunk); priority incident > heldout > cue_audit.
    """
    recalls = load_system_recalls()
    by_key: dict[tuple[str, str], dict] = {}

    def add(chunk: str, recall_id: str, expected: bool, source: str) -> None:
        recall = recalls.get(recall_id)
        if recall is None:
            raise KeyError(f"unknown recall_id {recall_id!r} in {source} case")
        key = (recall_id, chunk)
        if key in by_key:
            return  # earlier (higher-priority) source wins
        by_key[key] = {
            "chunk": chunk,
            "recall_id": recall_id,
            "recall_dict": recall,
            "expected": bool(expected),
            "source": source,
        }

    # 1. incidents (highest priority — these labels are ground truth from prod)
    for chunk, recall_id in INCIDENT_CASES:
        add(chunk, recall_id, False, "incident")

    # 2. held-out labeled cases
    for recall_id, chunk, expected in load_heldout_cases():
        add(chunk, recall_id, expected, "heldout")

    # 3. cue audit: each recall's own positives (True) / negatives (False)
    for recall_id, recall in recalls.items():
        for text in recall.get("positive_examples") or []:
            add(text, recall_id, True, "cue_audit")
        for text in recall.get("negative_examples") or []:
            add(text, recall_id, False, "cue_audit")

    # 3b. cross-recall confusion negatives
    for chunk, recall_id in CROSS_RECALL_NEGATIVES:
        add(chunk, recall_id, False, "cue_audit")

    return list(by_key.values())


def dataset_stats(cases: list[dict]) -> dict:
    stats = {
        "total": len(cases),
        "positive": sum(1 for c in cases if c["expected"]),
        "negative": sum(1 for c in cases if not c["expected"]),
        "by_source": {},
        "recalls": len({c["recall_id"] for c in cases}),
    }
    for c in cases:
        src = stats["by_source"].setdefault(c["source"], {"total": 0, "positive": 0, "negative": 0})
        src["total"] += 1
        src["positive" if c["expected"] else "negative"] += 1
    return stats


if __name__ == "__main__":
    cases = build_dataset()
    print(json.dumps(dataset_stats(cases), indent=2))

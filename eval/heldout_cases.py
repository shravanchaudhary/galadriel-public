"""Held-out labeled (recall_id, chunk, expected) cases for the recall e2e eval.

Pure data, no harness imports — kept separate so `eval/dataset.py` can load it
without pulling in `harness.recall`'s heavy deps. Split out from the old
Stage-2 verification test so the eval dataset keeps its held-out slice.
"""

from __future__ import annotations

# (recall_id, text, expect_yes) — held-out paraphrases / near-misses
CASES: list[tuple[str, str, bool]] = [
    ("sys_fact_lookup", "remind me what name was on yesterday's email?", True),
    ("sys_fact_lookup", "when did we choose MongoDB again?", True),
    ("sys_fact_lookup", "what was last month's cloud bill?", True),
    ("sys_fact_lookup", "what API design choice did we settle on?", True),
    ("sys_fact_lookup", "what do you think is the best way to write this function?", False),
    ("sys_fact_lookup", "can you write a script to scrape this website?", False),
    ("sys_fact_lookup", "tell me a joke about a programmer", False),
    ("sys_fact_lookup", "let's plan out the architecture for the new service", False),
    ("sys_procedure", "steps to ship this app to staging please?", True),
    ("sys_procedure", "we hit OOM — what's the recovery checklist?", True),
    ("sys_procedure", "how should I add a DB migration the usual way?", True),
    ("sys_procedure", "hello there, how are you doing today?", False),
    ("sys_procedure", "create a new file called test.py", False),
    ("sys_procedure", "what is the capital of France?", False),
    ("sys_learn_recall", "please remember that I prefer dark mode in editors", True),
    ("sys_learn_recall", "from now on always use type hints in python", True),
    ("sys_learn_recall", "save this rule: no emoji in commit messages", True),
    ("sys_learn_recall", "can you remember bill gates is no longer the godfather of capitalism", True),
    ("sys_learn_recall", "can you remember i like mangoes", True),
    ("sys_learn_recall", "can you remember i like mangoes", True),
    ("sys_learn_recall", "do you remember when we decided to switch to MongoDB?", False),
    ("sys_learn_recall", "what did we agree on regarding the new API design?", False),
    ("sys_learn_recall", "hello how are you today", False),
    ("sys_learn_recall", "why are you not creating semantic recall as well ?", False),
    ("sys_architecture", "how do memory tiers interact with the worker loop?", True),
    ("sys_architecture", "walk me through board files and compaction", True),
    ("sys_architecture", "write a unit test for the login form", False),
    # Tool-output / markup noise that Stage-1 falsely proposes (must stay NO).
    ("sys_identity", "<!doctype html>", False),
    ("sys_identity", "<body>", False),
    ("sys_credentials", "--bg: #ffffff;", False),
    ("sys_learn_recall", "<head>", False),
    ("sys_architecture", "read_file", False),
    ("sys_personal_tools", "Written 7 bytes to state/worker_control.md", False),
    ("sys_fact_lookup", '{"path": "state/plan/2026-08-10.html"}', False),
    ("sys_deferred_work", "<title>Progress · 2026-08-10</title>", False),
]

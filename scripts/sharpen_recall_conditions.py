#!/usr/bin/env python3
"""Rewrite system-recall activation conditions and exclusions for the judge.

The first backfill phrased conditions loosely and exclusions as bare noun
phrases, so the judge fired on any chunk that merely shared a topic ("can you
explain identity theft?" -> sys_identity). These rewrites do two things:

  * scope each condition to the agent's own self / work / workspace, and to the
    speech act (asks to DO x) rather than the topic (asks ABOUT x);
  * state exclusions as full clauses naming the discriminator, especially the
    three recurring confusions: definition requests, general how-does-it-work
    questions, and the user's own life rather than the agent's work.

It also repairs a contradiction: sys_deferred_work owns pause/resume worker (its
instruction and lexical cues say so), but the old exclusion handed it to
sys_jobs.

Idempotent — safe to re-run. Bounds match recall_judge.MAX_*_CHARS.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "system_recalls.json"
MAX_CHARS = 240

CONDITIONS: dict[str, tuple[str, str]] = {
    "sys_fact_lookup": (
        "The user asks the agent to state or rely on a specific past fact, "
        "decision, date, cost, name, or preference from this project's own "
        "history — something it must look up rather than reason out.",
        "Opinions, creative writing, or general world knowledge. Requests to "
        "produce new work rather than recall something already established.",
    ),
    "sys_procedure": (
        "The user asks how to carry out a procedure, recover from a known "
        "failure mode, or apply a technique that is documented in this "
        "project's own knowledge base.",
        "General programming or tooling questions with no project-specific "
        "procedure behind them. Greetings. Simple one-off file edits.",
    ),
    "sys_architecture": (
        "The user asks how THIS system is built: its memory tiers, worker "
        "role, board files, or control-plane/runtime split.",
        "Software architecture as a general subject, and any coding, layout, "
        "algorithm, or language question that is not about this system's own "
        "design.",
    ),
    "sys_tools_rules": (
        "The user asks which of the agent's own tools to use, where the agent "
        "should record its output, or how its heartbeat, wake, reflection, or "
        "browser-tab rules work.",
        "Questions about ordinary software or command-line tools. Plain task "
        "requests where no tool-selection policy is actually in question.",
    ),
    "sys_custom_script": (
        "The user asks the agent to write, run, or debug a script or one-off "
        "automation that operates on this workspace.",
        "Standalone code that is not automation for this workspace, generic "
        "snippets requested as examples, and questions about how a language "
        "feature or concept works.",
    ),
    "sys_personal_tools": (
        "The user asks the agent to build, update, or use one of its own coded "
        "personal tools, or to handle the credentials or preferences belonging "
        "to one.",
        "Physical real-world tools such as hammers. Edits to the agent's own "
        "harness source. Questions about what an API or a tool is in general.",
    ),
    "sys_jobs": (
        "The user asks the agent to pick up, start, or look up one of its "
        "background worker or curator jobs and the cookbook behind it.",
        "Human employment, careers, or job applications. Remarks about someone "
        "doing a good job. Pausing or resuming the worker itself, which is "
        "deferred-work control, not job pickup.",
    ),
    "sys_recurring_work": (
        "The user asks for work to happen on a repeating schedule — daily, "
        "weekly, or every time some event recurs.",
        "Something wanted once, right now. Repeating things in the user's own "
        "life that the agent is not being asked to run. Asking what a daily "
        "special or routine is.",
    ),
    "sys_deferred_work": (
        "The user asks the agent to park work for later, to pick queued work "
        "back up, to inspect the backlog, or to pause or resume its worker.",
        "Pausing unrelated things such as music. Requests to do something "
        "immediately. Writing background or async code, which is a programming "
        "task rather than backlog control.",
    ),
    "sys_plan": (
        "The user asks the agent to plan, re-plan, or prioritise its OWN work "
        "for the current day or work period.",
        "Planning something in the user's personal life, such as a trip or an "
        "event. Idioms like 'plan B'. Asking what a plan or business plan is.",
    ),
    "sys_finish_work": (
        "The agent's own current unit of work is being closed out and needs its "
        "status transition and wrap-up recorded.",
        "The user finishing their own unrelated task, such as homework. "
        "Statements that explicitly say no action is needed. Physical senses of "
        "'finish' like a wood finish or a finish line.",
    ),
    "sys_status": (
        "The user asks for current progress, counts, or blockers on the agent's "
        "ongoing work, without declaring that work finished.",
        "Declaring work complete. Asking to plan new work. Unrelated senses of "
        "'status' such as social status or HTTP status codes.",
    ),
    "sys_last_conversation": (
        "The user asks what was said or decided in an earlier session, or wants "
        "to resume where the last conversation left off.",
        "Operating on the text of the current conversation, such as translating "
        "or summarising it on request. Asking what a conversation is or how to "
        "start one.",
    ),
    "sys_credentials": (
        "The agent needs an actual secret, token, key, or login in order to "
        "perform an authenticated action against a real service.",
        "Asking what credentials are, how an auth protocol such as OAuth works "
        "in general, or how to reset a password on the user's own personal "
        "device. Generating a throwaway password.",
    ),
    "sys_identity": (
        "The user asks the agent about ITSELF — whether it is alive, sentient, "
        "conscious, or real, who it is, or how its own sense of self has "
        "developed.",
        "Identity as an abstract, legal, or criminal topic such as identity "
        "theft. Dictionary meanings of words like sentient. Song lyrics. "
        "Identifying some other thing, such as a bird.",
    ),
    "sys_learn_recall": (
        "The user states a durable fact, preference, or rule and wants the "
        "agent to retain it for future use.",
        "Asking whether something is already remembered, or what was decided "
        "before. A reminder or task tied to one specific occasion. Bare tool "
        "names, markup, or greetings.",
    ),
}


def _norm(text: str) -> str:
    return " ".join(text.split())


def main() -> int:
    recalls = json.loads(CONFIG.read_text())
    known = {r.get("recall_id") for r in recalls}
    missing = set(CONDITIONS) - known
    extra = known - set(CONDITIONS)
    if missing:
        print(f"[error] unknown recall ids in script: {sorted(missing)}")
        return 1
    if extra:
        print(f"[warn] system recalls with no rewrite: {sorted(extra)}")

    changed = 0
    for recall in recalls:
        pair = CONDITIONS.get(recall.get("recall_id"))
        if not pair:
            continue
        activation, exclusions = (_norm(p) for p in pair)
        for label, value in (("activation_condition", activation),
                             ("exclusions", exclusions)):
            if len(value) > MAX_CHARS:
                print(
                    f"[error] {recall['recall_id']}.{label} is {len(value)} "
                    f"chars; judge truncates at {MAX_CHARS}"
                )
                return 1
        if (recall.get("activation_condition") != activation
                or recall.get("exclusions") != exclusions):
            changed += 1
        recall["activation_condition"] = activation
        recall["exclusions"] = exclusions

    CONFIG.write_text(json.dumps(recalls, indent=4) + "\n")
    print(f"updated {changed}/{len(recalls)} system recalls in {CONFIG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

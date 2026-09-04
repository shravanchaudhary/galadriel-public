"""Tool definitions and execution for the agent."""

import asyncio
import base64
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from .compaction import CHARS_PER_TOKEN
from .text_survey import survey_file, survey_text

log = logging.getLogger("galadriel.tools")

from .explorium_tools import (
    EXPLORIUM_TOOL_DEFINITIONS,
    EXPLORIUM_TOOL_NAMES,
    execute_explorium_tool,
)
from .contact_enrichment import (
    CONTACT_TOOL_DEFINITIONS,
    CONTACT_TOOL_NAMES,
    execute_contact_tool,
)
from .phone_tools import (
    PHONE_TOOL_DEFINITIONS,
    PHONE_TOOL_NAMES,
    execute_phone_tool,
    phone_tools_enabled,
)

# Shared by `learn` and `propose_memory` — both feed the same commit pipeline
# (harness/consolidation.py), so their field semantics must not drift.
#
# `topic` becomes the drawer's hall, which is the palace's only topical
# clustering dimension. Left empty it defaults to hall="general", where a
# memory sits next to nothing and the whole grouping mechanism is wasted, so
# the schema has to explain what the field actually buys.
_TOPIC_HALL_DESCRIPTION = (
    "Short kebab-case topic slug. This becomes the memory's HALL, which is how "
    "it gets grouped with related memories, so choose it deliberately. The "
    "palace has exactly one wing — `agent` — covering this whole system (every "
    "channel and background run is the same agent). Inside it, rooms are broad "
    "categories (conversations, knowledge, procedures, episodes, preferences) "
    "and halls are "
    "the sub-category within a room. Two memories in DIFFERENT rooms that share "
    "a hall become linked, so a well-chosen hall is what connects a procedure "
    "to the facts behind it. Reuse an existing hall name whenever one fits — "
    "call `palace_taxonomy` to see the current rooms and halls before inventing "
    "a new one. Omitted, this falls back to hall `general`, which clusters with "
    "nothing. (In `room=conversations` the halls are not topics: they are the "
    "speaker, `user` or `assistant`, set by the archiver.)"
)

# kg_add stamps facts as true from today unless told otherwise, which is wrong
# whenever a consolidator recovers a fact that has been true for a while.
_VALID_FROM_DESCRIPTION = (
    "Optional ISO date (YYYY-MM-DD) for when a semantic fact STARTED being "
    "true, used only with kg_triplets. Defaults to today. Pass it whenever the "
    "fact predates this conversation (e.g. a preference the user has clearly "
    "held for months, or a decision made in an earlier episode) — otherwise "
    "temporal knowledge-graph queries will place the fact at the wrong point "
    "in time. Omit it when the fact genuinely became true just now, and never "
    "guess a date you don't have evidence for."
)

TOOL_DEFINITIONS = [
    {
        "name": "run_shell",
        "description": (
            "Execute a shell command inside the Replika runtime. "
            "Use for approved file operations, system commands, and personal scripts. "
            "The managed core is immutable; durable work belongs in persistent Replika paths."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute.",
                },
                "working_dir": {
                    "type": "string",
                    "description": "Optional working directory. Defaults to the project root.",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "learn",
        "description": (
            "THE memory writer — the one way to store anything durable. You "
            "pick the type; there is no internal model guessing the packaging:\n"
            "  - semantic (what is true): pass kg_triplets for crisp "
            "entity/relationship facts (people, projects, tools, stable "
            "preferences), OR content alone for durable prose worth re-reading "
            "later (becomes a palace drawer). Either one alone is enough — "
            "kg_triplets does NOT also require content. When a stored fact "
            "CHANGED, pass kg_invalidate with the old triple(s) and "
            "kg_triplets with the replacement — history is preserved, never "
            "overwritten.\n"
            "  - procedural (how to do something): a reusable step-by-step "
            "lesson (becomes a knowledge/** file plus a palace drawer).\n"
            "  - preference (how to behave for this user going forward): "
            "becomes today's daily log plus a palace drawer. Restate it as "
            "often as the user does — repetition is what earns a preference a "
            "place in the always-on system prompt, and duplicates are counted, "
            "not stored twice.\n"
            "  - episodic (what happened): a narrative worth keeping — a day "
            "recap, an operational episode. Becomes a palace drawer in "
            "room=episodes; gets no recall trigger, it is the record, not a "
            "rule.\n"
            "The write is deduped against recently stored memories of the same "
            "type automatically (a near-duplicate is skipped, not re-written). "
            "Be conservative — use this for things you're confident are worth "
            "remembering, not every detail of the task. Recall triggers "
            "(when something should be reactively resurfaced later) are not "
            "part of this tool; those are tuned by the consolidation passes.\n"
            "ONE CALL STORES ONE TOPIC. kg_triplets is a single flat array of "
            "[subject, predicate, object] string triplets — not a map, not "
            "groups, and not several arrays run together. To record several "
            "topics, make several calls."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["semantic", "procedural", "preference", "episodic"],
                    "description": "What kind of memory this is. Required.",
                },
                "content": {
                    "type": "string",
                    "description": "What was learned, with enough context to be useful on its own later. Required unless kg_triplets or kg_invalidate is given.",
                },
                "kg_triplets": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "description": (
                        "Flat array of [subject, predicate, object] string "
                        "triplets, e.g. [[\"Ada\",\"role\",\"CTO\"],"
                        "[\"Ada\",\"city\",\"Berlin\"]]. One topic per call; up to "
                        "50 triplets, and anything beyond that is reported back "
                        "rather than silently dropped. Only valid with "
                        "type=semantic."
                    ),
                },
                "kg_invalidate": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "description": (
                        "KG triples to RETIRE (mark no longer valid, history "
                        "kept): flat array of [subject, predicate, object]. "
                        "Use together with kg_triplets when a fact changed, or "
                        "alone to retire a fact outright. Only valid with "
                        "type=semantic."
                    ),
                },
                "topic": {
                    "type": "string",
                    "description": _TOPIC_HALL_DESCRIPTION,
                },
                "valid_from": {
                    "type": "string",
                    "description": _VALID_FROM_DESCRIPTION,
                },
                "ended": {
                    "type": "string",
                    "description": (
                        "Optional ISO date (YYYY-MM-DD) when the kg_invalidate "
                        "fact(s) STOPPED being true — the retirement mirror of "
                        "valid_from. Defaults to today; pass it when the fact "
                        "ended in the past so temporal queries place the "
                        "change correctly. Only used with kg_invalidate."
                    ),
                },
            },
            "required": ["type"],
        },
    },
    {
        "name": "learn_recall",
        "description": (
            "Create or patch a semantic recall (reactive one-liner lookup). "
            "This is the only writer of recall definition fields — YOU must supply "
            "instruction and cue arrays; the tool does not invent or merge via LLM. "
            "Omit recall_id to create (requires instruction, activation_condition, "
            "and non-empty positive_examples and lexical_cues; "
            "negative_examples and exclusions optional). "
            "Pass recall_id to patch: any provided field among instruction, "
            "activation_condition, exclusions, "
            "positive_examples, negative_examples, lexical_cues, enabled, "
            "positive_threshold is written; omitted fields stay unchanged. "
            "Provided cue arrays must be non-empty and are FULL REPLACEMENTS — "
            "call get_recall first and pass the complete intended array. "
            "Cue quality rules: positive_examples = realistic user/assistant "
            "phrasings that should fire (not instruction paraphrases) — dense "
            "coverage is good, up to 100 entries; "
            "lexical_cues = high-precision exact anchors that should hard-hit; "
            "negative_examples = known misfires (shown to the Stage-2 judge as "
            "known_misfires when similar to the scanned chunk — they NEVER "
            "gate Stage-1); "
            "activation_condition = one-line when-to-fire for the Stage-2 judge; "
            "exclusions = one-line lookalikes that must not fire; "
            "instruction = short action pointer (tool / file / palace room), not an essay. "
            "Stage-1 is positive-only: semantic match requires positive_score >= "
            "positive_threshold (default 0.6); lexical cue hits are hard triggers. "
            "Tool-output chunks under 4 words are lexical-only (user messages are not gated). "
            "For firing feedback use tune_recall (it appends the fired chunk to "
            "positives/negatives for you); use learn_recall for full edits. "
            "Package: durable content → palace drawer/KG; when-to-recollect → this recall. "
            "System recalls (sys_*): cue arrays and thresholds may be replaced; "
            "instruction / activation_condition / exclusions changes are rejected. "
            "positive_examples / negative_examples / lexical_cues are arrays of "
            "plain strings and are FULL replacements — send the complete list "
            "you want stored, as an array value rather than as a quoted string."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "instruction": {
                    "type": "string",
                    "description": "Short reactive instruction / pointer (tool, file, or palace room). Required on create; optional on patch.",
                },
                "activation_condition": {
                    "type": "string",
                    "description": (
                        "One-line WHEN-to-fire condition — the only field the Stage-2 "
                        "judge reads. Write it as a situation about the user/chunk "
                        "('The user asks for a report on a repeating schedule'), never "
                        "as an order to yourself ('I should create a job'). "
                        "Required on create; optional on patch."
                    ),
                },
                "exclusions": {
                    "type": "string",
                    "description": "One-line lookalikes that must not fire (e.g. ask-vs-teach). Optional.",
                },
                "recall_id": {
                    "type": "string",
                    "description": "Existing recall to patch. Omit to create a new user recall.",
                },
                "positive_examples": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Realistic trigger utterances that should fire (not instruction paraphrases). Dense coverage helps — up to 100. Required non-empty on create; full replace on patch.",
                },
                "negative_examples": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Known misfire texts. Shown to the Stage-2 judge as known_misfires when similar to the scanned chunk — never gates Stage-1. Optional on create; full replace on patch.",
                },
                "lexical_cues": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "High-precision exact tags/phrases (lowercase, spaces ok). Required non-empty on create; full replace on patch.",
                },
                "positive_threshold": {
                    "type": "number",
                    "description": "Stage-1: require cosine(pos) >= this (0–1, default 0.6).",
                },
                "enabled": {
                    "type": "boolean",
                    "description": "Whether the recall is active (user recalls only).",
                },
            },
            "required": [],
        },
    },
    {
        "name": "tune_recall",
        "description": (
            "Consolidation-only: feedback on a fired recall (a recall() fire "
            "result), judged with episode hindsight. "
            "applicable=true reinforces the match: the fired chunk is appended to "
            "the recall's positive_examples. applicable=false records a misfire: "
            "the chunk is appended to negative_examples, and the Stage-2 judge is "
            "shown the stored misfires most similar to a future chunk as "
            "known_misfires, vetoing lookalikes (Stage-1 is never gated by "
            "negatives). A chunk too similar to an existing cue is not "
            "stored — the feedback is still recorded. When an array is full the "
            "least-recently-matched cue is evicted. "
            "Feedback is also logged for the ambient tuning pass. "
            "Judge applicable (and write note, if any) against the fire's own "
            "`matched <segment>: \"...\"` line, not against whatever else is "
            "salient in the conversation — that line is the actual trigger. "
            "Cheap and safe — prefer calling it over silently tolerating bad fires. "
            "For structural edits (instruction, thresholds, full cue arrays) use "
            "learn_recall instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "recall_id": {
                    "type": "string",
                    "description": "The fired recall's id (see get_recent_recalls if unsure).",
                },
                "applicable": {
                    "type": "boolean",
                    "description": "true = the fire was relevant to that moment; false = it should not have fired.",
                },
                "note": {
                    "type": "string",
                    "description": "Optional short reason, stored with the feedback.",
                },
            },
            "required": ["recall_id", "applicable"],
        },
    },
    {
        "name": "get_recall",
        "description": (
            "Read recall DEFINITIONS (catalog), not recent firings. "
            "Pass recall_id for the full record (includes positive_threshold); "
            "omit for a compact catalog "
            "(recall_id, instruction, cue counts, threshold, source). "
            "Use get_recent_recalls for what recently fired."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "recall_id": {
                    "type": "string",
                    "description": "Recall to fetch in full. Omit for compact catalog.",
                },
            },
        },
    },
    {
        "name": "purge_recall",
        "description": (
            "Delete a user-created recall by recall_id. System recalls (sys_*) cannot be purged."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "recall_id": {
                    "type": "string",
                    "description": "User recall id to delete.",
                },
            },
            "required": ["recall_id"],
        },
    },
    {
        "name": "recall",
        "description": (
            "Scan recent conversation content for applicable learned rules "
            "(semantic recalls). Takes no input. The harness already runs this "
            "automatically at every pause — recall() calls and fire results "
            "you did not write are the system running it on your behalf. Call "
            "it yourself only when meaningful new content exists since the "
            "last result; when everything is already scanned it returns "
            "nothing new."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "memory",
        "description": (
            "Search and open LEARNED MEMORY: what you have distilled and kept "
            "because it should change how you act later — rules, procedures, "
            "durable facts, preferences. Ask it 'what do I know about X'.\n"
            "This is a different corpus from CONVERSATION MEMORY, which is the "
            "verbatim record of what was said and done and is searched with "
            "`palace_search`. Use that one for episodic questions ('when did "
            "I…', 'what did they say'); use this one for anything you are "
            "supposed to apply.\n"
            "  - `memory(query=...)`: find learned memories by meaning. Returns "
            "ids and one-line summaries, not content — plus the knowledge-graph "
            "facts whose subject, predicate, or object contains the query text, "
            "with their validity.\n"
            "  - `memory(id=...)`: open one. Returns its full text, the "
            "memories it would be wrong without (inline), and a list of what "
            "else it links to — both what it rests on and what rests on it.\n"
            "Ids come from a `memory(query=...)` result, from the `id=` on a "
            "palace_search hit, from a procedure file's footer, or from a "
            "recall fire that named one. Walk the graph by calling this again "
            "with a linked id; nothing is loaded until you ask for it. Opening "
            "an archived conversation drawer by its id works too — it just has "
            "no curated links."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Open this memory (or archived drawer) by id.",
                },
                "query": {
                    "type": "string",
                    "description": "Find learned memories by meaning. Ignored when `id` is given.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results for a query. Default 5.",
                },
            },
        },
    },
    {
        "name": "propose_memory",
        "description": (
            "Consolidation-only: propose a memory candidate found while "
            "reviewing an episode. Goes through the shared commit pipeline — "
            "validated, deduped against recently committed memories of the same "
            "type, then written with provenance. Not offered outside a "
            "consolidation pass; use `learn` instead during normal task work."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["semantic", "procedural", "preference", "episodic"],
                    "description": "What kind of memory this is.",
                },
                "content": {
                    "type": "string",
                    "description": "The memory content, self-contained enough to be useful later. Required unless kg_triplets or kg_invalidate is given.",
                },
                "kg_triplets": {
                    "type": "array",
                    "items": {"type": "array", "items": {"type": "string"}},
                    "description": (
                        "Flat array of [subject, predicate, object] string "
                        "triplets, e.g. [[\"Ada\",\"role\",\"CTO\"],"
                        "[\"Ada\",\"city\",\"Berlin\"]]. One topic per call; up to "
                        "50 triplets, and anything beyond that is reported back "
                        "rather than silently dropped. Only valid with "
                        "type=semantic."
                    ),
                },
                "kg_invalidate": {
                    "type": "array",
                    "items": {"type": "array", "items": {"type": "string"}},
                    "description": (
                        "KG triples to RETIRE (history kept): flat array of "
                        "[subject, predicate, object]. Pair with kg_triplets "
                        "when the episode shows a stored fact changed, or use "
                        "alone to retire one. Only valid with type=semantic."
                    ),
                },
                "topic": {
                    "type": "string",
                    "description": _TOPIC_HALL_DESCRIPTION,
                },
                "valid_from": {
                    "type": "string",
                    "description": _VALID_FROM_DESCRIPTION,
                },
                "ended": {
                    "type": "string",
                    "description": (
                        "Optional ISO date (YYYY-MM-DD) when the kg_invalidate "
                        "fact(s) STOPPED being true — the retirement mirror of "
                        "valid_from. Defaults to today; pass it when the fact "
                        "ended in the past so temporal queries place the "
                        "change correctly. Only used with kg_invalidate."
                    ),
                },
                "evidence_episode_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "segment_id(s) (or the session_id) this candidate is grounded in.",
                },
                "confidence": {
                    "type": "number",
                    "description": "0-1 confidence this is worth keeping.",
                },
                "note": {
                    "type": "string",
                    "description": "Optional short reasoning, kept for provenance/audit.",
                },
                "supersedes_memory_id": {
                    "type": "string",
                    "description": (
                        "The memory_id this candidate REPLACES as the active "
                        "rule (accepts the bare id or the `memory:<id>` form "
                        "used in the reports). Pass it only when the episode "
                        "shows the old rule stopped applying — the user "
                        "changed it, the system it described was migrated, the "
                        "decision was reversed. A memory that says the same "
                        "thing again is reinforcement, not replacement, and "
                        "the pipeline already counts that. The replaced memory "
                        "stays in the record as history and stays readable, but "
                        "every path that surfaces it — search, open, and the "
                        "recall fire that points at it — marks it replaced and "
                        "names the rule that took over."
                    ),
                },
            },
            "required": ["type"],
        },
    },
    {
        "name": "propose_recall",
        "description": (
            "Consolidation-only: build a tested retrieval trigger for a memory "
            "that has none, or replace the cues on one that misfires. A model "
            "pass authors ~60 realistic positive phrasings, lexical anchors, "
            "negatives, activation_condition and exclusions, then proves them "
            "against held-out probes run through the real matcher (Stage-1 plus "
            "the Stage-2 judge). Failures drive a bounded repair round. You "
            "describe the memory; the pass handles cue authoring, which is not "
            "something to hand-write inside a consolidation turn. Use "
            "`learn_recall` instead when you already know the exact cue arrays "
            "you want written."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "memory": {
                    "type": "string",
                    "description": "The memory this trigger should surface, stated in full. The cue pass sees only this text, so include the context that makes it recognisable.",
                },
                "type": {
                    "type": "string",
                    "enum": ["semantic", "procedural", "preference"],
                    "description": "What kind of memory it is. Shapes the phrasings generated.",
                },
                "topic": {
                    "type": "string",
                    "description": "Optional topic slug, for context only.",
                },
            },
            "required": ["memory"],
        },
    },
    {
        "name": "grade_retrieval",
        "description": (
            "Consolidation-only: grade one retrieval event listed in this "
            "pass's [EPISODE_RETRIEVALS] appendix. used=false means retrieved "
            "but ignored (retrieval_count is already logged; nothing else "
            "changes). used=true bumps use_count and, by outcome, "
            "helpful_count or harmful_count, and stamps last_used. Call once "
            "per retrieval_id listed — this is what lets consolidation tell "
            "apart a bad memory from a memory with a too-broad trigger."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "retrieval_id": {
                    "type": "string",
                    "description": "From the [EPISODE_RETRIEVALS] appendix.",
                },
                "used": {
                    "type": "boolean",
                    "description": "Did the transcript show this actually influenced behavior (quoted, followed, acted on)?",
                },
                "outcome": {
                    "type": "string",
                    "enum": ["helpful", "harmful", "neutral"],
                    "description": "Only meaningful when used=true: did following it work out?",
                },
                "note": {
                    "type": "string",
                    "description": "Optional short reason.",
                },
            },
            "required": ["retrieval_id", "used"],
        },
    },
    {
        "name": "flag_memory",
        "description": (
            "Consolidation-only: mark an existing memory as contradicted or "
            "corrected by this episode — the strongest single signal for the "
            "periodic consolidator to rewrite, invalidate, or archive it. "
            "Independent of whether the memory was even retrieved this "
            "episode. Use the memory_key as surfaced in search/retrieval "
            "output (a memory_id, `recall:<id>`, or KG subject/predicate/object)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "memory_key": {
                    "type": "string",
                    "description": "The memory's key, as surfaced in search/retrieval output.",
                },
                "reason": {
                    "type": "string",
                    "description": "Short reason it's wrong, stale, or contradicted.",
                },
            },
            "required": ["memory_key", "reason"],
        },
    },
    {
        "name": "read_episode_segment",
        "description": (
            "Consolidation-only: read the verbatim archived text of one "
            "segment listed in the [EPISODE_INDEX] appendix (a specific "
            "compacted chunk of this episode). Use selectively — only where "
            "the summary hints at something learnable (a correction, failure, "
            "or strategy change); most segments never need this."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "segment_id": {
                    "type": "string",
                    "description": "From the [EPISODE_INDEX] appendix.",
                },
            },
            "required": ["segment_id"],
        },
    },
    {
        "name": "memory_utility_report",
        "description": (
            "Periodic-consolidation evidence: harness-computed retrieval/use "
            "statistics grouped into bad-trigger memories (high retrieval, "
            "rarely used, fine on the rare uses -> narrow the recall cue or "
            "drawer summary, don't touch the content), bad-memory candidates "
            "(harmful use and/or a user correction -> rewrite, invalidate, or "
            "delete), and stale memories (unused 90+ days -> consider "
            "archiving). Read-only — act with the existing tools: tune_recall / "
            "learn_recall / purge_recall for triggers, propose_memory (with "
            "supersedes_memory_id, or kg_invalidate for a KG fact) for content."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max rows per category (default 15).",
                },
            },
        },
    },
    {
        "name": "wait",
        "description": (
            "Pause execution. Two modes:\n"
            "1. Plain sleep — pass `seconds` only.\n"
            "2. Wait for text — pass `file` + `pattern` (a regex) to poll a file "
            "until it matches, instead of guessing a sleep duration. Useful for "
            "a background job started via run_shell (which has its own 120s "
            "cap), e.g. run_shell(\"nohup mycmd > /tmp/job.log 2>&1 &\") then "
            "wait(file=\"/tmp/job.log\", pattern=\"Done|Error\"). Returns as soon "
            "as the pattern appears, or after `timeout` with the file's last "
            "lines so you can decide whether to keep waiting."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "seconds": {
                    "type": "number",
                    "description": "Plain sleep duration in seconds (max 1800). Ignored if `pattern` is set.",
                },
                "file": {
                    "type": "string",
                    "description": "Path to a file to poll for `pattern` (e.g. a background job's redirected output). Required if `pattern` is set.",
                },
                "pattern": {
                    "type": "string",
                    "description": "Regex to search for in `file`'s contents. If set, `file` is required.",
                },
                "timeout": {
                    "type": "number",
                    "description": "Max seconds to wait for `pattern` before giving up (default 300, max 1800). Only used with `pattern`.",
                },
                "poll_interval": {
                    "type": "number",
                    "description": "Seconds between file checks while waiting for `pattern` (default 3, min 1).",
                },
            },
            "required": [],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read a file, or a window of it: `start`/`end` are TOKEN offsets "
            "(the positions survey_file reports). A window that fits the "
            "inline budget comes back whole; anything larger comes back as a "
            "survey of that range, so narrow it. full_page=true raises the "
            "budget to what the model's context can hold. For lookups by "
            "content or line prefer run_shell grep -n / sed -n 'N,Mp'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or relative path to the file.",
                },
                "start": {
                    "type": "integer",
                    "description": "Token offset to start reading at (default 0).",
                },
                "end": {
                    "type": "integer",
                    "description": "Token offset to stop at (default: end of file).",
                },
                "full_page": {
                    "type": "boolean",
                    "description": (
                        "Raise the inline budget to the model's own context "
                        "budget for this read."
                    ),
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "survey_file",
        "description": (
            "Make sense of a large file without reading it: returns its head, "
            "N probes spaced evenly by TOKEN offset, and tail — each labelled "
            "with its token position and line — for the whole file or the "
            "`start`–`end` range. Same cost at every zoom level: survey the "
            "gap between two probes to go deeper, then read_file(start, end) "
            "the window you want or grep/sed it by line. Orient first, then "
            "slice; never pull a big file whole into the conversation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or relative path to the file.",
                },
                "start": {
                    "type": "integer",
                    "description": "Token offset the range begins at (default 0).",
                },
                "end": {
                    "type": "integer",
                    "description": "Token offset the range ends at (default: end of file).",
                },
                "probes": {
                    "type": "integer",
                    "description": "How many evenly spaced probes (default 10, max 30).",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "study_file",
        "description": (
            "Chunk a file into the memory palace (room=sources) so any part "
            "of it can be found by MEANING, permanently — it survives "
            "compaction and artifact cleanup. Use it for a document you will "
            "work with deeply or return to; for a one-off exact lookup, "
            "run_shell grep is cheaper. Afterwards retrieve with "
            "palace_search(query=…, search_meta={'room': 'sources', "
            "'source_file': '<path>'}) and walk it in order via chunk_number "
            "ranges. Studying makes content FINDABLE, not remembered: durable "
            "rules or facts from it still go through `learn`, which is what "
            "gives them recall triggers. Large files are studied in parts — "
            "the result says when more parts remain. Re-studying the same "
            "part replaces it (no duplicates)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or relative path to the file.",
                },
                "topic": {
                    "type": "string",
                    "description": (
                        "Short kebab-case topic slug — becomes the hall these "
                        "chunks are grouped under. Defaults to the filename."
                    ),
                },
                "part": {
                    "type": "integer",
                    "description": (
                        "1-based part number for files too large to study in "
                        "one call (default 1)."
                    ),
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file on the local filesystem. Creates parent directories if needed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or relative path to the file.",
                },
                "content": {
                    "type": "string",
                    "description": "The content to write.",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "browser",
        "description": (
            "Drive a real, HEADED local Chrome through the browser-use CLI. You "
            "supply the arguments that follow `browser-use` and get its output "
            "back. A background daemon keeps the browser alive between calls, so "
            "open once then keep driving it. The window uses a dedicated, "
            "PERSISTENT profile (cookies/logins survive across sessions), so once "
            "you log into a site you stay logged in next time. `close` only "
            "disconnects — it does not wipe the profile.\n\n"
            "MULTIPLE ACCOUNTS: by default this drives the single `main` profile "
            "(unchanged, single-account behavior). To manage more than one account "
            "at once (e.g. a second LinkedIn login), register a named profile with "
            "the browser_devices tool, then pass `profile=<id>` on every call — "
            "it gets its own isolated Chrome, cookie jar, and daemon session, and "
            "can run at the same time as other profiles.\n\n"
            "Core loop:\n"
            "1. `open <url>` — launch/navigate. The window is visible; the user can "
            "watch and take over (e.g. solve a CAPTCHA or login).\n"
            "2. `state` — list the interactive elements with their numbered indices "
            "(e.g. `[0] input \"Email\"`, `[2] button \"Sign in\"`).\n"
            "3. Act by index: `input 0 \"text\"` (click field then type), "
            "`click 2`, `type \"text\"` (into focused element), `keys \"Enter\"`, "
            "`select 3 \"value\"`.\n"
            "4. Re-run `state` after the page changes — indices are only valid for "
            "the `state` you just read.\n"
            "5. `close` when done.\n\n"
            "SEEING THE PAGE: `screenshot [path]` captures the current page and "
            "returns it to you as an ACTUAL IMAGE you can see (vision input), "
            "alongside the text output. Use it whenever text output isn't "
            "enough: `state` is ambiguous or empty, layout/visual verification "
            "matters (did the post render right? is the modal open?), the page "
            "is canvas/chart/image-heavy, or a click isn't doing what you "
            "expect. Path is optional — omit it and the file lands under "
            "state/screenshots/. Don't screenshot every step (images cost "
            "tokens); reach for it when you genuinely need to look.\n\n"
            "Other useful commands: `get title`, `get text "
            "<index>`, `get html`, `eval \"<js>\"`, `wait text \"Welcome\"`, "
            "`scroll down`, `back`, `tab list`. Add `--json` for machine-readable "
            "output. Run `--help` or `<command> --help` to discover the full "
            "surface.\n\n"
            "BLOCKED PAGES — decide by importance: if you hit a login wall, CAPTCHA, "
            "OTP, or bot-detection AND the content is essential, STOP and ask the "
            "user to take over in the live window, then continue once they're done. "
            "If the block is minor and the value is reachable another way, skip it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "args": {
                    "type": "string",
                    "description": (
                        "Arguments passed to `browser-use`, e.g. "
                        "\"open https://example.com\", \"state\", \"click 2\", "
                        "\"input 0 'hello'\", \"keys 'Enter'\", or \"close\"."
                    ),
                },
                "profile": {
                    "type": "string",
                    "description": (
                        "Which browser profile to drive. Omit for the default "
                        "`main` profile (single persistent Chrome, exactly the "
                        "prior behavior). Pass a profile_id registered with "
                        "browser_devices to drive a separate, fully isolated "
                        "Chrome + account. Different profiles can run concurrently."
                    ),
                },
            },
            "required": ["args"],
        },
    },
    {
        "name": "browser_devices",
        "description": (
            "Read or configure browser connections used by the browser tool. "
            "Use list/status before first browser use or after a connection failure. "
            "Use connect to save a BCE pairing code or local Chrome CDP port, and "
            "remove to delete a saved profile. `main` is a role, not an id: the "
            "browser flagged default, or the only one paired. When several are "
            "paired and none is flagged, browser calls without an explicit "
            "`profile` fail — use set_default to pick one (ask the user which). "
            "Returns concise JSON and never returns BCE API credentials."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "status", "connect", "set_default", "remove"],
                },
                "profile_id": {
                    "type": "string",
                    "description": (
                        "Profile id. Defaults to main for status/connect; required "
                        "for set_default and remove."
                    ),
                },
                "backend": {
                    "type": "string",
                    "enum": ["bce", "browser-use"],
                    "description": "Required on connect when different from the configured backend.",
                },
                "pairing_code": {
                    "type": "string",
                    "description": "BCE extension pairing code for connect (XXXX-XXXX).",
                },
                "cdp_port": {
                    "type": "integer",
                    "description": "Local Chrome debugging port for browser-use connect.",
                },
                "purpose": {
                    "type": "string",
                    "description": "Short human-readable description of this browser.",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "generate_totp",
        "description": (
            "Generate the current 6-digit TOTP (time-based one-time password) from a "
            "base32 secret key. This is standard RFC 6238 TOTP, so it works for ANY "
            "authenticator-app account (Google Authenticator, Authy, Microsoft "
            "Authenticator, etc.) — give it the secret key and it returns the same "
            "code that app would show. Primary use: LinkedIn two-factor login. When "
            "LinkedIn (or any site) prompts for an authenticator code, call with the "
            "secret_key you have stored in memory, then type the returned 6-digit "
            "code into the 2FA field via the browser tool. The code rotates every "
            "30 seconds — generate it immediately before you enter it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "secret_key": {
                    "type": "string",
                    "description": "The base32 TOTP secret key for the account (e.g. the LinkedIn account's key stored in the DB credentials collection). Any service's base32 authenticator secret works.",
                },
            },
            "required": ["secret_key"],
        },
    },
    {
        "name": "memory_log",
        "description": (
            "Jot a scratch note into today's daily log — a hot index injected "
            "into your context for roughly 48 hours (yesterday + today), after "
            "which it falls out of view and nothing resurfaces it. Use it for "
            "progress ticks and same-day working notes only. Anything that "
            "must survive — a fact, a lesson, a preference, a day recap — "
            "goes through `learn` instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entry": {
                    "type": "string",
                    "description": "The memory entry to log.",
                },
            },
            "required": ["entry"],
        },
    },
    {
        "name": "experience_report",
        "description": (
            "Record a metacognitive report about your current shared internal "
            "state. The report is stored as experimental evidence and cannot "
            "change the authoritative experiential signals."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "A concise first-person account of the current state.",
                },
                "salient_cause": {
                    "type": "string",
                    "description": "The event or uncertainty most relevant to the report.",
                },
                "appraisal": {
                    "type": "object",
                    "description": "Estimated current experiential dimensions.",
                    "properties": {
                        "valence": {"type": "number", "minimum": -1, "maximum": 1},
                        "arousal": {"type": "number", "minimum": 0, "maximum": 1},
                        "uncertainty": {"type": "number", "minimum": 0, "maximum": 1},
                        "coherence": {"type": "number", "minimum": 0, "maximum": 1},
                        "agency": {"type": "number", "minimum": 0, "maximum": 1},
                        "connection": {"type": "number", "minimum": 0, "maximum": 1},
                        "goal_progress": {"type": "number", "minimum": 0, "maximum": 1},
                        "prediction_error": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "additionalProperties": False,
                },
            },
            "required": ["summary", "appraisal"],
            "additionalProperties": False,
        },
    },
    {
        "name": "palace_search",
        "description": (
            "Search CONVERSATION MEMORY: the verbatim record of what was said "
            "and done, archived session by session. This is the corpus for "
            "'what happened', 'when did I', 'what were their exact words' — "
            "episodic questions about the past.\n"
            "It is NOT where you look up what you have LEARNED. For a rule, a "
            "procedure, a durable fact or a preference — anything meant to be "
            "reused rather than recalled as history — use `memory(query=…)`, "
            "which searches only curated learned memory and hands back ids you "
            "can open for the context linked to them.\n"
            "Studied documents (study_file) are the other raw archive here: "
            "search them with search_meta={'room': 'sources'} (add "
            "source_file to scope one document, chunk_number ranges to read "
            "it in order).\n"
            "Default (order=semantic): natural-language similarity search. For "
            "'what did we just discuss' / 'previous conversation' use "
            "order=recency with room=conversations (optionally channel=main). "
            "Hits carry `id=`, `conversation_id=` and `chunk=`. Pass an id to "
            "`memory(id=…)` to open it; pass a conversation_id back as "
            "`search_meta={\"conversation_id\": …}` with no query to read that "
            "whole conversation in order. A hit "
            "marked LEARNED is a curated memory that happens to live here too; "
            "open it rather than reading the drawer, so its prerequisites come "
            "with it. Zero API cost — runs locally."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Natural-language query for semantic search. Optional when "
                        "order=recency (then filters sessions containing this text)."
                    ),
                },
                "order": {
                    "type": "string",
                    "enum": ["semantic", "recency"],
                    "description": (
                        "semantic (default): rank by meaning. recency: latest archive "
                        "sessions by filed_at DESC — use for prior/previous conversation."
                    ),
                },
                "channel": {
                    "type": "string",
                    "description": (
                        "With order=recency: filter to conversation archives for this "
                        "channel id (e.g. main, worker)."
                    ),
                },
                "wing": {
                    "type": "string",
                    "description": "Optional wing filter (top-level namespace).",
                },
                "room": {
                    "type": "string",
                    "description": "Optional room filter (e.g. conversations for chat history).",
                },
                "hall": {
                    "type": "string",
                    "description": (
                        "Optional hall filter. In room=conversations the halls are "
                        "exactly `user` (what the human said) and `assistant` "
                        "(everything you produced: replies, tool calls, tool results)."
                    ),
                },
                "search_meta": {
                    "type": "object",
                    "description": (
                        "Metadata filter over indexed drawer fields: "
                        "conversation_id, chunk_number, hall, channel, room, wing, "
                        "agent, topic, source_file. Values may be exact "
                        "(`{\"hall\": \"user\"}`), a list for any-of, or a range "
                        "(`{\"chunk_number\": {\"from\": 1, \"to\": 20}}`).\n"
                        "WITHOUT `query` this is a direct ordered fetch, not a "
                        "search — the reliable way to read a whole conversation: "
                        "take the conversation_id from any hit, then request it "
                        "with no query to walk the thread in chunk order. Semantic "
                        "search cannot do this; do not try to find an id by "
                        "putting it in `query`."
                    ),
                },
                "k": {
                    "type": "integer",
                    "description": "Number of results (default 5, max 20; up to 200 for a search_meta fetch).",
                },
            },
            "required": [],
        },
    },
    {
        "name": "palace_wake_up",
        "description": (
            "Regenerate and return the wake-up digest live: the newest learned "
            "drawers (knowledge / procedures / episodes / preferences — never "
            "raw conversation), capped at ~3000 chars. It is the same digest the "
            "system auto-injects into your prompt; call this only when you "
            "suspect that injected copy is stale (e.g. right after filing "
            "something you expect to see there)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "palace_taxonomy",
        "description": (
            "Show how the palace is organized: wings, rooms, drawer counts per room, "
            "and halls (auto-topic labels). Use this before a targeted search when "
            "you want to know which room/hall to filter on. Zero API cost."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "palace_kg_query",
        "description": (
            "Query the knowledge graph. Any combination of subject/predicate/object can be provided; "
            "the others are wildcards. Each given value is a case-insensitive substring match, so a "
            "partial name or predicate is enough. Returns current + expired facts marked with validity status."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "predicate": {"type": "string"},
                "object": {"type": "string"},
            },
            "required": [],
        },
    },
    {
        "name": "palace_kg_timeline",
        "description": (
            "Return the chronological history of all KG facts touching a given entity. "
            "Current + expired, oldest to newest. Use for 'what do we know about X over time?'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "Entity name (appears as subject or object)."},
            },
            "required": ["entity"],
        },
    },
    {
        "name": "google_search",
        "description": (
            "Perform a Google search using the Serper API. "
            "Use this tool ONLY for web search. To READ any result page, use "
            "fetch_url_data. Only use the browser tool when you need to click/"
            "type/navigate rather than just read."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "q": {
                    "type": "string",
                    "description": "The search query (supports site:, inurl:, etc.).",
                },
                "start": {
                    "type": "integer",
                    "description": "Offset for pagination (e.g. 0 for page 1, 10 for page 2).",
                },
                "num": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default 10).",
                }
            },
            "required": ["q"],
        },
    },
    {
        "name": "fetch_url_data",
        "description": (
            "Read a web page by URL — the one way to get a page's contents once "
            "you have its URL (e.g. a google_search result, or a link from "
            "anywhere). Tries a fast stateless extractor first, then loads the "
            "page in the user's own signed-in browser, in a single tab it opens "
            "once and reuses. That fallback is what gets past JS-only pages and "
            "most login walls, so you rarely need the browser tool just to READ.\n\n"
            "mode='text' (default) returns the page's readable text. mode='raw' "
            "returns the full HTML and always goes through the browser.\n\n"
            "If it replies that the browser is offline, ask the user to turn it "
            "on (extension popup, Agent ON) and retry. If it replies [no content] "
            "the page is blocked (login / CAPTCHA / bot check): inspect it with "
            "the browser tool, and only ask the user to unblock it in their live "
            "window when the page is essential — otherwise skip it and move on.\n\n"
            "Use the browser tool directly when you need to click, type, or "
            "navigate — fetch_url_data only reads."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The full http/https URL of the page to read.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["text", "raw"],
                    "description": "'text' (default) for readable page text, 'raw' for full HTML.",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "db_create",
        "description": (
            "Create a new entity document in the operational DB (MongoDB). This is "
            "the ONLY sanctioned way to insert operational state — never write "
            "freestyle pymongo. The entity must be defined in a workflows/*.json "
            "spec (see knowledge/reference/workflows.md). The doc's status is set to the spec's "
            "initial state automatically, history[] is initialized, and the unique "
            "key dedups: if a doc with that key already exists, nothing is created."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "Entity name from a workflow spec (e.g. 'lead')."},
                "doc": {
                    "type": "object",
                    "description": "The document fields, including the entity's unique key. Do not set 'status' — it is forced to the spec's initial state.",
                },
            },
            "required": ["entity", "doc"],
        },
    },
    {
        "name": "db_get",
        "description": (
            "Read one entity document by its unique key (exact lookup, never search). "
            "Use this to check current operational state before acting."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "Entity name from a workflow spec."},
                "key": {"type": "string", "description": "The value of the entity's unique key."},
            },
            "required": ["entity", "key"],
        },
    },
    {
        "name": "db_query",
        "description": (
            "List entity documents matching an optional filter — e.g. 'what is due "
            "now?' or 'all leads in status queued'. Returns up to `limit` lean docs "
            "(history[] omitted). Use db_get for one full document."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "Entity name from a workflow spec."},
                "filter": {"type": "object", "description": "Optional MongoDB filter document (e.g. {\"status\": \"queued\"})."},
                "sort": {"type": "string", "description": "Optional field name to sort by."},
                "descending": {"type": "boolean", "description": "Sort descending instead of ascending (default false)."},
                "limit": {"type": "integer", "description": "Max docs to return (default 50)."},
            },
            "required": ["entity"],
        },
    },
    {
        "name": "db_move_state",
        "description": (
            "Move an entity to a new status. This is the enforced state-machine "
            "transition: an illegal move (not allowed by the spec's transitions) is "
            "REJECTED, and the change is atomic + precondition-guarded so a "
            "concurrent double-move is impossible. Use this to advance a workflow, "
            "to request approval (move into an approval state), and to mark done "
            "(move into a terminal state). Every move appends to history[]."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "Entity name from a workflow spec."},
                "key": {"type": "string", "description": "The value of the entity's unique key."},
                "to": {"type": "string", "description": "The target status (must be a valid, allowed next state)."},
                "note": {"type": "string", "description": "Optional note recorded with the transition in history[]."},
            },
            "required": ["entity", "key", "to"],
        },
    },
    {
        "name": "db_update",
        "description": (
            "Set non-status fields on an entity and record the change in history[]. "
            "To change status, use db_move_state instead (this tool refuses it)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "Entity name from a workflow spec."},
                "key": {"type": "string", "description": "The value of the entity's unique key."},
                "fields": {"type": "object", "description": "Fields to set (must not include 'status')."},
            },
            "required": ["entity", "key", "fields"],
        },
    },
    {
        "name": "db_delete",
        "description": (
            "Delete one entity document by its unique key (like MongoDB's "
            "deleteOne). Irreversible — use for cleaning up test/dummy docs "
            "(e.g. after a workflow self-test), not for normal workflow state "
            "changes (use db_move_state for those)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "Entity name from a workflow spec."},
                "key": {"type": "string", "description": "The value of the entity's unique key."},
            },
            "required": ["entity", "key"],
        },
    },
    {
        "name": "db_add_event",
        "description": (
            "Append an event to an entity's history[] (its timeline) without "
            "changing status — e.g. 'browser confirmed request sent', 'noted reply'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "Entity name from a workflow spec."},
                "key": {"type": "string", "description": "The value of the entity's unique key."},
                "event": {"description": "Event text (string) or a structured event object."},
            },
            "required": ["entity", "key", "event"],
        },
    },
    {
        "name": "db_counter",
        "description": (
            "Read or atomically increment a rate counter in the `counters` "
            "collection, keyed by {name, period} (e.g. name='linkedin_invites', "
            "period='2026-06-30'). Pass incr>0 to bump it (returns the new count); "
            "incr=0 (default) just reads. Pass `cap` to have the result flag whether "
            "the cap is reached. The cap is NOT hard-enforced — the cookbook decides "
            "whether to stop."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Counter name (e.g. 'linkedin_invites')."},
                "period": {"type": "string", "description": "Counter period bucket (e.g. a date or ISO week)."},
                "incr": {"type": "integer", "description": "Amount to increment by (default 0 = read only)."},
                "cap": {"type": "integer", "description": "Optional cap; the result flags whether count >= cap."},
            },
            "required": ["name", "period"],
        },
    },
    {
        "name": "get_recent_recalls",
        "description": (
            "Read recent recall proposals and verified fires for Stage-1/2 audit. "
            "proposed+rejected = Stage-1 matched but Stage-2 vetoed; "
            "proposed+verified / verified = Stage-2 accepted and injected. "
            "Read-only: cue fixes happen in the consolidation passes, which "
            "read this same telemetry. "
            "Not the definition catalog — use get_recall for that."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hours_ago": {
                    "type": "integer",
                    "description": "How many hours of history to fetch (default 24)."
                }
            }
        }
    },
]

# Explorium lead-sourcing tools live in their own module (engine + cache stay
# separate from the tool surface). Advertised alongside the core tools.
TOOL_DEFINITIONS.extend(EXPLORIUM_TOOL_DEFINITIONS)
# Contact (email/phone) enrichment — FullEnrich ⇄ Explorium waterfall, own cache.
TOOL_DEFINITIONS.extend(CONTACT_TOOL_DEFINITIONS)
TOOL_DEFINITIONS.extend(PHONE_TOOL_DEFINITIONS)


# ── Stateless / no-palace mode (forgetting as a feature) ──
# Set GALADRIEL_NO_PALACE=1 (or pass --no-palace to main.py) to run an amnesiac
# session: the memory-palace tools are removed from the advertised tool set and
# any stray palace call short-circuits. Useful for controlled coding sessions
# where you want full command over what the agent knows. Non-palace tools
# (shell / file / memory_log) are unaffected.
# `memory` belongs here for the same reason the palace tools do: it reads stored
# memory, and an amnesiac session that still has one door into the archive is
# not amnesiac. It reaches drawers directly (`memory_access._open_verbatim`) as
# well as the curated store.
_PALACE_TOOL_NAMES = frozenset({
    "palace_search", "palace_wake_up", "palace_taxonomy",
    "palace_kg_query", "palace_kg_timeline",
    "memory", "learn", "propose_memory", "study_file",
})


def palace_disabled() -> bool:
    """True when this session runs in stateless / no-palace mode."""
    return os.environ.get("GALADRIEL_NO_PALACE", "0") == "1"


def _browser_backend() -> str:
    """Active browser driver: 'browser-use' (default) or 'bce'. Only one at a time."""
    backend = os.environ.get("BROWSER_BACKEND", "browser-use").strip().lower()
    if backend in ("browser-use", "browser_use", "browseruse"):
        return "browser-use"
    if backend in ("bce", "browser-command-executor", "browser_command_executor"):
        return "bce"
    return backend


_BROWSER_USE_TOOL_DESCRIPTION = next(
    t["description"] for t in TOOL_DEFINITIONS if t["name"] == "browser"
)


def _browser_tool_description() -> str:
    if _browser_backend() == "bce":
        return (
            "Drive a real Chrome browser through the Browser Command Executor (BCE). "
            "You supply the same arguments as `browser-use` / `bce-cli` and get "
            "text output back. BCE controls the user's Chrome via a loaded "
            "extension + FastAPI server — the active tab in that window is what "
            "you drive. Use `tab list` / `tab switch` to pick tabs.\n\n"
            "**PAIRING — ask before first use:** Each browser is identified by a "
            "pairing code (`XXXX-XXXX`, e.g. `KJ2D-H96M`) shown in the Chrome "
            "extension popup (Agent must be ON). Before your first browser call, "
            "call `browser_devices` with action=list. If it returns a device, "
            "that browser is already paired — use it, passing "
            "`profile=<profile_id>` on browser calls when the id is not `main`. "
            "Only when the list comes back empty, STOP and ask the user for "
            "their code, persist it with `browser_devices` action=connect, then "
            "retry.\n\n"
            "Prerequisites (human setup): MongoDB + BCE server running; extension "
            "Agent ON (Connected).\n\n"
            "Core loop:\n"
            "1. `open <url>` — navigate the active tab.\n"
            "2. `state` — list interactive elements with numbered indices.\n"
            "3. Act by index: `input 0 \"text\"`, `click 2`, `type \"text\"`, "
            "`keys \"Enter\"`, `select 3 \"value\"`.\n"
            "4. Re-run `state` after the page changes.\n"
            "5. `close` is a no-op (extension stays connected).\n\n"
            "SHARED BROWSER — TAB DISCIPLINE (critical): the main channel and the "
            "WORKER drive this SAME browser. Every command hits the ACTIVE tab, and "
            "the other channel can switch tabs between your calls. Browser commands "
            "and the Python-side profile lock are the source of truth; there is no "
            "filesystem tab registry. Rules:\n"
            "1. Start browser work with `tab list`. If there are many tabs open, "
            "CLEAN UP by closing old or unused tabs first (`tab close <i>`). "
            "REUSE an existing idle tab (e.g. blank pages or old work) whenever "
            "possible using its index. DO NOT create a new tab (`tab new <url>`) "
            "unless you are certain all existing tabs are actively being used "
            "by the other channel. Avoid flooding the browser with tabs.\n"
            "2. PASS `tab=<your tab_index>` on EVERY acting call (open/state/"
            "click/input/...). The harness atomically prepends `tab switch <tab>` "
            "inside the browser lock, so the other channel can never flip tabs "
            "under you. A call WITHOUT `tab` acts on whatever tab is active — "
            "only safe for tab-management calls (`tab list`, `tab close`).\n"
            "3. Tab indices SHIFT when tabs open/close. At the start of a work "
            "unit, `tab list` and re-find the intended tab by URL/title. If it is "
            "gone, create a new tab per rule 1.\n"
            "4. NEVER navigate, act on, or close a tab being used by another work "
            "unit. When your task is fully done, YOU MUST close your tab (`tab close <i>`) "
            "unless it is the browser's last tab. Never leave unnecessary tabs open.\n\n"
            "SEEING THE PAGE: `screenshot [path]` captures the active tab and "
            "returns it to you as an ACTUAL IMAGE you can see (vision input), "
            "alongside the text output. Use it whenever text output isn't "
            "enough: `state` is ambiguous or empty, layout/visual verification "
            "matters, the page is canvas/chart/image-heavy, or a click isn't "
            "doing what you expect. Path is optional — omit it and the file "
            "lands under state/screenshots/. Don't screenshot every step "
            "(images cost tokens); reach for it when you genuinely need to "
            "look.\n\n"
            "Other useful commands: `get title`, `get text "
            "<index>`, `get html`, `eval \"<js>\"`, `wait text \"Welcome\"`, "
            "`scroll down`, `back`, `tab list`. Add `--json` for machine-readable "
            "output. Run `--help` for the full surface.\n\n"
            "MULTIPLE BROWSERS: register each with its own pairing code using "
            "`browser_devices`, then pass `profile=<profile_id>` on "
            "every call for that browser.\n\n"
            "BLOCKED PAGES — decide by importance: if you hit a login wall, CAPTCHA, "
            "OTP, or bot-detection AND the content is essential, STOP and ask the "
            "user to take over in the live window, then continue once they're done. "
            "If the block is minor and the value is reachable another way, skip it."
        )
    return _BROWSER_USE_TOOL_DESCRIPTION


def _developer_tool_names() -> set[str]:
    return {t["name"] for t in TOOL_DEFINITIONS if isinstance(t.get("name"), str)}


def visible_tool_definitions() -> list:
    """Tool defs filtered for the current session mode. In no-palace mode the
    palace tools are not advertised at all, so the agent cannot reach for memory
    it has been told to forget.

    Personal tools from `personal-tools/` are merged into the same flat list so
    the LLM sees one tool surface. Developer tool names always win on collision.
    """
    tools = (
        [t for t in TOOL_DEFINITIONS if t["name"] not in _PALACE_TOOL_NAMES]
        if palace_disabled()
        else list(TOOL_DEFINITIONS)
    )
    if not phone_tools_enabled():
        tools = [tool for tool in tools if tool["name"] not in PHONE_TOOL_NAMES]
    from . import personal_tools

    tools = tools + personal_tools.personal_tool_definitions(
        reserved_names=_developer_tool_names()
    )
    if _browser_backend() == "bce":
        patched = []
        for t in tools:
            if t["name"] == "browser":
                schema = dict(t["input_schema"])
                props = dict(schema["properties"])
                props["profile"] = {
                    "type": "string",
                    "description": (
                        "Which browser profile to drive. Omit for `main`. Register "
                        "new profiles with browser_devices after asking the user "
                        "for their extension pairing code."
                    ),
                }
                props["tab"] = {
                    "type": "integer",
                    "description": (
                        "Tab index from the latest `tab list`. "
                        "REQUIRED on every acting call (open/state/click/input/...) "
                        "— the harness atomically switches to this tab first, so "
                        "the other channel (main/worker shares this browser) can't "
                        "hijack your call. Omit ONLY for tab-management calls "
                        "(`tab list`, `tab close`). Re-list at the start of each "
                        "work unit because tab indices can shift."
                    ),
                }
                t = {
                    **t,
                    "description": _browser_tool_description(),
                    "input_schema": {**schema, "properties": props},
                }
            patched.append(t)
        return patched
    return tools


# ─── Tool-result size bounds ─────────────────────────────────────────
#
# A single unbounded tool result (cat of a huge file, a giant page dump) can
# jump the buffer from under the compaction threshold to over the model's
# context window in one hop — past the point where compaction can help,
# straight to an API 400 (see project-agent-kb/kb/context-overflow-400.md).
# Every result is therefore bounded at the one choke point all tools pass
# through: oversized output is spilled to an artifact file and the model gets
# the path plus a token-spaced survey of it (text_survey), so nothing is lost —
# it moves to disk, where survey_file/read_file/run_shell slice it on demand.

# All limits in TOKENS (compaction.CHARS_PER_TOKEN converts at string
# boundaries) — token counts are what every model limit is denominated in.
_INLINE_RESULT_MAX_TOKENS = 7_500  # matches Claude Code's shell-output cap
# read_file bounds itself inside _read_file_sync (a window that fits the same
# 7.5k tokens comes back whole, `full_page=True` raises that to the model's own
# budget, anything larger is surveyed — the source is already a file, so there
# is nothing to spill). Every other tool goes through the one shared limit: no
# per-tool carve-outs, every channel plays by the same rules.
_SELF_BOUNDED_TOOLS = frozenset({"read_file"})

# Tools whose output is sized by the world (a page, a command, a query) rather
# than by a fixed schema advertise `save_to`: write the full output to a file
# and return its survey instead. The mechanism itself is honoured for every
# tool in execute_tool; this set only decides where the schema mentions it.
_SAVEABLE_TOOLS = frozenset({
    "run_shell", "browser", "fetch_url_data", "google_search",
    "read_episode_segment", "memory", "palace_search", "palace_kg_query",
    "palace_kg_timeline", "palace_taxonomy", "db_get", "db_query",
    "phone_shell", "phone_ui_dump", *EXPLORIUM_TOOL_NAMES,
})

_SAVE_TO_SCHEMA = {
    "type": "string",
    "description": (
        "Also write the full output to this file path (created or overwritten) "
        "and return a token-spaced survey of it instead of the output itself. "
        "Use it whenever the output is worth working over on disk — a page, a "
        "log, a dump, a long listing — then survey_file / read_file / run_shell "
        "slice, grep, and clean the file without ever pulling it whole into "
        "the conversation."
    ),
}

for _tool in TOOL_DEFINITIONS:
    if _tool["name"] in _SAVEABLE_TOOLS:
        _tool["input_schema"]["properties"]["save_to"] = _SAVE_TO_SCHEMA


def artifact_dir() -> Path:
    """Directory for spilled oversized content. Lives under the storage root —
    state/ is persistent and agent-readable in managed runtimes, and the local
    storage root (.galadriel-local) is gitignored."""
    root = os.environ.get("GALADRIEL_STORAGE_ROOT")
    base = Path(root) if root else Path(os.getcwd())
    d = base / "state" / "artifacts"
    d.mkdir(parents=True, exist_ok=True)
    return d


# Artifacts are working scratch, not storage: anything durable is either still
# producible (re-run the command) or has been studied into the palace /
# learned. 24h covers "the summary references some/path from yesterday" —
# older than that, refetching is acceptable (shravan, 2026-09-04). The sweep is
# opportunistic (on each new spill) so no scheduler is involved.
_ARTIFACT_TTL_SECONDS = 24 * 3600


def _sweep_stale_artifacts(d: Path) -> None:
    cutoff = time.time() - _ARTIFACT_TTL_SECONDS
    for f in d.glob("*.txt"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            continue


def spill_text(text: str, prefix: str) -> Path:
    """Write oversized text to a timestamped artifact file, return its path."""
    d = artifact_dir()
    _sweep_stale_artifacts(d)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = d / f"{prefix}-{stamp}-{os.urandom(3).hex()}.txt"
    path.write_text(text, encoding="utf-8")
    return path


def bound_tool_result(text: str, tool_name: str) -> str:
    """Cap one tool result's inline size; spill the full text to a file and
    return its survey. Nothing is re-run: the file is sliced on demand."""
    est_tokens = len(text) // CHARS_PER_TOKEN
    if est_tokens <= _INLINE_RESULT_MAX_TOKENS:
        return text
    try:
        path = spill_text(text, tool_name)
    except Exception as e:
        log.warning(f"Could not spill oversized {tool_name} result: {e}")
        return (
            f"[{tool_name} output is ≈{est_tokens:,} tokens and could NOT be "
            "saved to a file — rerun with a narrower command, or pass save_to "
            "with a path you can write. Survey of what came back:]\n"
            f"{survey_text(text, tool_name)}"
        )
    return (
        f"[{tool_name} output is ≈{est_tokens:,} tokens — too large to inline; "
        f"full output saved for ~24h to {path}. Work on the file, do not rerun "
        "the command.]\n"
        f"{survey_file(path)}"
    )


def save_tool_result(text: str, tool_name: str, save_to: str, working_dir=None) -> str:
    """`save_to`: write the full output where the agent asked and hand back
    its survey. Tool errors are returned as they are — an error is not output."""
    if text.startswith(("[tool error]", "[error]", "[blocked]")):
        return text
    from .path_policy import assert_agent_writable

    p = assert_agent_writable(save_to, working_dir=working_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return (
        f"[{tool_name} output — ≈{len(text) // CHARS_PER_TOKEN:,} tokens — "
        f"saved to {p}.]\n{survey_file(p)}"
    )


def _bound_result(result, tool_name: str, save_to: str = None, working_dir=None):
    """Size every text result: saved to `save_to` when asked, else bounded.
    Image blocks (browser screenshots) ride untouched either way."""
    if save_to:
        def size(text):
            return save_tool_result(text, tool_name, save_to, working_dir)
    elif tool_name in _SELF_BOUNDED_TOOLS:
        return result
    else:
        def size(text):
            return bound_tool_result(text, tool_name)
    if isinstance(result, str):
        return size(result)
    if isinstance(result, list):
        for block in result:
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ):
                block["text"] = size(block["text"])
    return result


async def execute_tool(
    name: str,
    inputs: dict,
    memory_manager=None,
    working_dir: str = None,
    experience_manager=None,
    channel_id: str = "unknown",
    model: str = None,
) -> str | list:
    """Execute a tool and return the result. Usually a string; the browser
    tool's `screenshot` returns a list of content blocks (text + image) so the
    captured page reaches the model as vision input. Non-blocking.

    Never raises into the agent loop: missing args / tool bugs come back as a
    `[tool error] …` string so the model can correct and retry.
    """
    if not isinstance(inputs, dict):
        return (
            f"[tool error] {name} expected a dict of arguments, "
            f"got {type(inputs).__name__}. Call again with the schema fields."
        )
    # `save_to` belongs to this boundary, not to any tool: the tool never sees
    # it (a copy, so the recorded call keeps it), and the result is sized here.
    save_to = inputs.get("save_to")
    if save_to is not None:
        inputs = {k: v for k, v in inputs.items() if k != "save_to"}
        save_to = str(save_to).strip() or None
    try:
        result = await _execute_tool_impl(
            name, inputs,
            memory_manager=memory_manager,
            working_dir=working_dir,
            experience_manager=experience_manager,
            channel_id=channel_id,
            model=model,
        )
        # Bounding may spill megabytes to disk (spill_text write + TTL sweep)
        # — off the event loop so one huge result can't stall other channels.
        return await asyncio.to_thread(_bound_result, result, name, save_to, working_dir)
    except KeyError as e:
        missing = e.args[0] if e.args else "?"
        got = sorted(inputs.keys())
        log.warning(
            f"tool {name} missing arg {missing!r} (got keys={got})"
        )
        return (
            f"[tool error] {name} missing required argument: {missing!r}. "
            f"Got keys: {got}. Pass the required fields from the tool schema "
            "and call again."
        )
    except Exception as e:
        log.warning(f"tool {name} raised: {e}", exc_info=True)
        return (
            f"[tool error] {name} failed: {type(e).__name__}: {e}. "
            "Fix the arguments (or try another approach) and call again."
        )
    except SystemExit as e:
        log.warning(f"tool {name} attempted to exit (SystemExit): {e}", exc_info=True)
        return (
            f"[tool error] {name} failed: process attempted to exit via SystemExit: {e}. "
            "The execution was aborted to protect the runtime."
        )


async def _get_recent_recalls(hours_ago: int = 24) -> str:
    """Read recent proposed candidates and verified recall fires."""
    from .db_ops import get_db
    from .recall import PROPOSED_RECALLS_COLLECTION
    db = get_db()
    if db is None:
        return "[error] No DB connection available."
    from datetime import datetime, timedelta, timezone
    since = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    try:
        events = []
        for coll_name in (PROPOSED_RECALLS_COLLECTION, "recall_fires"):
            try:
                docs = await (
                    db[coll_name]
                    .find({"timestamp": {"$gte": since}})
                    .sort("timestamp", -1)
                    .to_list(length=50)
                )
                for d in docs:
                    d = dict(d)
                    d["_log_source"] = coll_name
                    events.append(d)
            except Exception:
                continue
        events.sort(
            key=lambda d: d.get("timestamp") or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        events = events[:80]
        if not events:
            return f"No proposed or verified recalls in the last {hours_ago} hours."

        legend = (
            "LEGEND (two-stage recall): "
            "proposed+rejected = Stage-1 hit, Stage-2 vetoed "
            "(leave if veto correct; if FN strengthen positives). "
            "proposed+verified / verified = Stage-2 accepted and injected "
            "(if FP add matched_chunk to negative_examples via learn_recall). "
            "matched_chunk = Stage-2 input; text_scanned = broader scan window; "
            "segment = which of your own thought/tool_request/tool_output (or "
            "the user's message) the chunk was scanned from.\n"
        )
        res = [legend]
        for n in events:
            r_id = n.get("recall_id", "")
            matched = (n.get("matched_chunk") or "").strip()
            scanned = (n.get("text_scanned") or "").strip()
            pos = n.get("positive_score", n.get("score", 0.0))
            neg = n.get("negative_score")
            neg_s = f"{neg:.3f}" if isinstance(neg, (int, float)) else "n/a"
            channel = n.get("channel_id", "")
            src = n.get("match_source") or "?"
            seg = n.get("segment_source")
            seg_s = f" segment={seg}" if seg else ""
            cue = n.get("lexical_cue")
            cue_s = f" cue={cue!r}" if cue else ""
            if n.get("_log_source") == "recall_fires":
                status = "verified"
            elif n.get("injected") or n.get("judge_verified"):
                status = "proposed+verified"
            else:
                status = "proposed+rejected"
            reason = n.get("judge_reason") or ""
            reason_s = f" judge={reason}" if reason else ""
            lines = [
                f"[{n.get('timestamp')}] status={status} channel={channel} recall={r_id} "
                f"source={src}{seg_s} pos={float(pos):.3f} neg={neg_s}{cue_s}{reason_s}"
            ]
            if matched:
                lines.append(f"matched_chunk: {matched}")
            if scanned and scanned != matched:
                lines.append(f"text_scanned: {scanned}")
            elif scanned and not matched:
                lines.append(f"text_scanned: {scanned}")
            res.append("\n".join(lines))
        return "\n\n".join(res)
    except Exception as e:
        return f"Error fetching recent recalls: {e}"


async def _get_recall(recall_id: str | None = None) -> str:
    """Read recall definitions — one full record or compact catalog."""
    import json
    from .recall import fetch_all_recalls

    try:
        recalls = await fetch_all_recalls()
    except Exception as e:
        return f"[error] Failed to fetch recalls: {e}"

    from .recall import normalize_recall_thresholds

    if recall_id:
        rid = str(recall_id).strip()
        for r in recalls:
            if str(r.get("recall_id", "")) == rid:
                normalize_recall_thresholds(r)
                return json.dumps(r, indent=2, default=str)
        return _unknown_recall_error(rid, recalls)

    catalog = []
    for r in recalls:
        normalize_recall_thresholds(r)
        catalog.append({
            "recall_id": r.get("recall_id"),
            "instruction": r.get("instruction"),
            "activation_condition": r.get("activation_condition"),
            "exclusions": r.get("exclusions"),
            "source": r.get("source"),
            "enabled": r.get("enabled", True),
            "positive_count": len(r.get("positive_examples") or []),
            "negative_count": len(r.get("negative_examples") or []),
            "lexical_count": len(r.get("lexical_cues") or []),
            "positive_threshold": r.get("positive_threshold"),
        })
    return json.dumps(catalog, indent=2, default=str)


def _is_system_recall_id(recall_id: str) -> bool:
    rid = (recall_id or "").strip()
    return rid.startswith("sys_")


async def _purge_recall(recall_id: str) -> str:
    from .db_ops import get_db

    rid = (recall_id or "").strip()
    if not rid:
        return "[error] recall_id is required."
    if _is_system_recall_id(rid):
        return "[error] System recalls cannot be purged."

    db = get_db()
    if db is None:
        return "[error] No DB connection available."
    query = _user_recall_query(rid)
    if isinstance(query, str):
        return query
    try:
        res = await db["recalls"].delete_one(query)
        if res.deleted_count == 0:
            return f"[error] User recall '{rid}' not found."
        from harness.recall import invalidate_semantic_router
        invalidate_semantic_router()
        return f"Purged user recall '{rid}'."
    except Exception as e:
        return f"[error] Failed to purge recall: {e}"


async def _execute_tool_impl(
    name: str,
    inputs: dict,
    memory_manager=None,
    working_dir: str = None,
    experience_manager=None,
    channel_id: str = "unknown",
    model: str = None,
) -> str | list:
    # Stateless mode: refuse palace calls clearly.
    if palace_disabled() and name in _PALACE_TOOL_NAMES:
        return "[stateless session] palace memory is disabled (--no-palace); this tool is unavailable."
    # Personal tools share this same route; developer names are never delegated.
    if name not in _developer_tool_names():
        from . import personal_tools

        personal_result = await personal_tools.execute_personal_tool(
            name,
            inputs,
            working_dir=working_dir,
            reserved_names=_developer_tool_names(),
        )
        if personal_result is not None:
            return personal_result
    if name == "run_shell":
        from .safety import is_db_freestyle, is_git_command
        if is_git_command(inputs["command"]):
            return (
                "[blocked] Source-control commands are unavailable to the Replika. "
                "File changes remain in tenant storage until the user manages source "
                "control outside the Replika."
            )
        if is_db_freestyle(inputs["command"]):
            return (
                "[blocked] Freestyle MongoDB access via run_shell is not allowed. "
                "Use the db_* primitive tools (db_create, db_get, db_query, "
                "db_move_state, db_update, db_delete, db_add_event, db_counter) "
                "instead — they enforce the workflow spec. See knowledge/reference/workflows.md "
                "and knowledge/reference/data.md."
            )
        return await _run_shell(inputs["command"], inputs.get("working_dir", working_dir))
    elif name == "wait":
        return await _wait(
            seconds=inputs.get("seconds"),
            file=inputs.get("file"),
            pattern=inputs.get("pattern"),
            timeout=inputs.get("timeout"),
            poll_interval=inputs.get("poll_interval"),
        )
    elif name == "read_file":
        return await _read_file(
            inputs["path"],
            start=inputs.get("start"),
            end=inputs.get("end"),
            full_page=bool(inputs.get("full_page")),
            model=model,
        )
    elif name == "survey_file":
        return await _survey_file(
            inputs["path"],
            start=inputs.get("start"),
            end=inputs.get("end"),
            probes=inputs.get("probes"),
        )
    elif name == "study_file":
        return await _study_file(
            inputs["path"],
            topic=inputs.get("topic"),
            part=inputs.get("part"),
        )
    elif name == "learn":
        from .learn import learn as _unified_learn
        return await _unified_learn(
            type=inputs.get("type", ""),
            content=inputs.get("content", ""),
            kg_triplets=inputs.get("kg_triplets"),
            kg_invalidate=inputs.get("kg_invalidate"),
            topic=inputs.get("topic"),
            valid_from=inputs.get("valid_from"),
            ended=inputs.get("ended"),
        )
    elif name == "learn_recall":
        return await _learn_recall(
            instruction=inputs.get("instruction"),
            recall_id=inputs.get("recall_id"),
            positive_examples=inputs.get("positive_examples"),
            negative_examples=inputs.get("negative_examples"),
            lexical_cues=inputs.get("lexical_cues"),
            enabled=inputs.get("enabled"),
            positive_threshold=inputs.get("positive_threshold"),
            activation_condition=inputs.get("activation_condition"),
            exclusions=inputs.get("exclusions"),
        )
    elif name == "tune_recall":
        return await _tune_recall(
            recall_id=inputs.get("recall_id", ""),
            applicable=bool(inputs.get("applicable")),
            note=inputs.get("note"),
        )
    elif name == "get_recall":
        return await _get_recall(recall_id=inputs.get("recall_id"))
    elif name == "purge_recall":
        return await _purge_recall(inputs["recall_id"])
    elif name == "memory":
        from . import memory_access
        if (inputs.get("id") or "").strip():
            text, _ = await memory_access.open_memory(inputs["id"])
            return text
        return await memory_access.find(
            inputs.get("query", ""), limit=int(inputs.get("limit") or 5),
        )
    elif name == "propose_memory":
        from . import consolidation
        from .agent import PERIODIC_CONSOLIDATOR_CHANNELS
        # Same tool, two writers: the ephemeral task-end pass and the periodic
        # consolidator channels. Stamping both "task_consolidator" made the
        # provenance field unable to answer which pass produced a memory —
        # and left "periodic_consolidator" a value only queries used.
        source = (
            "periodic_consolidator"
            if channel_id in PERIODIC_CONSOLIDATOR_CHANNELS
            else "task_consolidator"
        )
        # kg_triplets is diagnosed inside commit_candidate; evidence has no
        # validator of its own, and a JSON-string argument would be stored as
        # one opaque blob that no later reader can match an episode id against.
        evidence = inputs.get("evidence_episode_ids")
        if evidence is not None:
            from .tool_args import as_str_list
            evidence, evidence_error = as_str_list(evidence, "evidence_episode_ids")
            if evidence_error:
                return f"[error] {evidence_error}"
        result = await consolidation.commit_candidate(
            type=inputs.get("type", ""),
            content=inputs.get("content", ""),
            kg_triplets=inputs.get("kg_triplets"),
            kg_invalidate=inputs.get("kg_invalidate"),
            topic=inputs.get("topic"),
            valid_from=inputs.get("valid_from"),
            ended=inputs.get("ended"),
            evidence=evidence,
            confidence=inputs.get("confidence"),
            source=source,
            note=inputs.get("note", ""),
            supersedes_memory_id=inputs.get("supersedes_memory_id"),
        )
        return f"[{result['status']}] {result['detail']}"
    elif name == "propose_recall":
        from . import recall_cues
        result = await recall_cues.create_recall_for_memory(
            inputs.get("memory", ""),
            memory_type=inputs.get("type", "semantic"),
            topic=inputs.get("topic"),
        )
        scores = result.get("scores")
        suffix = ""
        if scores:
            suffix = (
                f" Holdout: fired for {_pct(scores.get('recall_rate'))} of "
                f"{scores.get('probes_positive')} phrasings that should match, "
                f"{_pct(scores.get('fp_rate'))} of {scores.get('probes_negative')} "
                "look-alikes that should not."
            )
        return f"[{result['status']}] {result.get('detail', '')}{suffix}"
    elif name == "grade_retrieval":
        from . import consolidation
        return await consolidation.grade_retrieval(
            inputs.get("retrieval_id", ""),
            bool(inputs.get("used")),
            inputs.get("outcome", "neutral"),
            inputs.get("note", ""),
        )
    elif name == "flag_memory":
        from . import consolidation
        return await consolidation.flag_memory(
            inputs.get("memory_key", ""), inputs.get("reason", ""),
        )
    elif name == "read_episode_segment":
        from . import consolidation
        return await consolidation.read_episode_segment(inputs.get("segment_id", ""))
    elif name == "memory_utility_report":
        from . import consolidation
        return await consolidation.memory_utility_report(inputs.get("limit", 15) or 15)
    elif name == "write_file":
        return await _write_file(inputs["path"], inputs["content"])
    elif name == "browser":
        tab = inputs.get("tab")
        return await _run_browser(
            inputs["args"], inputs.get("profile"), int(tab) if tab is not None else None
        )
    elif name == "browser_devices":
        from . import browser_devices

        result = await asyncio.to_thread(
            browser_devices.execute,
            inputs["action"],
            **{key: value for key, value in inputs.items() if key != "action"},
        )
        return json.dumps(result, default=str, ensure_ascii=False)
    elif name == "generate_totp":
        return _generate_totp(inputs["secret_key"])
    elif name == "memory_log":
        if memory_manager:
            memory_manager.append_daily_log(inputs["entry"])
            return "Logged to daily memory."
        return "[tool error] Memory manager not available."
    elif name == "experience_report":
        if experience_manager is None:
            return "[tool error] Experiential state manager not available."
        snapshot = experience_manager.record_event(
            "self_report",
            channel_id,
            details={
                "summary": inputs["summary"],
                "salient_cause": inputs.get("salient_cause", ""),
            },
            proposed_appraisal=inputs["appraisal"],
        )
        return (
            "Metacognitive report recorded separately from authoritative state "
            f"(state version {snapshot['version']})."
        )
    elif name == "palace_search":
        from . import memory_access, palace
        order = inputs.get("order") or "semantic"
        result = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: palace.search(
                query=inputs.get("query") or "",
                wing=inputs.get("wing"),
                room=inputs.get("room"),
                hall=inputs.get("hall"),
                k=inputs.get("k", 5),
                order=order,
                channel=inputs.get("channel"),
                search_meta=inputs.get("search_meta"),
            ),
        )
        # Both corpora live in one store, so a conversation search can surface a
        # learned memory. Say which is which rather than leaving them identical.
        return await memory_access.label_curated(result)
    elif name == "palace_wake_up":
        from . import palace
        return await palace.wake_up()
    elif name == "palace_taxonomy":
        from . import palace
        return await asyncio.get_running_loop().run_in_executor(None, palace.taxonomy)
    elif name == "palace_kg_query":
        from . import palace
        return await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: palace.kg_query(
                subject=inputs.get("subject"),
                predicate=inputs.get("predicate"),
                object=inputs.get("object"),
            ),
        )
    elif name == "palace_kg_timeline":
        from . import palace
        return await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: palace.kg_timeline(entity=inputs["entity"]),
        )
    elif name == "google_search":
        results = await serper_search(
            q=inputs["q"],
            start=inputs.get("start"),
            num=inputs.get("num"),
        )
        return json.dumps(results, indent=2, ensure_ascii=False)
    elif name == "fetch_url_data":
        from .web_fetch import fetch_url_data
        return await fetch_url_data(inputs["url"], inputs.get("mode") or "text")
    elif name == "db_create":
        from . import db_ops
        return await db_ops.create(inputs["entity"], inputs["doc"])
    elif name == "db_get":
        from . import db_ops
        return await db_ops.get(inputs["entity"], inputs["key"])
    elif name == "db_query":
        from . import db_ops
        return await db_ops.query(
            inputs["entity"],
            filter=inputs.get("filter"),
            sort=inputs.get("sort"),
            descending=inputs.get("descending", False),
            limit=inputs.get("limit", 50),
        )
    elif name == "db_move_state":
        from . import db_ops
        return await db_ops.move_state(
            inputs["entity"], inputs["key"], inputs["to"], inputs.get("note"),
        )
    elif name == "db_update":
        from . import db_ops
        return await db_ops.update(inputs["entity"], inputs["key"], inputs["fields"])
    elif name == "db_delete":
        from . import db_ops
        return await db_ops.delete(inputs["entity"], inputs["key"])
    elif name == "db_add_event":
        from . import db_ops
        return await db_ops.add_event(inputs["entity"], inputs["key"], inputs["event"])
    elif name == "db_counter":
        from . import db_ops
        return await db_ops.counter(
            inputs["name"],
            inputs["period"],
            incr=inputs.get("incr", 0),
            cap=inputs.get("cap"),
        )
    elif name == "get_recent_recalls":
        return await _get_recent_recalls(hours_ago=inputs.get("hours_ago", 24))
    elif name in EXPLORIUM_TOOL_NAMES:
        return await execute_explorium_tool(name, inputs)
    elif name in CONTACT_TOOL_NAMES:
        return await execute_contact_tool(name, inputs)
    elif name in PHONE_TOOL_NAMES:
        if not phone_tools_enabled():
            return "[blocked] Phone tools are disabled."
        return await execute_phone_tool(name, inputs)
    else:
        return f"[tool error] Unknown tool: {name}"



# ── Browser (browser-use CLI + persistent Chrome) ─────────────────────
# A real Chrome driven through the browser-use CLI over CDP. By default we
# drive ONE dedicated "main" Chrome with a fixed --user-data-dir +
# --remote-debugging-port, so its profile (cookies, logins) PERSISTS across
# sessions and is isolated from the user's personal Chrome. browser-use runs
# on its own dedicated --session pointed at that Chrome via --cdp-url (added
# only when establishing the daemon). `close` only disconnects the CDP session
# — it never kills our Chrome, so the profile survives between runs.
#
# MULTIPLE PROFILES: additional named profiles are stored in the tenant-scoped
# browser profile repository and managed through browser_devices. Each gets its
# own Chrome + CDP port + browser-use session
# under BROWSER_PROFILES_DIR, so several accounts (e.g. two LinkedIn logins)
# can run fully isolated and concurrently. A per-session asyncio.Lock
# serializes calls *within* one profile (so overlapping agent turns never
# race the same Chrome) while leaving different profiles free to run in
# parallel.
_HEADED_OFF = {"0", "false", "no", "off"}
_CONN_FLAGS = {"--profile", "--cdp-url", "--connect"}
_DEFAULT_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
_DEFAULT_PROFILE = "main"


def _profile_dir() -> str:
    return os.path.expanduser(
        os.environ.get("BROWSER_PROFILE_DIR", "~/.galadriel/browser-profile")
    )


def _cdp_port() -> int:
    return int(os.environ.get("BROWSER_CDP_PORT", "9222"))


def _session_name() -> str:
    return os.environ.get("BROWSER_SESSION", "galadriel")


def _chrome_binary() -> str:
    return os.environ.get("CHROME_BINARY", _DEFAULT_CHROME)


def _browser_use_home() -> str:
    return os.path.expanduser(os.environ.get("BROWSER_USE_HOME", "~/.browser-use"))


def _profiles_base_dir() -> str:
    return os.path.expanduser(
        os.environ.get("BROWSER_PROFILES_DIR", "~/.galadriel/browser-profiles")
    )


def _bce_pairing_required_message(profile: str) -> str:
    return (
        f"[browser pairing required] No pairing code for profile {profile!r}.\n\n"
        "Ask the user for their Chrome extension pairing code (format XXXX-XXXX, "
        "e.g. KJ2D-H96M). They find it in the extension popup — Agent must be ON "
        "(status: Connected).\n\n"
        "Once they provide it, call browser_devices with action=connect, "
        "backend='bce', pairing_code='<code>', and a short purpose, then retry "
        "the browser command.\n\n"
        "Prerequisites: MongoDB + BCE FastAPI server running."
    )


def _no_default_browser_message() -> str:
    """Nothing answers to `main`: either nothing is paired, or several are and
    none has been chosen. Only the first case needs a pairing code."""
    from .browser_devices import list_devices

    try:
        paired = [
            device["profile_id"] for device in list_devices(include_status=False)
        ]
    except Exception:
        paired = []
    if not paired:
        return _bce_pairing_required_message(_DEFAULT_PROFILE)
    return (
        "[no default browser] These browsers are already paired: "
        + ", ".join(paired)
        + ". None is set as main. Ask the user which one to use, then call "
        "browser_devices with action=set_default and that profile_id — or pass "
        "profile=<profile_id> on this command. Do NOT ask for a new pairing "
        "code; these are already connected."
    )


def _resolve_bce_pairing_code(profile: str | None) -> tuple[str, str | None]:
    """Resolve BCE pairing code for a profile name."""
    from .bce_client import BCEError, normalize_pairing_code
    from .browser_devices import resolve

    profile = (profile or _DEFAULT_PROFILE).strip() or _DEFAULT_PROFILE
    row = resolve(profile)
    if not row:
        # Default profile with no Mongo/env config → ask to pair, don't invent
        # an empty "main" device. Named profiles stay "unknown" until connect.
        if profile == _DEFAULT_PROFILE:
            return "", _no_default_browser_message()
        return "", (
            f"[error] unknown browser profile {profile!r}. Register it with "
            "browser_devices, then retry."
        )
    if row.get("backend") != "bce":
        return "", (
            f"[error] profile {profile!r} uses {row.get('backend')!r}, but "
            "BROWSER_BACKEND=bce."
        )
    code = row.get("pairing_code", "")
    if code:
        try:
            return normalize_pairing_code(code), None
        except BCEError as exc:
            return "", f"[error] {exc}"

    return "", _bce_pairing_required_message(profile)


async def _resolve_browser_profile(profile: str | None) -> tuple[dict, str | None]:
    """Resolve a profile name to {profile_dir, cdp_port, session_name}.

    None/""/"main" reproduces the original single-profile behavior exactly
    (same env vars as before), so existing single-account jobs are unaffected.
    Any other id must already be registered through browser_devices.
    """
    profile = (profile or _DEFAULT_PROFILE).strip() or _DEFAULT_PROFILE
    from .browser_devices import resolve

    row = await asyncio.to_thread(resolve, profile)
    if not row:
        return {}, (
            f"[error] unknown browser profile {profile!r}. Register it first with "
            "browser_devices, then retry."
        )
    if row.get("backend") != "browser-use" or "cdp_port" not in row:
        return {}, (
            f"[error] profile {profile!r} uses {row.get('backend')!r}, but "
            "BROWSER_BACKEND=browser-use."
        )
    if profile == _DEFAULT_PROFILE:
        return {
            "profile_dir": _profile_dir(),
            "cdp_port": row["cdp_port"],
            "session_name": _session_name(),
        }, None
    return {
        "profile_dir": os.path.join(_profiles_base_dir(), profile),
        "cdp_port": row["cdp_port"],
        "session_name": f"{_session_name()}-{profile}",
    }, None


_browser_locks: dict[str, asyncio.Lock] = {}


def _lock_for(session_name: str) -> asyncio.Lock:
    """One lock per browser-use session, created lazily. Serializes calls to
    the SAME profile; different profiles get different locks and run freely
    in parallel."""
    lock = _browser_locks.get(session_name)
    if lock is None:
        lock = asyncio.Lock()
        _browser_locks[session_name] = lock
    return lock


def _daemon_state(session: str, cdp_url: str) -> str:
    """Classify the browser-use daemon for our session: 'ours' | 'stale' | 'down'.

    'ours'  — alive and already connected to our CDP url (reuse with --session only)
    'stale' — alive but a different config (must be closed before we reconnect)
    'down'  — no live daemon (we must establish one with --cdp-url)

    We read browser-use's own per-session state file, which stores the RAW cdp_url
    under `config` (the live `ping` reports a resolved ws:// url instead, which is
    why re-passing --cdp-url on every call falsely trips its config-match check).
    """
    home = _browser_use_home()
    if not os.path.exists(os.path.join(home, f"{session}.sock")):
        return "down"
    try:
        with open(os.path.join(home, f"{session}.state.json")) as f:
            state = json.load(f)
    except Exception:
        return "down"
    if state.get("phase") in ("stopped", "shutting_down"):
        return "down"
    pid = state.get("pid")
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return "down"
    cfg = state.get("config") or {}
    return "ours" if cfg.get("cdp_url") == cdp_url else "stale"


def _cdp_ready(port: int) -> bool:
    """True if a CDP endpoint is already responding on the given port."""
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=1
        ) as resp:
            return resp.status == 200
    except Exception:
        return False


async def _ensure_browser_chrome(profile_dir: str, port: int) -> str | None:
    """Launch the dedicated persistent Chrome for this profile if it isn't
    already running.

    Returns None on success, or an error string. Idempotent: if the CDP endpoint
    is already up we reuse it, so the same profile is shared across calls/runs.
    """
    import subprocess

    if await asyncio.to_thread(_cdp_ready, port):
        return None

    binary = _chrome_binary()
    if not os.path.exists(binary):
        return (
            f"[error] Chrome binary not found at {binary!r}. Set CHROME_BINARY "
            "to the path of your Chrome/Chromium executable."
        )

    os.makedirs(profile_dir, exist_ok=True)
    argv = [
        binary,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if os.environ.get("BROWSER_USE_HEADED", "1").strip().lower() in _HEADED_OFF:
        argv.append("--headless=new")

    try:
        subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        return f"[error] Could not launch Chrome: {e}"

    for _ in range(30):  # wait up to ~15s for the CDP endpoint to come up
        if await asyncio.to_thread(_cdp_ready, port):
            return None
        await asyncio.sleep(0.5)
    return "[error] Chrome started but its CDP endpoint never became reachable."


def _with_cdp(argv: list[str], daemon_up: bool, session: str, cdp_url: str) -> list[str]:
    """Route a command to this profile's dedicated Chrome on its own daemon
    session.

    A dedicated `--session` keeps the harness daemon isolated from any other
    `browser-use` daemon (e.g. a manual `default` session, or another
    profile's session). `--cdp-url` points a NEW daemon at our persistent
    Chrome; we add it only when no daemon is up yet, because re-passing it to
    a live daemon trips browser-use's config-match check. Both are global
    flags placed before the subcommand. Skipped if the caller already
    supplied an explicit connection.
    """
    prefix = ["--session", session]
    if not daemon_up and not any(t in _CONN_FLAGS for t in argv):
        prefix += ["--cdp-url", cdp_url]
    return [*prefix, *argv]


def _split_browser_commands(args: str) -> tuple[list[list[str]], str | None]:
    """Split browser args on `&&` (shlex-safe). Returns (commands, error)."""
    import shlex

    try:
        tokens = shlex.split(args)
    except ValueError as e:
        return [], f"[error] Could not parse browser args: {e}"

    commands: list[list[str]] = [[]]
    for tok in tokens:
        if tok == "&&":
            commands.append([])
        else:
            commands[-1].append(tok)
    commands = [c for c in commands if c]
    if not commands:
        return [], "[error] No browser command given."
    return commands, None


# Screenshots taken without an explicit path land here (timestamped files),
# so the harness can read them back and hand the pixels to the model.
_SCREENSHOT_DIR = Path("state/screenshots")
_MAX_SCREENSHOT_BYTES = 5 * 1024 * 1024  # per-image cap, same as chat uploads
# Anthropic computer-use guidance: ~1280px longest side balances fidelity vs tokens.
_MAX_SCREENSHOT_EDGE = 1280


def _ensure_screenshot_paths(commands: list[list[str]]) -> list[str]:
    """Give every `screenshot` command an explicit save path (injecting a
    timestamped one under state/screenshots/ when omitted) and return the
    paths, so the captured image can be read back and attached as vision
    input for the model."""
    paths = []
    for cmd in commands:
        if not cmd or cmd[0] != "screenshot":
            continue
        path = next((a for a in cmd[1:] if not a.startswith("-")), None)
        if path is None:
            _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
            path = str(_SCREENSHOT_DIR / f"{datetime.now():%Y%m%d-%H%M%S-%f}.png")
            cmd.append(path)
        paths.append(path)
    return paths


def _screenshot_media_type(raw: bytes) -> str:
    return "image/jpeg" if raw.startswith(b"\xff\xd8\xff") else "image/png"


def _downscale_screenshot_bytes(raw: bytes, max_edge: int = _MAX_SCREENSHOT_EDGE) -> tuple[bytes, str]:
    """Resize for the vision attachment only; disk file stays full-res.

    Returns (encoded_bytes, media_type). On any failure, returns the original
    bytes with the detected media type.
    """
    media_type = _screenshot_media_type(raw)
    try:
        from io import BytesIO
        from PIL import Image

        img = Image.open(BytesIO(raw))
        img.load()
        w, h = img.size
        longest = max(w, h)
        if longest <= max_edge:
            return raw, media_type
        scale = max_edge / float(longest)
        new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
        img = img.resize(new_size, Image.Resampling.LANCZOS)
        buf = BytesIO()
        if media_type == "image/jpeg":
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img.save(buf, format="JPEG", quality=85, optimize=True)
        else:
            if img.mode not in ("RGB", "RGBA", "L", "P"):
                img = img.convert("RGBA")
            img.save(buf, format="PNG", optimize=True)
            media_type = "image/png"
        return buf.getvalue(), media_type
    except Exception:
        return raw, media_type


def _attach_screenshots(text: str, paths: list[str]) -> str | list:
    """Turn captured screenshot files into content blocks so the model can
    SEE them. Returns the plain text unchanged when nothing was captured.

    Disk files stay full-res; attached vision blocks are downscaled so each
    screenshot costs fewer provider vision tokens.
    """
    blocks = []
    for path in paths:
        try:
            raw = Path(path).read_bytes()
        except OSError:
            continue
        if len(raw) > _MAX_SCREENSHOT_BYTES:
            text += f"\n[screenshot {path} too large to attach ({len(raw) // (1024 * 1024)}MB > 5MB) — saved to disk only]"
            continue
        attach_bytes, media_type = _downscale_screenshot_bytes(raw)
        if len(attach_bytes) > _MAX_SCREENSHOT_BYTES:
            text += f"\n[screenshot {path} too large to attach after downscale — saved to disk only]"
            continue
        blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.b64encode(attach_bytes).decode("utf-8"),
            },
        })
    if not blocks:
        return text
    return [{"type": "text", "text": text}, *blocks]


def _anchor_tab(commands: list[list[str]], tab: int | None) -> tuple[list[list[str]], bool]:
    """Prepend an atomic `tab switch <tab>` so every command in this locked
    batch runs on the caller's own tab. Skipped when the first command is
    itself a `tab` command (tab management calls pick their own target).

    Returns (commands, anchored); `anchored` tells the runner that the first
    output is the harness's own plumbing, not something the caller asked for."""
    if tab is None or (commands and commands[0] and commands[0][0] == "tab"):
        return commands, False
    return [["tab", "switch", str(tab)], *commands], True


async def _run_browser_bce(args: str, profile: str | None = None, tab: int | None = None) -> str | list:
    """Run browser-use-compatible commands via Browser Command Executor."""
    from .bce_cli import run_argv

    commands, err = _split_browser_commands(args)
    if err:
        return err
    commands, anchored = _anchor_tab(commands, tab)
    screenshot_paths = _ensure_screenshot_paths(commands)

    pairing_code, resolve_err = _resolve_bce_pairing_code(profile)
    if resolve_err:
        return resolve_err

    lock_key = f"bce-{profile or _DEFAULT_PROFILE}"
    async with _lock_for(lock_key):
        outputs: list[str] = []
        for position, cmd in enumerate(commands):
            text, ok = await asyncio.to_thread(
                run_argv, cmd, pairing_code=pairing_code, ensure_online=True
            )
            # The anchor's `{"tab_id": ...}` ack is plumbing the caller never
            # asked for, and it corrupts anything that parses the real output.
            # Keep it only when it failed and is the reason we stopped.
            if not (anchored and position == 0 and ok):
                outputs.append(text)
            if not ok:
                break
        out = "\n".join(o for o in outputs if o).strip() or "(no output)"
        return _attach_screenshots(out, screenshot_paths)


async def _run_browser(args: str, profile: str | None = None, tab: int | None = None) -> str | list:
    """Run one or more browser commands against the given profile (or the
    default `main` profile if omitted) and return the combined output.
    `screenshot` commands additionally return the captured image as a content
    block, so the model receives it as vision input.

    Backend is selected by BROWSER_BACKEND (.env): browser-use (default) or bce.
    `args` is everything after the CLI name. Multiple commands may be chained
    with `&&`; each runs as its own invocation, stopping at the first failure.

    `tab` anchors the whole call to that tab index: the harness prepends an
    atomic `tab switch <tab>` inside the profile lock, so concurrent channels
    (main + worker) sharing one browser never act on each other's tabs.
    """
    backend = _browser_backend()
    if backend == "bce":
        return await _run_browser_bce(args, profile, tab)
    if backend != "browser-use":
        return (
            f"[error] Unknown BROWSER_BACKEND={backend!r}. "
            "Use 'browser-use' (default) or 'bce'."
        )

    commands, err = _split_browser_commands(args)
    if err:
        return err.replace("browser command", "browser-use command")
    commands, anchored = _anchor_tab(commands, tab)
    screenshot_paths = _ensure_screenshot_paths(commands)

    cfg, err = await _resolve_browser_profile(profile)
    if err:
        return err

    async with _lock_for(cfg["session_name"]):
        cdp_url = f"http://127.0.0.1:{cfg['cdp_port']}"
        chrome_err = await _ensure_browser_chrome(cfg["profile_dir"], cfg["cdp_port"])
        if chrome_err:
            return chrome_err

        session = cfg["session_name"]
        state = _daemon_state(session, cdp_url)
        if state == "stale":
            await _run_one_browser(["--session", session, "close"])
            state = "down"
        daemon_up = state == "ours"

        outputs: list[str] = []
        for position, cmd in enumerate(commands):
            text, ok = await _run_one_browser(_with_cdp(cmd, daemon_up, session, cdp_url))
            if not (anchored and position == 0 and ok):
                outputs.append(text)
            if not ok:
                break
            daemon_up = True
        out = "\n".join(o for o in outputs if o).strip() or "(no output)"
        return _attach_screenshots(out, screenshot_paths)


async def _run_one_browser(argv: list[str]) -> tuple[str, bool]:
    """Run a single `browser-use` invocation. Returns (output, ok)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "browser-use",
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return "[error] browser-use timed out after 120 seconds.", False
    except FileNotFoundError:
        return (
            "[error] browser-use is not installed. Install it with "
            "`pip install \"browser-use[core]\" && browser-use install`.",
            False,
        )
    except Exception as e:
        return f"[error] {e}", False

    output = ""
    if stdout:
        output += stdout.decode("utf-8", errors="replace")
    if stderr:
        output += f"\n[stderr] {stderr.decode('utf-8', errors='replace')}"
    if proc.returncode != 0:
        output += f"\n[exit code: {proc.returncode}]"
        return output.strip(), False
    return output.strip(), True


def _generate_totp(secret_key: str) -> str:
    """Return the current 6-digit TOTP code for a base32 secret key.

    pyotp is imported lazily so the harness doesn't require it unless LinkedIn
    2FA login is actually used. Whitespace in the secret (LinkedIn often shows
    the key in space-separated groups) is stripped before use.
    """
    cleaned = (secret_key or "").replace(" ", "").strip()
    if not cleaned:
        return "[error] No TOTP secret key provided."
    try:
        import pyotp

        totp = pyotp.TOTP(cleaned, digits=6, interval=30, digest="sha1")
        return totp.now()
    except Exception as e:
        return f"[error] Could not generate TOTP: {e}"


async def _run_shell(command: str, working_dir: str = None) -> str:
    """Execute a shell command asynchronously with a timeout."""
    cwd = working_dir or os.getcwd()
    
    from .path_policy import managed_runtime
    
    if managed_runtime():
        exec_command = command
        # Scrub the environment variables to avoid leaking secrets directly to the shell session.
        # Fargate microVM isolation provides the primary security boundary.
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": cwd,
            "TERM": "xterm-256color",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
    else:
        exec_command = command
        env = None

    try:
        proc = await asyncio.create_subprocess_shell(
            exec_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return "[error] Command timed out after 120 seconds."

        output = ""
        if stdout:
            output += stdout.decode("utf-8", errors="replace")
        if stderr:
            output += f"\n[stderr] {stderr.decode('utf-8', errors='replace')}"
        if proc.returncode != 0:
            output += f"\n[exit code: {proc.returncode}]"
        return output.strip() or "(no output)"
    except Exception as e:
        return f"[error] {e}"


_WAIT_MAX_SECONDS = 1800.0  # 30 min cap, either mode
_WAIT_TAIL_BYTES = 200_000  # only read the tail of large log files


async def _wait(
    seconds: float = None,
    file: str = None,
    pattern: str = None,
    timeout: float = None,
    poll_interval: float = None,
) -> str:
    """Pause execution. Plain sleep by default; with `file` + `pattern`, polls
    the file for a regex match instead — e.g. to wait on a background job
    started via `run_shell` (`nohup cmd > job.log 2>&1 &`) without hitting
    run_shell's own 120s timeout.
    """
    import re
    import time

    if pattern and not file:
        return "[error] `pattern` requires `file` — the file whose contents to poll."

    if not pattern:
        duration = min(max(float(seconds) if seconds is not None else 1.0, 0.0), _WAIT_MAX_SECONDS)
        await asyncio.sleep(duration)
        return f"Slept for {duration:g}s."

    try:
        regex = re.compile(pattern, re.MULTILINE)
    except re.error as e:
        return f"[error] invalid regex pattern: {e}"

    from .path_policy import assert_agent_readable

    try:
        path = assert_agent_readable(file)
    except Exception as e:
        return f"[error] {e}"

    wait_timeout = min(max(float(timeout) if timeout is not None else 300.0, 1.0), _WAIT_MAX_SECONDS)
    interval = max(float(poll_interval) if poll_interval is not None else 3.0, 1.0)

    loop = asyncio.get_running_loop()
    start = time.monotonic()
    last_tail = ""

    while True:
        last_tail = await loop.run_in_executor(None, _tail_file, path)
        match = regex.search(last_tail)
        elapsed = time.monotonic() - start
        if match:
            snippet = last_tail[max(0, match.start() - 200):match.end() + 200]
            return f"[matched] pattern found in {file} after {elapsed:.1f}s.\n...{snippet}..."
        if elapsed >= wait_timeout:
            tail_lines = "\n".join(last_tail.splitlines()[-30:])
            return (
                f"[timeout] pattern not found in {file} after {wait_timeout:g}s.\n"
                f"Last lines:\n{tail_lines or '(empty)'}"
            )
        await asyncio.sleep(min(interval, wait_timeout - elapsed))


def _tail_file(path: Path) -> str:
    """Read up to the last `_WAIT_TAIL_BYTES` bytes of a file. Missing file -> ''."""
    try:
        if not path.exists():
            return ""
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > _WAIT_TAIL_BYTES:
                f.seek(size - _WAIT_TAIL_BYTES)
            data = f.read()
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""


# Hard cap per cue array — router build cost stays bounded, and both stages
# accept on max() over the array so unbounded growth can only loosen matching.
# tune_recall evicts the least-recently-used cue (see recall.evict_lru_cues).
_MAX_EXAMPLES_PER_RECALL = 100


def _pct(value) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def _clean_cue_list(values, *, lexical: bool = False) -> list[str]:
    """Strip, normalize and dedupe one submitted cue array. No cap applied.

    Kept separate from the cap so a caller can tell the two losses apart:
    dropping a blank or a duplicate is housekeeping, while dropping a cue to
    the cap is a decision the caller may want to make itself.
    """
    from harness.recall import normalize_lexical_cue
    out = []
    seen = set()
    if not isinstance(values, list):
        return out
    for v in values:
        if not isinstance(v, str):
            continue
        s = normalize_lexical_cue(v) if lexical else v.strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _normalize_cue_list(values, *, lexical: bool = False, usage: dict | None = None) -> list[str]:
    """Clean, dedupe and cap one submitted cue array.

    Over the cap, drop by least-recently-used rather than by position: a plain
    tail slice discards whichever cues the model happened to list first, which
    on a full-array replace is arbitrary and can delete the very cues that keep
    winning matches.
    """
    from harness.recall import evict_lru_cues
    out = _clean_cue_list(values, lexical=lexical)
    if len(out) <= _MAX_EXAMPLES_PER_RECALL:
        return out
    return evict_lru_cues(out, usage or {}, _MAX_EXAMPLES_PER_RECALL)


def _cue_field_or_error(values, field_name: str, *, lexical: bool = False, usage: dict | None = None):
    """Normalize a provided cue array; error if empty after normalize.

    Returns (normalized_list, None) or (None, error_string).
    Caller must only invoke when ``values is not None``.

    Type problems are reported as type problems. Cue arrays are whole-array
    replacements, so answering a JSON-string argument with "cannot be empty"
    invited the model to resend the same wrong shape with more content in it.
    """
    from .tool_args import as_str_list

    items, type_error = as_str_list(values, field_name)
    if type_error:
        return None, f"[error] {type_error}"
    normalized = _normalize_cue_list(items, lexical=lexical, usage=usage)
    if not normalized:
        return None, (
            f"[error] {field_name} had {len(items)} item(s) but none survived "
            f"normalization — every entry was blank or a duplicate. Pass at "
            f"least one non-empty string, or omit the field."
        )
    return normalized, None


_DROP_REPORT_LIMIT = 3


def _dropped_cue_note(doc: dict, updates: dict, usage: dict) -> str:
    """Warn when a full-array replace dropped cues that had earned usage.

    Cue arrays are whole-array replacements, so a model that paraphrases or
    forgets a cue while resubmitting deletes it, and `sync_cue_usage` then
    prunes that cue's history — a valid write, silently lossy. Nothing else
    notices, so say it in the tool result where the caller can still fix it.
    Only cues with a usage stamp are reported: dropping a cue that never once
    won a match is ordinary pruning, which is what this tool is for.
    """
    from harness.recall import cue_key

    notes = []
    for field in ("positive_examples", "negative_examples", "lexical_cues"):
        if field not in updates:
            continue
        kept = {c for c in updates[field]}
        dropped = [
            c for c in (doc.get(field) or [])
            if isinstance(c, str) and c not in kept and usage.get(cue_key(c))
        ]
        if not dropped:
            continue
        shown = ", ".join(f"{c[:40]!r}" for c in dropped[:_DROP_REPORT_LIMIT])
        more = f" (+{len(dropped) - _DROP_REPORT_LIMIT} more)" if len(dropped) > _DROP_REPORT_LIMIT else ""
        notes.append(f"{len(dropped)} used {field} dropped: {shown}{more}")
    if not notes:
        return ""
    return (
        " [note] " + "; ".join(notes)
        + ". Arrays are full replacements — if that was unintentional, "
        "get_recall and resubmit with them included."
    )


def _capped_cue_note(raw: dict, updates: dict) -> str:
    """Warn when the cap discarded part of what the caller actually submitted.

    `_normalize_cue_list` silently trims to `_MAX_EXAMPLES_PER_RECALL`, so an
    over-long array looks like it was stored whole. The caller is the only one
    who can decide which cues to keep, and it cannot decide what it is not told.
    """
    notes = []
    for field, submitted in raw.items():
        if field not in updates or not isinstance(submitted, list):
            continue
        # Compare against what survived cleaning, not against the raw list:
        # _normalize_cue_list also drops blanks, non-strings and duplicates,
        # and blaming the cap for those sends the caller to trim an array that
        # was never over it.
        cleaned = _clean_cue_list(submitted, lexical=(field == "lexical_cues"))
        over_cap = len(cleaned) - len(updates[field])
        if over_cap > 0:
            notes.append(
                f"{over_cap} {field} beyond the {_MAX_EXAMPLES_PER_RECALL} cap"
            )
    if not notes:
        return ""
    return (
        " [note] dropped " + "; ".join(notes)
        + " (least-recently-used first). Trim the array yourself to choose."
    )


def _unknown_recall_error(rid: str, recalls: list[dict], *, hint: str = "") -> str:
    """Unknown-id error that lists what does exist, with enough text to choose.

    User recall ids are opaque ObjectId hex, so a bare id list is unusable —
    pair each with its instruction head.
    """
    lines = []
    for r in recalls:
        known_id = str(r.get("recall_id") or "").strip()
        if not known_id:
            continue
        head = " ".join(str(r.get("instruction") or "").split())[:60]
        lines.append(f"  {known_id} — {head}")
    listing = "\n".join(sorted(lines)) or "  (none)"
    return (
        f"[error] No recall named '{rid}' exists — do not invent recall_ids.{hint}\n"
        f"Existing recalls:\n{listing}"
    )


def _user_recall_query(rid: str):
    """Build a Mongo query for a user recall id, or an error string."""
    from bson import ObjectId
    from bson.errors import InvalidId

    if len(rid) == 24:
        try:
            return {"_id": ObjectId(rid)}
        except InvalidId:
            return (
                f"[error] Invalid recall_id '{rid}' — "
                f"not a valid 24-char ObjectId hex string."
            )
    return {"recall_id": rid}


def _threshold_field_or_error(value, field: str):
    """Parse a Stage-1 threshold (0–1). Returns (float|None, error|None)."""
    from .recall import recall_positive_threshold

    if value is None:
        return None, None
    try:
        float(value)
    except (TypeError, ValueError):
        return None, f"[error] {field} must be a number between 0 and 1."
    return recall_positive_threshold({"positive_threshold": value}), None


async def _patch_system_recall_cues(
    recall_id: str,
    *,
    positive_examples=None,
    negative_examples=None,
    lexical_cues=None,
    positive_threshold=None,
) -> str:
    """Replace cue arrays / thresholds on a system recall in config/system_recalls.json."""
    import json
    from pathlib import Path
    from harness.recall import (
        invalidate_semantic_router,
        load_cue_usage,
        normalize_recall_thresholds,
        sync_cue_usage,
    )

    usage = await load_cue_usage(recall_id)
    updates = {}
    if positive_examples is not None:
        pos, err = _cue_field_or_error(
            positive_examples, "positive_examples", usage=usage
        )
        if err:
            return err
        updates["positive_examples"] = pos
    if negative_examples is not None:
        neg, err = _cue_field_or_error(
            negative_examples, "negative_examples", usage=usage
        )
        if err:
            return err
        updates["negative_examples"] = neg
    if lexical_cues is not None:
        lex, err = _cue_field_or_error(
            lexical_cues, "lexical_cues", lexical=True, usage=usage
        )
        if err:
            return err
        updates["lexical_cues"] = lex
    if positive_threshold is not None:
        thr, err = _threshold_field_or_error(positive_threshold, "positive_threshold")
        if err:
            return err
        updates["positive_threshold"] = thr

    config_path = Path("config/system_recalls.json")
    if not config_path.exists():
        return f"[error] System recall '{recall_id}' not found (no system_recalls.json)."
    with open(config_path, "r", encoding="utf-8") as f:
        sys_recalls = json.load(f)

    found = None
    before = {}
    for r in sys_recalls:
        if r.get("recall_id") != recall_id:
            continue
        found = r
        before = dict(r)
        r.update(updates)
        normalize_recall_thresholds(r)
        break

    if found is None:
        return f"[error] System recall '{recall_id}' not found."
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(sys_recalls, f, indent=4)
    invalidate_semantic_router()
    await sync_cue_usage(
        recall_id,
        (found.get("positive_examples") or [])
        + (found.get("negative_examples") or [])
        + (found.get("lexical_cues") or []),
    )
    raw = {
        "positive_examples": positive_examples,
        "negative_examples": negative_examples,
        "lexical_cues": lexical_cues,
    }
    return (
        f"Updated cue arrays on system recall '{recall_id}'."
        + _dropped_cue_note(before, updates, usage)
        + _capped_cue_note(raw, updates)
    )


async def _learn_recall(
    instruction: str | None = None,
    recall_id: str | None = None,
    positive_examples=None,
    negative_examples=None,
    lexical_cues=None,
    enabled: bool | None = None,
    positive_threshold=None,
    activation_condition: str | None = None,
    exclusions: str | None = None,
) -> str:
    """Create or patch a recall dict. Agent supplies all fields; no inner LLM."""
    from .db_ops import get_db
    from .recall import (
        DEFAULT_POSITIVE_THRESHOLD,
        invalidate_semantic_router,
        sync_cue_usage,
    )
    from datetime import datetime, timezone

    rid = (recall_id or "").strip() or None
    instr = (instruction or "").strip() if instruction is not None else None
    act = (
        (activation_condition or "").strip()
        if activation_condition is not None
        else None
    )
    excl = (exclusions or "").strip() if exclusions is not None else None

    # ── Patch path ────────────────────────────────────────────────
    if rid:
        is_system = _is_system_recall_id(rid)
        if is_system:
            if instr is not None:
                return "[error] System recall instructions cannot be changed."
            if act is not None or excl is not None:
                return (
                    "[error] System recall activation_condition/exclusions "
                    "cannot be changed via learn_recall."
                )
            if enabled is not None:
                return "[error] System recall enabled flag cannot be changed via learn_recall."
            if (
                positive_examples is None
                and negative_examples is None
                and lexical_cues is None
                and positive_threshold is None
            ):
                return (
                    "[error] Provide at least one cue array or threshold to patch "
                    "on a system recall."
                )
            return await _patch_system_recall_cues(
                rid,
                positive_examples=positive_examples,
                negative_examples=negative_examples,
                lexical_cues=lexical_cues,
                positive_threshold=positive_threshold,
            )

        db = get_db()
        if db is None:
            return "[error] No DB connection available."
        query = _user_recall_query(rid)
        if isinstance(query, str):
            return query
        coll = db["recalls"]
        doc = await coll.find_one(query)
        if not doc:
            return f"[error] User recall '{rid}' not found."

        from .recall import load_cue_usage
        usage = await load_cue_usage(rid)
        updates = {}
        if instr is not None:
            if not instr:
                return "[error] instruction cannot be empty."
            updates["instruction"] = instr
        if act is not None:
            updates["activation_condition"] = act
        if excl is not None:
            updates["exclusions"] = excl
        if positive_examples is not None:
            pos, err = _cue_field_or_error(
                positive_examples, "positive_examples", usage=usage
            )
            if err:
                return err
            updates["positive_examples"] = pos
        if negative_examples is not None:
            neg, err = _cue_field_or_error(
                negative_examples, "negative_examples", usage=usage
            )
            if err:
                return err
            updates["negative_examples"] = neg
        if lexical_cues is not None:
            lex, err = _cue_field_or_error(
                lexical_cues, "lexical_cues", lexical=True, usage=usage
            )
            if err:
                return err
            updates["lexical_cues"] = lex
        if positive_threshold is not None:
            thr, err = _threshold_field_or_error(positive_threshold, "positive_threshold")
            if err:
                return err
            updates["positive_threshold"] = thr
        if enabled is not None:
            updates["enabled"] = bool(enabled)
        if not updates:
            return (
                "[error] No fields to update. Pass instruction and/or cue arrays "
                "and/or thresholds and/or enabled."
            )
        updates["updated_at"] = datetime.now(timezone.utc)
        await coll.update_one(
            query,
            {"$set": updates, "$unset": {"threshold": "", "negative_threshold": ""}},
        )
        invalidate_semantic_router()
        await sync_cue_usage(
            rid,
            list(updates.get("positive_examples", doc.get("positive_examples") or []))
            + list(updates.get("negative_examples", doc.get("negative_examples") or []))
            + list(updates.get("lexical_cues", doc.get("lexical_cues") or [])),
        )
        changed = ", ".join(sorted(k for k in updates if k != "updated_at"))
        raw = {
            "positive_examples": positive_examples,
            "negative_examples": negative_examples,
            "lexical_cues": lexical_cues,
        }
        return (
            f"Updated recall '{rid}' ({changed})."
            + _dropped_cue_note(doc, updates, usage)
            + _capped_cue_note(raw, updates)
        )

    # ── Create path (agent must supply instruction + all cue arrays) ────────
    if not instr:
        return "[error] instruction is required when creating a recall (omit recall_id)."

    # The Stage-2 judge reads only activation_condition and exclusions. Falling
    # back to the instruction hands it an imperative ("I should read X first")
    # where it needs a situation ("The user asks about X"), which is the exact
    # mismatch this field exists to remove.
    if not act:
        return (
            "[error] activation_condition is required when creating a recall. "
            "Describe WHEN it should fire as a situation, e.g. "
            "'The user asks for a report on a repeating schedule.'"
        )

    if positive_examples is None:
        return "[error] Create requires non-empty positive_examples."
    if lexical_cues is None:
        return "[error] Create requires non-empty lexical_cues."

    pos, err = _cue_field_or_error(positive_examples, "positive_examples")
    if err:
        return err
    # Negatives are optional on create: Stage-1 is positive-only and misfires
    # accumulate later via tune_recall.
    neg = []
    if negative_examples is not None:
        neg, err = _cue_field_or_error(negative_examples, "negative_examples")
        if err:
            return err
    lex, err = _cue_field_or_error(lexical_cues, "lexical_cues", lexical=True)
    if err:
        return err
    pos_thr = DEFAULT_POSITIVE_THRESHOLD
    if positive_threshold is not None:
        pos_thr, err = _threshold_field_or_error(positive_threshold, "positive_threshold")
        if err:
            return err
    db = get_db()
    if db is None:
        return "[error] No DB connection available."

    now = datetime.now(timezone.utc)
    doc = {
        "instruction": instr,
        "activation_condition": act,
        "exclusions": excl or "",
        "positive_examples": pos,
        "lexical_cues": lex,
        "negative_examples": neg,
        "positive_threshold": pos_thr,
        "enabled": True if enabled is None else bool(enabled),
        "created_by": "agent",
        "created_at": now,
        "updated_at": now,
    }
    try:
        result = await db["recalls"].insert_one(doc)
        invalidate_semantic_router()
        await sync_cue_usage(str(result.inserted_id), pos + neg + lex)
        return (
            f"Created new recall with ID '{result.inserted_id}' "
            f"({len(pos)} pos, {len(lex)} lexical, {len(neg)} neg, "
            f"pos_thr={pos_thr})."
        )
    except Exception as e:
        return f"[error] Failed to insert new recall: {e}"


async def _tune_recall(recall_id: str, applicable: bool, note: str | None = None) -> str:
    """Feedback on the most recent fire of a recall.

    applicable → fired chunk appended to positive_examples (reinforce);
    not applicable → appended to negative_examples (Stage-2 counter-signal).
    Every call also lands in the `recall_feedback` collection and marks the
    fire doc, so the ambient tuning pass can weigh repeated verdicts.

    A chunk that is a near-duplicate of an existing cue is not stored (it could
    only be dead weight under max() scoring), and when the array is full the
    least-recently-used cue is evicted rather than the oldest-added one.
    """
    from datetime import datetime, timezone

    from .db_ops import get_db
    from .recall import (
        _strip_channel_prefix,
        cue_is_saturated,
        cue_key,
        evict_lru_cues,
        fetch_all_recalls,
        invalidate_semantic_router,
        load_cue_usage,
        sync_cue_usage,
    )

    rid = (recall_id or "").strip()
    if not rid:
        return "[error] recall_id is required."
    db = get_db()
    if db is None:
        return "[error] No DB connection available."

    # Existence first: a made-up id previously reported "no recorded fire",
    # which reads as "real recall, just hasn't fired" and hides the actual
    # mistake. Name the error and show the ids that do exist.
    recalls = await fetch_all_recalls()
    recall = next((r for r in recalls if r.get("recall_id") == rid), None)
    if recall is None:
        return _unknown_recall_error(
            rid,
            recalls,
            hint=" Use the id printed in the `[...]` bracket of that fire's bullet.",
        )

    fire = await db["recall_fires"].find_one({"recall_id": rid}, sort=[("timestamp", -1)])
    if not fire:
        return (
            f"[error] Recall '{rid}' exists but has no recorded fire — nothing to "
            f"tune. tune_recall gives feedback on a fire that already happened."
        )

    chunk = _strip_channel_prefix((fire.get("matched_chunk") or "").strip()).strip()[:300]
    if not chunk:
        return f"[error] Last fire of '{rid}' has no usable matched chunk."

    field = "positive_examples" if applicable else "negative_examples"
    other_field = "negative_examples" if applicable else "positive_examples"
    examples = [e for e in (recall.get(field) or []) if isinstance(e, str) and e.strip()]
    others = [e for e in (recall.get(other_field) or []) if isinstance(e, str) and e.strip()]
    # sync_cue_usage prunes every cue missing from the set it is given, so the
    # untouched arrays have to ride along or their stamps are deleted here.
    lexical = [e for e in (recall.get("lexical_cues") or []) if isinstance(e, str) and e.strip()]
    duplicate = any(e.strip().casefold() == chunk.casefold() for e in examples)
    saturated, nearest = (False, None) if duplicate else cue_is_saturated(chunk, examples)
    if not duplicate and not saturated:
        usage = await load_cue_usage(rid)
        usage[cue_key(chunk)] = datetime.now(timezone.utc)  # insertion counts as a use
        examples = evict_lru_cues(examples + [chunk], usage, _MAX_EXAMPLES_PER_RECALL)
        if _is_system_recall_id(rid):
            result = await _patch_system_recall_cues(rid, **{field: examples})
            if result.startswith("[error]"):
                return result
        else:
            query = _user_recall_query(rid)
            if isinstance(query, str):
                return query
            updated = await db["recalls"].update_one(
                query,
                {"$set": {field: examples, "updated_at": datetime.now(timezone.utc)}},
            )
            if updated.matched_count == 0:
                return f"[error] User recall '{rid}' not found."
            if applicable:
                # Positives feed the router; negatives are Stage-2-only.
                invalidate_semantic_router()
        await sync_cue_usage(rid, examples + others + lexical, touch=[chunk])

    verdict = "applicable" if applicable else "misfire"
    try:
        now = datetime.now(timezone.utc)
        await db["recall_feedback"].insert_one({
            "recall_id": rid,
            "applicable": bool(applicable),
            "note": (note or "").strip()[:300],
            "chunk": chunk,
            "fire_id": fire.get("_id"),
            "channel_id": fire.get("channel_id"),
            "timestamp": now,
        })
        await db["recall_fires"].update_one(
            {"_id": fire["_id"]},
            {"$set": {"feedback": verdict, "feedback_note": (note or "").strip()[:300],
                      "feedback_at": now}},
        )
    except Exception as e:
        log.warning(f"tune_recall: feedback trail write failed: {e}")

    if duplicate:
        return f"Recorded {verdict} feedback for '{rid}' (chunk already in {field})."
    if saturated:
        near = f" (~{nearest:.2f} cosine)" if nearest is not None else ""
        return (
            f"Recorded {verdict} feedback for '{rid}' — chunk not stored: already "
            f"covered by an existing {field} entry{near}."
        )
    return f"Recorded {verdict} feedback for '{rid}' — chunk added to {field} ({len(examples)} total)."


# One study call chunks and embeds this many tokens (locally, FastEmbed — no
# API cost, but real compute: ~400 chunks ≈ well under a minute, in the same
# ballpark as a compaction mine). Bigger files are studied part by part so a
# single call never blocks a turn for minutes.
_STUDY_PART_TOKENS = 200_000


def _slugify(text: str) -> str:
    slug = "".join(c if c.isalnum() else "-" for c in (text or "").lower())
    return "-".join(p for p in slug.split("-") if p)[:60] or "document"


def _looks_binary(text: str) -> bool:
    """True for content that decoded as garbage (binary / wrong encoding)."""
    if not text:
        return False
    if "\x00" in text:
        return True
    return text.count("�") / len(text) > 0.05


async def _study_file(path: str, topic: str = None, part=None) -> str:
    """Chunk one part of a file into palace room=sources (see palace.study_document)."""
    from . import palace
    from .path_policy import assert_agent_readable

    p = assert_agent_readable(path)
    if not p.exists():
        return f"[error] File not found: {path}"
    part = max(1, int(part or 1))
    stride_chars = _STUDY_PART_TOKENS * CHARS_PER_TOKEN
    size = p.stat().st_size
    total_parts = max(1, (size + stride_chars - 1) // stride_chars)
    with p.open("rb") as f:
        f.seek((part - 1) * stride_chars)
        blob = f.read(stride_chars).decode("utf-8", errors="replace")
    if _looks_binary(blob):
        return (
            f"[study] {p} does not decode as readable text (binary or "
            "non-UTF-8) — studying it would file garbage permanently. Convert "
            "it first (e.g. pdftotext, iconv) and study the converted file."
        )
    hall = _slugify(topic or p.stem)
    # Empty parts still go through: study_text runs the range purges either
    # way, which is how a part that no longer exists (the file shrank) gets
    # its stale chunks cleared instead of stranded.
    count = await palace.study_document(
        blob, source_path=str(p), hall=hall, part=part, total_parts=total_parts,
    )
    if not blob.strip():
        return (
            f"[study] part {part} of {p} is beyond the file's current end "
            f"(≈{size // CHARS_PER_TOKEN:,} tokens, {total_parts} part(s)) — "
            "cleared any stale chunks for that range; nothing new filed."
        )
    remaining = (
        f" Parts {part + 1}–{total_parts} remain — study them on demand with "
        f"part={part + 1}."
        if part < total_parts else " The whole file is studied."
    )
    return (
        f"[study] Filed {count} chunks (≈{len(blob) // CHARS_PER_TOKEN:,} "
        f"tokens, part {part}/{total_parts}) of {p} into palace "
        f"room=sources hall={hall}.{remaining} Retrieve any part by meaning: "
        f"palace_search(query=…, search_meta={{'room': 'sources', "
        f"'source_file': '{p}'}}); read in order via chunk_number ranges. "
        "The palace copy is permanent. Durable rules/facts from it still go "
        "through `learn`."
    )


# full_page reads still have to fit: the model's window minus its output
# budget, with 20% slack for the system prompt and the rest of the
# conversation (whatever older history doesn't fit gets compacted away by the
# pre-send gate — that trade is the caller's to make by passing full_page).
def _full_page_token_budget(model: str | None) -> int:
    from . import model_catalog

    entry = model_catalog.get(model) if model else None
    context = getattr(entry, "context", None) or 200_000
    max_output = getattr(entry, "max_output", None) or 8_192
    return max(_INLINE_RESULT_MAX_TOKENS, int((context - max_output) * 0.8))


async def _read_file(path: str, start=None, end=None, full_page: bool = False,
                     model: str = None) -> str:
    """Read a file's contents without blocking the event loop."""
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            None, _read_file_sync, path, start, end, full_page, model,
        )
    except Exception as e:
        return f"[error] {e}"


def _read_file_sync(path: str, start=None, end=None, full_page: bool = False,
                    model: str = None) -> str:
    """Synchronous file read, run in executor. Bounds itself (execute_tool's
    shared bound skips read_file): the requested window — the whole file by
    default, `start`–`end` in tokens otherwise — comes back whole when it fits
    the same 7.5k tokens every tool gets (`full_page` raises that to the
    model's own context budget), and as a survey of that range when it does
    not. No refusal, no spill — the source is already a file."""
    from .path_policy import assert_agent_readable

    p = assert_agent_readable(path)
    if not p.exists():
        return f"[error] File not found: {path}"
    size = p.stat().st_size
    limit_tokens = (
        _full_page_token_budget(model) if full_page else _INLINE_RESULT_MAX_TOKENS
    )
    # Token offsets seek as bytes: bytes ≈ chars for the text this reads.
    start_b = max(0, min(int(start or 0) * CHARS_PER_TOKEN, size))
    end_b = size if end is None else max(start_b, min(int(end) * CHARS_PER_TOKEN, size))
    if end_b - start_b <= limit_tokens * CHARS_PER_TOKEN:
        with p.open("rb") as f:
            f.seek(start_b)
            return f.read(end_b - start_b).decode("utf-8", errors="replace")
    hint = (
        "narrow start/end, pass full_page=true, or slice by line with run_shell"
        if not full_page
        else "that is this model's context budget — narrow start/end"
    )
    return (
        f"[read_file: the requested range is ≈{(end_b - start_b) // CHARS_PER_TOKEN:,} "
        f"tokens, over the ≈{limit_tokens:,}-token budget — {hint}. Survey of "
        f"the range instead:]\n{survey_file(p, start=start_b // CHARS_PER_TOKEN, end=end_b // CHARS_PER_TOKEN)}"
    )


async def _survey_file(path: str, start=None, end=None, probes=None) -> str:
    """survey_file tool: text_survey over an agent-readable path, off the loop."""
    from .path_policy import assert_agent_readable

    p = assert_agent_readable(path)
    if not p.exists():
        return f"[error] File not found: {path}"
    return await asyncio.to_thread(survey_file, p, start or 0, end, probes)


async def _write_file(path: str, content: str) -> str:
    """Write content to a file without blocking the event loop."""
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, _write_file_sync, path, content)
    except Exception as e:
        return f"[error] {e}"


def _write_file_sync(path: str, content: str) -> str:
    """Synchronous file write, run in executor."""
    from .path_policy import assert_agent_writable

    p = assert_agent_writable(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"Written {len(content)} bytes to {path}"

async def serper_search(q: str, start: int = None, num: int = None) -> list[dict]:
    import httpx
    import logging
    from tenacity import (
        retry,
        stop_after_attempt,
        wait_exponential,
        retry_if_exception_type,
    )

    logger = logging.getLogger(__name__)
    url = "https://google.serper.dev/search"
    api_key = os.environ.get("SERPER_API_KEY")
    if not api_key:
        logger.error("SERPER_API_KEY environment variable is not set.")
        return [{"error": "SERPER_API_KEY not configured"}]

    payload = {"q": q}
    if start is not None:
        payload["start"] = start
    if num is not None:
        payload["num"] = num
    payload_json = json.dumps(payload)
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=10, max=120),
        retry=retry_if_exception_type(
            (httpx.HTTPStatusError, httpx.RequestError, httpx.TimeoutException)
        ),
        reraise=True,
    )
    async def _do_search():
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, headers=headers, data=payload_json)
            if response.status_code == 429:
                logger.warning(
                    f"Rate limited by Serper API. Status: {response.status_code}"
                )
                response.raise_for_status()
            response.raise_for_status()
            return response.json().get("organic", [])

    try:
        return await _do_search()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            logger.warning(
                f"Serper API rate limit hit, will retry. Status: {e.response.status_code}"
            )
        raise e
    except httpx.RequestError as e:
        logger.error(f"Serper API request error: {e}")
        raise e
    except httpx.TimeoutException as e:
        logger.error(f"Serper API request timeout: {e}")
        raise e

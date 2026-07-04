"""Tool definitions and execution for the agent."""

import asyncio
import json
import os
from pathlib import Path

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

TOOL_DEFINITIONS = [
    {
        "name": "run_shell",
        "description": (
            "Execute a shell command on the EC2 instance. "
            "Use for AWS CLI, git, file operations, system commands, python scripts, etc. "
            "Commands run in the project working directory."
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
        "name": "read_file",
        "description": "Read the contents of a file from the local filesystem.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or relative path to the file.",
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
            "at once (e.g. a second LinkedIn login), register a named profile in "
            "state/browser_profiles.md (read that file for the exact format/"
            "procedure), then pass `profile=<id>` on every call for that account — "
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
            "Other useful commands: `screenshot [path]`, `get title`, `get text "
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
                        "prior behavior). Pass a profile_id registered in "
                        "state/browser_profiles.md to drive a separate, fully "
                        "isolated Chrome + account instead — e.g. a second "
                        "LinkedIn login. Different profiles can run "
                        "concurrently. To register a new one: read_file "
                        "state/browser_profiles.md for the exact procedure, "
                        "then write_file the new row yourself — there is no "
                        "dedicated tool for this, it's a plain file."
                    ),
                },
            },
            "required": ["args"],
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
        "description": "Append an entry to today's memory log. Use this to persist important information across sessions.",
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
        "name": "palace_search",
        "description": (
            "Search the verbatim memory palace (MemPalace). "
            "Default (order=semantic): natural-language similarity search. "
            "For 'what did we just discuss' / 'previous conversation' use "
            "order=recency with room=conversations (optionally channel=main). "
            "Zero API cost — runs locally against ChromaDB."
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
                    "description": "Optional hall filter (decisions, problems, milestones, etc).",
                },
                "k": {
                    "type": "integer",
                    "description": "Number of results (default 5, max 20).",
                },
            },
            "required": [],
        },
    },
    {
        "name": "palace_add_drawer",
        "description": (
            "File a verbatim fact or memory directly into the palace *right now*. "
            "Unlike memory_log (which only writes to today's daily log and isn't "
            "palace-searchable until the next mine), this tool makes the content "
            "immediately retrievable via palace_search. "
            "Use sparingly — only for facts worth remembering across sessions "
            "(decisions, discoveries, one-off configuration, durable context). "
            "Everyday chatter belongs in the daily log."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The verbatim content to file. Write it the way you want to read it back.",
                },
                "topic": {
                    "type": "string",
                    "description": "Optional short topic hint (used for the filename slug).",
                },
                "room": {
                    "type": "string",
                    "description": (
                        "Optional room to route this drawer to (the relational "
                        "layer). Use a room name to group related drawers — e.g. "
                        "'dialogue' for notable exchanges, 'open_questions' for "
                        "unresolved threads. Omit for durable facts (defaults to "
                        "the palace's general room)."
                    ),
                },
                "wing": {
                    "type": "string",
                    "description": "Wing to file under (default 'agent').",
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "palace_wake_up",
        "description": (
            "Fetch a fresh L0+L1 wake-up snapshot from the palace (~800 tokens). "
            "Different from the auto-injected wake-up in your system prompt: this "
            "runs live, optionally filtered to a single wing, so you can pull a "
            "targeted palace overview mid-conversation. Useful when you suspect "
            "the auto-injected wake-up is stale, or when you need a focused "
            "recall of a specific project/person."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "wing": {
                    "type": "string",
                    "description": "Optional wing filter (project or person). Omit for a global snapshot.",
                },
            },
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
        "name": "palace_kg_add",
        "description": (
            "File a fact into the knowledge graph as a (subject, predicate, object) "
            "triple with a validity window. Use for durable relational facts: "
            "`user — prefers — direct commits`, "
            "`service — runs_on — ARM64 t4g`, `project — shipped_at — 2026-04-20`. "
            "Facts can be superseded later via palace_kg_invalidate. "
            "Prefer this over palace_add_drawer for structured relations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "The entity the fact is about."},
                "predicate": {"type": "string", "description": "The relationship verb (is/works_on/prefers/runs_on/ships_as/etc)."},
                "object": {"type": "string", "description": "What the subject relates to."},
                "valid_from": {"type": "string", "description": "Optional ISO date (YYYY-MM-DD) when the fact became true. Defaults to today."},
            },
            "required": ["subject", "predicate", "object"],
        },
    },
    {
        "name": "palace_kg_query",
        "description": (
            "Query the knowledge graph. Any combination of subject/predicate/object can be provided; "
            "the others are wildcards. Returns current + expired facts marked with validity status."
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
        "name": "palace_kg_invalidate",
        "description": (
            "Mark a KG fact as no longer valid (sets valid_to). Use when a fact changes: "
            "first invalidate the old triple, then palace_kg_add the new one. Preserves history."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "predicate": {"type": "string"},
                "object": {"type": "string"},
                "ended": {"type": "string", "description": "Optional ISO date when the fact stopped being true. Defaults to today."},
            },
            "required": ["subject", "predicate", "object"],
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
        "name": "palace_diary_write",
        "description": (
            "Write a diary entry — your personal journal. Use at end-of-session, goodnight, "
            "or any time something is worth remembering as a reflection (not as a raw fact). "
            "Different from palace_add_drawer (verbatim durable fact) and memory_log "
            "(append to today's daily log). Diary entries are your curated thoughts."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entry": {"type": "string", "description": "The diary entry text. Write it as you want to read it back."},
                "topic": {"type": "string", "description": "Optional topic tag (default 'general')."},
            },
            "required": ["entry"],
        },
    },
    {
        "name": "palace_diary_read",
        "description": (
            "Read the most recent N diary entries — your own reflections across past sessions. "
            "Useful on wake-up if the auto-injected L1 doesn't cover what you need."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "last_n": {"type": "integer", "description": "Number of entries to return (default 10, max 50)."},
            },
            "required": [],
        },
    },
    {
        "name": "google_search",
        "description": (
            "Perform a Google search using the Serper API. "
            "Use this tool ONLY for web search. To READ any result page, use "
            "fetch_url_data first (fast, no browser). Only use the browser tool "
            "when fetch_url_data can't reach the page, or when you need to click/"
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
            "Read a web page by URL — the FAST, CHEAP, FIRST-CHOICE way to get a "
            "page's text once you have its URL (e.g. a google_search result, or a "
            "link from anywhere). Runs a waterfall of lightweight extractor APIs; "
            "NO browser tab is opened, so it's far faster than the cloud browser.\n\n"
            "WATERFALL — follow this order whenever you just need to READ a page:\n"
            "1. Call fetch_url_data first. If it returns content, you're done — do "
            "NOT open the browser.\n"
            "2. If it returns [no content] (login wall, bot detection, JS-only "
            "page, or extraction failure), open the browser tool "
            "(`open <url>` then `eval \"document.body.innerText\"` or `state`).\n"
            "3. If the browser is ALSO blocked (login / CAPTCHA / OTP / "
            "bot-detection) AND the page is essential to the task, STOP and ask "
            "the user to take over in the live window to unblock it, then continue. "
            "If the page is not essential, skip it and move on.\n\n"
            "Use the browser directly (not this tool) when you need to "
            "click, type, or navigate — fetch_url_data only reads."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The full http/https URL of the page to read.",
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
            "spec (see config/WORKFLOWS.md). The doc's status is set to the spec's "
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
            "now?' or 'all leads in status queued'. Returns up to `limit` docs."
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
]

# Explorium lead-sourcing tools live in their own module (engine + cache stay
# separate from the tool surface). Advertised alongside the core tools.
TOOL_DEFINITIONS.extend(EXPLORIUM_TOOL_DEFINITIONS)
# Contact (email/phone) enrichment — FullEnrich ⇄ Explorium waterfall, own cache.
TOOL_DEFINITIONS.extend(CONTACT_TOOL_DEFINITIONS)


# ── Stateless / no-palace mode (forgetting as a feature) ──
# Set GALADRIEL_NO_PALACE=1 (or pass --no-palace to main.py) to run an amnesiac
# session: the memory-palace tools are removed from the advertised tool set and
# any stray palace call short-circuits. Useful for controlled coding sessions
# where you want full command over what the agent knows. Non-palace tools
# (shell / file / memory_log) are unaffected.
_PALACE_TOOL_NAMES = frozenset({
    "palace_search", "palace_add_drawer", "palace_wake_up", "palace_taxonomy",
    "palace_kg_add", "palace_kg_query", "palace_kg_invalidate",
    "palace_kg_timeline", "palace_diary_write", "palace_diary_read",
})


def palace_disabled() -> bool:
    """True when this session runs in stateless / no-palace mode."""
    return os.environ.get("GALADRIEL_NO_PALACE", "0") == "1"


def visible_tool_definitions() -> list:
    """Tool defs filtered for the current session mode. In no-palace mode the
    palace tools are not advertised at all, so the agent cannot reach for memory
    it has been told to forget."""
    if palace_disabled():
        return [t for t in TOOL_DEFINITIONS if t["name"] not in _PALACE_TOOL_NAMES]
    return list(TOOL_DEFINITIONS)


async def execute_tool(name: str, inputs: dict, memory_manager=None, working_dir: str = None) -> str:
    """Execute a tool and return the result as a string. All operations are non-blocking."""
    # Stateless mode: refuse palace calls clearly.
    if palace_disabled() and name in _PALACE_TOOL_NAMES:
        return "[stateless session] palace memory is disabled (--no-palace); this tool is unavailable."
    if name == "run_shell":
        from .safety import is_db_freestyle
        if is_db_freestyle(inputs["command"]):
            return (
                "[blocked] Freestyle MongoDB access via run_shell is not allowed. "
                "Use the db_* primitive tools (db_create, db_get, db_query, "
                "db_move_state, db_update, db_delete, db_add_event, db_counter) "
                "instead — they enforce the workflow spec. See config/WORKFLOWS.md "
                "and config/DATA.md."
            )
        return await _run_shell(inputs["command"], inputs.get("working_dir", working_dir))
    elif name == "read_file":
        return await _read_file(inputs["path"])
    elif name == "write_file":
        return await _write_file(inputs["path"], inputs["content"])
    elif name == "browser":
        return await _run_browser(inputs["args"], inputs.get("profile"))
    elif name == "generate_totp":
        return _generate_totp(inputs["secret_key"])
    elif name == "memory_log":
        if memory_manager:
            memory_manager.append_daily_log(inputs["entry"])
            return "Logged to daily memory."
        return "Memory manager not available."
    elif name == "palace_search":
        from . import palace
        order = inputs.get("order") or "semantic"
        return await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: palace.search(
                query=inputs.get("query") or "",
                wing=inputs.get("wing"),
                room=inputs.get("room"),
                hall=inputs.get("hall"),
                k=inputs.get("k", 5),
                order=order,
                channel=inputs.get("channel"),
            ),
        )
    elif name == "palace_add_drawer":
        from . import palace
        return await palace.add_drawer(
            content=inputs["content"],
            topic=inputs.get("topic"),
            wing=inputs.get("wing", "agent"),
            room=inputs.get("room"),
        )
    elif name == "palace_wake_up":
        from . import palace
        return await palace.wake_up(wing=inputs.get("wing"))
    elif name == "palace_taxonomy":
        from . import palace
        return await asyncio.get_running_loop().run_in_executor(None, palace.taxonomy)
    elif name == "palace_kg_add":
        from . import palace
        return await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: palace.kg_add(
                subject=inputs["subject"],
                predicate=inputs["predicate"],
                object=inputs["object"],
                valid_from=inputs.get("valid_from"),
            ),
        )
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
    elif name == "palace_kg_invalidate":
        from . import palace
        return await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: palace.kg_invalidate(
                subject=inputs["subject"],
                predicate=inputs["predicate"],
                object=inputs["object"],
                ended=inputs.get("ended"),
            ),
        )
    elif name == "palace_kg_timeline":
        from . import palace
        return await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: palace.kg_timeline(entity=inputs["entity"]),
        )
    elif name == "palace_diary_write":
        from . import palace
        return await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: palace.diary_write(
                entry=inputs["entry"],
                topic=inputs.get("topic", "general"),
            ),
        )
    elif name == "palace_diary_read":
        from . import palace
        return await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: palace.diary_read(last_n=inputs.get("last_n", 10)),
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
        content = await fetch_url_data(inputs["url"])
        if content:
            return content
        return (
            "[no content] Fast extraction could not read this URL (possible login "
            "wall, bot detection, or JS-only page). Read it with the browser tool "
            "(`open <url>` then `eval \"document.body.innerText\"` or `state`). If "
            "the browser is also blocked and the page is essential, ask the user to "
            "unblock it in the live window; otherwise skip it."
        )
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
    elif name in EXPLORIUM_TOOL_NAMES:
        return await execute_explorium_tool(name, inputs)
    elif name in CONTACT_TOOL_NAMES:
        return await execute_contact_tool(name, inputs)
    else:
        return f"Unknown tool: {name}"



# ── Browser (browser-use CLI + persistent Chrome) ─────────────────────
# A real Chrome driven through the browser-use CLI over CDP. By default we
# drive ONE dedicated "main" Chrome with a fixed --user-data-dir +
# --remote-debugging-port, so its profile (cookies, logins) PERSISTS across
# sessions and is isolated from the user's personal Chrome. browser-use runs
# on its own dedicated --session pointed at that Chrome via --cdp-url (added
# only when establishing the daemon). `close` only disconnects the CDP session
# — it never kills our Chrome, so the profile survives between runs.
#
# MULTIPLE PROFILES: additional named profiles, registered by the agent as
# plain rows (profile_id, cdp_port, reason) in state/browser_profiles.md via
# read_file/write_file — no dedicated tool, no DB, that file's own header has
# the procedure. Each gets its own Chrome + CDP port + browser-use session
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


def _valid_profile_id(profile_id: str) -> bool:
    import re

    return bool(re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", profile_id or ""))


# Named profiles are registered in a plain markdown table (state/browser_profiles.md),
# not the DB — this is harness-internal config (a name + why it exists), not
# operational/business state, so it doesn't belong behind db_ops/workflows or in
# the Tower UI. profile_id and reason are free text; cdp_port is the one
# mechanically-required field (assigned once at creation, kept stable).
_PROFILES_MD_PATH = Path("state/browser_profiles.md")


def _read_browser_profiles() -> list[dict]:
    """Parse {profile_id, cdp_port, reason} rows out of the markdown table.
    Any line that isn't a well-formed 3-cell data row (header, separator,
    prose, a hand-edit typo) is silently skipped rather than erroring.
    """
    if not _PROFILES_MD_PATH.exists():
        return []
    rows = []
    for line in _PROFILES_MD_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not (line.startswith("|") and line.endswith("|")):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) != 3 or not _valid_profile_id(cells[0]):
            continue
        try:
            port = int(cells[1])
        except ValueError:
            continue
        rows.append({"profile_id": cells[0], "cdp_port": port, "reason": cells[2]})
    return rows


async def _resolve_browser_profile(profile: str | None) -> tuple[dict, str | None]:
    """Resolve a profile name to {profile_dir, cdp_port, session_name}.

    None/""/"main" reproduces the original single-profile behavior exactly
    (same env vars as before), so existing single-account jobs are unaffected.
    Any other id must already be a row in state/browser_profiles.md — the
    agent registers new profiles itself with read_file/write_file, per that
    file's own instructions; there is no dedicated tool for it.
    """
    profile = (profile or _DEFAULT_PROFILE).strip() or _DEFAULT_PROFILE
    if profile == _DEFAULT_PROFILE:
        return {
            "profile_dir": _profile_dir(),
            "cdp_port": _cdp_port(),
            "session_name": _session_name(),
        }, None

    rows = await asyncio.to_thread(_read_browser_profiles)
    row = next((r for r in rows if r["profile_id"] == profile), None)
    if not row:
        return {}, (
            f"[error] unknown browser profile {profile!r}. Register it first as "
            f"a row in state/browser_profiles.md (read that file for the exact "
            "format/procedure), then retry."
        )
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


async def _run_browser(args: str, profile: str | None = None) -> str:
    """Run one or more `browser-use` commands against the given profile (or the
    default `main` profile if omitted) and return the combined output.

    `args` is everything after `browser-use`. Multiple commands may be chained
    with `&&`; each runs as its own invocation against the persistent daemon,
    stopping at the first failure. We split on `&&` at the token level after
    shlex parsing, so `&&` inside a quoted value (e.g. typed text) is preserved
    and not treated as a separator.
    """
    import shlex

    try:
        tokens = shlex.split(args)
    except ValueError as e:
        return f"[error] Could not parse browser args: {e}"

    commands: list[list[str]] = [[]]
    for tok in tokens:
        if tok == "&&":
            commands.append([])
        else:
            commands[-1].append(tok)
    commands = [c for c in commands if c]
    if not commands:
        return "[error] No browser-use command given."

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
        for cmd in commands:
            text, ok = await _run_one_browser(_with_cdp(cmd, daemon_up, session, cdp_url))
            outputs.append(text)
            if not ok:
                break
            daemon_up = True
        return "\n".join(o for o in outputs if o).strip() or "(no output)"


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
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
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


async def _read_file(path: str) -> str:
    """Read a file's contents without blocking the event loop."""
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, _read_file_sync, path)
    except Exception as e:
        return f"[error] {e}"


def _read_file_sync(path: str) -> str:
    """Synchronous file read, run in executor."""
    p = Path(path).expanduser()
    if not p.exists():
        return f"[error] File not found: {path}"
    if p.stat().st_size > 500_000:
        return f"[error] File too large ({p.stat().st_size} bytes). Use run_shell with head/tail instead."
    return p.read_text(encoding="utf-8")


async def _write_file(path: str, content: str) -> str:
    """Write content to a file without blocking the event loop."""
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, _write_file_sync, path, content)
    except Exception as e:
        return f"[error] {e}"


def _write_file_sync(path: str, content: str) -> str:
    """Synchronous file write, run in executor."""
    p = Path(path).expanduser()
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

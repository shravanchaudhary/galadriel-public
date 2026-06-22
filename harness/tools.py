"""Tool definitions and execution for the agent."""

import asyncio
import json
import os
from pathlib import Path

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
        "name": "cloud_browser_open",
        "description": (
            "Open a cloud browser tab you can drive. The cloud browser is a real, "
            "remote Chrome session. Call this once before cloud_browser_run when no "
            "tab is open yet, or to navigate a fresh tab to a starting URL. Returns "
            "a live watch URL the user can open to see the screen in real time and "
            "take over if needed.\n\n"
            "If opening fails because a browser is already open (it may be a stale "
            "session), call cloud_browser_close and then cloud_browser_open again."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Starting URL to load (e.g. https://amazon.com). Defaults to a blank page.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "cloud_browser_run",
        "description": (
            "Drive the open cloud browser tab with a natural-language instruction. "
            "The cloud browser is itself agentic: describe the goal in plain "
            "English (e.g. \"search amazon for wireless earbuds and open the first "
            "result\", \"click the Sign in button\", \"read the total on the cart "
            "page\") and it plans and executes the clicks/typing/navigation for "
            "you. Auto-opens a tab if none exists.\n\n"
            "Choose the right mode so you call the cheapest API for the job:\n"
            "- Default (extract_only=false): take actions on the page AND return "
            "what it found. Use for anything that requires clicking, typing, or "
            "navigating.\n"
            "- extract_only=true: do NOT act — just read/extract the visible data "
            "on the current page. Fast and cheap; use for 'what is the value of X "
            "on this page?'.\n"
            "- raw_html=true: return the raw page HTML instead of a summarized "
            "result. Use when you need exact markup or to scrape structured data.\n\n"
            "BLOCKED PAGES — decide by importance:\n"
            "- If you hit a login wall, CAPTCHA, OTP prompt, or bot-detection block "
            "AND the blocked content is essential to the user's task, STOP and ask "
            "the user to take over on the watch URL (they can log in / solve the "
            "challenge live), then continue once they say it's done.\n"
            "- If the block is minor and the same value is reachable another way, "
            "skip it and move on. Do NOT stall waiting on the user for unimportant "
            "blocks (e.g. a value you can read elsewhere)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Plain-English instruction describing the action to take or the data to read.",
                },
                "extract_only": {
                    "type": "boolean",
                    "description": "If true, only extract visible text/data from the current page without taking any action. Default false.",
                },
                "raw_html": {
                    "type": "boolean",
                    "description": "If true, return the raw page HTML instead of a summarized result. Default false.",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "cloud_browser_close",
        "description": (
            "Close the cloud browser tab and release the remote session. Call when "
            "you are done browsing to free resources. Safe to call even if no tab "
            "is open."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
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
            "code into the 2FA field via cloud_browser_run. The code rotates every "
            "30 seconds — generate it immediately before you enter it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "secret_key": {
                    "type": "string",
                    "description": "The base32 TOTP secret key for the account (e.g. the LinkedIn account's key stored in your MEMORY.md). Any service's base32 authenticator secret works.",
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
            "Semantic search over the verbatim memory palace (MemPalace). "
            "Use this to recall past conversations, decisions, code changes, or facts "
            "that are not in the current context window or daily logs. "
            "Zero API cost — searches run locally against ChromaDB."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language query. Works best with full phrases, not keywords.",
                },
                "wing": {
                    "type": "string",
                    "description": "Optional wing filter (top-level namespace).",
                },
                "room": {
                    "type": "string",
                    "description": "Optional room filter (folder-based — memory, harness, tower, etc).",
                },
                "hall": {
                    "type": "string",
                    "description": "Optional hall filter (keyword-based auto-topic — decisions, problems, milestones, etc). Best scope for cross-cutting topic recall.",
                },
                "k": {
                    "type": "integer",
                    "description": "Number of results (default 5, max 20).",
                },
            },
            "required": ["query"],
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
            "Use this tool ONLY for web search. To open any search result or perform operations on it, "
            "use the cloud_browser."
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
]


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
        return await _run_shell(inputs["command"], inputs.get("working_dir", working_dir))
    elif name == "read_file":
        return await _read_file(inputs["path"])
    elif name == "write_file":
        return await _write_file(inputs["path"], inputs["content"])
    elif name == "cloud_browser_open":
        return await _cloud_browser_open(inputs.get("url"))
    elif name == "cloud_browser_run":
        return await _cloud_browser_run(
            inputs["command"],
            extract_only=inputs.get("extract_only", False),
            raw_html=inputs.get("raw_html", False),
        )
    elif name == "cloud_browser_close":
        return await _cloud_browser_close()
    elif name == "generate_totp":
        return _generate_totp(inputs["secret_key"])
    elif name == "memory_log":
        if memory_manager:
            memory_manager.append_daily_log(inputs["entry"])
            return "Logged to daily memory."
        return "Memory manager not available."
    elif name == "palace_search":
        from . import palace
        return await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: palace.search(
                query=inputs["query"],
                wing=inputs.get("wing"),
                room=inputs.get("room"),
                hall=inputs.get("hall"),
                k=inputs.get("k", 5),
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
    else:
        return f"Unknown tool: {name}"



# ── Cloud browser ─────────────────────────────────────────────────────
# A single remote Chrome session per harness process, kept in-memory and keyed
# by a stable user id so it survives across tool calls within the process.
# cloud_browser is imported lazily so a harness that never browses doesn't pay
# the import cost.
_CLOUD_BROWSER_USER_ID = (
    os.environ.get("CLOUD_BROWSER_USER_ID")
    or os.environ.get("DISCORD_AUTHORIZED_USER_ID")
    or "galadriel"
)


async def _get_cloud_browser(create: bool = True):
    """Return the singleton CloudBrowserAgent for this process. Returns None
    when create=False and none exists yet."""
    from .cloud_browser import CLOUD_BROWSER_INSTANCE_MAP, CloudBrowserAgent

    agent = CLOUD_BROWSER_INSTANCE_MAP.get(_CLOUD_BROWSER_USER_ID)
    if agent is None and create:
        agent = CloudBrowserAgent(_CLOUD_BROWSER_USER_ID)
    return agent


async def _cloud_browser_open(url: str = None) -> str:
    """Open or re-navigate the cloud browser tab to a starting URL."""
    agent = await _get_cloud_browser(create=True)
    try:
        result = await agent.create_browser(
            agent=True, url=url or "https://example.com/"
        )
    except Exception as e:
        return f"[error] Could not open cloud browser: {e}"
    watch_url = result.get("browser_url")
    return f"Cloud browser ready. Watch live at: {watch_url}"


async def _cloud_browser_run(
    command: str, extract_only: bool = False, raw_html: bool = False
) -> str:
    """Drive the cloud browser with a natural-language command."""
    agent = await _get_cloud_browser(create=True)
    if not agent.agent_url:
        try:
            await agent.create_browser(agent=True)
        except Exception as e:
            return f"[error] Could not open cloud browser: {e}"
    try:
        responses = await agent.run_command(
            command,
            direct_extract_data=extract_only,
            raw_html=raw_html,
        )
    except Exception as e:
        return f"[error] Cloud browser command failed: {e}"
    try:
        return json.dumps(responses, default=str, ensure_ascii=False)
    except Exception:
        return str(responses)


async def _cloud_browser_close() -> str:
    """Close the cloud browser tab and release the remote session."""
    agent = await _get_cloud_browser(create=False)
    if agent is None:
        return "No cloud browser session to close."
    try:
        await agent.close_browser()
    except Exception as e:
        return f"[warn] Close request failed, cleaning up anyway: {e}"
    finally:
        await agent.cleanup()
    return "Cloud browser closed."


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


# ── Google Search (Serper API) ────────────────────────────────────────

def sync_serper_search(q: str) -> list[dict]:
    import requests
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

    payload = json.dumps({"q": q})
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=10, max=120),
        retry=retry_if_exception_type(
            (requests.HTTPError, requests.RequestException, requests.Timeout)
        ),
        reraise=True,
    )
    def _do_sync_search():
        response = requests.post(url, headers=headers, data=payload, timeout=30)
        if response.status_code == 429:
            logger.warning(
                f"Rate limited by Serper API. Status: {response.status_code}"
            )
            response.raise_for_status()
        response.raise_for_status()
        result = response.json()
        return result.get("organic", [])

    try:
        return _do_sync_search()
    except requests.HTTPError as e:
        if e.response and e.response.status_code == 429:
            logger.warning(
                f"Serper API rate limit hit, will retry. Status: {e.response.status_code}"
            )
        raise e
    except requests.RequestException as e:
        logger.error(f"Serper API request error: {e}")
        raise e


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

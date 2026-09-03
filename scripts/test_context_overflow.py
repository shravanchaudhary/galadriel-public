#!/usr/bin/env python3
"""Local tests for the context-overflow defenses.

Three layers, all pure/local — no API key or network needed:
  1. tools.bound_tool_result / _read_file_sync — no tool result enters the
     buffer oversized; the full text moves to an artifact file.
  2. agent.externalize_oversized_input — an oversized paste becomes an
     artifact stub before the recall scan, storage, or the API see it.
  3. llm_retry.is_context_overflow_error — every provider's over-window
     rejection is recognized (and transient errors are not), so the agent
     loop can compact and retry instead of wedging the channel.

Background: project-agent-kb/kb/context-overflow-400.md.

Usage:
    venv/bin/python scripts/test_context_overflow.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="galadriel-overflow-test-")
os.environ["GALADRIEL_STORAGE_ROOT"] = _TMP

from harness.agent import externalize_oversized_input  # noqa: E402
from harness.compaction import estimate_request_tokens  # noqa: E402
from harness.providers.llm_retry import is_context_overflow_error  # noqa: E402
from harness.tools import (  # noqa: E402
    _INLINE_RESULT_MAX_TOKENS,
    _bound_result,
    _read_file_sync,
    bound_tool_result,
)

# Char equivalent of the shared inline cap, for building test payloads.
INLINE_CHARS = _INLINE_RESULT_MAX_TOKENS * 4


def _check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))
    return ok


def _artifact_files() -> list[Path]:
    d = Path(_TMP) / "state" / "artifacts"
    return sorted(d.glob("*.txt")) if d.exists() else []


# ── 1. Tool-result bounds ────────────────────────────────────────────

def test_small_result_passes_through() -> bool:
    text = "x" * 1000
    return _check("small tool result passes through", bound_tool_result(text, "run_shell") is text)


def test_oversized_result_is_spilled() -> bool:
    text = "A" * 5000 + "M" * (INLINE_CHARS) + "Z" * 5000
    before = set(_artifact_files())
    out = bound_tool_result(text, "run_shell")
    new = [p for p in _artifact_files() if p not in before]
    ok = (
        len(out) < len(text)
        and out.startswith("A" * 100)
        and out.endswith("Z" * 100)
        and len(new) == 1
        and new[0].read_text() == text
        and str(new[0]) in out
    )
    return _check("oversized tool result → head+tail stub + full artifact file", ok)


def test_read_file_skips_the_central_bound() -> bool:
    # read_file bounds itself (model-aware full_page budget); the shared
    # bound must not re-truncate what it already sized.
    text = "y" * (INLINE_CHARS * 3)
    ok = _bound_result(text, "read_file") is text
    return _check("read_file is self-bounded — central bound skips it", ok)


def test_block_list_text_is_bounded() -> bool:
    big = "b" * (INLINE_CHARS * 2)
    result = [
        {"type": "text", "text": big},
        {"type": "image", "source": {"data": "..."}},
    ]
    out = _bound_result(result, "browser")
    ok = (
        len(out[0]["text"]) < len(big)
        and "omitted" in out[0]["text"]
        and out[1]["type"] == "image"
    )
    return _check("block-list result: text bounded, image untouched", ok)


def test_read_file_default_is_bounded_like_everything_else() -> bool:
    path = Path(_TMP) / "big_input.txt"
    body = "H" * 150_000 + "MIDDLE-MARKER" + "T" * 150_000
    path.write_text(body)
    out = _read_file_sync(str(path))
    ok = (
        out.startswith("H" * 100)
        and out.endswith("T" * 100)
        and "MIDDLE-MARKER" not in out
        and str(path) in out
        and "full_page" in out
        and len(out) < INLINE_CHARS + 500
    )
    return _check("read_file default → same 30k head+tail bound, full_page hint", ok)


def test_full_page_returns_whole_file_within_budget() -> bool:
    path = Path(_TMP) / "big_input.txt"  # 300k chars, written above
    out = _read_file_sync(str(path), full_page=True)
    # No model → 200k/8k defaults → ~613k-char budget: the file fits whole.
    ok = "MIDDLE-MARKER" in out and len(out) == 300_013
    return _check("full_page returns the whole file within the model budget", ok)


def test_full_page_still_respects_a_small_model_window() -> bool:
    path = Path(_TMP) / "big_input.txt"
    # gpt-oss-120b: 131072 ctx − 65536 out → ×0.8×4 ≈ 209k chars < 300k file.
    out = _read_file_sync(str(path), full_page=True, model="gpt-oss-120b")
    ok = (
        "MIDDLE-MARKER" not in out
        and out.startswith("H" * 100)
        and out.endswith("T" * 100)
        and "context budget" in out
        and len(out) < 250_000
    )
    return _check("full_page on a small-window model is still capped", ok)


# ── 2. Oversized inbound input ───────────────────────────────────────

WINDOW = 200_000  # tokens; inline cap = 20% * 4 chars = 160k chars


def test_small_input_untouched() -> bool:
    msg, spilled = externalize_oversized_input("hello there", WINDOW)
    return _check("small string input untouched", msg == "hello there" and spilled == 0)


def test_giant_paste_becomes_artifact() -> bool:
    text = "S" * 5000 + "d" * 400_000 + "E" * 5000
    before = set(_artifact_files())
    msg, spilled = externalize_oversized_input(text, WINDOW)
    new = [p for p in _artifact_files() if p not in before]
    ok = (
        spilled == 1
        and len(msg) < 20_000
        and "S" * 100 in msg
        and "E" * 100 in msg
        and len(new) == 1
        and new[0].read_text() == text
        and str(new[0]) in msg
        and "infer the intent" in msg
    )
    return _check("giant paste → stub + artifact + task instruction", ok)


def test_block_list_keeps_prompt_and_images() -> bool:
    doc = "D" * 700_000
    msg = [
        {"type": "text", "text": "summarize the attached report"},
        {"type": "text", "text": doc},
        {"type": "image", "source": {"data": "..."}},
    ]
    out, spilled = externalize_oversized_input(msg, WINDOW)
    ok = (
        spilled == 1
        and out[0]["text"] == "summarize the attached report"
        and len(out[1]["text"]) < len(doc)
        and out[2]["type"] == "image"
        # a real prompt survived inline → no synthesized instruction block
        and len(out) == 3
    )
    return _check("block list: prompt + image survive, only the doc spills", ok)


def test_all_documents_get_synthesized_instruction() -> bool:
    out, spilled = externalize_oversized_input(
        [{"type": "text", "text": "D" * 700_000}], WINDOW,
    )
    ok = (
        spilled == 1
        and out[-1]["type"] == "text"
        and "infer the intent" in out[-1]["text"]
    )
    return _check("all-document message gets a synthesized instruction block", ok)


def test_zero_window_passes_through() -> bool:
    text = "d" * 900_000
    msg, spilled = externalize_oversized_input(text, 0)
    return _check("unknown window (0) never spills", msg is text and spilled == 0)


# ── 3. Pre-send estimate ─────────────────────────────────────────────

def test_estimate_counts_all_parts() -> bool:
    messages = [{"role": "user", "content": "x" * 4000}]  # ~1000 tokens
    system = [{"type": "text", "text": "s" * 4000}]
    tools = [{"name": "t", "description": "d" * 4000}]
    est = estimate_request_tokens(messages, system, tools)
    ok = 2500 < est < 5000
    return _check("request estimate covers messages+system+tools", ok, f"est={est}")


def test_estimate_prices_images_nominally() -> bool:
    # A 5MB screenshot is ~6.7M base64 chars; counted by length it would fire
    # the send ceiling forever. It must cost ~1.6k tokens, like the API bills.
    screenshot = {
        "role": "user",
        "content": [{
            "type": "tool_result", "tool_use_id": "t1",
            "content": [
                {"type": "text", "text": "captured"},
                {"type": "image", "source": {"data": "A" * 6_700_000}},
            ],
        }],
    }
    est = estimate_request_tokens([screenshot], [], [])
    ok = est < 5_000
    return _check("images estimate at nominal cost, not base64 length", ok, f"est={est}")


def test_estimate_counts_reasoning_once_and_only_for_the_last_turn() -> bool:
    # Native Claude stores a turn's thinking twice (inline block + _thought
    # mirror) and the API bills only the current turn's thinking. Counting
    # both, for every turn, made thinking-heavy cascades look 2x their size
    # and compact prematurely.
    reasoning = "r" * 16_000  # 4,000 tokens
    def turn():
        return {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": reasoning, "signature": "s"},
                {"type": "text", "text": "ok"},
            ],
            "_thought": reasoning,
        }
    older, newest = turn(), turn()
    est = estimate_request_tokens([older, newest], [], [])
    # older turn: ~0 reasoning; newest: reasoning ONCE (~4k), not twice (~8k)
    ok = 3_900 < est < 4_600
    return _check("estimate: reasoning counted once, newest turn only", ok, f"est={est}")


def test_execute_tool_central_bound_applies_to_real_tools() -> bool:
    # read_file is self-bounded, so it cannot prove the shared wiring. A real
    # oversized shell result must come back through execute_tool as a stub.
    import asyncio
    from harness.tools import execute_tool

    out = asyncio.run(execute_tool(
        "run_shell", {"command": "python3 -c \"print('x' * 120000)\""},
    ))
    ok = (
        isinstance(out, str)
        and len(out) < INLINE_CHARS
        and "tokens omitted" in out
        and "saved for ~24h" in out
    )
    return _check("execute_tool: shared bound + spill wired for real tools", ok)


# ── 4. Over-window rejection classifier ──────────────────────────────

OVERFLOW_MESSAGES = [
    # Anthropic / Bedrock anthropic
    "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
    "'message': 'prompt is too long: 213462 tokens > 200000 maximum'}}",
    "ValidationException: Input is too long for requested model.",
    "input length and `max_tokens` exceed context limit: 195000 + 64000 > 204698",
    # OpenAI-compatible (Mantle)
    "Error code: 400 - {'error': {'message': \"This model's maximum context length "
    "is 131072 tokens. However, your messages resulted in 140000 tokens.\", "
    "'code': 'context_length_exceeded'}}",
    # google-genai
    "400 INVALID_ARGUMENT. The input token count (1234567) exceeds the maximum "
    "number of tokens allowed (1048576).",
]

NON_OVERFLOW_MESSAGES = [
    "Error code: 429 - rate limit exceeded, please retry after 30 seconds",
    "Error code: 529 - overloaded_error",
    "Error code: 400 - messages.1.content.0: unexpected `tool_use_id` found",
    "APITimeoutError: Request timed out.",
    "400 INVALID_ARGUMENT. thinking_budget=0 is not supported for this model.",
]


def test_overflow_messages_are_recognized() -> bool:
    misses = [m for m in OVERFLOW_MESSAGES if not is_context_overflow_error(Exception(m))]
    return _check("every provider overflow message is recognized", not misses, f"missed={misses}")


def test_non_overflow_messages_are_not() -> bool:
    hits = [m for m in NON_OVERFLOW_MESSAGES if is_context_overflow_error(Exception(m))]
    return _check("transient/unrelated errors are not misclassified", not hits, f"hit={hits}")


def test_413_status_is_overflow() -> bool:
    class E(Exception):
        status_code = 413
    return _check("HTTP 413 counts as overflow", is_context_overflow_error(E("payload too large")))


# ── 5. Uniform tool access ───────────────────────────────────────────

def test_consolidation_pass_can_chase_spill_stubs() -> bool:
    # Every channel plays by the same spill rules, so every channel needs the
    # file tools to chase a stub — including the silent consolidation pass.
    # But the recall-scan exclusion set derives from CONSOLIDATION_TOOLS, and
    # file tools must stay IN the scan corpus on normal turns.
    from harness.agent import (
        CONSOLIDATION_TURN_TOOLS,
        RECALL_SCAN_EXCLUDED_TOOLS,
    )
    ok = (
        {"read_file", "run_shell"} <= CONSOLIDATION_TURN_TOOLS
        and not ({"read_file", "run_shell"} & RECALL_SCAN_EXCLUDED_TOOLS)
    )
    return _check("consolidation gets file tools; recall scan still sees them", ok)


def test_execute_tool_boundary_plumbs_model_and_bounds() -> bool:
    # Through the REAL entry point (not the internals): the central bound
    # applies, and the model kwarg reaches read_file's full_page budget.
    import asyncio
    from harness.tools import execute_tool

    path = Path(_TMP) / "big_input.txt"  # 300k chars ≈ 75k tokens
    out_default = asyncio.run(execute_tool("read_file", {"path": str(path)}))
    out_small = asyncio.run(execute_tool(
        "read_file", {"path": str(path), "full_page": True},
        model="gpt-oss-120b",
    ))
    out_big = asyncio.run(execute_tool(
        "read_file", {"path": str(path), "full_page": True},
    ))
    ok = (
        "MIDDLE-MARKER" not in out_default
        and len(out_default) < INLINE_CHARS + 500
        and "MIDDLE-MARKER" not in out_small
        and "context budget" in out_small
        and "MIDDLE-MARKER" in out_big
    )
    return _check("execute_tool boundary: central bound + model plumb both live", ok)


def test_spill_failure_degrades_safely() -> bool:
    # A full/read-only disk must degrade, never crash: tool results still come
    # back bounded with an honest notice; user input passes through untouched
    # (the pre-send gate and 400 backstop still stand behind it).
    from unittest.mock import patch
    import harness.tools as tools_mod

    big = "q" * (INLINE_CHARS * 2)
    with patch.object(tools_mod, "spill_text", side_effect=OSError("disk full")):
        out = tools_mod.bound_tool_result(big, "run_shell")
        msg, spilled = externalize_oversized_input("z" * 900_000, WINDOW)
    ok = (
        "could NOT be saved" in out
        and len(out) < INLINE_CHARS
        and msg == "z" * 900_000
        and spilled == 0
    )
    return _check("spill failure: bounded notice for tools, passthrough for input", ok)


def test_in_conversation_summarizer_request_shape() -> bool:
    # The ONLY summarizer path: messages ride as messages with the summarize
    # instruction appended as the final user turn, the turn's own system/tools
    # ride along (prompt-cache reuse), output budgeted at
    # IN_CONVERSATION_MAX_TOKENS. An empty snapshot raises.
    import asyncio
    from harness import compaction

    calls = {}

    class _Block:
        text = "GOAL: test snapshot"

    class _Usage:
        input_tokens, output_tokens = 10, 5

    class _Resp:
        usage, stop_reason, content = _Usage(), "end_turn", [_Block()]

    class _Prov:
        async def create_message(self, **kw):
            calls.update(kw)
            return _Resp()

    msgs = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    result = asyncio.run(compaction.compact_to_snapshot(
        msgs,
        live_provider=_Prov(), live_model="test-model",
        live_system=[{"type": "text", "text": "sys"}], live_tools=[{"name": "t"}],
    ))
    sent = calls.get("messages") or []
    ok = (
        result["snapshot"] == "GOAL: test snapshot"
        and calls.get("max_tokens") == compaction.IN_CONVERSATION_MAX_TOKENS
        and calls.get("model") == "test-model"
        and calls.get("system") and calls.get("tools")
        and len(sent) == 3
        and sent[:2] == msgs
        and sent[-1]["role"] == "user"
        and sent[-1]["content"].startswith("[SYSTEM] Context window limit reached")
    )

    class _EmptyResp:
        usage, stop_reason, content = _Usage(), "end_turn", []

    class _EmptyProv:
        async def create_message(self, **kw):
            return _EmptyResp()

    try:
        asyncio.run(compaction.compact_to_snapshot(
            msgs, live_provider=_EmptyProv(), live_model="test-model",
        ))
        empty_raises = False
    except ValueError:
        empty_raises = True
    return _check("in-conversation summarizer: request shape + empty raises", ok and empty_raises)


# ── 6. Study flow + artifact lifecycle ───────────────────────────────

def test_study_text_files_parts_without_cross_purging() -> bool:
    # study_text is the sibling of the conversation miner: chunked, purge-
    # before-insert, but scoped to its own part's index range so re-studying
    # part 2 never deletes part 1. Chunks land in room=sources with the
    # source_file + chunk_number shape that search_meta walks.
    from unittest.mock import MagicMock, patch
    from harness import mongo_palace

    stored, deletes = [], []
    coll = MagicMock()
    coll.delete_many.side_effect = lambda q: deletes.append(q) or MagicMock()
    with patch.object(mongo_palace, "_collection", return_value=coll), \
         patch.object(
             mongo_palace, "_store_chunks",
             side_effect=lambda rows: stored.append(rows) or len(rows),
         ):
        n1 = mongo_palace.study_text(
            "First part sentence about alpha topics. " * 300,
            source_file="/a/doc.txt", hall="doc", part=1,
        )
        n2 = mongo_palace.study_text(
            "Second part sentence about beta topics. " * 300,
            source_file="/a/doc.txt", hall="doc", part=2,
        )
    stride = mongo_palace.STUDY_PART_STRIDE
    p1, p2 = stored
    ok = (
        n1 > 0 and n2 > 0
        and all(r["room"] == "sources" and r["source_file"] == "/a/doc.txt" for r in p1 + p2)
        and all(r["chunk_index"] < stride for r in p1)
        and all(stride <= r["chunk_index"] < 2 * stride for r in p2)
        and p2[0]["chunk_number"] == stride + 1
        # ids are namespaced: a studied path can NEVER collide with drawers the
        # conversation miner would mint from the same source_file, and part 2
        # can never overwrite part 1 by _id.
        and p1[0]["_id"] == mongo_palace._drawer_id("study:/a/doc.txt", 0)
        and p2[0]["_id"] == mongo_palace._drawer_id("study:/a/doc.txt", stride)
        and p1[0]["_id"] != mongo_palace._drawer_id("/a/doc.txt", 0)
        # each part purged only its own index range, scoped to room=sources so
        # study and the miner cannot delete each other's drawers
        and deletes[0]["chunk_index"]["$lt"] == stride
        and deletes[1]["chunk_index"]["$gte"] == stride
        and all(d["room"] == "sources" for d in deletes)
    )
    return _check("study_text: part-scoped, room-scoped, id-namespaced", ok)


def test_shrunk_file_tail_is_purged() -> bool:
    # A file studied at 3 parts then truncated to 1: any study call carrying
    # the current total_parts clears the stale tail; an empty part still runs
    # its purges and files nothing.
    from unittest.mock import MagicMock, patch
    from harness import mongo_palace

    deletes = []
    coll = MagicMock()
    coll.delete_many.side_effect = lambda q: deletes.append(q) or MagicMock()
    with patch.object(mongo_palace, "_collection", return_value=coll), \
         patch.object(mongo_palace, "_store_chunks", side_effect=lambda rows: len(rows)):
        n = mongo_palace.study_text(
            "", source_file="/a/doc.txt", hall="doc", part=2, total_parts=1,
        )
    stride = mongo_palace.STUDY_PART_STRIDE
    ok = (
        n == 0
        and len(deletes) == 2
        and deletes[0]["chunk_index"] == {"$gte": stride, "$lt": 2 * stride}
        and deletes[1]["chunk_index"] == {"$gte": stride}
        and all(d["room"] == "sources" for d in deletes)
    )
    return _check("shrunk file: stale tail purged, empty part still purges", ok)


def test_taught_retrieval_recipe_survives_build_filter() -> bool:
    # Every study result, tool description, and the stable section teach
    # palace_search(search_meta={'room','source_file','chunk_number' range}).
    # Pin that the real filter builder accepts exactly that recipe.
    from harness.mongo_palace import FILTERABLE, build_filter

    q = build_filter(None, None, None, {
        "room": "sources",
        "source_file": "/a/doc.txt",
        "chunk_number": {"from": 1, "to": 50},
    })
    ok = (
        {"room", "source_file", "chunk_number"} <= FILTERABLE
        and q["room"] == "sources"
        and q["source_file"] == "/a/doc.txt"
        and q["chunk_number"] == {"$gte": 1, "$lte": 50}
    )
    return _check("taught search_meta recipe is accepted by build_filter", ok)


def test_search_hit_renders_source_file() -> bool:
    # chunk_number collides across studied documents; a hit must hand back
    # source_file or the taught follow-up walk cannot be scoped.
    from unittest.mock import patch
    from harness import mongo_palace

    row = {
        "id": "abc123", "text": "chunk text", "similarity": 0.5,
        "metadata": {
            "wing": "agent", "room": "sources", "hall": "doc",
            "source_file": "/a/doc.txt", "chunk_number": 7,
        },
    }
    with patch.object(mongo_palace, "fetch_data", return_value=[row]):
        out = mongo_palace.search_markdown(search_meta={"room": "sources"})
    ok = "source_file=`/a/doc.txt`" in out and "chunk=7" in out
    return _check("sources search hit carries source_file for the walk", ok)


def test_wake_up_excludes_sources() -> bool:
    # A freshly studied document is hundreds of the newest drawers; without
    # this exclusion the wake-up digest becomes the document.
    from unittest.mock import MagicMock, patch
    from harness import mongo_palace

    coll = MagicMock()
    coll.find.return_value.sort.return_value.limit.return_value = []
    with patch.object(mongo_palace, "_collection", return_value=coll):
        mongo_palace.wake_up_text()
    room_filter = coll.find.call_args[0][0]["room"]
    ok = set(room_filter.get("$nin", [])) >= {"conversations", "sources"}
    return _check("wake_up digest excludes room=sources like conversations", ok)


def test_artifact_ttl_sweep() -> bool:
    # Artifacts are scratch: >24h old ones are deleted opportunistically when
    # a new spill lands. Fresh ones stay.
    import time as _time
    from harness.tools import _ARTIFACT_TTL_SECONDS, artifact_dir, spill_text

    d = artifact_dir()
    stale = d / "shell-old.txt"
    stale.write_text("old")
    old = _time.time() - _ARTIFACT_TTL_SECONDS - 60
    os.utime(stale, (old, old))
    # A recent pre-existing artifact must SURVIVE the sweep — this is what
    # catches a regression into delete-everything (the file written by the
    # sweeping spill itself cannot, since it lands after the sweep).
    keeper = d / "shell-keeper.txt"
    keeper.write_text("recent")
    fresh = spill_text("fresh content", "test")
    ok = not stale.exists() and keeper.exists() and fresh.exists()
    return _check("artifact sweep: >24h deleted, recent survives", ok)


def test_study_file_tool_slices_parts_and_reports() -> bool:
    # Through the real execute_tool boundary: part slicing respects the
    # per-call token budget, and the result teaches the retrieval recipe.
    import asyncio
    from unittest.mock import patch
    from harness.tools import _STUDY_PART_TOKENS, execute_tool

    big = Path(_TMP) / "book.txt"
    big.write_text("A sentence of book content. " * 40_000)  # > one part
    seen = {}

    async def _fake_study(text, *, source_path, hall, part, total_parts):
        seen.update(
            text_len=len(text), source_path=source_path, hall=hall,
            part=part, total_parts=total_parts,
        )
        return 42

    with patch("harness.palace.study_document", new=_fake_study):
        out = asyncio.run(execute_tool(
            "study_file", {"path": str(big), "topic": "My Book!"},
        ))
    ok = (
        seen["part"] == 1
        and seen["total_parts"] == 2
        and seen["hall"] == "my-book"
        and seen["text_len"] <= _STUDY_PART_TOKENS * 4
        and "Filed 42 chunks" in out
        and "part=2" in out
        and "palace_search" in out
        and "'room': 'sources'" in out
    )
    return _check("study_file tool: part slicing + retrieval recipe in result", ok)


def test_study_file_final_and_beyond_eof_parts() -> bool:
    # The last part says the file is fully studied; a part beyond the file's
    # current end still calls the palace (so the range purges run) but files
    # nothing and says so.
    import asyncio
    from unittest.mock import patch
    from harness.tools import execute_tool

    big = Path(_TMP) / "book.txt"  # 2 parts, written above
    calls = []

    async def _fake_study(text, *, source_path, hall, part, total_parts):
        calls.append((len(text), part, total_parts))
        return 7

    with patch("harness.palace.study_document", new=_fake_study):
        out_last = asyncio.run(execute_tool("study_file", {"path": str(big), "part": 2}))
        out_eof = asyncio.run(execute_tool("study_file", {"path": str(big), "part": 3}))
    ok = (
        "The whole file is studied" in out_last
        and calls[0][1:] == (2, 2)
        # beyond-EOF: empty blob still reaches the palace so purges run
        and calls[1][0] == 0
        and "beyond the file's current end" in out_eof
        and "Filed" not in out_eof
    )
    return _check("study_file: final-part message + beyond-EOF purge path", ok)


def test_binary_files_are_refused() -> bool:
    import asyncio
    from harness.tools import execute_tool

    bad = Path(_TMP) / "image.bin"
    bad.write_bytes(b"\x00\x01\xfe\xff" * 4000)
    out = asyncio.run(execute_tool("study_file", {"path": str(bad)}))
    ok = "does not decode as readable text" in out
    return _check("binary/non-UTF-8 files are refused, not studied as garbage", ok)


def test_stable_contract_names_all_four_instruments() -> bool:
    # The anti-confusion contract: one stable section, four instruments with
    # one job each. If any of these names drifts out, the agent loses the map.
    from harness.memory import OVERSIZED_INPUT_STABLE_SECTION as sec
    ok = all(
        term in sec
        for term in (
            "study_file", "learn", "read_file", "palace_search",
            "memory(query)", '"sources"', "~24 hours",
        )
    )
    return _check("stable section maps grep/study/learn/memory-vs-search", ok)


# ── 7. Model matrix: every model, every selectable limit ─────────────

def test_every_model_can_summarize_at_the_trigger() -> bool:
    # The in-conversation summarizer is the ONLY summarizer (fallback removed).
    # For every catalog model and every selectable threshold, the effective
    # trigger min(threshold, 85% of window) must be (a) strictly reachable and
    # (b) leave headroom for the summarizer's own output inside the window.
    # The full_page read budget must also leave the model's output space.
    from harness import model_catalog, tower_settings
    from harness.agent import _COMPACT_WINDOW_FRACTION
    from harness.compaction import IN_CONVERSATION_MAX_TOKENS
    from harness.tools import _full_page_token_budget

    bad = []
    for m in model_catalog.MODELS:
        window = m.context
        for opt in tower_settings.context_options_for_model(m.key):
            effective = min(opt, int(window * _COMPACT_WINDOW_FRACTION))
            if effective >= window:
                bad.append(f"{m.key}@{opt}: trigger unreachable")
            if effective + IN_CONVERSATION_MAX_TOKENS > window:
                bad.append(f"{m.key}@{opt}: no summarizer headroom")
        if _full_page_token_budget(m.key) > window - m.max_output:
            bad.append(f"{m.key}: full_page budget exceeds window-output")
    return _check(
        f"all {len(model_catalog.MODELS)} models: trigger reachable, "
        "summarizer headroom, full_page fits",
        not bad, "; ".join(bad[:4]),
    )


def test_fallback_summarizer_is_gone() -> bool:
    import asyncio
    import harness.compaction as compaction
    from harness import model_registry

    removed = (
        not hasattr(compaction, "COMPRESSION_MESSAGE")
        and not hasattr(compaction, "_render_transcript")
    )
    try:
        asyncio.run(
            compaction.compact_to_snapshot([{"role": "user", "content": "hi"}])
        )
        raised = False
    except ValueError:
        raised = True
    no_task = (
        "compaction" not in model_registry.TASKS
        and "compaction" not in model_registry.FOLLOW_ACTIVE_MODEL
    )
    return _check(
        "fallback summarizer fully removed; missing live summarizer raises",
        removed and raised and no_task,
    )


def main() -> int:
    results = [
        test_small_result_passes_through(),
        test_oversized_result_is_spilled(),
        test_read_file_skips_the_central_bound(),
        test_block_list_text_is_bounded(),
        test_read_file_default_is_bounded_like_everything_else(),
        test_full_page_returns_whole_file_within_budget(),
        test_full_page_still_respects_a_small_model_window(),
        test_small_input_untouched(),
        test_giant_paste_becomes_artifact(),
        test_block_list_keeps_prompt_and_images(),
        test_all_documents_get_synthesized_instruction(),
        test_zero_window_passes_through(),
        test_estimate_counts_all_parts(),
        test_estimate_prices_images_nominally(),
        test_estimate_counts_reasoning_once_and_only_for_the_last_turn(),
        test_execute_tool_central_bound_applies_to_real_tools(),
        test_overflow_messages_are_recognized(),
        test_non_overflow_messages_are_not(),
        test_413_status_is_overflow(),
        test_consolidation_pass_can_chase_spill_stubs(),
        test_execute_tool_boundary_plumbs_model_and_bounds(),
        test_spill_failure_degrades_safely(),
        test_in_conversation_summarizer_request_shape(),
        test_every_model_can_summarize_at_the_trigger(),
        test_fallback_summarizer_is_gone(),
        test_study_text_files_parts_without_cross_purging(),
        test_shrunk_file_tail_is_purged(),
        test_taught_retrieval_recipe_survives_build_filter(),
        test_search_hit_renders_source_file(),
        test_wake_up_excludes_sources(),
        test_artifact_ttl_sweep(),
        test_study_file_tool_slices_parts_and_reports(),
        test_study_file_final_and_beyond_eof_parts(),
        test_binary_files_are_refused(),
        test_stable_contract_names_all_four_instruments(),
    ]
    passed = sum(results)
    print(f"\n{passed}/{len(results)} tests passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

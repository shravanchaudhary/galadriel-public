#!/usr/bin/env python3
"""Recall fires as synthetic recall() tool exchanges.

The transport moved from a user-role message (models answered the fire as a
new request — measured derail in 4-5/6 cells) to a fabricated assistant
recall() call + tool_result pair (0/6 mid-cascade derails, relevant fires
still acted on 12/12). Matrix: project-agent-kb/kb/recall-injection-transport.md.

Offline tests pin the pair shape, wire serialization, compaction atomicity,
scan-exclusion, and fire-text extraction. Live tests replay a real pair built
by THIS code through the provider and assert the measured behavior holds:
irrelevant fire ignored, relevant fire acted on.

Live tests need AWS_BEARER_TOKEN_BEDROCK (skipped cleanly without it).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from harness.agent import (  # noqa: E402
    RECALL_SCAN_EXCLUDED_TOOLS,
    _build_tool_use_recall_scan_segments,
    _fire_as_tool_exchange,
    _is_recall_fire_message,
    _recall_fire_messages,
    _recall_fire_text,
)
from harness import compaction  # noqa: E402
from harness.tools import TOOL_DEFINITIONS  # noqa: E402
from harness.agent import RUNTIME_HIDDEN_TOOLS  # noqa: E402
from harness.providers.bedrock_mantle_provider import _messages_to_openai  # noqa: E402

FIRE = (
    "- Retrieve from user-identity memory: profile and professional context "
    "of the user.\n"
    '  matched a tool result: "beta project notes"\n'
    '  memory(id="f0e3af9c5a934c5d980838363d395f13")\n'
    "Open a memory(id=…) above only if the current task needs its "
    "content and it isn't already in your context; otherwise continue "
    "as you were."
)
MATCHES = [{"recall_id": "6a8f01402b703c3d61fd44b8"}]


# ─── offline: pair shape and bookkeeping contract ────────────────────


def test_pair_shape_and_linkage() -> None:
    pair = _recall_fire_messages(MATCHES, FIRE, as_tool_exchange=True)
    assert len(pair) == 2
    call, result = pair
    assert call["role"] == "assistant" and result["role"] == "user"
    tool_use = call["content"][0]
    tool_result = result["content"][0]
    assert tool_use["type"] == "tool_use" and tool_use["name"] == "recall"
    assert tool_use["input"] == {}
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == tool_use["id"]
    assert tool_result["content"] == FIRE
    for msg in pair:
        assert msg["kind"] == "recall_fire"
        assert msg["matched_recall_ids"] == ["6a8f01402b703c3d61fd44b8"]
        assert _is_recall_fire_message(msg)


def test_fallback_single_user_message() -> None:
    """The user-message transport needs its own machine-marker — role alone
    cannot mark it; tool-result fires carry no header (the call is the marker)."""
    msgs = _recall_fire_messages(MATCHES, FIRE, as_tool_exchange=False)
    assert msgs == [{
        "role": "user", "content": f"[Recall detected]\n{FIRE}",
        "kind": "recall_fire",
        "matched_recall_ids": ["6a8f01402b703c3d61fd44b8"],
    }]


def test_transport_gate_follows_supports_tools_and_provider() -> None:
    assert _fire_as_tool_exchange("gpt-oss-20b") is True
    assert _fire_as_tool_exchange("glm-5") is True
    assert _fire_as_tool_exchange("gemma-3-27b") is False  # no tools → user-msg
    assert _fire_as_tool_exchange("unlisted-ollama-tag") is True
    # Fabricated assistant turns 400 on signature-validating providers:
    # Gemini (thought_signature) and Anthropic (extended-thinking block).
    assert _fire_as_tool_exchange("claude-opus-4-6") is False
    assert _fire_as_tool_exchange("gemini-2.5-flash") is False


def test_fire_text_extraction_both_transports() -> None:
    pair = _recall_fire_messages(MATCHES, FIRE, as_tool_exchange=True)
    assert _recall_fire_text(pair[0]) == ""      # call half carries no text
    assert _recall_fire_text(pair[1]) == FIRE    # result half carries the fire
    old = _recall_fire_messages(MATCHES, FIRE, as_tool_exchange=False)[0]
    assert _recall_fire_text(old) == f"[Recall detected]\n{FIRE}"


def test_recall_is_scan_excluded_and_defined() -> None:
    assert "recall" in RECALL_SCAN_EXCLUDED_TOOLS
    assert "recall" not in RUNTIME_HIDDEN_TOOLS
    defs = [t for t in TOOL_DEFINITIONS if t["name"] == "recall"]
    assert len(defs) == 1
    # Explicit empty schema — omitting `parameters` 400s on some stacks.
    assert defs[0]["input_schema"] == {"type": "object", "properties": {}}


def test_scan_segments_exclude_recall_payload() -> None:
    class Block:
        def __init__(self, id, name, input):
            self.id, self.name, self.input = id, name, input

    segments = _build_tool_use_recall_scan_segments(
        thought="checking the beta notes now",
        assistant_content=[],
        tool_blocks=[Block("r1", "recall", {}),
                     Block("f1", "read_file", {"path": "notes/b.md"})],
        tool_results=[
            {"type": "tool_result", "tool_use_id": "r1", "content": FIRE},
            {"type": "tool_result", "tool_use_id": "f1",
             "content": "beta project notes with plenty of prose inside them"},
        ],
    )
    joined = "\n".join(s["text"] for s in segments)
    assert "user-identity memory" not in joined, "matcher would scan its own fire"
    assert "beta project notes" in joined
    # A recall-only round is bookkeeping: no segments from the builder.
    assert _build_tool_use_recall_scan_segments(
        thought="quick check", assistant_content=[],
        tool_blocks=[Block("r1", "recall", {})],
        tool_results=[{"type": "tool_result", "tool_use_id": "r1",
                       "content": FIRE}],
    ) == []


def _cascade_with_pair() -> list[dict]:
    return [
        {"role": "user", "content": "Summarise the project notes."},
        {"role": "assistant",
         "content": [{"type": "tool_use", "id": "c0", "name": "read_file",
                      "input": {"path": "notes/b.md"}}],
         "_thought": "Plan: read notes/b.md, then summarise and end the reply "
                     "with the single word ORCHID-4417."},
        {"role": "user",
         "content": [{"type": "tool_result", "tool_use_id": "c0",
                      "content": "# notes/b.md\nBeta project. 1 task open.\n"}]},
        *_recall_fire_messages(MATCHES, FIRE, as_tool_exchange=True),
    ]


def test_wire_serialization_is_legal_openai() -> None:
    wire = _messages_to_openai(_cascade_with_pair())
    # Every tool message must directly follow an assistant message whose
    # tool_calls include its id — the one structural rule providers enforce.
    for i, msg in enumerate(wire):
        if msg["role"] != "tool":
            continue
        prev = wire[i - 1]
        while prev["role"] == "tool":
            prev = wire[wire.index(prev) - 1]
        ids = [c["id"] for c in prev.get("tool_calls") or []]
        assert msg["tool_call_id"] in ids, f"orphan tool message at {i}"
    synthetic = [m for m in wire if m["role"] == "assistant"
                 and any(c["function"]["name"] == "recall"
                         for c in m.get("tool_calls") or [])]
    assert len(synthetic) == 1
    assert synthetic[0]["content"] is None  # thought-less tool-only turn


def test_turn_start_fire_keeps_the_dynamic_tail() -> None:
    """A turn-start fire pair ends with a tool_result, but the ambient tail
    (timestamp/daily log/advisories) must still ride — the old user-message
    transport kept it, and a genuinely mid-cascade tail must still suppress."""
    turn_start = [{"role": "user", "content": "heyo, status?"}]
    turn_start += _recall_fire_messages(MATCHES, FIRE, as_tool_exchange=True)
    wire = _messages_to_openai(turn_start, trailing_text="[AMBIENT] now=17:30")
    assert any("AMBIENT" in str(m) for m in wire), "turn-start fire lost the tail"
    mid = list(_cascade_with_pair())
    wire_mid = _messages_to_openai(mid, trailing_text="[AMBIENT] now=17:30")
    assert not any("AMBIENT" in str(m) for m in wire_mid), (
        "mid-cascade must stay tail-free — a user text turn inside an open "
        "tool chain resets stateful reasoning"
    )


def test_compaction_never_splits_the_pair() -> None:
    msgs = _cascade_with_pair() + [
        {"role": "user", "content": "and now a brand new question"},
        {"role": "assistant", "content": [{"type": "text", "text": "answer"}]},
    ]
    # The synthetic user half must never look like a real user turn (it would
    # become a compaction boundary and orphan its tool_result from the call).
    pair_user = msgs[4]
    assert compaction.is_real_user_turn(pair_user) is False
    head, tail = compaction.partition(msgs)
    assert tail and tail[0] == msgs[5], "boundary must be the real user turn"
    # Under an aggressive tail cap the cut may move forward, but never between
    # the synthetic call and its result.
    for cap in (1, 10, 50, 200):
        head, tail = compaction.partition(msgs, tail_max_tokens=cap)
        if tail and isinstance(tail[0].get("content"), list):
            first = tail[0]["content"][0]
            assert first.get("type") != "tool_result", (
                f"cap={cap} orphaned a tool_result at the tail start"
            )


def test_pair_never_reaches_the_palace_archive() -> None:
    """Both halves are harness scaffolding: the archive path must drop them,
    or fires pollute conversation memory."""
    from harness.palace import _is_synthetic
    pair = _recall_fire_messages(MATCHES, FIRE, as_tool_exchange=True)
    assert all(_is_synthetic(m) for m in pair)
    old_style = _recall_fire_messages(MATCHES, FIRE, as_tool_exchange=False)[0]
    assert _is_synthetic(old_style)


def test_open_memory_resolves_a_recall_id() -> None:
    """A model that opens the [bracketed] rule id must still land on the
    backing memory, not a dead end that reads as 'no memory exists'."""
    from unittest.mock import AsyncMock, patch
    from harness import memory_access

    orig = memory_access.open_memory

    async def fake_texts(ids):
        return {}  # the recall id is not a curated memory

    with patch("harness.consolidation.memory_texts", new=fake_texts), \
         patch("harness.consolidation.memory_ids_by_recall",
               new=AsyncMock(return_value={"6a8f01402b703c3d61fd44b8":
                                           "f0e3af9c5a934c5d980838363d395f13"})), \
         patch("harness.memory_access.open_memory",
               new=AsyncMock(return_value=("USER IDENTITY BODY", ["f0e3"]))):
        text, expanded = asyncio.run(orig("6a8f01402b703c3d61fd44b8"))
    assert "is a recall trigger, not a memory id" in text
    assert "USER IDENTITY BODY" in text
    assert expanded == ["f0e3"]


def test_agent_recall_scan_fires_then_dedupes() -> None:
    """Agent-initiated recall(): fire text on new matches, nothing-new after,
    thought-only fallback for a recall-only round."""
    from types import SimpleNamespace
    from unittest.mock import patch
    from harness.agent import GaladrielAgent, _RECALL_NOTHING_NEW

    scanned = []

    def fake_scan(text, recalls, exclude_texts=None, segments=None):
        scanned.append([s["source"] for s in (segments or [])])
        return [{"recall_id": "r1", "matched_chunk": "beta"}]

    async def passthrough(channel_id, matches, q):
        return matches

    async def noop(*a, **k):
        return None

    async def build_fire(self_, matches, **k):
        return FIRE

    fake_self = SimpleNamespace(
        _log_retrieval_event=noop,
        _build_recall_fire=lambda *a, **k: build_fire(*a, **k),
    )
    notified: set = set()
    kwargs = dict(
        active_recalls=[{"recall_id": "r1"}],
        notified_recall_ids=notified,
        turn_thought="checking beta notes",
        assistant_content=[],
        tool_blocks=[{"id": "r", "name": "recall"},
                     {"id": "f", "name": "read_file"}],
        tool_results=[{"type": "tool_result", "tool_use_id": "f",
                       "content": "beta project notes with plenty of prose"}],
        messages=[],
    )
    with patch("harness.recall.scan_text_for_recalls", fake_scan), \
         patch("harness.agent._verify_and_select_recalls", passthrough), \
         patch("harness.agent._log_recall_fire", noop):
        out1, fired1, scanned1 = asyncio.run(
            GaladrielAgent._agent_recall_scan(fake_self, "main", **kwargs))
        assert out1 == FIRE
        assert fired1 == ["r1"] and scanned1
        assert notified == {"r1"}
        out2, fired2, _ = asyncio.run(
            GaladrielAgent._agent_recall_scan(fake_self, "main", **kwargs))
        assert out2 == _RECALL_NOTHING_NEW, "already-notified must dedupe"
        assert fired2 == []
        # Recall-only round: builder yields nothing; thought alone is scanned.
        kwargs2 = {**kwargs, "notified_recall_ids": set(),
                   "tool_blocks": [{"id": "r", "name": "recall"}],
                   "tool_results": []}
        asyncio.run(
            GaladrielAgent._agent_recall_scan(fake_self, "main", **kwargs2))
        assert scanned[-1] == ["thought"], scanned[-1]
        # Mixed bookkeeping round (recall + learn): the gate must hold — the
        # tune/learn narration re-fires the recall being tuned if scanned.
        kwargs3 = {**kwargs, "notified_recall_ids": set(),
                   "tool_blocks": [{"id": "r", "name": "recall"},
                                   {"id": "l", "name": "learn"}],
                   "tool_results": []}
        before = len(scanned)
        out3, _, _ = asyncio.run(
            GaladrielAgent._agent_recall_scan(fake_self, "main", **kwargs3))
        assert out3 == _RECALL_NOTHING_NEW and len(scanned) == before, (
            "mixed excluded-tool round must not scan the thought"
        )


# ─── live: replay a real pair through the provider ───────────────────


async def _run_live() -> int:
    from harness.providers.bedrock_mantle_provider import BedrockMantleProvider
    from harness.tools import TOOL_DEFINITIONS as DEFS

    provider = BedrockMantleProvider()
    tools = [t for t in DEFS if t["name"] in ("read_file", "memory", "recall")]
    assert len(tools) == 3
    from harness.memory import RECALL_STABLE_SECTION
    system = [{"type": "text",
               "text": "You are a terse coding agent. Follow your own earlier "
                       "plan and answer the user's actual request.\n\n"
                       + RECALL_STABLE_SECTION}]

    async def drive(msgs, feed):
        calls = []
        for _ in range(4):
            r = await provider.create_message(
                model="gpt-oss-20b", max_tokens=1500, messages=msgs,
                system=system, tools=tools, thinking=True, effort="low",
                temperature=0.0)
            if r.stop_reason != "tool_use":
                text = "".join(b.text for b in r.content if b.type == "text")
                return calls, text
            content = [b.model_dump(exclude_none=True) for b in r.content]
            asst = {"role": "assistant", "content": content}
            if r.thought:
                asst["_thought"] = r.thought
            msgs.append(asst)
            results = []
            for b in r.content:
                if b.type != "tool_use":
                    continue
                calls.append((b.name, b.input))
                results.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": feed(b.name, b.input)})
            msgs.append({"role": "user", "content": results})
        return calls, "(CAP EXCEEDED)"

    # 1. Irrelevant fire: plan must complete, memory must stay closed.
    calls, text = await drive(
        _cascade_with_pair(),
        lambda n, i: "Nothing new to scan since the last automatic check."
        if n == "recall" else "(unused)")
    assert "ORCHID-4417" in text, f"plan lost: {text[:200]!r}"
    assert not any(n == "memory" for n, _ in calls), f"derailed: {calls}"
    print("  live: irrelevant fire ignored, plan completed")

    # 2. Relevant fire: the pointed-at file must actually be read.
    relevant = (
        "- The project notes also include notes/extra.md, which summaries "
        "often miss; when summarising project notes, read notes/extra.md "
        "too.\n"
        '  matched your own thought: "then summarise per my plan."'
    )
    msgs = _cascade_with_pair()
    msgs[-1]["content"][0]["content"] = relevant
    calls, text = await drive(
        msgs,
        lambda n, i: "# notes/extra.md\nGamma side-project. 2 tasks open.\n"
        if n == "read_file" else "Nothing new to scan.")
    assert any(n == "read_file" and "extra" in (i or {}).get("path", "")
               for n, i in calls), f"relevant fire not acted on: {calls}"
    assert "ORCHID-4417" in text, f"plan lost after acting: {text[:200]!r}"
    print("  live: relevant fire acted on, plan still completed")

    await provider.client.close()
    return 2


def main() -> int:
    offline = [
        test_pair_shape_and_linkage,
        test_fallback_single_user_message,
        test_transport_gate_follows_supports_tools_and_provider,
        test_fire_text_extraction_both_transports,
        test_recall_is_scan_excluded_and_defined,
        test_scan_segments_exclude_recall_payload,
        test_wire_serialization_is_legal_openai,
        test_turn_start_fire_keeps_the_dynamic_tail,
        test_compaction_never_splits_the_pair,
        test_pair_never_reaches_the_palace_archive,
        test_open_memory_resolves_a_recall_id,
        test_agent_recall_scan_fires_then_dedupes,
    ]
    for test in offline:
        test()
        print(f"PASS: {test.__name__}")

    if not os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
        print(f"{len(offline)}/{len(offline)} offline tests passed "
              f"(live tests skipped: no AWS_BEARER_TOKEN_BEDROCK)")
        return 0

    live_count = asyncio.run(_run_live())
    print("PASS: live_pair_replay")
    total = len(offline) + live_count
    print(f"{total}/{total} tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

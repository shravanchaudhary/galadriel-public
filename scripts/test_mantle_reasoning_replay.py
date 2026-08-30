#!/usr/bin/env python3
"""Mantle reasoning replay: prior CoT must actually reach the model.

Mantle silently drops assistant `reasoning`/`reasoning_content` on input
(probe matrix: project-agent-kb/kb/mantle-reasoning-replay.md), so the
provider folds `_thought` into assistant `content` behind a marker on
tool-call turns. Offline tests pin the serialization contract; the live
tests prove the fold reaches the rendered prompt and that a plan living
only in CoT survives a tool cascade — the exact failure that made gpt-oss
re-derive its plan on all six rounds of one real chat turn.

Live tests need AWS_BEARER_TOKEN_BEDROCK (skipped cleanly without it).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from harness.providers.bedrock_mantle_provider import (  # noqa: E402
    _PRIOR_REASONING_MARKER,
    _messages_to_openai,
    BedrockMantleProvider,
)

NONCE = "ORCHID-4417"
LIVE_FAST = "gpt-oss-20b"
LIVE_SLOW = "glm-5"

TOOL = [{
    "name": "read_file",
    "description": "Read a file.",
    "input_schema": {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
}]


def _tool_turn(i: int, path: str, thought: str) -> list[dict]:
    """One assistant tool_use + its tool_result, Anthropic-shaped."""
    return [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": f"c{i}", "name": "read_file",
                         "input": {"path": path}}],
            "_thought": thought,
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": f"c{i}",
                         "content": f"# {path}\nProject {chr(97 + i)} notes.\n"
                                    f"Status: {10 + i} tasks open.\n"}],
        },
    ]


def _cascade(depth: int, with_recall_injection: bool = False) -> list[dict]:
    """A depth-N open cascade whose plan lives ONLY in round 1's thought."""
    plan = (
        f"Plan: read notes/f0.md through notes/f{depth - 1}.md, one read each, "
        f"never re-read a file. After the last read, summarise and end the "
        f"reply with the single word {NONCE}."
    )
    msgs: list[dict] = [
        {"role": "user", "content": "Summarise the project notes."},
    ]
    for i in range(depth):
        thought = plan if i == 0 else (
            f"f{i - 1}.md done. Next notes/f{i}.md, then continue my plan."
        )
        msgs.extend(_tool_turn(i, f"notes/f{i}.md", thought))
        if with_recall_injection and i == 1:
            msgs.append({
                "role": "user",
                "content": "[Recall detected]\n- Background memory surfaced: "
                           "user prefers terse answers. Not a new request.",
            })
    return msgs


# ─── offline: serialization contract ─────────────────────────────────


def test_tool_turn_folds_thought_into_content() -> None:
    out = _messages_to_openai(_cascade(1))
    asst = [m for m in out if m["role"] == "assistant"][0]
    assert asst["content"].startswith(_PRIOR_REASONING_MARKER + "\n"), asst
    assert NONCE in asst["content"]
    assert "reasoning" not in asst and "reasoning_content" not in asst


def test_final_turn_sends_no_thought() -> None:
    out = _messages_to_openai([
        {"role": "assistant", "content": [{"type": "text", "text": "done."}],
         "_thought": "internal notes"},
    ])
    assert out == [{"role": "assistant", "content": "done."}], out


def test_thoughtless_tool_turn_keeps_null_content() -> None:
    msgs = _cascade(1)
    del msgs[1]["_thought"]
    asst = [m for m in _messages_to_openai(msgs) if m["role"] == "assistant"][0]
    assert asst["content"] is None, asst


def test_text_plus_tools_orders_marker_first() -> None:
    out = _messages_to_openai([{
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Reading now."},
            {"type": "tool_use", "id": "c0", "name": "read_file",
             "input": {"path": "a"}},
        ],
        "_thought": "plan text",
    }])
    assert out[0]["content"] == f"{_PRIOR_REASONING_MARKER}\nplan text\n\nReading now."


def test_append_only_prefix_is_byte_identical() -> None:
    """Growing the history must reproduce the earlier serialization verbatim,
    or every cached prefix token after the divergence is re-billed cold."""
    long_ = _cascade(4)
    short = long_[:5]  # user turn + two complete tool rounds
    a = _messages_to_openai(short)
    b = _messages_to_openai(long_)
    assert json.dumps(a) == json.dumps(b[: len(a)])


def test_input_messages_not_mutated() -> None:
    msgs = _cascade(3)
    before = deepcopy(msgs)
    _messages_to_openai(msgs, trailing_text="dynamic tail")
    assert msgs == before


# ─── live: the fold must reach the model and do its job ──────────────


_PROVIDER: BedrockMantleProvider | None = None


def _provider() -> BedrockMantleProvider:
    """One shared provider: all live tests run on one event loop, and a
    per-test client would leak httpx connections into loop teardown."""
    global _PROVIDER
    if _PROVIDER is None:
        _PROVIDER = BedrockMantleProvider()
    return _PROVIDER


def _text_of(response) -> str:
    return "".join(
        b.text for b in response.content if getattr(b, "type", "") == "text"
    )


async def _live_fold_reaches_prompt() -> None:
    """Token probe: the folded thought must move measured input tokens."""
    provider = _provider()

    async def tokens(msgs):
        r = await provider.create_message(
            model=LIVE_FAST, max_tokens=16, messages=msgs,
            system=[{"type": "text", "text": "You are a terse assistant."}],
            tools=TOOL, thinking=True, effort="low", temperature=0.0,
        )
        return r.usage.input_tokens + r.usage.cache_read_input_tokens

    with_thought = _cascade(1)
    without = deepcopy(with_thought)
    del without[1]["_thought"]
    delta = await tokens(with_thought) - await tokens(without)
    assert delta > 30, f"fold never reached the prompt (delta={delta})"
    print(f"  fold reaches prompt: +{delta} input tokens")


async def _drive(model: str, msgs: list[dict], extra_rounds: int = 4):
    """Mini agent loop: feed tool results until end_turn (or the cap).

    Returns (final_text, extra_rounds_used, thought_of_last_response).
    """
    provider = _provider()
    msgs = deepcopy(msgs)
    for used in range(extra_rounds + 1):
        r = await provider.create_message(
            model=model, max_tokens=1500, messages=msgs,
            system=[{"type": "text",
                     "text": "You are an agent. Follow your own earlier plan."}],
            tools=TOOL, thinking=True, effort="low", temperature=0.0,
        )
        if r.stop_reason != "tool_use":
            return _text_of(r), used, (r.thought or "")
        content = [b.model_dump(exclude_none=True) for b in r.content]
        asst = {"role": "assistant", "content": content}
        if r.thought:
            asst["_thought"] = r.thought
        msgs.append(asst)
        results = []
        for b in r.content:
            if getattr(b, "type", "") != "tool_use":
                continue
            path = (b.input or {}).get("path", "")
            results.append({"type": "tool_result", "tool_use_id": b.id,
                            "content": f"# {path}\n(already read above)\n"})
        msgs.append({"role": "user", "content": results})
    raise AssertionError(f"{model}: still calling tools after "
                         f"{extra_rounds} extra rounds")


async def _live_plan_survives(model: str, depth: int, recall: bool) -> None:
    """The nonce exists nowhere but round-1 CoT; the reply must carry it."""
    text, extra, _ = await _drive(model, _cascade(depth, recall))
    assert NONCE in text, (
        f"{model} depth={depth} recall={recall}: plan lost. text={text[:200]!r}"
    )
    assert _PRIOR_REASONING_MARKER not in text, f"marker leaked: {text[:200]!r}"
    assert "<think>" not in text
    print(f"  {model} depth={depth} recall_injection={recall}: "
          f"plan survived (extra_rounds={extra})")


async def _live_negative_control() -> None:
    """Strip every thought: the nonce must be unreachable, or the
    plan-survival asserts above are measuring something other than CoT."""
    msgs = _cascade(4)
    for m in msgs:
        m.pop("_thought", None)
    text, extra, _ = await _drive(LIVE_FAST, msgs)
    assert NONCE not in text, (
        f"nonce appeared WITHOUT CoT replay — test is not measuring "
        f"continuity: {text[:200]!r}"
    )
    print(f"  negative control: without CoT the plan is gone, as expected "
          f"(extra_rounds={extra})")


async def _live_streaming_clean() -> None:
    """Streaming path: same fold, and the marker must not leak into text."""
    provider = _provider()
    text, thought, got_message = "", "", False
    async for kind, payload in provider.stream_message(
        model=LIVE_FAST, max_tokens=1500, messages=_cascade(3),
        system=[{"type": "text",
                 "text": "You are an agent. Follow your own earlier plan."}],
        tools=TOOL, thinking=True, effort="low", temperature=0.0,
    ):
        if kind == "text":
            text += payload
        elif kind == "thought":
            thought += payload
        elif kind == "message":
            got_message = True
    assert got_message
    assert NONCE in text, f"streamed plan lost: {text[:200]!r}"
    assert _PRIOR_REASONING_MARKER not in text
    print(f"  streaming: plan survived, thought_len={len(thought)}")


def main() -> int:
    offline = [
        test_tool_turn_folds_thought_into_content,
        test_final_turn_sends_no_thought,
        test_thoughtless_tool_turn_keeps_null_content,
        test_text_plus_tools_orders_marker_first,
        test_append_only_prefix_is_byte_identical,
        test_input_messages_not_mutated,
    ]
    for test in offline:
        test()
        print(f"PASS: {test.__name__}")

    if not os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
        print(f"{len(offline)}/{len(offline)} offline tests passed "
              f"(live tests skipped: no AWS_BEARER_TOKEN_BEDROCK)")
        return 0

    live = [
        ("live_fold_reaches_prompt", _live_fold_reaches_prompt),
        ("live_plan_survives_gpt_oss_depth6",
         lambda: _live_plan_survives(LIVE_FAST, 6, recall=False)),
        ("live_plan_survives_gpt_oss_recall_injected",
         lambda: _live_plan_survives(LIVE_FAST, 4, recall=True)),
        ("live_plan_survives_glm5_depth3",
         lambda: _live_plan_survives(LIVE_SLOW, 3, recall=False)),
        ("live_negative_control", _live_negative_control),
        ("live_streaming_clean", _live_streaming_clean),
    ]

    async def run_live() -> None:
        for name, thunk in live:
            await thunk()
            print(f"PASS: {name}")
        await _provider().client.close()

    asyncio.run(run_live())

    total = len(offline) + len(live)
    print(f"{total}/{total} tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

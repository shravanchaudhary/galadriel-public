#!/usr/bin/env python3
"""Local tests for windowed compaction (`harness/compaction.partition`).

The cut decides what survives a compaction verbatim, so it is worth pinning
down: an in-flight instruction must never be summarized out from under the
model, injected user-role messages must never be mistaken for a human turn, and
a runaway tool cascade must still be compressible. Pure functions — no API key
or network needed.

Usage:
    venv/bin/python scripts/test_compaction_window.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness.agent import GaladrielAgent, MAIN_CHANNEL_ID  # noqa: E402
from harness.compaction import is_real_user_turn, partition  # noqa: E402


def user(text: str) -> dict:
    return {"role": "user", "content": text}


def assistant(text: str) -> dict:
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


def tool_call(name: str = "read_file") -> dict:
    return {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "t1", "name": name, "input": {}}],
    }


def tool_result(text: str) -> dict:
    return {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "t1", "content": text}],
    }


def recall_fire(text: str = "[Recall detected]") -> dict:
    return {"role": "user", "content": text, "kind": "recall_fire"}


def truncation_notice() -> dict:
    return {
        "role": "user",
        "content": "[SYSTEM] Your previous response was cut off",
        "kind": "truncation_notice",
    }


def check(label: str, got, want) -> bool:
    if got == want:
        print(f"PASS: {label}")
        return True
    print(f"FAIL: {label}\n  got:  {got}\n  want: {want}")
    return False


def test_real_user_turn() -> bool:
    cases = [
        ("plain user message", user("hi"), True),
        ("assistant", assistant("hi"), False),
        ("tool result", tool_result("data"), False),
        ("recall fire", recall_fire(), False),
        ("truncation notice", truncation_notice(), False),
        (
            "multimodal user message",
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look"},
                    {"type": "image", "source": {"type": "base64", "data": "x"}},
                ],
            },
            True,
        ),
    ]
    return all(check(f"is_real_user_turn: {n}", is_real_user_turn(m), w) for n, m, w in cases)


def test_new_message_keeps_only_itself() -> bool:
    """Auto-compaction on a new turn: everything before it summarizes away."""
    msgs = [
        user("old task"),
        assistant("old answer"),
        user("new task"),
    ]
    head, tail = partition(msgs)
    return check("new message: tail is the new turn", (len(head), tail), (2, [msgs[2]]))


def test_manual_compact_summarizes_everything() -> bool:
    msgs = [user("task"), assistant("answer"), user("follow up")]
    head, tail = partition(msgs, full=True)
    return check("manual /compact: no tail", (len(head), tail), (3, []))


def test_injected_messages_are_not_boundaries() -> bool:
    """A naive last-user scan would cut on these and preserve nothing real."""
    msgs = [
        user("older"),
        assistant("older answer"),
        user("the real task"),
        recall_fire(),
        tool_call(),
        tool_result("file contents"),
        truncation_notice(),
    ]
    head, tail = partition(msgs)
    return check(
        "injected messages: cut lands on the human turn",
        (len(head), len(tail), tail[0]["content"]),
        (2, 5, "the real task"),
    )


def test_midcascade_keeps_the_whole_turn() -> bool:
    """Under the cap, a cascade stays verbatim however deep it got."""
    msgs = [user("older"), assistant("older answer"), user("the real task")]
    for i in range(10):
        msgs.append(tool_call())
        msgs.append(tool_result(f"result {i}"))
    head, tail = partition(msgs, tail_max_tokens=100_000)
    return check(
        "mid-cascade under cap: whole turn survives",
        (len(head), len(tail)),
        (2, 21),
    )


def test_runaway_cascade_is_capped() -> bool:
    """Over the cap the cut moves into the cascade, or compaction frees nothing."""
    msgs = [user("older"), assistant("older answer"), user("the real task")]
    for i in range(20):
        msgs.append(tool_call())
        msgs.append(tool_result("x" * 40_000))  # ~10k tokens each
    head, tail = partition(msgs, tail_max_tokens=50_000)
    ok = check("runaway cascade: head grew past the user turn", len(head) > 2, True)
    ok = check("runaway cascade: tail was trimmed", len(tail) < 41, True) and ok
    # Whatever survives must be sendable: a tool_result may not lead the tail.
    ok = check("runaway cascade: tail starts on an assistant turn",
               tail[0]["role"], "assistant") and ok
    return ok

def test_cap_never_empties_the_tail() -> bool:
    """One oversized message must not leave the buffer with nothing to send."""
    msgs = [user("older"), assistant("older answer"), user("x" * 400_000)]
    head, tail = partition(msgs, tail_max_tokens=1000)
    return check("oversized turn: tail kept anyway", (len(head), len(tail)), (2, 1))


def test_no_human_turn_summarizes_everything() -> bool:
    """Worker ticks seed a buffer with no human message in it."""
    msgs = [recall_fire(), tool_call(), tool_result("data")]
    head, tail = partition(msgs)
    return check("machine-seeded buffer: no tail", (len(head), tail), (3, []))


def test_empty_buffer() -> bool:
    return check("empty buffer", partition([]), ([], []))


def _stub_agent(messages: list) -> GaladrielAgent:
    agent = GaladrielAgent.__new__(GaladrielAgent)
    agent.conversations = {MAIN_CHANNEL_ID: messages}
    agent.model = "gemini-3.1-pro-preview"
    agent._channel_models = {MAIN_CHANNEL_ID: agent.model}
    agent.compact_threshold = 300_000
    agent._compaction_summary = {}
    agent._last_input_tokens = {MAIN_CHANNEL_ID: 400_000}
    agent._last_archived_len = {MAIN_CHANNEL_ID: 7}
    agent._notified_recall_ids = {MAIN_CHANNEL_ID: {"r-old", "r-live"}}
    agent._session_id = {}
    agent._session_segments = {}
    agent.experience = None  # _record_experience_event is best-effort
    return agent


def _run_compaction(agent: GaladrielAgent, **kwargs) -> dict:
    """Compact MAIN with the palace and the LLM stubbed out.

    Compaction never runs memory consolidation (see on_episode_end) so there
    is nothing recall/learning-related left to stub here.
    """
    snapshot = {
        "snapshot": "GOAL: ship it",
        "messages_before": 0,
        "tokens_before": 100,
        "tokens_after": 10,
    }
    with patch("harness.palace.archive_conversation_durable", return_value=None), \
         patch.object(GaladrielAgent, "_live_summarizer", return_value={}), \
         patch(
             "harness.compaction.compact_to_snapshot",
             new=AsyncMock(return_value=dict(snapshot)),
         ):
        return asyncio.run(agent.compact_channel(MAIN_CHANNEL_ID, **kwargs))


def test_compaction_keeps_the_current_turn_in_the_buffer() -> bool:
    messages = [
        user("older"),
        assistant("older answer"),
        recall_fire(),
        user("the real task"),
        tool_call(),
        tool_result("data"),
    ]
    messages[2]["matched_recall_ids"] = ["r-old"]
    agent = _stub_agent(messages)
    result = _run_compaction(agent, full=False)

    ok = check("windowed compact: reported compacted", result["compacted"], True)
    ok = check(
        "windowed compact: buffer is the surviving tail",
        [m.get("content") for m in agent.conversations[MAIN_CHANNEL_ID]][0],
        "the real task",
    ) and ok
    ok = check(
        "windowed compact: tail length",
        len(agent.conversations[MAIN_CHANNEL_ID]), 3,
    ) and ok
    ok = check(
        "windowed compact: snapshot stored for the system block",
        agent._compaction_summary[MAIN_CHANNEL_ID], "GOAL: ship it",
    ) and ok
    ok = check(
        "windowed compact: summarized recall fires may fire again",
        agent._notified_recall_ids[MAIN_CHANNEL_ID], set(),
    ) and ok
    ok = check(
        "windowed compact: unarchived tail awaits checkpoint",
        agent._last_archived_len[MAIN_CHANNEL_ID], 0,
    ) and ok
    return check(
        "windowed compact: input measurement reset",
        MAIN_CHANNEL_ID in agent._last_input_tokens, False,
    ) and ok


def test_compaction_keeps_recalls_visible_in_the_tail_suppressed() -> bool:
    messages = [
        user("older"),
        assistant("older answer"),
        user("the real task"),
        recall_fire(),
    ]
    messages[3]["matched_recall_ids"] = ["r-live"]
    agent = _stub_agent(messages)
    _run_compaction(agent, full=False)
    return check(
        "surviving recall fire stays suppressed",
        agent._notified_recall_ids[MAIN_CHANNEL_ID], {"r-live"},
    )


def test_manual_compaction_empties_the_buffer() -> bool:
    messages = [user("task"), assistant("answer")]
    agent = _stub_agent(messages)
    result = _run_compaction(agent, full=True)
    ok = check("manual compact: reported compacted", result["compacted"], True)
    return check(
        "manual compact: buffer cleared",
        agent.conversations[MAIN_CHANNEL_ID], [],
    ) and ok


def test_compaction_declines_when_only_the_current_turn_exists() -> bool:
    messages = [user("the only task"), tool_call(), tool_result("data")]
    agent = _stub_agent(messages)
    result = _run_compaction(agent, full=False)
    ok = check("nothing older: declined", result["compacted"], False)
    return check("nothing older: buffer untouched", len(messages), 3) and ok




def main() -> int:
    results = [
        test_real_user_turn(),
        test_new_message_keeps_only_itself(),
        test_manual_compact_summarizes_everything(),
        test_injected_messages_are_not_boundaries(),
        test_midcascade_keeps_the_whole_turn(),
        test_runaway_cascade_is_capped(),
        test_cap_never_empties_the_tail(),
        test_no_human_turn_summarizes_everything(),
        test_empty_buffer(),
        test_compaction_keeps_the_current_turn_in_the_buffer(),
        test_compaction_keeps_recalls_visible_in_the_tail_suppressed(),
        test_manual_compaction_empties_the_buffer(),
        test_compaction_declines_when_only_the_current_turn_exists(),
    ]
    passed = sum(results)
    print(f"\n{passed}/{len(results)} tests passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

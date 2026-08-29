"""Focused tests for the durable conversation inbox."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.pop("MONGO_URI", None)
os.environ.pop("MONGO_DB", None)

from harness.conversation_queue import ConversationQueue, MemoryConversationStore  # noqa: E402


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def store_tests():
    store = MemoryConversationStore()
    first, created = store.enqueue(
        channel="main", source="tower", external_dedupe_key="request-1",
        sender={"id": "u1"}, payload="one", display_text="one",
        reply_target={"page": "tower"}, overlay="page context",
    )
    duplicate, duplicate_created = store.enqueue(
        channel="main", source="tower", external_dedupe_key="request-1",
        sender={"id": "u1"}, payload="ignored", display_text="ignored",
        reply_target=None, overlay=None,
    )
    check(created and not duplicate_created, "external key should deduplicate")
    check(first["id"] == duplicate["id"], "dedupe should return the original item")
    required = {
        "id", "channel", "sequence", "source", "external_dedupe_key",
        "sender", "payload", "display_text", "reply_target", "overlay",
        "state", "revision", "created_at", "updated_at", "claimed_at",
        "completed_at", "request_context",
    }
    check(required <= set(first), "queue item is missing required fields")

    second, _ = store.enqueue(
        channel="main", source="tower", external_dedupe_key="request-2",
        sender=None, payload="two", display_text="two",
        reply_target=None, overlay=None,
    )
    edited = store.edit(
        first["id"], first["revision"],
        {"display_text": "one edited", "payload": "one edited"},
    )
    check(edited and edited["revision"] == 2, "pending edit should increment revision")
    check(
        store.edit(first["id"], first["revision"], {"display_text": "stale"}) is None,
        "stale edit must fail",
    )
    reordered = store.reorder("main", [
        {"id": second["id"], "revision": second["revision"]},
        {"id": edited["id"], "revision": edited["revision"]},
    ])
    check([item["id"] for item in reordered] == [second["id"], first["id"]], "reorder failed")
    deleted = store.delete(first["id"], reordered[1]["revision"])
    check(deleted and deleted["state"] == "deleted", "pending delete failed")

    claimed = store.claim_pending("main", "consumer-a")
    check([item["id"] for item in claimed] == [second["id"]], "claim ordering failed")
    check(store.edit(second["id"], reordered[0]["revision"], {"display_text": "no"}) is None,
          "claimed items must not be editable")
    store.recover()
    check(store.list_pending("main")[0]["id"] == second["id"], "restart recovery lost claim")

    stopped_store = MemoryConversationStore()
    stopped, _ = stopped_store.enqueue(
        channel="stopped", source="tower", external_dedupe_key=None,
        sender=None, payload="do not replay", display_text="do not replay",
        reply_target=None, overlay=None,
    )
    stopped_store.claim_pending("stopped", "consumer")
    stopped_store.pause("stopped")
    stopped_store.recover()
    recovered = stopped_store.get(stopped["id"])
    check(
        recovered["state"] == "cancelled" and not stopped_store.list_pending("stopped"),
        "paused active claim must be cancelled rather than replayed after restart",
    )


async def queue_tests():
    calls = []

    async def runner(payload, **kwargs):
        calls.append((payload, kwargs))
        return "done"

    queue = ConversationQueue(runner, store=MemoryConversationStore())
    first = await queue.enqueue(
        "one", channel="main", source="tower", external_dedupe_key="one",
        display_text="one",
        request_context={"source": "tower", "actor_id": "owner", "trusted": True},
    )
    second = await queue.enqueue(
        "two", channel="main", source="slack", external_dedupe_key="two",
        display_text="two",
        request_context={
            "source": "slack", "actor_id": "member", "trusted": False,
            "replika_type": "organization",
        },
    )
    results = await asyncio.gather(
        queue.await_item(first["id"]), queue.await_item(second["id"]),
    )
    check(results == ["", "done"], "only the newest batch trigger should receive the reply")
    check(len(calls) == 1, "one consumer should merge pending items into one turn")
    merged = calls[0][0]
    check(
        isinstance(merged, list)
        and merged[0]["text"] == "one"
        and merged[-1]["text"] == "two",
        "merged payload order is incorrect",
    )
    check(calls[0][1]["run_source"] == "slack", "newest item should trigger the batch")
    check(
        calls[0][1]["request_context"]["actor_id"] == "member"
        and not calls[0][1]["request_context"]["trusted"],
        "older trusted actor must not elevate a newer untrusted trigger",
    )
    duplicate = await queue.enqueue(
        "ignored", channel="main", source="tower", external_dedupe_key="one",
        display_text="ignored",
    )
    check(duplicate["id"] == first["id"], "completed dedupe should return original item")
    check(await queue.await_item(duplicate["id"]) == "", "completed routed result should be durable")
    check(len(calls) == 1, "completed dedupe must not execute a second turn")

    reverse_calls = []

    async def reverse_runner(payload, **kwargs):
        reverse_calls.append(kwargs)
        return "trusted"

    reverse = ConversationQueue(reverse_runner, store=MemoryConversationStore())
    untrusted = await reverse.enqueue(
        "old member", channel="reverse", source="slack",
        request_context={
            "source": "slack", "actor_id": "member", "trusted": False,
            "replika_type": "organization",
        },
    )
    trusted = await reverse.enqueue(
        "new admin", channel="reverse", source="slack",
        request_context={
            "source": "slack", "actor_id": "admin", "trusted": True,
            "replika_type": "organization",
        },
    )
    await asyncio.gather(reverse.await_item(untrusted["id"]), reverse.await_item(trusted["id"]))
    check(
        reverse_calls[0]["request_context"]["actor_id"] == "admin"
        and reverse_calls[0]["request_context"]["trusted"],
        "newest admin trigger explicitly owns authority despite older untrusted context",
    )

    started = asyncio.Event()
    stop_signal = asyncio.Event()
    finalizer_reached = asyncio.Event()
    release = asyncio.Event()
    stop_calls = []

    async def blocking_runner(payload, **kwargs):
        stop_calls.append(payload)
        started.set()
        if len(stop_calls) == 1:
            await stop_signal.wait()
            finalizer_reached.set()
            return "(Stopped — turn cancelled.)"
        await release.wait()
        return "resumed"

    stop_callback_calls = []

    def cooperative_stop(channel):
        stop_callback_calls.append(channel)
        stop_signal.set()
        return True

    paused_queue = ConversationQueue(
        blocking_runner,
        store=MemoryConversationStore(),
        stop_callback=cooperative_stop,
    )
    old = await paused_queue.enqueue(
        "old", channel="main", source="tower", display_text="old",
    )
    old_waiter = asyncio.create_task(paused_queue.await_item(old["id"]))
    await started.wait()
    second = await paused_queue.enqueue(
        "second", channel="main", source="tower", display_text="second",
    )
    abandoned_waiter = asyncio.create_task(paused_queue.await_item(second["id"]))
    await asyncio.sleep(0)
    abandoned_waiter.cancel()
    try:
        await abandoned_waiter
    except asyncio.CancelledError:
        pass
    check(second["id"] not in paused_queue._waiters, "cancelled waiter leaked")

    check(paused_queue.stop("main"), "stop should cooperatively cancel the active turn")
    check(await old_waiter == "(Stopped — turn cancelled.)", "stop waiter result mismatch")
    await finalizer_reached.wait()
    check(stop_callback_calls == ["main"], "cooperative stop callback was not used")
    old_after_stop = paused_queue.store.get(old["id"])
    check(old_after_stop["state"] == "cancelled", "active item must become cancelled")
    check(
        old_after_stop["result"] == "(Stopped — turn cancelled.)",
        "cancelled item result was not persisted",
    )
    status = paused_queue.status("main")
    check(status["paused"] and status["depth"] == 1, "only post-claim message should remain pending")
    check(
        paused_queue.store.get(second["id"])["state"] == "pending",
        "message arriving during active turn must remain pending",
    )

    started.clear()
    new = await paused_queue.enqueue(
        "new", channel="main", source="tower", display_text="new",
    )
    await started.wait()
    release.set()
    resumed_results = await asyncio.gather(
        paused_queue.await_item(second["id"]),
        paused_queue.await_item(new["id"]),
    )
    check(resumed_results == ["", "resumed"], "resume result routing mismatch")
    check(len(stop_calls) == 2, "next substantive enqueue should restart consumer")
    resumed_payload = stop_calls[1]
    check(
        isinstance(resumed_payload, list)
        and resumed_payload[0]["text"] == "second"
        and resumed_payload[-1]["text"] == "new",
        "resume turn must include post-claim pending and new messages",
    )
    check(
        all(block.get("text") != "old" for block in resumed_payload),
        "cancelled active payload must not be requeued",
    )
    check(not paused_queue.status("main")["paused"], "enqueue should atomically unpause")

    # No callback is an isolated-test fallback: direct task cancellation still
    # finalizes the claimed item and resolves its waiter without requeueing it.
    fallback_started = asyncio.Event()
    fallback_finalizer_reached = asyncio.Event()

    async def fallback_runner(payload, **kwargs):
        fallback_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            fallback_finalizer_reached.set()

    fallback = ConversationQueue(fallback_runner, store=MemoryConversationStore())
    fallback_item = await fallback.enqueue(
        "fallback", channel="test", source="test", display_text="fallback",
    )
    fallback_waiter = asyncio.create_task(fallback.await_item(fallback_item["id"]))
    await fallback_started.wait()
    check(fallback.stop("test"), "fallback stop should cancel its turn task")
    check(await fallback_waiter == "(Stopped — turn cancelled.)", "fallback waiter leaked")
    check(fallback_finalizer_reached.is_set(), "direct cancellation skipped runner finalization")
    check(
        fallback.store.get(fallback_item["id"])["state"] == "cancelled",
        "fallback cancellation must finalize the claim",
    )


if __name__ == "__main__":
    store_tests()
    asyncio.run(queue_tests())
    print("conversation queue tests passed")

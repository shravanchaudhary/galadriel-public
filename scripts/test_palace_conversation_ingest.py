#!/usr/bin/env python3
"""Tests for conversation ingest: speaker halls, chunk budgeting, cursor, search_meta.

Covers the behaviour the previous palace tests could not see, because they were
fully mocked: the chunker's window invariant, span folding and synthetic-message
exclusion, the durable archive cursor's claim arithmetic (the duplicate-drawer
fix), and the filter grammar behind search_meta.

Usage:
    python scripts/test_palace_conversation_ingest.py
"""

from __future__ import annotations

import copy
import datetime
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# The store module reads these at import time for its client; the tests below
# never open a connection.
os.environ.setdefault("MONGO_URI", "mongodb://localhost:1/unused")
os.environ.setdefault("MONGO_DB", "unused")

from harness import mongo_palace as store  # noqa: E402
from harness import palace  # noqa: E402
from harness.palace_cursor import _claim  # noqa: E402


# Archive stamps come from the agent's configured timezone, which is a Mongo
# read. These tests are about file layout, not the clock.
_FIXED_NOW = datetime.datetime(2026, 8, 28, 10, 0, 0, tzinfo=datetime.timezone.utc)


def _fixed_clock():
    return patch.object(palace, "_agent_stamp", return_value=_FIXED_NOW)


def _honest_token_count(text: str) -> int:
    """Token count with truncation disabled — the model's tokenizer caps at 512
    and so cannot prove a chunk is inside the window."""
    tokenizer = copy.deepcopy(store._tokenizer())
    tokenizer.no_truncation()
    return len(tokenizer.encode(text, add_special_tokens=False).ids)


class ChunkerTests(unittest.TestCase):
    """The chunker exists to keep every chunk inside the embedder's window.

    The previous 3000-char chunker did not: against a 256-token model most of
    each chunk was past the truncation point and invisible to vector search.
    """

    def test_every_chunk_fits_the_window(self):
        cases = {
            "prose": "The agent archived the conversation into the palace. " * 500,
            "no punctuation": "word " * 4000,
            "markdown and json": (
                "## assistant\n\nHere is the result.\n\n### tool_result (id=t1)\n\n"
                + '{"rows":[' + ",".join(f'{{"id":{i}}}' for i in range(800)) + "]}\n\n"
            ) * 3,
            "single unsplittable blob": '{"k":"' + "x" * 40000 + '"}',
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                chunks = store._chunks(text)
                self.assertTrue(chunks)
                for chunk in chunks:
                    self.assertLessEqual(
                        _honest_token_count(chunk), store.EMBED_MAX_TOKENS,
                        f"{name}: chunk exceeds the {store.EMBED_MAX_TOKENS}-token window",
                    )

    def test_no_content_is_dropped(self):
        text = "Alpha sentence one. Bravo sentence two. Charlie sentence three. " * 60
        joined = " ".join(store._chunks(text))
        for marker in ("Alpha", "Bravo", "Charlie"):
            self.assertGreaterEqual(joined.count(marker), text.count(marker))

    def test_empty_input_yields_no_chunks(self):
        self.assertEqual(store._chunks(""), [])
        self.assertEqual(store._chunks("   \n  "), [])


class SpanFoldingTests(unittest.TestCase):
    """Halls in room=conversations are the speaker, not a topic."""

    def _messages(self):
        return [
            {"role": "user", "content": "[SYSTEM:WORKER_TICK] board meta"},
            {"role": "user", "content": "the human asks something"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "reasoning"},
                {"type": "tool_use", "name": "read_file", "id": "t1", "input": {"p": "x"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "file body"},
            ]},
            {"role": "assistant", "content": "the answer"},
            {"role": "user", "content": "[Recall detected]\n- [r1] x", "kind": "recall_fire"},
            {"role": "user", "content": "a second human turn"},
        ]

    def test_speaker_partitioning(self):
        spans = palace._conversation_spans(self._messages())
        self.assertEqual([s["hall"] for s in spans], ["user", "assistant", "user"])

    def test_tool_traffic_is_assistant_not_user(self):
        # tool_result blocks arrive as role=user; they are agent traffic.
        spans = palace._conversation_spans(self._messages())
        assistant = next(s for s in spans if s["hall"] == "assistant")
        self.assertIn("tool_result", assistant["text"])
        self.assertIn("tool_use", assistant["text"])
        user_text = " ".join(s["text"] for s in spans if s["hall"] == "user")
        self.assertNotIn("tool_result", user_text)

    def test_synthetic_messages_never_reach_the_palace(self):
        joined = " ".join(s["text"] for s in palace._conversation_spans(self._messages()))
        self.assertNotIn("[SYSTEM:", joined)
        self.assertNotIn("Recall detected", joined)

    def test_dropping_synthetic_does_not_split_a_span(self):
        # A scheduler prompt between two assistant messages must not create two
        # assistant spans out of one continuous stretch of agent output.
        spans = palace._conversation_spans([
            {"role": "assistant", "content": "first"},
            {"role": "user", "content": "[SYSTEM:HEARTBEAT] tick"},
            {"role": "assistant", "content": "second"},
        ])
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0]["hall"], "assistant")

    def test_all_synthetic_slice_stages_nothing(self):
        with tempfile.TemporaryDirectory() as tmp, _fixed_clock():
            result = palace._write_conversation_batch(
                Path(tmp), "worker",
                [{"role": "user", "content": "[SYSTEM:WORKER_TICK] only this"}],
                kind="checkpoint", conversation_id="c1",
            )
        self.assertIsNone(result, "an all-scaffolding slice must not stage a batch")


class BatchManifestTests(unittest.TestCase):
    """The spans manifest is what the miner files from; it must survive a crash."""

    def test_manifest_carries_identity_and_numbering(self):
        messages = [
            {"role": "user", "content": "question " * 400},
            {"role": "assistant", "content": "answer " * 900},
        ]
        with tempfile.TemporaryDirectory() as tmp, _fixed_clock():
            batch = palace._write_conversation_batch(
                Path(tmp), "main", messages, kind="full",
                conversation_id="run-abc",
            )
            self.assertIsNotNone(batch)
            manifest = json.loads((batch / palace.SPANS_FILE).read_text())
            self.assertEqual(manifest["conversation_id"], "run-abc")
            self.assertEqual([s["hall"] for s in manifest["spans"]], ["user", "assistant"])
            # The verbatim .md must still be there: read_episode_segment globs it.
            self.assertTrue(list((batch / "conversations").glob("*.md")))


class CursorTests(unittest.TestCase):
    """The archive cursor is the duplicate-drawer fix.

    The in-memory counter it replaced reset on restart, so the shutdown archiver
    re-mined buffers the scheduler had already checkpointed — 353 drawers
    holding 181 distinct texts when measured.
    """

    def test_fresh_channel_claims_everything(self):
        claim, state = _claim(None, "conv1", 10)
        self.assertEqual((claim["start"], claim["end"]), (0, 10))
        self.assertEqual(state["cursor"], 10)

    def test_second_claim_only_takes_new_messages(self):
        prior = {"conversation_id": "conv1", "cursor": 10}
        claim, state = _claim(prior, "conv1", 14)
        self.assertEqual(claim["start"], 10)
        self.assertEqual(state["cursor"], 14)

    def test_nothing_new_claims_an_empty_slice(self):
        prior = {"conversation_id": "conv1", "cursor": 14}
        claim, _ = _claim(prior, "conv1", 14)
        self.assertEqual(claim["start"], claim["end"])

    def test_new_conversation_resets_cursor_and_numbering(self):
        prior = {"conversation_id": "old-run", "cursor": 30}
        claim, _ = _claim(prior, "new-run", 3)
        self.assertEqual(claim["start"], 0)
        self.assertTrue(claim["reset"])

    def test_shrunk_buffer_never_claims_a_negative_slice(self):
        # Compaction replaces the buffer with a shorter tail.
        prior = {"conversation_id": "conv1", "cursor": 50}
        claim, _ = _claim(prior, "conv1", 12)
        self.assertEqual(claim["start"], 12)
        self.assertEqual(claim["end"], 12)


class ManifestlessBatchTests(unittest.TestCase):
    """A conversation batch with no spans.json must still mine.

    The markdown fallback called `_spans_from_markdown`, which had been deleted
    from the module — so every batch staged by an older build raised NameError,
    was swallowed as a warning, never rmtree'd, and retried forever. The purge
    ran *before* the crash, so each retry also deleted that source's drawers.
    """

    def test_markdown_recovery_helper_lives_in_the_store(self):
        self.assertTrue(
            hasattr(store, "_spans_from_markdown"),
            "mine_directory's markdown fallback calls this; it must be importable "
            "from the store, not only from the migration script",
        )

    def test_legacy_transcript_recovers_speaker_halls(self):
        markdown = (
            "<!-- message 0 -->\n## user\n\n[SYSTEM:HEARTBEAT] scaffolding\n\n"
            "<!-- message 1 -->\n## user\n\nwhat is the threshold\n\n"
            "<!-- message 2 -->\n## assistant\n\n### tool_result (id=t1)\n\nit is 88\n"
        )
        spans = store._spans_from_markdown(markdown)
        self.assertEqual([s["hall"] for s in spans], ["user", "assistant"])
        joined = " ".join(s["text"] for s in spans)
        self.assertNotIn("[SYSTEM:", joined)

    def test_a_batch_without_a_manifest_does_not_raise(self):
        stored = {}
        with tempfile.TemporaryDirectory() as tmp:
            batch = Path(tmp) / "conversation_main_full_2026-08-27T10-00-00"
            (batch / "conversations").mkdir(parents=True)
            (batch / "conversations" / "c.md").write_text(
                "<!-- message 0 -->\n## user\n\nhello\n", encoding="utf-8",
            )
            self.assertFalse((batch / store.SPANS_FILE).exists())
            with patch.object(store, "_purge_source", return_value=0), \
                 patch.object(store, "_reserve_chunk_numbers", return_value=1), \
                 patch.object(store, "_store_chunks",
                              side_effect=lambda rows: stored.setdefault("rows", rows) and 0):
                self.assertTrue(store.mine_directory(batch, agent="test"))
        self.assertTrue(stored.get("rows"), "nothing was filed from the fallback")
        self.assertEqual(stored["rows"][0]["hall"], "user")


class ChunkNumberingTests(unittest.TestCase):
    """Numbering is assigned by the miner, and must not collide across batches.

    Three bugs lived here when numbering was reserved at stage time from a
    *message* count: consecutive checkpoints all started at the same number,
    compaction's cursor reset threw the counter away, and a conversation change
    made the reservation upsert fail with a duplicate-key error (silently
    restarting at 1). Numbering now comes from an atomic counter in the store,
    which is the only layer that knows how many chunks a batch produces.
    """

    def test_counter_arithmetic_is_correct_from_an_absent_document(self):
        # `$inc` on a missing field starts at 0, so the counter stores the LAST
        # number handed out rather than the next one.
        calls = []

        def fake_find_one_and_update(query, update, **kwargs):
            calls.append(update)
            fake_find_one_and_update.total += update["$inc"]["last_chunk"]
            return {"last_chunk": fake_find_one_and_update.total}
        fake_find_one_and_update.total = 0

        collection = unittest.mock.MagicMock()
        collection.find_one_and_update.side_effect = fake_find_one_and_update
        with patch.object(store, "_db", return_value={store.CHUNK_COUNTERS: collection}):
            first = store._reserve_chunk_numbers("conv", 4)
            second = store._reserve_chunk_numbers("conv", 3)
            third = store._reserve_chunk_numbers("conv", 2)
        self.assertEqual(first, 1, "first batch must start at 1")
        self.assertEqual(second, 5, "second batch must continue, not repeat")
        self.assertEqual(third, 8)

    def test_zero_or_missing_conversation_is_safe(self):
        self.assertEqual(store._reserve_chunk_numbers("conv", 0), 1)
        self.assertEqual(store._reserve_chunk_numbers("", 5), 1)


class FilterGrammarTests(unittest.TestCase):
    """search_meta is the navigation path; a bad key must fail loudly."""

    def test_scalar_list_and_range(self):
        built = store.build_filter(search_meta={
            "conversation_id": "abc",
            "hall": ["user", "assistant"],
            "chunk_number": {"from": 3, "to": 9},
        })
        self.assertEqual(built["conversation_id"], "abc")
        self.assertEqual(built["hall"], {"$in": ["user", "assistant"]})
        self.assertEqual(built["chunk_number"], {"$gte": 3, "$lte": 9})

    def test_open_ended_range(self):
        self.assertEqual(
            store.build_filter(search_meta={"chunk_number": {"from": 5}})["chunk_number"],
            {"$gte": 5},
        )

    def test_unknown_field_is_rejected_with_the_valid_list(self):
        with self.assertRaises(store.FilterError) as caught:
            store.build_filter(search_meta={"converstaion_id": "typo"})
        message = str(caught.exception)
        self.assertIn("converstaion_id", message)
        self.assertIn("conversation_id", message, "the error must name the valid fields")

    def test_empty_range_is_rejected(self):
        with self.assertRaises(store.FilterError):
            store.build_filter(search_meta={"chunk_number": {}})

    def test_positional_filters_still_compose(self):
        built = store.build_filter("agent", "conversations", "user", {"channel": "main"})
        self.assertEqual(built, {
            "wing": "agent", "room": "conversations",
            "hall": "user", "channel": "main",
        })


class MarkdownStructureTests(unittest.TestCase):
    """Chunks are read back as prose by the agent, so structure has to survive."""

    def test_paragraph_and_list_breaks_survive_chunking(self):
        markdown = "# Heading\n\nFirst para.\n\n- item one\n- item two\n\nLast para."
        chunk = store._chunks(markdown)[0]
        self.assertIn("\n\n", chunk, "paragraph breaks were flattened")
        self.assertIn("- item one", chunk)
        self.assertIn("- item two", chunk)

    def test_opaque_blob_is_not_budgeted_as_one_token(self):
        # WordPiece collapses any run over 100 chars to a single [UNK], so token
        # count alone let one chunk grow without bound.
        self.assertGreater(store._token_count("x" * 4000), 100)
        self.assertLess(max(len(c) for c in store._chunks("y" * 40000)), 8000)


class ScoringTests(unittest.TestCase):
    """Both arms are absolute, so an exact lexical match can outrank a near miss.

    Under the previous rank-relative fusion the top vector hit always scored
    0.6 and a perfect lexical match was capped at 0.4, so it could never win —
    measured at 46% recall for rare exact tokens (100% after this change).
    """

    def test_weights_let_lexical_beat_a_weak_vector_hit(self):
        weak_vector_only = store.VECTOR_WEIGHT * 0.45
        exact_lexical_plus_ok_vector = (
            store.LEXICAL_WEIGHT * 1.0 + store.VECTOR_WEIGHT * 0.40
        )
        self.assertGreater(exact_lexical_plus_ok_vector, weak_vector_only)

    def test_no_tunable_similarity_threshold_exists(self):
        # Every absolute or corpus-relative cutoff tried here mis-fired once the
        # corpus changed (a real query fell to 0.663 while a random UUID scored
        # 0.714). Reintroducing one is a regression, not a tuning knob.
        self.assertFalse(
            hasattr(store, "MIN_SIMILARITY"),
            "similarity thresholds drift with the corpus — see the note in "
            "mongo_palace.py before adding one back",
        )

    def test_bm25_returns_normalized_and_raw(self):
        documents = [
            {"_id": "a", "text": "zebra quokka narwhal"},
            {"_id": "b", "text": "ordinary filler text here"},
        ]
        normalized, raw = store._bm25("quokka", documents)
        self.assertEqual(max(normalized.values()), 1.0, "normalized always crowns one")
        self.assertGreater(raw["a"], 0.0)
        self.assertEqual(raw["b"], 0.0, "a document with no query term has no evidence")

    def test_rejection_needs_zero_lexical_overlap(self):
        # The only claim the gate makes: nothing in scope shares a single term
        # with the query. It needs no constant, so it cannot drift.
        documents = [
            {"_id": "a", "text": "the worker board and the backlog"},
            {"_id": "b", "text": "palace search over conversations"},
        ]
        _, gibberish = store._bm25("xyzzy plugh frotz", documents)
        self.assertFalse(any(v > 0 for v in gibberish.values()))
        _, ordinary = store._bm25("how does the board work", documents)
        self.assertTrue(any(v > 0 for v in ordinary.values()))

    def test_empty_result_returns_a_distinctive_sentinel(self):
        # Archived drawers quote past search output verbatim, so the no-match
        # signal must not be an ordinary phrase that can appear inside a
        # returned drawer — a caller checking for it would get a false positive.
        with patch.object(store, "search_data", return_value=[]):
            out = store.search_markdown("anything at all", room="conversations")
        self.assertTrue(out.startswith("[palace_search] NO_MATCH"), out[:80])
        self.assertIn("not remembered", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Focused tests for MongoDB/DocumentDB palace behavior."""

import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if "pymongo" not in sys.modules:
    try:
        import pymongo  # noqa: F401
    except ImportError:
        pymongo_stub = types.ModuleType("pymongo")
        pymongo_stub.MongoClient = MagicMock
        pymongo_stub.ReturnDocument = types.SimpleNamespace(AFTER="after")
        sys.modules["pymongo"] = pymongo_stub

from harness import mongo_palace
from harness import palace


class Cursor(list):
    def sort(self, key, direction):
        reverse = direction < 0
        return Cursor(sorted(self, key=lambda row: row.get(key, ""), reverse=reverse))

    def skip(self, count):
        return Cursor(self[count:])

    def limit(self, count):
        return Cursor(self[:count])


class MemoryCollection:
    def __init__(self):
        self.documents = {}

    def replace_one(self, match, document, upsert=False):
        self.documents[document["_id"]] = dict(document)

    def find_one(self, match, projection=None):
        row = self.documents.get(match.get("_id"))
        return dict(row) if row else None

    def find(self, match, projection=None):
        rows = [
            dict(row) for row in self.documents.values()
            if all(row.get(key) == value for key, value in match.items())
        ]
        return Cursor(rows)

    def update_one(self, match, update, upsert=False):
        row = self.documents.get(match.get("_id"))
        if row:
            row.update(update.get("$set", {}))

    def delete_one(self, match):
        deleted = int(self.documents.pop(match.get("_id"), None) is not None)
        return type("DeleteResult", (), {"deleted_count": deleted})()


class MongoPalaceTests(unittest.TestCase):
    def setUp(self):
        mongo_palace._native_vector_search = False

    def test_documentdb_hnsw_command(self):
        command = mongo_palace._vector_index_command()
        options = command["indexes"][0]["vectorOptions"]
        self.assertEqual(command["createIndexes"], mongo_palace.DRAWERS)
        self.assertEqual(options["type"], "hnsw")
        self.assertEqual(options["dimensions"], 384)
        self.assertEqual(options["similarity"], "cosine")

    def test_exact_cosine_fallback_combines_vector_and_bm25(self):
        collection = MagicMock()
        documents = [
            {
                "_id": "apple",
                "text": "apple orchard",
                "embedding": [1.0, 0.0],
                "wing": "agent",
                "room": "knowledge",
                "hall": "fruit",
            },
            {
                "_id": "pear",
                "text": "pear tree",
                "embedding": [0.0, 1.0],
                "wing": "agent",
                "room": "knowledge",
                "hall": "fruit",
            },
        ]
        collection.find.side_effect = [Cursor(documents), Cursor(documents)]
        with patch.object(mongo_palace, "_collection", return_value=collection), patch.object(
            mongo_palace, "_embedding", return_value=[1.0, 0.0]
        ):
            rows = mongo_palace.search_data("apple", k=2)
        self.assertEqual(rows[0]["id"], "apple")
        collection.aggregate.assert_not_called()

    def test_crud_behavior(self):
        collection = MemoryCollection()
        with patch.object(mongo_palace, "_collection", return_value=collection), patch.object(
            mongo_palace, "_embedding", return_value=[0.0] * 384
        ):
            created = mongo_palace.upsert_drawer("durable fact")
            drawer_id = created["id"]
            self.assertEqual(mongo_palace.get_drawer(drawer_id)["text"], "durable fact")
            self.assertIn("updated", mongo_palace.update_drawer(drawer_id, hall="storage"))
            self.assertEqual(mongo_palace.get_drawer(drawer_id)["hall"], "storage")
            self.assertIn("deleted", mongo_palace.delete_drawer(drawer_id))
            self.assertIsNone(mongo_palace.get_drawer(drawer_id))

    def test_list_drawers_uses_inclusion_projection(self):
        collection = MagicMock()
        collection.find.return_value = Cursor([])
        collection.count_documents.return_value = 0
        with patch.object(mongo_palace, "_collection", return_value=collection):
            mongo_palace.list_drawers(room="episodes", limit=50, offset=50)
        collection.find.assert_called_once_with(
            {"room": "episodes"}, mongo_palace._LIST_PROJECTION,
        )
        self.assertNotIn("embedding", mongo_palace._LIST_PROJECTION)

    def test_kg_add_query_and_invalidate(self):
        kg = MagicMock()
        kg.find.return_value = Cursor([{
            "subject": "Clyra",
            "predicate": "uses",
            "object": "S3 Files",
            "valid_from": "2026-07-16",
            "valid_to": None,
        }])
        database = {mongo_palace.KG: kg}
        with patch.object(mongo_palace, "_db", return_value=database):
            self.assertIn("filed", mongo_palace.kg_add("Clyra", "uses", "S3 Files"))
            rows = mongo_palace.kg_query_rows(subject="Clyra")
            self.assertEqual(rows[0]["object"], "S3 Files")
            self.assertIn("invalidated", mongo_palace.kg_invalidate("Clyra", "uses", "S3 Files"))
        kg.update_one.assert_called_once()
        kg.update_many.assert_called_once()


class PalaceFacadeTests(unittest.TestCase):
    """Collapsing the chroma/mongo branches deleted shared function tails."""

    def test_kg_timeline_returns_formatted_markdown(self):
        # The branch collapse removed the tail that formatted `facts` and
        # returned it, so this returned None and the tool dispatcher then did
        # `for b in None` mid-turn.
        rows = [{"subject": "clyra", "predicate": "uses", "object": "documentdb",
                 "valid_from": "2026-07-01", "valid_to": None}]
        with patch.object(palace, "_documentdb") as store:
            store.return_value.kg_query_rows.return_value = rows
            out = palace.kg_timeline("clyra")
        self.assertIsInstance(out, str)
        self.assertIn("KG timeline for `clyra`", out)
        self.assertIn("--[uses]-> `documentdb`", out)

    def test_kg_timeline_reports_an_empty_history(self):
        with patch.object(palace, "_documentdb") as store:
            store.return_value.kg_query_rows.return_value = []
            out = palace.kg_timeline("nobody")
        self.assertIsInstance(out, str)
        self.assertIn("No KG history", out)

    def test_public_palace_functions_never_fall_off_the_end(self):
        # Guard for the whole class of damage, not just kg_timeline.
        import ast
        tree = ast.parse(open("harness/palace.py").read())
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            returns_value = any(
                isinstance(r, ast.Return) and r.value is not None
                for r in ast.walk(node)
            )
            if not returns_value:
                continue
            last = node.body[-1]
            ends_well = isinstance(last, (ast.Return, ast.Raise)) or (
                isinstance(last, ast.Try)
                and all(
                    isinstance(b[-1], (ast.Return, ast.Raise))
                    for b in [last.body] + [h.body for h in last.handlers] if b
                )
            )
            if not ends_well:
                offenders.append(f"{node.name}:{node.lineno}")
        self.assertEqual(offenders, [], f"function(s) can fall off the end: {offenders}")


if __name__ == "__main__":
    unittest.main()

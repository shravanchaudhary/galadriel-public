"""Focused tests for MongoDB/DocumentDB palace behavior."""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

if "pymongo" not in sys.modules:
    try:
        import pymongo  # noqa: F401
    except ImportError:
        pymongo_stub = types.ModuleType("pymongo")
        pymongo_stub.MongoClient = MagicMock
        pymongo_stub.ReturnDocument = types.SimpleNamespace(AFTER="after")
        sys.modules["pymongo"] = pymongo_stub

from harness import documentdb_palace as mongo_palace
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

    def test_backend_selection_accepts_mongo_and_documentdb(self):
        for backend, expected in (
            ("chroma", False),
            ("mongo", True),
            ("documentdb", True),
        ):
            with self.subTest(backend=backend), patch.dict(
                os.environ, {"PALACE_BACKEND": backend}, clear=False
            ):
                self.assertEqual(palace._uses_documentdb(), expected)

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

    def test_crud_and_diary_behavior(self):
        collection = MemoryCollection()
        with patch.object(mongo_palace, "_collection", return_value=collection), patch.object(
            mongo_palace, "_embedding", return_value=[0.0] * 384
        ):
            created = mongo_palace.create_drawer("durable fact")
            drawer_id = created["id"]
            self.assertEqual(mongo_palace.get_drawer(drawer_id)["text"], "durable fact")
            self.assertIn("updated", mongo_palace.update_drawer(drawer_id, hall="storage"))
            self.assertEqual(mongo_palace.get_drawer(drawer_id)["hall"], "storage")
            self.assertIn("saved", mongo_palace.diary_write("remember this"))
            self.assertIn("remember this", mongo_palace.diary_read())
            self.assertIn("deleted", mongo_palace.delete_drawer(drawer_id))
            self.assertIsNone(mongo_palace.get_drawer(drawer_id))

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
        kg.insert_one.assert_called_once()
        kg.find_one_and_update.assert_called_once()


if __name__ == "__main__":
    unittest.main()

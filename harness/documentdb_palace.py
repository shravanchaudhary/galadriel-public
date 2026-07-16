"""Mongo-compatible memory palace for local MongoDB and AWS DocumentDB.

DocumentDB uses its native HNSW search. Ordinary MongoDB stores the same
embeddings and falls back to exact cosine search when that operator or vector
index type is unavailable.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from pymongo import MongoClient, ReturnDocument

DRAWERS = "palace_drawers"
KG = "palace_knowledge_graph"
VECTOR_INDEX = "palace_embedding_hnsw"
DIMENSIONS = 384
DEFAULT_WING = "agent"
_client = None
_database = None
_embedder = None
_indexed = False
_native_vector_search = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db():
    global _client, _database
    if _database is not None:
        return _database
    uri = os.environ.get("MONGO_URI")
    name = os.environ.get("MONGO_DB")
    if not uri or not name:
        raise RuntimeError("MONGO_URI / MONGO_DB are required for PALACE_BACKEND=mongo or documentdb")
    _client = MongoClient(uri)
    _database = _client[name]
    return _database


def _collection():
    global _indexed, _native_vector_search
    collection = _db()[DRAWERS]
    if not _indexed:
        collection.create_index([("wing", 1), ("room", 1), ("hall", 1)])
        collection.create_index([("source_file", 1), ("chunk_index", 1)])
        collection.create_index([("filed_at", -1)])
        if os.environ.get("PALACE_BACKEND", "mongo").lower() == "documentdb":
            try:
                existing = {index["name"] for index in collection.list_indexes()}
                if VECTOR_INDEX not in existing:
                    _db().command(_vector_index_command())
                _native_vector_search = True
            except Exception:
                # Local MongoDB and older DocumentDB clusters do not support
                # DocumentDB's vector index type. Exact cosine remains correct.
                _native_vector_search = False
        _indexed = True
    return collection


def _vector_index_command() -> dict:
    """Return the DocumentDB native HNSW index command."""
    return {
        "createIndexes": DRAWERS,
        "indexes": [{
            "key": {"embedding": "vector"},
            "name": VECTOR_INDEX,
            "vectorOptions": {
                "type": "hnsw",
                "dimensions": DIMENSIONS,
                "similarity": "cosine",
                "m": 16,
                "efConstruction": 64,
            },
        }],
    }


def _vector_search_pipeline(query_vector: list[float], candidate_count: int) -> list[dict]:
    """Return the AWS DocumentDB native vector-search pipeline."""
    return [{
        "$search": {
            "vectorSearch": {
                "vector": query_vector,
                "path": "embedding",
                "similarity": "cosine",
                "k": candidate_count,
                "efSearch": min(1000, candidate_count * 2),
            }
        }
    }]


def _embedding(text: str) -> list[float]:
    global _embedder
    if _embedder is None:
        from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

        _embedder = DefaultEmbeddingFunction()
    vector = _embedder([text])[0]
    result = vector.tolist() if hasattr(vector, "tolist") else list(vector)
    if len(result) != DIMENSIONS:
        raise ValueError(f"expected {DIMENSIONS}-dimension embedding, got {len(result)}")
    return [float(value) for value in result]


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _bm25(query: str, documents: list[dict]) -> dict[str, float]:
    terms = _tokens(query)
    if not terms or not documents:
        return {}
    tokenized = [_tokens(document.get("text", "")) for document in documents]
    average_length = sum(map(len, tokenized)) / max(len(tokenized), 1)
    frequencies = Counter(term for tokens in tokenized for term in set(tokens))
    scores: dict[str, float] = {}
    for document, tokens in zip(documents, tokenized):
        counts = Counter(tokens)
        score = 0.0
        for term in terms:
            document_frequency = frequencies.get(term, 0)
            if not document_frequency:
                continue
            inverse = math.log(1 + (len(documents) - document_frequency + 0.5) / (document_frequency + 0.5))
            frequency = counts.get(term, 0)
            denominator = frequency + 1.5 * (1 - 0.75 + 0.75 * len(tokens) / max(average_length, 1))
            score += inverse * (frequency * 2.5 / denominator if denominator else 0)
        scores[str(document["_id"])] = score
    maximum = max(scores.values(), default=0)
    return {key: value / maximum if maximum else 0.0 for key, value in scores.items()}


def _matches(document: dict, wing=None, room=None, hall=None) -> bool:
    return all(
        expected is None or document.get(key) == expected
        for key, expected in (("wing", wing), ("room", room), ("hall", hall))
    )


def _without_embedding(document: dict) -> dict:
    result = dict(document)
    result["id"] = str(result.pop("_id"))
    result["metadata"] = {
        key: result.get(key)
        for key in ("wing", "room", "hall", "source_file", "filed_at", "topic", "agent")
        if result.get(key) is not None
    }
    result.pop("embedding", None)
    return result


def search_data(query: str, wing=None, room=None, hall=None, k: int = 20) -> list[dict]:
    if not query.strip():
        return []
    collection = _collection()
    query_vector = _embedding(query)
    candidate_count = max(k * 10, 50)
    vector_documents = []
    if _native_vector_search:
        try:
            vector_documents = list(
                collection.aggregate(_vector_search_pipeline(query_vector, candidate_count))
            )
        except Exception:
            # Index creation can still be in progress. Exact cosine keeps
            # behavior correct while the HNSW index becomes ready.
            vector_documents = []
    if not vector_documents:
        vector_documents = list(collection.find({}, {"embedding": 1, "text": 1, "wing": 1, "room": 1, "hall": 1, "source_file": 1, "filed_at": 1}))
        for document in vector_documents:
            vector = document.get("embedding") or []
            denominator = math.sqrt(sum(x * x for x in vector)) * math.sqrt(sum(x * x for x in query_vector))
            document["_cosine"] = sum(a * b for a, b in zip(vector, query_vector)) / denominator if denominator else 0
        vector_documents.sort(key=lambda item: item.get("_cosine", 0), reverse=True)

    vector_documents = [
        document for document in vector_documents
        if _matches(document, wing=wing, room=room, hall=hall)
    ][:candidate_count]
    filtered = list(collection.find(
        {key: value for key, value in (("wing", wing), ("room", room), ("hall", hall)) if value is not None},
        {"embedding": 0},
    ))
    lexical = _bm25(query, filtered)
    vector_rank = {
        str(document["_id"]): 1.0 - rank / max(len(vector_documents), 1)
        for rank, document in enumerate(vector_documents)
    }
    combined = {str(document["_id"]): 0.4 * lexical.get(str(document["_id"]), 0) for document in filtered}
    by_id = {str(document["_id"]): document for document in filtered}
    for document in vector_documents:
        key = str(document["_id"])
        combined[key] = combined.get(key, 0) + 0.6 * vector_rank[key]
        by_id[key] = document
    ranked = sorted(combined, key=combined.get, reverse=True)[: max(1, min(k, 50))]
    return [{**_without_embedding(by_id[key]), "score": combined[key]} for key in ranked]


def search_markdown(query="", wing=None, room=None, hall=None, k=5, order=None, channel=None) -> str:
    if order == "recency":
        match = {key: value for key, value in (("wing", wing), ("room", room), ("hall", hall)) if value}
        if channel:
            match["channel"] = channel
        if query:
            match["text"] = {"$regex": re.escape(query), "$options": "i"}
        rows = list(_collection().find(match, {"embedding": 0}).sort("filed_at", -1).limit(max(1, min(k, 20))))
    else:
        if not query.strip():
            return "[palace_search] query is required for semantic search (use order=`recency` for latest sessions)"
        rows = search_data(query, wing=wing, room=room, hall=hall, k=k)
    if not rows:
        return "No drawers matched the requested palace query."
    lines = ["**Palace search (DocumentDB):**", ""]
    for index, row in enumerate(rows, 1):
        lines.extend([
            f"### {index}. {row.get('wing', '?')} / {row.get('room', '?')} / hall={row.get('hall', '?')}",
            (row.get("text") or "").strip(),
            "",
        ])
    return "\n".join(lines).rstrip()


def upsert_drawer(
    text: str,
    *,
    drawer_id: str | None = None,
    wing: str = DEFAULT_WING,
    room: str = "knowledge",
    hall: str = "general",
    source_file: str = "",
    filed_at: str | None = None,
    chunk_index: int = 0,
    **metadata,
) -> dict:
    identifier = drawer_id or str(uuid.uuid4())
    document = {
        "_id": identifier,
        "text": text.strip(),
        "embedding": _embedding(text),
        "wing": wing,
        "room": room,
        "hall": hall,
        "source_file": source_file,
        "filed_at": filed_at or _now(),
        "chunk_index": chunk_index,
        "updated_at": _now(),
        **metadata,
    }
    _collection().replace_one({"_id": identifier}, document, upsert=True)
    return _without_embedding(document)


def import_drawer(drawer_id: str, text: str, metadata: dict, embedding) -> None:
    vector = embedding.tolist() if hasattr(embedding, "tolist") else list(embedding or [])
    if len(vector) != DIMENSIONS:
        vector = _embedding(text)
    document = {
        "_id": str(drawer_id),
        "text": text,
        "embedding": [float(value) for value in vector],
        "wing": metadata.get("wing", DEFAULT_WING),
        "room": metadata.get("room", "knowledge"),
        "hall": metadata.get("hall", "general"),
        "source_file": metadata.get("source_file", ""),
        "filed_at": metadata.get("filed_at") or _now(),
        "chunk_index": metadata.get("chunk_index", 0),
        "updated_at": _now(),
        **{key: value for key, value in metadata.items() if key not in {"wing", "room", "hall", "source_file", "filed_at", "chunk_index"}},
    }
    _collection().replace_one({"_id": document["_id"]}, document, upsert=True)


def import_kg(rows: list[dict]) -> int:
    imported = 0
    for row in rows:
        document = {
            key: row.get(key)
            for key in ("subject", "predicate", "object", "valid_from", "valid_to")
        }
        if not all(document.get(key) for key in ("subject", "predicate", "object")):
            continue
        _db()[KG].update_one(
            {key: document[key] for key in ("subject", "predicate", "object", "valid_from")},
            {"$set": document},
            upsert=True,
        )
        imported += 1
    return imported


def _chunks(text: str, size: int = 3000) -> list[str]:
    paragraphs = text.splitlines(keepends=True)
    chunks, current = [], ""
    for paragraph in paragraphs:
        if current and len(current) + len(paragraph) > size:
            chunks.append(current.strip())
            current = ""
        current += paragraph
    if current.strip():
        chunks.append(current.strip())
    return chunks or [text.strip()]


def mine_directory(batch_dir: Path, *, agent: str = DEFAULT_WING) -> bool:
    markdown = sorted(batch_dir.rglob("*.md"))
    for path in markdown:
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(batch_dir)
        room = relative.parts[0] if len(relative.parts) > 1 else "knowledge"
        channel_match = re.search(r"conversation_([^_]+)_", path.name)
        for index, chunk in enumerate(_chunks(text)):
            identifier = hashlib.sha256(f"{path}:{index}:{chunk}".encode()).hexdigest()
            upsert_drawer(
                chunk,
                drawer_id=identifier,
                wing=DEFAULT_WING,
                room=room,
                hall="general",
                source_file=str(path),
                chunk_index=index,
                agent=agent,
                channel=channel_match.group(1) if channel_match else None,
            )
    return True


def taxonomy_data() -> dict:
    pipeline = [{"$group": {"_id": {"wing": "$wing", "room": "$room"}, "count": {"$sum": 1}}}]
    wings: dict[str, dict[str, int]] = {}
    total = 0
    for row in _collection().aggregate(pipeline):
        wing = row["_id"].get("wing") or "?"
        room = row["_id"].get("room") or "?"
        wings.setdefault(wing, {})[room] = row["count"]
        total += row["count"]
    halls = {
        row["_id"] or "?": row["count"]
        for row in _collection().aggregate([{"$group": {"_id": "$hall", "count": {"$sum": 1}}}])
    }
    return {"total": total, "wings": wings, "halls": halls}


def list_drawers(wing=None, room=None, hall=None, limit=50, offset=0) -> dict:
    match = {key: value for key, value in (("wing", wing), ("room", room), ("hall", hall)) if value is not None}
    collection = _collection()
    rows = collection.find(match, {"embedding": 0}).sort("filed_at", -1).skip(max(0, offset)).limit(max(1, limit))
    return {"total": collection.count_documents(match), "drawers": [_without_embedding(row) for row in rows]}


def get_drawer(drawer_id: str) -> dict | None:
    row = _collection().find_one({"_id": drawer_id}, {"embedding": 0})
    return _without_embedding(row) if row else None


def update_drawer(drawer_id: str, text=None, wing=None, room=None, hall=None) -> str:
    current = _collection().find_one({"_id": drawer_id})
    if not current:
        return f"[palace edit] drawer `{drawer_id}` not found"
    changes = {key: value for key, value in (("wing", wing), ("room", room), ("hall", hall)) if value is not None}
    if text is not None and text.strip():
        changes.update(text=text.strip(), embedding=_embedding(text))
    if not changes:
        return "[palace edit] nothing to change"
    changes["updated_at"] = _now()
    _collection().update_one({"_id": drawer_id}, {"$set": changes})
    return f"Drawer `{drawer_id}` updated."


def delete_drawer(drawer_id: str) -> str:
    deleted = _collection().delete_one({"_id": drawer_id}).deleted_count
    return f"Drawer `{drawer_id}` deleted." if deleted else f"[palace delete] drawer `{drawer_id}` not found"


def create_drawer(text: str, wing=DEFAULT_WING, room="general", hall="general") -> dict:
    if not text.strip():
        return {"error": "empty content"}
    return upsert_drawer(text, wing=wing, room=room, hall=hall, source_file="tower:create")


def diary_write(entry: str, topic="general", agent_name=DEFAULT_WING) -> str:
    if not entry.strip():
        return "[diary write] empty entry — nothing saved."
    upsert_drawer(entry, wing=DEFAULT_WING, room="diary", hall=topic, topic=topic, agent=agent_name)
    return f"Diary entry saved to wing `{DEFAULT_WING}`, topic `{topic}`."


def diary_read(last_n=10, agent_name=DEFAULT_WING) -> str:
    rows = list(_collection().find(
        {"room": "diary", "agent": agent_name},
        {"embedding": 0},
    ).sort("filed_at", -1).limit(max(1, min(last_n, 50))))
    if not rows:
        return f"No diary entries yet for agent `{agent_name}`."
    lines = [f"**Diary — `{agent_name}` (last {len(rows)})**", ""]
    for row in rows:
        lines.extend([f"### {row.get('filed_at', '?')}  _(topic: {row.get('topic', '?')})_", row["text"], ""])
    return "\n".join(lines).rstrip()


def kg_add(subject, predicate, object, valid_from=None) -> str:
    _db()[KG].insert_one({
        "subject": subject,
        "predicate": predicate,
        "object": object,
        "valid_from": valid_from or datetime.now(timezone.utc).date().isoformat(),
        "valid_to": None,
    })
    return f"KG: filed `{subject}` --[{predicate}]-> `{object}`"


def kg_query_rows(subject=None, predicate=None, object=None, limit=200) -> list[dict]:
    match = {key: value for key, value in (("subject", subject), ("predicate", predicate), ("object", object)) if value}
    return list(_db()[KG].find(match, {"_id": 0}).sort("valid_from", -1).limit(limit))


def kg_invalidate(subject, predicate, object, ended=None) -> str:
    _db()[KG].find_one_and_update(
        {"subject": subject, "predicate": predicate, "object": object, "valid_to": None},
        {"$set": {"valid_to": ended or datetime.now(timezone.utc).date().isoformat()}},
        return_document=ReturnDocument.AFTER,
    )
    return f"KG: invalidated `{subject}` --[{predicate}]-> `{object}`"


def wake_up_text() -> str:
    rows = list(_collection().find({}, {"embedding": 0}).sort("filed_at", -1).limit(20))
    return "\n\n".join(f"- [{row.get('room', '?')}] {row.get('text', '')[:500]}" for row in rows)


def close() -> None:
    global _client, _database, _indexed, _native_vector_search
    if _client is not None:
        _client.close()
    _client = None
    _database = None
    _indexed = False
    _native_vector_search = False

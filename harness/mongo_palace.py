"""The memory palace. Storage is MongoDB locally and AWS DocumentDB in staging.

DocumentDB uses its native HNSW search. Ordinary MongoDB stores the same
embeddings and falls back to exact cosine search when that operator or vector
index type is unavailable.

Embeddings come from BAAI/bge-small-en-v1.5 via FastEmbed — the same model and
the same loader the recall router already uses (`harness/recall.py`), so the
service holds one embedding model rather than two. It is 384-dimensional like
the MiniLM model it replaced (the vector index is unchanged) but has a 512-token
window instead of 256 and materially better retrieval quality, which is what the
token-budget chunker below is sized against.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import os
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from pymongo import MongoClient, ReturnDocument

log = logging.getLogger("galadriel.palace.store")

DRAWERS = "palace_drawers"
KG = "palace_knowledge_graph"
# Monotonic chunk-number counters, one document per conversation_id.
CHUNK_COUNTERS = "palace_chunk_counters"
VECTOR_INDEX = "palace_embedding_hnsw"
DIMENSIONS = 384
# Written by harness/palace.py beside every staged conversation batch.
SPANS_FILE = "spans.json"
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
# bge-small-en-v1.5 truncates at 512 tokens. Chunks are packed to a target below
# that so the whole chunk is inside the window: text past the cut is invisible to
# the vector arm, which is exactly the defect the 3000-char chunker had (~2/3 of
# every drawer unreachable except lexically).
EMBED_MAX_TOKENS = 512
CHUNK_TARGET_TOKENS = 440
CHUNK_OVERLAP_TOKENS = 50
DEFAULT_WING = "agent"
_client = None
_database = None
_embedder = None
_tokenizer_cache = None
_indexed = False
_kg_indexed = False
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
        raise RuntimeError("MONGO_URI / MONGO_DB are required — the palace has no local-file backend")
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
        # Walking one conversation in order is a primary access path, not a scan.
        collection.create_index([("conversation_id", 1), ("chunk_number", 1)])
        collection.create_index([("channel", 1), ("filed_at", -1)])
        # PALACE_BACKEND no longer selects an engine (there is only one); it just
        # marks the cluster as DocumentDB, which has native HNSW that Atlas and
        # local MongoDB do not expose the same way.
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


def _model():
    """Process-cached FastEmbed model. Loaded on first use, never at import."""
    global _embedder
    if _embedder is None:
        from fastembed import TextEmbedding

        _embedder = TextEmbedding(model_name=EMBED_MODEL)
    return _embedder


def _tokenizer():
    """The embedding model's tokenizer, with truncation disabled, for budgeting.

    Chunking against the real tokenizer (not a character estimate) is the only
    way to guarantee a chunk fits the window: tokens-per-character varies by an
    order of magnitude between prose and the JSON tool output that fills agent
    transcripts.

    The model's own tokenizer truncates at the window, so it reports 512 for
    anything longer and cannot say how much longer. Budgeting needs the true
    length, so this counts on a detached copy with truncation off — the model's
    tokenizer is left alone, since the embedder does need to truncate.
    """
    global _tokenizer_cache
    if _tokenizer_cache is None:
        source = getattr(getattr(_model(), "model", None), "tokenizer", None)
        if source is None:
            return None
        try:
            counter = copy.deepcopy(source)
            counter.no_truncation()
            _tokenizer_cache = counter
        except Exception:
            _tokenizer_cache = source
    return _tokenizer_cache


# WordPiece gives up on any whitespace-delimited run longer than this and emits a
# single [UNK], so token count alone under-measures opaque blobs (base64, minified
# JSON, a long hash) by orders of magnitude. Characters are the honest bound there.
_MAX_WORD_CHARS = 100
_CHARS_PER_TOKEN = 4


def _token_count(text: str) -> int:
    """Tokens, floored by a character estimate over WordPiece's blind spot."""
    tokenizer = _tokenizer()
    opaque = sum(
        len(word) for word in text.split() if len(word) > _MAX_WORD_CHARS
    )
    floor = opaque // _CHARS_PER_TOKEN
    if tokenizer is None:
        # No tokenizer exposed by this fastembed version — approximate rather
        # than fail. 4 chars/token is conservative for English prose.
        return max(1, len(text) // _CHARS_PER_TOKEN)
    try:
        counted = len(tokenizer.encode(text, add_special_tokens=False).ids)
    except Exception:
        return max(1, len(text) // _CHARS_PER_TOKEN)
    return max(1, counted, floor)


def _embedding(text: str) -> list[float]:
    vector = next(iter(_model().embed([text])))
    result = vector.tolist() if hasattr(vector, "tolist") else list(vector)
    if len(result) != DIMENSIONS:
        raise ValueError(f"expected {DIMENSIONS}-dimension embedding, got {len(result)}")
    return [float(value) for value in result]


def _embeddings(texts: list[str]) -> list[list[float]]:
    """Batch embed. FastEmbed batches internally, so mining N chunks costs one
    model pass instead of N."""
    out = []
    for vector in _model().embed(list(texts)):
        result = vector.tolist() if hasattr(vector, "tolist") else list(vector)
        if len(result) != DIMENSIONS:
            raise ValueError(f"expected {DIMENSIONS}-dimension embedding, got {len(result)}")
        out.append([float(value) for value in result])
    return out


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _bm25(query: str, documents: list[dict]) -> tuple[dict[str, float], dict[str, float]]:
    """Okapi BM25 over the scoped corpus.

    Returns (normalized, raw). The normalized scores are what the ranker fuses;
    the raw scores are what the gate trusts, because max-normalization always
    promotes *something* to 1.0 — a query whose only overlap with the corpus is
    the word "for" would otherwise look like a perfect lexical match.
    """
    terms = _tokens(query)
    if not terms or not documents:
        return {}, {}
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
    normalized = {
        key: (value / maximum if maximum else 0.0) for key, value in scores.items()
    }
    return normalized, scores


# Metadata fields a caller may filter on. Whitelisted rather than free-form so a
# typo becomes an error naming the valid keys instead of a silent unfiltered
# collection scan, and so every filterable field stays index-backed.
FILTERABLE = frozenset({
    "wing", "room", "hall", "conversation_id", "chunk_number",
    "channel", "agent", "topic", "source_file",
})

# There is deliberately NO similarity threshold. Several were built and measured
# against the real corpus, and none survived:
#
#   - Absolute cosine floor: bge-small's range is compressed and shifts with the
#     corpus. Calibrated at 0.74 it worked, then 281 drawers were deleted and the
#     bands inverted — a random UUID scored 0.714 while the genuine query "what
#     did we do yesterday" fell to 0.663. A constant tuned to one snapshot of the
#     corpus silently mis-fires on the next.
#   - Corpus-relative z-score (is the top hit an outlier?): self-calibrating, but
#     it does not separate either — present queries 2.64-4.65, absent 2.48-3.31.
#   - Raw BM25 threshold: scales with query length, so a bar that rejects a
#     4-word absent query also rejects single-token id lookups (recall 100% → 33%).
#   - Rarest-matched-term (IDF): an absent query's uncommon words still occur
#     incidentally — "recipe for sourdough starter hydration" scored 5.44 against
#     the *present* "palace search memory" at 2.44. Ordering inverted.
#
# So the only claim this code makes is the one that needs no constant: the query
# shares not a single term with anything in scope. Gibberish and stray ids match
# nothing lexically (measured 0.00), while any real question overlaps on ordinary
# words (measured minimum 6.16). Everything else is returned WITH its similarity
# score, so the agent judges relevance from the text and the number rather than
# from a threshold we cannot honestly calibrate.
#
# What this deliberately does NOT try to detect: a well-formed question about a
# topic the palace has never seen ("best hiking trails in patagonia" — real
# words, absent subject). That is indistinguishable from a weak real match at the
# embedding level, so it returns the nearest drawers and the agent reads them.
# Weight on the lexical arm. Both arms are absolute scores in [0,1], so an exact
# term match can actually outrank a merely-adjacent embedding — under the old
# rank-relative fusion the top vector hit always scored 0.6 and a perfect
# lexical match was capped at 0.4, so it could never win.
LEXICAL_WEIGHT = 0.4
VECTOR_WEIGHT = 0.6


class FilterError(ValueError):
    """Raised for an unknown or malformed search_meta key."""


def build_filter(
    wing=None, room=None, hall=None, search_meta: dict | None = None,
) -> dict:
    """Translate caller filters into one Mongo query document.

    ``search_meta`` accepts scalars (exact match), lists (``$in``) and
    ``{"from": n, "to": n}`` ranges, so navigation filters compose without the
    tool growing a parameter per field.
    """
    query: dict = {}
    for key, value in (("wing", wing), ("room", room), ("hall", hall)):
        if value is not None:
            query[key] = value
    for key, value in (search_meta or {}).items():
        if key not in FILTERABLE:
            raise FilterError(
                f"unknown search_meta field `{key}`. "
                f"Valid fields: {', '.join(sorted(FILTERABLE))}"
            )
        if isinstance(value, dict):
            bounds = {}
            if value.get("from") is not None:
                bounds["$gte"] = value["from"]
            if value.get("to") is not None:
                bounds["$lte"] = value["to"]
            if not bounds:
                raise FilterError(f"`{key}` range needs `from` and/or `to`")
            query[key] = bounds
        elif isinstance(value, (list, tuple, set)):
            query[key] = {"$in": list(value)}
        else:
            query[key] = value
    return query


def _without_embedding(document: dict) -> dict:
    result = dict(document)
    result["id"] = str(result.pop("_id"))
    result["metadata"] = {
        key: result.get(key)
        for key in (
            "wing", "room", "hall", "source_file", "filed_at", "topic", "agent",
            "conversation_id", "chunk_number", "channel", "chunk_index",
        )
        if result.get(key) is not None
    }
    result.pop("embedding", None)
    return result


def _cosine(a: list[float], b: list[float]) -> float:
    denominator = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b)) / denominator if denominator else 0.0


def fetch_data(
    search_meta: dict | None = None,
    *,
    wing=None, room=None, hall=None,
    limit: int = 50,
    order: str = "chunk_number",
) -> list[dict]:
    """Deterministic metadata fetch — no embedding, no ranking.

    This is how a conversation is walked end to end: land on any chunk
    semantically, then read the whole thread in order by its conversation_id.
    Hybrid ranking cannot do this job (measured: 46% recall on exact rare-token
    queries, and an id absent from the corpus still returns confident rows), so
    navigation is a filter, not a search.
    """
    query = build_filter(wing, room, hall, search_meta)
    if not query:
        raise FilterError("a metadata filter is required for a non-semantic fetch")
    sort = [(order, 1)] if order else [("filed_at", -1)]
    rows = _collection().find(query, _LIST_PROJECTION).sort(sort).limit(max(1, min(limit, 200)))
    return [_without_embedding(row) for row in rows]


def search_data(
    query: str,
    wing=None, room=None, hall=None,
    k: int = 20,
    search_meta: dict | None = None,
) -> list[dict]:
    """Hybrid search: absolute cosine fused with BM25, filtered at the query.

    Filters are pushed into Mongo before ranking. Previously they were applied
    after taking the global top candidates, so a scoped search silently lost any
    in-scope drawer outside that global window.
    """
    if not query.strip():
        return []
    collection = _collection()
    scope = build_filter(wing, room, hall, search_meta)
    query_vector = _embedding(query)
    candidate_count = max(k * 10, 50)

    scored: dict[str, float] = {}
    similarity: dict[str, float] = {}
    by_id: dict[str, dict] = {}

    if _native_vector_search:
        try:
            pipeline = list(_vector_search_pipeline(query_vector, candidate_count))
            if scope:
                pipeline.append({"$match": scope})
            for document in collection.aggregate(pipeline):
                key = str(document["_id"])
                by_id[key] = document
                similarity[key] = _cosine(document.get("embedding") or [], query_vector)
                scored[key] = VECTOR_WEIGHT * similarity[key]
        except Exception as exc:
            log.warning("Native vector search failed, using exact cosine: %s", exc)

    corpus = list(collection.find(scope, {"embedding": 0}))
    if not scored:
        # Exact cosine over the scoped corpus. Correct, and bounded by the scope
        # rather than the whole collection.
        for document in collection.find(scope, {"embedding": 1}):
            key = str(document["_id"])
            similarity[key] = _cosine(document.get("embedding") or [], query_vector)
            scored[key] = VECTOR_WEIGHT * similarity[key]
        by_id = {str(d["_id"]): d for d in corpus}

    lexical, lexical_raw = _bm25(query, corpus)
    for key, score in lexical.items():
        scored[key] = scored.get(key, 0.0) + LEXICAL_WEIGHT * score
    for document in corpus:
        by_id.setdefault(str(document["_id"]), document)

    # Nothing in scope shares a single term with the query: it is gibberish or a
    # stray identifier, not a question about anything stored here. Only applied
    # when the query HAS comparable terms — `_tokens` is ASCII-only, so a CJK or
    # emoji query tokenizes to nothing and must fall through to the vector arm
    # rather than be reported as "not remembered".
    if _tokens(query) and not any(value > 0 for value in lexical_raw.values()):
        return []

    ranked = sorted(scored, key=scored.get, reverse=True)
    ranked = [key for key in ranked if key in by_id][: max(1, min(k, 50))]
    return [
        {
            **_without_embedding(by_id[key]),
            "score": scored[key],
            "similarity": (
                round(similarity[key], 3) if key in similarity else None
            ),
        }
        for key in ranked
    ]


def search_markdown(
    query="", wing=None, room=None, hall=None, k=5, order=None,
    channel=None, search_meta=None,
) -> str:
    """Render search / fetch results as the markdown a tool result returns.

    Three modes. A `search_meta` filter with no query is a deterministic walk
    (ordered by chunk_number). `order="recency"` lists the latest archives.
    Otherwise it is a hybrid semantic search.
    """
    meta = dict(search_meta or {})
    if channel:
        meta.setdefault("channel", channel)
    try:
        if order == "recency":
            scope = build_filter(wing, room, hall, meta)
            if query:
                scope["text"] = {"$regex": re.escape(query), "$options": "i"}
            rows = [
                _without_embedding(row) for row in
                _collection().find(scope, _LIST_PROJECTION)
                .sort("filed_at", -1).limit(max(1, min(k, 20)))
            ]
            header = "Palace — most recent"
        elif not query.strip():
            if not meta:
                return (
                    "[palace_search] give a `query` for semantic search, a "
                    "`search_meta` filter to walk drawers directly, or "
                    "order=`recency` for the latest archives."
                )
            rows = fetch_data(meta, wing=wing, room=room, hall=hall, limit=k)
            header = "Palace — metadata fetch (in chunk order)"
        else:
            rows = search_data(query, wing, room, hall, k, search_meta=meta)
            header = "Palace search"
    except FilterError as exc:
        return f"[palace_search] {exc}"

    if not rows:
        return (
            "[palace_search] NO_MATCH — nothing in the palace is close enough to "
            "count as a match. Treat this as 'not remembered', not 'nothing "
            "exists'."
        )
    lines = [f"**{header}:**", ""]
    for index, row in enumerate(rows, 1):
        meta_row = row.get("metadata") or row
        identifier = row.get("id") or row.get("_id") or ""
        bits = [
            f"### {index}. {meta_row.get('wing', '?')} / {meta_row.get('room', '?')}",
            f"hall={meta_row.get('hall', '?')}",
        ]
        if meta_row.get("conversation_id"):
            bits.append(f"conversation_id=`{meta_row['conversation_id']}`")
        if meta_row.get("chunk_number") is not None:
            bits.append(f"chunk={meta_row['chunk_number']}")
        if meta_row.get("filed_at"):
            bits.append(f"filed={str(meta_row['filed_at'])[:19]}")
        if row.get("similarity") is not None:
            # Raw cosine, shown rather than thresholded: relevance is a judgment
            # the reader makes from the text, with this as one input.
            bits.append(f"similarity={row['similarity']}")
        if identifier:
            bits.append(f"id=`{identifier}`")
        lines.extend([" / ".join(bits), (row.get("text") or "").strip(), ""])
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


# Capturing group: the separator is KEPT so paragraph breaks, list items and
# table rows survive into the stored drawer. Joining on " " instead threw
# away every blank line and made markdown unreadable when read back.
_SENTENCE_SPLIT = re.compile(r"((?<=[.!?])\s+|\n{2,}|\n(?=[#\-*>|]))")


def _sentences(text: str) -> list[str]:
    """Split into sentence-ish units, preserving markdown block boundaries.

    Agent transcripts are not prose: headings, list items, fenced JSON and tool
    output all need to survive as units, so blank lines and markdown line starts
    break as hard as sentence punctuation does.
    """
    pieces = _SENTENCE_SPLIT.split(text)
    units: list[str] = []
    for index in range(0, len(pieces), 2):
        body = pieces[index]
        separator = pieces[index + 1] if index + 1 < len(pieces) else ""
        unit = body + separator
        if unit.strip():
            units.append(unit)
    return units or ([text] if text.strip() else [])


def _split_oversized(unit: str, budget: int) -> list[str]:
    """Hard-split a single unit that exceeds the budget on its own.

    A minified JSON tool result can be one 'sentence' of 40k tokens. Bisecting on
    whitespace keeps the pieces inside the window without a token-by-token walk.
    """
    if _token_count(unit) <= budget:
        return [unit]
    words = unit.split(" ")
    if len(words) < 2:
        # Not splittable on whitespace (one enormous token blob) — cut by
        # characters proportionally to how far over budget it is.
        ratio = max(2, -(-_token_count(unit) // budget))
        size = max(1, len(unit) // ratio)
        return [unit[i:i + size] for i in range(0, len(unit), size)]
    middle = len(words) // 2
    return (
        _split_oversized(" ".join(words[:middle]), budget)
        + _split_oversized(" ".join(words[middle:]), budget)
    )


def _chunks(
    text: str,
    *,
    target: int = CHUNK_TARGET_TOKENS,
    overlap: int = CHUNK_OVERLAP_TOKENS,
) -> list[str]:
    """Pack text into chunks that fit the embedder's window.

    Packs whole sentences up to `target` tokens, then carries `overlap` tokens'
    worth of trailing sentences into the next chunk so a match that straddles a
    boundary is still retrievable. Never emits a chunk over the model window.
    """
    text = (text or "").strip()
    if not text:
        return []
    units: list[str] = []
    for unit in _sentences(text):
        # Deliberately NOT stripped: each unit carries its trailing separator, and
        # that separator is the paragraph / list / table break. Stripping here put
        # the markdown structure straight back in the bin.
        units.extend(_split_oversized(unit, target))

    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for unit in units:
        unit_tokens = _token_count(unit)
        if current and current_tokens + unit_tokens > target:
            chunks.append("".join(current).strip())
            # Carry back trailing units until the overlap budget is used up.
            carry: list[str] = []
            carried = 0
            for previous in reversed(current):
                previous_tokens = _token_count(previous)
                if carried + previous_tokens > overlap:
                    break
                carry.insert(0, previous)
                carried += previous_tokens
            current = carry
            current_tokens = carried
        current.append(unit)
        current_tokens += unit_tokens
    if current:
        chunks.append("".join(current).strip())
    return [c for c in chunks if c]


def _drawer_id(source_file: str, index: int) -> str:
    """Deterministic drawer id: same file + same position => same id.

    Re-mining a batch therefore overwrites its own drawers in place instead of
    minting a second copy, which is what makes a retried or duplicated mine
    idempotent at the store level.
    """
    return hashlib.sha256(f"{source_file}:{index}".encode()).hexdigest()


def _purge_source(source_file: str) -> int:
    """Drop every drawer previously mined from a file.

    Mined before insert, so a file that now yields fewer chunks than last time
    does not leave the tail of the old run stranded and searchable forever.
    """
    return _collection().delete_many({"source_file": source_file}).deleted_count


def _store_chunks(
    rows: list[dict],
) -> int:
    """Embed and upsert a batch of prepared drawer rows in one model pass."""
    if not rows:
        return 0
    vectors = _embeddings([row["text"] for row in rows])
    collection = _collection()
    stamp = _now()
    for row, vector in zip(rows, vectors):
        document = {k: v for k, v in row.items() if v is not None}
        document["embedding"] = vector
        document["filed_at"] = row.get("filed_at") or stamp
        document["updated_at"] = stamp
        collection.replace_one({"_id": document["_id"]}, document, upsert=True)
    return len(rows)


_MESSAGE_MARKER = re.compile(r"^<!-- message \d+ -->$", re.M)
_ROLE_HEADING = re.compile(r"^## (user|assistant)\s*$", re.M)


def _spans_from_markdown(text: str) -> list[dict]:
    """Recover speaker spans from an archived transcript that has no manifest.

    Batches staged by an older build — and any archive written by a process that
    started before this change and exits after it — carry only the .md. Without
    this they would mine as `hall=general`, putting untyped drawers back into
    room=conversations. Same rules as the live path: a `## user` block carrying a
    tool_result is agent traffic, and harness scaffolding is dropped.
    """
    spans: list[dict] = []
    for block in _MESSAGE_MARKER.split(text):
        block = block.strip()
        heading = _ROLE_HEADING.search(block)
        if not heading:
            continue
        body = block[heading.end():].strip()
        if not body or body.startswith("[SYSTEM:") or body.startswith("[Recall detected]"):
            continue
        role = heading.group(1)
        hall = "user" if (role == "user" and "### tool_result" not in body) else "assistant"
        piece = f"## {role}\n\n{body}"
        if spans and spans[-1]["hall"] == hall:
            spans[-1]["text"] += "\n\n" + piece
        else:
            spans.append({"hall": hall, "text": piece})
    return spans


def _reserve_chunk_numbers(conversation_id: str, count: int) -> int:
    """Atomically take `count` consecutive chunk numbers for a conversation.

    Numbering is assigned HERE rather than when the batch was staged, because
    only this layer knows how many chunks the spans actually produce — the
    stager knows a message count, which is a different number. Assigning it
    upstream produced colliding chunk numbers across consecutive checkpoints.

    Re-mining a batch takes fresh numbers, so a re-mine can leave gaps. That is
    fine: the contract is that chunk_number *orders* a conversation, not that it
    is dense.
    """
    if not conversation_id or count <= 0:
        return 1
    # Stores the LAST number handed out, not the next one: `$inc` on an absent
    # field starts from 0, so reading "next" back gives `count` after the first
    # batch when the next free number is actually `count + 1`. Counting what was
    # used makes the arithmetic correct from an empty document with no seeding.
    document = _db()[CHUNK_COUNTERS].find_one_and_update(
        {"_id": conversation_id},
        {"$inc": {"last_chunk": count}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    last = int((document or {}).get("last_chunk", count) or count)
    return max(1, last - count + 1)


def _mine_conversation_spans(
    batch_dir: Path, manifest: dict, agent: str, source_file: str | None = None,
) -> int:
    """File a conversation batch by speaker span.

    Each span is chunked independently, so a chunk is never half user text and
    half agent text, and `chunk_number` runs monotonically across the whole
    conversation rather than restarting per archive batch. That is what lets the
    agent land on any chunk semantically and then walk the conversation in order
    via search_meta.
    """
    conversation_id = manifest.get("conversation_id")
    channel_id = manifest.get("channel_id")
    room = manifest.get("room") or "conversations"
    filed_at = manifest.get("archived_at") or _now()
    if source_file is None:
        source_file = str(batch_dir / SPANS_FILE)
        _purge_source(source_file)

    pieces: list[tuple[str, str]] = [
        (span.get("hall") or "assistant", piece)
        for span in manifest.get("spans", [])
        for piece in _chunks(span.get("text", ""))
    ]
    # The chunk count is known now, so the numbers can be taken in one atomic
    # bite. Reserving upstream at stage time could only ever guess from a
    # message count, which produced colliding numbers across checkpoints.
    first = _reserve_chunk_numbers(conversation_id, len(pieces))
    return _store_chunks([
        {
            "_id": _drawer_id(source_file, position),
            "text": piece,
            "wing": DEFAULT_WING,
            "room": room,
            "hall": hall,
            "source_file": source_file,
            "chunk_index": position,
            "chunk_number": first + position,
            "conversation_id": conversation_id,
            "channel": channel_id,
            "agent": agent,
            "filed_at": filed_at,
        }
        for position, (hall, piece) in enumerate(pieces)
    ])


def mine_directory(batch_dir: Path, *, agent: str = DEFAULT_WING) -> bool:
    """Mine one staged batch directory into the palace.

    A conversation batch carries a `spans.json` manifest (written at stage time,
    when the message structure is still known) and is filed span by span. Any
    other batch — a filed drawer, a Slack observation set — is chunked from its
    markdown, where the folder name gives the room.
    """
    manifest_path = batch_dir / SPANS_FILE
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("Unreadable spans manifest at %s: %s", manifest_path, exc)
            return False
        _mine_conversation_spans(batch_dir, manifest, agent)
        return True

    for path in sorted(batch_dir.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(batch_dir)
        room = relative.parts[0] if len(relative.parts) > 1 else "knowledge"
        channel_match = re.search(r"conversation_([^_]+)_", path.name)
        source_file = str(path)
        _purge_source(source_file)

        if room == "conversations":
            spans = _spans_from_markdown(text)
            if spans:
                _mine_conversation_spans(batch_dir, {
                    "channel_id": channel_match.group(1) if channel_match else None,
                    "conversation_id": f"archive:{batch_dir.name}",
                    "room": room,
                    "spans": spans,
                }, agent, source_file=source_file)
                continue

        _store_chunks([
            {
                "_id": _drawer_id(source_file, index),
                "text": chunk,
                "wing": DEFAULT_WING,
                "room": room,
                "hall": "general",
                "source_file": source_file,
                "chunk_index": index,
                "agent": agent,
                "channel": channel_match.group(1) if channel_match else None,
            }
            for index, chunk in enumerate(_chunks(text))
        ])
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


# List/browse fields. Inclusion — never pull embedding (384 floats) or
# future blobs and strip them after the round-trip.
_LIST_PROJECTION = {
    "_id": 1,
    "text": 1,
    "wing": 1,
    "room": 1,
    "hall": 1,
    "source_file": 1,
    "filed_at": 1,
    "topic": 1,
    "agent": 1,
    "chunk_index": 1,
    "chunk_number": 1,
    "conversation_id": 1,
    "channel": 1,
    "updated_at": 1,
}


def list_drawers(wing=None, room=None, hall=None, limit=50, offset=0) -> dict:
    match = {key: value for key, value in (("wing", wing), ("room", room), ("hall", hall)) if value is not None}
    collection = _collection()
    rows = collection.find(match, _LIST_PROJECTION).sort("filed_at", -1).skip(max(0, offset)).limit(max(1, limit))
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


def _kg():
    global _kg_indexed
    collection = _db()[KG]
    if not _kg_indexed:
        collection.create_index([("subject", 1), ("predicate", 1), ("object", 1)])
        collection.create_index([("valid_to", 1), ("valid_from", -1)])
        _kg_indexed = True
    return collection


def kg_add(subject, predicate, object, valid_from=None) -> str:
    """File a triple, or leave the existing open one alone.

    Upsert rather than insert: re-asserting a fact the agent already knows is
    normal (it happens on every consolidation pass), and a plain insert made a
    new open row each time, so the same relationship accumulated duplicates that
    invalidate then could not fully close.
    """
    result = _kg().update_one(
        {"subject": subject, "predicate": predicate, "object": object, "valid_to": None},
        {"$setOnInsert": {
            "subject": subject,
            "predicate": predicate,
            "object": object,
            "valid_from": valid_from or datetime.now(timezone.utc).date().isoformat(),
            "valid_to": None,
        }},
        upsert=True,
    )
    if result.upserted_id is None:
        return f"KG: already current — `{subject}` --[{predicate}]-> `{object}`"
    return f"KG: filed `{subject}` --[{predicate}]-> `{object}`"


def kg_query_rows(
    subject=None, predicate=None, object=None, limit=200,
    as_of=None, current_only=False,
) -> list[dict]:
    """Query triples, optionally as they stood on a date.

    ``as_of`` answers "what did we believe then" — a fact is in force if it
    started on or before that date and had not yet been invalidated.
    """
    match = {
        key: value for key, value in
        (("subject", subject), ("predicate", predicate), ("object", object))
        if value
    }
    if as_of:
        match["valid_from"] = {"$lte": as_of}
        match["$or"] = [{"valid_to": None}, {"valid_to": {"$gt": as_of}}]
    elif current_only:
        match["valid_to"] = None
    return list(_kg().find(match, {"_id": 0}).sort("valid_from", -1).limit(limit))


def kg_invalidate(subject, predicate, object, ended=None) -> str:
    """Close every open row for this triple, not just the first one found."""
    closed = _kg().update_many(
        {"subject": subject, "predicate": predicate, "object": object, "valid_to": None},
        {"$set": {"valid_to": ended or datetime.now(timezone.utc).date().isoformat()}},
    ).modified_count
    if not closed:
        return f"KG: nothing open to invalidate for `{subject}` --[{predicate}]-> `{object}`"
    return f"KG: invalidated `{subject}` --[{predicate}]-> `{object}` ({closed} row(s))"


WAKE_UP_MAX_CHARS = 3000


def segment_text(segment_id: str, limit: int = 400) -> str:
    """Reassemble one archived segment from the drawers it produced.

    The drawers are the durable copy: they carry `source_file` (which contains
    the staging batch's directory name) and `chunk_number` (which orders them),
    so a segment can be read back without the staged .md still existing. That
    matters because the shutdown path deletes staged batches once mined, and on
    Fargate the staging dir is not guaranteed across hosts at all.

    Returns "" when the segment produced no drawers, so the caller can decide
    whether to fall back to disk.
    """
    if not segment_id:
        return ""
    # Anchored to the batch directory, not to any path component: a bare
    # "conversations" would otherwise match every segment's .md path and splice
    # unrelated conversations together.
    anchored = re.escape(segment_id)
    rows = list(
        _collection()
        .find(
            {"source_file": {"$regex": f"/{anchored}/(spans\\.json|conversations/)"}},
            {"text": 1, "hall": 1, "chunk_number": 1, "chunk_index": 1},
        )
        .sort([("chunk_number", 1), ("chunk_index", 1)])
        .limit(max(1, limit))
    )
    if not rows:
        return ""
    parts, last_hall = [], None
    for row in rows:
        text = (row.get("text") or "").strip()
        if not text:
            continue
        hall = row.get("hall")
        # Span text already opens with its own "## user"/"## assistant" heading;
        # only add one when the chunk starts mid-span.
        if hall and hall != last_hall and not text.startswith(f"## {hall}"):
            parts.append(f"## {hall}")
        last_hall = hall
        parts.append(text)
    return "\n\n".join(parts)


def wake_up_text() -> str:
    """Standing palace digest for the dynamic system block.

    Deliberately excludes room=conversations: conversation chunks are the bulk
    of the palace and the newest of them are always whatever was just archived,
    so including them made this a re-injection of the last chat into every
    single prompt. What belongs here is what the agent learned, not what it
    just said. Hard-capped so it cannot grow into the context unbounded.
    """
    rows = list(
        _collection()
        .find({"room": {"$ne": "conversations"}}, _LIST_PROJECTION)
        .sort("filed_at", -1)
        .limit(20)
    )
    lines: list[str] = []
    budget = WAKE_UP_MAX_CHARS
    for row in rows:
        entry = f"- [{row.get('room', '?')}/{row.get('hall', '?')}] {row.get('text', '')[:400]}"
        if len(entry) > budget:
            break
        lines.append(entry)
        budget -= len(entry)
    return "\n\n".join(lines)


def close() -> None:
    global _client, _database, _indexed, _kg_indexed, _native_vector_search
    if _client is not None:
        _client.close()
    _client = None
    _database = None
    _indexed = False
    _kg_indexed = False
    _native_vector_search = False

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from semantic_router import Route
from semantic_router.routers import SemanticRouter

from .db_ops import get_db

log = logging.getLogger("galadriel.recall")

# semantic_router installs its own colorlog handler with propagate=False, then
# re-warns "No index provided" / "No config is written" on every router build.
# Both are expected: we deliberately use the in-memory LocalIndex, which has no
# config to persist. Respect the library's own env knob if an operator set it.
if "SEMANTIC_ROUTER_LOG_LEVEL" not in os.environ:
    logging.getLogger("semantic_router").setLevel(logging.ERROR)

RECALLS_COLLECTION = "recalls"
PROPOSED_RECALLS_COLLECTION = "proposed_recalls"

_ENCODER = None
_ENCODER_TYPE = None
_ROUTER_CACHE = None
_CACHED_RECALL_IDS = set()
_ROUTER_DIRTY = False

# Default per-recall Stage-1 positive floor. There is no negative counterpart:
# near-miss negatives share the same cosine band as true positives, so an absolute
# neg floor (e.g. 0.6) systematically over-vetoes when both scores clear ~0.6.
# Negatives are a Stage-2 veto only, scored relative to the winning positive.
DEFAULT_POSITIVE_THRESHOLD = 0.6
# Router encoder floor is open so per-recall positive_threshold can go below 0.6.
_ROUTER_SCORE_FLOOR = 0.0
# Lexical hard-hit score (exact cue match).
_LEXICAL_POSITIVE_SCORE = 1.0
# Tool-output chunks shorter than this many words never reach the embedding
# router — lexical cues only. Bare tool names / tiny args carry no semantic
# intent. Direct user messages never hit this gate (short queries like
# "who are you?" must reach the router).
_MIN_SEMANTIC_SCAN_WORDS = 4
# Structured tool output is not prose: JSON fragments ('"key": value', '{...')
# and ls -l rows embed close to everything and were the dominant mid-turn
# false-positive source (2026-08-16: 11 Stage-1 hits on trade JSON / directory
# listings, 11 Stage-2 rejects at ~0.50, ~72s of blocked turn). These skip the
# semantic router; lexical cues still run — same contract as the min-words gate.
# Applied to tool_output / tool_request segments only.
_STRUCTURED_CHUNK_RE = re.compile(
    r"^(?:"
    r"[\{\}\]\"']"                        # JSON/dict fragment starts
    r"|\[[\{\[\"\d]"                      # array-of-structure; NOT [Tower]: prefixes
    r"|[-bcdlps][rwxsStT-]{9}[.+@]?\s"    # ls -l permission column
    r")"
)
# URL hosts are the intent-bearing part; path/query slug length used to swing
# lexical and embedding scores. Collapse to scheme+host before scanning.
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

# Stage-1 flat-ranking diagnostic threshold (logged, never a veto). A gap below
# this used to reject every candidate; now we take a top-k union instead.
_STAGE1_MARGIN_DEFAULT = 0.05
# Dense router candidates kept per chunk before RRF fusion with lexical/fuzzy.
_STAGE1_DENSE_TOP_K = 5
# RRF constant (Cormack et al.); higher = flatter fusion.
_STAGE1_RRF_K = 60
# Fuzzy lexical: SequenceMatcher ratio floor for near-miss cue hits.
_FUZZY_LEXICAL_RATIO = 0.88
# Fuzzy matching is pure-Python SequenceMatcher and costs ~130 ms per 1 KB chunk
# across the cue catalog. It exists to absorb typos in what a human typed, so it
# runs on prose segments only and only on chunks short enough to be one utterance.
_FUZZY_MAX_CHUNK_CHARS = 240
# Segment sources that receive the tool-junk gates (min words / structured).
_TOOL_SEGMENT_SOURCES = frozenset({"tool_output", "tool_request", "tool"})

# A new cue at or above this cosine to an existing one adds no coverage: both
# stages accept on max(), so a near-duplicate is either never the argmax (dead
# weight) or a marginal region extension. Skipping it keeps the LRU window for
# cues that actually differ.
_CUE_SATURATION_DEFAULT = 0.9
# Max candidates verified per Stage-2 pass, best Stage-1 positive first.
# Each candidate costs seconds on CPU Fargate, so an unbounded Stage-1 burst
# (11 candidates on one tool result, 2026-08-16) makes the turn latency
# unbounded too. Overflow is logged as rejected with reason
# stage2_candidate_cap. Lexical hits score 1.0 and always survive the cut.
_STAGE2_MAX_CANDIDATES_DEFAULT = 3


def _clamp_threshold(value, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return max(0.0, min(1.0, v))


def recall_positive_threshold(recall: dict | None) -> float:
    if not isinstance(recall, dict):
        return DEFAULT_POSITIVE_THRESHOLD
    return _clamp_threshold(
        recall.get("positive_threshold"), DEFAULT_POSITIVE_THRESHOLD
    )


def normalize_recall_thresholds(recall: dict) -> dict:
    """Ensure positive_threshold is present with a default; drop legacy fields.

    `negative_threshold` used to gate Stage-1 and is stripped on read, so stored
    copies age out without a migration.
    """
    if not isinstance(recall, dict):
        return recall
    recall.pop("threshold", None)
    recall.pop("negative_threshold", None)
    recall["positive_threshold"] = recall_positive_threshold(recall)
    return recall


def recall_activation_condition(recall: dict | None) -> str:
    """Matching target for Stage-2. Falls back to instruction for unmigrated rows."""
    if not isinstance(recall, dict):
        return ""
    text = (recall.get("activation_condition") or "").strip()
    if text:
        return text
    return (recall.get("instruction") or "").strip()


def recall_exclusions(recall: dict | None) -> str:
    if not isinstance(recall, dict):
        return ""
    return (recall.get("exclusions") or "").strip()


def normalize_scan_text(text: str) -> str:
    """Collapse URLs to scheme+host so path slug length cannot swing a verdict."""
    if not text:
        return ""

    def _host_only(match: re.Match) -> str:
        url = match.group(0)
        try:
            from urllib.parse import urlparse

            parsed = urlparse(url)
            if parsed.scheme and parsed.netloc:
                return f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            pass
        return url

    return _URL_RE.sub(_host_only, text)


def _is_tool_segment(source: str | None) -> bool:
    return (source or "user").strip().lower() in _TOOL_SEGMENT_SOURCES


def _should_run_semantic_router(chunk: str, source: str | None) -> bool:
    """Min-words and structured-chunk gates apply to tool segments only."""
    if _is_tool_segment(source):
        if len(chunk.split()) < _MIN_SEMANTIC_SCAN_WORDS:
            return False
        if _STRUCTURED_CHUNK_RE.match(chunk):
            return False
    return True


def get_encoder(force_type=None):
    """Lazily load and cache the encoder model based on environment config."""
    global _ENCODER, _ENCODER_TYPE, _ROUTER_CACHE, _CACHED_RECALL_IDS

    encoder_type = (force_type or os.environ.get("RECALL_ENCODER", "fastembed")).lower()

    if _ENCODER is not None and _ENCODER_TYPE == encoder_type:
        if getattr(_ENCODER, "score_threshold", None) != _ROUTER_SCORE_FLOOR:
            _ENCODER.score_threshold = _ROUTER_SCORE_FLOOR
            if _ROUTER_CACHE is not None:
                _ROUTER_CACHE.score_threshold = _ROUTER_SCORE_FLOOR
        return _ENCODER

    # If encoder type changed, clear the router cache
    if _ENCODER is not None:
        _ROUTER_CACHE = None
        _CACHED_RECALL_IDS = set()

    if encoder_type == "gemini":
        try:
            from semantic_router.encoders import GoogleEncoder
            _ENCODER = GoogleEncoder(name="models/text-embedding-004")
            _ENCODER.score_threshold = _ROUTER_SCORE_FLOOR
            _ENCODER_TYPE = "gemini"
            log.info("Initialized Gemini encoder for semantic router")
        except Exception as e:
            log.warning(f"Failed to load GoogleEncoder, falling back to fastembed: {e}")
            encoder_type = "fastembed"

    if encoder_type == "fastembed":
        from semantic_router.encoders import FastEmbedEncoder
        _ENCODER = FastEmbedEncoder(name="BAAI/bge-small-en-v1.5")
        _ENCODER.score_threshold = _ROUTER_SCORE_FLOOR
        _ENCODER_TYPE = "fastembed"
        log.info("Initialized FastEmbedEncoder for semantic router")

    return _ENCODER


def invalidate_semantic_router() -> None:
    """Mark the Stage-1 index stale; the next scan rebuilds it exactly once.

    A cue edit changes a route's utterances without changing the recall-id set,
    which is all the cache key can see — so writers must invalidate explicitly.
    Deferring to the next scan collapses a burst of writes (a learn pass patches
    every recall it touched) into one rebuild instead of one rebuild per write.
    """
    global _ROUTER_DIRTY
    _ROUTER_DIRTY = True


def get_semantic_router(recalls: list[dict], force_encoder_type=None) -> SemanticRouter:
    """Get or build the SemanticRouter for the current set of recalls."""
    global _ROUTER_CACHE, _CACHED_RECALL_IDS, _ROUTER_DIRTY

    current_ids = {r.get("recall_id") for r in recalls if r.get("recall_id")}

    encoder = get_encoder(force_encoder_type)

    if _ROUTER_CACHE is not None and current_ids == _CACHED_RECALL_IDS and not _ROUTER_DIRTY:
        return _ROUTER_CACHE

    routes = []
    for recall in recalls:
        recall_id = recall.get("recall_id")
        if not recall_id:
            continue

        utterances = recall.get("positive_examples", [])[:]
        if not utterances:
            for tag in recall.get("regex_tags", []):
                # Clean up the legacy regex tags into natural language utterances
                clean = tag.replace("\\b", "").replace("(", "").replace(")", "").replace("\\s*", " ").replace(".*", " ")
                clean = clean.replace("?", "").replace("\\", "")
                utterances.extend([u.strip() for u in clean.split("|") if u.strip()])

        if utterances:
            routes.append(Route(name=recall_id, utterances=utterances))

    if not routes:
        return None

    _ROUTER_CACHE = SemanticRouter(encoder=encoder, routes=routes, auto_sync="local")
    _CACHED_RECALL_IDS = current_ids
    _ROUTER_DIRTY = False
    return _ROUTER_CACHE


def _load_system_recalls() -> list[dict]:
    """Load system defaults from config/system_recalls.json."""
    config_path = Path("config/system_recalls.json")
    if not config_path.exists():
        return []
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            for recall in data:
                recall["source"] = "system"
                normalize_recall_thresholds(recall)
            return data
    except Exception as e:
        log.error(f"Failed to load system recalls: {e}")
        return []


async def fetch_all_recalls() -> list[dict]:
    """Fetch both system recalls and user-defined recalls from DB."""
    recalls = _load_system_recalls()
    db = get_db()
    if db is not None:
        try:
            coll = db[RECALLS_COLLECTION]
            async for doc in coll.find({"enabled": {"$ne": False}}):
                doc["source"] = "user"
                doc["recall_id"] = str(doc.pop("_id"))
                normalize_recall_thresholds(doc)
                recalls.append(doc)
        except Exception as e:
            log.error(f"Failed to fetch user recalls from DB: {e}")
    return recalls


def sanitize_text_with_exclude_texts(text: str, exclude_texts: list[str] | None = None) -> str:
    """Strip exact prior recall-fire texts and their lines from text before semantic scanning."""
    if not text:
        return ""
    cleaned = text
    if exclude_texts:
        for ex in exclude_texts:
            if ex and isinstance(ex, str):
                cleaned = cleaned.replace(ex, "")

        ex_lines = set()
        for ex in exclude_texts:
            if isinstance(ex, str):
                for line in ex.split("\n"):
                    s = line.strip()
                    if len(s) > 5:
                        ex_lines.add(s)

        cleaned_lines = []
        for line in cleaned.split("\n"):
            s = line.strip()
            if s and s in ex_lines:
                continue
            cleaned_lines.append(line)
        cleaned = "\n".join(cleaned_lines)
    return cleaned.strip()


# bge-small-en-v1.5: hard max 512 tokens, 384-dim. For low-dim embedders,
# ~150-300 token windows keep semantics sharp; ~20% overlap covers boundary phrases.
_RECALL_CHUNK_TOKENS = 256
_RECALL_CHUNK_OVERLAP = 50

# Sentinel so a failed tokenizer lookup is cached instead of retried per line.
_UNSET = object()
_EMBED_TOKENIZER = _UNSET


def _embedding_tokenizer():
    """Untruncated copy of the encoder's tokenizer, or None if unavailable.

    The encoder's live tokenizer pins truncation at 512, so it reports 512 for
    anything longer and cannot say how much it dropped. A copy with truncation
    off is what lets the splitter measure a line before the model silently cuts
    it. Non-fastembed encoders (gemini) and any upstream attribute rename fall
    back to the word approximation rather than breaking the scan.
    """
    global _EMBED_TOKENIZER
    if _EMBED_TOKENIZER is not _UNSET:
        return _EMBED_TOKENIZER
    _EMBED_TOKENIZER = None
    try:
        from tokenizers import Tokenizer

        live = get_encoder()._client.model.tokenizer
        raw = Tokenizer.from_str(live.to_str())
        raw.no_truncation()
        _EMBED_TOKENIZER = raw
    except Exception as e:
        log.warning(
            "Embedding tokenizer unavailable (%s); chunking by word approximation", e
        )
    return _EMBED_TOKENIZER


def _split_by_true_tokens(
    line: str, tokenizer, max_tokens: int, overlap_tokens: int
) -> list[str] | None:
    """Window a line by real token count, slicing on token offsets. None on failure.

    Offsets keep the returned text byte-identical to the input, so lexical cue
    matching and logging see the original characters rather than a detokenized
    approximation. Special tokens ([CLS]/[SEP]) are zero-width and dropped here;
    they still count against the model's 512 at encode time, which is why the
    window sits well below it.
    """
    try:
        spans = [(s, e) for s, e in tokenizer.encode(line).offsets if e > s]
    except Exception as e:
        log.warning("Tokenizing line for chunking failed (%s); using word approximation", e)
        return None
    if len(spans) <= max_tokens:
        return [line]
    step = max(1, max_tokens - overlap_tokens)
    chunks: list[str] = []
    for start in range(0, len(spans), step):
        window = spans[start:start + max_tokens]
        if not window:
            break
        piece = line[window[0][0]:window[-1][1]].strip()
        if piece:
            chunks.append(piece)
        if start + max_tokens >= len(spans):
            break
    return chunks


def _split_line_for_embedding(
    line: str,
    max_tokens: int | None = None,
    overlap_tokens: int | None = None,
) -> list[str]:
    """Split one newline chunk into embedding-sized windows, with overlap.

    Counts real tokens, not words. The two diverge hard on machine output: a
    minified JSON body or CSV row has no spaces at all, so `str.split()` sees
    one "word" and never splits, handing the embedder a 6000-token line that
    fastembed truncates at 512 — 92% of it discarded with no signal that it
    happened. Prose is unaffected (~1.0 tokens/word).
    """
    if not line:
        return []
    if max_tokens is None:
        max_tokens = _RECALL_CHUNK_TOKENS
    if overlap_tokens is None:
        overlap_tokens = _RECALL_CHUNK_OVERLAP

    tokenizer = _embedding_tokenizer()
    if tokenizer is not None:
        chunks = _split_by_true_tokens(line, tokenizer, max_tokens, overlap_tokens)
        if chunks is not None:
            return chunks

    words = line.split()
    if len(words) <= max_tokens:
        return [line]
    step = max(1, max_tokens - overlap_tokens)
    chunks: list[str] = []
    for start in range(0, len(words), step):
        piece = words[start:start + max_tokens]
        if not piece:
            break
        chunks.append(" ".join(piece))
        if start + max_tokens >= len(words):
            break
    return chunks


# Sentence boundary: terminator, optional closing quote/bracket, then whitespace.
# Deliberately does NOT require a capital after the break — chat prose is mostly
# lowercase, and requiring one made this split nothing at all on real messages.
# Rule-based on purpose, and measured rather than assumed. A neural segmenter
# (SaT / wtpsplit, ONNX CPU) was benchmarked against this on 2026-08-18 and
# rejected. It segments far better in isolation — 0.90 boundary F1 for sat-3l-sm
# and 0.94 for sat-12l-sm where this scores 0.00, on text with the terminators
# stripped — but every scan document whose lines are already newline-separated
# came out identical under both. The whole gain sat in unpunctuated run-on
# messages, worth 3 recoveries of one recall, against +14% Stage-1 false-positive
# proposals (each a Stage-2 forward pass), +680 MB resident and 1.5x scan
# latency. A boundary error here is graceful anyway: both halves are still
# scanned and lexical cues still match.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])[\"')\]]*\s+(?=\S)")
# Terminators that end an abbreviation rather than a sentence. Checked against
# the tail of the candidate left-hand side, lowercased.
_ABBREVIATIONS = (
    "e.g.", "i.e.", "etc.", "vs.", "cf.", "al.", "approx.", "dr.", "mr.", "mrs.",
    "ms.", "prof.", "sr.", "jr.", "st.", "no.", "fig.", "inc.", "ltd.", "co.",
)
# A unit shorter than this is a fragment ("It's this:") — not self-contained
# enough to embed on its own, so it joins the next unit. Dense X Retrieval
# (EMNLP 2024) is explicit that the retrieval unit has to stand alone. Swept 3/4/5
# on the chunk dataset: all three recover buried-paragraph recall in full, so 4 is
# chosen for the low false-positive count and because it leaves genuine four-word
# questions ("who are you really?") standing on their own.
_MIN_UNIT_WORDS = 4


def _sentence_split(line: str) -> list[str]:
    """Split one line into sentences, then glue fragments onto their successor."""
    parts: list[str] = []
    start = 0
    for match in _SENTENCE_SPLIT_RE.finditer(line):
        left = line[start:match.start()]
        stripped = left.rstrip().lower()
        if any(stripped.endswith(a) for a in _ABBREVIATIONS):
            continue
        # "3. " in a numbered list, or a decimal, is not a sentence end.
        if re.search(r"(?:^|\s)\d+\.$", stripped):
            continue
        piece = left.strip()
        if piece:
            parts.append(piece)
        start = match.end()
    tail = line[start:].strip()
    if tail:
        parts.append(tail)
    if not parts:
        return []

    merged: list[str] = []
    pending = ""
    for piece in parts:
        candidate = f"{pending} {piece}".strip() if pending else piece
        if len(candidate.split()) < _MIN_UNIT_WORDS:
            pending = candidate
            continue
        merged.append(candidate)
        pending = ""
    if pending:
        if merged:
            merged[-1] = f"{merged[-1]} {pending}"
        else:
            merged.append(pending)
    return merged


def split_scan_units(text: str) -> list[str]:
    """The exact units Stage-1 will score for one segment.

    Newline, then URL, then sentence. Splitting only on newlines let an
    unwrapped paragraph embed as one vector, which averages a cue into the
    paragraph's topic centroid: on the chunk dataset's long_paragraph family
    that cost 37.5 points of Stage-1 recall (0.625 -> 1.0 here), for +5.8%
    chunks and no measurable latency penalty. A URL becomes its own unit
    because it is the intent-bearing token in a line like "can you read this:
    https://medium.com", where it would otherwise be diluted by the sentence
    around it.

    Public because the chunking eval has to measure what production does rather
    than keep its own copy of the rules.
    """
    units: list[str] = []
    for line in (c.strip() for c in text.split("\n")):
        if not line:
            continue
        pos = 0
        for match in _URL_RE.finditer(line):
            before = line[pos:match.start()].strip()
            if before:
                units.extend(_sentence_split(before))
            units.append(match.group(0))
            pos = match.end()
        rest = line[pos:].strip()
        if rest:
            units.extend(_sentence_split(rest))
    # Oversized units (minified JSON, CSV rows) still need the token windower.
    return [sub for unit in units for sub in _split_line_for_embedding(unit)]


def _as_float_score(score) -> float | None:
    if score is None or score == "N/A":
        return None
    try:
        return float(score.item()) if hasattr(score, "item") else float(score)
    except (TypeError, ValueError):
        return None


def _argmax_cosine(encoder, text: str, examples: list[str]) -> tuple[float, str] | None:
    """Return the best (cosine, example) pair, or None if nothing is scorable."""
    cleaned = [e.strip() for e in examples if isinstance(e, str) and e.strip()]
    if not cleaned or not text:
        return None
    try:
        vectors = encoder([text] + cleaned)
        if not vectors or len(vectors) < 2:
            return None
        query = np.asarray(vectors[0], dtype=np.float64)
        q_norm = np.linalg.norm(query)
        if q_norm == 0:
            return None
        best: tuple[float, str] | None = None
        for emb, example in zip(vectors[1:], cleaned):
            vec = np.asarray(emb, dtype=np.float64)
            v_norm = np.linalg.norm(vec)
            if v_norm == 0:
                continue
            sim = float(np.dot(query, vec) / (q_norm * v_norm))
            if best is None or sim > best[0]:
                best = (sim, example)
        return best
    except Exception as e:
        log.warning(f"Failed to score cue examples: {e}")
        return None


def _max_cosine(encoder, text: str, examples: list[str]) -> float | None:
    """Return max cosine similarity between text and examples, or None if no examples."""
    hit = _argmax_cosine(encoder, text, examples)
    return None if hit is None else hit[0]


def top_k_vector_indices(query_vector, vectors: list, k: int) -> list[int]:
    """Indices of the k vectors closest to `query_vector` by cosine, best first.

    Takes vectors rather than text so a caller that has already stored its
    embeddings does not have to re-encode a corpus to rank it. [] on any
    failure, which callers treat as "no candidates".
    """
    if not vectors or k <= 0:
        return []
    try:
        query = np.asarray(query_vector, dtype=np.float64)
        q_norm = np.linalg.norm(query)
        if q_norm == 0:
            return []
        scored: list[tuple[float, int]] = []
        for index, raw in enumerate(vectors):
            if raw is None:
                continue
            vec = np.asarray(raw, dtype=np.float64)
            v_norm = np.linalg.norm(vec)
            if v_norm == 0 or vec.shape != query.shape:
                continue
            scored.append((float(np.dot(query, vec) / (q_norm * v_norm)), index))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [index for _, index in scored[:k]]
    except Exception as e:
        log.warning(f"Failed to rank vectors: {e}")
        return []


def _top_k_cosine(encoder, text: str, examples: list[str], k: int) -> list[str]:
    """The k examples most cosine-similar to text, best first. [] on any failure."""
    cleaned = [e.strip() for e in examples if isinstance(e, str) and e.strip()]
    if not cleaned or not text or k <= 0:
        return []
    try:
        vectors = encoder([text] + cleaned)
        if not vectors or len(vectors) < 2:
            return []
        query = np.asarray(vectors[0], dtype=np.float64)
        q_norm = np.linalg.norm(query)
        if q_norm == 0:
            return []
        scored: list[tuple[float, str]] = []
        for emb, example in zip(vectors[1:], cleaned):
            vec = np.asarray(emb, dtype=np.float64)
            v_norm = np.linalg.norm(vec)
            if v_norm == 0:
                continue
            scored.append((float(np.dot(query, vec) / (q_norm * v_norm)), example))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [example for _, example in scored[:k]]
    except Exception as e:
        log.warning(f"Failed to rank cue examples: {e}")
        return []


def normalize_lexical_cue(cue: str) -> str:
    """Lowercase, collapse whitespace, replace underscores with spaces."""
    if not isinstance(cue, str):
        return ""
    return " ".join(cue.strip().replace("_", " ").casefold().split())


def _lexical_hit(chunk: str, cues: list) -> str | None:
    """Return the first lexical cue that matches chunk with word boundaries, else None."""
    if not chunk or not cues:
        return None
    text = chunk.casefold()
    for raw in cues:
        cue = normalize_lexical_cue(raw)
        if not cue:
            continue
        if re.search(rf"(?<!\w){re.escape(cue)}(?!\w)", text):
            return cue
    return None


def _fuzzy_lexical_hit(chunk: str, cues: list, *, source: str | None = None) -> str | None:
    """Near-miss lexical anchor via SequenceMatcher (typos in typed prose).

    Skipped for tool segments and long chunks: machine output has no typos to
    absorb, and the matcher is quadratic pure Python (see _FUZZY_MAX_CHUNK_CHARS).
    """
    if not chunk or not cues:
        return None
    if _is_tool_segment(source):
        return None
    text = " ".join(chunk.casefold().split())
    if not text or len(text) > _FUZZY_MAX_CHUNK_CHARS:
        return None

    from difflib import SequenceMatcher

    words = text.split()
    best_cue = None
    best_ratio = 0.0
    for raw in cues:
        cue = normalize_lexical_cue(raw)
        if not cue or len(cue) < 4:
            continue
        # Exact substrings are _lexical_hit's job; it already ran and missed.
        n = len(cue.split())
        # Compare the cue against same-length word windows only.
        for i in range(max(1, len(words) - n + 1)):
            piece = " ".join(words[i : i + n])
            ratio = SequenceMatcher(None, cue, piece).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_cue = cue
            if best_ratio >= 0.99:
                break
    if best_cue is not None and best_ratio >= _FUZZY_LEXICAL_RATIO:
        return best_cue
    return None


def _rrf_fuse(
    ranked_lists: list[list[tuple[str, float]]],
    *,
    k: int = _STAGE1_RRF_K,
) -> list[tuple[str, float]]:
    """Reciprocal rank fusion over (id, score) lists. Returns (id, rrf_score)."""
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, (rid, _) in enumerate(ranked):
            if not rid:
                continue
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda pair: pair[1], reverse=True)


# Ordering of evidence kinds when one recall is proposed by several chunks. An
# exact cue is a hand-written hard trigger and outranks any cosine, so it wins
# even against a higher-scoring semantic neighbour.
_MATCH_SOURCE_RANK = {"lexical": 2, "fuzzy": 1, "semantic": 0}


def _proposal_strength(match: dict) -> tuple[int, float]:
    return (
        _MATCH_SOURCE_RANK.get(match.get("match_source"), 0),
        _as_float_score(match.get("positive_score")) or 0.0,
    )


def _accept_candidate(
    *,
    recall: dict,
    recall_id: str,
    chunk: str,
    positive_score: float,
    match_source: str,
    segment_source: str | None = None,
    lexical_cue: str | None = None,
) -> dict | None:
    """Build the match for one chunk's proposal, or None if it misses the floor.

    Semantic accepts at pos >= pos_thr; lexical is a hard hit. Stage-1 is
    positive-only: negative examples never gate here. Near-miss negatives share
    the positive cosine band, so any Stage-1 neg veto (absolute or relative)
    randomly blocks true hits while letting real false positives through.
    Negatives remain stored on the recall as Stage-2 / tune_recall
    counter-signal only.
    """
    pos_thr = recall_positive_threshold(recall)

    if match_source == "semantic" and positive_score < pos_thr:
        log.info(
            f"[Semantic Miss] route='{recall_id}' source={match_source} "
            f"pos={positive_score:.4f} < pos_thr={pos_thr:.4f} chunk='{chunk[:100]}'"
        )
        return None

    cue_note = f" cue={lexical_cue!r}" if lexical_cue else ""
    log.info(
        f"[Semantic Match] route='{recall_id}' source={match_source} "
        f"pos={positive_score:.4f}>={pos_thr:.4f}"
        f"{cue_note} chunk='{chunk[:100]}'"
    )
    match_obj = dict(recall)
    normalize_recall_thresholds(match_obj)
    match_obj["positive_score"] = positive_score
    match_obj["negative_score"] = None
    match_obj["match_source"] = match_source
    match_obj["matched_chunk"] = chunk
    # Which conversation segment the chunk came from (user / thought /
    # tool_request / tool_output) — distinct from match_source (lexical /
    # fuzzy / semantic, i.e. HOW it matched). Surfaced in the fire text so the
    # agent grounds tune_recall feedback in the real trigger instead of
    # guessing from whatever else is in its context window.
    if segment_source:
        match_obj["segment_source"] = segment_source
    if lexical_cue:
        match_obj["lexical_cue"] = lexical_cue
    return match_obj


def _rank_decisions(decisions, recall_map: dict, chunk: str) -> list[tuple[str, float]]:
    """(recall_id, score) for scorable, known routes, best first."""
    ranked: list[tuple[str, float]] = []
    for decision in decisions or []:
        name = getattr(decision, "name", None)
        if not name or name == "None" or name not in recall_map:
            continue
        score = _as_float_score(getattr(decision, "similarity_score", None))
        if score is None:
            log.info(
                f"[Semantic Match] route='{name}' missing positive_score; "
                f"treating as miss chunk='{chunk[:100]}'"
            )
            continue
        ranked.append((name, score))
    ranked.sort(key=lambda pair: pair[1], reverse=True)
    return ranked


def _margin_accepts(
    ranked: list[tuple[str, float]], margin: float
) -> list[tuple[str, float]]:
    """Legacy single-winner gate (kept for chunking eval / diagnostics only).

    Production Stage-1 uses `_candidate_union` instead: a flat ranking is a
    signal to verify more candidates, not to reject all of them.
    """
    if not ranked:
        return []
    if margin <= 0:
        return ranked
    if len(ranked) == 1:
        return ranked
    return ranked[:1] if (ranked[0][1] - ranked[1][1]) >= margin else []


def _candidate_union(
    dense_ranked: list[tuple[str, float]],
    lexical_hits: list[tuple[str, float]],
    fuzzy_hits: list[tuple[str, float]],
    *,
    dense_top_k: int = _STAGE1_DENSE_TOP_K,
    cap: int | None = None,
) -> list[tuple[str, float]]:
    """Fuse dense top-k with lexical/fuzzy anchors via RRF; cap survivors.

    Exact lexical cues are hand-written hard triggers, so they are seeded ahead
    of the fused order and can never be squeezed out by dense neighbours.
    """
    if cap is None:
        cap = _stage2_max_candidates()
    cap = max(1, cap)
    dense = dense_ranked[: max(0, dense_top_k)]
    fused = _rrf_fuse([dense, lexical_hits, fuzzy_hits])
    if not fused:
        return []
    fused_scores = dict(fused)
    forced = [rid for rid, _ in lexical_hits][:cap]
    out = [(rid, fused_scores.get(rid, 0.0)) for rid in forced]
    for rid, score in fused:
        if len(out) >= cap:
            break
        if rid not in forced:
            out.append((rid, score))
    return out


def scan_text_for_recalls(
    text: str | None = None,
    recalls: list[dict] | None = None,
    exclude_texts: list[str] | None = None,
    force_encoder_type=None,
    segments: list[dict] | None = None,
) -> list[dict]:
    """Scan text: semantic OR lexical/fuzzy cue hit, gated by the positive floor.

    `segments` is an optional list of `{text, source}` dicts. `source` is one of
    user / thought / tool_request / tool_output. Min-words and structured-chunk
    gates apply only to tool segments so short user queries still reach the
    router. When `segments` is omitted the whole `text` is treated as `user`.
    """
    if recalls is None:
        recalls = []
    if not recalls:
        return []

    if not recall_system_armed():
        return []

    if segments:
        prepared: list[tuple[str, str]] = []
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            raw = normalize_scan_text(str(seg.get("text") or ""))
            cleaned = sanitize_text_with_exclude_texts(raw, exclude_texts)
            if cleaned:
                prepared.append((cleaned, str(seg.get("source") or "user")))
    else:
        if not text:
            return []
        cleaned = sanitize_text_with_exclude_texts(
            normalize_scan_text(text), exclude_texts
        )
        if not cleaned:
            return []
        prepared = [(cleaned, "user")]

    if not prepared:
        return []

    router = get_semantic_router(recalls, force_encoder_type)
    recall_map = {r.get("recall_id"): r for r in recalls if r.get("recall_id")}
    if not recall_map:
        return []

    # recall_id -> its strongest proposal across every chunk of every segment.
    # Keeping the first proposal instead made the whole scan order-dependent: a
    # weak semantic hit on an early line claimed the recall, and the line that
    # actually triggered it — often an exact cue scoring 1.0 — was skipped, so
    # Stage-2 was handed evidence that genuinely did not match and rejected it.
    best: dict[str, dict] = {}
    margin = _stage1_margin()

    for sanitized_text, source in prepared:
        chunks = split_scan_units(sanitized_text)

        for chunk in chunks:
            dense_ranked: list[tuple[str, float]] = []
            if router is not None and _should_run_semantic_router(chunk, source):
                decisions = router(chunk, limit=_STAGE1_DENSE_TOP_K)
                if decisions and not isinstance(decisions, list):
                    decisions = [decisions]
                dense_ranked = _rank_decisions(decisions, recall_map, chunk)
                # Flat ranking is a diagnostic, not a veto.
                if (
                    margin > 0
                    and len(dense_ranked) >= 2
                    and (dense_ranked[0][1] - dense_ranked[1][1]) < margin
                ):
                    log.info(
                        f"[Stage1 Flat] top={dense_ranked[0][0]!r} "
                        f"pos={dense_ranked[0][1]:.4f} "
                        f"runner_up={dense_ranked[1][1]:.4f} gap<{margin:.4f} "
                        f"source={source} chunk='{chunk[:100]}'"
                    )

            lexical_hits: list[tuple[str, float]] = []
            fuzzy_hits: list[tuple[str, float]] = []
            cue_by_id: dict[str, str] = {}
            for recall_id, recall in recall_map.items():
                cues = recall.get("lexical_cues") or []
                hit_cue = _lexical_hit(chunk, cues)
                if hit_cue:
                    lexical_hits.append((recall_id, _LEXICAL_POSITIVE_SCORE))
                    cue_by_id[recall_id] = hit_cue
                    continue
                fuzzy = _fuzzy_lexical_hit(chunk, cues, source=source)
                if fuzzy:
                    fuzzy_hits.append((recall_id, 0.95))
                    cue_by_id[recall_id] = fuzzy

            # Filter dense by per-recall positive floor before fusion.
            dense_cleared = [
                (rid, score)
                for rid, score in dense_ranked
                if score >= recall_positive_threshold(recall_map[rid])
            ]
            accepted = _candidate_union(dense_cleared, lexical_hits, fuzzy_hits)

            lex_map = {rid: score for rid, score in lexical_hits}
            fuzzy_map = {rid: score for rid, score in fuzzy_hits}
            dense_map = {rid: score for rid, score in dense_cleared}

            for recall_id, fused_score in accepted:
                if recall_id in lex_map:
                    match_source = "lexical"
                    positive_score = lex_map[recall_id]
                    lexical_cue = cue_by_id.get(recall_id)
                elif recall_id in fuzzy_map:
                    match_source = "fuzzy"
                    positive_score = fuzzy_map[recall_id]
                    lexical_cue = cue_by_id.get(recall_id)
                else:
                    match_source = "semantic"
                    positive_score = dense_map.get(recall_id, fused_score)
                    lexical_cue = None
                candidate = _accept_candidate(
                    recall=recall_map[recall_id],
                    recall_id=recall_id,
                    chunk=chunk,
                    positive_score=positive_score,
                    match_source=match_source,
                    segment_source=source,
                    lexical_cue=lexical_cue,
                )
                if candidate is None:
                    continue
                incumbent = best.get(recall_id)
                # Strictly greater, so an earlier chunk holds a tie and the
                # result stays deterministic.
                if incumbent is None or _proposal_strength(candidate) > _proposal_strength(
                    incumbent
                ):
                    best[recall_id] = candidate

    matches = list(best.values())

    if matches:
        log.debug(
            "Semantic scan matched %s rule(s) for text: %s...",
            len(matches),
            (text or prepared[0][0])[:200],
        )

    return matches


# Display labels for the segment a fired chunk was scanned from (see
# `_build_tool_use_recall_scan_segments`). Distinct from `match_source`
# (lexical/fuzzy/semantic — HOW it matched), this is WHERE it came from.
_SEGMENT_SOURCE_LABELS = {
    "user": "the user's message",
    "thought": "your own thought",
    "tool_request": "your tool call",
    "tool_output": "a tool result",
}
# Fire-text snippet cap: long enough to be recognizable, short enough to stay
# a pointer, not an essay (matches the Tower recall-test preview truncation).
_FIRE_TEXT_SNIPPET_CHARS = 160


def generate_recall_fire_text(matched_recalls: list[dict]) -> str:
    """Injected recall-fire text: `- [recall_id] instruction` per match, plus
    a short provenance line naming what actually matched.

    The id must be in the visible text. The stable block tells the agent to call
    `tune_recall(recall_id, ...)` after a fire, but the id previously lived only
    in the message's `matched_recall_ids` metadata, which the model never sees —
    so it invented plausible ids (`sys_worker_board`, `sys_cookbooks`) and every
    feedback call failed.

    The same blind spot existed for WHAT matched: with no visible evidence of
    the triggering segment, a model asked to judge applicability will
    rationalize a plausible-sounding but wrong cause instead of admitting it
    doesn't know — observed 2026-08-18, where a fire caused by the agent's own
    thought ("...worker control...") got a tune_recall note blaming the user's
    unrelated greeting. `matched_chunk` + `segment_source` are already computed
    by Stage-1; naming them here removes the guesswork rather than prompting
    around it.
    """
    if not matched_recalls:
        return ""

    fire_lines = []
    for recall in matched_recalls:
        rid = (recall.get("recall_id") or "").strip()
        instruction = recall.get("instruction")
        line = f"- [{rid}] {instruction}" if rid else f"- {instruction}"
        chunk = _strip_channel_prefix(recall.get("matched_chunk") or "")
        if chunk:
            label = _SEGMENT_SOURCE_LABELS.get(
                recall.get("segment_source"), "the scanned text"
            )
            snippet = chunk[:_FIRE_TEXT_SNIPPET_CHARS]
            if len(chunk) > _FIRE_TEXT_SNIPPET_CHARS:
                snippet += "…"
            line += f'\n  matched {label}: "{snippet}"'
        fire_lines.append(line)

    return "\n".join(fire_lines)


def _judge_verify_enabled() -> bool:
    """Master Stage-2 on/off switch."""
    raw = os.environ.get("RECALL_JUDGE_VERIFY") or "1"
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _judge_model_for_status() -> str:
    from . import tower_settings
    from .recall_judge import resolve_judge_model

    try:
        return resolve_judge_model()
    except Exception:
        return tower_settings.DEFAULT_RECALL_JUDGE_MODEL


def get_recall_status() -> dict:
    """Status payload for Tower UI / API (Stage-2 is judge-only)."""
    return {
        "enabled": _judge_verify_enabled(),
        "judge_model": _judge_model_for_status(),
        "armed": recall_system_armed(),
    }


def _stage2_max_candidates() -> int:
    raw = (os.environ.get("RECALL_STAGE2_MAX_CANDIDATES") or "").strip()
    if not raw:
        return _STAGE2_MAX_CANDIDATES_DEFAULT
    try:
        return max(1, int(raw))
    except ValueError:
        return _STAGE2_MAX_CANDIDATES_DEFAULT


def _stage1_margin() -> float:
    raw = (os.environ.get("RECALL_STAGE1_MARGIN") or "").strip()
    if not raw:
        return _STAGE1_MARGIN_DEFAULT
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _STAGE1_MARGIN_DEFAULT


_DISARM_LOGGED = False


def recall_system_armed() -> bool:
    """Master switch: a recall system without its verifier must not run at all.

    Armed when the credential for the SELECTED judge model's provider is
    present; otherwise disarm (fail-closed). The judge is provider-generic and
    defaults to a Bedrock model, so this must follow `RECALL_JUDGE_MODEL` — a
    vendor-specific key check disarmed the whole system whenever that one
    vendor's key was absent, no matter which provider actually serves the
    judge. RECALL_JUDGE_VERIFY=0 is a deliberate operator choice to disable
    Stage-2 entirely; that path stays armed since there is nothing to verify.
    """
    global _DISARM_LOGGED
    if not _judge_verify_enabled():
        return True
    from . import model_registry, tower_settings
    from .recall_judge import peek_judge_model

    try:
        # No I/O on this path: it gates every scan, and the agent calls
        # scan_text_for_recalls inline on the event loop. Env wins, then
        # whatever the judge's own async path has already cached, then the
        # documented default — a Tower override applies from the next scan
        # after the judge refreshes that cache.
        # Each candidate is validated the way the judge validates it. An
        # unlisted name resolves to the Ollama provider, which needs no
        # credential, so an unnormalized value would arm the system OPEN on
        # exactly the typo that stops the judge working.
        model = next(
            (
                m for m in (
                    tower_settings.normalize_recall_judge_model(
                        os.environ.get("RECALL_JUDGE_MODEL")
                    ),
                    tower_settings.normalize_recall_judge_model(peek_judge_model()),
                )
                if m
            ),
            tower_settings.DEFAULT_RECALL_JUDGE_MODEL,
        )
        provider = model_registry.provider_for_model(model)
        armed = model_registry.provider_key_present(provider, allow_lookup=False)
    except Exception as e:
        log.warning("Recall arming check failed (%s); treating as disarmed", e)
        model, provider, armed = "?", "?", False
    if armed:
        _DISARM_LOGGED = False
    elif not _DISARM_LOGGED:
        log.error(
            "Recall system DISARMED: judge model %s needs a %s credential and "
            "none is available. No recalls will be scanned or injected until "
            "one is.",
            model,
            provider,
        )
        _DISARM_LOGGED = True
    return armed


_CHANNEL_PREFIX_RE = re.compile(r"^\[[^\]]+\]:\s*")

# Bare tool / markup noise from tool-output scanning — reject without a judge call.
_STAGE2_BARE_TOOLS: frozenset[str] = frozenset(
    {
        "read_file",
        "write_file",
        "run_shell",
        "db_query",
        "db_upsert",
        "db_delete",
        "palace_search",
        "palace_add_drawer",
        "palace_kg_add",
        "palace_kg_query",
        "palace_taxonomy",
        "memory_log",
        "learn_recall",
        "get_recall",
        "google_search",
    }
)


def _strip_channel_prefix(chunk: str) -> str:
    return _CHANNEL_PREFIX_RE.sub("", (chunk or "").strip()).strip()


def _is_stage2_junk(chunk: str) -> bool:
    text = _strip_channel_prefix(chunk)
    if not text:
        return False
    if text in _STAGE2_BARE_TOOLS:
        return True
    low = text.lower()
    if low.startswith(
        ("<!doctype", "<html", "<head", "<body", "<div", "<title", "--bg", "--fg")
    ):
        return True
    if text.startswith("<") and text.endswith(">") and len(text) < 48:
        return True
    if re.match(r"Written \d+ bytes to ", text):
        return True
    return False


_JUDGE_PROVIDER_CACHE: dict[str, object] = {}


def _judge_provider(model: str, passed):
    """Provider that actually serves `model`.

    The judge model is configured independently of the agent's, so the caller's
    provider is not necessarily the right one — a Claude or Ollama agent with a
    judge on another provider would post that model name to the wrong API.
    `passed` is an
    explicit override for tests; production callers leave it None.
    """
    if passed is not None:
        return passed
    from . import model_registry

    want = model_registry.provider_for_model(model)
    cached = _JUDGE_PROVIDER_CACHE.get(want)
    if cached is not None:
        return cached
    try:
        built = model_registry.build_provider(want)
    except Exception as e:
        log.warning("Recall judge provider %s unavailable: %s", want, e)
        return None
    _JUDGE_PROVIDER_CACHE[want] = built
    return built


async def filter_matches_with_judge(
    matches: list[dict],
    *,
    provider=None,
    usage_callback=None,
) -> tuple[list[dict], list[dict]]:
    """Batched LLM entailment judge. Fail-closed: an unreachable provider or
    a structurally unusable judgment rejects the batch rather than injecting
    unverified candidates."""
    if not matches:
        return [], []
    if not _judge_verify_enabled():
        out = []
        for m in matches:
            enriched = dict(m)
            enriched["judge_verified"] = True
            enriched["judge_reason"] = "stage2_disabled"
            out.append(enriched)
        return out, []

    cap = _stage2_max_candidates()
    ordered = sorted(
        matches,
        key=lambda m: _as_float_score(m.get("positive_score")) or 0.0,
        reverse=True,
    )
    rejected: list[dict] = []
    for match in ordered[cap:]:
        enriched = dict(match)
        enriched["judge_verified"] = False
        enriched["judge_reason"] = f"stage2_candidate_cap:{cap}"
        rejected.append(enriched)

    candidates = ordered[:cap]
    live: list[dict] = []
    for match in candidates:
        chunk = match.get("matched_chunk") or ""
        if _is_stage2_junk(_strip_channel_prefix(chunk)):
            enriched = dict(match)
            enriched["judge_verified"] = False
            enriched["judge_reason"] = "stage2_junk"
            rejected.append(enriched)
            log.info(
                "[Stage2 Reject] route=%r reason=stage2_junk chunk=%r",
                match.get("recall_id"),
                chunk[:100],
            )
        else:
            live.append(match)

    if not live:
        return [], rejected

    pre_judge_rejected = list(rejected)

    def _fail_closed(reason: str) -> tuple[list[dict], list[dict]]:
        log.warning("Recall judge %s; rejecting %d candidate(s)", reason, len(live))
        out = list(pre_judge_rejected)
        for m in live:
            enriched = dict(m)
            enriched["judge_verified"] = False
            enriched["judge_reason"] = f"judge_unavailable:{reason}"
            out.append(enriched)
        return [], out

    from .recall_judge import judge_applicability, resolve_judge_model

    model = resolve_judge_model()
    provider = _judge_provider(model, provider)
    if provider is None:
        return _fail_closed("no_provider")

    by_chunk: dict[str, list[dict]] = {}
    for match in live:
        key = (match.get("matched_chunk") or "").strip()
        by_chunk.setdefault(key, []).append(match)

    from .recall_judge import MAX_MISFIRES_PER_CANDIDATE

    verified: list[dict] = []
    judge_rejected: list[dict] = []
    for chunk, group in by_chunk.items():
        # tune_recall negatives land here: show the judge the few stored
        # misfires most similar to this chunk (bounded, so the array can grow
        # to its cap without bloating the judge prompt). Selection uses the
        # same local encoder as Stage-1, so this is cheap.
        judge_candidates = []
        for m in group:
            cand = dict(m)
            try:
                negatives = _top_k_cosine(
                    get_encoder(), chunk, m.get("negative_examples") or [],
                    MAX_MISFIRES_PER_CANDIDATE,
                )
            except Exception:
                negatives = []
            if negatives:
                cand["judge_negatives"] = negatives
            judge_candidates.append(cand)
        judgment = await judge_applicability(
            provider,
            chunk=chunk,
            candidates=judge_candidates,
            model=model,
            usage_callback=usage_callback,
        )
        if judgment is None:
            return _fail_closed("unusable_judgment")
        applicable = set(judgment.get("applicable") or [])
        for match in group:
            rid = match.get("recall_id")
            enriched = dict(match)
            if rid in applicable:
                enriched["judge_verified"] = True
                enriched["judge_reason"] = "judge:applicable"
                verified.append(enriched)
            else:
                enriched["judge_verified"] = False
                enriched["judge_reason"] = "judge:none"
                judge_rejected.append(enriched)
                log.info(
                    "[Stage2 Reject] route=%r reason=judge:none chunk=%r",
                    rid,
                    chunk[:100],
                )
    return verified, pre_judge_rejected + judge_rejected


# ── Cue usage / LRU ─────────────────────────────────────────────────────────
# Cue lists are capped, so something must be dropped when a new cue arrives.
# FIFO drops whichever cue was added first, which meant hand-authored seeds went
# before auto-appended chunks. Instead track when each cue last won a Stage-2
# match and evict the least-recently-used, so cues that never do any work go
# first. Usage lives in its own collection: it is written on every fire and must
# not touch recall definitions (system recalls are a JSON file on disk).

CUE_USAGE_COLLECTION = "recall_cue_usage"

_LRU_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def cue_key(text: str) -> str:
    """Stable, Mongo-safe key for a cue string (no dots or `$` in hex)."""
    normalized = (text or "").strip().casefold()
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


def _as_aware(value):
    """Coerce a stored timestamp to tz-aware UTC (pymongo returns naive)."""
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _cue_saturation_threshold() -> float:
    raw = (os.environ.get("RECALL_CUE_SATURATION") or "").strip()
    if not raw:
        return _CUE_SATURATION_DEFAULT
    try:
        return float(raw)
    except ValueError:
        return _CUE_SATURATION_DEFAULT


def cue_is_saturated(text: str, examples: list[str]) -> tuple[bool, float | None]:
    """(saturated, nearest_cosine) — True when `text` adds no coverage.

    Both stages accept on max() over the cue list, so a cue that sits very close
    to an existing one can only be dead weight or a marginal region extension.
    Fails open (not saturated) if the encoder is unavailable.
    """
    pool = [e for e in (examples or []) if isinstance(e, str) and e.strip()]
    if not (text or "").strip() or not pool:
        return False, None
    try:
        score = _max_cosine(get_encoder(), text, pool)
    except Exception as e:
        log.warning("cue saturation check failed (%s); allowing append", e)
        return False, None
    if score is None:
        return False, None
    return score >= _cue_saturation_threshold(), float(score)


def evict_lru_cues(examples: list[str], usage: dict, cap: int) -> list[str]:
    """Trim `examples` to `cap`, dropping least-recently-used cues first.

    Cues with no usage entry rank oldest, so callers must stamp a cue they are
    inserting (insertion counts as a use) or it becomes its own victim. Both
    write paths do this: `sync_cue_usage` stamps every stored cue, and
    `tune_recall` stamps the appended chunk before calling this.
    """
    if len(examples) <= cap:
        return list(examples)
    ranked = sorted(
        range(len(examples)),
        key=lambda i: (usage.get(cue_key(examples[i])) or _LRU_EPOCH, i),
    )
    dropped = set(ranked[: len(examples) - cap])
    return [e for i, e in enumerate(examples) if i not in dropped]


async def load_cue_usage(recall_id: str) -> dict:
    """{cue_key: last_used (aware UTC)} for one recall; {} when unavailable."""
    db = get_db()
    if db is None or not recall_id:
        return {}
    try:
        doc = await db[CUE_USAGE_COLLECTION].find_one({"recall_id": recall_id})
    except Exception as e:
        log.warning("cue usage read failed for %s: %s", recall_id, e)
        return {}
    stored = (doc or {}).get("usage") or {}
    out = {}
    for key, value in stored.items():
        stamp = _as_aware(value)
        if isinstance(key, str) and stamp is not None:
            out[key] = stamp
    return out


async def sync_cue_usage(recall_id: str, cues, *, touch=()) -> None:
    """Stamp new/touched cues with now; drop entries for cues no longer stored.

    `cues` must be the full tracked set (positives + negatives + lexical) —
    anything missing from it is pruned. Lexical cues belong in that set because
    a lexical hit is what the fire path stamps for a lexical match; leaving
    them out silently deleted those stamps on the next write.
    """
    db = get_db()
    if db is None or not recall_id:
        return
    now = datetime.now(timezone.utc)
    valid = {cue_key(c) for c in cues if isinstance(c, str) and c.strip()}
    if not valid:
        return
    existing = await load_cue_usage(recall_id)
    merged = {key: existing.get(key) or now for key in valid}
    for cue in touch:
        if isinstance(cue, str) and cue.strip():
            key = cue_key(cue)
            if key in merged:
                merged[key] = now
    try:
        await db[CUE_USAGE_COLLECTION].update_one(
            {"recall_id": recall_id},
            {"$set": {"usage": merged, "updated_at": now}},
            upsert=True,
        )
    except Exception as e:
        log.warning("cue usage write failed for %s: %s", recall_id, e)


def winning_cue(match: dict) -> str | None:
    """The stored cue this match actually fired on, for usage stamping.

    Lexical and fuzzy hits already name their cue. A semantic hit does not:
    Stage-1 accepts on max cosine over `positive_examples`, so the argmax over
    that array IS the cue that won, and this is the only place the winner is
    still recoverable. Runs on verified fires only — at most a handful per turn
    — never on the Stage-1 scan path, where it would cost an encode per chunk.

    Without this, nothing stamps cue usage on a real fire, and `evict_lru_cues`
    silently ranks by write time instead of by which cues earn their place.
    """
    if not isinstance(match, dict):
        return None
    cue = (match.get("lexical_cue") or "").strip()
    if cue:
        return cue
    chunk = (match.get("matched_chunk") or "").strip()
    positives = match.get("positive_examples") or []
    if not chunk or not positives:
        return None
    try:
        hit = _argmax_cosine(get_encoder(), chunk, positives)
    except Exception as e:
        log.warning(
            "winning-cue resolution failed for %s: %s", match.get("recall_id"), e
        )
        return None
    return None if hit is None else hit[1]


async def touch_cue_usage(recall_id: str, cues) -> None:
    """Bump last_used for specific cues (fire path — no pruning)."""
    db = get_db()
    if db is None or not recall_id:
        return
    keys = {cue_key(c) for c in cues if isinstance(c, str) and c.strip()}
    if not keys:
        return
    now = datetime.now(timezone.utc)
    updates = {f"usage.{key}": now for key in keys}
    updates["updated_at"] = now
    try:
        await db[CUE_USAGE_COLLECTION].update_one(
            {"recall_id": recall_id}, {"$set": updates}, upsert=True
        )
    except Exception as e:
        log.warning("cue usage touch failed for %s: %s", recall_id, e)

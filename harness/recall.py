import hashlib
import json
import logging
import os
import re
import threading
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
_SLM_CLIENT = None
_SLM_CLIENT_FAILED = False
_SLM_LOADED_KEY = None
_SLM_LOCK = threading.RLock()
_RERANKER = None
_RERANKER_FAILED = False
_RERANKER_LOCK = threading.RLock()

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

# Legacy logit-margin default (experimental RECALL_STAGE2_MODE=logit only).
_SLM_LOGIT_MARGIN_DEFAULT = 2.0
# Embedding pos−neg margin (explicit RECALL_STAGE2_MODE=embed only — never an
# automatic fallback; see recall_system_armed).
_STAGE2_EMBED_MARGIN_DEFAULT = 0.0
# Cross-encoder P(yes) floor, calibrated by eval/calibrate_local_threshold.py
# against GGUF sha256 c04f5f56 (see eval/results/local_threshold_calibration.json).
# The old 0.65 was read off double-sigmoid scores squeezed into [0.50, 0.73] and
# is meaningless on the corrected scale, where true paraphrase pairs land ~0.55.
# This tier cannot reach P>=0.95 at any threshold — 0.487 is its best operating
# point at P=0.864 / R=0.537, which is why `judge` is the default.
_STAGE2_RERANK_THRESHOLD_DEFAULT = 0.487
# Counter-signal slack for the rerank veto: reject when the best negative scores
# above (best positive − delta). 0.0 = veto only when a negative strictly wins.
# Negatives are verbatim misfire chunks, so a recurrence self-matches near 1.0
# and loses to nothing — which is exactly when the veto should bite. Raising
# this trades recall for precision and is NOT covered by the eval set.
_STAGE2_NEG_DELTA_DEFAULT = 0.0
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
# How many cues the cross-encoder sees per candidate, chosen by cheap cosine
# pre-rank. Each pair is a full forward pass (~52ms), so scoring every cue makes
# Stage-2 linear in cue count: 337ms at today's 5-9 cues but 5.7s at the
# 100-cue cap. Measured on the 198-case eval set with leave-one-out (the exact
# chunk dropped from the cue list, since cue_audit chunks are the cues
# themselves): the cosine top-2 plus the instruction reproduces the full sweep's
# decision on 198/198 positive candidates and 57/57 negative vetoes. top-1 flips
# 3 (recall 0.505 -> 0.474); top-3 buys nothing. The instruction is always
# scored — it was the winning document in 66/198 cases.
_STAGE2_PRERANK_K = 2


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

    `negative_threshold` gated Stage-1 until the reranker rewrite and is stripped
    on read, so stored copies age out without a migration.
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


# Text -> unit vector, backing the Stage-2 pre-rank. An embedding is a pure
# function of its text, so an edited cue lands on a new key and the stale entry
# just ages out; nothing needs invalidating. Scanned chunks share the cache with
# cues, which is what makes a multi-candidate scan embed its chunk once. On
# overflow the whole cache is dropped rather than tracking per-entry recency —
# the only cost is re-embedding at ~1.4ms per text.
_TEXT_VEC_CACHE: dict[str, np.ndarray] = {}
_TEXT_VEC_CACHE_MAX = 4096


def _unit(vec) -> np.ndarray | None:
    arr = np.asarray(vec, dtype=np.float64)
    norm = np.linalg.norm(arr)
    return None if norm == 0 else arr / norm


def _text_vectors(encoder, texts: list[str]) -> dict[str, np.ndarray]:
    """Unit vectors for texts, embedding only cache misses in one batched call."""
    out: dict[str, np.ndarray] = {}
    missing: list[str] = []
    for text in texts:
        hit = _TEXT_VEC_CACHE.get(cue_key(text))
        if hit is None:
            if text not in missing:
                missing.append(text)
        else:
            out[text] = hit
    if missing:
        for text, vec in zip(missing, encoder(missing)):
            unit = _unit(vec)
            if unit is None:
                continue
            if len(_TEXT_VEC_CACHE) >= _TEXT_VEC_CACHE_MAX:
                _TEXT_VEC_CACHE.clear()
            _TEXT_VEC_CACHE[cue_key(text)] = unit
            out[text] = unit
    return out


def _prerank_cues(text: str, cues: list[str], k: int) -> list[str]:
    """The k cues closest to text by cosine, best first.

    Bounds Stage-2 to a fixed number of cross-encoder passes however many cues a
    recall has accumulated (see _STAGE2_PRERANK_K). Best-first also lets
    best_match's stop_at short-circuit on the first pass for a true match.
    Any failure returns the full list, so a pre-rank problem costs time rather
    than a missed match.
    """
    if len(cues) <= k:
        return cues
    try:
        encoder = get_encoder()
        vectors = _text_vectors(encoder, cues + [text])
        query = vectors.get(text)
        ranked = sorted(
            (c for c in cues if c in vectors),
            key=lambda c: float(np.dot(query, vectors[c])),
            reverse=True,
        ) if query is not None else []
        return ranked[:k] or cues
    except Exception as e:
        log.warning("Stage-2 cue pre-rank failed (%s); scoring all cues", e)
        return cues


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


def generate_recall_fire_text(matched_recalls: list[dict]) -> str:
    """Injected recall-fire text: `- [recall_id] instruction` per match.

    The id must be in the visible text. The stable block tells the agent to call
    `tune_recall(recall_id, ...)` after a fire, but the id previously lived only
    in the message's `matched_recall_ids` metadata, which the model never sees —
    so it invented plausible ids (`sys_worker_board`, `sys_cookbooks`) and every
    feedback call failed.
    """
    if not matched_recalls:
        return ""

    fire_lines = []
    for recall in matched_recalls:
        rid = (recall.get("recall_id") or "").strip()
        instruction = recall.get("instruction")
        fire_lines.append(f"- [{rid}] {instruction}" if rid else f"- {instruction}")

    return "\n".join(fire_lines)


def _slm_verify_enabled() -> bool:
    raw = (os.environ.get("RECALL_SLM_VERIFY") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _configured_slm_model_key() -> str:
    """Active Stage-2 profile key from tower_settings (falls back to default)."""
    try:
        from . import tower_settings

        return tower_settings.get_recall_slm_model()
    except Exception:
        from local_llm.config import DEFAULT_RECALL_SLM_MODEL

        return DEFAULT_RECALL_SLM_MODEL


def _close_slm_client_unlocked() -> None:
    global _SLM_CLIENT, _SLM_CLIENT_FAILED, _SLM_LOADED_KEY
    client = _SLM_CLIENT
    _SLM_CLIENT = None
    _SLM_LOADED_KEY = None
    _SLM_CLIENT_FAILED = False
    if client is not None:
        try:
            client.close()
        except Exception as e:
            log.warning("Recall SLM close failed: %s", e)


def _build_slm_client(model_key: str):
    """Construct an in-process client for one profile. Raises on hard failures."""
    from local_llm import LocalGemma, LocalLLMClient, model_path_for, resolve_model_profile

    profile = resolve_model_profile(model_key)
    path = model_path_for(model_key)
    if not path.exists():
        return None, path, profile
    engine = LocalGemma(
        model_path=path,
        ensure=False,
        model_id=profile["model_id"],
        hf_repo=profile["hf_repo"],
    )
    client = LocalLLMClient(
        in_process=True,
        engine=engine,
        model=profile["model_id"],
    )
    return client, path, profile


def _get_slm_client():
    """Lazy LocalLLMClient for Stage-2 verify. One model loaded; None if unavailable."""
    global _SLM_CLIENT, _SLM_CLIENT_FAILED, _SLM_LOADED_KEY
    if not _slm_verify_enabled():
        return None
    with _SLM_LOCK:
        if _SLM_CLIENT_FAILED:
            return None
        wanted = _configured_slm_model_key()
        if _SLM_CLIENT is not None and _SLM_LOADED_KEY == wanted:
            return _SLM_CLIENT
        if _SLM_CLIENT is not None and _SLM_LOADED_KEY != wanted:
            log.info(
                "Recall SLM: configured model changed %s → %s; unloading",
                _SLM_LOADED_KEY,
                wanted,
            )
            _close_slm_client_unlocked()
        try:
            client, path, profile = _build_slm_client(wanted)
            if client is None:
                # Do not sticky-fail: image/entrypoint may populate weights later.
                log.warning(
                    "Recall SLM verify: GGUF missing at %s; fail-open until available",
                    path,
                )
                return None
            _SLM_CLIENT = client
            _SLM_LOADED_KEY = wanted
            log.info(
                "Recall SLM loaded model=%s id=%s path=%s",
                wanted,
                profile["model_id"],
                path,
            )
            return _SLM_CLIENT
        except Exception as e:
            log.warning(f"Recall SLM verify: client init failed ({e}); fail-open")
            _SLM_CLIENT_FAILED = True
            return None


def _judge_model_for_status() -> str:
    from .recall_judge import DEFAULT_JUDGE_MODEL, resolve_judge_model

    try:
        return resolve_judge_model()
    except Exception:
        return DEFAULT_JUDGE_MODEL


def get_recall_slm_status() -> dict:
    """Status payload for Tower UI / API."""
    from local_llm import list_model_profiles

    key = _configured_slm_model_key()
    with _SLM_LOCK:
        loaded = _SLM_LOADED_KEY
        loaded_now = _SLM_CLIENT is not None and loaded == key
    mode = _stage2_mode()
    with _RERANKER_LOCK:
        reranker_loaded = _RERANKER is not None
    tier = "judge" if mode == "judge" else "local"
    try:
        from . import tower_settings

        tier = tower_settings.get_recall_stage2_tier()
    except Exception:
        pass
    return {
        "model": key,
        "loaded_model": loaded,
        "loaded": loaded_now,
        "enabled": _slm_verify_enabled(),
        "options": list_model_profiles(),
        "stage2_mode": mode,
        "stage2_tier": tier,
        "stage2_model_active": mode == "logit",
        "judge_model": _judge_model_for_status() if mode == "judge" else None,
        "armed": recall_system_armed(),
        "reranker": {
            "loaded": reranker_loaded,
            "present": _reranker_present(),
            "threshold": _stage2_rerank_threshold(),
        },
    }


def set_recall_slm_model(model_key: str, *, preload: bool = True) -> dict:
    """Switch Stage-2 model. With preload=True, persist only after a successful load.

    Only one GGUF is resident at a time. On preload failure the previous model
    stays configured and is best-effort reloaded (unload-then-load to fit 4GB
    tenants). Returns status including load_ms.
    """
    global _SLM_CLIENT, _SLM_CLIENT_FAILED, _SLM_LOADED_KEY
    from . import tower_settings

    model = tower_settings.normalize_recall_slm_model(model_key)
    if model is None:
        raise ValueError(
            f"Unsupported recall SLM model: {model_key}; "
            f"expected one of {list(tower_settings.RECALL_SLM_MODEL_OPTIONS)}"
        )

    load_ms = None
    error = None
    with _SLM_LOCK:
        # Config-only: persist immediately; unload mismatch so the next verify
        # lazy-loads the new key.
        if not preload or not _slm_verify_enabled():
            saved = tower_settings.set_recall_slm_model(model)
            if _SLM_LOADED_KEY != saved:
                _close_slm_client_unlocked()
            status = get_recall_slm_status()
            status["load_ms"] = load_ms
            return status

        # Already resident — just ensure Mongo matches.
        if _SLM_CLIENT is not None and _SLM_LOADED_KEY == model:
            tower_settings.set_recall_slm_model(model)
            status = get_recall_slm_status()
            status["load_ms"] = 0.0
            return status

        previous_loaded = _SLM_LOADED_KEY
        # Free RAM before loading the replacement (important on 4GB tenants).
        if _SLM_LOADED_KEY != model:
            _close_slm_client_unlocked()

        t0 = time.perf_counter()
        # Clear sticky fail so a previous hard fail can recover after bake/swap.
        _SLM_CLIENT_FAILED = False
        try:
            client, path, profile = _build_slm_client(model)
            if client is None:
                error = f"GGUF missing at {path}"
            else:
                _SLM_CLIENT = client
                _SLM_LOADED_KEY = model
                tower_settings.set_recall_slm_model(model)
                load_ms = round((time.perf_counter() - t0) * 1000.0, 1)
                log.info(
                    "Recall SLM hot-swap model=%s id=%s load_ms=%.1f",
                    model,
                    profile["model_id"],
                    load_ms,
                )
        except Exception as e:
            error = str(e)
            log.warning("Recall SLM hot-swap failed: %s", e)

        if error:
            # Do not persist the failed key. Reload the previous resident model.
            if previous_loaded:
                try:
                    restored, _, _ = _build_slm_client(previous_loaded)
                    if restored is not None:
                        _SLM_CLIENT = restored
                        _SLM_LOADED_KEY = previous_loaded
                        _SLM_CLIENT_FAILED = False
                        log.info(
                            "Recall SLM restored previous model=%s after failed swap",
                            previous_loaded,
                        )
                    else:
                        # Missing weights must not sticky-fail the process.
                        _SLM_CLIENT_FAILED = False
                except Exception as restore_e:
                    log.warning(
                        "Recall SLM restore after failed swap failed: %s", restore_e
                    )
                    _SLM_CLIENT_FAILED = True
            elif "GGUF missing" in error:
                _SLM_CLIENT_FAILED = False
            else:
                _SLM_CLIENT_FAILED = True

    status = get_recall_slm_status()
    status["load_ms"] = load_ms
    if error:
        status["error"] = error
    return status


def _slm_logit_margin() -> float:
    raw = (os.environ.get("RECALL_SLM_MARGIN") or "").strip()
    if not raw:
        return _SLM_LOGIT_MARGIN_DEFAULT
    try:
        return float(raw)
    except ValueError:
        return _SLM_LOGIT_MARGIN_DEFAULT


_STAGE2_TIER_CACHE: tuple[float, str] | None = None
_STAGE2_TIER_TTL_SECONDS = 15.0


def invalidate_stage2_tier_cache() -> None:
    """Drop the cached Tower tier so a UI change takes effect immediately."""
    global _STAGE2_TIER_CACHE
    _STAGE2_TIER_CACHE = None


def _stage2_tier_cached() -> str:
    """Tower-persisted tier, TTL-cached — _stage2_mode runs on the scan hot path."""
    global _STAGE2_TIER_CACHE
    now = time.monotonic()
    if _STAGE2_TIER_CACHE is not None and now - _STAGE2_TIER_CACHE[0] < _STAGE2_TIER_TTL_SECONDS:
        return _STAGE2_TIER_CACHE[1]
    try:
        from . import tower_settings

        tier = tower_settings.get_recall_stage2_tier()
        value = "judge" if tier == "judge" else "rerank"
    except Exception:
        value = "judge"
    _STAGE2_TIER_CACHE = (now, value)
    return value


def _stage2_mode() -> str:
    """Stage-2 backend: judge (default), rerank/local, embed, or experimental logit.

    `judge` = Gemini batched entailment (paid tier).
    `rerank` / `local` = Qwen3 cross-encoder (free tier).
    `embed` / `logit` = test/dev only.
    Tower tier setting maps local→rerank and judge→judge; env wins when set.
    """
    raw = (os.environ.get("RECALL_STAGE2_MODE") or "").strip().lower()
    if not raw:
        raw = _stage2_tier_cached()
    if raw == "local":
        raw = "rerank"
    return raw if raw in ("embed", "logit", "rerank", "judge") else "judge"


def _stage2_embed_margin() -> float:
    raw = (os.environ.get("RECALL_STAGE2_MARGIN") or "").strip()
    if not raw:
        return _STAGE2_EMBED_MARGIN_DEFAULT
    try:
        return float(raw)
    except ValueError:
        return _STAGE2_EMBED_MARGIN_DEFAULT


def _stage2_rerank_threshold() -> float:
    raw = (os.environ.get("RECALL_STAGE2_RERANK_THRESHOLD") or "").strip()
    if not raw:
        return _STAGE2_RERANK_THRESHOLD_DEFAULT
    try:
        return float(raw)
    except ValueError:
        return _STAGE2_RERANK_THRESHOLD_DEFAULT


def _stage2_neg_delta() -> float:
    raw = (os.environ.get("RECALL_STAGE2_NEG_DELTA") or "").strip()
    if not raw:
        return _STAGE2_NEG_DELTA_DEFAULT
    try:
        return float(raw)
    except ValueError:
        return _STAGE2_NEG_DELTA_DEFAULT


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


def _reranker_present() -> bool:
    try:
        from local_llm.config import reranker_path

        path = reranker_path()
        return path.exists() and path.stat().st_size > 1_000_000
    except Exception:
        return False


def _get_reranker():
    """Lazy singleton Qwen3 reranker. None (fail-open) if unavailable."""
    global _RERANKER, _RERANKER_FAILED
    if not _slm_verify_enabled():
        return None
    with _RERANKER_LOCK:
        if _RERANKER_FAILED:
            return None
        if _RERANKER is not None:
            return _RERANKER
        try:
            from local_llm.reranker import Qwen3Reranker

            _RERANKER = Qwen3Reranker()
            log.info("Recall Stage-2 reranker loaded (Qwen3-Reranker-0.6B)")
        except FileNotFoundError as e:
            # Missing weights are recoverable — do not sticky-fail the process.
            log.warning("Recall Stage-2 reranker unavailable: %s", e)
            return None
        except Exception as e:
            log.warning("Recall Stage-2 reranker load failed (%s); recall disarmed", e)
            _RERANKER_FAILED = True
            return None
        return _RERANKER


def close_reranker() -> None:
    global _RERANKER, _RERANKER_FAILED
    with _RERANKER_LOCK:
        rr = _RERANKER
        _RERANKER = None
        _RERANKER_FAILED = False
    if rr is not None:
        try:
            rr.close()
        except Exception as e:
            log.warning("Recall Stage-2 reranker close failed: %s", e)


_DISARM_LOGGED = False


def recall_system_armed() -> bool:
    """Master switch: a recall system without its verifier must not run at all.

    - rerank/local: fail-closed until the GGUF loads (no silent embed fallback).
    - judge: armed when a Gemini key is present, OR when the local reranker is
      loaded as a degraded fallback. Otherwise disarm.
    - embed/logit and RECALL_SLM_VERIFY=0: deliberate operator choices; stay armed.
    """
    global _DISARM_LOGGED
    if not _slm_verify_enabled():
        return True
    mode = _stage2_mode()
    if mode in ("embed", "logit"):
        return True
    if mode == "judge":
        # Short-circuit: never pay the GGUF load (~400 MB, seconds of CPU) just to
        # confirm a fallback we will not use while the judge is reachable.
        armed = bool((os.environ.get("GEMINI_API_KEY") or "").strip()) or (
            _get_reranker() is not None
        )
        if armed:
            _DISARM_LOGGED = False
        elif not _DISARM_LOGGED:
            log.error(
                "Recall system DISARMED: judge mode configured but GEMINI_API_KEY "
                "is missing and the local reranker is unavailable."
            )
            _DISARM_LOGGED = True
        return armed
    # rerank / local
    armed = _get_reranker() is not None
    if armed:
        _DISARM_LOGGED = False
    elif not _DISARM_LOGGED:
        log.error(
            "Recall system DISARMED: rerank mode configured but the reranker "
            "is unavailable. No recalls will be scanned or injected until the "
            "GGUF loads (python -m local_llm download --reranker)."
        )
        _DISARM_LOGGED = True
    return armed


_CHANNEL_PREFIX_RE = re.compile(r"^\[[^\]]+\]:\s*")

# Bare tool / markup noise from tool-output scanning — reject without generative SLM.
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


# Kept for experimental RECALL_STAGE2_MODE=logit only. Tiny ITs latch onto the
# favored completion token and do not judge the question (hola/bola repro).
_SLM_GLOBAL_HARD_NEGATIVES: tuple[str, ...] = (
    "<!doctype html>",
    "<head>",
    "--bg: #ffffff;",
    "read_file",
    "write_file",
    "Written 7 bytes to state/worker_control.md",
    "hello how are you today",
)


def _build_slm_verify_prompt(chunk: str, recall: dict) -> str:
    """Few-shot YES/NO steering-value prompt (logit scoring path).

    The question is framed as steering value, not surface match: would
    injecting this rule's instruction lead the agent to a better response —
    one more likely what the user (or the agent's own current task) wants?
    """
    instruction = (recall.get("instruction") or "").strip()
    lines = [
        "An agent is mid-conversation. TEXT is what the agent just saw. RULE is a",
        "learned instruction that may be injected as a hint. Answer YES or NO:",
        "would injecting RULE now lead the agent to a better response — one more",
        "likely what the user or the agent's own current task wants?",
        "YES only if TEXT is genuinely about RULE's situation.",
        "NO for HTML/CSS markup, bare tool names, file-write acks, tiny fragments,",
        "or chatter where RULE would only distract.",
        f"RULE: {instruction[:160]}",
    ]
    for ex in _SLM_GLOBAL_HARD_NEGATIVES:
        lines.append(f"TEXT: {ex}\nAnswer: NO")
    for ex in (recall.get("positive_examples") or [])[:3]:
        if isinstance(ex, str) and ex.strip():
            lines.append(f"TEXT: {ex.strip()[:160]}\nAnswer: YES")
    for ex in (recall.get("negative_examples") or [])[:3]:
        if isinstance(ex, str) and ex.strip():
            if ex.strip() in _SLM_GLOBAL_HARD_NEGATIVES:
                continue
            lines.append(f"TEXT: {ex.strip()[:160]}\nAnswer: NO")
    lines.append(f"TEXT: {chunk.strip()[:400]}\nAnswer:")
    return "\n".join(lines)


def verify_recall_candidate_embed(
    chunk: str,
    recall: dict,
    *,
    margin: float | None = None,
    encoder=None,
    out: dict | None = None,
) -> tuple[bool, str]:
    """Stage-2 via embedding pos−neg margin (explicit RECALL_STAGE2_MODE=embed).

    Test/dev backend only — no GGUF dependency, but it blocked 0/7 production
    FP incidents, so it is never used as an automatic fallback for rerank.
    Re-scores with the same FastEmbed encoder used at Stage-1: accept when
    max(pos) − max(neg) > margin.
    """
    if not _slm_verify_enabled():
        return True, "stage2_disabled"

    instruction = (recall.get("instruction") or "").strip()
    text = _strip_channel_prefix(chunk)
    if not text or not instruction:
        return True, "stage2_skip_empty"

    if _is_stage2_junk(text):
        return False, "stage2_junk"

    positives = recall.get("positive_examples") or []
    negatives = recall.get("negative_examples") or []
    if not positives:
        return True, "stage2_no_positives"

    try:
        enc = encoder if encoder is not None else get_encoder()
        pos_hit = _argmax_cosine(enc, text, positives)
        pos_score = None if pos_hit is None else pos_hit[0]
        neg_score = _max_cosine(enc, text, negatives) if negatives else None
    except Exception as e:
        log.warning(
            "Recall Stage-2 embed error for %s (%s); fail-open",
            recall.get("recall_id"),
            e,
        )
        return True, f"stage2_error:{type(e).__name__}"

    if pos_score is None:
        return True, "stage2_pos_unscored"

    threshold = _stage2_embed_margin() if margin is None else float(margin)
    if neg_score is None:
        ok = pos_score > threshold  # no negatives → require some positive mass
        reason = (
            f"embed_pos:{pos_score:.3f}>{threshold:.3f}"
            if ok
            else f"embed_pos:{pos_score:.3f}<={threshold:.3f}"
        )
        if ok and out is not None:
            out["matched_example"] = pos_hit[1]
        return ok, reason

    diff = float(pos_score) - float(neg_score)
    ok = diff > threshold
    reason = (
        f"embed_margin:{diff:+.3f}>{threshold:.3f}"
        f"(pos={pos_score:.3f},neg={neg_score:.3f})"
        if ok
        else f"embed_margin:{diff:+.3f}<={threshold:.3f}"
        f"(pos={pos_score:.3f},neg={neg_score:.3f})"
    )
    if out is not None:
        out["stage2_negative_score"] = round(float(neg_score), 4)
        if ok:
            out["matched_example"] = pos_hit[1]
    return ok, reason


def verify_recall_candidate_rerank(
    chunk: str,
    recall: dict,
    *,
    threshold: float | None = None,
    reranker=None,
    out: dict | None = None,
) -> tuple[bool, str]:
    """Stage-2 via Qwen3 cross-encoder (local / free tier).

    Scores the chunk against the recall's activation_condition (preferred) plus
    a small preranked positive-cue set, and accepts when the best P(yes) clears
    both an absolute floor and a margin over frozen off-topic anchors. Negatives
    still veto after positives clear. Fail-closed when the reranker is missing.
    """
    if not _slm_verify_enabled():
        return True, "stage2_disabled"

    instruction = (recall.get("instruction") or "").strip()
    text = _strip_channel_prefix(chunk)
    if not text or not instruction:
        return True, "stage2_skip_empty"

    if _is_stage2_junk(text):
        return False, "stage2_junk"

    rr = reranker if reranker is not None else _get_reranker()
    if rr is None:
        return False, "stage2_disarmed:reranker_unavailable"

    positives = [
        e for e in (recall.get("positive_examples") or [])
        if isinstance(e, str) and e.strip()
    ]
    ranked = _prerank_cues(text, positives, _STAGE2_PRERANK_K)
    truncated = len(ranked) < len(positives)
    # Qwen3-Reranker is a query->document *relevance* model. Concrete utterances
    # score in the usable band; abstract meta-text ("the user is asking the agent
    # to...") scores ~0.01 even on a true match, so activation_condition is the
    # judge tier's input and never a document here.
    docs = list(ranked) + [instruction]
    negatives = [
        e for e in (recall.get("negative_examples") or [])
        if isinstance(e, str) and e.strip()
    ]

    thr = _stage2_rerank_threshold() if threshold is None else float(threshold)
    delta = _stage2_neg_delta()
    try:
        hit = rr.best_match(text, docs, stop_at=thr)
        if hit is None:
            return True, "stage2_rerank_unscored"
        score, matched = hit
        neg_hit = (
            rr.best_match(
                text,
                _prerank_cues(text, negatives, _STAGE2_PRERANK_K),
                stop_at=score - delta,
            )
            if negatives and score > thr
            else None
        )
        if truncated and neg_hit is not None and neg_hit[0] > score - delta:
            exact = rr.best_match(text, positives, stop_at=neg_hit[0] + delta)
            if exact is not None and exact[0] > score:
                score, matched = exact
    except Exception as e:
        log.warning(
            "Recall Stage-2 rerank error for %s (%s); fail-closed",
            recall.get("recall_id"),
            e,
        )
        return False, f"stage2_rerank_error:{type(e).__name__}"

    if score <= thr:
        return False, f"rerank:{score:.3f}<={thr:.3f}"

    if neg_hit is not None and neg_hit[0] > score - delta:
        if out is not None:
            out["stage2_negative_score"] = round(neg_hit[0], 4)
        return False, (
            f"rerank_neg_veto:neg={neg_hit[0]:.3f}>pos={score:.3f}-{delta:.3f}"
        )

    if out is not None:
        out["matched_example"] = matched
        if neg_hit is not None:
            out["stage2_negative_score"] = round(neg_hit[0], 4)
    return True, f"rerank:{score:.3f}>{thr:.3f}"


def verify_recall_candidate_slm(
    chunk: str,
    recall: dict,
    *,
    client=None,
    margin: float | None = None,
    out: dict | None = None,
) -> tuple[bool, str]:
    """Stage-2 intent filter. Returns (ok, reason).

    Default (`RECALL_STAGE2_MODE=judge`): use `filter_matches_with_judge` for the
    batched path; this per-candidate entry falls through to local rerank so
    sync callers (tower test, embed scripts) keep working when judge is unset.
    `rerank` / `local`: Qwen3 cross-encoder.
    `embed` / `logit`: test/dev only.
    """
    mode = _stage2_mode()
    if mode in ("rerank", "judge"):
        # Judge mode still uses local rerank here as the sync/fallback path;
        # the async batched judge is filter_matches_with_judge.
        return verify_recall_candidate_rerank(chunk, recall, out=out)
    if mode != "logit":
        return verify_recall_candidate_embed(chunk, recall, margin=margin, out=out)

    if not _slm_verify_enabled():
        return True, "slm_disabled"

    instruction = (recall.get("instruction") or "").strip()
    text = _strip_channel_prefix(chunk)
    if not text or not instruction:
        return True, "slm_skip_empty"

    if _is_stage2_junk(text):
        return False, "stage2_junk"

    llm = client if client is not None else _get_slm_client()
    if llm is None:
        return True, "slm_unavailable"

    prompt = _build_slm_verify_prompt(text, recall)
    threshold = _slm_logit_margin() if margin is None else float(margin)
    try:
        diff = float(llm.yes_no_logit_margin(prompt))
        ok = diff > threshold
        reason = (
            f"slm_logit:{diff:+.2f}>{threshold:.2f}"
            if ok
            else f"slm_logit:{diff:+.2f}<={threshold:.2f}"
        )
        return ok, reason
    except Exception as e:
        log.warning(
            "Recall SLM verify error for %s (%s); fail-open",
            recall.get("recall_id"),
            e,
        )
        return True, f"slm_error:{type(e).__name__}"


def filter_matches_with_slm(matches: list[dict], *, client=None) -> tuple[list[dict], list[dict]]:
    """Verify Stage-1 matches synchronously (local/embed/logit). Returns (verified, rejected).

    Cap at _STAGE2_MAX_CANDIDATES. For the paid judge tier prefer
    `filter_matches_with_judge` (async, one batched call); this path remains the
    local fallback and the sync API used by Tower test + eval.
    """
    verified: list[dict] = []
    rejected: list[dict] = []
    cap = _stage2_max_candidates()
    ordered = sorted(
        matches,
        key=lambda m: _as_float_score(m.get("positive_score")) or 0.0,
        reverse=True,
    )
    for match in ordered[cap:]:
        enriched = dict(match)
        enriched["slm_verified"] = False
        enriched["slm_reason"] = f"stage2_candidate_cap:{cap}"
        rejected.append(enriched)
        log.info(
            "[Stage2 Skip] route=%r reason=candidate_cap(%d) pos=%s chunk=%r",
            match.get("recall_id"), cap, match.get("positive_score"),
            (match.get("matched_chunk") or "")[:100],
        )
    for match in ordered[:cap]:
        chunk = match.get("matched_chunk") or ""
        enriched = dict(match)
        ok, reason = verify_recall_candidate_slm(
            chunk, match, client=client, out=enriched
        )
        enriched["slm_verified"] = bool(ok)
        enriched["slm_reason"] = reason
        if ok:
            verified.append(enriched)
        else:
            rejected.append(enriched)
            log.info(
                "[Stage2 Reject] route=%r reason=%s chunk=%r",
                match.get("recall_id"),
                reason,
                (chunk or "")[:100],
            )
    return verified, rejected


_JUDGE_PROVIDER_CACHE: dict[str, object] = {}


def _judge_provider(model: str, passed):
    """Provider that actually serves `model`.

    The judge model is configured independently of the agent's, so the caller's
    provider is not necessarily the right one — a Claude or Ollama agent with a
    Gemini judge would post a Gemini model name to the wrong API. `passed` is an
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
    """Batched Gemini entailment judge. Falls back to local rerank on failure."""
    if not matches:
        return [], []
    if not _slm_verify_enabled():
        out = []
        for m in matches:
            enriched = dict(m)
            enriched["slm_verified"] = True
            enriched["slm_reason"] = "stage2_disabled"
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
        enriched["slm_verified"] = False
        enriched["slm_reason"] = f"stage2_candidate_cap:{cap}"
        rejected.append(enriched)

    candidates = ordered[:cap]
    live: list[dict] = []
    for match in candidates:
        chunk = match.get("matched_chunk") or ""
        if _is_stage2_junk(_strip_channel_prefix(chunk)):
            enriched = dict(match)
            enriched["slm_verified"] = False
            enriched["slm_reason"] = "stage2_junk"
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

    def _local_fallback() -> tuple[list[dict], list[dict]]:
        log.warning(
            "Recall judge unavailable/malformed; falling back to local rerank"
        )
        local_v, local_r = filter_matches_with_slm(live)
        verified_fb = []
        for m in local_v:
            enriched = dict(m)
            enriched["slm_reason"] = f"judge_fallback:{m.get('slm_reason')}"
            verified_fb.append(enriched)
        rejected_fb = list(pre_judge_rejected)
        for m in local_r:
            enriched = dict(m)
            enriched["slm_reason"] = f"judge_fallback:{m.get('slm_reason')}"
            rejected_fb.append(enriched)
        return verified_fb, rejected_fb

    from .recall_judge import judge_applicability, resolve_judge_model

    model = resolve_judge_model()
    provider = _judge_provider(model, provider)
    if provider is None:
        return _local_fallback()

    by_chunk: dict[str, list[dict]] = {}
    for match in live:
        key = (match.get("matched_chunk") or "").strip()
        by_chunk.setdefault(key, []).append(match)

    verified: list[dict] = []
    judge_rejected: list[dict] = []
    for chunk, group in by_chunk.items():
        judgment = await judge_applicability(
            provider,
            chunk=chunk,
            candidates=group,
            model=model,
            usage_callback=usage_callback,
        )
        if judgment is None:
            return _local_fallback()
        applicable = set(judgment.get("applicable") or [])
        reasons = judgment.get("reasons") or {}
        for match in group:
            rid = match.get("recall_id")
            enriched = dict(match)
            if rid in applicable:
                enriched["slm_verified"] = True
                enriched["slm_reason"] = f"judge:{reasons.get(rid) or 'applicable'}"
                verified.append(enriched)
            else:
                enriched["slm_verified"] = False
                enriched["slm_reason"] = "judge:none"
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

    `cues` must be the full tracked set (positives + negatives) — anything
    missing from it is pruned.
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

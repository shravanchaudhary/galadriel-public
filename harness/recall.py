import json
import logging
import os
import re
from pathlib import Path

import numpy as np
from semantic_router import Route
from semantic_router.routers import SemanticRouter

from .db_ops import get_db

log = logging.getLogger("galadriel.recall")

RECALLS_COLLECTION = "recalls"

_ENCODER = None
_ENCODER_TYPE = None
_ROUTER_CACHE = None
_CACHED_RECALL_IDS = set()

# Noise floor: candidate must be at least this similar to the positive route.
# Pos/neg compare then discriminates; this is not a tuning dial.
_POSITIVE_SCORE_FLOOR = 0.7
# Lexical hard-hit score (exact cue match); still subject to neg veto.
_LEXICAL_POSITIVE_SCORE = 1.0


def get_encoder(force_type=None):
    """Lazily load and cache the encoder model based on environment config."""
    global _ENCODER, _ENCODER_TYPE, _ROUTER_CACHE, _CACHED_RECALL_IDS

    encoder_type = (force_type or os.environ.get("RECALL_ENCODER", "fastembed")).lower()

    if _ENCODER is not None and _ENCODER_TYPE == encoder_type:
        if getattr(_ENCODER, "score_threshold", None) != _POSITIVE_SCORE_FLOOR:
            _ENCODER.score_threshold = _POSITIVE_SCORE_FLOOR
            if _ROUTER_CACHE is not None:
                _ROUTER_CACHE.score_threshold = _POSITIVE_SCORE_FLOOR
        return _ENCODER

    # If encoder type changed, clear the router cache
    if _ENCODER is not None:
        _ROUTER_CACHE = None
        _CACHED_RECALL_IDS = set()

    if encoder_type == "gemini":
        try:
            from semantic_router.encoders import GoogleEncoder
            _ENCODER = GoogleEncoder(name="models/text-embedding-004")
            _ENCODER.score_threshold = _POSITIVE_SCORE_FLOOR
            _ENCODER_TYPE = "gemini"
            log.info("Initialized Gemini encoder for semantic router")
        except Exception as e:
            log.warning(f"Failed to load GoogleEncoder, falling back to fastembed: {e}")
            encoder_type = "fastembed"

    if encoder_type == "fastembed":
        from semantic_router.encoders import FastEmbedEncoder
        _ENCODER = FastEmbedEncoder(name="BAAI/bge-small-en-v1.5")
        _ENCODER.score_threshold = _POSITIVE_SCORE_FLOOR
        _ENCODER_TYPE = "fastembed"
        log.info("Initialized FastEmbedEncoder for semantic router")

    return _ENCODER


def get_semantic_router(recalls: list[dict], force_encoder_type=None, force_reload=False) -> SemanticRouter:
    """Get or build the SemanticRouter for the current set of recalls."""
    global _ROUTER_CACHE, _CACHED_RECALL_IDS

    current_ids = {r.get("recall_id") for r in recalls if r.get("recall_id")}

    encoder = get_encoder(force_encoder_type)

    if _ROUTER_CACHE is not None and current_ids == _CACHED_RECALL_IDS and not force_reload:
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
                recall.pop("threshold", None)
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
                doc.pop("threshold", None)
                recalls.append(doc)
        except Exception as e:
            log.error(f"Failed to fetch user recalls from DB: {e}")
    return recalls


def sanitize_text_with_exclude_texts(text: str, exclude_texts: list[str] | None = None) -> str:
    """Strip exact prior nudge texts and their lines from text before semantic scanning."""
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


def _split_line_for_embedding(
    line: str,
    max_tokens: int = _RECALL_CHUNK_TOKENS,
    overlap_tokens: int = _RECALL_CHUNK_OVERLAP,
) -> list[str]:
    """Split one newline chunk into embedding-sized windows (word approx, with overlap)."""
    if not line:
        return []
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


def _as_float_score(score) -> float | None:
    if score is None or score == "N/A":
        return None
    try:
        return float(score.item()) if hasattr(score, "item") else float(score)
    except (TypeError, ValueError):
        return None


def _max_cosine(encoder, text: str, examples: list[str]) -> float | None:
    """Return max cosine similarity between text and examples, or None if no examples."""
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
        best = None
        for emb in vectors[1:]:
            vec = np.asarray(emb, dtype=np.float64)
            v_norm = np.linalg.norm(vec)
            if v_norm == 0:
                continue
            sim = float(np.dot(query, vec) / (q_norm * v_norm))
            if best is None or sim > best:
                best = sim
        return best
    except Exception as e:
        log.warning(f"Failed to score negative examples: {e}")
        return None


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


def _accept_candidate(
    *,
    recall: dict,
    recall_id: str,
    chunk: str,
    positive_score: float,
    match_source: str,
    encoder,
    seen: set,
    matches: list,
    lexical_cue: str | None = None,
) -> None:
    negatives = recall.get("negative_examples") or []
    negative_score = _max_cosine(encoder, chunk, negatives) if negatives else None

    if negative_score is not None and negative_score >= positive_score:
        log.info(
            f"[Semantic Veto] route='{recall_id}' source={match_source} "
            f"pos={positive_score:.4f} neg={negative_score:.4f} chunk='{chunk[:100]}'"
        )
        return

    cue_note = f" cue={lexical_cue!r}" if lexical_cue else ""
    log.info(
        f"[Semantic Match] route='{recall_id}' source={match_source} "
        f"pos={positive_score:.4f} "
        f"neg={negative_score if negative_score is not None else 'n/a'}"
        f"{cue_note} chunk='{chunk[:100]}'"
    )
    seen.add(recall_id)
    match_obj = dict(recall)
    match_obj.pop("threshold", None)
    match_obj["positive_score"] = positive_score
    match_obj["negative_score"] = negative_score
    match_obj["match_source"] = match_source
    if lexical_cue:
        match_obj["lexical_cue"] = lexical_cue
    matches.append(match_obj)


def scan_text_for_recalls(
    text: str,
    recalls: list[dict],
    exclude_texts: list[str] | None = None,
    force_encoder_type=None,
) -> list[dict]:
    """Scan text: semantic (pos>=floor) OR lexical cue hit, then shared neg veto."""
    if not text or not recalls:
        return []

    sanitized_text = sanitize_text_with_exclude_texts(text, exclude_texts)
    if not sanitized_text:
        return []

    router = get_semantic_router(recalls, force_encoder_type)
    encoder = get_encoder(force_encoder_type)
    recall_map = {r.get("recall_id"): r for r in recalls if r.get("recall_id")}
    if not recall_map:
        return []

    matches = []
    seen = set()

    line_chunks = [c.strip() for c in sanitized_text.split("\n") if c.strip()]
    chunks = [
        sub
        for line in line_chunks
        for sub in _split_line_for_embedding(line)
    ]

    for chunk in chunks:
        if router is not None:
            decisions = router(chunk, limit=5)
            if decisions and not isinstance(decisions, list):
                decisions = [decisions]
            for decision in decisions or []:
                if not decision or not decision.name or decision.name == "None":
                    continue
                if decision.name in seen or decision.name not in recall_map:
                    continue

                positive_score = _as_float_score(getattr(decision, "similarity_score", None))
                if positive_score is None:
                    log.info(
                        f"[Semantic Match] route='{decision.name}' missing positive_score; "
                        f"treating as miss chunk='{chunk[:100]}'"
                    )
                    continue
                if positive_score < _POSITIVE_SCORE_FLOOR:
                    log.info(
                        f"[Semantic Miss] route='{decision.name}' pos={positive_score:.4f} "
                        f"< floor={_POSITIVE_SCORE_FLOOR} chunk='{chunk[:100]}'"
                    )
                    continue

                _accept_candidate(
                    recall=recall_map[decision.name],
                    recall_id=decision.name,
                    chunk=chunk,
                    positive_score=positive_score,
                    match_source="semantic",
                    encoder=encoder,
                    seen=seen,
                    matches=matches,
                )

        for recall_id, recall in recall_map.items():
            if recall_id in seen:
                continue
            hit_cue = _lexical_hit(chunk, recall.get("lexical_cues") or [])
            if not hit_cue:
                continue
            _accept_candidate(
                recall=recall,
                recall_id=recall_id,
                chunk=chunk,
                positive_score=_LEXICAL_POSITIVE_SCORE,
                match_source="lexical",
                encoder=encoder,
                seen=seen,
                matches=matches,
                lexical_cue=hit_cue,
            )

    if matches:
        log.debug(f"Semantic scan matched {len(matches)} rule(s) for text: {text[:200]}...")

    return matches


def generate_nudge(matched_recalls: list[dict]) -> str:
    """Generate the nudge text from matched recalls."""
    if not matched_recalls:
        return ""

    nudge_lines = []
    for recall in matched_recalls:
        nudge_lines.append(f"- {recall.get('instruction')}")

    return "\n".join(nudge_lines)

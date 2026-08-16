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

# Default per-recall Stage-1 positive floor. Negative veto is relative (neg >= pos):
# near-miss negatives share the same cosine band as true positives, so an absolute
# neg floor (e.g. 0.6) systematically over-vetoes when both scores clear ~0.6.
DEFAULT_POSITIVE_THRESHOLD = 0.6
# Stored on recalls for API/UI compat; not used for Stage-1 veto gating.
DEFAULT_NEGATIVE_THRESHOLD = 0.6
# Router encoder floor is open so per-recall positive_threshold can go below 0.6.
_ROUTER_SCORE_FLOOR = 0.0
# Lexical hard-hit score (exact cue match).
_LEXICAL_POSITIVE_SCORE = 1.0
# Chunks shorter than this many words never reach the embedding router —
# lexical cues only. Bare tool names / tiny args carry no semantic intent.
_MIN_SEMANTIC_SCAN_WORDS = 4
# Structured tool output is not prose: JSON fragments ('"key": value', '{...')
# and ls -l rows embed close to everything and were the dominant mid-turn
# false-positive source (2026-08-16: 11 Stage-1 hits on trade JSON / directory
# listings, 11 Stage-2 rejects at ~0.50, ~72s of blocked turn). These skip the
# semantic router; lexical cues still run — same contract as the min-words gate.
_STRUCTURED_CHUNK_RE = re.compile(
    r"^(?:"
    r"[\{\}\]\"']"                        # JSON/dict fragment starts
    r"|\[[\{\[\"\d]"                      # array-of-structure; NOT [Tower]: prefixes
    r"|[-bcdlps][rwxsStT-]{9}[.+@]?\s"    # ls -l permission column
    r")"
)

# Legacy logit-margin default (experimental RECALL_STAGE2_MODE=logit only).
_SLM_LOGIT_MARGIN_DEFAULT = 2.0
# Embedding pos−neg margin (explicit RECALL_STAGE2_MODE=embed only — never an
# automatic fallback; see recall_system_armed).
_STAGE2_EMBED_MARGIN_DEFAULT = 0.0
# Cross-encoder P(yes) floor. Benchmarked score mass is strongly bimodal —
# negatives cluster at ~0.50 (p90 0.58), positives at ~0.731 — so 0.65 sits in
# the empty gap between the modes rather than on a sharp operating point.
_STAGE2_RERANK_THRESHOLD_DEFAULT = 0.65
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


def recall_negative_threshold(recall: dict | None) -> float:
    if not isinstance(recall, dict):
        return DEFAULT_NEGATIVE_THRESHOLD
    return _clamp_threshold(
        recall.get("negative_threshold"), DEFAULT_NEGATIVE_THRESHOLD
    )


def normalize_recall_thresholds(recall: dict) -> dict:
    """Ensure positive/negative thresholds are present with defaults; drop legacy."""
    if not isinstance(recall, dict):
        return recall
    recall.pop("threshold", None)
    recall["positive_threshold"] = recall_positive_threshold(recall)
    recall["negative_threshold"] = recall_negative_threshold(recall)
    return recall


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


def _accept_candidate(
    *,
    recall: dict,
    recall_id: str,
    chunk: str,
    positive_score: float,
    match_source: str,
    seen: set,
    matches: list,
    lexical_cue: str | None = None,
) -> None:
    """Accept semantic if pos >= pos_thr; lexical is a hard hit.

    Stage-1 is positive-only: negative examples never gate here. Near-miss
    negatives share the positive cosine band, so any Stage-1 neg veto
    (absolute or relative) randomly blocks true hits while letting real false
    positives through. Negatives remain stored on the recall as Stage-2 /
    tune_recall counter-signal only.
    """
    pos_thr = recall_positive_threshold(recall)

    if match_source == "semantic" and positive_score < pos_thr:
        log.info(
            f"[Semantic Miss] route='{recall_id}' source={match_source} "
            f"pos={positive_score:.4f} < pos_thr={pos_thr:.4f} chunk='{chunk[:100]}'"
        )
        return

    cue_note = f" cue={lexical_cue!r}" if lexical_cue else ""
    log.info(
        f"[Semantic Match] route='{recall_id}' source={match_source} "
        f"pos={positive_score:.4f}>={pos_thr:.4f}"
        f"{cue_note} chunk='{chunk[:100]}'"
    )
    seen.add(recall_id)
    match_obj = dict(recall)
    normalize_recall_thresholds(match_obj)
    match_obj["positive_score"] = positive_score
    match_obj["negative_score"] = None
    match_obj["match_source"] = match_source
    match_obj["matched_chunk"] = chunk
    if lexical_cue:
        match_obj["lexical_cue"] = lexical_cue
    matches.append(match_obj)


def scan_text_for_recalls(
    text: str,
    recalls: list[dict],
    exclude_texts: list[str] | None = None,
    force_encoder_type=None,
) -> list[dict]:
    """Scan text: semantic OR lexical cue hit, gated by the positive floor.

    Chunks under _MIN_SEMANTIC_SCAN_WORDS words (bare tool names, tiny args
    like "active", write-acks) skip the embedding router entirely — they carry
    no intent signal and were the main mid-turn false-positive source. Lexical
    cues still run on them: exact cues are intentional hard triggers.
    """
    if not text or not recalls:
        return []

    if not recall_system_armed():
        return []

    sanitized_text = sanitize_text_with_exclude_texts(text, exclude_texts)
    if not sanitized_text:
        return []

    router = get_semantic_router(recalls, force_encoder_type)
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
        if (
            router is not None
            and len(chunk.split()) >= _MIN_SEMANTIC_SCAN_WORDS
            and not _STRUCTURED_CHUNK_RE.match(chunk)
        ):
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

                _accept_candidate(
                    recall=recall_map[decision.name],
                    recall_id=decision.name,
                    chunk=chunk,
                    positive_score=positive_score,
                    match_source="semantic",
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
                seen=seen,
                matches=matches,
                lexical_cue=hit_cue,
            )

    if matches:
        log.debug(f"Semantic scan matched {len(matches)} rule(s) for text: {text[:200]}...")

    return matches


def generate_recall_fire_text(matched_recalls: list[dict]) -> str:
    """Generate the injected recall-fire suggestion text from matched recalls."""
    if not matched_recalls:
        return ""

    fire_lines = []
    for recall in matched_recalls:
        fire_lines.append(f"- {recall.get('instruction')}")

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
    return {
        "model": key,
        "loaded_model": loaded,
        "loaded": loaded_now,
        "enabled": _slm_verify_enabled(),
        "options": list_model_profiles(),
        # The generative model above only drives Stage-2 in logit mode; surface
        # the active backend so the UI does not imply the selector is in play.
        "stage2_mode": mode,
        "stage2_model_active": mode == "logit",
        # Fail-closed master switch: in rerank mode the whole recall pipeline
        # is off until the reranker loads (no embed fallback).
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


def _stage2_mode() -> str:
    """Stage-2 backend: rerank (default), embed, or experimental logit.

    Benchmarked on eval/ (198 labeled cases, composed after Stage-1):
      rerank  P=0.912 R=0.979 F1=0.944 @0.65, 6/7 production FP incidents
              blocked (106 MB RSS; swept optimum F1=0.968 @0.718)
      embed   P=0.833 R=1.000 F1=0.909, 0/7
      logit   best generative was qwen2.5-3b P=0.847 R=0.874 F1=0.860, 6/7
              (3.9 GB peak RSS, 2.4 s mean); every smaller IT scored worse
    """
    raw = (os.environ.get("RECALL_STAGE2_MODE") or "rerank").strip().lower()
    return raw if raw in ("embed", "logit", "rerank") else "rerank"


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

    In the default rerank mode the whole pipeline disarms — Stage-1 included —
    when the reranker cannot load (missing/broken GGUF, dead rank head). There
    is no silent embed fallback: the embed margin blocked 0/7 production FP
    incidents, so a degraded recall system is worse than none. Explicit
    non-default configs (RECALL_SLM_VERIFY=0, RECALL_STAGE2_MODE=embed/logit)
    are deliberate operator choices and stay armed.
    """
    global _DISARM_LOGGED
    if not _slm_verify_enabled() or _stage2_mode() != "rerank":
        return True
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
    """Stage-2 via Qwen3 cross-encoder (default path).

    Scores the chunk against the recall's most similar positive examples plus
    its instruction and accepts on the best P(yes). Unlike the embedding margin
    this reads the pair jointly, so near-miss negatives that share a cosine
    band with true positives get separated. Fail-closed: no reranker or a
    scoring error rejects the candidate — never a silent embed fallback
    (see recall_system_armed).

    Both sweeps are pre-ranked by cosine to a fixed cue budget, which is what
    keeps cost flat as tune_recall grows a cue list toward its cap.

    Negatives are scored only after the positives clear, so a recall with no
    recorded misfires costs nothing extra and a rejected candidate is never
    charged for the counter-signal pass.
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
        # scan_text_for_recalls is already disarmed in this state; this guards
        # direct callers (tower test endpoints, scripts).
        return False, "stage2_disarmed:reranker_unavailable"

    positives = [
        e for e in (recall.get("positive_examples") or [])
        if isinstance(e, str) and e.strip()
    ]
    ranked = _prerank_cues(text, positives, _STAGE2_PRERANK_K)
    truncated = len(ranked) < len(positives)
    docs = list(ranked)
    docs.append(instruction)
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
            # The threshold test is absolute, but the veto compares two maxima —
            # so a pre-ranked positive score, which is only a lower bound, can
            # manufacture a veto that the full sweep would not produce (observed
            # on the eval set: true max 0.718 vs pre-ranked 0.669 against a
            # 0.702 negative). Escalate to the exact positive max before
            # rejecting; stop_at exits as soon as the negative cannot win.
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

    Default (`RECALL_STAGE2_MODE=rerank`): Qwen3 cross-encoder P(yes);
    fail-closed when the reranker is unavailable (see recall_system_armed).
    Test/dev (`embed`, explicit config only): FastEmbed pos−neg margin.
    Experimental (`logit`): LocalLLM YES/NO logits — tiny instruction-tuned
    models latch onto a completion token and should not be used in production.

    `out`, when given, collects diagnostics: `matched_example` (the cue that won,
    for LRU usage tracking) and `stage2_negative_score`.
    """
    mode = _stage2_mode()
    if mode == "rerank":
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
    """Verify Stage-1 matches. Returns (verified, rejected). Fail-open keeps match.

    Verification is capped at _STAGE2_MAX_CANDIDATES (best Stage-1 positive
    first) so one noisy scan cannot buy an unbounded number of cross-encoder
    passes; overflow is rejected unverified with reason stage2_candidate_cap.
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

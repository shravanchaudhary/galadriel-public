import json
import logging
import os
import re
import threading
import time
from pathlib import Path

import numpy as np
from semantic_router import Route
from semantic_router.routers import SemanticRouter

from .db_ops import get_db

log = logging.getLogger("galadriel.recall")

RECALLS_COLLECTION = "recalls"
PROPOSED_RECALLS_COLLECTION = "proposed_recalls"

_ENCODER = None
_ENCODER_TYPE = None
_ROUTER_CACHE = None
_CACHED_RECALL_IDS = set()
_SLM_CLIENT = None
_SLM_CLIENT_FAILED = False
_SLM_LOADED_KEY = None
_SLM_LOCK = threading.RLock()

# Default per-recall Stage-1 thresholds (absolute, independent).
DEFAULT_POSITIVE_THRESHOLD = 0.6
DEFAULT_NEGATIVE_THRESHOLD = 0.6
# Router encoder floor is open so per-recall positive_threshold can go below 0.6.
_ROUTER_SCORE_FLOOR = 0.0
# Lexical hard-hit score (exact cue match); still subject to neg threshold.
_LEXICAL_POSITIVE_SCORE = 1.0

# YES must beat NO by this logit margin (tiny models are YES-biased otherwise).
_SLM_LOGIT_MARGIN_DEFAULT = 2.0


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
    """Accept semantic if pos >= pos_thr and neg < neg_thr; lexical hard-hits skip neg thr."""
    pos_thr = recall_positive_threshold(recall)
    neg_thr = recall_negative_threshold(recall)
    negatives = recall.get("negative_examples") or []
    negative_score = _max_cosine(encoder, chunk, negatives) if negatives else None

    if match_source == "semantic" and positive_score < pos_thr:
        log.info(
            f"[Semantic Miss] route='{recall_id}' source={match_source} "
            f"pos={positive_score:.4f} < pos_thr={pos_thr:.4f} chunk='{chunk[:100]}'"
        )
        return

    # Absolute negative threshold applies to semantic proposals only. Lexical cues
    # are intentional hard triggers and must not be blocked by near-miss cosine.
    if (
        match_source == "semantic"
        and negative_score is not None
        and negative_score >= neg_thr
    ):
        log.info(
            f"[Semantic Veto] route='{recall_id}' source={match_source} "
            f"pos={positive_score:.4f} neg={negative_score:.4f} "
            f">= neg_thr={neg_thr:.4f} chunk='{chunk[:100]}'"
        )
        return

    cue_note = f" cue={lexical_cue!r}" if lexical_cue else ""
    log.info(
        f"[Semantic Match] route='{recall_id}' source={match_source} "
        f"pos={positive_score:.4f}>={pos_thr:.4f} "
        f"neg={negative_score if negative_score is not None else 'n/a'}"
        f"(thr={neg_thr:.4f})"
        f"{cue_note} chunk='{chunk[:100]}'"
    )
    seen.add(recall_id)
    match_obj = dict(recall)
    normalize_recall_thresholds(match_obj)
    match_obj["positive_score"] = positive_score
    match_obj["negative_score"] = negative_score
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
    """Scan text: semantic OR lexical cue hit, then per-recall pos/neg thresholds."""
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
    return {
        "model": key,
        "loaded_model": loaded,
        "loaded": loaded_now,
        "enabled": _slm_verify_enabled(),
        "options": list_model_profiles(),
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


# Global hard-negatives shown on every Stage-2 prompt. Tiny models are YES-biased
# on markup / bare tool names / file-write ack noise from tool-output scanning.
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
    """Few-shot YES/NO prompt for logit scoring.

    Order: task rule → recall instruction → global hard-negatives → this
    recall's own pos/neg cues → query chunk. Keep examples short; 1B models
    need explicit NO anchors more than long prose.
    """
    instruction = (recall.get("instruction") or "").strip()
    lines = [
        "Decide if TEXT matches the recall intent. Answer YES or NO.",
        "YES only if TEXT clearly asks for or is about that intent.",
        "NO for HTML/CSS markup, bare tool names, file-write acks, or unrelated chatter.",
        f"Recall: {instruction[:160]}",
    ]
    # Global hard-negatives first (tool-output junk), then this recall's cues.
    # Putting globals last over-biases tiny models toward NO on true positives.
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


def verify_recall_candidate_slm(
    chunk: str,
    recall: dict,
    *,
    client=None,
    margin: float | None = None,
) -> tuple[bool, str]:
    """Stage-2 intent filter. Returns (ok, reason). Fail-open on errors.

    Uses in-process LocalLLM YES/NO logit margin with few-shot examples from
    the recall itself. ok=True means inject/fire; ok=False means proposed-only.
    """
    if not _slm_verify_enabled():
        return True, "slm_disabled"

    instruction = (recall.get("instruction") or "").strip()
    text = (chunk or "").strip()
    if not text or not instruction:
        return True, "slm_skip_empty"

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
    """Verify Stage-1 matches. Returns (verified, rejected). Fail-open keeps match."""
    verified: list[dict] = []
    rejected: list[dict] = []
    for match in matches:
        chunk = match.get("matched_chunk") or ""
        ok, reason = verify_recall_candidate_slm(chunk, match, client=client)
        enriched = dict(match)
        enriched["slm_verified"] = bool(ok)
        enriched["slm_reason"] = reason
        if ok:
            verified.append(enriched)
        else:
            rejected.append(enriched)
            log.info(
                "[SLM Reject] route=%r reason=%s chunk=%r",
                match.get("recall_id"),
                reason,
                (chunk or "")[:100],
            )
    return verified, rejected

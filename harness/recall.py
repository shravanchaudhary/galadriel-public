import json
import logging
import os
import re
from pathlib import Path

from semantic_router import Route
from semantic_router.routers import SemanticRouter

from .db_ops import get_db

log = logging.getLogger("galadriel.recall")

RECALLS_COLLECTION = "recalls"

_ENCODER = None
_ENCODER_TYPE = None
_ROUTER_CACHE = None
_CACHED_RECALL_IDS = set()

def get_encoder(force_type=None, force_threshold=None):
    """Lazily load and cache the encoder model based on environment config."""
    global _ENCODER, _ENCODER_TYPE, _ROUTER_CACHE, _CACHED_RECALL_IDS
    
    encoder_type = (force_type or os.environ.get("RECALL_ENCODER", "fastembed")).lower()
    
    if force_threshold is None:
        try:
            from .tower_settings import get_semantic_threshold
            force_threshold = get_semantic_threshold(0.80)
        except Exception:
            force_threshold = 0.80
            
    if _ENCODER is not None and _ENCODER_TYPE == encoder_type:
        if _ENCODER.score_threshold != force_threshold:
            _ENCODER.score_threshold = force_threshold
        return _ENCODER
        
    # If encoder type changed, clear the router cache
    if _ENCODER is not None:
        _ROUTER_CACHE = None
        _CACHED_RECALL_IDS = set()
    
    if encoder_type == "gemini":
        try:
            from semantic_router.encoders import GoogleEncoder
            _ENCODER = GoogleEncoder(name="models/text-embedding-004")
            _ENCODER_TYPE = "gemini"
            if force_threshold is not None:
                _ENCODER.score_threshold = force_threshold
            log.info("Initialized Gemini encoder for semantic router")
        except Exception as e:
            log.warning(f"Failed to load GoogleEncoder, falling back to fastembed: {e}")
            encoder_type = "fastembed"
            
    if encoder_type == "fastembed":
        from semantic_router.encoders import FastEmbedEncoder
        # Use a fast local model, doesn't block the app
        _ENCODER = FastEmbedEncoder(name="BAAI/bge-small-en-v1.5")
        _ENCODER.score_threshold = force_threshold if force_threshold is not None else 0.80
        _ENCODER_TYPE = "fastembed"
        log.info("Initialized FastEmbedEncoder for semantic router")
        
    return _ENCODER

async def async_get_semantic_threshold(default: float = 0.80) -> float:
    """Fetch the global semantic threshold using the async Motor DB client."""
    from .db_ops import get_db
    import os
    db = get_db()
    if db is None:
        return default
    try:
        tenant_id = os.environ.get("REPLIKA_TENANT_ID", "default").strip() or "default"
        doc_id = f"{tenant_id}:semantic_threshold"
        doc = await db["tower_settings"].find_one({"_id": doc_id})
        if doc and "threshold" in doc:
            return float(doc["threshold"])
    except Exception:
        pass
    return default

def get_semantic_router(recalls: list[dict], force_encoder_type=None, force_threshold=None, force_reload=False) -> SemanticRouter:
    """Get or build the SemanticRouter for the current set of recalls."""
    global _ROUTER_CACHE, _CACHED_RECALL_IDS
    
    current_ids = {r.get("recall_id") for r in recalls if r.get("recall_id")}
    
    # We call get_encoder first, which will clear _ROUTER_CACHE if the encoder type changed
    encoder = get_encoder(force_encoder_type, force_threshold)
    
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
            # Ensure proper schema
            for recall in data:
                recall["source"] = "system"
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
                recalls.append(doc)
        except Exception as e:
            log.error(f"Failed to fetch user recalls from DB: {e}")
    return recalls

def scan_text_for_recalls(text: str, recalls: list[dict], force_encoder_type=None, force_threshold=None) -> list[dict]:
    """Scan text against all recalls and return matched recall objects using semantic router."""
    if not text or not recalls:
        return []
    
    router = get_semantic_router(recalls, force_encoder_type, force_threshold)
    if not router:
        return []
        
    recall_map = {r.get("recall_id"): r for r in recalls if r.get("recall_id")}
    matches = []
    seen = set()
    
    # Split text into manageable chunks (e.g. paragraphs/lines) for semantic matching
    chunks = [c.strip() for c in text.split("\n") if c.strip()]
    
    for chunk in chunks:
        # Semantic router checks if the chunk falls within a threshold tolerance of any route
        # Using limit=5 to get the most relevant routes that match above threshold
        decisions = router(chunk, limit=5)
        
        # If router returns a single object instead of a list (fallback), wrap it
        if decisions and not isinstance(decisions, list):
            decisions = [decisions]
            
        if not decisions:
            continue
            
        for decision in decisions:
            if decision and decision.name and decision.name != "None":
                score = getattr(decision, "similarity_score", "N/A")
                log.info(f"[Semantic Match] route='{decision.name}' score={score} chunk='{chunk[:100]}'")
                
                float_score = None
                if score != "N/A":
                    float_score = float(score) if hasattr(score, 'item') else float(score)
                    # Use recall's specific threshold if defined, otherwise fallback
                    recall_specific = recall_map.get(decision.name, {}).get("threshold")
                    threshold = force_threshold if force_threshold is not None else (recall_specific if recall_specific is not None else router.encoder.score_threshold)
                    if float_score < threshold:
                        continue
                
                if decision.name not in seen:
                    seen.add(decision.name)
                    if decision.name in recall_map:
                        match_obj = dict(recall_map[decision.name])
                        if float_score is not None:
                            match_obj["similarity_score"] = float_score
                        matches.append(match_obj)
                
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

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
_ROUTER_CACHE = None
_CACHED_RECALL_IDS = set()

def get_encoder():
    """Lazily load and cache the encoder model based on environment config."""
    global _ENCODER
    if _ENCODER is not None:
        return _ENCODER
    
    encoder_type = os.environ.get("RECALL_ENCODER", "fastembed").lower()
    
    if encoder_type == "gemini":
        try:
            from semantic_router.encoders import GoogleEncoder
            _ENCODER = GoogleEncoder(name="models/text-embedding-004")
            log.info("Initialized Gemini encoder for semantic router")
        except Exception as e:
            log.warning(f"Failed to load GoogleEncoder, falling back to fastembed: {e}")
            encoder_type = "fastembed"
            
    if encoder_type == "fastembed":
        from semantic_router.encoders import FastEmbedEncoder
        # Use a fast local model, doesn't block the app
        _ENCODER = FastEmbedEncoder(name="BAAI/bge-small-en-v1.5")
        log.info("Initialized FastEmbedEncoder for semantic router")
        
    return _ENCODER

def get_semantic_router(recalls: list[dict]) -> SemanticRouter:
    """Get or build the SemanticRouter for the current set of recalls."""
    global _ROUTER_CACHE, _CACHED_RECALL_IDS
    
    current_ids = {r.get("recall_id") for r in recalls if r.get("recall_id")}
    
    if _ROUTER_CACHE is not None and current_ids == _CACHED_RECALL_IDS:
        return _ROUTER_CACHE
        
    routes = []
    for recall in recalls:
        recall_id = recall.get("recall_id")
        if not recall_id:
            continue
            
        utterances = []
        for tag in recall.get("regex_tags", []):
            # Clean up the legacy regex tags into natural language utterances
            clean = tag.replace("\\b", "").replace("(", "").replace(")", "").replace("\\s*", " ").replace(".*", " ")
            clean = clean.replace("?", "").replace("\\", "")
            utterances.extend([u.strip() for u in clean.split("|") if u.strip()])
            
        if utterances:
            routes.append(Route(name=recall_id, utterances=utterances))
            
    if not routes:
        return None
        
    encoder = get_encoder()
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

def scan_text_for_recalls(text: str, recalls: list[dict]) -> list[dict]:
    """Scan text against all recalls and return matched recall objects using semantic router."""
    if not text or not recalls:
        return []
    
    router = get_semantic_router(recalls)
    if not router:
        return []
        
    recall_map = {r.get("recall_id"): r for r in recalls if r.get("recall_id")}
    matches = []
    seen = set()
    
    # Split text into manageable chunks (e.g. paragraphs/lines) for semantic matching
    chunks = [c.strip() for c in text.split("\n") if c.strip()]
    
    for chunk in chunks:
        # Semantic router checks if the chunk falls within a threshold tolerance of any route
        # Using limit=None to get all routes that match above threshold for this chunk
        decisions = router(chunk, limit=None)
        
        # If router returns a single object instead of a list (fallback), wrap it
        if decisions and not isinstance(decisions, list):
            decisions = [decisions]
            
        if not decisions:
            continue
            
        for decision in decisions:
            if decision and decision.name and decision.name not in seen:
                seen.add(decision.name)
                if decision.name in recall_map:
                    matches.append(recall_map[decision.name])
                
    if matches:
        log.debug(f"Semantic scan matched {len(matches)} rule(s) for text: {text[:200]}...")
                
    return matches

def _build_ledger(messages: list) -> str:
    lines = []
    for msg in messages[-20:]:
        role = msg.get("role")
        content = msg.get("content")
        thought = msg.get("_thought", "")
        
        if role == "assistant":
            lines.append("Assistant:")
            if thought:
                lines.append(f"  Thought: {thought[:200]}...")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        btype = block.get("type")
                        if btype == "text":
                            text = block.get("text", "")
                            if text:
                                lines.append(f"  Output: {text[:100]}...")
                        elif btype == "tool_use":
                            name = block.get("name")
                            args = block.get("input", {})
                            lines.append(f"  Tool Call: {name} (args: {str(args)[:100]}...)")
            elif isinstance(content, str):
                if content:
                    lines.append(f"  Output: {content[:100]}...")
        elif role == "user":
            lines.append("User:")
            if isinstance(content, str):
                lines.append(f"  {content[:100]}...")
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        btype = block.get("type")
                        if btype == "text":
                            text = block.get("text", "")
                            if text:
                                lines.append(f"  {text[:100]}...")
                        elif btype == "tool_result":
                            name = block.get("name", "tool")
                            lines.append(f"  Tool Result for {name}: {str(block.get('content', ''))[:100]}...")
    return "\n".join(lines)

async def check_completion_nudge_needed(matched_recalls: list[dict], messages: list) -> list[dict]:
    if not matched_recalls:
        return []
        
    from . import model_registry
    
    ledger = _build_ledger(messages)
    
    prompt = (
        "You are an evaluator checking if a primary agent missed critical steps that are expected of it.\n\n"
        "Here is a ledger of the recent conversation, showing the agent's thoughts, tool calls, and truncated outputs:\n"
        f"---\n{ledger}\n---\n\n"
        "Here are the rules (recalls) that might apply to this conversation:\n"
    )
    
    for i, m in enumerate(matched_recalls):
        prompt += f"[{i}] {m.get('instruction')}\n"
        
    prompt += (
        "\nFor each rule, evaluate whether the primary agent FORGOT to follow it in the current turn. "
        "Return nudge_needed: true ONLY if the agent clearly missed this step and needs to be nudged to do it right now.\n"
        "Respond in valid JSON format ONLY: {\"results\": [{\"index\": 0, \"nudge_needed\": true/false}, ...]}"
    )
    
    # We use a faster/cheaper model for evaluation if available, defaulting to standard if not.
    try:
        provider = model_registry.get_provider("compaction")
        model = model_registry.model_for("compaction")
    except Exception:
        provider = model_registry.get_provider("default")
        model = model_registry.model_for("default")
    
    try:
        response = await provider.create_message(
            model=model,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
            thinking=False
        )
        
        text = ""
        for block in getattr(response, "content", []):
            if hasattr(block, "type") and block.type == "text":
                text += block.text
            elif isinstance(block, dict) and block.get("type") == "text":
                text += block.get("text", "")
                
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end >= start:
            data = json.loads(text[start:end+1])
            results = data.get("results", [])
            
            needed_indices = {r["index"] for r in results if r.get("nudge_needed")}
            needed_recalls = [m for i, m in enumerate(matched_recalls) if i in needed_indices]
            return needed_recalls
    except Exception as e:
        log.warning(f"Failed to filter recalls with LLM: {e}")
        
    # If the LLM check fails, we conservatively return nothing to avoid endless loops
    return []

def generate_nudge(matched_recalls: list[dict]) -> str:
    """Generate the nudge text from matched recalls."""
    if not matched_recalls:
        return ""
    
    nudge_lines = ["⚡ **Automated Nudge:**"]
    for recall in matched_recalls:
        nudge_lines.append(f"- {recall.get('instruction')}")
    
    return "\n".join(nudge_lines)

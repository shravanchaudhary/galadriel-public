import json
import logging
import os
import re
from pathlib import Path

from .db_ops import get_db

log = logging.getLogger("galadriel.recall")

RECALLS_COLLECTION = "recalls"

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
    """Scan text against all recalls and return matched recall objects."""
    if not text:
        return []
    
    matches = []
    # Case insensitive matching
    lower_text = text.lower()
    
    for recall in recalls:
        for tag in recall.get("regex_tags", []):
            try:
                # Compile regex with ignorecase
                pattern = re.compile(tag, re.IGNORECASE)
                if pattern.search(lower_text):
                    matches.append(recall)
                    break # Only add once per recall
            except re.error as e:
                log.error(f"Invalid regex tag '{tag}' in recall {recall.get('recall_id')}: {e}")
                
    if matches:
        log.debug(f"Recall scan matched {len(matches)} rule(s) for text: {text[:200]}...")
                
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

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

def generate_nudge(matched_recalls: list[dict]) -> str:
    """Generate the nudge text from matched recalls."""
    if not matched_recalls:
        return ""
    
    nudge_lines = ["⚡ **Automated Nudge:**"]
    for recall in matched_recalls:
        nudge_lines.append(f"- {recall.get('instruction')}")
    
    return "\n".join(nudge_lines)

"""Contact enrichment — two tools: fetch_email and fetch_phone.

Ports wario's decision-maker email/phone waterfall (was entangled with the lead
model + billing in ``services/intent_leads.py``) into two flat tools, minus
credits/cost-tracking/webhooks.

  * ``fetch_email`` → FullEnrich first (cheapest, with a verification status),
    Explorium contacts as fallback.
  * ``fetch_phone`` → Explorium contacts first, FullEnrich as fallback.

FullEnrich needs first+last name and (domain or company_name); it bills per
field, so each tool only ever asks it for its own field. Explorium needs a
``prospect_id`` and returns email+phone together in one flat-cost call — so
whenever a waterfall hits Explorium and gets the *other* field for free, that
value is cached too, making the sibling tool's call free.

Caching: a shared 60-day MongoDB cache (``contact_enrichment_cache``) keyed by
contact identity. Explorium is also cached by the ExploriumClient; this layer
additionally caches FullEnrich (which has none of its own) and "not found".
"""

import json
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .fullenrich import FullEnrichService

_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from lib.db import get_db  # noqa: E402  (path set above)

logger = logging.getLogger(__name__)

CACHE_TTL_DAYS = 60
COL_NAME = "contact_enrichment_cache"
_indexes_ensured = False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _cache_key(first_name, last_name, company_name, domain, linkedin_url, prospect_id) -> str:
    """Stable identity key. LinkedIn URL is strongest; then prospect_id; then name+company/domain."""
    if linkedin_url:
        u = re.sub(r"^https?://", "", linkedin_url.lower().strip())
        u = re.sub(r"^([a-z]{2,3}\.)?www\.", "", u)
        u = re.sub(r"^[a-z]{2,3}\.linkedin\.", "linkedin.", u)
        return "li:" + u.split("?")[0].split("#")[0].rstrip("/")
    if prospect_id:
        return "pid:" + prospect_id.strip().lower()
    who = f"{first_name} {last_name}".strip().lower()
    org = (domain or company_name or "").strip().lower()
    return f"nm:{who}|{org}"


async def _get_fresh_row(col, key: str) -> Optional[dict]:
    global _indexes_ensured
    if not _indexes_ensured:
        await col.create_index("key", unique=True, background=True)
        await col.create_index("cached_at", background=True)
        _indexes_ensured = True
    row = await col.find_one({"key": key})
    if row and row.get("cached_at") and row["cached_at"] >= _utcnow() - timedelta(days=CACHE_TTL_DAYS):
        return row
    return None


async def _save(col, key: str, updates: dict) -> None:
    updates["key"] = key
    updates["cached_at"] = _utcnow()
    await col.update_one({"key": key}, {"$set": updates}, upsert=True)


async def fetch_email(
    first_name: str,
    last_name: str,
    company_name: Optional[str] = None,
    domain: Optional[str] = None,
    linkedin_url: Optional[str] = None,
    prospect_id: Optional[str] = None,
    **_,
) -> str:
    """Find a person's work email (FullEnrich → Explorium fallback), cached 60 days."""
    if not first_name or not last_name:
        return json.dumps({"error": "first_name and last_name are required."})
    can_fullenrich = bool(domain or company_name)
    can_explorium = bool(prospect_id)
    if not can_fullenrich and not can_explorium:
        return json.dumps({"error": "Provide (domain or company_name) for FullEnrich, and/or a prospect_id for Explorium."})

    col = get_db()[COL_NAME]
    key = _cache_key(first_name, last_name, company_name, domain, linkedin_url, prospect_id)
    row = await _get_fresh_row(col, key) or {}
    if (row.get("attempted") or {}).get("email"):
        return json.dumps({"email": row.get("email"), "email_verified": row.get("email_verified"), "source": row.get("email_source"), "cached": True})

    email = email_verified = source = None
    updates = {"attempted": {**(row.get("attempted") or {}), "email": True}}

    # Step 1 — FullEnrich (email field only)
    if can_fullenrich:
        try:
            fe = await FullEnrichService.get_email(
                firstname=first_name, lastname=last_name,
                company_name=company_name, domain=domain, linkedin_url=linkedin_url,
            )
        except Exception:
            logger.exception("FullEnrich email lookup failed for %s", key)
            fe = {}
        if fe.get("email"):
            email, email_verified, source = fe["email"], fe.get("email_verified"), "fullenrich"

    # Step 2 — Explorium contacts (returns phone too → cache it free)
    if not email and can_explorium:
        exp = await _explorium_contacts(prospect_id, key)
        if exp.get("email"):
            email, source = exp["email"], "explorium"
        if exp.get("phone"):
            updates.update({"phone": exp["phone"], "phone_source": "explorium", "attempted": {**updates["attempted"], "phone": True}})

    updates.update({"email": email, "email_verified": email_verified, "email_source": source})
    await _save(col, key, updates)
    return json.dumps({"email": email, "email_verified": email_verified, "source": source, "cached": False})


async def fetch_phone(
    first_name: str,
    last_name: str,
    company_name: Optional[str] = None,
    domain: Optional[str] = None,
    linkedin_url: Optional[str] = None,
    prospect_id: Optional[str] = None,
    **_,
) -> str:
    """Find a person's phone (Explorium → FullEnrich fallback), cached 60 days."""
    if not first_name or not last_name:
        return json.dumps({"error": "first_name and last_name are required."})
    can_fullenrich = bool(domain or company_name)
    can_explorium = bool(prospect_id)
    if not can_fullenrich and not can_explorium:
        return json.dumps({"error": "Provide (domain or company_name) for FullEnrich, and/or a prospect_id for Explorium."})

    col = get_db()[COL_NAME]
    key = _cache_key(first_name, last_name, company_name, domain, linkedin_url, prospect_id)
    row = await _get_fresh_row(col, key) or {}
    if (row.get("attempted") or {}).get("phone"):
        return json.dumps({"phone": row.get("phone"), "source": row.get("phone_source"), "cached": True})

    phone = source = None
    updates = {"attempted": {**(row.get("attempted") or {}), "phone": True}}

    # Step 1 — Explorium contacts (returns email too → cache it free)
    if can_explorium:
        exp = await _explorium_contacts(prospect_id, key)
        if exp.get("phone"):
            phone, source = exp["phone"], "explorium"
        if exp.get("email"):
            updates.update({"email": exp["email"], "email_source": "explorium", "attempted": {**updates["attempted"], "email": True}})

    # Step 2 — FullEnrich fallback (phone field only)
    if not phone and can_fullenrich:
        try:
            fe = await FullEnrichService.get_phone(
                firstname=first_name, lastname=last_name,
                company_name=company_name, domain=domain, linkedin_url=linkedin_url,
            )
        except Exception:
            logger.exception("FullEnrich phone lookup failed for %s", key)
            fe = {}
        if fe.get("phone"):
            phone, source = fe["phone"], "fullenrich"

    updates.update({"phone": phone, "phone_source": source})
    await _save(col, key, updates)
    return json.dumps({"phone": phone, "source": source, "cached": False})


async def _explorium_contacts(prospect_id: str, key: str) -> dict:
    try:
        return await FullEnrichService._fetch_contacts_from_explorium(prospect_id) or {}
    except Exception:
        logger.exception("Explorium contacts lookup failed for %s", key)
        return {}


# ── Dispatch + tool definitions (Anthropic schema, matching tools.py) ─────────
CONTACT_TOOL_NAMES = frozenset({"fetch_email", "fetch_phone"})


async def execute_contact_tool(name: str, inputs: dict) -> str:
    inputs = inputs or {}
    if name == "fetch_email":
        return await fetch_email(**inputs)
    if name == "fetch_phone":
        return await fetch_phone(**inputs)
    return json.dumps({"error": f"Unknown contact tool: {name}"})


_IDENTITY_PROPS = {
    "first_name": {"type": "string", "description": "Person's first name (required)."},
    "last_name": {"type": "string", "description": "Person's last name (required)."},
    "company_name": {"type": "string", "description": "Their company name (FullEnrich)."},
    "domain": {"type": "string", "description": "Company domain, e.g. 'acme.com' (FullEnrich; better than company_name)."},
    "linkedin_url": {"type": "string", "description": "Person's LinkedIn URL (improves FullEnrich match)."},
    "prospect_id": {"type": "string", "description": "Explorium prospect_id (enables the Explorium path)."},
}

CONTACT_TOOL_DEFINITIONS = [
    {
        "name": "fetch_email",
        "description": (
            "Find a person's work EMAIL via a waterfall: FullEnrich first (cheapest, "
            "returns a verification status), Explorium contacts as fallback. Cached 60 "
            "days in MongoDB (repeats free, including 'not found'). FullEnrich needs "
            "first_name+last_name and (domain or company_name); Explorium needs a "
            "prospect_id (from explorium_search_prospects/explorium_match_prospect). "
            "Supply everything you have. Returns email, email_verified, and source. "
            "Only fetches email — call fetch_phone separately for a number. Needs "
            "FULLENRICH_API_KEY and/or AGENTSOURCE_API_KEY; a missing key disables that "
            "provider. Never construct an email — only use what this returns."
        ),
        "input_schema": {"type": "object", "properties": _IDENTITY_PROPS, "required": ["first_name", "last_name"]},
    },
    {
        "name": "fetch_phone",
        "description": (
            "Find a person's PHONE via a waterfall: Explorium contacts first, FullEnrich "
            "as fallback. Cached 60 days in MongoDB (repeats free). Explorium needs a "
            "prospect_id (from explorium_search_prospects/explorium_match_prospect); "
            "FullEnrich needs first_name+last_name and (domain or company_name). Returns "
            "phone and source. Only fetches phone — call fetch_email separately for an "
            "email. Needs AGENTSOURCE_API_KEY and/or FULLENRICH_API_KEY; a missing key "
            "disables that provider."
        ),
        "input_schema": {"type": "object", "properties": _IDENTITY_PROPS, "required": ["first_name", "last_name"]},
    },
]

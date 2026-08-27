"""Explorium lead-sourcing tools — thin wrappers over harness/explorium.py.

These expose the Explorium 100M+ company/prospect database to the agent as
flat tools (same shape as everything in `harness/tools.py`). They are kept in
their own module so the engine (`harness/explorium.py`, the API client + its
60-day Mongo cache) stays cleanly separated from the tool surface.

Two families:

  Sourcing (find/resolve companies & people by ICP)
    - explorium_search_businesses   : discover companies by filters
    - explorium_business_statistics : FREE availability check before paid search
    - explorium_autocomplete        : resolve exact filter values
    - explorium_match_business      : domain/name/linkedin → business_id
    - explorium_search_prospects    : find people at a company by title/dept
    - explorium_match_prospect      : email/linkedin/name → prospect_id

  Research (enrich a known company/person + buying signals)
    - explorium_enrich_business     : firmographics, funding, technographics, …
    - explorium_business_events     : hiring surges, funding, M&A, …
    - explorium_enrich_prospect     : profile / contacts / linkedin posts
    - explorium_prospect_events     : role changes, anniversaries

Single-tenant: org-scoped billing is not used here, so `org_id` is left None
(the client collapses to "free on cache hit" in that case). The cache keys on
(business_id, enrichment_type) etc., which is org-agnostic.
"""

import json
import logging
import re
from datetime import datetime, timedelta
from typing import List, Optional

from .explorium import (
    BUSINESS_ENRICHMENT_URL_TYPES,
    BUSINESS_EVENT_TYPES,
    PROSPECT_ENRICHMENT_URL_TYPES,
    PROSPECT_EVENT_TYPES,
    ExploriumClient,
)

logger = logging.getLogger(__name__)


# ── Date helpers (enrichment types that need extra params) ────────────────────
def _current_quarter_iso() -> str:
    now = datetime.now()
    start_month = ((now.month - 1) // 3) * 3 + 1
    return f"{now.year}-{start_month:02d}-01T00:00:00"


def _quarter_to_iso(date_str: str) -> str:
    s = date_str.strip()
    m = re.match(r"^(\d{4})-[Qq]([1-4])$", s)
    if m:
        year, quarter = int(m.group(1)), int(m.group(2))
        return f"{year}-{(quarter - 1) * 3 + 1:02d}-01T00:00:00"
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return f"{s}T00:00:00"
    return s


def _current_month() -> str:
    return datetime.now().strftime("%Y-%m")


# ── Sourcing tools ────────────────────────────────────────────────────────────
async def explorium_search_businesses(
    filters: Optional[dict] = None, size: int = 20, page: int = 1, **_
) -> str:
    """Discover companies matching ICP filters. First ≤5 are firmographics-enriched."""
    try:
        client = ExploriumClient()
        resp = await client.search_businesses(
            size=size, page_size=size, page=page, filters=filters or {}
        )
        return json.dumps(
            {
                "data": resp.get("data") or [],
                "total_results": resp.get("total_results"),
                "preview_note": resp.get("preview_note"),
                "credits_used": resp.get("credits_used"),
                "next_cursor": resp.get("next_cursor"),
            },
            default=str,
        )
    except Exception as exc:
        logger.warning("explorium_search_businesses failed: %s", exc)
        return json.dumps({"error": str(exc)})


async def explorium_business_statistics(filters: Optional[dict] = None, **_) -> str:
    """FREE aggregated counts for a filter plan — use to size availability before paid search."""
    try:
        client = ExploriumClient()
        resp = await client.fetch_businesses_statistics(filters=filters or {})
        return json.dumps(resp, default=str)
    except Exception as exc:
        logger.warning("explorium_business_statistics failed: %s", exc)
        return json.dumps({"error": str(exc)})


async def explorium_autocomplete(
    field: str, query: str = "", semantic_search: bool = False, **_
) -> str:
    """FREE — resolve exact filter values (category, location, tech, intent topics)."""
    try:
        client = ExploriumClient()
        results = await client.autocomplete(
            field, query=query, semantic_search=semantic_search
        )
        return json.dumps(results, default=str)
    except Exception as exc:
        logger.warning("explorium_autocomplete failed: %s", exc)
        return json.dumps({"error": str(exc)})


async def explorium_match_business(
    name: Optional[str] = None,
    domain: Optional[str] = None,
    linkedin_url: Optional[str] = None,
    **_,
) -> str:
    """Resolve a company (by name/domain/linkedin) to its Explorium business_id."""
    entry = {k: v for k, v in {"name": name, "domain": domain, "linkedin_url": linkedin_url}.items() if v}
    if not entry:
        return json.dumps({"error": "Provide at least one of: name, domain, linkedin_url."})
    try:
        client = ExploriumClient()
        resp = await client.match_businesses([entry])
        return json.dumps(resp.get("matched_businesses") or [], default=str)
    except Exception as exc:
        logger.warning("explorium_match_business failed: %s", exc)
        return json.dumps({"error": str(exc)})


async def explorium_search_prospects(
    business_id: str,
    job_titles: Optional[List[str]] = None,
    departments: Optional[List[str]] = None,
    countries: Optional[List[str]] = None,
    seniority_levels: Optional[List[str]] = None,
    size: int = 20,
    **_,
) -> str:
    """Find people at a company, filtered by title/department/country/seniority."""
    filters: dict = {"business_id": {"values": [business_id]}}
    if job_titles:
        filters["job_title"] = {"values": job_titles}
    if departments:
        filters["job_department"] = {"values": departments}
    if countries:
        filters["country"] = {"values": countries}
    if seniority_levels:
        filters["job_level"] = {"values": seniority_levels}
    try:
        client = ExploriumClient()
        resp = await client.search_prospects(size=size, page_size=size, filters=filters)
        return json.dumps(resp.get("data") or [], default=str)
    except Exception as exc:
        logger.warning("explorium_search_prospects failed: %s", exc)
        return json.dumps({"error": str(exc)})


async def explorium_match_prospect(
    full_name: Optional[str] = None,
    company_name: Optional[str] = None,
    email: Optional[str] = None,
    linkedin: Optional[str] = None,
    business_id: Optional[str] = None,
    **_,
) -> str:
    """Resolve a person (by email/linkedin/name+company) to their Explorium prospect_id."""
    entry = {
        k: v
        for k, v in {
            "full_name": full_name,
            "company_name": company_name,
            "email": email,
            "linkedin": linkedin,
            "business_id": business_id,
        }.items()
        if v
    }
    if not entry:
        return json.dumps({"error": "Provide at least one identifier (email, linkedin, or full_name+company_name)."})
    try:
        client = ExploriumClient()
        resp = await client.match_prospects([entry])
        return json.dumps(resp.get("matched_prospects") or [], default=str)
    except Exception as exc:
        logger.warning("explorium_match_prospect failed: %s", exc)
        return json.dumps({"error": str(exc)})


# ── Research tools ────────────────────────────────────────────────────────────
async def explorium_enrich_business(
    business_id: str,
    enrichment_type: str,
    date: Optional[str] = None,
    keywords: Optional[List[str]] = None,
    month_period: Optional[str] = None,
    **_,
) -> str:
    """Structured enrichment for one company (firmographics, funding, technographics, …)."""
    if enrichment_type not in BUSINESS_ENRICHMENT_URL_TYPES:
        return json.dumps({"error": f"Unknown enrichment_type '{enrichment_type}'. Valid: {BUSINESS_ENRICHMENT_URL_TYPES}"})
    parameters: Optional[dict] = None
    if enrichment_type == "financial_indicators":
        parameters = {"date": _quarter_to_iso(date) if date else _current_quarter_iso()}
    elif enrichment_type == "website_traffic":
        parameters = {"month_period": month_period or _current_month()}
    elif enrichment_type == "company_website_keywords":
        if not keywords:
            return json.dumps({"error": "company_website_keywords requires a 'keywords' list (e.g. ['CRM','Salesforce'])."})
        parameters = {"keywords": keywords}
    try:
        client = ExploriumClient()
        resp = await client.bulk_enrich_businesses([business_id], enrichment_type, parameters=parameters)
        items = resp.get("data") or []
        data = items[0].get("data") if items else {}
        if not data:
            return json.dumps({"result": None, "note": f"No '{enrichment_type}' data found for this business."})
        return json.dumps(data, default=str)
    except Exception as exc:
        logger.warning("explorium_enrich_business failed: %s", exc)
        return json.dumps({"error": str(exc)})


async def explorium_business_events(
    business_id: str, event_types: List[str], days_back: int = 90, **_
) -> str:
    """Recent company events (hiring surges, funding, M&A, …) — buying signals."""
    unknown = [e for e in event_types if e not in BUSINESS_EVENT_TYPES]
    if unknown:
        return json.dumps({"error": f"Unknown event_types {unknown}. Valid: {BUSINESS_EVENT_TYPES}"})
    ts_from = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        client = ExploriumClient()
        resp = await client.fetch_businesses_events([business_id], event_types, timestamp_from=ts_from)
        return json.dumps((resp.get("output_events") or [])[:50], default=str)
    except Exception as exc:
        logger.warning("explorium_business_events failed: %s", exc)
        return json.dumps({"error": str(exc)})


async def explorium_enrich_prospect(
    prospect_id: str, enrichment_type: str = "profiles", **_
) -> str:
    """Detailed enrichment for one person (profile / contacts / linkedin posts)."""
    if enrichment_type not in PROSPECT_ENRICHMENT_URL_TYPES:
        return json.dumps({"error": f"Unknown enrichment_type '{enrichment_type}'. Valid: {PROSPECT_ENRICHMENT_URL_TYPES}"})
    try:
        client = ExploriumClient()
        resp = await client.bulk_enrich_prospects([prospect_id], enrichment_type)
        items = resp.get("data") or []
        data = items[0].get("data") if items else {}
        return json.dumps(data, default=str)
    except Exception as exc:
        logger.warning("explorium_enrich_prospect failed: %s", exc)
        return json.dumps({"error": str(exc)})


async def explorium_prospect_events(
    prospect_id: str, event_types: Optional[List[str]] = None, days_back: int = 90, **_
) -> str:
    """Recent events for a person (role change, company hop, anniversary)."""
    et = event_types or PROSPECT_EVENT_TYPES
    unknown = [e for e in et if e not in PROSPECT_EVENT_TYPES]
    if unknown:
        return json.dumps({"error": f"Unknown event_types {unknown}. Valid: {PROSPECT_EVENT_TYPES}"})
    ts_from = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        client = ExploriumClient()
        resp = await client.fetch_prospects_events([prospect_id], et, timestamp_from=ts_from)
        return json.dumps((resp.get("output_events") or [])[:50], default=str)
    except Exception as exc:
        logger.warning("explorium_prospect_events failed: %s", exc)
        return json.dumps({"error": str(exc)})


# ── Dispatch + tool definitions (Anthropic tool schema, matching tools.py) ────
_HANDLERS = {
    "explorium_search_businesses": explorium_search_businesses,
    "explorium_business_statistics": explorium_business_statistics,
    "explorium_autocomplete": explorium_autocomplete,
    "explorium_match_business": explorium_match_business,
    "explorium_search_prospects": explorium_search_prospects,
    "explorium_match_prospect": explorium_match_prospect,
    "explorium_enrich_business": explorium_enrich_business,
    "explorium_business_events": explorium_business_events,
    "explorium_enrich_prospect": explorium_enrich_prospect,
    "explorium_prospect_events": explorium_prospect_events,
}

EXPLORIUM_TOOL_NAMES = frozenset(_HANDLERS)


def _array_params(name: str) -> list[str]:
    """Parameters this tool declares as arrays, read from its own schema."""
    for tool in EXPLORIUM_TOOL_DEFINITIONS:
        if tool["name"] != name:
            continue
        props = (tool.get("input_schema") or {}).get("properties") or {}
        return [k for k, p in props.items() if isinstance(p, dict) and p.get("type") == "array"]
    return []


async def execute_explorium_tool(name: str, inputs: dict) -> str:
    """Dispatch an explorium_* tool call. Returns a JSON string."""
    handler = _HANDLERS.get(name)
    if handler is None:
        return json.dumps({"error": f"Unknown explorium tool: {name}"})
    inputs = dict(inputs or {})
    # A model can send an array argument as a JSON string. Left alone, these
    # handlers iterate that string character by character — `event_types`
    # becomes a per-character validation error naming letters as event types.
    # Coerce from the tool's own schema so a new array parameter is covered
    # without a second edit here.
    from .tool_args import as_list

    for field in _array_params(name):
        # Empty means absent: these filters were previously skipped by a plain
        # falsiness check, so erroring on "" would reject calls that worked.
        if not inputs.get(field):
            continue
        items, error = as_list(inputs[field], field)
        if error:
            return json.dumps({"error": error})
        inputs[field] = items
    return await handler(**inputs)


EXPLORIUM_TOOL_DEFINITIONS = [
    {
        "name": "explorium_search_businesses",
        "description": (
            "Discover companies from the Explorium 100M+ database that match an ICP. "
            "PAID: the first ≤5 results are firmographics-enriched (1 credit each, "
            "cached 60 days; free on repeat); the rest are free preview rows. ALWAYS "
            "run explorium_business_statistics first to size availability for free. "
            "Filter shape: each key maps to {\"values\": [...]} or a range, e.g. "
            "{\"country_code\": {\"values\": [\"us\"]}, \"company_size\": {\"values\": "
            "[\"11-50\",\"51-200\"]}, \"linkedin_category\": {\"values\": [\"software "
            "development\"]}, \"website_keywords\": {\"values\": [\"sales automation\"]}}. "
            "Resolve dynamic exact-value fields (category/location/tech/intent topics) "
            "via explorium_autocomplete first; do NOT use the broken `topics` filter."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filters": {"type": "object", "description": "Explorium filter dict (see description for the {field: {values:[...]}} shape)."},
                "size": {"type": "integer", "description": "Max results to return (default 20)."},
                "page": {"type": "integer", "description": "Page number for pagination (default 1)."},
            },
            "required": ["filters"],
        },
    },
    {
        "name": "explorium_business_statistics",
        "description": (
            "FREE. Return aggregated counts (by industry, size, revenue, location, …) "
            "for a candidate business filter plan. Uses the SAME filter shape as "
            "explorium_search_businesses. Use this to test strict/balanced/broad filter "
            "plans and confirm enough availability BEFORE spending credits on a search."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filters": {"type": "object", "description": "Explorium filter dict to size."},
            },
            "required": ["filters"],
        },
    },
    {
        "name": "explorium_autocomplete",
        "description": (
            "FREE. Resolve valid exact values for a dynamic Explorium filter field "
            "(country, city_region_country, region_country_code, linkedin_category, "
            "google_category, naics_category, company_tech_stack_category/_tech, company "
            "name, intent topics). Use the returned `value` (not `label`) in filters. "
            "NOTE the autocomplete field name can differ from the filter key (e.g. "
            "autocomplete field 'country' → filter key 'country_code'). Do NOT "
            "autocomplete enumerated ranges/booleans (company_size, company_revenue, "
            "job_level, has_website, …) — use those directly. Pass semantic_search=true "
            "to discover Bombora intent topic strings."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "field": {"type": "string", "description": "The field to autocomplete (e.g. 'country', 'linkedin_category', 'company_tech_stack_tech')."},
                "query": {"type": "string", "description": "Partial text to match (e.g. 'soft' for 'software development')."},
                "semantic_search": {"type": "boolean", "description": "Set true for fuzzy/semantic matching (e.g. intent topics)."},
            },
            "required": ["field"],
        },
    },
    {
        "name": "explorium_match_business",
        "description": (
            "Resolve a known company to its Explorium business_id, given any of name / "
            "domain / linkedin_url. PAID (cached). Use this AFTER finding companies via "
            "google_search/web research when Explorium can't search the criterion "
            "directly — match the found companies, then enrich them. Best results: "
            "name + domain together."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Company name."},
                "domain": {"type": "string", "description": "Company domain or website URL."},
                "linkedin_url": {"type": "string", "description": "Company LinkedIn URL."},
            },
            "required": [],
        },
    },
    {
        "name": "explorium_search_prospects",
        "description": (
            "Find people (decision makers / employees) at a company in the Explorium "
            "database. Requires the company's business_id (from explorium_match_business "
            "or explorium_search_businesses). Filter by job title, department, country, "
            "or seniority. Returns lightweight rows (prospect_id, full_name, job_title); "
            "call explorium_enrich_prospect for full detail. Preview search is free."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "business_id": {"type": "string", "description": "Explorium business_id of the target company."},
                "job_titles": {"type": "array", "items": {"type": "string"}, "description": "Titles to filter on (e.g. ['CTO','VP Engineering'])."},
                "departments": {"type": "array", "items": {"type": "string"}, "description": "Departments (e.g. ['Engineering','Sales'])."},
                "countries": {"type": "array", "items": {"type": "string"}, "description": "Countries (e.g. ['US','UK'])."},
                "seniority_levels": {"type": "array", "items": {"type": "string"}, "description": "Seniority (e.g. ['C-Suite','VP','Director'])."},
                "size": {"type": "integer", "description": "Max results (default 20)."},
            },
            "required": ["business_id"],
        },
    },
    {
        "name": "explorium_match_prospect",
        "description": (
            "Resolve a known person to their Explorium prospect_id, given any of email / "
            "linkedin / full_name+company_name (optionally business_id). PAID (cached). "
            "Email is the strongest key. Use to get a prospect_id for enrichment when you "
            "already know who the person is."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "full_name": {"type": "string", "description": "Person's full name."},
                "company_name": {"type": "string", "description": "Their company name (pairs with full_name)."},
                "email": {"type": "string", "description": "Work email (strongest identifier)."},
                "linkedin": {"type": "string", "description": "Person LinkedIn URL."},
                "business_id": {"type": "string", "description": "Optional Explorium business_id to scope the match."},
            },
            "required": [],
        },
    },
    {
        "name": "explorium_enrich_business",
        "description": (
            "Fetch structured enrichment for ONE company from Explorium by business_id "
            "(firmographics, funding_and_acquisition, technographics, workforce_trends, "
            "company_ratings_by_employees, website_traffic, and more). PAID, cached 60 "
            "days. Some types need extra params: financial_indicators → 'date' "
            "(ISO start-of-quarter or YYYY-QN; auto-filled to current quarter); "
            "company_website_keywords → 'keywords' list (REQUIRED); website_traffic → "
            "'month_period' YYYY-MM (auto-filled)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "business_id": {"type": "string", "description": "Explorium business_id."},
                "enrichment_type": {"type": "string", "enum": list(BUSINESS_ENRICHMENT_URL_TYPES), "description": "Data category to retrieve."},
                "date": {"type": "string", "description": "financial_indicators only: ISO start-of-quarter (e.g. '2026-04-01') or YYYY-QN."},
                "keywords": {"type": "array", "items": {"type": "string"}, "description": "company_website_keywords only: keywords to look for (e.g. ['CRM','RevOps'])."},
                "month_period": {"type": "string", "description": "website_traffic only: month in YYYY-MM."},
            },
            "required": ["business_id", "enrichment_type"],
        },
    },
    {
        "name": "explorium_business_events",
        "description": (
            "Fetch recent company events from Explorium (hiring surges, new funding "
            "rounds, IPOs, M&A, new products/offices, layoffs, awards, …) by business_id. "
            "Ideal for buying signals. PAID, cached."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "business_id": {"type": "string", "description": "Explorium business_id."},
                "event_types": {"type": "array", "items": {"type": "string", "enum": list(BUSINESS_EVENT_TYPES)}, "description": "Event types to retrieve."},
                "days_back": {"type": "integer", "description": "Look-back window in days (default 90)."},
            },
            "required": ["business_id", "event_types"],
        },
    },
    {
        "name": "explorium_enrich_prospect",
        "description": (
            "Fetch detailed data for ONE person from Explorium by prospect_id: "
            "'profiles' (role, seniority, history), 'contacts_information' (email/phone), "
            "or 'linkedin_posts'. PAID, cached. Get the prospect_id from "
            "explorium_search_prospects or explorium_match_prospect first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "prospect_id": {"type": "string", "description": "Explorium prospect_id."},
                "enrichment_type": {"type": "string", "enum": list(PROSPECT_ENRICHMENT_URL_TYPES), "description": "Data category (default 'profiles')."},
            },
            "required": ["prospect_id"],
        },
    },
    {
        "name": "explorium_prospect_events",
        "description": (
            "Fetch recent events for a person by prospect_id (role change, company hop, "
            "job anniversary). Use to spot fresh engagement timing. PAID, cached."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "prospect_id": {"type": "string", "description": "Explorium prospect_id."},
                "event_types": {"type": "array", "items": {"type": "string", "enum": list(PROSPECT_EVENT_TYPES)}, "description": "Event types (defaults to all prospect event types)."},
                "days_back": {"type": "integer", "description": "Look-back window in days (default 90)."},
            },
            "required": ["prospect_id"],
        },
    },
]

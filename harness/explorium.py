import logging
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import httpx
import tenacity
from pymongo import UpdateOne
from pymongo.errors import DuplicateKeyError
from thefuzz import fuzz

# Reuse the shared async Mongo connector under scripts/lib (same pattern as
# harness/db_ops.py). The Explorium cache collections are plain caches, not
# workflow entities, so they connect through the driver directly rather than
# the db_* primitives.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from lib.db import get_db  # noqa: E402  (path set above)


class _LazyDB:
    """Defer the Mongo handle until a collection is actually accessed.

    All usage in this module is ``mongo_db[COLLECTION]``, so resolving the
    real db on first ``__getitem__`` keeps import side-effect free (no
    MONGO_URI required at import time) while behaving identically afterwards.
    """

    def __getitem__(self, name: str):
        return get_db()[name]


mongo_db = _LazyDB()

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# MongoDB cache collections
# ──────────────────────────────────────────────────────────────────────────────
COL_BUSINESSES = "explorium_businesses"  # keyed by (business_id, enrichment_type)
COL_PROSPECTS = "explorium_prospects"  # keyed by (prospect_id, enrichment_type)
COL_EVENTS = "explorium_events"  # business + prospect events (append-only)
COL_MATCH_BUSINESSES = (
    "explorium_match_businesses"  # keyed by norm_domain, norm_linkedin, or norm_name
)
COL_MATCH_PROSPECTS = (
    "explorium_match_prospects"  # keyed by norm_email, norm_linkedin, or norm_full_name
)

# Enrichment data is re-fetched from the API after this many days
CACHE_TTL_DAYS = 60

# Match results: cached forever. The mapping from a domain / linkedin /
# email / normalised name to its canonical Explorium business_id /
# prospect_id is effectively immutable (a company's domain doesn't
# usually change identity, and when it does we'd rather treat it as a
# new entity than silently re-bill on the old key). Kept as a constant
# only so historical references compile; the match-cache lookups no
# longer enforce it.
MATCH_CACHE_TTL_DAYS = 0

# Minimum fuzzy score (0-100) for a name to be considered matching.
# Mirrors Explorium's "smart fuzzy" threshold: handles typos, abbreviations,
# token reordering, and common legal-suffix noise.
FUZZY_NAME_THRESHOLD = 82

# Legal/structural suffixes stripped before name comparison
_LEGAL_SUFFIXES: Set[str] = {
    "inc",
    "incorporated",
    "llc",
    "ltd",
    "limited",
    "corp",
    "corporation",
    "co",
    "company",
    "plc",
    "gmbh",
    "ag",
    "sa",
    "sas",
    "bv",
    "nv",
    "lp",
    "llp",
    "pllc",
    "pc",
    "pty",
    "srl",
    "sl",
    "spa",
    "kk",
    "group",
    "holdings",
    "international",
    "intl",
    "global",
    "worldwide",
    "north",
    "america",
    "emea",
    "apac",
}

# ──────────────────────────────────────────────────────────────────────────────
# Enrichment category → URL path segment mapping
# (category names from MCP/credit catalog → URL path used in the REST API)
# ──────────────────────────────────────────────────────────────────────────────
BUSINESS_ENRICHMENT_URL_TYPES = [
    "firmographics",
    "financial_indicators",
    "funding_and_acquisition",
    "company_website_keywords",
    "technographics",
    "company_ratings_by_employees",
    "pc_business_challenges_10k",
    "pc_competitive_landscape_10k",
    "pc_strategy_10k",
    "workforce_trends",
    "linkedin_posts",
    "website_changes",
    "webstack",
    "company_hierarchies",
    "lookalikes",
    "website_traffic",
    "bombora_intent",
]

# Credit costs per enrichment category (enrich-prospects)
PROSPECT_ENRICHMENT_URL_TYPES = [
    "profiles",
    "contacts_information",
    "linkedin_posts",
]

BUSINESS_EVENT_TYPES = [
    "ipo_announcement",
    "new_funding_round",
    "new_investment",
    "new_product",
    "new_office",
    "closing_office",
    "new_partnership",
    "increase_in_engineering_department",
    "increase_in_sales_department",
    "increase_in_marketing_department",
    "increase_in_operations_department",
    "increase_in_customer_service_department",
    "increase_in_all_departments",
    "decrease_in_engineering_department",
    "decrease_in_sales_department",
    "decrease_in_marketing_department",
    "decrease_in_operations_department",
    "decrease_in_customer_service_department",
    "decrease_in_all_departments",
    "employee_joined_company",
    "hiring_in_creative_department",
    "hiring_in_education_department",
    "hiring_in_engineering_department",
    "hiring_in_finance_department",
    "hiring_in_health_department",
    "hiring_in_human_resources_department",
    "hiring_in_legal_department",
    "hiring_in_marketing_department",
    "hiring_in_operations_department",
    "hiring_in_professional_service_department",
    "hiring_in_sales_department",
    "hiring_in_support_department",
    "hiring_in_trade_department",
    "hiring_in_unknown_department",
    "company_award",
    "outages_and_security_breaches",
    "cost_cutting",
    "merger_and_acquisitions",
    "lawsuits_and_legal_issues",
]

PROSPECT_EVENT_TYPES = [
    "prospect_changed_role",
    "prospect_changed_company",
    "prospect_job_start_anniversary",
]

COMPANY_SIZE_RANGES = [
    "1-10",
    "11-50",
    "51-200",
    "201-500",
    "501-1000",
    "1001-5000",
    "5001-10000",
    "10001+",
]

COMPANY_REVENUE_RANGES = [
    "0-500K",
    "500K-1M",
    "1M-5M",
    "5M-10M",
    "10M-25M",
    "25M-75M",
    "75M-200M",
    "200M-500M",
    "500M-1B",
    "1B-10B",
    "10B-100B",
    "100B-1T",
    "1T-10T",
    "10T+",
]

COMPANY_AGE_RANGES = ["0-3", "3-6", "6-10", "10-20", "20+"]

NUMBER_OF_LOCATIONS_RANGES = [
    "0-1",
    "2-5",
    "6-20",
    "21-50",
    "51-100",
    "101-1000",
    "1001+",
]

JOB_LEVELS = [
    "owner",
    "c-suite",
    "vice president",
    "director",
    "senior non-managerial",
    "manager",
    "partner",
    "non-managerial",
    "junior",
    "president",
    "senior manager",
    "advisor",
    "freelancer",
    "board member",
    "founder",
    "training",
    "unpaid",
]

JOB_DEPARTMENTS = [
    "administration",
    "real estate",
    "healthcare",
    "partnerships",
    "c-suite",
    "design",
    "human resources",
    "engineering",
    "education",
    "strategy",
    "product",
    "sales",
    "r&d",
    "retail",
    "customer success",
    "security",
    "public service",
    "creative",
    "it",
    "support",
    "marketing",
    "trade",
    "legal",
    "operations",
    "procurement",
    "data",
    "manufacturing",
    "logistics",
    "finance",
]

BUSINESS_SEARCH_EVENT_TYPES = [*BUSINESS_EVENT_TYPES, "award"]

# Valid field values for the autocomplete GET endpoints
AUTOCOMPLETE_FIELDS = [
    "country",
    "country_code",
    "region_country_code",
    "google_category",
    "naics_category",
    "linkedin_category",
    "company_tech_stack_tech",
    "company_tech_stack_categories",
    "job_title",
    "company_size",
    "company_revenue",
    "number_of_locations",
    "company_age",
    "job_department",
    "job_level",
    "city_region_country",
    "company_name",
    "business_intent_topics",
]


# Retry decorator shared across all methods
_retry = tenacity.retry(
    stop=tenacity.stop_after_attempt(3),
    wait=tenacity.wait_exponential(multiplier=1, min=2, max=15),
    retry=tenacity.retry_if_exception_type(
        (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.NetworkError,
        )
    ),
    before_sleep=lambda retry_state: logger.info(
        f"Explorium API call failed, retrying in {retry_state.next_action.sleep}s …"
    ),
)


def _utcnow() -> datetime:
    """Return current UTC time as a timezone-naive datetime (what MongoDB expects)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _org_already_accessed(doc: Dict[str, Any], org_id: Optional[str]) -> bool:
    """Return True iff this org has previously paid for the cached doc.

    A missing ``org_id`` (e.g. ad-hoc internal callers without an active
    account context) collapses to "treat as already-accessed", which keeps
    the historical "free on cache hit" behaviour intact for those callers
    instead of silently re-billing them. New per-org bookkeeping only
    kicks in when the caller actually identifies an org.
    """
    if not org_id:
        return True
    accessed = doc.get("accessed_by_orgs") or []
    return org_id in accessed


def _accessed_by_orgs_seed(org_id: Optional[str]) -> List[str]:
    """Initial value for ``accessed_by_orgs`` on a fresh insert.

    When the caller has no org_id we leave the list empty rather than
    poisoning it with sentinel values; the next caller that does pass
    org_id will be billed correctly because absence != presence.
    """
    return [org_id] if org_id else []


def _parse_event_time(time_str: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 event timestamp string into a UTC-naive datetime.

    MongoDB / motor stores all BSON dates as UTC internally.  Storing and
    querying with timezone-naive UTC datetimes avoids comparison mismatches
    that can occur when mixing timezone-aware and timezone-naive objects
    across different pymongo/motor versions.
    """
    if not time_str:
        return None
    try:
        dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, AttributeError):
        return None


def _normalize_domain(domain: str) -> str:
    """Normalise a domain/URL to a bare host for cache keying.

    Examples::
        "https://www.Starbucks.com/about" → "starbucks.com"
        "starbucks.com"                   → "starbucks.com"
    """
    d = domain.lower().strip()
    d = re.sub(r"^https?://", "", d)
    d = re.sub(r"^www\.", "", d)
    d = d.split("/")[0]
    d = d.split("?")[0]
    d = d.split("#")[0]
    return d.strip()


def _normalize_linkedin_url(url: str) -> str:
    """Canonicalise a company LinkedIn URL for cache keying.

    Strips protocol, ``www.``, regional sub-domains (``in.``, ``uk.``,
    etc. — LinkedIn serves the same company under several locales),
    query/fragment noise, and the trailing slash, so equivalent inputs
    collapse to one cache key.

    Examples::
        "https://www.linkedin.com/company/Clodexa/"        → "linkedin.com/company/clodexa"
        "https://in.linkedin.com/company/clodexa?foo=1"    → "linkedin.com/company/clodexa"
        "https://www.in.linkedin.com/company/clodexa/"     → "linkedin.com/company/clodexa"
        "linkedin.com/company/clodexa"                     → "linkedin.com/company/clodexa"
    """
    u = url.lower().strip()
    u = re.sub(r"^https?://", "", u)
    # Strip ``www.`` BEFORE the regional sub-domain rule so combined
    # prefixes like ``www.in.linkedin.`` collapse to ``linkedin.``;
    # otherwise the regional regex misses (it anchors at the start) and
    # we'd cache ``in.linkedin.com/...`` separately from the canonical
    # ``linkedin.com/...`` for the same company.
    u = re.sub(r"^www\.", "", u)
    u = re.sub(r"^[a-z]{2,3}\.linkedin\.", "linkedin.", u)
    u = u.split("?")[0]
    u = u.split("#")[0]
    u = u.rstrip("/")
    return u.strip()


def _normalize_name(name: str) -> str:
    """Normalise a business name for fuzzy comparison.

    Steps:
    1. NFKD → ASCII transliteration (handles accented chars)
    2. Lowercase & remove punctuation
    3. Strip common legal / structural suffixes
    """
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    n = n.lower()
    n = re.sub(r"[^\w\s]", " ", n)
    tokens = n.split()
    filtered = [t for t in tokens if t not in _LEGAL_SUFFIXES]
    return " ".join(filtered or tokens)


def _names_fuzzy_score(norm_a: str, norm_b: str) -> int:
    """Return the best fuzzy score (0-100) between two *already-normalised* names.

    Takes the max of:
    - token_sort_ratio: handles word reordering ("Microsoft Corp" vs "Corp Microsoft")
    - token_set_ratio:  handles subset/superset, but only when both names have the
      same token count to avoid false positives like "apple" matching "apple bank".
    """
    score = fuzz.token_sort_ratio(norm_a, norm_b)
    # Only apply token_set_ratio when token counts match; avoids subset false positives
    # (e.g. "apple" ⊂ "apple bank" would otherwise score 100).
    if len(norm_a.split()) == len(norm_b.split()):
        score = max(score, fuzz.token_set_ratio(norm_a, norm_b))
    return score


class ExploriumClient:
    """Async client for the Explorium AgentSource REST API with MongoDB cache.

    Enrichments (businesses & prospects) are cached per (entity_id × enrichment_type)
    and refreshed after CACHE_TTL_DAYS days.

    Events are stored once per event_id (append-only).  After a
    (entity_id × event_type) pair has been fetched from the API, all subsequent
    requests for that pair are served from MongoDB filtered by the requested
    time range – the API is never called again for the same pair.

    Usage::

        async with ExploriumClient() as client:
            businesses = await client.fetch_businesses(size=5, filters={...})
            events = await client.fetch_businesses_events(
                business_ids=["id1"],
                event_types=["new_funding_round"],
            )
    """

    BASE_URL = "https://api.explorium.ai"
    _indexes_ensured: bool = False

    def __init__(self, api_key: Optional[str] = None, timeout: float = 120.0):
        self.api_key = api_key or os.environ.get("AGENTSOURCE_API_KEY")
        if not self.api_key:
            raise ValueError(
                "Explorium API key is required. "
                "Set AGENTSOURCE_API_KEY env variable or pass api_key."
            )
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    # ── context manager ────────────────────────────────────────────────────────

    async def __aenter__(self) -> "ExploriumClient":
        self._client = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client:
            await self._client.aclose()
            self._client = None

    # ── HTTP helpers ───────────────────────────────────────────────────────────

    @property
    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "api_key": self.api_key,
        }

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Send a POST request and return the parsed JSON response."""
        client = self._ensure_client()
        url = f"{self.BASE_URL}{path}"
        clean_payload = {k: v for k, v in payload.items() if v is not None}
        logger.debug("POST %s  payload_keys=%s", url, list(clean_payload.keys()))
        response = await client.post(url, json=clean_payload, headers=self._headers)
        if response.status_code != 200:
            logger.error(
                "Explorium API error: status=%s url=%s body=%s",
                response.status_code,
                url,
                response.text[:500],
            )
            raise Exception(
                f"Explorium API error: status={response.status_code} url={url} body={response.text[:500]}"
            )
        return response.json()

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """Send a GET request and return the parsed JSON response."""
        client = self._ensure_client()
        url = f"{self.BASE_URL}{path}"
        clean_params = (
            {k: v for k, v in params.items() if v is not None} if params else {}
        )
        logger.debug("GET %s  params=%s", url, clean_params)
        response = await client.get(url, params=clean_params, headers=self._headers)
        if response.status_code != 200:
            logger.error(
                "Explorium API error: status=%s url=%s body=%s",
                response.status_code,
                url,
                response.text[:500],
            )
            raise Exception(
                f"Explorium API error: status={response.status_code} url={url} body={response.text[:500]}"
            )
        return response.json()

    # ── MongoDB index setup ────────────────────────────────────────────────────

    async def _ensure_indexes(self) -> None:
        """Create MongoDB indexes for cache collections (idempotent, runs once per process)."""
        if ExploriumClient._indexes_ensured:
            return
        col_biz = mongo_db[COL_BUSINESSES]
        col_pro = mongo_db[COL_PROSPECTS]
        col_evt = mongo_db[COL_EVENTS]
        col_match = mongo_db[COL_MATCH_BUSINESSES]
        col_match_pro = mongo_db[COL_MATCH_PROSPECTS]

        # explorium_businesses: unique per (business_id, enrichment_type)
        await col_biz.create_index(
            [("business_id", 1), ("enrichment_type", 1)], unique=True, background=True
        )
        # Multikey index on accessed_by_orgs powers the per-org "have I
        # paid for this row before?" lookup in O(log n). Without it, the
        # cache-hit-but-new-for-org check would fall back to a collection
        # scan for every billable enrichment call.
        await col_biz.create_index("accessed_by_orgs", background=True)
        # explorium_prospects: unique per (prospect_id, enrichment_type)
        await col_pro.create_index(
            [("prospect_id", 1), ("enrichment_type", 1)], unique=True, background=True
        )
        await col_pro.create_index("accessed_by_orgs", background=True)
        # explorium_events: unique per event_id (sparse so meta markers are excluded)
        await col_evt.create_index(
            "event_id", unique=True, sparse=True, background=True
        )
        await col_evt.create_index(
            [("business_id", 1), ("event_type", 1)], background=True
        )
        await col_evt.create_index(
            [("prospect_id", 1), ("event_type", 1)], background=True
        )
        await col_evt.create_index("event_time", background=True)
        # Per-org access tracking lives on the meta marker documents
        # (one per (entity_id, event_type) pair), not on individual events,
        # because that's the natural billing unit.
        await col_evt.create_index("accessed_by_orgs", background=True)

        # explorium_match_businesses:
        #   - unique per norm_domain (domain-keyed entries)
        #   - unique per norm_linkedin (linkedin-keyed entries with no domain)
        #   - index on norm_name for fast name-only fuzzy pre-filtering
        #   - index on business_id for fast enrichment-backfill lookups
        #   - TTL index to auto-expire old entries
        await col_match.create_index(
            "norm_domain",
            unique=True,
            sparse=True,  # linkedin- and name-only docs have no norm_domain
            background=True,
        )
        await col_match.create_index(
            "norm_linkedin",
            unique=True,
            sparse=True,
            background=True,
        )
        await col_match.create_index("norm_name", background=True)
        await col_match.create_index("business_id", sparse=True, background=True)
        await col_match.create_index("cached_at", background=True)
        await col_match.create_index("accessed_by_orgs", background=True)

        # explorium_match_prospects:
        #   - unique per norm_email (email-keyed entries, strongest anchor)
        #   - unique per norm_linkedin (linkedin-keyed entries, no email)
        #   - index on norm_full_name for name+company fuzzy pre-filtering
        #   - TTL index to auto-expire old entries
        await col_match_pro.create_index(
            "norm_email",
            unique=True,
            sparse=True,
            background=True,
        )
        await col_match_pro.create_index(
            "norm_linkedin",
            unique=True,
            sparse=True,
            background=True,
        )
        await col_match_pro.create_index("norm_full_name", background=True)
        await col_match_pro.create_index("cached_at", background=True)
        await col_match_pro.create_index("accessed_by_orgs", background=True)

        ExploriumClient._indexes_ensured = True

    # ──────────────────────────────────────────────────────────────────────────
    # Fetch Businesses  (FREE – not cached)
    # ──────────────────────────────────────────────────────────────────────────

    @_retry
    async def fetch_businesses(
        self,
        *,
        mode: str = "preview",
        size: int = 500,
        page_size: int = 500,
        page: int = 1,
        filters: Optional[Dict[str, Any]] = None,
        exclude: Optional[List[str]] = None,
        next_cursor: Optional[str] = None,
        request_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Fetch businesses matching filter criteria (FREE).

        POST /v1/businesses

        Args:
            mode: "preview" (all fields) or "preview" (minimal fields).
            size: Total max records across all pages (≤ 60 000).
            page_size: Records per page (≤ 500).
            page: 1-based page number.
            filters: Filter dict – keys like ``country_code``, ``company_size``,
                ``linkedin_category``, ``google_category``, ``naics_category``,
                ``company_revenue``, ``company_tech_stack_tech``,
                ``website_keywords``, ``topics``, ``events``, etc.
                Each filter value is ``{"values": [...]}`` (OR within, AND across).
            exclude: Business IDs to exclude (≤ 1 000).
            next_cursor: Cursor string for cursor-based pagination (overrides *page*).
            request_context: Optional request metadata.

        Returns:
            Dict with ``data`` (list of business objects), ``total_results``,
            ``total_pages``, ``page``, and ``response_context``.
        """
        payload: Dict[str, Any] = {
            "mode": mode,
            "size": size,
            "page_size": page_size,
            "page": page,
            "filters": filters or {},
            "exclude": exclude,
            "next_cursor": next_cursor,
            "request_context": request_context,
        }
        return await self._post("/v1/businesses", payload)

    # ──────────────────────────────────────────────────────────────────────────
    # Business Statistics  (FREE – not cached)
    # ──────────────────────────────────────────────────────────────────────────

    @_retry
    async def fetch_businesses_statistics(
        self,
        filters: Optional[Dict[str, Any]] = None,
        *,
        request_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Aggregated business statistics by industry, revenue, size, location (FREE).

        POST /v1/businesses/stats

        Args:
            filters: Same filter dict as ``fetch_businesses`` – e.g.
                ``country_code``, ``company_size``, ``linkedin_category``, etc.
                Only one category filter per request (google, linkedin, or naics).
            request_context: Optional request metadata.

        Returns:
            Dict with ``total_results``, ``stats`` (containing
            ``business_categories_per_location``, ``revenue_per_category``,
            ``number_of_employees_per_category``), and ``response_context``.
        """
        payload: Dict[str, Any] = {
            "filters": filters or {},
            "request_context": request_context,
        }
        return await self._post("/v1/businesses/stats", payload)

    # ──────────────────────────────────────────────────────────────────────────
    # Fetch Prospects  (FREE – not cached)
    # ──────────────────────────────────────────────────────────────────────────

    @_retry
    async def fetch_prospects(
        self,
        *,
        mode: str = "preview",
        size: int = 100,
        page_size: int = 100,
        page: int = 1,
        filters: Optional[Dict[str, Any]] = None,
        exclude: Optional[List[str]] = None,
        next_cursor: Optional[str] = None,
        request_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Fetch prospect records matching filter criteria (FREE).

        POST /v1/prospects

        Args:
            mode: "preview" returns complete prospect data.
            size: Total max records across all pages (≤ 60 000).
            page_size: Records per page (≤ 100).
            page: 1-based page number.
            filters: Filter dict – keys like ``business_id``, ``job_level``,
                ``job_department``, ``job_title``, ``has_email``,
                ``has_phone_number``, ``country_code``,
                ``region_country_code``, ``company_size``,
                ``company_revenue``, ``linkedin_category``, etc.
                Multi-value filters use ``{"values": [...]}``, single-value
                use ``{"value": ...}``, range use ``{"gte": ..., "lte": ...}``.
            exclude: Prospect IDs to exclude.
            next_cursor: Cursor string for cursor-based pagination.
            request_context: Optional request metadata.

        Returns:
            Dict with ``data`` (list of prospect objects), ``total_results``,
            ``total_pages``, ``page``, and ``response_context``.
        """
        payload: Dict[str, Any] = {
            "mode": mode,
            "size": size,
            "page_size": page_size,
            "page": page,
            "filters": filters or {},
            "exclude": exclude,
            "next_cursor": next_cursor,
            "request_context": request_context,
        }
        return await self._post("/v1/prospects", payload)

    # ──────────────────────────────────────────────────────────────────────────
    # Prospects Statistics  (FREE – not cached)
    # ──────────────────────────────────────────────────────────────────────────

    @_retry
    async def fetch_prospects_statistics(
        self,
        filters: Optional[Dict[str, Any]] = None,
        *,
        request_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Aggregated prospect statistics by department, location, etc. (FREE).

        POST /v1/prospects/stats

        Args:
            filters: Same filter dict as ``fetch_prospects`` – e.g.
                ``job_department``, ``job_level``, ``country_code``,
                ``region_country_code``, ``company_size``, ``business_id``, etc.
            request_context: Optional request metadata.

        Returns:
            Dict with ``total_results``, ``stats`` (containing
            ``job_departments_per_location``, ``total_per_location``),
            and ``response_context``. Response structure adapts dynamically
            based on which filters are used.
        """
        payload: Dict[str, Any] = {
            "filters": filters or {},
            "request_context": request_context,
        }
        return await self._post("/v1/prospects/stats", payload)

    # ──────────────────────────────────────────────────────────────────────────
    # Search Businesses / Prospects  (preview for all + cached full mode for first 5)
    # ──────────────────────────────────────────────────────────────────────────

    async def search_businesses(
        self,
        *,
        size: int = 100,
        page_size: int = 100,
        page: int = 1,
        filters: Optional[Dict[str, Any]] = None,
        exclude: Optional[List[str]] = None,
        next_cursor: Optional[str] = None,
        request_context: Optional[Dict[str, Any]] = None,
        org_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Search businesses: preview mode for all results, firmographics enrichment for first min(5, page_size).

        Firmographic data is fetched via ``bulk_enrich_businesses`` (``enrichment_type='firmographics'``),
        which handles its own MongoDB caching keyed by ``business_id``.  This means pagination is
        safe — enrichment is always looked up by ID, never by page offset.

        Returns the standard fetch_businesses response augmented with:
          - ``preview_note``: human-readable description of how many results are enriched.
          - ``full_mode_count``: number of items enriched with firmographics.
          - ``credits_used``: credits deducted for this call (true API misses
            + cache hits new for this org, both at 1 credit each).
        """
        # 1. Fetch all results in preview mode (FREE)
        preview_response = await self.fetch_businesses(
            mode="preview",
            size=size,
            page_size=page_size,
            page=page,
            filters=filters,
            exclude=exclude,
            next_cursor=next_cursor,
            request_context=request_context,
        )

        preview_data: List[Dict[str, Any]] = preview_response.get("data", [])
        if not preview_data:
            preview_response["preview_note"] = "No results found."
            preview_response["full_mode_count"] = 0
            preview_response["credits_used"] = 0
            return preview_response

        # 2. Determine which IDs to enrich with firmographics (at most 5)
        full_mode_count = min(5, page_size, len(preview_data))
        full_mode_ids = [
            item.get("business_id")
            for item in preview_data[:full_mode_count]
            if item.get("business_id")
        ]

        if not full_mode_ids:
            preview_response["preview_note"] = (
                f"Results are in preview mode. First {full_mode_count} results "
                "could not be enriched (no business_id)."
            )
            preview_response["full_mode_count"] = 0
            preview_response["credits_used"] = 0
            return preview_response

        # 3. Fetch firmographics for the first ≤5 IDs (bulk_enrich handles its own cache)
        firm_response = await self.bulk_enrich_businesses(
            full_mode_ids,
            "firmographics",
            request_context=request_context,
            org_id=org_id,
        )
        firm_data_map: Dict[str, Any] = {
            record["business_id"]: record.get("data", {})
            for record in firm_response.get("data", [])
            if record.get("business_id")
        }
        resp_ctx = firm_response.get("response_context", {})
        cached_count = resp_ctx.get("cached_count", 0)
        api_count = resp_ctx.get("api_count", 0)
        cache_charge_count = resp_ctx.get("cache_charge_count", 0)
        cache_free_count = resp_ctx.get("cache_free_count", 0)
        credits_used = api_count + cache_charge_count

        # 4. Merge firmographic data into preview results (keep preview for unmatched)
        merged_data: List[Dict[str, Any]] = []
        for i, item in enumerate(preview_data):
            bid = item.get("business_id")
            if i < full_mode_count and bid and bid in firm_data_map:
                merged_data.append({**item, **firm_data_map[bid], "business_id": bid})
            else:
                merged_data.append(item)

        preview_response["data"] = merged_data
        preview_response["full_mode_count"] = full_mode_count
        preview_response["credits_used"] = credits_used
        preview_response["preview_note"] = (
            f"First {full_mode_count} result(s) enriched with firmographics "
            f"({cache_free_count} free for this org, "
            f"{cache_charge_count} cache hits new for this org, "
            f"{api_count} freshly fetched — {credits_used} credit(s) used). "
            f"Remaining {len(preview_data) - full_mode_count} result(s) are in PREVIEW mode. "
            "If the enriched results look correct, proceed with add_businesses and enrichment."
        )
        return preview_response

    async def search_prospects(
        self,
        *,
        size: int = 100,
        page_size: int = 100,
        page: int = 1,
        filters: Optional[Dict[str, Any]] = None,
        exclude: Optional[List[str]] = None,
        next_cursor: Optional[str] = None,
        request_context: Optional[Dict[str, Any]] = None,
        org_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Search prospects: preview mode for all results, full mode for first min(5, page_size).

        Full-mode results are cached in ``explorium_prospects`` with
        ``enrichment_type='full_search'`` and refreshed after ``CACHE_TTL_DAYS``.

        Returns the standard fetch_prospects response augmented with:
          - ``preview_note``: human-readable description of how many results are fully shown.
          - ``full_mode_count``: number of items returned in full mode.
          - ``credits_used``: credits deducted for this call (cache misses only, 1 each).
        """
        # 1. Fetch all results in preview mode (FREE)
        preview_response = await self.fetch_prospects(
            mode="preview",
            size=size,
            page_size=page_size,
            page=page,
            filters=filters,
            exclude=exclude,
            next_cursor=next_cursor,
            request_context=request_context,
        )

        preview_data: List[Dict[str, Any]] = preview_response.get("data", [])
        if not preview_data:
            preview_response["preview_note"] = "No results found."
            preview_response["full_mode_count"] = 0
            preview_response["credits_used"] = 0
            return preview_response

        return {
            "data": [
                {
                    "prospect_id": prospect.get("prospect_id"),
                    "full_name": prospect.get("full_name"),
                    "job_title": prospect.get("job_title"),
                    "company_name": prospect.get("company_name"),
                }
                for prospect in preview_data
            ],
            "total_results": len(preview_data),
            "total_pages": 1,
            "page": 1,
            "credits_used": 0,
        }

    # ──────────────────────────────────────────────────────────────────────────
    # Autocomplete  (FREE – not cached)
    # ──────────────────────────────────────────────────────────────────────────

    @_retry
    async def autocomplete(
        self,
        field: str,
        query: str = "",
        *,
        semantic_search: bool = False,
    ) -> List[Dict[str, str]]:
        """Real-time autocomplete suggestions for filter fields (FREE).

        GET /v1/businesses/autocomplete

        The businesses autocomplete endpoint covers the same field set used
        by both business and prospect filters, so a single method suffices.

        Args:
            field: One of the values in :data:`AUTOCOMPLETE_FIELDS`, e.g.
                ``"country"``, ``"linkedin_category"``, ``"job_department"``,
                ``"job_level"``, ``"company_tech_stack_tech"``,
                ``"business_intent_topics"``, etc.
            query: Partial text to autocomplete (e.g. ``"soft"``).
            semantic_search: When *True* and *field* is
                ``"business_intent_topics"``, returns broader semantic
                suggestions. Ignored for other fields.

        Returns:
            List of dicts, each with ``query``, ``label``, and ``value`` keys.
        """
        if field not in AUTOCOMPLETE_FIELDS:
            raise ValueError(
                f"Unknown autocomplete field '{field}'. "
                f"Valid fields: {AUTOCOMPLETE_FIELDS}"
            )
        params: Dict[str, Any] = {
            "field": field,
            "query": query,
        }
        if semantic_search:
            params["semantic_search"] = "true"
        return await self._get("/v1/businesses/autocomplete", params)

    # ──────────────────────────────────────────────────────────────────────────
    # Match Businesses – raw API call (private, retry-wrapped)
    # ──────────────────────────────────────────────────────────────────────────

    @_retry
    async def _api_match_businesses(
        self,
        businesses_to_match: List[Dict[str, Optional[str]]],
        *,
        request_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Direct API call for business matching (no cache)."""
        payload: Dict[str, Any] = {
            "businesses_to_match": businesses_to_match,
            "request_context": request_context,
        }
        return await self._post("/v1/businesses/match", payload)

    # ──────────────────────────────────────────────────────────────────────────
    # Match Businesses – cache helpers (private)
    # ──────────────────────────────────────────────────────────────────────────

    async def _find_cached_matches(
        self,
        businesses: List[Dict[str, Optional[str]]],
        col,
        ttl_cutoff: datetime,
        org_id: Optional[str] = None,
    ) -> Dict[int, Tuple[Optional[str], bool, Optional[Any]]]:
        """Look up the match cache for a batch of business inputs.

        Each input is checked against every identifier we have for it,
        in priority order — first hit wins:

        1. ``domain`` / ``url`` → exact match on ``norm_domain``
        2. ``linkedin_url``      → exact match on ``norm_linkedin``
        3. ``name``              → fuzzy match on ``norm_name``
           (``token_sort`` + ``token_set``, threshold
           ``FUZZY_NAME_THRESHOLD``)

        An input may carry several of these at once (e.g. name + domain
        + linkedin_url). The *strongest* identifier we can resolve wins;
        weaker ones are tried only when the stronger lookups missed.

        Returns:
            Mapping of *input list index* → ``(business_id, needs_charging, doc_id)``.
            A present entry always means cache_hit=True (caller infers from
            the dict membership); ``business_id`` may still be ``None`` when
            we cached a "no-match" result. ``needs_charging=True`` means
            this org has never paid for this row before — caller bills 1
            credit and stamps the org via ``doc_id``.
        """
        results: Dict[int, Tuple[Optional[str], bool, Optional[Any]]] = {}

        # Partition inputs by which identifier we can use to look them up.
        # An input with multiple identifiers is registered under each;
        # whichever bucket resolves first wins.
        domain_to_indices: Dict[str, List[int]] = {}
        linkedin_to_indices: Dict[str, List[int]] = {}
        name_to_indices: List[int] = []

        for i, biz in enumerate(businesses):
            raw_domain = biz.get("domain") or biz.get("url")
            raw_linkedin = biz.get("linkedin_url")
            raw_name = biz.get("name")
            if raw_domain:
                domain_to_indices.setdefault(_normalize_domain(raw_domain), []).append(
                    i
                )
            if raw_linkedin:
                linkedin_to_indices.setdefault(
                    _normalize_linkedin_url(raw_linkedin), []
                ).append(i)
            if raw_name:
                name_to_indices.append(i)

        # ── 1. Domain-keyed lookup (strongest identifier) ────────────────────
        # Match-cache lookups are no longer TTL-gated: the mapping from
        # an identifier to a canonical business_id doesn't drift, so
        # ``ttl_cutoff`` (kept in the signature for compatibility) is
        # ignored.
        if domain_to_indices:
            async for doc in col.find(
                {
                    "norm_domain": {"$in": list(domain_to_indices.keys())},
                }
            ):
                nd = doc["norm_domain"]
                needs_charging = not _org_already_accessed(doc, org_id)
                for idx in domain_to_indices.get(nd, []):
                    results.setdefault(
                        idx,
                        (doc.get("business_id"), needs_charging, doc.get("_id")),
                    )

        # ── 2. LinkedIn-keyed lookup (only for inputs not already resolved) ──
        pending_linkedin = {
            nl: [i for i in idxs if i not in results]
            for nl, idxs in linkedin_to_indices.items()
        }
        pending_linkedin = {nl: idxs for nl, idxs in pending_linkedin.items() if idxs}
        if pending_linkedin:
            async for doc in col.find(
                {
                    "norm_linkedin": {"$in": list(pending_linkedin.keys())},
                }
            ):
                nl = doc["norm_linkedin"]
                needs_charging = not _org_already_accessed(doc, org_id)
                for idx in pending_linkedin.get(nl, []):
                    results.setdefault(
                        idx,
                        (doc.get("business_id"), needs_charging, doc.get("_id")),
                    )

        # ── 3. Name-only fuzzy lookup (weakest, last resort) ──────────────────
        unresolved_name_indices = [i for i in name_to_indices if i not in results]
        if unresolved_name_indices:
            name_cache: List[Dict[str, Any]] = []
            async for doc in col.find(
                {
                    "norm_name": {"$exists": True},
                }
            ):
                name_cache.append(doc)

            for i in unresolved_name_indices:
                raw_name = businesses[i].get("name", "")
                if not raw_name:
                    continue
                query_norm = _normalize_name(raw_name)
                best_doc = None
                best_score = -1
                for doc in name_cache:
                    cached_norm = doc.get("norm_name", "")
                    if not cached_norm:
                        continue
                    score = _names_fuzzy_score(query_norm, cached_norm)
                    if score > best_score and score >= FUZZY_NAME_THRESHOLD:
                        best_score = score
                        best_doc = doc
                if best_doc is not None:
                    results[i] = (
                        best_doc.get("business_id"),
                        not _org_already_accessed(best_doc, org_id),
                        best_doc.get("_id"),
                    )

        return results

    async def _merge_match_doc(
        self,
        col,
        *,
        business_id: Optional[str],
        nd: Optional[str],
        nl: Optional[str],
        nn: Optional[str],
        raw_input: Dict[str, Any],
        now: datetime,
        org_id: Optional[str] = None,
        _attempt: int = 0,
    ) -> None:
        """Idempotent upsert that maintains the cache invariant:
        *one* document per business, carrying the union of every
        identifier we have ever seen for it.

        How it works:

        1. Locate any existing doc whose ``business_id`` /
           ``norm_domain`` / ``norm_linkedin`` matches one of the new
           identifiers (name is too ambiguous to use as a merge key —
           it's stored for fuzzy *reads* only).
        2. Pick a deterministic survivor (lowest ``_id``) so concurrent
           writers always agree on which doc lives. Delete the rest
           first so the survivor's ``$set`` can claim their unique-sparse
           index slots without colliding.
        3. ``$set`` the union of all known identifiers on the survivor.
           ``input`` is *only* written on insert, so user-supplied
           provenance (CRM IDs, user IDs, etc.) is never clobbered by
           a backfill or a later match call.
        4. ``DuplicateKeyError`` (a racing writer claimed a unique-sparse
           field between our find and our update) triggers a bounded
           retry that re-runs the merge and discovers the new doc.

        Called by both ``_save_match_cache`` (user input → match) and
        ``_backfill_match_from_enrichment`` (firmographics → match), so
        there is exactly one merge code path to reason about.
        """
        MAX_RETRIES = 3
        if _attempt >= MAX_RETRIES:
            logger.warning(
                "match cache merge gave up after %d retries (biz_id=%s, "
                "nd=%s, nl=%s)",
                MAX_RETRIES,
                business_id,
                nd,
                nl,
            )
            return

        if not (business_id or nd or nl or nn):
            return  # nothing usable to cache

        # Locate every doc that could already represent this business.
        # Name alone is *not* used as a merge key: two different
        # companies can share a fuzzy-equal name, and merging them
        # would wreck the cache. Name is only stored for fuzzy reads.
        or_clauses: List[Dict[str, Any]] = []
        if business_id:
            or_clauses.append({"business_id": business_id})
        if nd:
            or_clauses.append({"norm_domain": nd})
        if nl:
            or_clauses.append({"norm_linkedin": nl})

        candidates: List[Dict[str, Any]] = []
        if or_clauses:
            async for doc in col.find({"$or": or_clauses}):
                candidates.append(doc)

        # Union of identifiers across new input + all candidate docs.
        # New-input values win for normalised forms (we just normalised
        # them, the cached ones might be from older normaliser versions),
        # but only when present.
        union_bid = business_id
        union_nd = nd
        union_nl = nl
        union_nn = nn
        for doc in candidates:
            union_bid = union_bid or doc.get("business_id")
            union_nd = union_nd or doc.get("norm_domain")
            union_nl = union_nl or doc.get("norm_linkedin")
            union_nn = union_nn or doc.get("norm_name")

        set_payload: Dict[str, Any] = {"cached_at": now}
        if union_bid is not None:
            set_payload["business_id"] = union_bid
        if union_nd:
            set_payload["norm_domain"] = union_nd
        if union_nl:
            set_payload["norm_linkedin"] = union_nl
        if union_nn:
            set_payload["norm_name"] = union_nn

        if not candidates:
            insert_doc: Dict[str, Any] = {**set_payload, "input": raw_input}
            if org_id:
                # Fresh insert by *this* org owns the seed so the next
                # call from the same org is a paid-cache-hit, not a
                # duplicate charge.
                insert_doc["accessed_by_orgs"] = [org_id]
            try:
                await col.insert_one(insert_doc)
            except DuplicateKeyError:
                # Another writer beat us to insert. Re-run so we find
                # their doc and merge into it instead.
                await self._merge_match_doc(
                    col,
                    business_id=business_id,
                    nd=nd,
                    nl=nl,
                    nn=nn,
                    raw_input=raw_input,
                    now=now,
                    org_id=org_id,
                    _attempt=_attempt + 1,
                )
            return

        # Deterministic survivor: lowest _id. ObjectId ordering is
        # globally stable across all callers, unlike cached_at (which
        # can differ between concurrent invocations under clock skew).
        candidates.sort(key=lambda d: d["_id"])
        survivor = candidates[0]
        duplicates = candidates[1:]

        # Union accessed_by_orgs across the candidates we're collapsing,
        # so a sub-account that previously paid via a sibling key isn't
        # silently re-billed when the docs merge.
        survivor_accessed: Set[str] = set(survivor.get("accessed_by_orgs") or [])
        for d in duplicates:
            survivor_accessed.update(d.get("accessed_by_orgs") or [])
        if org_id:
            survivor_accessed.add(org_id)

        if duplicates:
            await col.delete_many({"_id": {"$in": [d["_id"] for d in duplicates]}})

        update_doc: Dict[str, Any] = {"$set": set_payload}
        if survivor_accessed:
            # Replace the field outright (rather than $addToSet) because
            # we already merged the duplicates' lists in-memory above.
            update_doc["$set"]["accessed_by_orgs"] = sorted(survivor_accessed)
        try:
            await col.update_one({"_id": survivor["_id"]}, update_doc)
        except DuplicateKeyError:
            # A parallel writer inserted a fresh doc claiming one of
            # our unique-sparse fields after we read candidates. Retry
            # the whole merge to discover and absorb it.
            await self._merge_match_doc(
                col,
                business_id=business_id,
                nd=nd,
                nl=nl,
                nn=nn,
                raw_input=raw_input,
                now=now,
                org_id=org_id,
                _attempt=_attempt + 1,
            )

    async def _save_match_cache(
        self,
        businesses: List[Dict[str, Optional[str]]],
        matched: List[Dict[str, Any]],
        col,
        now: datetime,
        org_id: Optional[str] = None,
    ) -> None:
        """Persist match results into the cache, one doc per business.

        Delegates to :meth:`_merge_match_doc` per input so that the cache
        invariant (one doc per business, union of all known identifiers)
        is maintained even when the same business appears twice in one
        batch with different identifier shapes, or has been previously
        cached by a sibling code path under a different identifier.

        Inputs with no usable identifiers are skipped (Explorium would
        have rejected them too).
        """
        for i, biz in enumerate(businesses):
            if i >= len(matched):
                break
            business_id = matched[i].get("business_id")
            raw_domain = biz.get("domain") or biz.get("url")
            raw_linkedin = biz.get("linkedin_url")
            raw_name = biz.get("name", "")

            nd = _normalize_domain(raw_domain) if raw_domain else None
            nl = _normalize_linkedin_url(raw_linkedin) if raw_linkedin else None
            nn = _normalize_name(raw_name) if raw_name else None

            # Empty-after-normalisation values would coalesce unrelated
            # docs onto the same key — drop them.
            nd = nd or None
            nl = nl or None
            nn = nn or None

            await self._merge_match_doc(
                col,
                business_id=business_id,
                nd=nd,
                nl=nl,
                nn=nn,
                raw_input=biz,
                now=now,
                org_id=org_id,
            )

    # ──────────────────────────────────────────────────────────────────────────
    # Match cache – enrichment-driven backfill
    # ──────────────────────────────────────────────────────────────────────────

    async def _backfill_match_from_enrichment(
        self,
        business_id: str,
        firmographics: Dict[str, Any],
        col,
        now: datetime,
    ) -> None:
        """Teach the match cache the canonical identifiers Explorium
        returned via firmographics enrichment, so a future
        ``match_businesses`` call by *any* of those identifiers is a
        cache hit — not just the one the original input arrived with.

        Shares the merge implementation with :meth:`_save_match_cache`
        so the cache invariant (one doc per business, union of all
        identifiers, original ``input`` preserved) holds for both
        write paths.

        Backfill never carries an ``org_id`` because we don't want it to
        accidentally teach the match cache that a passive enrichment
        backfill counts as a billable access for the org.
        """
        website = firmographics.get("website")
        linkedin = firmographics.get("linkedin_profile")
        name = firmographics.get("name")

        nd = _normalize_domain(website) if website else None
        nl = _normalize_linkedin_url(linkedin) if linkedin else None
        nn = _normalize_name(name) if name else None

        nd = nd or None
        nl = nl or None
        nn = nn or None

        await self._merge_match_doc(
            col,
            business_id=business_id,
            nd=nd,
            nl=nl,
            nn=nn,
            raw_input={
                "_source": "enrichment_backfill",
                "name": name,
                "domain": website,
                "linkedin_url": linkedin,
            },
            now=now,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Match – shared cache orchestration (private)
    # ──────────────────────────────────────────────────────────────────────────

    async def _run_match_cache(
        self,
        entities: List[Dict[str, Optional[str]]],
        *,
        col_name: str,
        find_cached_fn: Callable,
        save_cache_fn: Callable,
        api_fn: Callable,
        response_list_key: str,
        entity_id_key: str,
        log_label: str,
        request_context: Optional[Dict[str, Any]] = None,
        org_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Generic cache-aside orchestration for entity matching.

        Steps:
        1. Look up cached results via *find_cached_fn* (with org-aware billing).
        2. Call the API for cache misses and persist via *save_cache_fn*.
        3. Stamp ``accessed_by_orgs`` on cache hits that this org hadn't
           paid for, so the next call from the same org is free.
        4. Reconstruct an API-shaped response preserving input order.

        Per-org billing in the response_context:
          * ``api_count`` — entries that hit Explorium (always billable).
          * ``cache_charge_count`` — cache hits new for this org (billable).
          * ``cache_free_count`` — cache hits already paid by this org (free).
          * ``billable_matches`` — sum of the two billables, restricted to
            entries that actually resolved to a non-null entity id (no-match
            cache hits don't bill anything since Explorium charges per match).
        """
        await self._ensure_indexes()
        col = mongo_db[col_name]
        now = _utcnow()
        ttl_cutoff = now - timedelta(days=MATCH_CACHE_TTL_DAYS)

        cached_results = await find_cached_fn(entities, col, ttl_cutoff, org_id)

        miss_indices = [i for i in range(len(entities)) if i not in cached_results]
        api_results: Dict[int, Optional[str]] = {}

        if miss_indices:
            miss_entities = [entities[i] for i in miss_indices]
            logger.info(
                "explorium %s match cache miss – fetching %d/%d from API",
                log_label,
                len(miss_entities),
                len(entities),
            )
            response = await api_fn(miss_entities, request_context=request_context)
            matched_api = response.get(response_list_key, [])
            await save_cache_fn(miss_entities, matched_api, col, now, org_id)
            for j, result in enumerate(matched_api):
                api_results[miss_indices[j]] = result.get(entity_id_key)

        # Stamp accessed_by_orgs on cache hits we just billed for, so the
        # same org won't be re-billed on the next read of the same row.
        if org_id:
            charge_doc_ids = [
                doc_id
                for (_eid, needs_charging, doc_id) in cached_results.values()
                if needs_charging and doc_id is not None
            ]
            if charge_doc_ids:
                await col.update_many(
                    {"_id": {"$in": charge_doc_ids}},
                    {"$addToSet": {"accessed_by_orgs": org_id}},
                )

        matched: List[Dict[str, Any]] = []
        cache_charge_count = 0
        cache_free_count = 0
        billable_matches = 0
        for i, entity in enumerate(entities):
            if i in cached_results:
                entity_id, needs_charging, _ = cached_results[i]
                if needs_charging:
                    cache_charge_count += 1
                    if entity_id:
                        billable_matches += 1
                else:
                    cache_free_count += 1
            else:
                entity_id = api_results.get(i)
                if entity_id:
                    billable_matches += 1
            matched.append({"input": entity, entity_id_key: entity_id})

        total_matches = sum(1 for m in matched if m.get(entity_id_key))
        return {
            response_list_key: matched,
            "total_results": len(matched),
            "total_matches": total_matches,
            "response_context": {
                "request_status": "success",
                "cached_count": len(cached_results),
                "api_count": len(miss_indices),
                "cache_charge_count": cache_charge_count,
                "cache_free_count": cache_free_count,
                "billable_matches": billable_matches,
            },
        }

    # ──────────────────────────────────────────────────────────────────────────
    # Match Businesses – cached public method
    # ──────────────────────────────────────────────────────────────────────────

    async def match_businesses(
        self,
        businesses_to_match: List[Dict[str, Optional[str]]],
        *,
        request_context: Optional[Dict[str, Any]] = None,
        org_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Match business attributes to Explorium Business IDs (PAID, MongoDB-cached).

        Implements the same fuzzy-matching logic as the Explorium API at the
        cache layer so repeated (or near-identical) lookups never consume credits.

        Each cached entry stores *every* identifier we have seen for the
        business (domain, linkedin_url, name); a later lookup that
        supplies any one of them — even one that wasn't in the original
        call — is a cache hit. Lookup priority (strongest first):

        * **Domain / URL** → exact match on ``norm_domain``.
        * **LinkedIn URL** → exact match on ``norm_linkedin``
          (canonicalised: protocol / ``www.`` / regional sub-domain /
          trailing slash stripped).
        * **Name** → fuzzy match (``token_sort + token_set``, threshold
          ``FUZZY_NAME_THRESHOLD``) across all cached entries that ever
          recorded a name. Catches typos, abbreviations, and CRM noise.

        Cache entries expire after ``MATCH_CACHE_TTL_DAYS`` days, after which
        the API is called again and the entry is refreshed.

        POST /v1/businesses/match

        Args:
            businesses_to_match: List of dicts (1-50), each with optional keys:
                ``name``, ``domain``, ``url``, ``linkedin_url``.
                Provide at least ``name`` or ``domain`` per entry.  Best
                results come from combining ``name`` + ``domain``.
            request_context: Optional request metadata forwarded to Explorium.

        Returns:
            Dict with ``matched_businesses`` (list preserving input order,
            each containing ``input`` and ``business_id`` or null),
            ``total_results``, ``total_matches``, and ``response_context``
            (includes ``cached_count`` and ``api_count``).
        """
        if not businesses_to_match or len(businesses_to_match) > 50:
            raise ValueError("businesses_to_match must contain 1-50 entries.")
        return await self._run_match_cache(
            businesses_to_match,
            col_name=COL_MATCH_BUSINESSES,
            find_cached_fn=self._find_cached_matches,
            save_cache_fn=self._save_match_cache,
            api_fn=self._api_match_businesses,
            response_list_key="matched_businesses",
            entity_id_key="business_id",
            log_label="business",
            request_context=request_context,
            org_id=org_id,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Match Prospects – raw API call (private, retry-wrapped)
    # ──────────────────────────────────────────────────────────────────────────

    @_retry
    async def _api_match_prospects(
        self,
        prospects_to_match: List[Dict[str, Optional[str]]],
        *,
        request_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Direct API call for prospect matching (no cache)."""
        payload: Dict[str, Any] = {
            "prospects_to_match": prospects_to_match,
            "request_context": request_context,
        }
        return await self._post("/v1/prospects/match", payload)

    # ──────────────────────────────────────────────────────────────────────────
    # Match Prospects – cache helpers (private)
    # ──────────────────────────────────────────────────────────────────────────

    async def _find_cached_prospect_matches(
        self,
        prospects: List[Dict[str, Optional[str]]],
        col,
        ttl_cutoff: datetime,
        org_id: Optional[str] = None,
    ) -> Dict[int, Tuple[Optional[str], bool, Optional[Any]]]:
        """Look up the prospect match cache for a batch of prospect inputs.

        Cache key priority (mirrors Explorium's own resolution order):

        1. **Email** (strongest – globally unique): exact match on ``norm_email``.
        2. **LinkedIn URL** (strong – globally unique): exact match on
           ``norm_linkedin`` for entries that have no email anchor.
        3. **Name + company** (fallback): fuzzy token-sort+set matching against
           all name-only cache entries (threshold ``FUZZY_NAME_THRESHOLD``).

        Returns:
            Mapping of *input list index* → ``(prospect_id, needs_charging, doc_id)``.
            See :meth:`_find_cached_matches` for the tuple contract.
        """
        results: Dict[int, Tuple[Optional[str], bool, Optional[Any]]] = {}

        email_to_indices: Dict[str, List[int]] = {}
        linkedin_to_indices: Dict[str, List[int]] = {}
        name_only_indices: List[int] = []

        for i, p in enumerate(prospects):
            raw_email = (p.get("email") or "").strip().lower()
            raw_linkedin = _normalize_domain(p.get("linkedin") or "")
            if raw_email:
                email_to_indices.setdefault(raw_email, []).append(i)
            elif raw_linkedin:
                linkedin_to_indices.setdefault(raw_linkedin, []).append(i)
            else:
                name_only_indices.append(i)

        # ── Email-keyed lookup ─────────────────────────────────────────────────
        # Match-cache lookups are no longer TTL-gated (see _find_cached_matches).
        if email_to_indices:
            async for doc in col.find(
                {
                    "norm_email": {"$in": list(email_to_indices.keys())},
                }
            ):
                ne = doc["norm_email"]
                needs_charging = not _org_already_accessed(doc, org_id)
                for idx in email_to_indices.get(ne, []):
                    results[idx] = (
                        doc.get("prospect_id"),
                        needs_charging,
                        doc.get("_id"),
                    )

        # ── LinkedIn-keyed lookup ──────────────────────────────────────────────
        if linkedin_to_indices:
            async for doc in col.find(
                {
                    "norm_linkedin": {"$in": list(linkedin_to_indices.keys())},
                    "norm_email": {"$exists": False},
                }
            ):
                nl = doc["norm_linkedin"]
                needs_charging = not _org_already_accessed(doc, org_id)
                for idx in linkedin_to_indices.get(nl, []):
                    results[idx] = (
                        doc.get("prospect_id"),
                        needs_charging,
                        doc.get("_id"),
                    )

        # ── Name + company fuzzy lookup ────────────────────────────────────────
        if name_only_indices:
            name_cache: List[Dict[str, Any]] = []
            async for doc in col.find(
                {
                    "norm_email": {"$exists": False},
                    "norm_linkedin": {"$exists": False},
                }
            ):
                name_cache.append(doc)

            for i in name_only_indices:
                raw_name = (prospects[i].get("full_name") or "").strip()
                if not raw_name:
                    continue
                query_norm = _normalize_name(raw_name)
                raw_company = _normalize_name(prospects[i].get("company_name") or "")
                best_doc = None
                best_score = -1
                for doc in name_cache:
                    cached_norm = doc.get("norm_full_name", "")
                    score = _names_fuzzy_score(query_norm, cached_norm)
                    # If company is present on both sides, factor it in
                    if raw_company and doc.get("norm_company_name"):
                        co_score = _names_fuzzy_score(
                            raw_company, doc["norm_company_name"]
                        )
                        score = (score + co_score) / 2
                    if score > best_score and score >= FUZZY_NAME_THRESHOLD:
                        best_score = score
                        best_doc = doc
                if best_doc is not None:
                    results[i] = (
                        best_doc.get("prospect_id"),
                        not _org_already_accessed(best_doc, org_id),
                        best_doc.get("_id"),
                    )

        return results

    async def _save_prospect_match_cache(
        self,
        prospects: List[Dict[str, Optional[str]]],
        matched: List[Dict[str, Any]],
        col,
        now: datetime,
        org_id: Optional[str] = None,
    ) -> None:
        """Upsert prospect match results into the cache collection.

        Cache key priority:
        - Email present  → keyed by ``norm_email`` (unique sparse index).
        - No email, linkedin present → keyed by ``norm_linkedin``.
        - Neither → keyed by ``norm_full_name`` (+ optional ``norm_company_name``).

        When ``org_id`` is supplied, the upsert seeds ``accessed_by_orgs``
        via ``$addToSet`` so the same org isn't re-billed on the next read.
        """
        ops: List[UpdateOne] = []
        for i, p in enumerate(prospects):
            if i >= len(matched):
                break
            prospect_id = matched[i].get("prospect_id")
            raw_email = (p.get("email") or "").strip().lower()
            raw_linkedin = _normalize_domain(p.get("linkedin") or "")
            raw_name = (p.get("full_name") or "").strip()
            raw_company = (p.get("company_name") or "").strip()

            def _build_update(
                filter_q: Dict[str, Any], set_payload: Dict[str, Any]
            ) -> UpdateOne:
                update_doc: Dict[str, Any] = {"$set": set_payload}
                if org_id:
                    update_doc["$addToSet"] = {"accessed_by_orgs": org_id}
                return UpdateOne(filter_q, update_doc, upsert=True)

            if raw_email:
                set_doc: Dict[str, Any] = {
                    "norm_email": raw_email,
                    "input": p,
                    "prospect_id": prospect_id,
                    "cached_at": now,
                }
                if raw_name:
                    set_doc["norm_full_name"] = _normalize_name(raw_name)
                if raw_company:
                    set_doc["norm_company_name"] = _normalize_name(raw_company)
                ops.append(_build_update({"norm_email": raw_email}, set_doc))
            elif raw_linkedin:
                set_doc = {
                    "norm_linkedin": raw_linkedin,
                    "input": p,
                    "prospect_id": prospect_id,
                    "cached_at": now,
                }
                if raw_name:
                    set_doc["norm_full_name"] = _normalize_name(raw_name)
                if raw_company:
                    set_doc["norm_company_name"] = _normalize_name(raw_company)
                ops.append(
                    _build_update(
                        {
                            "norm_linkedin": raw_linkedin,
                            "norm_email": {"$exists": False},
                        },
                        set_doc,
                    )
                )
            elif raw_name:
                nn = _normalize_name(raw_name)
                set_doc = {
                    "norm_full_name": nn,
                    "input": p,
                    "prospect_id": prospect_id,
                    "cached_at": now,
                }
                if raw_company:
                    set_doc["norm_company_name"] = _normalize_name(raw_company)
                ops.append(
                    _build_update(
                        {
                            "norm_full_name": nn,
                            "norm_email": {"$exists": False},
                            "norm_linkedin": {"$exists": False},
                        },
                        set_doc,
                    )
                )

        if ops:
            await col.bulk_write(ops, ordered=False)

    # ──────────────────────────────────────────────────────────────────────────
    # Match Prospects – cached public method
    # ──────────────────────────────────────────────────────────────────────────

    async def match_prospects(
        self,
        prospects_to_match: List[Dict[str, Optional[str]]],
        *,
        request_context: Optional[Dict[str, Any]] = None,
        org_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Match prospect attributes to Explorium Prospect IDs (PAID, MongoDB-cached).

        Cache key priority (mirrors Explorium's own resolution order):

        * **Email** (strongest): exact match on ``norm_email``.
        * **LinkedIn URL**: exact match on ``norm_linkedin`` for entries
          without an email anchor.
        * **Name + company** (fallback): fuzzy token-sort+set matching
          (threshold ``FUZZY_NAME_THRESHOLD``) against all name-only entries.

        Cache entries expire after ``MATCH_CACHE_TTL_DAYS`` days.

        POST /v1/prospects/match

        Args:
            prospects_to_match: List of dicts (1-50), each with optional keys:
                ``full_name``, ``company_name``, ``email``, ``phone_number``,
                ``linkedin``, ``business_id``.
                Provide at least one identifier per entry.
            request_context: Optional request metadata.

        Returns:
            Dict with ``matched_prospects`` (list preserving input order,
            each containing ``input`` and ``prospect_id`` or null),
            ``total_results``, ``total_matches``, and ``response_context``
            (includes ``cached_count`` and ``api_count``).
        """
        if not prospects_to_match or len(prospects_to_match) > 50:
            raise ValueError("prospects_to_match must contain 1-50 entries.")
        return await self._run_match_cache(
            prospects_to_match,
            col_name=COL_MATCH_PROSPECTS,
            find_cached_fn=self._find_cached_prospect_matches,
            save_cache_fn=self._save_prospect_match_cache,
            api_fn=self._api_match_prospects,
            response_list_key="matched_prospects",
            entity_id_key="prospect_id",
            log_label="prospect",
            request_context=request_context,
            org_id=org_id,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Business Enrichment – raw API call (private, retry-wrapped)
    # ──────────────────────────────────────────────────────────────────────────

    @_retry
    async def _api_bulk_enrich_businesses(
        self,
        business_ids: List[str],
        enrichment_type: str,
        *,
        parameters: Optional[Dict[str, Any]] = None,
        request_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Direct API call for business enrichment (no cache)."""
        payload: Dict[str, Any] = {
            "business_ids": business_ids,
            "request_context": request_context,
            "parameters": parameters or {},
        }
        return await self._post(
            f"/v1/businesses/{enrichment_type}/bulk_enrich", payload
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Business Enrichment – cached public method
    # ──────────────────────────────────────────────────────────────────────────

    async def bulk_enrich_businesses(
        self,
        business_ids: List[str],
        enrichment_type: str,
        *,
        parameters: Optional[Dict[str, Any]] = None,
        request_context: Optional[Dict[str, Any]] = None,
        org_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Bulk-enrich up to 50 businesses by enrichment type (PAID, MongoDB-cached).

        Checks ``explorium_businesses`` for fresh records keyed by
        ``(business_id, enrichment_type)``.  Cache entries older than
        ``CACHE_TTL_DAYS`` are transparently re-fetched and updated.

        Per-org billing (when ``org_id`` is supplied):
          * Cache hit AND ``org_id`` is already in ``accessed_by_orgs`` → free.
          * Cache hit AND ``org_id`` is NOT in ``accessed_by_orgs`` → counted as
            ``cache_charge_count`` so the caller bills 1 credit and ``org_id``
            is added to ``accessed_by_orgs`` for next time.
          * Cache miss → API call as before; the new doc is seeded with
            ``[org_id]`` so the same org won't be re-billed on the next read.

        ``org_id=None`` collapses to the historical "free on cache hit"
        behaviour so internal callers without an active account context
        (e.g. backfill scripts) keep working unchanged.

        Returns the same structure as the Explorium API:
        ``{"data": [{"business_id": ..., "data": {...}}, ...],
        "total_results": N, "entity_id": null, "response_context": {...}}``.
        ``response_context`` carries ``api_count`` (true API calls — billable),
        ``cache_charge_count`` (cache hits new for this org — also billable),
        ``cache_free_count`` (cache hits already paid by this org — free), and
        legacy ``cached_count`` (= charge + free) for back-compat.
        """
        if len(business_ids) > 50:
            raise ValueError(
                "Bulk enrich supports a maximum of 50 business IDs per call."
            )
        if enrichment_type not in BUSINESS_ENRICHMENT_URL_TYPES:
            raise ValueError(
                f"Unknown enrichment type '{enrichment_type}'. "
                f"Valid types: {BUSINESS_ENRICHMENT_URL_TYPES}"
            )

        await self._ensure_indexes()
        col = mongo_db[COL_BUSINESSES]
        now = _utcnow()
        ttl_cutoff = now - timedelta(days=CACHE_TTL_DAYS)

        # ── 1. Read cache (also pulling accessed_by_orgs for billing) ─────────
        cached: Dict[str, Any] = {}
        cache_charge_ids: List[str] = []  # cache hits new for this org → bill
        async for doc in col.find(
            {
                "business_id": {"$in": business_ids},
                "enrichment_type": enrichment_type,
                "cached_at": {"$gte": ttl_cutoff},
            }
        ):
            bid = doc["business_id"]
            cached[bid] = doc["data"]
            if not _org_already_accessed(doc, org_id):
                cache_charge_ids.append(bid)

        # ── 2. Fetch misses from API ───────────────────────────────────────────
        miss_ids = [bid for bid in business_ids if bid not in cached]
        api_data: Dict[str, Any] = {}

        if miss_ids:
            logger.info(
                "explorium cache miss – fetching %d/%d businesses for enrichment '%s'",
                len(miss_ids),
                len(business_ids),
                enrichment_type,
            )
            response = await self._api_bulk_enrich_businesses(
                miss_ids,
                enrichment_type,
                parameters=parameters,
                request_context=request_context,
            )
            ops: List[UpdateOne] = []
            for record in response.get("data") or []:
                bid = record.get("business_id")
                if not bid:
                    continue
                data_payload = record.get("data", {})
                api_data[bid] = data_payload
                update_doc: Dict[str, Any] = {
                    "$set": {
                        "business_id": bid,
                        "enrichment_type": enrichment_type,
                        "data": data_payload,
                        "cached_at": now,
                    }
                }
                # First-touch by *this* org owns the seed so the read on the
                # very next call is a paid-cache-hit, not a duplicate charge.
                if org_id:
                    update_doc["$addToSet"] = {"accessed_by_orgs": org_id}
                ops.append(
                    UpdateOne(
                        {"business_id": bid, "enrichment_type": enrichment_type},
                        update_doc,
                        upsert=True,
                    )
                )
            if ops:
                await col.bulk_write(ops, ordered=False)

        # ── 3. Stamp accessed_by_orgs on cache-charged docs ────────────────────
        # Done in a single bulk update so the collection scan doesn't need to
        # repeat per business_id; safe to run with the API-write batch above.
        if org_id and cache_charge_ids:
            await col.update_many(
                {
                    "business_id": {"$in": cache_charge_ids},
                    "enrichment_type": enrichment_type,
                },
                {"$addToSet": {"accessed_by_orgs": org_id}},
            )

        # ── 4. Reconstruct API-shaped response ────────────────────────────────
        combined: List[Dict[str, Any]] = []
        for bid in business_ids:
            if bid in cached:
                combined.append({"business_id": bid, "data": cached[bid]})
            elif bid in api_data:
                combined.append({"business_id": bid, "data": api_data[bid]})

        # ── 5. Backfill the *match* cache with identifiers learned here ───────
        # Firmographics enrichment is the canonical source of truth for
        # a business's website / linkedin_profile / name. Teach the
        # match cache about them so a subsequent ``match_businesses``
        # call by any of those identifiers becomes a cache hit instead
        # of a fresh paid API call. Only meaningful for the
        # ``firmographics`` enrichment type — other enrichments don't
        # carry these fields.
        if enrichment_type == "firmographics":
            match_col = mongo_db[COL_MATCH_BUSINESSES]
            for record in combined:
                bid = record["business_id"]
                data = record.get("data") or {}
                try:
                    await self._backfill_match_from_enrichment(
                        bid, data, match_col, now
                    )
                except Exception:
                    # Backfill must NEVER break enrichment — log and move on.
                    logger.exception("match-cache backfill raised for biz_id=%s", bid)

        cache_charge_count = len(cache_charge_ids)
        return {
            "data": combined,
            "total_results": len(combined),
            "entity_id": None,
            "response_context": {
                "request_status": "success",
                "cached_count": len(cached),
                "api_count": len(api_data),
                "cache_charge_count": cache_charge_count,
                "cache_free_count": len(cached) - cache_charge_count,
            },
        }

    # ──────────────────────────────────────────────────────────────────────────
    # Business Events – raw API call (private, retry-wrapped)
    # ──────────────────────────────────────────────────────────────────────────

    @_retry
    async def _api_fetch_businesses_events(
        self,
        business_ids: List[str],
        event_types: List[str],
        *,
        entity_type: str = "business",
        timestamp_from: Optional[str] = None,
        timestamp_to: Optional[str] = None,
        request_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Direct API call for business events (no cache)."""
        payload = {
            "event_types": event_types,
            "business_ids": business_ids,
            "entity_type": entity_type,
            "timestamp_from": timestamp_from,
            "timestamp_to": timestamp_to,
            "request_context": request_context,
        }
        return await self._post("/v1/businesses/events", payload)

    # ──────────────────────────────────────────────────────────────────────────
    # Business Events – cached public method
    # ──────────────────────────────────────────────────────────────────────────

    async def fetch_businesses_events(
        self,
        business_ids: List[str],
        event_types: List[str],
        *,
        entity_type: str = "business",
        timestamp_from: Optional[str] = None,
        timestamp_to: Optional[str] = None,
        request_context: Optional[Dict[str, Any]] = None,
        org_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch events for businesses (PAID: 2 credits per business per event type).

        Events are stored in ``explorium_events`` and never expire (append-only).
        Once a ``(business_id × event_type)`` pair has been fetched from the API,
        all future calls are served from MongoDB filtered by *timestamp_from* /
        *timestamp_to* without contacting the API again.

        Per-org billing lives on the meta-marker docs (one per pair, not per
        event), since pair-level is the natural billing unit. See
        :meth:`bulk_enrich_businesses` for the cache_charge_count contract.

        Returns the same structure as the Explorium API:
        ``{"output_events": [{...event...}, ...], "response_context": {...}}``.
        """
        if len(business_ids) > 40:
            raise ValueError(
                "Business events supports a maximum of 40 business IDs per call."
            )
        unknown = set(event_types) - set(BUSINESS_EVENT_TYPES)
        if unknown:
            raise ValueError(
                f"Unknown event type(s) {unknown!r}. "
                f"Valid types: {BUSINESS_EVENT_TYPES}"
            )

        await self._ensure_indexes()
        col = mongo_db[COL_EVENTS]
        now = _utcnow()

        # ── 1. Find which (business_id × event_type) pairs are cached ─────────
        # We pull accessed_by_orgs alongside so the per-org billing decision
        # comes off the same query — no second round trip to MongoDB.
        marker_ids = [
            f"meta:business:{bid}:{etype}"
            for bid in business_ids
            for etype in event_types
        ]
        cached_pairs: Set[Tuple[str, str]] = set()
        cache_charge_marker_ids: List[str] = []
        async for doc in col.find({"_id": {"$in": marker_ids}}):
            # _id format: "meta:business:{bid}:{etype}"
            parts = doc["_id"].split(":", 3)
            if len(parts) == 4:
                cached_pairs.add((parts[2], parts[3]))
                if not _org_already_accessed(doc, org_id):
                    cache_charge_marker_ids.append(doc["_id"])

        # ── 2. Fetch uncached pairs from API ──────────────────────────────────
        all_pairs: Set[Tuple[str, str]] = {
            (bid, etype) for bid in business_ids for etype in event_types
        }
        uncached_pairs = all_pairs - cached_pairs
        pairs_with_valid_events: Set[Tuple[str, str]] = set()

        if uncached_pairs:
            # Fetch all uncached business_ids with all uncached event_types in
            # one API call (no time bounds so the full history lands in cache).
            uncached_bids = list({bid for bid, _ in uncached_pairs})
            uncached_etypes = list({etype for _, etype in uncached_pairs})
            logger.info(
                "explorium cache miss – fetching events for %d businesses × %d event types",
                len(uncached_bids),
                len(uncached_etypes),
            )
            response = await self._api_fetch_businesses_events(
                uncached_bids,
                uncached_etypes,
                entity_type=entity_type,
                request_context=request_context,
                # No timestamp bounds: pull full history into cache
            )
            ops: List[UpdateOne] = []
            for event in response.get("output_events", []):
                event_id = event.get("event_id")
                bid = event.get("business_id")
                etype = event.get("event_name")
                time_str = event.get("event_time")
                if not event_id or not bid:
                    # None event_id means no chargeable data – skip caching
                    continue
                pairs_with_valid_events.add((bid, etype))
                ops.append(
                    UpdateOne(
                        {"event_id": event_id},
                        {
                            "$setOnInsert": {
                                "event_id": event_id,
                                "entity_type": "business",
                                "business_id": bid,
                                "event_type": etype,
                                "event_name": event.get("event_name"),
                                "event_time": _parse_event_time(time_str),
                                "event_time_str": time_str,
                                "data": event.get("data", {}),
                                "cached_at": now,
                            }
                        },
                        upsert=True,
                    )
                )
            # Only create fetch-markers for pairs that returned valid events;
            # pairs with no/None data are left uncached so the API is retried later.
            for bid, etype in pairs_with_valid_events:
                marker_id = f"meta:business:{bid}:{etype}"
                # Two-stage write keeps the immutable identity inside
                # $setOnInsert while still letting *every* call (insert OR
                # later read) bump accessed_by_orgs via $addToSet.
                marker_update: Dict[str, Any] = {
                    "$setOnInsert": {
                        "_id": marker_id,
                        "is_cache_marker": True,
                        "entity_type": "business",
                        "business_id": bid,
                        "event_type": etype,
                        "cached_at": now,
                    }
                }
                if org_id:
                    marker_update["$addToSet"] = {"accessed_by_orgs": org_id}
                ops.append(UpdateOne({"_id": marker_id}, marker_update, upsert=True))
            if ops:
                await col.bulk_write(ops, ordered=False)

        # ── 3. Stamp accessed_by_orgs on cache-charged markers ────────────────
        if org_id and cache_charge_marker_ids:
            await col.update_many(
                {"_id": {"$in": cache_charge_marker_ids}},
                {"$addToSet": {"accessed_by_orgs": org_id}},
            )

        # ── 4. Serve from MongoDB, filtered by the requested time range ────────
        query: Dict[str, Any] = {
            "entity_type": "business",
            "business_id": {"$in": business_ids},
            "event_type": {"$in": event_types},
            "is_cache_marker": {"$ne": True},
        }
        if timestamp_from or timestamp_to:
            time_filter: Dict[str, Any] = {}
            if timestamp_from:
                time_filter["$gte"] = _parse_event_time(timestamp_from)
            if timestamp_to:
                time_filter["$lte"] = _parse_event_time(timestamp_to)
            query["event_time"] = time_filter

        output_events: List[Dict[str, Any]] = []
        async for doc in col.find(query):
            output_events.append(
                {
                    "event_name": doc.get("event_name"),
                    "event_time": doc.get("event_time_str"),
                    "event_id": doc.get("event_id"),
                    "data": doc.get("data", {}),
                    "business_id": doc.get("business_id"),
                }
            )

        cache_charge_count = len(cache_charge_marker_ids)
        return {
            "output_events": output_events,
            "response_context": {
                "request_status": "success",
                "cached_count": len(cached_pairs),
                "api_count": len(pairs_with_valid_events),
                "cache_charge_count": cache_charge_count,
                "cache_free_count": len(cached_pairs) - cache_charge_count,
            },
        }

    # ──────────────────────────────────────────────────────────────────────────
    # Prospect Enrichment – raw API call (private, retry-wrapped)
    # ──────────────────────────────────────────────────────────────────────────

    @_retry
    async def _api_bulk_enrich_prospects(
        self,
        prospect_ids: List[str],
        enrichment_type: str,
        *,
        parameters: Optional[Dict[str, Any]] = None,
        request_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Direct API call for prospect enrichment (no cache)."""
        payload: Dict[str, Any] = {
            "prospect_ids": prospect_ids,
            "request_context": request_context,
            "parameters": parameters or {},
        }
        return await self._post(f"/v1/prospects/{enrichment_type}/bulk_enrich", payload)

    # ──────────────────────────────────────────────────────────────────────────
    # Prospect Enrichment – cached public method
    # ──────────────────────────────────────────────────────────────────────────

    async def bulk_enrich_prospects(
        self,
        prospect_ids: List[str],
        enrichment_type: str,
        *,
        parameters: Optional[Dict[str, Any]] = None,
        request_context: Optional[Dict[str, Any]] = None,
        org_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Bulk-enrich prospects by enrichment type (PAID, MongoDB-cached).

        Checks ``explorium_prospects`` for fresh records keyed by
        ``(prospect_id, enrichment_type)``.  Cache entries older than
        ``CACHE_TTL_DAYS`` are transparently re-fetched and updated.

        Per-org billing: see :meth:`bulk_enrich_businesses` — same model
        applies here on ``(prospect_id, enrichment_type)`` rows.

        Returns the same structure as the Explorium API:
        ``{"data": [{"prospect_id": ..., "data": {...}}, ...],
        "total_results": N, "entity_id": null, "response_context": {...}}``.
        """
        if enrichment_type not in PROSPECT_ENRICHMENT_URL_TYPES:
            raise ValueError(
                f"Unknown prospect enrichment type '{enrichment_type}'. "
                f"Valid types: {PROSPECT_ENRICHMENT_URL_TYPES}"
            )

        await self._ensure_indexes()
        col = mongo_db[COL_PROSPECTS]
        now = _utcnow()
        ttl_cutoff = now - timedelta(days=CACHE_TTL_DAYS)

        # ── 1. Read cache (also pulling accessed_by_orgs for billing) ─────────
        cached: Dict[str, Any] = {}
        cache_charge_ids: List[str] = []
        async for doc in col.find(
            {
                "prospect_id": {"$in": prospect_ids},
                "enrichment_type": enrichment_type,
                "cached_at": {"$gte": ttl_cutoff},
            }
        ):
            pid = doc["prospect_id"]
            cached[pid] = doc["data"]
            if not _org_already_accessed(doc, org_id):
                cache_charge_ids.append(pid)

        # ── 2. Fetch misses from API ───────────────────────────────────────────
        miss_ids = [pid for pid in prospect_ids if pid not in cached]
        api_data: Dict[str, Any] = {}

        if miss_ids:
            logger.info(
                "explorium cache miss – fetching %d/%d prospects for enrichment '%s'",
                len(miss_ids),
                len(prospect_ids),
                enrichment_type,
            )
            response = await self._api_bulk_enrich_prospects(
                miss_ids,
                enrichment_type,
                parameters=parameters,
                request_context=request_context,
            )
            ops: List[UpdateOne] = []
            for record in response.get("data") or []:
                pid = record.get("prospect_id")
                if not pid:
                    continue
                data_payload = record.get("data", {})
                api_data[pid] = data_payload
                update_doc: Dict[str, Any] = {
                    "$set": {
                        "prospect_id": pid,
                        "enrichment_type": enrichment_type,
                        "data": data_payload,
                        "cached_at": now,
                    }
                }
                if org_id:
                    update_doc["$addToSet"] = {"accessed_by_orgs": org_id}
                ops.append(
                    UpdateOne(
                        {"prospect_id": pid, "enrichment_type": enrichment_type},
                        update_doc,
                        upsert=True,
                    )
                )
            if ops:
                await col.bulk_write(ops, ordered=False)

        # ── 3. Stamp accessed_by_orgs on cache-charged docs ────────────────────
        if org_id and cache_charge_ids:
            await col.update_many(
                {
                    "prospect_id": {"$in": cache_charge_ids},
                    "enrichment_type": enrichment_type,
                },
                {"$addToSet": {"accessed_by_orgs": org_id}},
            )

        # ── 4. Reconstruct API-shaped response ────────────────────────────────
        combined: List[Dict[str, Any]] = []
        for pid in prospect_ids:
            if pid in cached:
                combined.append({"prospect_id": pid, "data": cached[pid]})
            elif pid in api_data:
                combined.append({"prospect_id": pid, "data": api_data[pid]})

        cache_charge_count = len(cache_charge_ids)
        return {
            "data": combined,
            "total_results": len(combined),
            "entity_id": None,
            "response_context": {
                "request_status": "success",
                "cached_count": len(cached),
                "api_count": len(api_data),
                "cache_charge_count": cache_charge_count,
                "cache_free_count": len(cached) - cache_charge_count,
            },
        }

    # ──────────────────────────────────────────────────────────────────────────
    # Prospect Events – raw API call (private, retry-wrapped)
    # ──────────────────────────────────────────────────────────────────────────

    @_retry
    async def _api_fetch_prospects_events(
        self,
        prospect_ids: List[str],
        event_types: List[str],
        *,
        entity_type: str = "prospect",
        timestamp_from: Optional[str] = None,
        timestamp_to: Optional[str] = None,
        request_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Direct API call for prospect events (no cache)."""
        payload = {
            "event_types": event_types,
            "prospect_ids": prospect_ids,
            "entity_type": entity_type,
            "timestamp_from": timestamp_from,
            "timestamp_to": timestamp_to,
            "request_context": request_context,
        }
        return await self._post("/v1/prospects/events", payload)

    # ──────────────────────────────────────────────────────────────────────────
    # Prospect Events – cached public method
    # ──────────────────────────────────────────────────────────────────────────

    async def fetch_prospects_events(
        self,
        prospect_ids: List[str],
        event_types: List[str],
        *,
        entity_type: str = "prospect",
        timestamp_from: Optional[str] = None,
        timestamp_to: Optional[str] = None,
        request_context: Optional[Dict[str, Any]] = None,
        org_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch events for prospects (PAID: 2 credits per prospect per event type).

        Events are stored in ``explorium_events`` and never expire (append-only).
        Once a ``(prospect_id × event_type)`` pair has been fetched from the API,
        all future calls are served from MongoDB filtered by *timestamp_from* /
        *timestamp_to* without contacting the API again.

        Returns the same structure as the Explorium API:
        ``{"output_events": [{...event...}, ...], "response_context": {...}}``.
        """
        unknown = set(event_types) - set(PROSPECT_EVENT_TYPES)
        if unknown:
            raise ValueError(
                f"Unknown event type(s) {unknown!r}. "
                f"Valid types: {PROSPECT_EVENT_TYPES}"
            )

        await self._ensure_indexes()
        col = mongo_db[COL_EVENTS]
        now = _utcnow()

        # ── 1. Find which (prospect_id × event_type) pairs are cached ─────────
        marker_ids = [
            f"meta:prospect:{pid}:{etype}"
            for pid in prospect_ids
            for etype in event_types
        ]
        cached_pairs: Set[Tuple[str, str]] = set()
        cache_charge_marker_ids: List[str] = []
        async for doc in col.find({"_id": {"$in": marker_ids}}):
            # _id format: "meta:prospect:{pid}:{etype}"
            parts = doc["_id"].split(":", 3)
            if len(parts) == 4:
                cached_pairs.add((parts[2], parts[3]))
                if not _org_already_accessed(doc, org_id):
                    cache_charge_marker_ids.append(doc["_id"])

        # ── 2. Fetch uncached pairs from API ──────────────────────────────────
        all_pairs: Set[Tuple[str, str]] = {
            (pid, etype) for pid in prospect_ids for etype in event_types
        }
        uncached_pairs = all_pairs - cached_pairs

        if uncached_pairs:
            uncached_pids = list({pid for pid, _ in uncached_pairs})
            uncached_etypes = list({etype for _, etype in uncached_pairs})
            logger.info(
                "explorium cache miss – fetching events for %d prospects × %d event types",
                len(uncached_pids),
                len(uncached_etypes),
            )
            response = await self._api_fetch_prospects_events(
                uncached_pids,
                uncached_etypes,
                entity_type=entity_type,
                request_context=request_context,
                # No timestamp bounds: pull full history into cache
            )
            ops: List[UpdateOne] = []
            for event in response.get("output_events", []):
                event_id = event.get("event_id")
                pid = event.get("prospect_id")
                etype = event.get("event_name")
                time_str = event.get("event_time")
                if not event_id or not pid:
                    continue
                ops.append(
                    UpdateOne(
                        {"event_id": event_id},
                        {
                            "$setOnInsert": {
                                "event_id": event_id,
                                "entity_type": "prospect",
                                "prospect_id": pid,
                                "event_type": etype,
                                "event_name": event.get("event_name"),
                                "event_time": _parse_event_time(time_str),
                                "event_time_str": time_str,
                                "data": event.get("data", {}),
                                "cached_at": now,
                            }
                        },
                        upsert=True,
                    )
                )
            # Create fetch-marker per uncached pair
            for pid, etype in uncached_pairs:
                marker_id = f"meta:prospect:{pid}:{etype}"
                marker_update: Dict[str, Any] = {
                    "$setOnInsert": {
                        "_id": marker_id,
                        "is_cache_marker": True,
                        "entity_type": "prospect",
                        "prospect_id": pid,
                        "event_type": etype,
                        "cached_at": now,
                    }
                }
                if org_id:
                    marker_update["$addToSet"] = {"accessed_by_orgs": org_id}
                ops.append(UpdateOne({"_id": marker_id}, marker_update, upsert=True))
            if ops:
                await col.bulk_write(ops, ordered=False)

        # ── 3. Stamp accessed_by_orgs on cache-charged markers ────────────────
        if org_id and cache_charge_marker_ids:
            await col.update_many(
                {"_id": {"$in": cache_charge_marker_ids}},
                {"$addToSet": {"accessed_by_orgs": org_id}},
            )

        # ── 4. Serve from MongoDB, filtered by the requested time range ────────
        query: Dict[str, Any] = {
            "entity_type": "prospect",
            "prospect_id": {"$in": prospect_ids},
            "event_type": {"$in": event_types},
            "is_cache_marker": {"$ne": True},
        }
        if timestamp_from or timestamp_to:
            time_filter: Dict[str, Any] = {}
            if timestamp_from:
                time_filter["$gte"] = _parse_event_time(timestamp_from)
            if timestamp_to:
                time_filter["$lte"] = _parse_event_time(timestamp_to)
            query["event_time"] = time_filter

        output_events: List[Dict[str, Any]] = []
        async for doc in col.find(query):
            output_events.append(
                {
                    "event_name": doc.get("event_name"),
                    "event_time": doc.get("event_time_str"),
                    "event_id": doc.get("event_id"),
                    "data": doc.get("data", {}),
                    "prospect_id": doc.get("prospect_id"),
                }
            )

        cache_charge_count = len(cache_charge_marker_ids)
        return {
            "output_events": output_events,
            "response_context": {
                "request_status": "success",
                "cached_count": len(cached_pairs),
                "api_count": len(uncached_pairs),
                "cache_charge_count": cache_charge_count,
                "cache_free_count": len(cached_pairs) - cache_charge_count,
            },
        }
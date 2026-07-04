import asyncio
import logging
import os
from typing import Dict, List, Optional, Union
import httpx
from pydantic import BaseModel
import tenacity

logger = logging.getLogger(__name__)


class FullEnrichContact(BaseModel):
    firstname: str
    lastname: str
    domain: Optional[str] = None
    company_name: Optional[str] = None
    linkedin_url: Optional[str] = None
    enrich_fields: Optional[List[str]] = None
    custom: Optional[Dict[str, str]] = None


class FullEnrichRequest(BaseModel):
    name: str
    datas: List[FullEnrichContact]
    webhook_url: Optional[str] = None


class FullEnrichResponse(BaseModel):
    enrichment_id: str


class FullEnrichResult(BaseModel):
    id: str
    name: str
    status: str
    datas: List[Dict]


class FullEnrichClient:
    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.environ.get("FULLENRICH_API_KEY")
        if not self.api_key:
            raise ValueError("FullEnrich API key is required")

        self.base_url = "https://app.fullenrich.com/api/v1"
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        self.client = httpx.AsyncClient(timeout=300.0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.client.aclose()

    @tenacity.retry(
        stop=tenacity.stop_after_attempt(3),
        wait=tenacity.wait_exponential(multiplier=1, min=4, max=10),
        retry=tenacity.retry_if_exception_type(
            (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.ReadTimeout,
                httpx.NetworkError,
            )
        ),
        before_sleep=lambda retry_state: logger.info(
            f"FullEnrich API call failed, retrying in {retry_state.next_action.sleep} seconds..."
        ),
    )
    async def start_bulk_enrichment(
        self, request: FullEnrichRequest
    ) -> FullEnrichResponse:
        """Start bulk enrichment for multiple contacts"""
        url = f"{self.base_url}/contact/enrich/bulk"

        response = await self.client.post(
            url, headers=self.headers, json=request.dict(exclude_none=True)
        )

        if response.status_code != 200:
            raise Exception(
                f"FullEnrich API error: {response.text}, status: {response.status_code}"
            )

        return FullEnrichResponse(**response.json())

    @tenacity.retry(
        stop=tenacity.stop_after_attempt(3),
        wait=tenacity.wait_exponential(multiplier=1, min=4, max=10),
        retry=tenacity.retry_if_exception_type(
            (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.ReadTimeout,
                httpx.NetworkError,
            )
        ),
        before_sleep=lambda retry_state: logger.info(
            f"FullEnrich API call failed, retrying in {retry_state.next_action.sleep} seconds..."
        ),
    )
    async def get_enrichment_result(
        self, enrichment_id: str, force_results: bool = False
    ) -> FullEnrichResult:
        """Get enrichment result by ID"""
        url = f"{self.base_url}/contact/enrich/bulk/{enrichment_id}"
        params = {"forceResults": force_results} if force_results else {}

        response = await self.client.get(url, headers=self.headers, params=params)

        if response.status_code != 200:
            raise Exception(
                f"FullEnrich API error: {response.text}, status: {response.status_code}"
            )

        return FullEnrichResult(**response.json())

    async def wait_for_completion(
        self, enrichment_id: str, max_wait_time: int = 300, poll_interval: int = 5
    ) -> FullEnrichResult:
        """Wait for enrichment to complete with polling"""
        start_time = asyncio.get_event_loop().time()

        while True:
            result = await self.get_enrichment_result(enrichment_id)

            if result.status in ["FINISHED", "CANCELED", "CREDITS_INSUFFICIENT"]:
                return result

            elapsed_time = asyncio.get_event_loop().time() - start_time
            if elapsed_time > max_wait_time:
                logger.warning(
                    f"Enrichment {enrichment_id} timed out after {max_wait_time} seconds"
                )
                return result

            await asyncio.sleep(poll_interval)

    async def get_credit_balance(self) -> Dict[str, Union[int, str]]:
        """Get current credit balance"""
        url = f"{self.base_url}/credit/balance"

        response = await self.client.get(url, headers=self.headers)

        if response.status_code != 200:
            raise Exception(
                f"FullEnrich API error: {response.text}, status: {response.status_code}"
            )

        return response.json()

    async def validate_api_key(self) -> bool:
        """Check if API key is valid"""
        try:
            await self.get_credit_balance()
            return True
        except Exception:
            return False

"""Fast HTTP web-page extraction (waterfall over external extractor APIs).

The first-choice way to read a page once you have its URL — far faster and
cheaper than opening a cloud-browser tab. Tries a sequence of stateless
extractor APIs and returns the first that yields usable content. If every
extractor fails (login wall, bot detection, JS-only page, network error), it
returns None and the caller should fall back to the cloud browser.
"""

import asyncio
import logging
import os
from typing import Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

# Keep in-flight requests below the Trafilatura Lambda's reserved concurrency
# (currently 10). 8 leaves headroom for bursts and avoids AWS throttling (429s).
_LAMBDA_SEMAPHORE = asyncio.Semaphore(8)


async def _trafilatura_get_text(url: str, timeout: float = 10.0) -> Optional[str]:
    """Extract page text via the self-hosted Trafilatura Lambda. Cheapest/fastest."""
    endpoint = os.environ.get("TRAFILATURA_ENDPOINT")
    api_key = os.environ.get("TRAFILATURA_API_KEY")
    if not endpoint or not api_key:
        return None
    async with _LAMBDA_SEMAPHORE:
        try:
            headers = {"x-api-key": api_key, "Content-Type": "application/json"}
            payload = {"url": url, "timeout": timeout}
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    endpoint, headers=headers, json=payload, timeout=timeout + 10.0
                )
                response.raise_for_status()
                result = response.json()
            if not result.get("success"):
                logger.warning(f"Trafilatura returned success=False for {url}")
                return None
            return result.get("content") or None
        except Exception as e:
            logger.error(f"Trafilatura extraction failed for {url}: {e}")
            return None


# async def _handinger_markdown(url: str, timeout: float = 120.0) -> Optional[str]:
#     """Extract page markdown via the Handinger API. More robust fallback."""
#     api_key = os.environ.get("HANDINGER_API_KEY")
#     if not api_key:
#         return None
#     try:
#         encoded_url = quote(url, safe="")
#         api_url = f"https://api.handinger.com/markdown?fresh=false&url={encoded_url}"
#         headers = {"Authorization": f"Bearer {api_key}"}
#         async with httpx.AsyncClient(timeout=timeout) as client:
#             response = await client.get(api_url, headers=headers)
#             response.raise_for_status()
#             return response.text or None
#     except Exception as e:
#         logger.error(f"Handinger extraction failed for {url}: {e}")
#         return None


async def fetch_url_data(url: str) -> Optional[str]:
    """Waterfall page extraction. Returns the first extractor's usable text/markdown,
    or None if all failed (caller should fall back to the cloud browser)."""
    for extractor in (_trafilatura_get_text,):
        content = await extractor(url)
        if content and content.strip():
            logger.info(f"fetch_url_data: {extractor.__name__} extracted {url}")
            return content
    logger.info(f"fetch_url_data: all extractors failed for {url}")
    return None

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from logging import getLogger
from typing import Optional

import httpx
from pydantic import BaseModel
import tenacity

from .utils import export_model

logger = getLogger(__name__)
CLOUD_BROWSER_INSTANCE_MAP: dict[str, "CloudBrowserAgent"] = {}

# Cloud-browser config (replaces the private-repo `settings` module). The env
# vars are loaded by python-dotenv at startup (see main.py).
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")
CLOUD_BROWSER_URL = os.environ.get("CLOUD_BROWSER_URL", "")

class DynamicBrowserEnabled:
    def __bool__(self):
        from dotenv import load_dotenv
        load_dotenv(override=True)
        return os.environ.get("ENABLE_CLOUDBROWSER", "").strip().lower() in ("1", "true", "yes", "on")

CLOUD_BROWSER_ENABLED = DynamicBrowserEnabled()


class BrowserUnavailableError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(self.message)


class CloudBrowserAgent:
    def __init__(self, user_id: str):
        self.user_id = user_id
        self.session = httpx.AsyncClient(timeout=None, verify=False)
        self.frontend_session_url = (
            f"{PUBLIC_URL}/browser-session/redirect?user_id={self.user_id}"
        )
        self.agent_url: str | None = None
        self.session_url: str | None = None
        self.close_url: str | None = None
        CLOUD_BROWSER_INSTANCE_MAP[self.user_id] = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.cleanup()

    async def create_browser_until_available(
        self, agent: bool = True, url: str = "https://example.com/"
    ):
        if not CLOUD_BROWSER_ENABLED:
            # Return a dummy successful response without making network calls
            self.session_url = "about:blank"
            self.agent_url = "disabled://agent"
            self.close_url = "disabled://close"
            return {
                "browser_url": self.session_url,
                "agent_url": self.agent_url,
                "close_url": self.close_url,
            }
        max_retry_time = 30 * 10
        retry_delay = 30
        start_time = time.time()
        last_error: Exception | None = None
        while time.time() - start_time < max_retry_time:
            try:
                return await self.create_browser(agent, url)
            except Exception as e:  # pragma: no cover - network error paths
                last_error = e
                logger.warning(f"Failed to create browser: {e}")
                logger.info(f"Retrying in {retry_delay} seconds")
                await asyncio.sleep(retry_delay)
        raise BrowserUnavailableError(f"Failed to create browser: {last_error}")

    async def create_browser(self, agent: bool, url: str):
        if not CLOUD_BROWSER_ENABLED:
            # Dummy values when cloud browser is disabled
            self.session_url = "about:blank"
            self.agent_url = "disabled://agent"
            self.close_url = "disabled://close"
            return {
                "browser_url": self.session_url,
                "agent_url": self.agent_url,
                "close_url": self.close_url,
            }
        response = await self.session.post(
            f"{CLOUD_BROWSER_URL}/open?agent={agent}",
            json={"email": self.user_id, "url": url},
            timeout=400,
        )
        if response.status_code == 500:
            raise BrowserUnavailableError(
                "Browser is likely already open and the existing session may be "
                "stale. Close the browser (cloud_browser_close) and open it again."
            )
        if response.status_code != 200:
            raise Exception(
                f"Failed to create browser: {response.text}, status code: {response.status_code} for user {self.user_id}"
            )
        response_json = response.json()
        self.session_url = response_json["browser_url"]
        self.agent_url = response_json["agent_url"]
        self.close_url = response_json["close_url"]
        return response_json

    async def close_browser(self):
        if not CLOUD_BROWSER_ENABLED:
            return {"message": "Cloud browser disabled. Dummy close successful."}
        response = await self.session.post(self.close_url)
        if response.status_code != 200:
            logger.warning(
                f"Failed to close browser: {response.text}, status code: {response.status_code}"
            )
        return response.json()

    async def _restart_browser_on_error(self, error_description: str):
        """Helper method to restart browser when errors occur"""
        logging.warning(f"{error_description} detected, restarting browser...")
        try:
            await self.close_browser()
        except Exception as close_error:
            logging.warning(f"Failed to close browser during restart: {close_error}")
        try:
            await self.create_browser_until_available(agent=True)
            logging.info("Browser restarted successfully")
        except Exception as restart_error:
            logging.error(f"Failed to restart browser: {restart_error}")
            raise

    async def _run_command_base(
        self,
        command: str,
        response_format_model: Optional[BaseModel] = None,
        streaming: bool = False,
        extra_data_extraction_prompt: Optional[str] = None,
        planner_agent_example_prompt: Optional[str] = None,
        direct_extract_data: bool = False,
        skip_planner: bool = False,
        intercept_matching_api: Optional[str] = None,
        raw_html: bool = False,
        timeout_before_extracting_data: int = 0,
    ):
        self.last_activity = datetime.now()
        if not streaming:
            responses: list[object] = []
        # Short-circuit with dummy responses when cloud browser is disabled
        if not CLOUD_BROWSER_ENABLED:
            if direct_extract_data:
                dummy = {
                    "type": "direct_extracted_data",
                    "message": "Direct Extracted Data: ",
                }
            else:
                dummy = {
                    "message": f"Cloud browser disabled. Dummy response for: {command[:200]}"
                }
            if streaming:
                yield dummy
                return
            else:
                yield [dummy]
                return
        try:
            payload = {
                "command": command,
            }
            if response_format_model:
                payload["response_format"] = export_model(response_format_model)
            if extra_data_extraction_prompt:
                payload["extra_data_extraction_prompt"] = extra_data_extraction_prompt
            if planner_agent_example_prompt:
                payload["planner_agent_example_prompt"] = planner_agent_example_prompt
            if direct_extract_data:
                payload["just_extract_web_text"] = True
            if skip_planner:
                payload["skip_planner"] = True
            if intercept_matching_api:
                payload["intercept_matching_api"] = intercept_matching_api
            if raw_html:
                payload["raw_html"] = True
            if timeout_before_extracting_data:
                payload[
                    "timeout_before_extracting_data"
                ] = timeout_before_extracting_data
            async with self.session.stream(
                "POST",
                self.agent_url,
                json=payload,
                timeout=900.0,
            ) as response:
                if response.status_code == 502:
                    logger.warning("Received 502 Bad Gateway error, restarting browser")
                    await self._restart_browser_on_error("502 Bad Gateway error")
                    error_response = {
                        "error": "502 Bad Gateway",
                        "message": "Service temporarily unavailable - browser restarted",
                    }
                    if streaming:
                        yield error_response
                        return
                    responses = [error_response]
                    yield responses
                    return
                response.raise_for_status()
                # httpx automatically handles cookies from the response
                buffer = ""
                async for chunk in response.aiter_text():
                    if not chunk:
                        continue
                    buffer += chunk
                    while "\n\n" in buffer:
                        raw, buffer = buffer.split("\n\n", 1)
                        raw = raw.strip()
                        if not raw:
                            continue
                        if raw.startswith("data:"):
                            raw = raw[len("data:") :].strip()
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            data = raw
                        if streaming:
                            yield data
                        else:
                            responses.append(data)
                leftover = buffer.strip()
                if leftover:
                    if leftover.startswith("data:"):
                        leftover = leftover[len("data:") :].strip()
                    try:
                        data = json.loads(leftover)
                    except json.JSONDecodeError:
                        data = leftover
                    if streaming:
                        yield data
                    else:
                        responses.append(data)
        except httpx.HTTPStatusError as e:  # pragma: no cover - network error paths
            if e.response.status_code == 502:
                logger.warning("Received 502 Bad Gateway error, restarting browser")
                await self._restart_browser_on_error("502 Bad Gateway error")
                error_response = {
                    "error": "502 Bad Gateway",
                    "message": "Service temporarily unavailable - browser restarted",
                }
                if streaming:
                    yield error_response
                    return
                responses = [error_response]
                yield responses
                return
            logging.error(f"HTTP status error: {e}")
            raise
        except httpx.TimeoutException:  # pragma: no cover - network error paths
            logging.error("Request timed out while streaming response")
            raise
        except Exception as e:  # pragma: no cover - network error paths
            logging.error(f"Error during command execution: {str(e)}")
            # Handle DNS resolution errors by restarting the browser
            if "nodename nor servname provided, or not known" in str(e):
                await self._restart_browser_on_error("DNS resolution error")
            raise
        if not streaming:
            logging.info(
                f"Command execution completed with {len(responses)} response chunks"
            )
            yield responses

    @tenacity.retry(
        stop=tenacity.stop_after_attempt(5),
        wait=tenacity.wait_exponential(multiplier=2, min=2, max=30),
        retry=tenacity.retry_if_exception_type(
            (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.ReadTimeout,
                httpx.NetworkError,
                httpx.HTTPStatusError,
                httpx.RemoteProtocolError,
            )
        ),
        before_sleep=lambda retry_state: logging.info(
            f"Connection failed, retrying in {retry_state.next_action.sleep} seconds..."
        ),
    )
    async def run_command(
        self,
        command: str,
        response_format_model: Optional[BaseModel] = None,
        extra_data_extraction_prompt: Optional[str] = None,
        planner_agent_example_prompt: Optional[str] = None,
        direct_extract_data: bool = False,
        skip_planner: bool = False,
        intercept_matching_api: Optional[str] = None,
        raw_html: bool = False,
        timeout_before_extracting_data: int = 0,
    ):
        async for responses in self._run_command_base(
            command,
            response_format_model,
            streaming=False,
            extra_data_extraction_prompt=extra_data_extraction_prompt,
            planner_agent_example_prompt=planner_agent_example_prompt,
            direct_extract_data=direct_extract_data,
            skip_planner=skip_planner,
            intercept_matching_api=intercept_matching_api,
            raw_html=raw_html,
            timeout_before_extracting_data=timeout_before_extracting_data,
        ):
            return responses

    @tenacity.retry(
        stop=tenacity.stop_after_attempt(5),
        wait=tenacity.wait_exponential(multiplier=2, min=2, max=30),
        retry=tenacity.retry_if_exception_type(
            (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.ReadTimeout,
                httpx.NetworkError,
                httpx.HTTPStatusError,
                httpx.RemoteProtocolError,
            )
        ),
        before_sleep=lambda retry_state: logging.info(
            f"Connection failed, retrying in {retry_state.next_action.sleep} seconds..."
        ),
    )
    async def run_command_stream(
        self,
        command: str,
        response_format_model: Optional[BaseModel] = None,
        extra_data_extraction_prompt: Optional[str] = None,
        planner_agent_example_prompt: Optional[str] = None,
        intercept_matching_api: Optional[str] = None,
        raw_html: bool = False,
        timeout_before_extracting_data: int = 0,
    ):
        async for chunk in self._run_command_base(
            command,
            response_format_model,
            streaming=True,
            extra_data_extraction_prompt=extra_data_extraction_prompt,
            planner_agent_example_prompt=planner_agent_example_prompt,
            intercept_matching_api=intercept_matching_api,
            raw_html=raw_html,
            timeout_before_extracting_data=timeout_before_extracting_data,
        ):
            yield chunk

    async def cleanup(self):
        await self.session.aclose()
        CLOUD_BROWSER_INSTANCE_MAP.pop(self.user_id, None)

    async def get_session_url_with_cookies(self):
        """Get session URL with current cookies (httpx manages cookies automatically)"""
        return {"url": self.session_url, "cookies": dict(self.session.cookies)}

    @classmethod
    async def close_all_browsers(cls):
        logger.info("Closing all existingbrowsers")
        if not CLOUD_BROWSER_ENABLED:
            return
        await httpx.AsyncClient(verify=False).post(
            f"{CLOUD_BROWSER_URL}/close_all"
        )

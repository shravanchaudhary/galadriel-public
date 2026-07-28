"""Browser Command Executor — Python client library.

Control a paired Chrome browser via HTTP (Chrome extension + FastAPI server).

Prerequisites:
  1. MongoDB running, FastAPI server started
  2. Chrome extension loaded, pairing code visible in popup
  3. Agent toggled ON in extension (status: Connected)

Configuration:
  BCE_BASE_URL  — default http://localhost:8000
  BCE_API_KEY   — must match server BCE_API_KEY
  BCE_PAIRING_CODE — optional; demo prompts if unset
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Literal


class BCEError(Exception):
    """Base error for BCE client."""


class BCEOfflineError(BCEError):
    """Extension is not connected (Agent OFF or WebSocket down)."""


class BCECommandError(BCEError):
    def __init__(self, code: str, message: str, response: CommandResult) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.response = response


class BCEHTTPError(BCEError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


@dataclass
class CommandResult:
    id: str
    ok: bool
    duration_ms: int | None = None
    result: Any = None
    state: Any = None
    error: dict[str, Any] | None = None

    def raise_on_error(self) -> CommandResult:
        if not self.ok:
            err = self.error or {}
            raise BCECommandError(
                err.get("code", "COMMAND_FAILED"),
                err.get("message", "Command failed"),
                self,
            )
        return self


@dataclass
class DeviceStatus:
    device_id: str
    pairing_code: str
    name: str
    online: bool
    last_seen_at: str | None = None


@dataclass
class DeviceInfo:
    device_id: str
    pairing_code: str
    name: str
    online: bool
    last_seen_at: str | None = None


def normalize_pairing_code(code: str) -> str:
    cleaned = code.strip().upper().replace(" ", "")
    if "-" not in cleaned and len(cleaned) == 8:
        cleaned = f"{cleaned[:4]}-{cleaned[4:]}"
    if not re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}", cleaned):
        raise BCEError(f"Invalid pairing code format: {code!r} (expected XXXX-XXXX)")
    return cleaned


class BCEClient:
    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        api_key: str = "dev-api-key",
        pairing_code: str = "",
        default_timeout_ms: int = 10000,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.pairing_code = normalize_pairing_code(pairing_code) if pairing_code else ""
        self.default_timeout_ms = default_timeout_ms

    @classmethod
    def from_env(cls) -> BCEClient:
        from dotenv import load_dotenv
        load_dotenv(override=True)
        code = os.environ.get("BCE_PAIRING_CODE", "")
        return cls(
            base_url=os.environ.get("BCE_BASE_URL", "http://localhost:8000"),
            api_key=os.environ.get("BCE_API_KEY", "dev-api-key"),
            pairing_code=code,
            default_timeout_ms=int(os.environ.get("BCE_TIMEOUT_MS", "10000")),
        )

    def connect(self, pairing_code: str | None = None) -> str:
        """Bind client to a browser using its pairing code from the extension popup."""
        code = pairing_code or self.pairing_code
        if not code:
            raise BCEError("pairing_code required — read it from the extension popup")
        self.pairing_code = normalize_pairing_code(code)
        status = self.device_status()
        return status.device_id

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        auth: bool = True,
        timeout_ms: int | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(
                req,
                timeout=max(1.0, (timeout_ms or self.default_timeout_ms) / 1000),
            ) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()
            try:
                parsed = json.loads(detail)
                detail = parsed.get("detail", detail)
            except json.JSONDecodeError:
                pass
            raise BCEHTTPError(exc.code, str(detail)) from exc

    def _command(
        self,
        command: str,
        args: dict[str, Any] | None = None,
        *,
        timeout_ms: int | None = None,
        raise_on_error: bool = True,
    ) -> CommandResult:
        if not self.pairing_code:
            raise BCEError("Not connected — call connect(pairing_code) first")
        effective_timeout = timeout_ms or self.default_timeout_ms
        payload = self._request(
            "POST",
            f"/devices/by-code/{self.pairing_code}/commands",
            {
                "command": command,
                "args": args or {},
                "timeout_ms": effective_timeout,
            },
            timeout_ms=effective_timeout + 5000,
        )
        result = CommandResult(
            id=payload["id"],
            ok=payload["ok"],
            duration_ms=payload.get("duration_ms"),
            result=payload.get("result"),
            state=payload.get("state"),
            error=payload.get("error"),
        )
        if raise_on_error:
            result.raise_on_error()
        return result

    def list_devices(self, *, online_only: bool = False) -> list[DeviceInfo]:
        query = "?online=true" if online_only else ""
        payload = self._request("GET", f"/devices{query}")
        return [
            DeviceInfo(
                device_id=item["device_id"],
                pairing_code=item["pairing_code"],
                name=item["name"],
                online=item["online"],
                last_seen_at=item.get("last_seen_at"),
            )
            for item in payload
        ]

    def device_status(self) -> DeviceStatus:
        if not self.pairing_code:
            raise BCEError("Not connected — call connect(pairing_code) first")
        payload = self._request("GET", f"/devices/by-code/{self.pairing_code}")
        return DeviceStatus(
            device_id=payload["device_id"],
            pairing_code=payload["pairing_code"],
            name=payload["name"],
            online=payload["online"],
            last_seen_at=payload.get("last_seen_at"),
        )

    def ensure_online(self) -> None:
        if not self.pairing_code:
            raise BCEError("Not connected — call connect(pairing_code) first")
        status = self.device_status()
        if not status.online:
            raise BCEOfflineError(
                f"Browser {status.pairing_code} offline — toggle Agent ON in extension popup"
            )

    def open(self, url: str, *, timeout_ms: int | None = None) -> CommandResult:
        return self._command("open", {"url": url}, timeout_ms=timeout_ms or 15000)

    def back(self, *, timeout_ms: int | None = None) -> CommandResult:
        return self._command("back", {}, timeout_ms=timeout_ms)

    def scroll(
        self,
        direction: Literal["up", "down"] = "down",
        amount: int = 800,
        *,
        timeout_ms: int | None = None,
    ) -> CommandResult:
        return self._command(
            "scroll", {"direction": direction, "amount": amount}, timeout_ms=timeout_ms
        )

    def state(self, *, timeout_ms: int | None = None) -> CommandResult:
        return self._command("state", {}, timeout_ms=timeout_ms or 15000)

    def screenshot(self, *, full: bool = False, timeout_ms: int | None = None) -> CommandResult:
        return self._command(
            "screenshot", {"full": full} if full else {}, timeout_ms=timeout_ms
        )

    def get(
        self,
        field: Literal["title", "html", "text", "value", "attributes", "bbox"],
        *,
        selector: str | None = None,
        index: int | None = None,
        timeout_ms: int | None = None,
    ) -> CommandResult:
        args: dict[str, Any] = {"field": field}
        if selector is not None:
            args["selector"] = selector
        if index is not None:
            args["index"] = index
        return self._command("get", args, timeout_ms=timeout_ms)

    def get_title(self, *, timeout_ms: int | None = None) -> str:
        return self.get("title", timeout_ms=timeout_ms).result["value"]

    def click(self, index: int, *, timeout_ms: int | None = None) -> CommandResult:
        return self._command("click", {"index": index}, timeout_ms=timeout_ms)

    def click_xy(self, x: float, y: float, *, timeout_ms: int | None = None) -> CommandResult:
        return self._command("click", {"x": x, "y": y}, timeout_ms=timeout_ms)

    def type_text(self, text: str, *, timeout_ms: int | None = None) -> CommandResult:
        return self._command("type", {"text": text}, timeout_ms=timeout_ms)

    def input(self, index: int, text: str, *, timeout_ms: int | None = None) -> CommandResult:
        return self._command("input", {"index": index, "text": text}, timeout_ms=timeout_ms)

    def keys(self, keys: str, *, timeout_ms: int | None = None) -> CommandResult:
        return self._command("keys", {"keys": keys}, timeout_ms=timeout_ms)

    def select(self, index: int, text: str, *, timeout_ms: int | None = None) -> CommandResult:
        return self._command("select", {"index": index, "text": text}, timeout_ms=timeout_ms)

    def hover(self, index: int, *, timeout_ms: int | None = None) -> CommandResult:
        return self._command("hover", {"index": index}, timeout_ms=timeout_ms)

    def dblclick(self, index: int, *, timeout_ms: int | None = None) -> CommandResult:
        return self._command("dblclick", {"index": index}, timeout_ms=timeout_ms)

    def rightclick(self, index: int, *, timeout_ms: int | None = None) -> CommandResult:
        return self._command("rightclick", {"index": index}, timeout_ms=timeout_ms)

    def tab_list(self, *, timeout_ms: int | None = None) -> list[dict[str, Any]]:
        return self._command("tab", {"action": "list"}, timeout_ms=timeout_ms).result["tabs"]

    def tab_new(self, url: str | None = None, *, timeout_ms: int | None = None) -> CommandResult:
        args: dict[str, Any] = {"action": "new"}
        if url:
            args["url"] = url
        return self._command("tab", args, timeout_ms=timeout_ms or 15000)

    def tab_switch(self, index: int, *, timeout_ms: int | None = None) -> CommandResult:
        return self._command("tab", {"action": "switch", "index": index}, timeout_ms=timeout_ms)

    def tab_close(self, index: int, *, timeout_ms: int | None = None) -> CommandResult:
        return self._command("tab", {"action": "close", "index": index}, timeout_ms=timeout_ms)

    def wait_selector(
        self, selector: str, *, timeout_ms: int = 5000, command_timeout_ms: int | None = None
    ) -> CommandResult:
        return self._command(
            "wait",
            {"type": "selector", "selector": selector, "timeout_ms": timeout_ms},
            timeout_ms=command_timeout_ms or max(timeout_ms + 5000, self.default_timeout_ms),
        )

    def wait_text(
        self, text: str, *, timeout_ms: int = 5000, command_timeout_ms: int | None = None
    ) -> CommandResult:
        return self._command(
            "wait",
            {"type": "text", "text": text, "timeout_ms": timeout_ms},
            timeout_ms=command_timeout_ms or max(timeout_ms + 5000, self.default_timeout_ms),
        )

    def eval(self, code: str, *, dangerous: bool = True, timeout_ms: int | None = None) -> CommandResult:
        return self._command(
            "eval", {"code": code, "dangerous": dangerous}, timeout_ms=timeout_ms
        )

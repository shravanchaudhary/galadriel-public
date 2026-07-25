"""Shared protocol and session types for the phone bridge."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime

from fastapi import WebSocket


class DeviceUnavailable(RuntimeError):
    """Raised when no phone can service a local TCP connection."""


class StreamUnavailable(RuntimeError):
    """Raised when a requested stream cannot be attached."""


@dataclass
class StreamSession:
    """A stream WebSocket and the signal keeping its route alive."""

    websocket: WebSocket
    device_id: str
    closed: asyncio.Event = field(default_factory=asyncio.Event)

    def finish(self) -> None:
        self.closed.set()


@dataclass(frozen=True)
class ControlSession:
    """Authenticated phone control session."""

    websocket: WebSocket
    tenant_id: str
    device_id: str
    expires_at: datetime


@dataclass
class PendingStream:
    """Single-use authorization for a requested stream WebSocket."""

    device_id: str
    token: str
    future: asyncio.Future[StreamSession]

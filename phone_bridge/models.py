"""Shared protocol types for the phone bridge."""

from dataclasses import dataclass, field
import asyncio

from fastapi import WebSocket


class DeviceUnavailable(RuntimeError):
    """Raised when no phone can service a local TCP connection."""


class StreamUnavailable(RuntimeError):
    """Raised when a requested stream cannot be attached."""


@dataclass
class StreamSession:
    """A stream WebSocket and the signal keeping its route alive."""

    websocket: WebSocket
    closed: asyncio.Event = field(default_factory=asyncio.Event)

    def finish(self) -> None:
        self.closed.set()

"""Single-device control and stream coordination."""

import asyncio
from datetime import datetime, timezone
import logging
import secrets
import threading

from fastapi import WebSocket

from .auth import AuthStore
from .models import (
    ControlSession,
    DeviceUnavailable,
    PendingStream,
    StreamSession,
    StreamUnavailable,
)

log = logging.getLogger("galadriel.phone_bridge.registry")
_snapshot_lock = threading.Lock()
_live_snapshot: dict = {
    "connected": False,
    "device_id": None,
    "tenant_id": None,
    "session_expires_at": None,
    "stream_active": False,
}


def live_phone_snapshot(tenant_id: str | None = None) -> dict:
    """Return a cross-thread-safe, serializable view of the live phone."""
    with _snapshot_lock:
        snapshot = dict(_live_snapshot)
    if tenant_id and snapshot.get("tenant_id") not in {None, tenant_id}:
        return {
            "connected": False,
            "device_id": None,
            "session_expires_at": None,
            "stream_active": False,
        }
    snapshot.pop("tenant_id", None)
    return snapshot


class DeviceRegistry:
    """Coordinates one phone and one active ADB transport."""

    def __init__(self, auth_store: AuthStore, stream_timeout: float = 10.0):
        self.auth_store = auth_store
        self.stream_timeout = stream_timeout
        self._control: ControlSession | None = None
        self._control_send_lock = asyncio.Lock()
        self._lock = asyncio.Lock()
        self._pending: dict[str, PendingStream] = {}
        self._stream_active = False
        self._active_session: StreamSession | None = None

    def _publish_snapshot(self) -> None:
        control = self._control
        snapshot = {
            "connected": control is not None,
            "device_id": control.device_id if control else None,
            "tenant_id": control.tenant_id if control else None,
            "session_expires_at": (
                control.expires_at.isoformat().replace("+00:00", "Z")
                if control
                else None
            ),
            "stream_active": bool(control and self._stream_active),
        }
        with _snapshot_lock:
            _live_snapshot.update(snapshot)

    async def register_control(self, session: ControlSession) -> None:
        async with self._lock:
            previous = self._control
            self._control = session
            pending = []
            active_session = None
            if previous is not None and previous.websocket is not session.websocket:
                pending = [item.future for item in self._pending.values()]
                self._pending.clear()
                active_session = self._active_session
                if active_session is None:
                    self._stream_active = False
            self._publish_snapshot()
        for future in pending:
            if not future.done():
                future.set_exception(DeviceUnavailable("Phone connection replaced"))
        if active_session is not None:
            active_session.finish()
        if previous is not None and previous.websocket is not session.websocket:
            await previous.websocket.close(
                code=1012,
                reason="Replaced by a new phone connection",
            )
        log.info("Authenticated phone control socket connected")

    async def unregister_control(self, websocket: WebSocket) -> None:
        async with self._lock:
            if self._control is None or self._control.websocket is not websocket:
                return
            self._control = None
            pending = [item.future for item in self._pending.values()]
            self._pending.clear()
            active_session = self._active_session
            if active_session is None:
                self._stream_active = False
            self._publish_snapshot()
        for future in pending:
            if not future.done():
                future.set_exception(DeviceUnavailable("Phone disconnected"))
        if active_session is not None:
            active_session.finish()
        log.info("Authenticated phone control socket disconnected")

    async def request_stream(self, stream_id: str) -> StreamSession:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[StreamSession] = loop.create_future()
        async with self._lock:
            control = self._control
            if control is None:
                raise DeviceUnavailable("No phone is connected")
            if not self._control_authorized(control):
                raise DeviceUnavailable("Phone session is expired or revoked")
            if self._stream_active:
                raise DeviceUnavailable("The phone already has an active ADB stream")
            self._stream_active = True
            token = secrets.token_urlsafe(32)
            self._pending[stream_id] = PendingStream(
                device_id=control.device_id,
                token=token,
                future=future,
            )
            self._publish_snapshot()

        try:
            async with self._control_send_lock:
                await control.websocket.send_json({
                    "type": "open_stream",
                    "stream_id": stream_id,
                    "stream_token": token,
                })
            return await asyncio.wait_for(future, timeout=self.stream_timeout)
        except TimeoutError as exc:
            raise StreamUnavailable("Phone did not open the requested stream") from exc
        except Exception:
            raise
        finally:
            async with self._lock:
                self._pending.pop(stream_id, None)
                attached = (
                    future.done()
                    and not future.cancelled()
                    and future.exception() is None
                )
                if not attached:
                    self._stream_active = False
                    self._publish_snapshot()

    async def attach_stream(
        self,
        stream_id: str,
        stream_token: str,
        websocket: WebSocket,
    ) -> StreamSession:
        async with self._lock:
            pending = self._pending.get(stream_id)
            control = self._control
            if (
                pending is None
                or pending.future.done()
                or control is None
                or pending.device_id != control.device_id
                or not secrets.compare_digest(pending.token, stream_token)
                or not self._control_authorized(control)
            ):
                raise StreamUnavailable("Unknown or expired stream")
            self._pending.pop(stream_id, None)
            session = StreamSession(
                websocket=websocket,
                device_id=control.device_id,
            )
            self._active_session = session
            pending.future.set_result(session)
            self._publish_snapshot()
            return session

    async def release_stream(self) -> None:
        async with self._lock:
            self._stream_active = False
            self._active_session = None
            self._publish_snapshot()

    def _control_authorized(self, control: ControlSession) -> bool:
        return (
            datetime.now(timezone.utc) < control.expires_at
            and self.auth_store.device_active(
                control.tenant_id,
                control.device_id,
            )
        )

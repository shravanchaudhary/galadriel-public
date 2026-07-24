"""Single-device control and stream coordination."""

import asyncio
import logging

from fastapi import WebSocket

from .models import DeviceUnavailable, StreamSession, StreamUnavailable

log = logging.getLogger("galadriel.phone_bridge.registry")


class DeviceRegistry:
    """Coordinates one phone and one active ADB transport."""

    def __init__(self, stream_timeout: float = 10.0):
        self.stream_timeout = stream_timeout
        self._control: WebSocket | None = None
        self._control_send_lock = asyncio.Lock()
        self._lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[StreamSession]] = {}
        self._stream_active = False
        self._active_session: StreamSession | None = None

    async def register_control(self, websocket: WebSocket) -> None:
        async with self._lock:
            previous = self._control
            self._control = websocket
        if previous is not None and previous is not websocket:
            await previous.close(code=1012, reason="Replaced by a new phone connection")
        log.info("Phone control socket connected")

    async def unregister_control(self, websocket: WebSocket) -> None:
        async with self._lock:
            if self._control is not websocket:
                return
            self._control = None
            pending = list(self._pending.values())
            self._pending.clear()
            active_session = self._active_session
            if active_session is None:
                self._stream_active = False
        for future in pending:
            if not future.done():
                future.set_exception(DeviceUnavailable("Phone disconnected"))
        if active_session is not None:
            active_session.finish()
        log.info("Phone control socket disconnected")

    async def request_stream(self, stream_id: str) -> StreamSession:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[StreamSession] = loop.create_future()
        async with self._lock:
            control = self._control
            if control is None:
                raise DeviceUnavailable("No phone is connected")
            if self._stream_active:
                raise DeviceUnavailable("The phone already has an active ADB stream")
            self._stream_active = True
            self._pending[stream_id] = future

        try:
            async with self._control_send_lock:
                await control.send_json({
                    "type": "open_stream",
                    "stream_id": stream_id,
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

    async def attach_stream(
        self, stream_id: str, websocket: WebSocket
    ) -> StreamSession:
        async with self._lock:
            future = self._pending.get(stream_id)
            if future is None or future.done():
                raise StreamUnavailable("Unknown or expired stream")
            session = StreamSession(websocket=websocket)
            self._active_session = session
            future.set_result(session)
            return session

    async def release_stream(self) -> None:
        async with self._lock:
            self._stream_active = False
            self._active_session = None

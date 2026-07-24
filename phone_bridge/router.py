"""FastAPI WebSocket routes for phone control and byte streams."""

import contextlib
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from .device_registry import DeviceRegistry
from .models import StreamUnavailable

log = logging.getLogger("galadriel.phone_bridge.router")


def create_router(registry: DeviceRegistry) -> APIRouter:
    router = APIRouter()

    @router.websocket("/phone/control")
    async def phone_control(websocket: WebSocket) -> None:
        await websocket.accept()
        await registry.register_control(websocket)
        try:
            while True:
                message = await websocket.receive_json()
                if message.get("type") != "hello":
                    log.info("Ignoring unknown phone control message")
        except WebSocketDisconnect:
            pass
        finally:
            await registry.unregister_control(websocket)

    @router.websocket("/phone/stream/{stream_id}")
    async def phone_stream(websocket: WebSocket, stream_id: str) -> None:
        await websocket.accept()
        try:
            session = await registry.attach_stream(stream_id, websocket)
        except StreamUnavailable:
            await websocket.close(code=1008, reason="Unknown or expired stream")
            return

        try:
            await session.closed.wait()
        finally:
            if websocket.client_state is not WebSocketState.DISCONNECTED:
                with contextlib.suppress(RuntimeError):
                    await websocket.close()

    return router

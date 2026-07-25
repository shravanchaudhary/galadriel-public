"""FastAPI WebSocket routes for phone control and byte streams."""

import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
import logging
import secrets

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from .auth import AuthStore
from .device_registry import DeviceRegistry
from .models import ControlSession, StreamUnavailable

log = logging.getLogger("galadriel.phone_bridge.router")


def create_router(
    registry: DeviceRegistry,
    auth_store: AuthStore,
    tenant_id: str,
    *,
    session_seconds: int = 3600,
) -> APIRouter:
    router = APIRouter()

    @router.get("/phone/healthz")
    async def phone_health() -> dict[str, str]:
        return {"status": "ok"}

    @router.websocket("/phone/control")
    async def phone_control(websocket: WebSocket) -> None:
        await websocket.accept()
        nonce = secrets.token_urlsafe(32)
        await websocket.send_json({
            "type": "auth_challenge",
            "nonce": nonce,
        })
        try:
            message = await asyncio.wait_for(
                websocket.receive_json(),
                timeout=15,
            )
            device_id = _authenticate_message(
                auth_store,
                tenant_id,
                nonce,
                message,
            )
        except WebSocketDisconnect:
            return
        except (TimeoutError, ValueError, KeyError, TypeError):
            await websocket.close(code=1008, reason="Authentication failed")
            return

        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=session_seconds
        )
        session = ControlSession(
            websocket=websocket,
            tenant_id=tenant_id,
            device_id=device_id,
            expires_at=expires_at,
        )
        await registry.register_control(session)
        try:
            await websocket.send_json({
                "type": "authenticated",
                "device_id": device_id,
                "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
            })
            while True:
                if not auth_store.device_active(tenant_id, device_id):
                    await websocket.close(code=1008, reason="Device revoked")
                    break
                remaining = (
                    expires_at - datetime.now(timezone.utc)
                ).total_seconds()
                if remaining <= 0:
                    await websocket.close(code=1008, reason="Session expired")
                    break
                try:
                    message = await asyncio.wait_for(
                        websocket.receive_json(),
                        timeout=min(5.0, remaining),
                    )
                except TimeoutError:
                    continue
                if message.get("type") not in {"hello", "ping"}:
                    log.info("Ignoring unknown phone control message")
        except WebSocketDisconnect:
            pass
        finally:
            await registry.unregister_control(websocket)

    @router.websocket("/phone/stream/{stream_id}")
    async def phone_stream(websocket: WebSocket, stream_id: str) -> None:
        await websocket.accept()
        stream_token = websocket.headers.get("x-phone-stream-token", "")
        try:
            session = await registry.attach_stream(
                stream_id,
                stream_token,
                websocket,
            )
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


def _authenticate_message(
    auth_store: AuthStore,
    tenant_id: str,
    nonce: str,
    message: dict,
) -> str:
    message_type = message.get("type")
    if message_type == "enroll":
        device_id = auth_store.enroll(
            tenant_id,
            str(message["code"]),
            str(message["public_key"]),
            nonce,
            str(message["signature"]),
        )
        return device_id
    if message_type == "authenticate":
        device_id = str(message["device_id"])
        auth_store.authenticate(
            tenant_id,
            device_id,
            nonce,
            str(message["signature"]),
        )
        return device_id
    raise ValueError("Unsupported authentication message")

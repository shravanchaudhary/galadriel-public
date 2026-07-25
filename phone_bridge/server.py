"""ASGI server lifecycle for the in-process phone bridge."""

from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path
import threading

from fastapi import FastAPI
import uvicorn

from .auth import current_tenant_id, get_auth_store
from .device_registry import DeviceRegistry
from .router import create_router
from .tcp_listener import TcpListener

log = logging.getLogger("galadriel.phone_bridge")
_server_thread: threading.Thread | None = None


def create_app(
    *,
    tcp_port: int = 37000,
    stream_timeout: float = 10.0,
    session_seconds: int = 3600,
    auth_store_path: Path | None = None,
    tenant_id: str | None = None,
) -> FastAPI:
    auth_store = get_auth_store(auth_store_path)
    tenant_id = tenant_id or current_tenant_id()
    registry = DeviceRegistry(
        auth_store=auth_store,
        stream_timeout=stream_timeout,
    )
    listener = TcpListener(registry=registry, port=tcp_port)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await listener.start()
        try:
            yield
        finally:
            await listener.stop()

    app = FastAPI(title="Phone Agent Bridge", lifespan=lifespan)
    app.include_router(
        create_router(
            registry,
            auth_store,
            tenant_id,
            session_seconds=session_seconds,
        )
    )
    app.state.phone_registry = registry
    app.state.phone_tcp_listener = listener
    app.state.phone_auth_store = auth_store
    return app


def start_phone_bridge() -> threading.Thread:
    """Start Uvicorn and the local ADB listener in a daemon thread."""
    global _server_thread
    if _server_thread is not None and _server_thread.is_alive():
        return _server_thread

    host = os.environ.get("PHONE_BRIDGE_WS_HOST", "0.0.0.0")
    port = int(os.environ.get("PHONE_BRIDGE_WS_PORT", "8765"))
    tcp_port = int(os.environ.get("PHONE_BRIDGE_TCP_PORT", "37000"))
    stream_timeout = float(
        os.environ.get("PHONE_BRIDGE_STREAM_TIMEOUT_SECONDS", "10")
    )
    session_seconds = int(
        os.environ.get("PHONE_BRIDGE_SESSION_SECONDS", "3600")
    )
    app = create_app(
        tcp_port=tcp_port,
        stream_timeout=stream_timeout,
        session_seconds=session_seconds,
    )

    def run() -> None:
        log.info("Phone WebSocket bridge starting on %s:%d", host, port)
        uvicorn.run(app, host=host, port=port, access_log=False)

    _server_thread = threading.Thread(
        target=run,
        name="phone-bridge",
        daemon=True,
    )
    _server_thread.start()
    return _server_thread

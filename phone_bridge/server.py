"""ASGI server lifecycle for the in-process phone bridge."""

from contextlib import asynccontextmanager
import logging
import os
import threading

from fastapi import FastAPI
import uvicorn

from .device_registry import DeviceRegistry
from .router import create_router
from .tcp_listener import TcpListener

log = logging.getLogger("galadriel.phone_bridge")
_server_thread: threading.Thread | None = None


def create_app(
    *,
    tcp_port: int = 37000,
    stream_timeout: float = 10.0,
) -> FastAPI:
    registry = DeviceRegistry(stream_timeout=stream_timeout)
    listener = TcpListener(registry=registry, port=tcp_port)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await listener.start()
        try:
            yield
        finally:
            await listener.stop()

    app = FastAPI(title="Phone Agent Bridge", lifespan=lifespan)
    app.include_router(create_router(registry))
    app.state.phone_registry = registry
    app.state.phone_tcp_listener = listener
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
    app = create_app(tcp_port=tcp_port, stream_timeout=stream_timeout)

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

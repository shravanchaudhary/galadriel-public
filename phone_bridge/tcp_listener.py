"""Local TCP listener and bidirectional byte proxy."""

import asyncio
import contextlib
import logging
import secrets

from fastapi import WebSocketDisconnect

from .device_registry import DeviceRegistry
from .models import DeviceUnavailable, StreamSession, StreamUnavailable

log = logging.getLogger("galadriel.phone_bridge.tcp")


class TcpListener:
    def __init__(
        self,
        registry: DeviceRegistry,
        host: str = "127.0.0.1",
        port: int = 37000,
    ):
        if host != "127.0.0.1":
            raise ValueError("The phone bridge TCP listener must bind to 127.0.0.1")
        self.registry = registry
        self.host = host
        self.port = port
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self.handle_connection,
            host=self.host,
            port=self.port,
        )
        log.info("Phone ADB listener started on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        stream_id = secrets.token_urlsafe(16)
        session: StreamSession | None = None
        try:
            session = await self.registry.request_stream(stream_id)
            await self._proxy(reader, writer, session)
        except (DeviceUnavailable, StreamUnavailable) as exc:
            log.info("Rejected local ADB connection: %s", exc)
        except Exception:
            log.exception("ADB tunnel failed")
        finally:
            if session is not None:
                session.finish()
                await self.registry.release_stream()
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def _proxy(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        session: StreamSession,
    ) -> None:
        websocket = session.websocket

        async def tcp_to_websocket() -> None:
            while data := await reader.read(64 * 1024):
                await websocket.send_bytes(data)

        async def websocket_to_tcp() -> None:
            try:
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    data = message.get("bytes")
                    if data is None:
                        continue
                    writer.write(data)
                    await writer.drain()
            except WebSocketDisconnect:
                return

        tasks = {
            asyncio.create_task(tcp_to_websocket()),
            asyncio.create_task(websocket_to_tcp()),
        }
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()

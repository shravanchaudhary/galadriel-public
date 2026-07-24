#!/usr/bin/env python3
"""End-to-end protocol checks for the Stage 1 phone bridge."""

import asyncio
import json
import os
import socket
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import uvicorn
import websockets
from websockets.exceptions import ConnectionClosedError

from phone_bridge.server import create_app


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def wait_for_server(server: uvicorn.Server) -> None:
    for _ in range(100):
        if server.started:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("Uvicorn did not start")


async def expect_tcp_close(port: int) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        assert await asyncio.wait_for(reader.read(1), timeout=1) == b""
    finally:
        writer.close()
        await writer.wait_closed()


async def run_checks() -> None:
    websocket_port = unused_port()
    tcp_port = unused_port()
    app = create_app(tcp_port=tcp_port, stream_timeout=0.15)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=websocket_port,
            log_level="critical",
        )
    )
    server_task = asyncio.create_task(server.serve())
    await wait_for_server(server)

    base_url = f"ws://127.0.0.1:{websocket_port}"
    try:
        # No control socket: local ADB connections fail closed.
        await expect_tcp_close(tcp_port)

        # Stream URLs cannot be guessed or attached after expiry.
        async with websockets.connect(
            f"{base_url}/phone/stream/not-pending"
        ) as unknown_stream:
            try:
                await unknown_stream.recv()
                raise AssertionError("Unknown stream remained open")
            except ConnectionClosedError as exc:
                assert exc.rcvd is not None and exc.rcvd.code == 1008

        async with websockets.connect(f"{base_url}/phone/control") as control:
            await control.send(json.dumps({
                "type": "hello",
                "adb_available": True,
            }))

            # A phone that does not attach times out, and the active slot is reset.
            first_reader, first_writer = await asyncio.open_connection(
                "127.0.0.1", tcp_port
            )
            first_request = json.loads(await control.recv())
            assert first_request["type"] == "open_stream"
            assert await asyncio.wait_for(first_reader.read(1), timeout=1) == b""
            first_writer.close()
            await first_writer.wait_closed()

            # The next connection gets a fresh stream and transports opaque bytes.
            tcp_reader, tcp_writer = await asyncio.open_connection(
                "127.0.0.1", tcp_port
            )
            request = json.loads(await control.recv())
            assert request["type"] == "open_stream"
            assert request["stream_id"] != first_request["stream_id"]

            async with websockets.connect(
                f"{base_url}/phone/stream/{request['stream_id']}"
            ) as stream:
                tcp_writer.write(b"backend-to-phone")
                await tcp_writer.drain()
                assert await asyncio.wait_for(stream.recv(), timeout=1) == (
                    b"backend-to-phone"
                )

                await stream.send(b"phone-to-backend")
                assert await asyncio.wait_for(
                    tcp_reader.readexactly(len(b"phone-to-backend")),
                    timeout=1,
                ) == b"phone-to-backend"

            tcp_writer.close()
            await tcp_writer.wait_closed()
    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=2)


if __name__ == "__main__":
    asyncio.run(run_checks())
    print("phone bridge tests passed")

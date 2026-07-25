#!/usr/bin/env python3
"""End-to-end security and transport checks for the phone bridge."""

import asyncio
import base64
from datetime import datetime
import json
import os
from pathlib import Path
import socket
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
import uvicorn
import websockets
from websockets.exceptions import ConnectionClosedError

from phone_bridge.auth import challenge_payload, get_auth_store
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


def device_credentials() -> tuple[ec.EllipticCurvePrivateKey, str]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_key, base64.b64encode(public_key).decode("ascii")


def sign(private_key: ec.EllipticCurvePrivateKey, nonce: str) -> str:
    signature = private_key.sign(
        challenge_payload(nonce),
        ec.ECDSA(hashes.SHA256()),
    )
    return base64.b64encode(signature).decode("ascii")


async def authenticate(
    control,
    private_key: ec.EllipticCurvePrivateKey,
    *,
    device_id: str | None = None,
    code: str | None = None,
    public_key: str | None = None,
) -> dict:
    challenge = json.loads(await control.recv())
    assert challenge["type"] == "auth_challenge"
    message = {
        "type": "authenticate" if device_id else "enroll",
        "signature": sign(private_key, challenge["nonce"]),
    }
    if device_id:
        message["device_id"] = device_id
    else:
        message.update({"code": code, "public_key": public_key})
    await control.send(json.dumps(message))
    result = json.loads(await control.recv())
    assert result["type"] == "authenticated"
    datetime.fromisoformat(result["expires_at"].replace("Z", "+00:00"))
    return result


async def expect_policy_close(websocket) -> None:
    try:
        await websocket.recv()
        raise AssertionError("Rejected WebSocket remained open")
    except ConnectionClosedError as exc:
        assert exc.rcvd is not None and exc.rcvd.code == 1008


async def run_checks_in(directory: Path) -> None:
    websocket_port = unused_port()
    tcp_port = unused_port()
    auth_path = directory / "phone_auth.json"
    tenant_id = "test-tenant"
    auth_store = get_auth_store(auth_path)
    private_key, public_key = device_credentials()
    expired_code, _ = auth_store.create_enrollment_code(
        tenant_id,
        ttl_seconds=-1,
    )
    try:
        auth_store.enroll(
            tenant_id,
            expired_code,
            public_key,
            "expired",
            sign(private_key, "expired"),
        )
        raise AssertionError("Expired enrollment code was accepted")
    except ValueError:
        pass
    code, _ = auth_store.create_enrollment_code(tenant_id)
    app = create_app(
        tcp_port=tcp_port,
        stream_timeout=0.15,
        session_seconds=2,
        auth_store_path=auth_path,
        tenant_id=tenant_id,
    )
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
            await expect_policy_close(unknown_stream)

        async with websockets.connect(f"{base_url}/phone/control") as control:
            authenticated = await authenticate(
                control,
                private_key,
                code=code,
                public_key=public_key,
            )
            device_id = authenticated["device_id"]
            wrong_key, _ = device_credentials()
            for rejected_tenant, rejected_signature in [
                ("other-tenant", sign(private_key, "direct-check")),
                (tenant_id, sign(wrong_key, "direct-check")),
            ]:
                try:
                    auth_store.authenticate(
                        rejected_tenant,
                        device_id,
                        "direct-check",
                        rejected_signature,
                    )
                    raise AssertionError("Invalid device authentication was accepted")
                except ValueError:
                    pass

            # Enrollment codes are single-use.
            async with websockets.connect(f"{base_url}/phone/control") as replay:
                challenge = json.loads(await replay.recv())
                await replay.send(json.dumps({
                    "type": "enroll",
                    "code": code,
                    "public_key": public_key,
                    "signature": sign(private_key, challenge["nonce"]),
                }))
                await expect_policy_close(replay)

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

            # The stream ID alone, or a wrong token, cannot attach.
            async with websockets.connect(
                f"{base_url}/phone/stream/{request['stream_id']}",
                additional_headers={"X-Phone-Stream-Token": "wrong"},
            ) as wrong_stream:
                await expect_policy_close(wrong_stream)

            # A wrong token did not consume the real single-use authorization.
            async with websockets.connect(
                f"{base_url}/phone/stream/{request['stream_id']}",
                additional_headers={
                    "X-Phone-Stream-Token": request["stream_token"]
                },
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

            # Stream authorization is consumed and cannot be replayed.
            async with websockets.connect(
                f"{base_url}/phone/stream/{request['stream_id']}",
                additional_headers={
                    "X-Phone-Stream-Token": request["stream_token"]
                },
            ) as replayed_stream:
                await expect_policy_close(replayed_stream)

            # The backend independently expires an enabled session.
            await expect_policy_close(control)
        await expect_tcp_close(tcp_port)

        # Reconnect with a fresh nonce and then revoke the persisted device.
        async with websockets.connect(f"{base_url}/phone/control") as control:
            await authenticate(control, private_key, device_id=device_id)
            assert auth_store.revoke_device(tenant_id, device_id)
            await expect_policy_close(control)
        await expect_tcp_close(tcp_port)
    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=2)


async def run_checks() -> None:
    with tempfile.TemporaryDirectory() as directory:
        await run_checks_in(Path(directory))


if __name__ == "__main__":
    asyncio.run(run_checks())
    print("phone bridge tests passed")

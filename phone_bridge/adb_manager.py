"""Bounded, serialized ADB subprocess access for phone agent tools."""

from __future__ import annotations

import asyncio
import os

DEFAULT_SERIAL = "127.0.0.1:37000"
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_OUTPUT_BYTES = 1_000_000


class AdbError(RuntimeError):
    pass


class AdbManager:
    def __init__(
        self,
        *,
        serial: str = DEFAULT_SERIAL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ):
        self.serial = serial
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        self._lock = asyncio.Lock()

    async def run(self, *args: str) -> str:
        if not args or any(not isinstance(value, str) for value in args):
            raise ValueError("ADB arguments must be non-empty strings")
        async with self._lock:
            connect_output = await self._run_process("connect", self.serial)
            lowered = connect_output.lower()
            if "connected to" not in lowered and "already connected" not in lowered:
                raise AdbError(connect_output.strip() or "Could not connect to phone")
            return await self._run_process("-s", self.serial, *args)

    async def _run_process(self, *args: str) -> str:
        process = await asyncio.create_subprocess_exec(
            os.environ.get("ADB_BINARY", "adb"),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_task = asyncio.create_task(
            _read_bounded(process.stdout, self.max_output_bytes)
        )
        stderr_task = asyncio.create_task(
            _read_bounded(process.stderr, self.max_output_bytes)
        )
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=self.timeout_seconds,
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise AdbError(
                f"ADB command timed out after {self.timeout_seconds:g}s"
            ) from exc
        stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
        output = stdout.decode(errors="replace")
        error = stderr.decode(errors="replace")
        if process.returncode != 0:
            raise AdbError(error.strip() or output.strip() or "ADB command failed")
        if stderr:
            output = f"{output}\n{error}" if output else error
        return output


async def _read_bounded(
    stream: asyncio.StreamReader,
    maximum: int,
) -> bytes:
    retained = bytearray()
    truncated = False
    while chunk := await stream.read(64 * 1024):
        remaining = maximum - len(retained)
        if remaining > 0:
            retained.extend(chunk[:remaining])
        if len(chunk) > max(remaining, 0):
            truncated = True
    if truncated:
        retained.extend(b"\n[output truncated]\n")
    return bytes(retained)


_manager: AdbManager | None = None


def get_adb_manager() -> AdbManager:
    global _manager
    if _manager is None:
        _manager = AdbManager(
            serial=os.environ.get("PHONE_BRIDGE_ADB_SERIAL", DEFAULT_SERIAL),
            timeout_seconds=float(
                os.environ.get("PHONE_BRIDGE_ADB_TIMEOUT_SECONDS", "20")
            ),
            max_output_bytes=int(
                os.environ.get("PHONE_BRIDGE_ADB_MAX_OUTPUT_BYTES", "1000000")
            ),
        )
    return _manager

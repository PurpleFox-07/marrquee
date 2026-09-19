"""Shared pytest fixtures for the marrquee test suite."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest


class RecordingDockerStub:
    """A tiny unix-socket server that records request lines and answers them.

    This is a recording mirror, not a real Docker Engine - it exists only to
    prove that `SocketDockerEngine` speaks HTTP correctly over a unix socket
    and issues exactly the request it claims to.
    """

    def __init__(self) -> None:
        self.request_lines: list[str] = []
        self._server: asyncio.AbstractServer | None = None
        self._response_body = b'{"Version": "27.3.1", "ApiVersion": "1.47"}'
        self._status_line = "HTTP/1.1 200 OK"

    def respond_with(self, status_line: str, body: bytes) -> None:
        """Configure the fixed reply the stub sends to the next connection."""
        self._status_line = status_line
        self._response_body = body

    def respond_with_json(
        self, payload: dict[str, str], status_line: str = "HTTP/1.1 200 OK"
    ) -> None:
        """Convenience wrapper for JSON replies."""
        self.respond_with(status_line, json.dumps(payload).encode())

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request_line = await reader.readline()
        self.request_lines.append(request_line.decode().strip())
        # Drain the rest of the request headers so the client's write completes
        # cleanly, without trying to parse or act on them.
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b""):
                break
        response = (
            f"{self._status_line}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(self._response_body)}\r\n"
            "\r\n"
        ).encode() + self._response_body
        writer.write(response)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def start(self, socket_path: Path) -> None:
        self._server = await asyncio.start_unix_server(self._handle, path=str(socket_path))

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


@pytest.fixture
async def docker_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[RecordingDockerStub, Path]]:
    """A running unix-socket stub Docker daemon, plus the socket path it bound.

    macOS unix-socket paths are limited to roughly 104 bytes, and pytest's
    `tmp_path` can live deep under `/private/var/folders/...`. Binding a short
    relative path after `chdir`-ing into `tmp_path` keeps the bound path
    comfortably under that limit regardless of where the test runs from.
    """
    monkeypatch.chdir(tmp_path)
    # Bind AND connect through this short, relative name rather than
    # tmp_path's absolute form: macOS caps AF_UNIX paths at roughly 104
    # bytes, and pytest's tmp_path routinely exceeds that on its own.
    relative_socket_path = Path("docker.sock")
    stub = RecordingDockerStub()
    await stub.start(relative_socket_path)
    try:
        yield stub, relative_socket_path
    finally:
        await stub.stop()

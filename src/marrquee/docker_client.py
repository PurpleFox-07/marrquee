"""The Docker seam: every later story's access to Docker goes through this.

`DockerEngine` has exactly one method, `status()`. That is not an oversight -
Story 1's safety rule is that nothing in this codebase may start, stop or
modify a container, and a read-only, one-method protocol is the enforceable
form of that rule rather than a promise in a comment. Later stories widen the
protocol; this one cannot.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import httpx


class DockerFailure(StrEnum):
    """The ways a Docker status check can fail, mapped to plain-language copy
    by the page that renders `DockerStatus` (see the story's Content
    Direction table).
    """

    SOCKET_MISSING = "socket_missing"
    PERMISSION_DENIED = "permission_denied"
    NO_ANSWER = "no_answer"
    BAD_RESPONSE = "bad_response"


@dataclass(frozen=True)
class DockerStatus:
    """The answer to "is Docker reachable, and what version is it?".

    `detail` carries the raw technical string (an exception message, an HTTP
    status code) for logs only. The template contract forbids rendering it -
    the copy the owner sees comes from `failure` alone, never from `detail`.
    """

    connected: bool
    version: str | None = None
    api_version: str | None = None
    failure: DockerFailure | None = None
    detail: str | None = None


class DockerEngine(Protocol):
    """Read-only access to Docker. One method, on purpose - see module docstring."""

    async def status(self) -> DockerStatus: ...


class SocketDockerEngine:
    """Talks to the real Docker Engine API over its unix socket.

    Uses httpx's `uds=` transport instead of a Docker SDK: the only call this
    story needs is `GET /version`, and a synchronous SDK client would block
    FastAPI's event loop for no benefit.
    """

    def __init__(self, socket_path: Path, timeout: float = 2.0) -> None:
        self._socket_path = socket_path
        self._timeout = timeout

    async def status(self) -> DockerStatus:
        try:
            socket_exists = self._socket_path.exists()
        except PermissionError as error:
            return DockerStatus(
                connected=False, failure=DockerFailure.PERMISSION_DENIED, detail=str(error)
            )

        # A missing socket is the commonest first-run state (the owner
        # hasn't added the mount yet, or removed it). Checking for it up
        # front turns that case into an instant answer instead of a
        # multi-second connection timeout.
        if not socket_exists:
            return DockerStatus(
                connected=False,
                failure=DockerFailure.SOCKET_MISSING,
                detail=f"{self._socket_path} does not exist",
            )

        transport = httpx.AsyncHTTPTransport(uds=str(self._socket_path))
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://docker", timeout=self._timeout
            ) as client:
                response = await client.get("/version")
        except PermissionError as error:
            return DockerStatus(
                connected=False, failure=DockerFailure.PERMISSION_DENIED, detail=str(error)
            )
        except (httpx.TimeoutException, httpx.ConnectError) as error:
            return DockerStatus(connected=False, failure=DockerFailure.NO_ANSWER, detail=str(error))

        return _parse_version_response(response)


def _parse_version_response(response: httpx.Response) -> DockerStatus:
    """Turn a `GET /version` response into a DockerStatus.

    Never trusts the daemon further than it has to: a non-200 status, a body
    that isn't JSON, or JSON missing `Version` are all treated the same way -
    the daemon answered, but not in a shape we understand.
    """
    if response.status_code != 200:
        return DockerStatus(
            connected=False,
            failure=DockerFailure.BAD_RESPONSE,
            detail=f"Docker replied with HTTP {response.status_code}",
        )

    try:
        payload = response.json()
    except ValueError as error:
        return DockerStatus(connected=False, failure=DockerFailure.BAD_RESPONSE, detail=str(error))

    version = payload.get("Version") if isinstance(payload, dict) else None
    if not isinstance(version, str):
        return DockerStatus(
            connected=False,
            failure=DockerFailure.BAD_RESPONSE,
            detail=f"Docker's /version reply had no 'Version' string: {payload!r}",
        )

    api_version = payload.get("ApiVersion") if isinstance(payload, dict) else None
    return DockerStatus(
        connected=True,
        version=version,
        api_version=api_version if isinstance(api_version, str) else None,
    )


class FakeDockerEngine:
    """An in-memory DockerEngine that always returns the status it was given.

    Exported from here (not a test file) so stories 2-6 can use it without
    reaching into this story's test suite.
    """

    def __init__(self, status: DockerStatus) -> None:
        self._status = status

    async def status(self) -> DockerStatus:
        return self._status

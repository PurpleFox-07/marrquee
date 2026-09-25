"""The Docker seam: every later story's access to Docker goes through this.

Story 1 gave `DockerEngine` exactly one method, `status()`, on purpose - a
read-only, one-method protocol was the enforceable form of "nothing in this
codebase starts, stops or modifies a container" until this story needed to.
This chunk widens the protocol with the operations the deploy engine needs
(inspecting and starting containers, joining a network, reading logs) while
keeping `status()` itself exactly as narrow as it always was - see its own
test below.
"""

from __future__ import annotations

import asyncio
import os
import socket
import struct
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

import httpx

# The Docker Engine API's container states, straight off `State.Status` in a
# `GET /containers/{name}/json` reply.
ContainerState = Literal["created", "running", "restarting", "exited", "paused", "dead", "removing"]

_DEFAULT_COMPOSE_BINARY = Path("/usr/local/bin/docker-compose")
_DEFAULT_TIMEOUT = 2.0
# `compose_up` for one app blocks for the whole of its own image pull - a
# ~200-300 MB linuxserver image, on a NAS's own (often slow, upstream-
# limited) internet connection, the first time it has ever been deployed.
# 300s (5 minutes) was tight enough to plausibly mistake a genuinely slow
# but working pull for a hang; 900s (15 minutes) is generous the same way
# `DeployManager.NEVER_READY_AFTER_SECONDS` is generous about a cold
# database migration - a real problem should still look like one well
# before this fires.
_DEFAULT_COMPOSE_TIMEOUT = 900.0


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

    `platform_name`, `os_type` and `kernel_version` come from the same
    `/version` reply - no extra request - and exist only so `detect_host_kind`
    can tell a Docker Desktop host apart from a real NAS or Linux box.
    """

    connected: bool
    version: str | None = None
    api_version: str | None = None
    failure: DockerFailure | None = None
    detail: str | None = None
    platform_name: str | None = None
    os_type: str | None = None
    kernel_version: str | None = None


@dataclass(frozen=True)
class ContainerSnapshot:
    """The answer to "does this container exist, and what is it doing?".

    `detail` is a raw technical string (a fetch error, an unrecognised
    reply), never rendered - it exists for logs and diagnostics only, same
    contract as `DockerStatus.detail`.

    `started_at` and `finished_at` come from the same inspect reply's
    `State.StartedAt` / `State.FinishedAt` - no extra call. They are
    optional and defaulted because most callers only need `state`.
    """

    name: str
    exists: bool
    state: ContainerState | None
    exit_code: int | None
    image: str | None
    detail: str | None
    started_at: str | None = None
    finished_at: str | None = None


@dataclass(frozen=True)
class ComposeResult:
    """The outcome of running the pinned `docker-compose` binary.

    `output` is combined stdout+stderr, for logs and the diagnostics file
    only - the same "raw technical text, never rendered directly" contract
    as every other `detail`-shaped field in this codebase.
    """

    ok: bool
    exit_code: int
    output: str


@dataclass(frozen=True)
class ContainerRemoveResult:
    """The outcome of asking Docker to force-remove one container.

    `detail` carries the status code and Docker's own message, for logs and
    the diagnostics file only - same contract as every other `detail`-shaped
    field in this codebase.
    """

    ok: bool
    detail: str | None


@dataclass(frozen=True)
class NetworkConnectResult:
    """The outcome of asking Docker to join our own container to a network.

    A bare bool can't say *why* a join failed - and "why" is exactly what
    turned one real deploy's failure into an undebuggable "docker
    unreachable" instead of "the network doesn't exist yet". `detail`
    carries the status code and Docker's own message, for logs and the
    diagnostics file only - same contract as every other `detail`-shaped
    field in this codebase.
    """

    ok: bool
    detail: str | None


class DockerEngine(Protocol):
    """Access to Docker: the original read-only status check, plus the
    read and write operations the deploy engine needs to start and watch
    the stack.
    """

    async def status(self) -> DockerStatus: ...
    async def inspect(self, name: str) -> ContainerSnapshot: ...
    async def image_present(self, reference: str) -> bool: ...
    async def connect_network(self, network: str, container: str) -> NetworkConnectResult: ...
    async def logs(self, name: str, tail: int = 50) -> str: ...
    async def compose_up(self, project: str, compose_file: Path, service: str) -> ComposeResult: ...
    async def self_container_id(self) -> str | None: ...
    async def remove_container(self, name: str) -> ContainerRemoveResult: ...


class SocketDockerEngine:
    """Talks to the real Docker Engine API over its unix socket.

    Uses httpx's `uds=` transport instead of a Docker SDK: every operation
    here is a single, simple HTTP request (or, for `compose_up`, a
    subprocess) and a synchronous SDK client would block FastAPI's event
    loop for no benefit.
    """

    def __init__(
        self,
        socket_path: Path,
        *,
        compose_binary: Path = _DEFAULT_COMPOSE_BINARY,
        timeout: float = _DEFAULT_TIMEOUT,
        compose_timeout: float = _DEFAULT_COMPOSE_TIMEOUT,
    ) -> None:
        self._socket_path = socket_path
        self._compose_binary = compose_binary
        self._timeout = timeout
        self._compose_timeout = compose_timeout

    def _client(self) -> httpx.AsyncClient:
        """A fresh client bound to our unix socket, one per request.

        Docker's socket has no keep-alive expectations worth pooling for at
        this call volume, and a fresh client per call is what the existing
        `status()` already did - every new method follows the same shape.
        """
        transport = httpx.AsyncHTTPTransport(uds=str(self._socket_path))
        return httpx.AsyncClient(
            transport=transport, base_url="http://docker", timeout=self._timeout
        )

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

        try:
            async with self._client() as client:
                response = await client.get("/version")
        except PermissionError as error:
            return DockerStatus(
                connected=False, failure=DockerFailure.PERMISSION_DENIED, detail=str(error)
            )
        except (httpx.TimeoutException, httpx.ConnectError) as error:
            return DockerStatus(connected=False, failure=DockerFailure.NO_ANSWER, detail=str(error))

        return _parse_version_response(response)

    async def inspect(self, name: str) -> ContainerSnapshot:
        try:
            async with self._client() as client:
                response = await client.get(f"/containers/{name}/json")
        except (httpx.TimeoutException, httpx.ConnectError) as error:
            return ContainerSnapshot(
                name=name, exists=False, state=None, exit_code=None, image=None, detail=str(error)
            )

        # A 404 is Docker's normal answer for "no container by that name" -
        # this is also the name-clash pre-check, so it has to be a positive
        # result rather than an error.
        if response.status_code == 404:
            return ContainerSnapshot(
                name=name, exists=False, state=None, exit_code=None, image=None, detail=None
            )
        if response.status_code != 200:
            return ContainerSnapshot(
                name=name,
                exists=False,
                state=None,
                exit_code=None,
                image=None,
                detail=f"Docker replied with HTTP {response.status_code}",
            )

        try:
            payload = response.json()
        except ValueError as error:
            return ContainerSnapshot(
                name=name, exists=False, state=None, exit_code=None, image=None, detail=str(error)
            )

        return _parse_inspect_payload(name, payload)

    async def image_present(self, reference: str) -> bool:
        try:
            async with self._client() as client:
                response = await client.get(f"/images/{reference}/json")
        except (httpx.TimeoutException, httpx.ConnectError):
            return False
        return response.status_code == 200

    async def connect_network(self, network: str, container: str) -> NetworkConnectResult:
        try:
            async with self._client() as client:
                response = await client.post(
                    f"/networks/{network}/connect", json={"Container": container}
                )
        except (httpx.TimeoutException, httpx.ConnectError) as error:
            return NetworkConnectResult(ok=False, detail=str(error))

        if response.status_code == 200:
            return NetworkConnectResult(ok=True, detail=None)
        # The Engine API's own docs say "a network cannot be re-attached to
        # a running container" - already-connected is an error response, so
        # idempotence has to be recognised here rather than assumed away.
        if response.status_code in (403, 409) and _reports_already_connected(response):
            return NetworkConnectResult(ok=True, detail=None)
        return NetworkConnectResult(
            ok=False,
            detail=f"HTTP {response.status_code}: {_docker_error_message(response)}",
        )

    async def logs(self, name: str, tail: int = 50) -> str:
        query = f"stdout=1&stderr=1&tail={tail}"
        try:
            async with self._client() as client:
                response = await client.get(f"/containers/{name}/logs?{query}")
        except (httpx.TimeoutException, httpx.ConnectError) as error:
            return f"(could not fetch logs for {name}: {error})"

        if response.status_code != 200:
            return f"(Docker replied with HTTP {response.status_code} fetching logs for {name})"

        return _demultiplex_docker_stream(response.content)

    async def compose_up(self, project: str, compose_file: Path, service: str) -> ComposeResult:
        argv = (
            str(self._compose_binary),
            "-p",
            project,
            "-f",
            str(compose_file),
            "up",
            "-d",
            "--no-recreate",
            service,
        )
        # Deliberately minimal - built from scratch rather than inheriting
        # the parent process's whole environment (which may carry unrelated
        # settings) - but HOME and PATH are not optional extras. Docker's
        # own CLI config loader falls back to resolving the running user's
        # home directory from `/etc/passwd` when HOME is unset, and that
        # fallback chain succeeding is untested territory on every possible
        # base image; passing both through explicitly (with a safe default
        # if the parent process somehow lacks them too) removes the doubt
        # entirely rather than hoping the fallback works.
        env = {
            "DOCKER_HOST": f"unix://{self._socket_path}",
            "HOME": os.environ.get("HOME", "/root"),
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        }

        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self._compose_timeout
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            return ComposeResult(
                ok=False,
                exit_code=-1,
                output=f"docker-compose did not finish within {self._compose_timeout:.0f}s",
            )
        except OSError as error:
            # The pinned binary missing or not executable is a broken image,
            # not a reason to crash the deploy - it comes back as a result
            # like any other compose failure.
            return ComposeResult(ok=False, exit_code=-1, output=str(error))

        output = (stdout + stderr).decode("utf-8", errors="replace")
        exit_code = process.returncode if process.returncode is not None else -1
        return ComposeResult(ok=exit_code == 0, exit_code=exit_code, output=output)

    async def self_container_id(self) -> str | None:
        # Docker sets a container's hostname to its own short id by default.
        # Our container is created by a different compose project than the
        # stack it is joining, so there is no label to filter on instead -
        # the fallback chain is explicit because a wrong id would connect
        # the wrong container to the stack network.
        hostname = socket.gethostname()
        if (await self.inspect(hostname)).exists:
            return hostname
        if (await self.inspect("marrquee")).exists:
            return "marrquee"
        return None

    async def remove_container(self, name: str) -> ContainerRemoveResult:
        try:
            async with self._client() as client:
                response = await client.delete(f"/containers/{name}?force=true")
        except (httpx.TimeoutException, httpx.ConnectError) as error:
            return ContainerRemoveResult(ok=False, detail=str(error))

        return _classify_remove_response(response)


def _classify_remove_response(response: httpx.Response) -> ContainerRemoveResult:
    """204 means removed, 404 means already gone - Cancel treats both as
    done. Anything else (409 "removal already in progress", for one) is a
    genuine failure, carrying Docker's own message for the diagnostics file.
    """
    if response.status_code in (204, 404):
        return ContainerRemoveResult(ok=True, detail=None)
    return ContainerRemoveResult(
        ok=False, detail=f"HTTP {response.status_code}: {_docker_error_message(response)}"
    )


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
    platform_block = payload.get("Platform") if isinstance(payload, dict) else None
    platform_name = platform_block.get("Name") if isinstance(platform_block, dict) else None
    os_type = payload.get("Os") if isinstance(payload, dict) else None
    kernel_version = payload.get("KernelVersion") if isinstance(payload, dict) else None
    return DockerStatus(
        connected=True,
        version=version,
        api_version=api_version if isinstance(api_version, str) else None,
        platform_name=platform_name if isinstance(platform_name, str) else None,
        os_type=os_type if isinstance(os_type, str) else None,
        kernel_version=kernel_version if isinstance(kernel_version, str) else None,
    )


HostKind = Literal["nas_or_linux", "docker_desktop", "unknown"]


def detect_host_kind(status: DockerStatus) -> HostKind:
    """Guess whether Docker is running on Docker Desktop or a real NAS/Linux
    box, from fields already in the `/version` reply `status()` fetched -
    no second request needed.

    Matching is positive-only: anything unrecognised comes back "unknown"
    rather than a guess, so a NAS shape Marrquee doesn't recognise yet never
    earns a false "this isn't supported" warning.
    """
    if not status.connected:
        return "unknown"

    platform_name = (status.platform_name or "").lower()
    kernel_version = (status.kernel_version or "").lower()
    if platform_name.startswith("docker desktop") or "linuxkit" in kernel_version:
        return "docker_desktop"
    if status.os_type == "linux":
        return "nas_or_linux"
    return "unknown"


# Go's zero-value `time.Time`, formatted the way Docker's JSON encoder
# renders it. A container that has never stopped reports `FinishedAt` as
# this exact string rather than an empty one - "never happened", not
# "unknown" - so it has to be checked for by value, not by truthiness.
_ZERO_TIMESTAMP = "0001-01-01T00:00:00Z"


def _parse_docker_timestamp(raw: object) -> str | None:
    if not isinstance(raw, str) or raw == _ZERO_TIMESTAMP:
        return None
    return raw


def _parse_container_state(raw: object) -> ContainerState | None:
    match raw:
        case "created" | "running" | "restarting" | "exited" | "paused" | "dead" | "removing":
            return raw
        case _:
            return None


def _parse_inspect_payload(name: str, payload: object) -> ContainerSnapshot:
    if not isinstance(payload, dict):
        return ContainerSnapshot(
            name=name,
            exists=False,
            state=None,
            exit_code=None,
            image=None,
            detail=f"unexpected inspect payload: {payload!r}",
        )

    state_block = payload.get("State")
    state_block = state_block if isinstance(state_block, dict) else {}
    exit_code = state_block.get("ExitCode")

    config_block = payload.get("Config")
    config_block = config_block if isinstance(config_block, dict) else {}
    image = config_block.get("Image")

    return ContainerSnapshot(
        name=name,
        exists=True,
        state=_parse_container_state(state_block.get("Status")),
        exit_code=exit_code if isinstance(exit_code, int) else None,
        image=image if isinstance(image, str) else None,
        detail=None,
        started_at=_parse_docker_timestamp(state_block.get("StartedAt")),
        finished_at=_parse_docker_timestamp(state_block.get("FinishedAt")),
    )


def _docker_error_message(response: httpx.Response) -> str:
    """Docker's own `message` field from an error body, or the raw response
    text when the body isn't the JSON shape Docker normally sends.

    This is Docker's own wording about a network or container's state - it
    never carries anything Marrquee itself set (an API key, a path), so it
    is safe to put straight into a `Failure.technical` field.
    """
    try:
        payload = response.json()
    except ValueError:
        return response.text
    message = payload.get("message") if isinstance(payload, dict) else None
    return message if isinstance(message, str) else response.text


def _reports_already_connected(response: httpx.Response) -> bool:
    """Does this network-connect error mean "already connected" rather than
    a genuine failure?

    The Engine API's own docs say re-attaching an already-connected
    container is an error, not a silent success - so idempotence is decided
    here, from the error message, instead of assumed from the status code
    alone.
    """
    lowered = _docker_error_message(response).lower()
    return "already" in lowered and "exist" in lowered


# Docker's multiplexed-stream frame: a 1-byte stream type (0=stdin,
# 1=stdout, 2=stderr), 3 padding bytes, then a big-endian uint32 payload
# size - 8 bytes total, per the Engine API's attach/logs documentation.
_STREAM_TYPES = frozenset({0, 1, 2})
_FRAME_HEADER_SIZE = 8


def _demultiplex_docker_stream(raw: bytes) -> str:
    """Strip Docker's frame headers so log text is clean for copy-paste.

    A container created without a TTY multiplexes stdout/stderr behind this
    framing; a TTY container's logs arrive as plain, unframed bytes instead,
    signalled by a first byte that isn't a valid stream type - in which case
    the whole body is already plain text.
    """
    if not raw or raw[0] not in _STREAM_TYPES:
        return raw.decode("utf-8", errors="replace")

    chunks: list[bytes] = []
    offset = 0
    while offset + _FRAME_HEADER_SIZE <= len(raw):
        stream_type, size = struct.unpack(">BxxxL", raw[offset : offset + _FRAME_HEADER_SIZE])
        if stream_type not in _STREAM_TYPES:
            break
        offset += _FRAME_HEADER_SIZE
        chunks.append(raw[offset : offset + size])
        offset += size
    return b"".join(chunks).decode("utf-8", errors="replace")


class FakeDockerEngine:
    """An in-memory DockerEngine, scripted with the answers a test wants.

    Exported from here (not a test file) so stories 2-6 can use it without
    reaching into this story's test suite. Every call is recorded on
    `.calls` as `(method_name, args)` so a test can assert not just the
    outcome but that the right calls happened, in the right order.
    """

    def __init__(
        self,
        status: DockerStatus,
        *,
        containers: Mapping[str, ContainerSnapshot] | None = None,
        images: Iterable[str] | None = None,
        compose_results: Mapping[str, ComposeResult] | None = None,
        network_connects: bool | None = None,
        network_exists: bool = False,
        self_container_id: str | None = "fake-marrquee-container",
        remove_results: Mapping[str, bool] | None = None,
    ) -> None:
        self._status = status
        self._containers = dict(containers) if containers is not None else {}
        self._images = frozenset(images) if images is not None else frozenset()
        self._compose_results = dict(compose_results) if compose_results is not None else {}
        self._remove_results = dict(remove_results) if remove_results is not None else {}
        # `None` (the default) models a real Docker daemon honestly: the
        # stack's network is created by compose's own first successful `up`,
        # not by anything before it - `network_exists` starts False (a
        # fresh host) unless a test explicitly pre-seeds it (a resume
        # scenario). `network_connects` is an explicit override for tests
        # that don't care about that sequencing at all.
        self._network_connects_override = network_connects
        self._network_exists = network_exists
        self._self_container_id = self_container_id
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def status(self) -> DockerStatus:
        self.calls.append(("status", ()))
        return self._status

    async def inspect(self, name: str) -> ContainerSnapshot:
        self.calls.append(("inspect", (name,)))
        return self._containers.get(
            name,
            ContainerSnapshot(
                name=name, exists=False, state=None, exit_code=None, image=None, detail=None
            ),
        )

    async def image_present(self, reference: str) -> bool:
        self.calls.append(("image_present", (reference,)))
        return reference in self._images

    async def connect_network(self, network: str, container: str) -> NetworkConnectResult:
        self.calls.append(("connect_network", (network, container)))
        if self._network_connects_override is not None:
            return NetworkConnectResult(ok=self._network_connects_override, detail=None)
        if self._network_exists:
            return NetworkConnectResult(ok=True, detail=None)
        return NetworkConnectResult(
            ok=False, detail=f"network {network!r} does not exist yet (no successful compose up)"
        )

    async def logs(self, name: str, tail: int = 50) -> str:
        self.calls.append(("logs", (name, tail)))
        return ""

    async def compose_up(self, project: str, compose_file: Path, service: str) -> ComposeResult:
        self.calls.append(("compose_up", (project, str(compose_file), service)))
        result = self._compose_results.get(service, ComposeResult(ok=True, exit_code=0, output=""))
        if result.ok:
            # Real compose creates the stack's network as a side effect of
            # its first successful `up` - whichever service happens to be
            # first - not before, and not conditional on which one it is.
            self._network_exists = True
        return result

    async def self_container_id(self) -> str | None:
        self.calls.append(("self_container_id", ()))
        return self._self_container_id

    async def remove_container(self, name: str) -> ContainerRemoveResult:
        self.calls.append(("remove_container", (name,)))
        ok = self._remove_results.get(name, True)
        if ok:
            # A removed container must stop answering inspect - an
            # always-"yes" fake could never prove Cancel actually removed
            # anything.
            self._containers.pop(name, None)
            return ContainerRemoveResult(ok=True, detail=None)
        return ContainerRemoveResult(ok=False, detail=f"scripted failure removing {name!r}")

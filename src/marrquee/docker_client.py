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
import logging
import os
import socket
import struct
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

import httpx

logger = logging.getLogger(__name__)

# The Docker Engine API's container states, straight off `State.Status` in a
# `GET /containers/{name}/json` reply.
ContainerState = Literal["created", "running", "restarting", "exited", "paused", "dead", "removing"]

# Docker's own HEALTHCHECK verdict, straight off `State.Health.Status` - only
# an image that ships a HEALTHCHECK (Gluetun's does; today's arr images
# don't) ever reports one. Docker's own fourth value, "none", means "no
# HEALTHCHECK is defined" and is folded into `None` rather than kept as a
# fourth member here - nothing in this codebase ever needs to tell "no check
# defined" apart from "the check hasn't reported yet".
ContainerHealth = Literal["starting", "healthy", "unhealthy"]

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

# `probe_host_path`'s own throwaway container: never started, always
# force-removed, and named so a leftover from a crashed run is unmistakably
# ours to clean up rather than a real app.
_GRAPHICS_CHECK_CONTAINER_NAME = "marrquee-graphics-check"
_GRAPHICS_CHECK_MOUNT_TARGET = "/marrquee-check"
_GRAPHICS_CHECK_LABELS = {"com.marrquee.purpose": "graphics-check"}
# Docker's own wording (bind-mounts docs: "`--mount` does not automatically
# create" the source) for a CREATE-time refusal of a `Mounts` bind whose
# source doesn't exist on the host - the one substring that turns a 400 into
# "absent" rather than "couldn't tell".
_BIND_SOURCE_MISSING = "bind source path does not exist"


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

    `health` comes from the same reply's `State.Health.Status` - also no
    extra call, and also optional and defaulted, since most containers carry
    no HEALTHCHECK at all.
    """

    name: str
    exists: bool
    state: ContainerState | None
    exit_code: int | None
    image: str | None
    detail: str | None
    started_at: str | None = None
    finished_at: str | None = None
    health: ContainerHealth | None = None


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
class ContainerStopResult:
    """The outcome of asking Docker to gently stop one container.

    A gentle stop (SIGTERM, then SIGKILL only after `timeout_seconds`) is
    what keeps a mover's own resume data and torrent list intact when it's
    about to be removed and recreated behind (or in front of) a VPN -
    `remove_container`'s own `force=true` skips straight to a kill, which
    would lose whatever a graceful shutdown would have flushed to disk.
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


@dataclass(frozen=True)
class ExecStartResult:
    """The outcome of asking Docker to create and detach-start one exec.

    `exec_id` is carried even on a start failure (the create half can
    succeed while start fails) so a caller can still poll or log against it.
    `detail` carries the status code and Docker's own message, for logs and
    the diagnostics file only - same contract as every other `detail`-shaped
    field in this codebase. A 409 on create means "the container is
    stopped" - Docker's own wording, not anything this codebase invents.
    """

    ok: bool
    exec_id: str | None
    detail: str | None


@dataclass(frozen=True)
class ExecState:
    """The answer to "is this exec still running, and how did it end?".

    `known=False` covers both a 404 (Docker has forgotten this exec id, or
    never had it) and any other reply this codebase doesn't understand -
    a caller that only cares "is it done" treats both the same way.
    """

    known: bool
    running: bool
    exit_code: int | None
    detail: str | None


@dataclass(frozen=True)
class HostPathProbe:
    """The answer to "does this path exist on the HOST, from outside any
    container's own bind mounts?".

    `detail` carries the status code and Docker's own message (or a
    connection error), for logs only - same contract as every other
    `detail`-shaped field in this codebase. `"unknown"` covers every case
    that isn't a clean present/absent classification - a caller must never
    treat it as either answer.
    """

    result: Literal["present", "absent", "unknown"]
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
    async def compose_up(
        self, project: str, compose_file: Path, service: str, *, recreate: bool = False
    ) -> ComposeResult: ...
    async def self_container_id(self) -> str | None: ...
    async def remove_container(self, name: str) -> ContainerRemoveResult: ...
    async def stop_container(
        self, name: str, *, timeout_seconds: int = 30
    ) -> ContainerStopResult: ...
    async def exec_start(self, container: str, cmd: Sequence[str]) -> ExecStartResult: ...
    async def exec_inspect(self, exec_id: str) -> ExecState: ...
    async def host_gateway(self, container: str) -> str | None: ...
    async def probe_host_path(self, self_container: str, host_path: str) -> HostPathProbe: ...


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

    async def compose_up(
        self, project: str, compose_file: Path, service: str, *, recreate: bool = False
    ) -> ComposeResult:
        # `--no-recreate` is what keeps a `compose up` on an already-running,
        # unchanged service a no-op - the default every caller except the
        # login run wants. Dropping it (`recreate=True`) is the only way to
        # let compose recreate a service whose compose file changed (an env
        # var an app's own config.xml can't override until its container is
        # recreated), and it still leaves an unchanged service alone -
        # compose itself decides that, not this flag.
        argv = (
            str(self._compose_binary),
            "-p",
            project,
            "-f",
            str(compose_file),
            "up",
            "-d",
            *(() if recreate else ("--no-recreate",)),
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

    async def stop_container(self, name: str, *, timeout_seconds: int = 30) -> ContainerStopResult:
        # The client's own default timeout (`_DEFAULT_TIMEOUT`) is far too
        # short for Docker's own wait-then-kill window - this request gets
        # its own, generous timeout instead, ten seconds past however long
        # Docker itself has been told to wait before killing the container.
        request_timeout = timeout_seconds + 10
        try:
            async with self._client() as client:
                response = await client.post(
                    f"/containers/{name}/stop",
                    params={"t": timeout_seconds},
                    timeout=request_timeout,
                )
        except (httpx.TimeoutException, httpx.ConnectError) as error:
            return ContainerStopResult(ok=False, detail=str(error))

        return _classify_stop_response(response)

    async def exec_start(self, container: str, cmd: Sequence[str]) -> ExecStartResult:
        try:
            async with self._client() as client:
                create_response = await client.post(
                    f"/containers/{container}/exec",
                    json={
                        "AttachStdin": False,
                        "AttachStdout": False,
                        "AttachStderr": False,
                        "Tty": False,
                        "Cmd": list(cmd),
                    },
                )
        except (httpx.TimeoutException, httpx.ConnectError) as error:
            return ExecStartResult(ok=False, exec_id=None, detail=str(error))

        if create_response.status_code != 201:
            # A 409 here is Docker's own way of saying the container isn't
            # running - the caller reads that from `detail`, same as every
            # other failure in this codebase.
            return ExecStartResult(
                ok=False,
                exec_id=None,
                detail=f"HTTP {create_response.status_code}: "
                f"{_docker_error_message(create_response)}",
            )

        try:
            created = create_response.json()
        except ValueError as error:
            return ExecStartResult(ok=False, exec_id=None, detail=str(error))

        exec_id = created.get("Id") if isinstance(created, dict) else None
        if not isinstance(exec_id, str):
            return ExecStartResult(
                ok=False,
                exec_id=None,
                detail=f"Docker's exec-create reply had no 'Id': {created!r}",
            )

        try:
            async with self._client() as client:
                start_response = await client.post(
                    f"/exec/{exec_id}/start", json={"Detach": True, "Tty": False}
                )
        except (httpx.TimeoutException, httpx.ConnectError) as error:
            return ExecStartResult(ok=False, exec_id=exec_id, detail=str(error))

        if start_response.status_code != 200:
            return ExecStartResult(
                ok=False,
                exec_id=exec_id,
                detail=f"HTTP {start_response.status_code}: "
                f"{_docker_error_message(start_response)}",
            )

        return ExecStartResult(ok=True, exec_id=exec_id, detail=None)

    async def exec_inspect(self, exec_id: str) -> ExecState:
        try:
            async with self._client() as client:
                response = await client.get(f"/exec/{exec_id}/json")
        except (httpx.TimeoutException, httpx.ConnectError) as error:
            return ExecState(known=False, running=False, exit_code=None, detail=str(error))

        if response.status_code == 404:
            return ExecState(known=False, running=False, exit_code=None, detail=None)
        if response.status_code != 200:
            return ExecState(
                known=False,
                running=False,
                exit_code=None,
                detail=f"HTTP {response.status_code}: {_docker_error_message(response)}",
            )

        try:
            payload = response.json()
        except ValueError as error:
            return ExecState(known=False, running=False, exit_code=None, detail=str(error))

        if not isinstance(payload, dict):
            return ExecState(
                known=False,
                running=False,
                exit_code=None,
                detail=f"unexpected exec-inspect payload: {payload!r}",
            )

        exit_code = payload.get("ExitCode")
        return ExecState(
            known=True,
            running=bool(payload.get("Running")),
            exit_code=exit_code if isinstance(exit_code, int) else None,
            detail=None,
        )

    async def host_gateway(self, container: str) -> str | None:
        try:
            async with self._client() as client:
                response = await client.get(f"/containers/{container}/json")
        except (httpx.TimeoutException, httpx.ConnectError):
            return None

        if response.status_code != 200:
            return None

        try:
            payload = response.json()
        except ValueError:
            return None

        return _parse_host_gateway_payload(payload)

    async def probe_host_path(self, self_container: str, host_path: str) -> HostPathProbe:
        # Our own running container's image id - never a tag, which can
        # move after an update, and never `Config.Image` (a snapshot of
        # whatever tag created it) - the id a `create` with this same image
        # is guaranteed to still resolve.
        try:
            async with self._client() as client:
                inspect_response = await client.get(f"/containers/{self_container}/json")
        except (httpx.TimeoutException, httpx.ConnectError) as error:
            return HostPathProbe(result="unknown", detail=str(error))

        if inspect_response.status_code != 200:
            return HostPathProbe(
                result="unknown",
                detail=f"HTTP {inspect_response.status_code} inspecting {self_container}",
            )
        try:
            inspect_payload = inspect_response.json()
        except ValueError as error:
            return HostPathProbe(result="unknown", detail=str(error))

        image = inspect_payload.get("Image") if isinstance(inspect_payload, dict) else None
        if not isinstance(image, str):
            return HostPathProbe(
                result="unknown", detail=f"{self_container} has no top-level Image id"
            )

        # A leftover from a crashed earlier probe would otherwise make a
        # genuinely present path look like a 409 name conflict - any answer
        # (404 "never existed", 204 "removed", anything else) is ignored,
        # exactly like Cancel's own force-remove.
        try:
            async with self._client() as client:
                await client.delete(f"/containers/{_GRAPHICS_CHECK_CONTAINER_NAME}?force=true")
        except (httpx.TimeoutException, httpx.ConnectError):
            pass

        body = {
            "Image": image,
            "Labels": _GRAPHICS_CHECK_LABELS,
            "HostConfig": {
                "Mounts": [
                    {
                        "Type": "bind",
                        "Source": host_path,
                        "Target": _GRAPHICS_CHECK_MOUNT_TARGET,
                        "ReadOnly": True,
                    }
                ]
            },
        }
        try:
            async with self._client() as client:
                create_response = await client.post(
                    f"/containers/create?name={_GRAPHICS_CHECK_CONTAINER_NAME}", json=body
                )
        except (httpx.TimeoutException, httpx.ConnectError) as error:
            return HostPathProbe(result="unknown", detail=str(error))

        if create_response.status_code == 201:
            await self._remove_graphics_check_container(create_response)
            return HostPathProbe(result="present", detail=None)

        if create_response.status_code == 400 and _BIND_SOURCE_MISSING in _docker_error_message(
            create_response
        ):
            return HostPathProbe(result="absent", detail=None)

        return HostPathProbe(
            result="unknown",
            detail=f"HTTP {create_response.status_code}: {_docker_error_message(create_response)}",
        )

    async def _remove_graphics_check_container(self, create_response: httpx.Response) -> None:
        """Best-effort cleanup of the container `probe_host_path` just
        created - never started, and never load-bearing for the "present"
        answer itself. A failed DELETE here is logged, never surfaced to
        the owner as an error - the leftover is harmless (it never runs)
        and the next probe's own leading DELETE will try again.
        """
        try:
            created = create_response.json()
        except ValueError:
            created = {}
        container_id = created.get("Id") if isinstance(created, dict) else None
        name = container_id if isinstance(container_id, str) else _GRAPHICS_CHECK_CONTAINER_NAME
        try:
            async with self._client() as client:
                remove_response = await client.delete(f"/containers/{name}?force=true")
        except (httpx.TimeoutException, httpx.ConnectError) as error:
            logger.warning("graphics-chip probe left %s behind: %s", name, error)
            return
        if remove_response.status_code not in (204, 404):
            logger.warning(
                "graphics-chip probe left %s behind: HTTP %s",
                name,
                remove_response.status_code,
            )


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


def _classify_stop_response(response: httpx.Response) -> ContainerStopResult:
    """204 means it stopped, 304 means it was already stopped, and 404
    means there's nothing left to stop - a mover that's already gone (a
    resumed, idempotent retry) is not a failure here either. Anything else
    is a genuine failure, carrying Docker's own message for the
    diagnostics file.
    """
    if response.status_code in (204, 304, 404):
        return ContainerStopResult(ok=True, detail=None)
    return ContainerStopResult(
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


def _parse_container_health(raw: object) -> ContainerHealth | None:
    """Docker's `"none"` (no HEALTHCHECK defined) and a missing `Health`
    block both fall through to `None` here - the same "not sure" value.
    """
    match raw:
        case "starting" | "healthy" | "unhealthy":
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

    health_block = state_block.get("Health")
    health_block = health_block if isinstance(health_block, dict) else {}

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
        health=_parse_container_health(health_block.get("Status")),
    )


# Mirrors compose.py's own `_NETWORK_NAME` literally, rather than importing
# it - this module is a leaf (nothing under `marrquee.*` imports anything),
# and the two constants have no reason to ever drift: compose only ever
# names Marrquee's shared bridge network this one way.
_MARRQUEE_NETWORK_NAME = "marrquee"


def _parse_host_gateway_payload(payload: object) -> str | None:
    """`GET /containers/{name}/json`'s payload, read for the one address a
    host-networked Plex can be reached at from inside a container.

    Marrquee itself running with `network_mode: host` answers every
    request at `127.0.0.1` (there is no bridge gateway to read at all in
    that case) - checked first, and regardless of `NetworkSettings`, since
    Docker leaves a host-networked container's `Networks` map populated
    with stale, unreachable entries. Otherwise, the `marrquee` network's
    own gateway is preferred (the network this codebase actually manages);
    falling back to the first non-empty IPv4 gateway, in network-name
    order, covers Marrquee joined to nothing but a NAS app's own default
    network.
    """
    if not isinstance(payload, dict):
        return None

    host_config = payload.get("HostConfig")
    host_config = host_config if isinstance(host_config, dict) else {}
    if host_config.get("NetworkMode") == "host":
        return "127.0.0.1"

    network_settings = payload.get("NetworkSettings")
    network_settings = network_settings if isinstance(network_settings, dict) else {}
    networks = network_settings.get("Networks")
    networks = networks if isinstance(networks, dict) else {}

    marrquee_network = networks.get(_MARRQUEE_NETWORK_NAME)
    if isinstance(marrquee_network, dict):
        gateway = marrquee_network.get("Gateway")
        if isinstance(gateway, str) and gateway:
            return gateway

    for name in sorted(networks):
        network = networks[name]
        if not isinstance(network, dict):
            continue
        gateway = network.get("Gateway")
        if isinstance(gateway, str) and gateway:
            return gateway

    return None


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


def _service_config_signature(compose_file: Path, service: str) -> str | None:
    """`service`'s own block of `compose_file`, or `None` when the file
    can't be read or doesn't mention this service at all - `_model_recreate`
    treats `None` as "changed", never as "unchanged".
    """
    try:
        text = compose_file.read_text()
    except OSError:
        return None
    return _service_config_block(text, service)


def _service_config_block(compose_text: str, service: str) -> str | None:
    """Slice out one service's own block from a rendered compose file.

    Real Compose recreates only the services whose OWN resolved config
    changed - comparing the whole file would recreate every service the
    moment any one of them changed, which is not what a recreate fake needs
    to prove. `render_compose` (`compose.py`) always writes a service's
    block starting at `"  {service}:"` (2-space indent) with every line that
    belongs to it indented 4 spaces or more (or blank), so the block ends at
    the next 2-space-indented line - either the next service, or the
    trailing `networks:` block.
    """
    marker = f"  {service}:"
    lines = compose_text.splitlines()
    try:
        start = lines.index(marker)
    except ValueError:
        return None
    end = start + 1
    while end < len(lines) and (lines[end] == "" or lines[end].startswith("    ")):
        end += 1
    return "\n".join(lines[start:end])


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
        logs: Mapping[str, str] | None = None,
        frames: Mapping[str, Sequence[ContainerSnapshot]] | None = None,
        exec_exit_codes: Mapping[str, int] | None = None,
        exec_running_polls: int = 0,
        host_gateway: str | None = "172.18.0.1",
        host_paths: Iterable[str] = (),
    ) -> None:
        self._status = status
        self._containers = dict(containers) if containers is not None else {}
        self._images = frozenset(images) if images is not None else frozenset()
        self._compose_results = dict(compose_results) if compose_results is not None else {}
        self._remove_results = dict(remove_results) if remove_results is not None else {}
        self._logs = dict(logs) if logs is not None else {}
        # A scripted exec's exit code, keyed by the CONTAINER it ran in
        # (never the exec id, which this fake mints itself) - `0` is Docker's
        # own "the command succeeded" default for a test that doesn't care.
        self._exec_exit_codes = dict(exec_exit_codes) if exec_exit_codes is not None else {}
        self._exec_running_polls = exec_running_polls
        self._exec_containers: dict[str, str] = {}
        self._exec_polls: dict[str, int] = {}
        self._exec_sequence = 0
        # A VPN whose health changes tick by tick is state the fake must
        # model (docker-fakes-model-state) - each name scripted here drains
        # in order, then repeats its last frame, and always wins over
        # `_containers` for that name (including a snapshot `_model_recreate`
        # itself just wrote).
        self._frames: dict[str, list[ContainerSnapshot]] = (
            {name: list(sequence) for name, sequence in frames.items()}
            if frames is not None
            else {}
        )
        self._frame_positions: dict[str, int] = dict.fromkeys(self._frames, 0)
        # `None` (the default) models a real Docker daemon honestly: the
        # stack's network is created by compose's own first successful `up`,
        # not by anything before it - `network_exists` starts False (a
        # fresh host) unless a test explicitly pre-seeds it (a resume
        # scenario). `network_connects` is an explicit override for tests
        # that don't care about that sequencing at all.
        self._network_connects_override = network_connects
        self._network_exists = network_exists
        self._self_container_id = self_container_id
        self._host_gateway = host_gateway
        self._host_paths = frozenset(host_paths)
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        # What a real Docker daemon uses to decide whether a recreate is a
        # no-op: the service's own rendered config, as last seen. Tracked
        # only for `recreate=True` calls - nothing about the plain
        # `recreate=False` path (used everywhere else) changes.
        self._service_signatures: dict[str, str] = {}
        self._recreate_sequence = 0

    async def status(self) -> DockerStatus:
        self.calls.append(("status", ()))
        return self._status

    async def inspect(self, name: str) -> ContainerSnapshot:
        self.calls.append(("inspect", (name,)))
        if name in self._frames:
            frame = self._next_frame(name)
            if frame is not None:
                return frame
        return self._containers.get(
            name,
            ContainerSnapshot(
                name=name, exists=False, state=None, exit_code=None, image=None, detail=None
            ),
        )

    def _next_frame(self, name: str) -> ContainerSnapshot | None:
        frames = self._frames[name]
        if not frames:
            return None
        position = self._frame_positions[name]
        if position < len(frames) - 1:
            self._frame_positions[name] = position + 1
        return frames[position]

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
        return self._logs.get(name, "")

    async def compose_up(
        self, project: str, compose_file: Path, service: str, *, recreate: bool = False
    ) -> ComposeResult:
        # A distinct call name for a recreate run - never "compose_up" with
        # an extra bool tucked on the end - so every existing `.calls`
        # assertion written against the old (non-recreate-aware) signature
        # stays untouched.
        call_name = "compose_up_recreate" if recreate else "compose_up"
        self.calls.append((call_name, (project, str(compose_file), service)))
        result = self._compose_results.get(service, ComposeResult(ok=True, exit_code=0, output=""))
        if not result.ok:
            return result
        # Real compose creates the stack's network as a side effect of its
        # first successful `up` - whichever service happens to be first -
        # not before, and not conditional on which one it is.
        self._network_exists = True
        # Tracked on EVERY successful call, recreate or not - a later
        # recreate needs a genuine baseline to compare against, including
        # one set by the plain `compose up --no-recreate` an initial deploy
        # already ran for this same service.
        changed = self._update_service_signature(compose_file, service)
        if recreate:
            self._model_recreate(service, changed=changed)
        return result

    def _update_service_signature(self, compose_file: Path, service: str) -> bool:
        """Record `service`'s current config signature and report whether it
        differs from what was last seen. Unreadable or never-seen content
        always reports "changed" - the safe default a recreate fake must
        fail toward, never a silent no-op.
        """
        signature = _service_config_signature(compose_file, service)
        previous = self._service_signatures.get(service)
        if signature is not None:
            self._service_signatures[service] = signature
        return signature is None or signature != previous

    def _model_recreate(self, service: str, *, changed: bool) -> None:
        """Real Compose only replaces a container whose OWN rendered config
        actually changed - an unchanged service is left running untouched,
        never given a new container (per the project's own
        docker-fakes-model-state rule).
        """
        if service in self._containers and not changed:
            return  # an unchanged, already-known service is left exactly alone

        self._recreate_sequence += 1
        previous_container = self._containers.get(service)
        self._containers[service] = ContainerSnapshot(
            name=service,
            exists=True,
            state="running",
            exit_code=None,
            image=previous_container.image if previous_container is not None else None,
            detail=None,
            started_at=f"fake-recreated-{self._recreate_sequence}",
            finished_at=None,
            # A real recreate doesn't reset Docker's own health-check
            # history - the container keeps its last reported health until
            # the new health check runs and reports otherwise.
            health=previous_container.health if previous_container is not None else None,
        )

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

    async def stop_container(self, name: str, *, timeout_seconds: int = 30) -> ContainerStopResult:
        self.calls.append(("stop_container", (name,)))
        if name in self._containers:
            self._containers[name] = replace(self._containers[name], state="exited")
        return ContainerStopResult(ok=True, detail=None)

    async def exec_start(self, container: str, cmd: Sequence[str]) -> ExecStartResult:
        self.calls.append(("exec_start", (container, tuple(cmd))))
        # Read `_containers` directly, never through `inspect()` - `inspect`
        # also consults `_frames` and advances them, and a Sync-now request
        # must never itself tick a scripted health sequence forward.
        snapshot = self._containers.get(container)
        if snapshot is None or snapshot.state != "running":
            return ExecStartResult(ok=False, exec_id=None, detail="container is not running")

        self._exec_sequence += 1
        exec_id = f"exec-{self._exec_sequence}"
        self._exec_containers[exec_id] = container
        self._exec_polls[exec_id] = 0
        return ExecStartResult(ok=True, exec_id=exec_id, detail=None)

    async def exec_inspect(self, exec_id: str) -> ExecState:
        self.calls.append(("exec_inspect", (exec_id,)))
        container = self._exec_containers.get(exec_id)
        if container is None:
            return ExecState(known=False, running=False, exit_code=None, detail=None)

        polls_done = self._exec_polls[exec_id]
        if polls_done < self._exec_running_polls:
            self._exec_polls[exec_id] = polls_done + 1
            return ExecState(known=True, running=True, exit_code=None, detail=None)

        return ExecState(
            known=True,
            running=False,
            exit_code=self._exec_exit_codes.get(container, 0),
            detail=None,
        )

    async def host_gateway(self, container: str) -> str | None:
        self.calls.append(("host_gateway", (container,)))
        # An unknown container has no networks at all (docker-fakes-model-
        # state) - only Marrquee's own self container ever gets an answer.
        if container == self._self_container_id:
            return self._host_gateway
        return None

    async def probe_host_path(self, self_container: str, host_path: str) -> HostPathProbe:
        self.calls.append(("probe_host_path", (self_container, host_path)))
        # An unknown container can't be inspected for its own image id
        # either (docker-fakes-model-state) - only Marrquee's own self
        # container ever gets a real answer.
        if self_container != self._self_container_id:
            return HostPathProbe(result="unknown", detail=None)
        if host_path in self._host_paths:
            return HostPathProbe(result="present", detail=None)
        return HostPathProbe(result="absent", detail=None)

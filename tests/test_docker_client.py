"""Tests for the Docker seam: SocketDockerEngine and FakeDockerEngine.

Nothing here talks to a real Docker daemon - there isn't one on this machine.
`docker_stub` (see conftest.py) is a stand-in unix-socket server that records
what was sent to it and replies with a fixed, controllable response.
"""

from __future__ import annotations

import asyncio
import json
import socket
import struct
from pathlib import Path

import httpx
import pytest

from marrquee.docker_client import (
    ComposeResult,
    ContainerRemoveResult,
    ContainerSnapshot,
    ContainerStopResult,
    DockerEngine,
    DockerFailure,
    DockerStatus,
    ExecStartResult,
    ExecState,
    FakeDockerEngine,
    HostPathProbe,
    NetworkConnectResult,
    SocketDockerEngine,
    detect_host_kind,
)

_HEALTHY = ("HTTP/1.1 200 OK", b'{"State": {"Status": "running", "Health": {"Status": "healthy"}}}')
_STARTING = (
    "HTTP/1.1 200 OK",
    b'{"State": {"Status": "running", "Health": {"Status": "starting"}}}',
)
_UNHEALTHY = (
    "HTTP/1.1 200 OK",
    b'{"State": {"Status": "running", "Health": {"Status": "unhealthy"}}}',
)
_HEALTH_NONE = (
    "HTTP/1.1 200 OK",
    b'{"State": {"Status": "running", "Health": {"Status": "none"}}}',
)
_NO_HEALTH_BLOCK = ("HTTP/1.1 200 OK", b'{"State": {"Status": "running"}}')


async def test_talks_http_over_a_unix_socket_and_reads_the_version(docker_stub):
    """FIRST TEST - the plan's weakest assumption: does our client actually
    speak HTTP over a unix socket the way the real Docker Engine API expects?
    """
    stub, socket_path = docker_stub
    engine = SocketDockerEngine(socket_path=socket_path)

    status = await engine.status()

    assert stub.request_lines[0] == "GET /version HTTP/1.1"
    assert status.connected is True
    assert status.version == "27.3.1"
    assert status.api_version == "1.47"


async def test_status_still_issues_exactly_one_get_version_and_nothing_else(docker_stub):
    """The predecessor's safety property for `status()` specifically - re-
    asserted after this chunk widens the protocol with write operations
    `status()` itself never touches.
    """
    stub, socket_path = docker_stub
    engine = SocketDockerEngine(socket_path=socket_path)

    await engine.status()

    assert len(stub.request_lines) == 1
    method, path, _ = stub.request_lines[0].split(" ")
    assert method == "GET"
    assert path == "/version"


async def test_missing_socket_reports_socket_missing_without_waiting(tmp_path: Path) -> None:
    """A fresh install with no Docker mount yet is the commonest first-run
    state, and it should answer instantly rather than time out.
    """
    engine = SocketDockerEngine(socket_path=tmp_path / "does-not-exist.sock")

    status = await engine.status()

    assert status.connected is False
    assert status.failure == DockerFailure.SOCKET_MISSING


async def test_socket_that_refuses_the_connection_reports_no_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A socket file can exist with nothing listening on it - bind it and
    close it again without ever calling listen().
    """
    monkeypatch.chdir(tmp_path)
    relative_name = "refusing.sock"
    raw_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    raw_socket.bind(relative_name)
    raw_socket.close()

    engine = SocketDockerEngine(socket_path=tmp_path / relative_name)
    status = await engine.status()

    assert status.connected is False
    assert status.failure == DockerFailure.NO_ANSWER


async def test_a_500_from_the_daemon_reports_bad_response(docker_stub):
    stub, socket_path = docker_stub
    stub.respond_with_json({"message": "boom"}, status_line="HTTP/1.1 500 Internal Server Error")
    engine = SocketDockerEngine(socket_path=socket_path)

    status = await engine.status()

    assert status.connected is False
    assert status.failure == DockerFailure.BAD_RESPONSE


async def test_a_200_with_no_version_key_reports_bad_response(docker_stub):
    stub, socket_path = docker_stub
    stub.respond_with_json({"ApiVersion": "1.47"})
    engine = SocketDockerEngine(socket_path=socket_path)

    status = await engine.status()

    assert status.connected is False
    assert status.failure == DockerFailure.BAD_RESPONSE


async def test_a_docker_desktop_version_reply_is_detected_as_docker_desktop(docker_stub):
    """FIRST TEST for this chunk - pins the mapping against a Desktop-shaped
    `/version` body, since there is no real Docker Desktop on this machine
    to check it against.
    """
    stub, socket_path = docker_stub
    stub.respond_with_json(
        {
            "Version": "27.3.1",
            "ApiVersion": "1.47",
            "Platform": {"Name": "Docker Desktop 4.34.0 (165256)"},
            "Os": "linux",
            "KernelVersion": "6.10.11-linuxkit",
        }
    )
    engine = SocketDockerEngine(socket_path=socket_path)

    status = await engine.status()

    assert status.platform_name == "Docker Desktop 4.34.0 (165256)"
    assert status.os_type == "linux"
    assert status.kernel_version == "6.10.11-linuxkit"
    assert detect_host_kind(status) == "docker_desktop"


async def test_a_linux_engine_version_reply_is_detected_as_nas_or_linux(docker_stub):
    stub, socket_path = docker_stub
    stub.respond_with_json(
        {
            "Version": "27.3.1",
            "ApiVersion": "1.47",
            "Platform": {"Name": "Docker Engine - Community"},
            "Os": "linux",
            "KernelVersion": "6.8.0-45-generic",
        }
    )
    engine = SocketDockerEngine(socket_path=socket_path)

    status = await engine.status()

    assert detect_host_kind(status) == "nas_or_linux"


async def test_a_reply_without_platform_os_or_kernel_parses_to_none_and_is_unknown(docker_stub):
    stub, socket_path = docker_stub
    stub.respond_with_json({"Version": "27.3.1", "ApiVersion": "1.47"})
    engine = SocketDockerEngine(socket_path=socket_path)

    status = await engine.status()

    assert status.platform_name is None
    assert status.os_type is None
    assert status.kernel_version is None
    assert detect_host_kind(status) == "unknown"


def test_a_disconnected_status_is_unknown() -> None:
    status = DockerStatus(connected=False, failure=DockerFailure.SOCKET_MISSING)

    assert detect_host_kind(status) == "unknown"


async def test_the_fake_returns_whatever_status_it_was_given() -> None:
    given = DockerStatus(connected=True, version="1.2.3", api_version="1.99")
    engine = FakeDockerEngine(given)

    assert await engine.status() is given


# --- inspect() -----------------------------------------------------------


async def test_inspect_returns_exists_false_on_404_instead_of_raising(docker_stub):
    stub, socket_path = docker_stub
    stub.respond_with_json(
        {"message": "no such container: sonarr"}, status_line="HTTP/1.1 404 Not Found"
    )
    engine = SocketDockerEngine(socket_path=socket_path)

    snapshot = await engine.inspect("sonarr")

    assert snapshot == ContainerSnapshot(
        name="sonarr", exists=False, state=None, exit_code=None, image=None, detail=None
    )
    assert stub.request_lines[0] == "GET /containers/sonarr/json HTTP/1.1"


async def test_inspect_parses_state_exit_code_and_image_from_a_running_container(docker_stub):
    stub, socket_path = docker_stub
    stub.respond_with_json(
        {
            "State": {"Status": "running", "ExitCode": 0, "StartedAt": "2026-09-19T10:00:00Z"},
            "Config": {"Image": "lscr.io/linuxserver/sonarr:latest"},
        }
    )
    engine = SocketDockerEngine(socket_path=socket_path)

    snapshot = await engine.inspect("sonarr")

    assert snapshot.exists is True
    assert snapshot.state == "running"
    assert snapshot.exit_code == 0
    assert snapshot.image == "lscr.io/linuxserver/sonarr:latest"


async def test_inspect_reports_started_at_from_the_state_block(docker_stub):
    stub, socket_path = docker_stub
    stub.respond_with_json(
        {
            "State": {
                "Status": "running",
                "ExitCode": 0,
                "StartedAt": "2026-09-19T10:00:00.123456789Z",
                "FinishedAt": "0001-01-01T00:00:00Z",
            },
            "Config": {"Image": "lscr.io/linuxserver/sonarr:latest"},
        }
    )
    engine = SocketDockerEngine(socket_path=socket_path)

    snapshot = await engine.inspect("sonarr")

    assert snapshot.started_at == "2026-09-19T10:00:00.123456789Z"


async def test_inspect_treats_the_zero_date_finished_at_as_never_stopped(docker_stub):
    """TRAP: a container that has never stopped reports FinishedAt as Go's
    zero-value timestamp, not an empty string - it has to be recognised by
    its exact value and turned into None.
    """
    stub, socket_path = docker_stub
    stub.respond_with_json(
        {
            "State": {
                "Status": "running",
                "ExitCode": 0,
                "StartedAt": "2026-09-19T10:00:00Z",
                "FinishedAt": "0001-01-01T00:00:00Z",
            },
            "Config": {"Image": "lscr.io/linuxserver/sonarr:latest"},
        }
    )
    engine = SocketDockerEngine(socket_path=socket_path)

    snapshot = await engine.inspect("sonarr")

    assert snapshot.finished_at is None


async def test_inspect_reports_a_real_finished_at_when_the_container_stopped(docker_stub):
    stub, socket_path = docker_stub
    stub.respond_with_json(
        {
            "State": {
                "Status": "exited",
                "ExitCode": 1,
                "StartedAt": "2026-09-19T10:00:00Z",
                "FinishedAt": "2026-09-19T10:05:00Z",
            },
            "Config": {"Image": "lscr.io/linuxserver/sonarr:latest"},
        }
    )
    engine = SocketDockerEngine(socket_path=socket_path)

    snapshot = await engine.inspect("sonarr")

    assert snapshot.finished_at == "2026-09-19T10:05:00Z"


async def test_inspect_parses_healthy_starting_unhealthy_none_and_absent_health(docker_stub):
    """FIRST TEST - the plan's weakest assumption: Docker's inspect reply
    carries `State.Health.Status` in {starting, healthy, unhealthy}, and a
    container with no HEALTHCHECK (every arr image today) must parse to
    `None`, never to a made-up value.
    """
    stub, socket_path = docker_stub
    stub.respond_with_sequence([_HEALTHY, _STARTING, _UNHEALTHY, _HEALTH_NONE, _NO_HEALTH_BLOCK])
    engine = SocketDockerEngine(socket_path=socket_path)

    results = [(await engine.inspect("gluetun")).health for _ in range(5)]

    assert results == ["healthy", "starting", "unhealthy", None, None]


# --- image_present() ------------------------------------------------------


async def test_image_present_maps_200_to_true(docker_stub):
    stub, socket_path = docker_stub
    stub.respond_with_json({"Id": "sha256:abc"})
    engine = SocketDockerEngine(socket_path=socket_path)

    present = await engine.image_present("lscr.io/linuxserver/sonarr:latest")

    assert present is True
    assert stub.request_lines[0].startswith("GET /images/")


async def test_image_present_maps_404_to_false(docker_stub):
    stub, socket_path = docker_stub
    stub.respond_with_json({"message": "no such image"}, status_line="HTTP/1.1 404 Not Found")
    engine = SocketDockerEngine(socket_path=socket_path)

    present = await engine.image_present("lscr.io/linuxserver/sonarr:latest")

    assert present is False


# --- connect_network() ----------------------------------------------------


async def test_connect_network_returns_true_on_a_plain_200(docker_stub):
    stub, socket_path = docker_stub
    stub.respond_with("HTTP/1.1 200 OK", b"")
    engine = SocketDockerEngine(socket_path=socket_path)

    result = await engine.connect_network("marrquee", "abc123")

    assert result == NetworkConnectResult(ok=True, detail=None)
    assert stub.request_lines[0] == "POST /networks/marrquee/connect HTTP/1.1"


async def test_connect_network_treats_already_connected_as_success(docker_stub):
    stub, socket_path = docker_stub
    stub.respond_with_json(
        {"message": "endpoint with name marrquee already exists in network marrquee"},
        status_line="HTTP/1.1 403 Forbidden",
    )
    engine = SocketDockerEngine(socket_path=socket_path)

    result = await engine.connect_network("marrquee", "abc123")

    assert result.ok is True


async def test_connect_network_treats_a_409_already_exists_as_success(docker_stub):
    stub, socket_path = docker_stub
    stub.respond_with_json(
        {"message": "endpoint already exists"}, status_line="HTTP/1.1 409 Conflict"
    )
    engine = SocketDockerEngine(socket_path=socket_path)

    result = await engine.connect_network("marrquee", "abc123")

    assert result.ok is True


async def test_connect_network_reports_a_genuine_failure_as_false(docker_stub):
    stub, socket_path = docker_stub
    stub.respond_with_json(
        {"message": "network marrquee not found"}, status_line="HTTP/1.1 404 Not Found"
    )
    engine = SocketDockerEngine(socket_path=socket_path)

    result = await engine.connect_network("marrquee", "abc123")

    assert result.ok is False


async def test_connect_network_failure_detail_names_the_status_and_dockers_own_message(
    docker_stub,
):
    """This is what turned a real deploy's failure into a mysterious "docker
    unreachable" instead of a debuggable one - the whole reason a bare bool
    was widened to carry `detail`.
    """
    stub, socket_path = docker_stub
    stub.respond_with_json(
        {"message": "network marrquee not found"}, status_line="HTTP/1.1 404 Not Found"
    )
    engine = SocketDockerEngine(socket_path=socket_path)

    result = await engine.connect_network("marrquee", "abc123")

    assert result.detail is not None
    assert "404" in result.detail
    assert "network marrquee not found" in result.detail


async def test_connect_network_failure_detail_survives_a_non_json_error_body(docker_stub):
    stub, socket_path = docker_stub
    stub.respond_with("HTTP/1.1 500 Internal Server Error", b"not json at all")
    engine = SocketDockerEngine(socket_path=socket_path)

    result = await engine.connect_network("marrquee", "abc123")

    assert result.ok is False
    assert result.detail is not None
    assert "500" in result.detail


# --- logs() -----------------------------------------------------------


async def test_logs_strips_the_multiplexed_frame_headers(docker_stub):
    stub, socket_path = docker_stub
    stdout_chunk = b"hello "
    stderr_chunk = b"world\n"
    framed = (
        struct.pack(">BxxxL", 1, len(stdout_chunk))
        + stdout_chunk
        + struct.pack(">BxxxL", 2, len(stderr_chunk))
        + stderr_chunk
    )
    stub.respond_with("HTTP/1.1 200 OK", framed)
    engine = SocketDockerEngine(socket_path=socket_path)

    text = await engine.logs("sonarr", tail=50)

    assert text == "hello world\n"
    assert stub.request_lines[0] == "GET /containers/sonarr/logs?stdout=1&stderr=1&tail=50 HTTP/1.1"


async def test_logs_falls_back_to_raw_decode_for_an_unframed_tty_stream(docker_stub):
    stub, socket_path = docker_stub
    stub.respond_with("HTTP/1.1 200 OK", b"plain tty output\n")
    engine = SocketDockerEngine(socket_path=socket_path)

    text = await engine.logs("sonarr")

    assert text == "plain tty output\n"


# --- compose_up() -----------------------------------------------------


async def test_compose_up_runs_the_pinned_binary_against_our_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: dict[str, object] = {}

    class _FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    async def fake_create_subprocess_exec(*args: str, **kwargs: object) -> _FakeProcess:
        recorded["argv"] = args
        recorded["env"] = kwargs.get("env")
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    engine = SocketDockerEngine(
        socket_path=Path("/var/run/docker.sock"),
        compose_binary=Path("/usr/local/bin/docker-compose"),
    )

    result = await engine.compose_up("marrquee", Path("/host/vol/marrquee/compose.yaml"), "sonarr")

    assert recorded["argv"] == (
        "/usr/local/bin/docker-compose",
        "-p",
        "marrquee",
        "-f",
        "/host/vol/marrquee/compose.yaml",
        "up",
        "-d",
        "--no-recreate",
        "sonarr",
    )
    env = recorded["env"]
    assert isinstance(env, dict)
    assert env["DOCKER_HOST"] == "unix:///var/run/docker.sock"
    # docker-compose resolves its own config directory from HOME (falling
    # back to the OS user's home directory only if that lookup itself
    # succeeds) - untested territory on every possible base image, so both
    # are passed through explicitly rather than left to that fallback chain.
    assert "HOME" in env
    assert "PATH" in env
    assert result.ok is True


async def test_compose_up_recreate_drops_no_recreate_default_keeps_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FIRST TEST - the plan's weakest Docker assumption: does dropping
    `--no-recreate` actually change the argv `compose_up` runs, and does the
    default keep today's behaviour untouched?
    """
    recorded_argv: list[tuple[str, ...]] = []

    class _FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    async def fake_create_subprocess_exec(*args: str, **kwargs: object) -> _FakeProcess:
        recorded_argv.append(args)
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    engine = SocketDockerEngine(
        socket_path=Path("/var/run/docker.sock"),
        compose_binary=Path("/usr/local/bin/docker-compose"),
    )

    await engine.compose_up(
        "marrquee-apps", Path("/host/vol/marrquee/compose.yaml"), "sonarr", recreate=True
    )
    await engine.compose_up("marrquee-apps", Path("/host/vol/marrquee/compose.yaml"), "sonarr")

    recreate_call, default_call = recorded_argv
    assert recreate_call == (
        "/usr/local/bin/docker-compose",
        "-p",
        "marrquee-apps",
        "-f",
        "/host/vol/marrquee/compose.yaml",
        "up",
        "-d",
        "sonarr",
    )
    assert default_call == (
        "/usr/local/bin/docker-compose",
        "-p",
        "marrquee-apps",
        "-f",
        "/host/vol/marrquee/compose.yaml",
        "up",
        "-d",
        "--no-recreate",
        "sonarr",
    )


async def test_compose_up_never_leaves_home_or_path_empty_even_if_the_parent_lacks_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The subprocess env is built from scratch on purpose (it never
    inherits the parent process's environment, which could carry unrelated
    settings) - but that must never mean HOME/PATH end up missing entirely
    just because the parent process happened not to have them either.
    """
    recorded: dict[str, object] = {}

    class _FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    async def fake_create_subprocess_exec(*args: str, **kwargs: object) -> _FakeProcess:
        recorded["env"] = kwargs.get("env")
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("PATH", raising=False)
    engine = SocketDockerEngine(socket_path=Path("/var/run/docker.sock"))

    await engine.compose_up("marrquee", Path("/host/vol/marrquee/compose.yaml"), "sonarr")

    env = recorded["env"]
    assert isinstance(env, dict)
    assert env["HOME"]
    assert env["PATH"]


async def test_compose_up_reports_a_non_zero_exit_as_ok_false_with_the_output_captured(
    tmp_path: Path,
) -> None:
    script = tmp_path / "fake-compose"
    script.write_text("#!/bin/sh\necho starting stack\necho something went wrong 1>&2\nexit 3\n")
    script.chmod(0o755)
    engine = SocketDockerEngine(socket_path=tmp_path / "unused.sock", compose_binary=script)

    result = await engine.compose_up("marrquee", tmp_path / "compose.yaml", "sonarr")

    assert result.ok is False
    assert result.exit_code == 3
    assert "starting stack" in result.output
    assert "something went wrong" in result.output


def test_compose_timeout_default_is_generous_enough_for_a_first_ever_image_pull() -> None:
    """`compose_up` blocks for the whole of its own image pull - a
    ~200-300MB linuxserver image, over a NAS's own (often slow) internet
    connection, the first time it is ever deployed. 5 minutes was tight
    enough to plausibly mistake a slow-but-working pull for a hang.
    """
    engine = SocketDockerEngine(socket_path=Path("/var/run/docker.sock"))

    assert engine._compose_timeout >= 900.0


async def test_compose_up_times_out_rather_than_hanging_forever(tmp_path: Path) -> None:
    script = tmp_path / "fake-compose"
    script.write_text("#!/bin/sh\nsleep 5\n")
    script.chmod(0o755)
    engine = SocketDockerEngine(
        socket_path=tmp_path / "unused.sock", compose_binary=script, compose_timeout=0.2
    )

    result = await engine.compose_up("marrquee", tmp_path / "compose.yaml", "sonarr")

    assert result.ok is False


async def test_compose_up_never_raises_when_the_binary_is_missing(tmp_path: Path) -> None:
    engine = SocketDockerEngine(
        socket_path=tmp_path / "unused.sock", compose_binary=tmp_path / "does-not-exist"
    )

    result = await engine.compose_up("marrquee", tmp_path / "compose.yaml", "sonarr")

    assert result.ok is False


# --- self_container_id() -----------------------------------------------------


async def test_self_container_id_resolves_from_the_hostname(
    docker_stub, monkeypatch: pytest.MonkeyPatch
):
    stub, socket_path = docker_stub
    monkeypatch.setattr("marrquee.docker_client.socket.gethostname", lambda: "abc123deadbeef")
    stub.respond_with_json({"State": {"Status": "running"}})
    engine = SocketDockerEngine(socket_path=socket_path)

    container_id = await engine.self_container_id()

    assert container_id == "abc123deadbeef"
    assert stub.request_lines == ["GET /containers/abc123deadbeef/json HTTP/1.1"]


async def test_self_container_id_falls_back_from_hostname_to_the_container_name(
    docker_stub, monkeypatch: pytest.MonkeyPatch
):
    stub, socket_path = docker_stub
    monkeypatch.setattr("marrquee.docker_client.socket.gethostname", lambda: "abc123deadbeef")
    not_found = ("HTTP/1.1 404 Not Found", b'{"message": "no such container"}')
    found = ("HTTP/1.1 200 OK", b'{"State": {"Status": "running"}}')
    stub.respond_with_sequence([not_found, found])
    engine = SocketDockerEngine(socket_path=socket_path)

    container_id = await engine.self_container_id()

    assert container_id == "marrquee"
    assert stub.request_lines == [
        "GET /containers/abc123deadbeef/json HTTP/1.1",
        "GET /containers/marrquee/json HTTP/1.1",
    ]


async def test_self_container_id_is_none_when_neither_lookup_succeeds(
    docker_stub, monkeypatch: pytest.MonkeyPatch
):
    stub, socket_path = docker_stub
    monkeypatch.setattr("marrquee.docker_client.socket.gethostname", lambda: "abc123deadbeef")
    not_found = ("HTTP/1.1 404 Not Found", b'{"message": "no such container"}')
    stub.respond_with_sequence([not_found, not_found])
    engine = SocketDockerEngine(socket_path=socket_path)

    assert await engine.self_container_id() is None


# --- remove_container() ----------------------------------------------------


async def test_remove_container_sends_delete_with_force_and_treats_404_as_done(docker_stub):
    """FIRST TEST - the plan's weakest assumption: does the exact DELETE
    request the plan promised actually go out, and does Docker's 204/404/409
    table come back classified the way Cancel needs it to be?

    No `v=` on the query string on purpose - every volume this container
    could have is a bind mount, so there is nothing anonymous to lose.
    """
    stub, socket_path = docker_stub
    stub.respond_with_sequence(
        [
            ("HTTP/1.1 204 No Content", b""),
            ("HTTP/1.1 404 Not Found", b'{"message": "no such container: radarr"}'),
            ("HTTP/1.1 409 Conflict", b'{"message": "removal of container already in progress"}'),
        ]
    )
    engine = SocketDockerEngine(socket_path=socket_path)

    removed = await engine.remove_container("radarr")
    already_gone = await engine.remove_container("radarr")
    still_removing = await engine.remove_container("radarr")

    assert stub.request_lines[0] == "DELETE /containers/radarr?force=true HTTP/1.1"
    assert removed == ContainerRemoveResult(ok=True, detail=None)
    assert already_gone == ContainerRemoveResult(ok=True, detail=None)
    assert still_removing.ok is False


async def test_remove_container_failure_detail_names_the_status_and_dockers_own_message(
    docker_stub,
):
    stub, socket_path = docker_stub
    stub.respond_with_json(
        {"message": "removal of container already in progress"},
        status_line="HTTP/1.1 409 Conflict",
    )
    engine = SocketDockerEngine(socket_path=socket_path)

    result = await engine.remove_container("radarr")

    assert result.ok is False
    assert result.detail is not None
    assert "409" in result.detail
    assert "removal of container already in progress" in result.detail


async def test_remove_container_reports_a_connection_error_as_not_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    relative_name = "refusing.sock"
    raw_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    raw_socket.bind(relative_name)
    raw_socket.close()

    engine = SocketDockerEngine(socket_path=tmp_path / relative_name)
    result = await engine.remove_container("radarr")

    assert result.ok is False
    assert result.detail is not None


# --- stop_container() -------------------------------------------------------


async def test_stop_container_sends_post_with_the_timeout_as_a_query_param(docker_stub):
    """FIRST TEST - the plan's weakest assumption: does the exact POST the
    plan promised go out, with Docker's own `t=` query parameter carrying
    the gentle-stop budget?
    """
    stub, socket_path = docker_stub
    stub.respond_with("HTTP/1.1 204 No Content", b"")
    engine = SocketDockerEngine(socket_path=socket_path)

    result = await engine.stop_container("qbittorrent")

    assert stub.request_lines[0] == "POST /containers/qbittorrent/stop?t=30 HTTP/1.1"
    assert result == ContainerStopResult(ok=True, detail=None)


async def test_stop_container_honours_a_custom_timeout_in_the_query_string(docker_stub):
    stub, socket_path = docker_stub
    stub.respond_with("HTTP/1.1 204 No Content", b"")
    engine = SocketDockerEngine(socket_path=socket_path)

    await engine.stop_container("qbittorrent", timeout_seconds=5)

    assert stub.request_lines[0] == "POST /containers/qbittorrent/stop?t=5 HTTP/1.1"


@pytest.mark.parametrize("status_line", ["HTTP/1.1 204 No Content", "HTTP/1.1 304 Not Modified"])
async def test_stop_container_treats_204_and_304_as_ok(docker_stub, status_line: str) -> None:
    stub, socket_path = docker_stub
    stub.respond_with(status_line, b"")
    engine = SocketDockerEngine(socket_path=socket_path)

    result = await engine.stop_container("qbittorrent")

    assert result == ContainerStopResult(ok=True, detail=None)


async def test_stop_container_treats_a_missing_container_as_ok(docker_stub):
    """A mover already gone (an idempotent retry after a resume) must
    never turn a stop into a failure - the removal that follows is what
    actually has to succeed.
    """
    stub, socket_path = docker_stub
    stub.respond_with("HTTP/1.1 404 Not Found", b'{"message": "no such container: qbittorrent"}')
    engine = SocketDockerEngine(socket_path=socket_path)

    result = await engine.stop_container("qbittorrent")

    assert result == ContainerStopResult(ok=True, detail=None)


async def test_stop_container_reports_a_500_as_not_ok_with_dockers_own_message(docker_stub):
    stub, socket_path = docker_stub
    stub.respond_with_json({"message": "server error"}, status_line="HTTP/1.1 500 Server Error")
    engine = SocketDockerEngine(socket_path=socket_path)

    result = await engine.stop_container("qbittorrent")

    assert result.ok is False
    assert result.detail is not None
    assert "500" in result.detail
    assert "server error" in result.detail


async def test_stop_container_sends_a_request_timeout_ten_seconds_past_the_stop_budget() -> None:
    """The client's own 2s default (`_DEFAULT_TIMEOUT`) would give up long
    before a real 30s Docker stop finishes - this request must carry its
    own, generous timeout instead.
    """
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    engine = SocketDockerEngine(socket_path=Path("/dev/null"))
    original_client = engine._client  # type: ignore[attr-defined]

    def patched_client() -> httpx.AsyncClient:
        client = original_client()
        client._transport = transport  # type: ignore[attr-defined]
        return client

    engine._client = patched_client  # type: ignore[method-assign]

    result = await engine.stop_container("qbittorrent", timeout_seconds=30)

    assert result == ContainerStopResult(ok=True, detail=None)
    request = captured["request"]
    assert request.method == "POST"
    assert request.url.path == "/containers/qbittorrent/stop"
    assert request.url.query == b"t=30"
    timeout = request.extensions["timeout"]
    assert timeout["connect"] >= 40
    assert timeout["read"] >= 40


async def test_stop_container_reports_a_connection_error_as_not_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    relative_name = "refusing.sock"
    raw_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    raw_socket.bind(relative_name)
    raw_socket.close()

    engine = SocketDockerEngine(socket_path=tmp_path / relative_name)
    result = await engine.stop_container("qbittorrent")

    assert result.ok is False
    assert result.detail is not None


# --- exec_start() / exec_inspect() ------------------------------------------


async def test_exec_start_creates_then_starts_detached(docker_stub):
    """FIRST TEST - the plan's weakest assumption: does Sync now's detached
    exec actually issue the two-request shape the swagger promises, in
    order, with the bodies it promises?
    """
    stub, socket_path = docker_stub
    stub.respond_with_sequence(
        [
            ("HTTP/1.1 201 Created", b'{"Id": "abc"}'),
            ("HTTP/1.1 200 OK", b""),
        ]
    )
    engine = SocketDockerEngine(socket_path=socket_path)

    result = await engine.exec_start("recyclarr", ("recyclarr", "sync"))

    assert result == ExecStartResult(ok=True, exec_id="abc", detail=None)
    assert stub.request_lines == [
        "POST /containers/recyclarr/exec HTTP/1.1",
        "POST /exec/abc/start HTTP/1.1",
    ]
    assert json.loads(stub.request_bodies[0]) == {
        "AttachStdin": False,
        "AttachStdout": False,
        "AttachStderr": False,
        "Tty": False,
        "Cmd": ["recyclarr", "sync"],
    }
    assert json.loads(stub.request_bodies[1]) == {"Detach": True, "Tty": False}


async def test_exec_start_on_a_stopped_container_reports_409_as_not_ok(docker_stub):
    stub, socket_path = docker_stub
    stub.respond_with_json(
        {"message": "container 6d61 is not running"}, status_line="HTTP/1.1 409 Conflict"
    )
    engine = SocketDockerEngine(socket_path=socket_path)

    result = await engine.exec_start("recyclarr", ("recyclarr", "sync"))

    assert result.ok is False
    assert result.exec_id is None
    assert result.detail is not None
    assert "409" in result.detail
    assert "not running" in result.detail


async def test_exec_start_reports_a_failed_start_as_not_ok_but_still_carries_the_exec_id(
    docker_stub,
):
    """The create half can succeed (Docker minted an exec id) while the
    start half fails (the container paused or died in between) - that must
    still come back `ok=False`, or a sync that never actually started would
    be indistinguishable from one that did.
    """
    stub, socket_path = docker_stub
    stub.respond_with_sequence(
        [
            ("HTTP/1.1 201 Created", b'{"Id": "abc"}'),
            ("HTTP/1.1 409 Conflict", b'{"message": "container abc is paused"}'),
        ]
    )
    engine = SocketDockerEngine(socket_path=socket_path)

    result = await engine.exec_start("recyclarr", ("recyclarr", "sync"))

    assert result.ok is False
    assert result.exec_id == "abc"
    assert result.detail is not None
    assert "409" in result.detail
    assert "container abc is paused" in result.detail


async def test_exec_inspect_parses_running_and_exit_code_and_404_is_unknown(docker_stub):
    stub, socket_path = docker_stub
    stub.respond_with_json({"Running": True, "ExitCode": None})
    engine = SocketDockerEngine(socket_path=socket_path)

    running = await engine.exec_inspect("abc")
    assert running == ExecState(known=True, running=True, exit_code=None, detail=None)

    stub.respond_with_json({"Running": False, "ExitCode": 1})
    finished = await engine.exec_inspect("abc")
    assert finished == ExecState(known=True, running=False, exit_code=1, detail=None)

    stub.respond_with("HTTP/1.1 404 Not Found", b'{"message": "no such exec instance"}')
    unknown = await engine.exec_inspect("does-not-exist")
    assert unknown == ExecState(known=False, running=False, exit_code=None, detail=None)


# --- host_gateway() ----------------------------------------------------------


async def test_host_gateway_prefers_marrquee_falls_back_handles_host_mode(docker_stub):
    """FIRST TEST - a Plex-only stack never creates the `marrquee` network
    (compose only creates a service's own bridge network as a side effect of
    a `compose up` that includes it), so Marrquee's own networks - not the
    stack's - are the only reliable source of an address a host-networked
    Plex can be reached at.
    """
    stub, socket_path = docker_stub
    engine = SocketDockerEngine(socket_path=socket_path)

    # Prefers the `marrquee` network's own gateway over any other network.
    stub.respond_with_json(
        {
            "HostConfig": {"NetworkMode": "marrquee-apps_default"},
            "NetworkSettings": {
                "Networks": {
                    "bridge": {"Gateway": "172.17.0.1"},
                    "marrquee": {"Gateway": "172.19.0.1"},
                }
            },
        }
    )
    assert await engine.host_gateway("self") == "172.19.0.1"
    assert stub.request_lines[-1] == "GET /containers/self/json HTTP/1.1"

    # No `marrquee` network: falls back to the first non-empty gateway, in
    # network-name order - "bridge" sorts before "custom".
    stub.respond_with_json(
        {
            "HostConfig": {"NetworkMode": "bridge"},
            "NetworkSettings": {
                "Networks": {
                    "custom": {"Gateway": "172.20.0.1"},
                    "bridge": {"Gateway": ""},
                    "another": {"Gateway": "172.21.0.1"},
                }
            },
        }
    )
    assert await engine.host_gateway("self") == "172.21.0.1"

    # Marrquee itself is host-networked: 127.0.0.1, regardless of Networks.
    stub.respond_with_json(
        {
            "HostConfig": {"NetworkMode": "host"},
            "NetworkSettings": {"Networks": {"marrquee": {"Gateway": "172.19.0.1"}}},
        }
    )
    assert await engine.host_gateway("self") == "127.0.0.1"


async def test_host_gateway_returns_none_on_a_missing_container(docker_stub):
    stub, socket_path = docker_stub
    stub.respond_with_json(
        {"message": "no such container: self"}, status_line="HTTP/1.1 404 Not Found"
    )
    engine = SocketDockerEngine(socket_path=socket_path)

    assert await engine.host_gateway("self") is None


async def test_host_gateway_returns_none_when_no_network_has_a_gateway(docker_stub):
    stub, socket_path = docker_stub
    stub.respond_with_json(
        {
            "HostConfig": {"NetworkMode": "bridge"},
            "NetworkSettings": {"Networks": {"bridge": {"Gateway": ""}}},
        }
    )
    engine = SocketDockerEngine(socket_path=socket_path)

    assert await engine.host_gateway("self") is None


async def test_host_gateway_returns_none_on_a_malformed_body(docker_stub):
    stub, socket_path = docker_stub
    stub.respond_with("HTTP/1.1 200 OK", b"not json")
    engine = SocketDockerEngine(socket_path=socket_path)

    assert await engine.host_gateway("self") is None


async def test_host_gateway_returns_none_on_a_connection_error(tmp_path: Path) -> None:
    engine = SocketDockerEngine(socket_path=tmp_path / "no-such.sock")

    assert await engine.host_gateway("self") is None


# --- probe_host_path() --------------------------------------------------------


async def test_probe_host_path_classifies_a_missing_bind_source_as_absent(docker_stub):
    """FIRST TEST - the exact request shape: inspect self for its own image
    id, an ignored cleanup DELETE, then the one POST whose CREATE-time
    refusal is what decides "does the host have this path". Docker's own
    substring for a missing `Mounts` bind source is what turns a 400 into
    "absent" rather than "couldn't tell".
    """
    stub, socket_path = docker_stub
    stub.respond_with_sequence(
        [
            ("HTTP/1.1 200 OK", b'{"Image": "sha256:x"}'),
            ("HTTP/1.1 404 Not Found", b'{"message": "no such container"}'),
            (
                "HTTP/1.1 400 Bad Request",
                b'{"message": "invalid mount config for type \\"bind\\": '
                b'bind source path does not exist: /dev/dri/renderD128"}',
            ),
        ]
    )
    engine = SocketDockerEngine(socket_path=socket_path)

    result = await engine.probe_host_path("self", "/dev/dri/renderD128")

    assert result == HostPathProbe(result="absent", detail=None)
    assert stub.request_lines == [
        "GET /containers/self/json HTTP/1.1",
        "DELETE /containers/marrquee-graphics-check?force=true HTTP/1.1",
        "POST /containers/create?name=marrquee-graphics-check HTTP/1.1",
    ]
    created_body = json.loads(stub.request_bodies[-1])
    assert created_body == {
        "Image": "sha256:x",
        "Labels": {"com.marrquee.purpose": "graphics-check"},
        "HostConfig": {
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": "/dev/dri/renderD128",
                    "Target": "/marrquee-check",
                    "ReadOnly": True,
                }
            ]
        },
    }


async def test_probe_host_path_classifies_a_successful_create_as_present_and_removes_it(
    docker_stub,
):
    stub, socket_path = docker_stub
    stub.respond_with_sequence(
        [
            ("HTTP/1.1 200 OK", b'{"Image": "sha256:x"}'),
            ("HTTP/1.1 404 Not Found", b'{"message": "no such container"}'),
            ("HTTP/1.1 201 Created", b'{"Id": "c1"}'),
            ("HTTP/1.1 204 No Content", b""),
        ]
    )
    engine = SocketDockerEngine(socket_path=socket_path)

    result = await engine.probe_host_path("self", "/dev/dri/renderD128")

    assert result == HostPathProbe(result="present", detail=None)
    assert stub.request_lines[-1] == "DELETE /containers/c1?force=true HTTP/1.1"


async def test_probe_host_path_still_answers_present_when_its_own_cleanup_delete_fails(
    docker_stub, caplog: pytest.LogCaptureFixture
):
    """A cleanup DELETE that fails is logged, never surfaced as an error -
    the check's own answer is already decided by the CREATE that came
    before it, and a leftover container never runs (it was never started),
    so it is harmless rather than something the owner must be told about.
    """
    stub, socket_path = docker_stub
    stub.respond_with_sequence(
        [
            ("HTTP/1.1 200 OK", b'{"Image": "sha256:x"}'),
            ("HTTP/1.1 404 Not Found", b'{"message": "no such container"}'),
            ("HTTP/1.1 201 Created", b'{"Id": "c1"}'),
            ("HTTP/1.1 500 Internal Server Error", b'{"message": "daemon is busy"}'),
        ]
    )
    engine = SocketDockerEngine(socket_path=socket_path)

    with caplog.at_level("WARNING"):
        result = await engine.probe_host_path("self", "/dev/dri/renderD128")

    assert result == HostPathProbe(result="present", detail=None)
    assert stub.request_lines[-1] == "DELETE /containers/c1?force=true HTTP/1.1"
    assert "c1" in caplog.text


async def test_probe_host_path_never_starts_the_container_it_creates(docker_stub):
    """No matter the classification, `/start` is never called - the whole
    point of the probe is that the check container never runs.
    """
    stub, socket_path = docker_stub
    stub.respond_with_sequence(
        [
            ("HTTP/1.1 200 OK", b'{"Image": "sha256:x"}'),
            ("HTTP/1.1 404 Not Found", b'{"message": "no such container"}'),
            ("HTTP/1.1 201 Created", b'{"Id": "c1"}'),
            ("HTTP/1.1 204 No Content", b""),
        ]
    )
    engine = SocketDockerEngine(socket_path=socket_path)

    await engine.probe_host_path("self", "/dev/dri/renderD128")

    assert not any(line.split(" ")[1].endswith("/start") for line in stub.request_lines)


async def test_probe_host_path_classifies_a_daemon_error_as_unknown(docker_stub):
    stub, socket_path = docker_stub
    stub.respond_with_sequence(
        [
            ("HTTP/1.1 200 OK", b'{"Image": "sha256:x"}'),
            ("HTTP/1.1 404 Not Found", b'{"message": "no such container"}'),
            ("HTTP/1.1 500 Internal Server Error", b'{"message": "daemon is busy"}'),
        ]
    )
    engine = SocketDockerEngine(socket_path=socket_path)

    result = await engine.probe_host_path("self", "/dev/dri/renderD128")

    assert result.result == "unknown"
    assert result.detail is not None


async def test_probe_host_path_classifies_an_unrelated_400_as_unknown_not_absent(docker_stub):
    """A 400 is only "absent" when Docker's own message is specifically
    about a missing bind source - any other 400 (a malformed body, an
    unknown image, a request Docker refused for some other reason) must
    never be read as a definitive "no chip".
    """
    stub, socket_path = docker_stub
    stub.respond_with_sequence(
        [
            ("HTTP/1.1 200 OK", b'{"Image": "sha256:x"}'),
            ("HTTP/1.1 404 Not Found", b'{"message": "no such container"}'),
            ("HTTP/1.1 400 Bad Request", b'{"message": "invalid reference format"}'),
        ]
    )
    engine = SocketDockerEngine(socket_path=socket_path)

    result = await engine.probe_host_path("self", "/dev/dri/renderD128")

    assert result.result == "unknown"
    assert result.detail is not None


async def test_probe_host_path_classifies_a_409_name_conflict_as_unknown(docker_stub):
    stub, socket_path = docker_stub
    stub.respond_with_sequence(
        [
            ("HTTP/1.1 200 OK", b'{"Image": "sha256:x"}'),
            ("HTTP/1.1 404 Not Found", b'{"message": "no such container"}'),
            (
                "HTTP/1.1 409 Conflict",
                b'{"message": "Conflict. The container name '
                b'\\"/marrquee-graphics-check\\" is already in use"}',
            ),
        ]
    )
    engine = SocketDockerEngine(socket_path=socket_path)

    result = await engine.probe_host_path("self", "/dev/dri/renderD128")

    assert result.result == "unknown"


async def test_probe_host_path_reports_unknown_when_the_self_inspect_has_no_image(docker_stub):
    stub, socket_path = docker_stub
    stub.respond_with_json({"State": {"Status": "running"}})
    engine = SocketDockerEngine(socket_path=socket_path)

    result = await engine.probe_host_path("self", "/dev/dri/renderD128")

    assert result.result == "unknown"
    # Never gets far enough to touch create or delete at all.
    assert stub.request_lines == ["GET /containers/self/json HTTP/1.1"]


async def test_probe_host_path_reports_unknown_on_a_connection_error(tmp_path: Path) -> None:
    engine = SocketDockerEngine(socket_path=tmp_path / "no-such.sock")

    result = await engine.probe_host_path("self", "/dev/dri/renderD128")

    assert result.result == "unknown"
    assert result.detail is not None


# --- FakeDockerEngine.probe_host_path() ---------------------------------------


async def test_fake_probe_host_path_present_absent_and_unknown() -> None:
    fake = FakeDockerEngine(
        DockerStatus(connected=True),
        self_container_id="marrquee",
        host_paths={"/dev/dri/renderD128"},
    )

    present = await fake.probe_host_path("marrquee", "/dev/dri/renderD128")
    absent = await fake.probe_host_path("marrquee", "/dev/dri/card0")
    unknown = await fake.probe_host_path("someone-else", "/dev/dri/renderD128")

    assert present == HostPathProbe(result="present", detail=None)
    assert absent == HostPathProbe(result="absent", detail=None)
    assert unknown == HostPathProbe(result="unknown", detail=None)
    assert fake.calls == [
        ("probe_host_path", ("marrquee", "/dev/dri/renderD128")),
        ("probe_host_path", ("marrquee", "/dev/dri/card0")),
        ("probe_host_path", ("someone-else", "/dev/dri/renderD128")),
    ]


async def test_fake_probe_host_path_defaults_to_no_chip() -> None:
    """Every existing test's `FakeDockerEngine()` gets no `host_paths=` at
    all - the default keeps every one of them chip-free.
    """
    fake = FakeDockerEngine(DockerStatus(connected=True), self_container_id="marrquee")

    result = await fake.probe_host_path("marrquee", "/dev/dri/renderD128")

    assert result == HostPathProbe(result="absent", detail=None)


# --- FakeDockerEngine.host_gateway() -----------------------------------------


async def test_fake_host_gateway_answers_only_for_the_self_container() -> None:
    fake = FakeDockerEngine(DockerStatus(connected=True), self_container_id="marrquee")

    address = await fake.host_gateway("marrquee")
    unknown = await fake.host_gateway("sonarr")

    assert address == "172.18.0.1"
    assert unknown is None
    assert fake.calls == [("host_gateway", ("marrquee",)), ("host_gateway", ("sonarr",))]


async def test_fake_host_gateway_is_scriptable_and_can_be_turned_off() -> None:
    fake = FakeDockerEngine(
        DockerStatus(connected=True), self_container_id="marrquee", host_gateway="10.0.0.1"
    )
    assert await fake.host_gateway("marrquee") == "10.0.0.1"

    off = FakeDockerEngine(
        DockerStatus(connected=True), self_container_id="marrquee", host_gateway=None
    )
    assert await off.host_gateway("marrquee") is None


# --- FakeDockerEngine.exec_start() / exec_inspect() -------------------------


async def test_the_fake_refuses_exec_on_a_stopped_container_then_reports_scripted_exit() -> None:
    running_recyclarr = ContainerSnapshot(
        name="recyclarr", exists=True, state="running", exit_code=None, image=None, detail=None
    )
    fake = FakeDockerEngine(
        DockerStatus(connected=True),
        containers={"recyclarr": running_recyclarr},
        exec_exit_codes={"recyclarr": 1},
        exec_running_polls=2,
    )

    refused = await fake.exec_start("prowlarr", ("recyclarr", "sync"))
    assert refused == ExecStartResult(ok=False, exec_id=None, detail="container is not running")

    started = await fake.exec_start("recyclarr", ("recyclarr", "sync"))
    assert started.ok is True
    assert started.exec_id is not None

    first_poll = await fake.exec_inspect(started.exec_id)
    second_poll = await fake.exec_inspect(started.exec_id)
    third_poll = await fake.exec_inspect(started.exec_id)

    assert first_poll == ExecState(known=True, running=True, exit_code=None, detail=None)
    assert second_poll == ExecState(known=True, running=True, exit_code=None, detail=None)
    assert third_poll == ExecState(known=True, running=False, exit_code=1, detail=None)

    assert await fake.exec_inspect("no-such-exec") == ExecState(
        known=False, running=False, exit_code=None, detail=None
    )


# --- FakeDockerEngine -----------------------------------------------------


async def test_the_fake_satisfies_the_widened_protocol_and_records_its_calls() -> None:
    """Structural assertion: mypy rejects this file if FakeDockerEngine ever
    drifts out of step with the widened DockerEngine protocol.
    """

    def _accepts(engine: DockerEngine) -> DockerEngine:
        return engine

    running_sonarr = ContainerSnapshot(
        name="sonarr",
        exists=True,
        state="running",
        exit_code=None,
        image="lscr.io/linuxserver/sonarr:latest",
        detail=None,
    )
    fake = FakeDockerEngine(
        DockerStatus(connected=True),
        containers={"sonarr": running_sonarr},
        images={"lscr.io/linuxserver/sonarr:latest"},
        compose_results={"sonarr": ComposeResult(ok=True, exit_code=0, output="")},
        network_connects=True,
    )
    assert _accepts(fake) is fake

    await fake.status()
    await fake.inspect("sonarr")
    await fake.image_present("lscr.io/linuxserver/sonarr:latest")
    await fake.connect_network("marrquee", "abc")
    await fake.logs("sonarr")
    await fake.compose_up("marrquee", Path("/tmp/compose.yaml"), "sonarr")
    await fake.self_container_id()
    await fake.remove_container("sonarr")
    await fake.stop_container("sonarr")
    await fake.host_gateway("abc")
    await fake.probe_host_path("abc", "/dev/dri/renderD128")

    assert [name for name, _args in fake.calls] == [
        "status",
        "inspect",
        "image_present",
        "connect_network",
        "logs",
        "compose_up",
        "self_container_id",
        "remove_container",
        "stop_container",
        "host_gateway",
        "probe_host_path",
    ]


async def test_the_fake_refuses_to_connect_to_a_network_that_does_not_exist_yet() -> None:
    """The regression this whole shape exists to catch: on a fresh host,
    nothing has ever created the stack's network - only a successful
    `compose up` does that. An always-succeeding fake could never have
    caught the real deploy engine trying to join it too early.
    """
    fake = FakeDockerEngine(DockerStatus(connected=True))

    result = await fake.connect_network("marrquee", "abc")

    assert result.ok is False


async def test_a_successful_compose_up_brings_the_network_into_existence() -> None:
    fake = FakeDockerEngine(
        DockerStatus(connected=True),
        compose_results={"prowlarr": ComposeResult(ok=True, exit_code=0, output="")},
    )

    await fake.compose_up("marrquee", Path("/tmp/compose.yaml"), "prowlarr")
    result = await fake.connect_network("marrquee", "abc")

    assert result.ok is True


async def test_a_failed_compose_up_does_not_create_the_network() -> None:
    fake = FakeDockerEngine(
        DockerStatus(connected=True),
        compose_results={"prowlarr": ComposeResult(ok=False, exit_code=1, output="boom")},
    )

    await fake.compose_up("marrquee", Path("/tmp/compose.yaml"), "prowlarr")
    result = await fake.connect_network("marrquee", "abc")

    assert result.ok is False


async def test_the_fake_stop_container_models_an_exited_container_and_ignores_an_absent_one() -> (
    None
):
    running = ContainerSnapshot(
        name="qbittorrent",
        exists=True,
        state="running",
        exit_code=None,
        image="lscr.io/linuxserver/qbittorrent:5.2.3",
        detail=None,
    )
    fake = FakeDockerEngine(DockerStatus(connected=True), containers={"qbittorrent": running})

    stopped = await fake.stop_container("qbittorrent")
    absent = await fake.stop_container("does-not-exist")

    assert stopped == ContainerStopResult(ok=True, detail=None)
    assert absent == ContainerStopResult(ok=True, detail=None)
    snapshot = await fake.inspect("qbittorrent")
    assert snapshot.state == "exited"
    assert [name for name, _args in fake.calls if name == "stop_container"] == [
        "stop_container",
        "stop_container",
    ]


async def test_the_fake_records_a_recreate_call_under_its_own_name() -> None:
    """A distinct call name for `recreate=True` - never `compose_up` with an
    extra bool tucked on the end - so every existing `.calls` assertion
    written against the old signature stays untouched.
    """
    fake = FakeDockerEngine(
        DockerStatus(connected=True),
        compose_results={"sonarr": ComposeResult(ok=True, exit_code=0, output="")},
    )

    await fake.compose_up("marrquee", Path("/tmp/compose.yaml"), "sonarr")
    await fake.compose_up("marrquee", Path("/tmp/compose.yaml"), "sonarr", recreate=True)

    assert fake.calls == [
        ("compose_up", ("marrquee", "/tmp/compose.yaml", "sonarr")),
        ("compose_up_recreate", ("marrquee", "/tmp/compose.yaml", "sonarr")),
    ]


async def test_network_exists_can_be_pre_seeded_for_a_resume_scenario() -> None:
    fake = FakeDockerEngine(DockerStatus(connected=True), network_exists=True)

    result = await fake.connect_network("marrquee", "abc")

    assert result.ok is True


async def test_network_connects_override_forces_a_fixed_answer_regardless_of_history() -> None:
    """An explicit override for tests that don't care about network
    sequencing at all - like the structural protocol test above, which
    calls `connect_network` before `compose_up` on purpose.
    """
    always_fails = FakeDockerEngine(DockerStatus(connected=True), network_connects=False)

    result = await always_fails.connect_network("marrquee", "abc")

    assert result.ok is False


async def test_the_fake_reports_unscripted_containers_and_images_as_absent() -> None:
    fake = FakeDockerEngine(DockerStatus(connected=True))

    snapshot = await fake.inspect("sonarr")
    present = await fake.image_present("lscr.io/linuxserver/sonarr:latest")

    assert snapshot.exists is False
    assert present is False


async def test_the_fake_defaults_compose_up_to_a_clean_success() -> None:
    fake = FakeDockerEngine(DockerStatus(connected=True))

    result = await fake.compose_up("marrquee", Path("/tmp/compose.yaml"), "sonarr")

    assert result == ComposeResult(ok=True, exit_code=0, output="")


async def test_the_fakes_self_container_id_is_scriptable() -> None:
    """The deploy engine needs to test both the happy path and "Marrquee
    can't find its own container" (running outside Docker in dev) - a fixed
    return value can only ever cover one of those.
    """
    found = FakeDockerEngine(DockerStatus(connected=True), self_container_id="abc123")
    missing = FakeDockerEngine(DockerStatus(connected=True), self_container_id=None)

    assert await found.self_container_id() == "abc123"
    assert await missing.self_container_id() is None


async def test_the_fakes_self_container_id_defaults_to_a_fixed_id() -> None:
    """Unscripted, the fake keeps behaving exactly as it did before this
    knob existed, so every earlier test that never mentions the parameter
    stays green.
    """
    fake = FakeDockerEngine(DockerStatus(connected=True))

    assert await fake.self_container_id() == "fake-marrquee-container"


# --- FakeDockerEngine.remove_container() -----------------------------------


async def test_the_fake_forgets_a_removed_container() -> None:
    """The regression this shape exists to catch: a fake that always
    answers "yes" to a remove could never prove Cancel actually stopped
    seeing the container it just removed.
    """
    radarr = ContainerSnapshot(
        name="radarr", exists=True, state="running", exit_code=None, image=None, detail=None
    )
    fake = FakeDockerEngine(DockerStatus(connected=True), containers={"radarr": radarr})

    result = await fake.remove_container("radarr")
    snapshot = await fake.inspect("radarr")

    assert result == ContainerRemoveResult(ok=True, detail=None)
    assert snapshot.exists is False


async def test_the_fake_defaults_remove_container_to_ok() -> None:
    fake = FakeDockerEngine(DockerStatus(connected=True))

    result = await fake.remove_container("radarr")

    assert result.ok is True


# --- FakeDockerEngine: scripted logs and health frames ---------------------


async def test_the_fake_returns_scripted_logs_by_name_else_empty() -> None:
    fake = FakeDockerEngine(DockerStatus(connected=True), logs={"gluetun": "...AUTH_FAILED..."})

    assert await fake.logs("gluetun") == "...AUTH_FAILED..."
    assert await fake.logs("sonarr") == ""


async def test_the_fake_drains_scripted_frames_then_repeats_the_last_one() -> None:
    """A VPN that becomes healthy over time is state the fake must model -
    see the docker-fakes-model-state rule.
    """
    starting = ContainerSnapshot(
        name="gluetun",
        exists=True,
        state="running",
        exit_code=None,
        image=None,
        detail=None,
        health="starting",
    )
    healthy = ContainerSnapshot(
        name="gluetun",
        exists=True,
        state="running",
        exit_code=None,
        image=None,
        detail=None,
        health="healthy",
    )
    fake = FakeDockerEngine(DockerStatus(connected=True), frames={"gluetun": [starting, healthy]})

    results = [(await fake.inspect("gluetun")).health for _ in range(4)]

    assert results == ["starting", "healthy", "healthy", "healthy"]


async def test_scripted_frames_win_over_containers_even_after_a_recreate() -> None:
    """Amendment: `frames[name]` always wins over `_containers`, including a
    snapshot `_model_recreate` itself just wrote - a scripted health-check
    script must not be shadowed by compose recreating the container.
    """
    stale = ContainerSnapshot(
        name="gluetun", exists=True, state="running", exit_code=None, image=None, detail=None
    )
    scripted = ContainerSnapshot(
        name="gluetun",
        exists=True,
        state="running",
        exit_code=None,
        image=None,
        detail=None,
        health="healthy",
    )
    fake = FakeDockerEngine(
        DockerStatus(connected=True),
        containers={"gluetun": stale},
        frames={"gluetun": [scripted]},
        compose_results={"gluetun": ComposeResult(ok=True, exit_code=0, output="")},
    )

    await fake.compose_up("marrquee-apps", Path("/tmp/compose.yaml"), "gluetun", recreate=True)
    result = await fake.inspect("gluetun")

    assert result.health == "healthy"


async def test_model_recreate_carries_the_previous_health_forward() -> None:
    """A real recreate doesn't reset Docker's own health-check history -
    the container keeps its last reported health until the new health check
    runs and reports otherwise.
    """
    previously_healthy = ContainerSnapshot(
        name="gluetun",
        exists=True,
        state="running",
        exit_code=None,
        image=None,
        detail=None,
        health="healthy",
    )
    fake = FakeDockerEngine(
        DockerStatus(connected=True),
        containers={"gluetun": previously_healthy},
        compose_results={"gluetun": ComposeResult(ok=True, exit_code=0, output="")},
    )

    await fake.compose_up("marrquee-apps", Path("/tmp/compose.yaml"), "gluetun", recreate=True)
    result = await fake.inspect("gluetun")

    assert result.health == "healthy"


async def test_a_scripted_remove_failure_keeps_the_container_in_place() -> None:
    radarr = ContainerSnapshot(
        name="radarr", exists=True, state="running", exit_code=None, image=None, detail=None
    )
    fake = FakeDockerEngine(
        DockerStatus(connected=True),
        containers={"radarr": radarr},
        remove_results={"radarr": False},
    )

    result = await fake.remove_container("radarr")
    snapshot = await fake.inspect("radarr")

    assert result.ok is False
    assert snapshot.exists is True

"""Tests for the Docker seam: SocketDockerEngine and FakeDockerEngine.

Nothing here talks to a real Docker daemon - there isn't one on this machine.
`docker_stub` (see conftest.py) is a stand-in unix-socket server that records
what was sent to it and replies with a fixed, controllable response.
"""

from __future__ import annotations

import asyncio
import socket
import struct
from pathlib import Path

import pytest

from marrquee.docker_client import (
    ComposeResult,
    ContainerSnapshot,
    DockerEngine,
    DockerFailure,
    DockerStatus,
    FakeDockerEngine,
    SocketDockerEngine,
)


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

    connected = await engine.connect_network("marrquee", "abc123")

    assert connected is True
    assert stub.request_lines[0] == "POST /networks/marrquee/connect HTTP/1.1"


async def test_connect_network_treats_already_connected_as_success(docker_stub):
    stub, socket_path = docker_stub
    stub.respond_with_json(
        {"message": "endpoint with name marrquee already exists in network marrquee"},
        status_line="HTTP/1.1 403 Forbidden",
    )
    engine = SocketDockerEngine(socket_path=socket_path)

    assert await engine.connect_network("marrquee", "abc123") is True


async def test_connect_network_treats_a_409_already_exists_as_success(docker_stub):
    stub, socket_path = docker_stub
    stub.respond_with_json(
        {"message": "endpoint already exists"}, status_line="HTTP/1.1 409 Conflict"
    )
    engine = SocketDockerEngine(socket_path=socket_path)

    assert await engine.connect_network("marrquee", "abc123") is True


async def test_connect_network_reports_a_genuine_failure_as_false(docker_stub):
    stub, socket_path = docker_stub
    stub.respond_with_json(
        {"message": "network marrquee not found"}, status_line="HTTP/1.1 404 Not Found"
    )
    engine = SocketDockerEngine(socket_path=socket_path)

    assert await engine.connect_network("marrquee", "abc123") is False


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
    assert result.ok is True


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

    assert [name for name, _args in fake.calls] == [
        "status",
        "inspect",
        "image_present",
        "connect_network",
        "logs",
        "compose_up",
        "self_container_id",
    ]


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

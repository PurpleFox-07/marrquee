"""Tests for the Docker seam: SocketDockerEngine and FakeDockerEngine.

Nothing here talks to a real Docker daemon - there isn't one on this machine.
`docker_stub` (see conftest.py) is a stand-in unix-socket server that records
what was sent to it and replies with a fixed, controllable response.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from marrquee.docker_client import (
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


async def test_issues_no_request_other_than_get_version(docker_stub):
    """The safety rule - stated as a testable property, not a promise - is that
    this engine never starts, stops or modifies anything. One GET, nothing else.
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

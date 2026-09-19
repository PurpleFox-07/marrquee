"""Tests for the alive page and its health check.

Every test builds the app through `create_app`, injecting a `Settings` that
points at a throwaway directory and a `DockerEngine` that never touches a
real socket - the seam Chunk 2 built and this chunk wires up.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marrquee import __version__
from marrquee.config import Settings
from marrquee.docker_client import (
    ComposeResult,
    ContainerSnapshot,
    DockerFailure,
    DockerStatus,
    FakeDockerEngine,
    NetworkConnectResult,
)
from marrquee.main import create_app


class _CountingDockerEngine:
    """Records how many times `status()` was awaited, to prove nothing caches it.

    The other DockerEngine operations are never exercised by the alive page -
    they exist here only so this stays a structurally valid DockerEngine
    after Chunk 4 widened the protocol.
    """

    def __init__(self, status: DockerStatus) -> None:
        self._status = status
        self.call_count = 0

    async def status(self) -> DockerStatus:
        self.call_count += 1
        return self._status

    async def inspect(self, name: str) -> ContainerSnapshot:
        raise NotImplementedError("the alive page never inspects a container")

    async def image_present(self, reference: str) -> bool:
        raise NotImplementedError("the alive page never checks for an image")

    async def connect_network(self, network: str, container: str) -> NetworkConnectResult:
        raise NotImplementedError("the alive page never joins a network")

    async def logs(self, name: str, tail: int = 50) -> str:
        raise NotImplementedError("the alive page never fetches logs")

    async def compose_up(self, project: str, compose_file: Path, service: str) -> ComposeResult:
        raise NotImplementedError("the alive page never runs compose")

    async def self_container_id(self) -> str | None:
        raise NotImplementedError("the alive page never looks up its own container")


class _ExplodingDockerEngine:
    """A DockerEngine whose every method always raises.

    Proves a caller never awaits any of them.
    """

    async def status(self) -> DockerStatus:
        raise RuntimeError("healthz must never call the Docker engine")

    async def inspect(self, name: str) -> ContainerSnapshot:
        raise RuntimeError("healthz must never call the Docker engine")

    async def image_present(self, reference: str) -> bool:
        raise RuntimeError("healthz must never call the Docker engine")

    async def connect_network(self, network: str, container: str) -> NetworkConnectResult:
        raise RuntimeError("healthz must never call the Docker engine")

    async def logs(self, name: str, tail: int = 50) -> str:
        raise RuntimeError("healthz must never call the Docker engine")

    async def compose_up(self, project: str, compose_file: Path, service: str) -> ComposeResult:
        raise RuntimeError("healthz must never call the Docker engine")

    async def self_container_id(self) -> str | None:
        raise RuntimeError("healthz must never call the Docker engine")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(config_dir=tmp_path / "config")


def _client(settings: Settings, status: DockerStatus) -> TestClient:
    app = create_app(settings=settings, engine=FakeDockerEngine(status))
    return TestClient(app)


def test_healthz_is_200_with_a_docker_failure_engine(settings: Settings) -> None:
    client = _client(settings, DockerStatus(connected=False, failure=DockerFailure.SOCKET_MISSING))

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "app": "marrquee", "version": __version__}


def test_healthz_never_calls_the_engine(settings: Settings) -> None:
    app = create_app(settings=settings, engine=_ExplodingDockerEngine())
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 200


def test_the_page_renders_connected_with_the_daemon_version(settings: Settings) -> None:
    client = _client(settings, DockerStatus(connected=True, version="27.3.1", api_version="1.47"))

    response = client.get("/")

    assert response.status_code == 200
    assert "Talking to Docker" in response.text
    assert "27.3.1" in response.text


@pytest.mark.parametrize(
    ("failure", "expected_title"),
    [
        (DockerFailure.SOCKET_MISSING, "Marrquee can&#39;t see Docker"),
        (DockerFailure.PERMISSION_DENIED, "Marrquee isn&#39;t allowed to talk to Docker"),
        (DockerFailure.NO_ANSWER, "Docker didn&#39;t answer"),
        (DockerFailure.BAD_RESPONSE, "Docker answered in a way Marrquee didn&#39;t understand"),
    ],
)
def test_the_page_renders_each_docker_failure_with_its_own_title(
    settings: Settings, failure: DockerFailure, expected_title: str
) -> None:
    client = _client(settings, DockerStatus(connected=False, failure=failure))

    response = client.get("/")

    assert response.status_code == 200
    assert expected_title in response.text


def test_the_page_never_shows_raw_technical_detail(settings: Settings) -> None:
    client = _client(
        settings,
        DockerStatus(
            connected=False,
            failure=DockerFailure.PERMISSION_DENIED,
            detail="[Errno 13] Permission denied: '/var/run/docker.sock'",
        ),
    )

    response = client.get("/")

    assert "Errno" not in response.text
    assert "Permission denied:" not in response.text


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file mode bits")
def test_the_page_reports_a_settings_folder_it_cannot_write_to(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o555)
    settings = Settings(config_dir=locked)

    try:
        client = _client(settings, DockerStatus(connected=True, version="27.3.1"))
        response = client.get("/")
    finally:
        locked.chmod(0o755)

    assert response.status_code == 200
    assert "Marrquee can&#39;t save its settings" in response.text


def test_the_page_reports_a_settings_folder_it_just_created(tmp_path: Path) -> None:
    target = tmp_path / "not-yet-created"
    settings = Settings(config_dir=target)

    client = _client(settings, DockerStatus(connected=True, version="27.3.1"))
    response = client.get("/")

    assert response.status_code == 200
    assert "Settings folder ready" in response.text
    assert target.is_dir()


def test_both_checks_rerun_on_every_request(settings: Settings) -> None:
    engine = _CountingDockerEngine(DockerStatus(connected=True, version="27.3.1"))
    app = create_app(settings=settings, engine=engine)
    client = TestClient(app)

    client.get("/")
    client.get("/")

    assert engine.call_count == 2


def test_tokens_css_is_linked_before_app_css(settings: Settings) -> None:
    client = _client(settings, DockerStatus(connected=True, version="27.3.1"))

    response = client.get("/")

    tokens_index = response.text.index("tokens.css")
    app_index = response.text.index("app.css")
    assert tokens_index < app_index


def test_the_check_again_control_is_a_link_to_root_styled_as_the_pill_button(
    settings: Settings,
) -> None:
    client = _client(settings, DockerStatus(connected=True, version="27.3.1"))

    response = client.get("/")

    assert '<a class="btn-primary" href="/">Check again</a>' in response.text


def test_the_page_declares_lang_viewport_and_color_scheme(settings: Settings) -> None:
    client = _client(settings, DockerStatus(connected=True, version="27.3.1"))

    response = client.get("/")

    assert '<html lang="en">' in response.text
    assert 'name="viewport" content="width=device-width, initial-scale=1"' in response.text
    assert 'name="color-scheme" content="dark"' in response.text


def test_status_icons_are_aria_hidden_and_state_is_in_the_title_text(settings: Settings) -> None:
    client = _client(settings, DockerStatus(connected=True, version="27.3.1"))

    response = client.get("/")

    # Two status rows on the page, each with a decorative, aria-hidden icon -
    # the wording of the title carries the state, not the icon's colour.
    assert response.text.count('aria-hidden="true"') == 2
    assert "Talking to Docker" in response.text
    assert "Settings folder ready" in response.text

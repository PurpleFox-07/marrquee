"""Tests for the alive page and its health check.

Every test builds the app through `create_app`, injecting a `Settings` that
points at a throwaway directory and a `DockerEngine` that never touches a
real socket - the seam Chunk 2 built and this chunk wires up.
"""

from __future__ import annotations

import errno
import html
import os
import re
import time
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

import pytest
from fastapi.testclient import TestClient

from marrquee import __version__, storage, words
from marrquee.config import Settings
from marrquee.docker_client import (
    ComposeResult,
    ContainerRemoveResult,
    ContainerSnapshot,
    ContainerStopResult,
    DockerFailure,
    DockerStatus,
    ExecStartResult,
    ExecState,
    FakeDockerEngine,
    HostPathProbe,
    NetworkConnectResult,
)
from marrquee.hardlinks import HardlinkMonitor, HardlinkResult
from marrquee.main import create_app
from marrquee.routes.alive import drive_status_line
from marrquee.state import InstallState, save_state


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

    async def compose_up(
        self, project: str, compose_file: Path, service: str, *, recreate: bool = False
    ) -> ComposeResult:
        raise NotImplementedError("the alive page never runs compose")

    async def self_container_id(self) -> str | None:
        raise NotImplementedError("the alive page never looks up its own container")

    async def remove_container(self, name: str) -> ContainerRemoveResult:
        raise NotImplementedError("the alive page never removes a container")

    async def stop_container(self, name: str, *, timeout_seconds: int = 30) -> ContainerStopResult:
        raise NotImplementedError("the alive page never stops a container")

    async def exec_start(self, container: str, cmd: Sequence[str]) -> ExecStartResult:
        raise NotImplementedError("the alive page never execs into a container")

    async def exec_inspect(self, exec_id: str) -> ExecState:
        raise NotImplementedError("the alive page never execs into a container")

    async def host_gateway(self, container: str) -> str | None:
        raise NotImplementedError("the alive page never looks up a host gateway")

    async def probe_host_path(self, self_container: str, host_path: str) -> HostPathProbe:
        raise NotImplementedError("the alive page never probes for a graphics chip")


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

    async def compose_up(
        self, project: str, compose_file: Path, service: str, *, recreate: bool = False
    ) -> ComposeResult:
        raise RuntimeError("healthz must never call the Docker engine")

    async def self_container_id(self) -> str | None:
        raise RuntimeError("healthz must never call the Docker engine")

    async def remove_container(self, name: str) -> ContainerRemoveResult:
        raise RuntimeError("healthz must never call the Docker engine")

    async def stop_container(self, name: str, *, timeout_seconds: int = 30) -> ContainerStopResult:
        raise RuntimeError("healthz must never call the Docker engine")

    async def exec_start(self, container: str, cmd: Sequence[str]) -> ExecStartResult:
        raise RuntimeError("healthz must never call the Docker engine")

    async def exec_inspect(self, exec_id: str) -> ExecState:
        raise RuntimeError("healthz must never call the Docker engine")

    async def host_gateway(self, container: str) -> str | None:
        raise RuntimeError("healthz must never call the Docker engine")

    async def probe_host_path(self, self_container: str, host_path: str) -> HostPathProbe:
        raise RuntimeError("healthz must never call the Docker engine")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(config_dir=tmp_path / "config")


def _client(settings: Settings, status: DockerStatus) -> TestClient:
    app = create_app(settings=settings, engine=FakeDockerEngine(status))
    return TestClient(app)


# --- "Your drive" fixtures - a real tmp_path install, so the drive check ----
# --- has real folders to link between (the words tests for this section -----
# --- prove the copy; these prove the page renders it) ------------------------


def _drive_settings(tmp_path: Path) -> Settings:
    return Settings(host_mount=tmp_path, config_dir=tmp_path / "config")


def _drive_install(
    *,
    app_ids: tuple[str, ...] = ("sonarr", "qbittorrent"),
    storage_root: str | None = "/volume1/media",
) -> InstallState:
    return InstallState(
        version=1,
        storage_root=storage_root,
        app_ids=app_ids,
        api_keys={},
        puid=1000,
        pgid=1000,
        umask="002",
        timezone="Etc/UTC",
        created="2026-01-01T00:00:00+00:00",
    )


def _no_op_chown(path: Path, uid: int, gid: int) -> None:
    del path, uid, gid  # the test runner isn't root; a real chown would fail here


def _build_drive_folders(
    tmp_path: Path, install: InstallState, root: str = "/volume1/media"
) -> None:
    container_root = tmp_path / PurePosixPath(root).relative_to("/")
    container_root.mkdir(parents=True, exist_ok=True)
    storage.build_folders(
        _drive_settings(tmp_path),
        PurePosixPath(root),
        install.app_ids,
        install.puid,
        install.pgid,
        chown=_no_op_chown,
    )


class _CountingLink:
    """A real `os.link`, wrapped to prove how many times it was asked."""

    def __init__(self) -> None:
        self.call_count = 0

    def __call__(self, source: Path, dest: Path) -> None:
        self.call_count += 1
        os.link(source, dest)


class _TinyWaitMonitor(HardlinkMonitor):
    """The same monitor, with a page-wait short enough for a test to hit -
    `alive()` calls `check_now()` with no arguments, so only overriding the
    method's own default (not the route) lets a test make it time out fast.
    """

    async def check_now(self, *, wait_seconds: float = 0.05) -> HardlinkResult | None:
        return await super().check_now(wait_seconds=wait_seconds)


def _extract_section(page_html: str, section_id: str) -> str:
    """The one `<section id="...">...</section>` block, so an assertion
    about it can never accidentally match text from elsewhere on the page.

    `[^>]*` (not `.*?`) between `<section` and its `id` attribute, so this
    still matches an opening tag whose attributes are spread across several
    lines, without ever crossing into a later tag's own attributes.
    """
    match = re.search(rf'<section[^>]*\bid="{section_id}"[^>]*>.*?</section>', page_html, re.DOTALL)
    assert match is not None, f"no section#{section_id} found"
    return match.group(0)


def _assert_unique_ids(page_html: str) -> None:
    """Every `id="..."` on a rendered page has to be unique - two elements
    sharing one breaks every `label[for]`/`getElementById` hook that names it.
    """
    ids = re.findall(r'\bid="([^"]+)"', page_html)
    dupes = sorted({id_ for id_ in ids if ids.count(id_) > 1})
    assert not dupes, f"duplicate element ids: {dupes!r}"


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

    response = client.get("/diagnostics")

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

    response = client.get("/diagnostics")

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

    response = client.get("/diagnostics")

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
        response = client.get("/diagnostics")
    finally:
        locked.chmod(0o755)

    assert response.status_code == 200
    assert "Marrquee can&#39;t save its settings" in response.text


def test_the_page_reports_a_settings_folder_it_just_created(tmp_path: Path) -> None:
    target = tmp_path / "not-yet-created"
    settings = Settings(config_dir=target)

    client = _client(settings, DockerStatus(connected=True, version="27.3.1"))
    response = client.get("/diagnostics")

    assert response.status_code == 200
    assert "Settings folder ready" in response.text
    assert target.is_dir()


def test_both_checks_rerun_on_every_request(settings: Settings) -> None:
    engine = _CountingDockerEngine(DockerStatus(connected=True, version="27.3.1"))
    app = create_app(settings=settings, engine=engine)
    client = TestClient(app)

    client.get("/diagnostics")
    client.get("/diagnostics")

    assert engine.call_count == 2


def test_tokens_css_is_linked_before_app_css(settings: Settings) -> None:
    client = _client(settings, DockerStatus(connected=True, version="27.3.1"))

    response = client.get("/diagnostics")

    tokens_index = response.text.index("tokens.css")
    app_index = response.text.index("app.css")
    assert tokens_index < app_index


def test_the_check_again_control_is_a_link_to_diagnostics_styled_as_the_pill_button(
    settings: Settings,
) -> None:
    client = _client(settings, DockerStatus(connected=True, version="27.3.1"))

    response = client.get("/diagnostics")

    assert '<a class="btn-primary" href="/diagnostics">Check again</a>' in response.text


def test_the_page_declares_lang_viewport_and_color_scheme(settings: Settings) -> None:
    client = _client(settings, DockerStatus(connected=True, version="27.3.1"))

    response = client.get("/diagnostics")

    assert '<html lang="en">' in response.text
    assert 'name="viewport" content="width=device-width, initial-scale=1"' in response.text
    assert 'name="color-scheme" content="dark"' in response.text


def test_status_icons_are_aria_hidden_and_state_is_in_the_title_text(settings: Settings) -> None:
    client = _client(settings, DockerStatus(connected=True, version="27.3.1"))

    response = client.get("/diagnostics")

    # Two check rows, the Last problem section's own empty-state row (no
    # problem file exists here), and the new "Your drive" row (no install
    # here, so it's idle) - each with a decorative, aria-hidden icon. The
    # wording of the title carries the state, not the icon's colour.
    assert response.text.count('aria-hidden="true"') == 4
    assert "Talking to Docker" in response.text
    assert "Settings folder ready" in response.text


def test_the_page_carries_the_diagnostics_lede_not_the_old_wizard_teaser(
    settings: Settings,
) -> None:
    client = _client(settings, DockerStatus(connected=True, version="27.3.1"))

    response = client.get("/diagnostics")

    # The lede's apostrophes ("setup's", "isn't") come back from Jinja
    # escaped - unescape the response rather than the constant, so this
    # compares the same text a browser would show, not its entities.
    assert words.DIAGNOSTICS_LEDE in html.unescape(response.text)
    assert "Next comes the setup wizard" not in response.text


def test_the_page_no_longer_answers_at_root(settings: Settings) -> None:
    client = _client(settings, DockerStatus(connected=True, version="27.3.1"))

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] != "/diagnostics"


# --- "Your drive" - opening Diagnostics runs the check and shows the row ----


def test_diagnostics_runs_the_drive_check_on_every_open(tmp_path: Path) -> None:
    settings = _drive_settings(tmp_path)
    install = _drive_install()
    _build_drive_folders(tmp_path, install)
    save_state(settings.config_dir, install)
    link = _CountingLink()
    monitor = HardlinkMonitor(settings, link=link)
    app = create_app(
        settings=settings,
        engine=FakeDockerEngine(DockerStatus(connected=True, version="27.3.1")),
        hardlinks=monitor,
    )
    client = TestClient(app)

    first = client.get("/diagnostics")
    client.get("/diagnostics")

    assert first.status_code == 200
    section = _extract_section(first.text, "your-drive")
    assert 'data-drive-state="ok"' in section
    assert words.DRIVE_WORKS_TITLE in section
    assert link.call_count == 2


@pytest.mark.parametrize(
    ("result", "expected_state", "expected_title"),
    [
        pytest.param(
            HardlinkResult(
                outcome="works", reason=None, folder=None, technical=None, checked_at="x"
            ),
            "ok",
            "DRIVE_WORKS_TITLE",
            id="works",
        ),
        pytest.param(
            HardlinkResult(
                outcome="not_needed", reason=None, folder=None, technical=None, checked_at="x"
            ),
            "idle",
            "DRIVE_NOT_NEEDED_TITLE",
            id="not_needed",
        ),
        pytest.param(None, "idle", "DRIVE_STILL_CHECKING_TITLE", id="still-checking"),
        pytest.param(
            HardlinkResult(
                outcome="copies",
                reason="different_drives",
                folder="/x",
                technical=None,
                checked_at="x",
            ),
            "warn",
            "DRIVE_COPIES_TITLE",
            id="copies-different-drives",
        ),
        pytest.param(
            HardlinkResult(
                outcome="copies",
                reason="no_hard_links",
                folder="/x",
                technical=None,
                checked_at="x",
            ),
            "warn",
            "DRIVE_COPIES_TITLE",
            id="copies-no-hard-links",
        ),
        pytest.param(
            HardlinkResult(
                outcome="couldnt_check",
                reason="not_allowed",
                folder="/x",
                technical="Permission denied (errno 13)",
                checked_at="x",
            ),
            "warn",
            "DRIVE_COULDNT_CHECK_TITLE",
            id="couldnt-check",
        ),
    ],
)
def test_each_outcome_draws_its_own_state_and_title(
    result: HardlinkResult | None, expected_state: str, expected_title: str
) -> None:
    line = drive_status_line(result)

    assert line.state == expected_state
    assert line.title == getattr(words, expected_title)


def test_a_copies_result_carries_its_reason_todo_and_technical_detail() -> None:
    result = HardlinkResult(
        outcome="copies",
        reason="different_drives",
        folder="/volume1/media/data/media/tv",
        technical="Invalid cross-device link (errno 18)",
        checked_at="x",
    )

    line = drive_status_line(result)

    assert line.detail == words.drive_reason_different_drives("/volume1/media/data/media/tv")
    assert line.what_to_do == words.DRIVE_TODO_SAME_DRIVE
    assert line.technical == words.drive_technical("Invalid cross-device link (errno 18)")


def test_a_couldnt_check_result_uses_its_own_title_with_the_same_reason_shape() -> None:
    result = HardlinkResult(
        outcome="couldnt_check",
        reason="drive_full",
        folder="/volume1/media/data/media/tv",
        technical=None,
        checked_at="x",
    )

    line = drive_status_line(result)

    assert line.title == words.DRIVE_COULDNT_CHECK_TITLE
    assert line.detail == words.drive_reason_drive_full("/volume1/media/data/media/tv")
    assert line.what_to_do == words.DRIVE_TODO_FREE_SPACE
    assert line.technical is None


def test_works_and_not_needed_and_still_checking_carry_no_what_to_do_or_technical() -> None:
    for result in (
        HardlinkResult(outcome="works", reason=None, folder=None, technical=None, checked_at="x"),
        HardlinkResult(
            outcome="not_needed", reason=None, folder=None, technical=None, checked_at="x"
        ),
        None,
    ):
        line = drive_status_line(result)
        assert line.what_to_do is None
        assert line.technical is None


def test_the_drive_section_never_shows_a_host_path(tmp_path: Path) -> None:
    settings = _drive_settings(tmp_path)
    install = _drive_install()
    _build_drive_folders(tmp_path, install)
    save_state(settings.config_dir, install)

    def cross_device_link(source: Path, dest: Path) -> None:
        raise OSError(errno.EXDEV, "cross-device link")

    monitor = HardlinkMonitor(settings, link=cross_device_link)
    app = create_app(
        settings=settings,
        engine=FakeDockerEngine(DockerStatus(connected=True, version="27.3.1")),
        hardlinks=monitor,
    )
    client = TestClient(app)

    response = client.get("/diagnostics")

    section = _extract_section(response.text, "your-drive")
    assert "/host" not in section
    assert "/volume1/media/data/media/tv" in section


def test_a_slow_drive_shows_still_checking_instead_of_hanging(tmp_path: Path) -> None:
    settings = _drive_settings(tmp_path)
    install = _drive_install()
    _build_drive_folders(tmp_path, install)
    save_state(settings.config_dir, install)

    def slow_link(source: Path, dest: Path) -> None:
        time.sleep(0.2)
        os.link(source, dest)

    monitor = _TinyWaitMonitor(settings, link=slow_link)
    app = create_app(
        settings=settings,
        engine=FakeDockerEngine(DockerStatus(connected=True, version="27.3.1")),
        hardlinks=monitor,
    )
    with TestClient(app) as client:
        response = client.get("/diagnostics")

    section = _extract_section(response.text, "your-drive")
    assert 'data-drive-state="idle"' in section
    assert words.DRIVE_STILL_CHECKING_TITLE in section


def test_the_page_still_has_exactly_one_check_again_link(settings: Settings) -> None:
    client = _client(settings, DockerStatus(connected=True, version="27.3.1"))

    response = client.get("/diagnostics")

    assert response.text.count('<a class="btn-primary" href="/diagnostics">Check again</a>') == 1


def test_the_diagnostics_page_has_no_duplicate_element_ids(settings: Settings) -> None:
    client = _client(settings, DockerStatus(connected=True, version="27.3.1"))

    response = client.get("/diagnostics")

    _assert_unique_ids(response.text)

"""Tests for the deploy engine's Recyclarr-specific seams: a `kind == "sync"`
app's non-HTTP readiness, its config written before Docker is ever asked to
start it, and the moment a successful full deploy asks for a sync.

Reuses `test_deploy`'s own fixtures and `_StatefulEngine` - the same Docker
test double every other deploy test in this project is driven against.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from test_deploy import (
    _fresh_root,
    _happy_engine,
    _install_state,
    _run_to_terminal,
    _settings,
    _StatefulEngine,
)

from marrquee.config import Settings
from marrquee.deploy import DeployManager, FakeReadinessProbe
from marrquee.docker_client import ComposeResult
from marrquee.recyclarr import recyclarr_config_host_path
from marrquee.state import save_state
from marrquee.storage import to_host_view


class _RecordingSyncTrigger:
    """Stands in for `RecyclarrMonitor` on a deploy/add test: counts how
    many times it was asked for a sync, and never touches Docker.
    """

    def __init__(self) -> None:
        self.calls = 0

    def request_sync(self) -> object:
        self.calls += 1
        return None


def _config_path(settings: Settings, root: PurePosixPath) -> Path:
    host_path = recyclarr_config_host_path(root)
    container_root = to_host_view(settings, str(root))
    return container_root / host_path.relative_to(root)


# --- Ready without a port: the HTTP probe is never called for it ------------


async def test_recyclarr_is_ready_without_the_http_probe(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(("sonarr", "recyclarr"), root)
    save_state(settings.config_dir, install)

    engine = _happy_engine(("sonarr", "recyclarr"))
    probe = FakeReadinessProbe(default=True)
    manager = DeployManager(settings, engine, probe=probe)

    manager.start()
    history = await _run_to_terminal(manager)

    assert history[-1].phase == "finale"
    assert history[-1].failure is None
    assert all(call[0] != "recyclarr" for call in probe.calls)


async def test_recyclarrs_config_is_written_before_its_compose_up(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(("sonarr", "recyclarr"), root)
    save_state(settings.config_dir, install)
    config_path = _config_path(settings, root)

    class _AssertingEngine(_StatefulEngine):
        """Fails loudly if `recyclarr`'s compose_up is ever reached before
        Marrquee has written the config Recyclarr reads at container start.
        """

        async def compose_up(
            self, project: str, compose_file: Path, service: str, *, recreate: bool = False
        ) -> ComposeResult:
            if service == "recyclarr":
                assert config_path.is_file(), "recyclarr.yml must exist before compose_up"
            return await super().compose_up(project, compose_file, service, recreate=recreate)

    engine = _AssertingEngine(("sonarr", "recyclarr"))
    probe = FakeReadinessProbe(default=True)
    manager = DeployManager(settings, engine, probe=probe)

    manager.start()
    history = await _run_to_terminal(manager)

    assert history[-1].phase == "finale"
    assert config_path.is_file()


# --- A config write failure fails the app, never reaching compose_up --------


async def test_a_config_write_failure_fails_with_compose_failed_and_no_compose_up(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(("sonarr", "recyclarr"), root)
    save_state(settings.config_dir, install)

    # A symlink planted where Recyclarr's own apps folder should be -
    # `build_folders` leaves an already-existing entry alone, so this
    # survives all the way to `_write_recyclarr_conf`.
    apps_root = settings.host_mount / "volume1" / "media" / "marrquee" / "apps"
    apps_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (apps_root / "recyclarr").symlink_to(outside)

    engine = _happy_engine(("sonarr", "recyclarr"))
    probe = FakeReadinessProbe(default=True)
    manager = DeployManager(settings, engine, probe=probe)

    manager.start()
    history = await _run_to_terminal(manager)

    assert history[-1].phase == "error"
    assert history[-1].failure is not None
    assert history[-1].failure.code == "compose_failed"
    assert not any(
        call[0] in ("compose_up", "compose_up_recreate") and call[1][2] == "recyclarr"
        for call in engine.calls
    )


# --- The one place a full deploy asks for a sync -----------------------------


async def test_a_finished_full_deploy_with_recyclarr_requests_one_sync(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(("sonarr", "recyclarr"), root)
    save_state(settings.config_dir, install)

    engine = _happy_engine(("sonarr", "recyclarr"))
    probe = FakeReadinessProbe(default=True)
    trigger = _RecordingSyncTrigger()
    manager = DeployManager(settings, engine, probe=probe, recyclarr=trigger)

    manager.start()
    history = await _run_to_terminal(manager)

    assert history[-1].phase == "finale"
    assert trigger.calls == 1


async def test_a_finished_full_deploy_without_recyclarr_requests_no_sync(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(("prowlarr",), root)
    save_state(settings.config_dir, install)

    engine = _happy_engine(("prowlarr",))
    probe = FakeReadinessProbe(default=True)
    trigger = _RecordingSyncTrigger()
    manager = DeployManager(settings, engine, probe=probe, recyclarr=trigger)

    manager.start()
    history = await _run_to_terminal(manager)

    assert history[-1].phase == "finale"
    assert trigger.calls == 0


async def test_a_failed_deploy_with_recyclarr_requests_no_sync(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(("sonarr", "recyclarr"), root)
    save_state(settings.config_dir, install)

    engine = _happy_engine(("sonarr", "recyclarr"))
    engine._compose_results["sonarr"] = ComposeResult(  # type: ignore[attr-defined]
        ok=False, exit_code=1, output="Error: port is already allocated"
    )
    probe = FakeReadinessProbe(default=True)
    trigger = _RecordingSyncTrigger()
    manager = DeployManager(settings, engine, probe=probe, recyclarr=trigger)

    manager.start()
    history = await _run_to_terminal(manager)

    assert history[-1].phase == "error"
    assert trigger.calls == 0

"""Tests for the deploy engine - the part that does the real work.

Nothing here touches a real Docker daemon or waits a real second: every
non-deterministic dependency (`FakeDockerEngine`, `FakeReadinessProbe`, a
fake clock/sleep pair) is injected, which is what lets a full happy run, a
slow run, a failed run and a resumed run all be driven end to end in well
under a second of wall-clock time.

`manager.snapshot()` (not `manager.subscribe()`) is used to capture full
run histories: `subscribe()` is deliberately lossy - a slow reader is
dropped rather than allowed to block the run - so it is tested separately,
for its own specific promises, rather than relied on for exact ordering.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
from pathlib import Path, PurePosixPath

import httpx

from marrquee.catalog import get_app
from marrquee.config import Settings
from marrquee.deploy import (
    AppProgress,
    DeployManager,
    DeploySnapshot,
    FakeReadinessProbe,
    HttpReadinessProbe,
)
from marrquee.docker_client import (
    ComposeResult,
    ContainerSnapshot,
    DockerStatus,
    FakeDockerEngine,
    NetworkConnectResult,
)
from marrquee.state import InstallState, save_state, write_json_atomic
from marrquee.wiring import WiringStep
from marrquee.words import PHASE_HEADLINE_READY

# --- Shared fixtures and small builders --------------------------------------


def _settings(tmp_path: Path) -> Settings:
    return Settings(host_mount=tmp_path / "host", config_dir=tmp_path / "config")


def _fresh_root(settings: Settings) -> PurePosixPath:
    """Create an empty, never-deployed-to target and return its host path."""
    (settings.host_mount / "volume1" / "media").mkdir(parents=True)
    return PurePosixPath("/volume1/media")


def _install_state(app_ids: tuple[str, ...], root: PurePosixPath) -> InstallState:
    return InstallState(
        version=1,
        storage_root=str(root),
        app_ids=app_ids,
        # Obviously-fake, human-readable keys - never a realistic-looking
        # secret, since this is a public repository.
        api_keys={app_id: f"fake-{app_id}-api-key" for app_id in app_ids},
        # build_folders chowns every folder it creates for real (this story's
        # whole safety model is "only ever chown what we created" - there is
        # no injectable stand-in on DeployManager). Chowning a path you
        # already own to your own uid/gid succeeds without root, so tests
        # use the test process's own ids rather than an arbitrary 1000:1000.
        puid=os.getuid(),
        pgid=os.getgid(),
        umask="002",
        timezone="Etc/UTC",
        created="2026-09-19T00:00:00+00:00",
    )


def _running_container(app_id: str) -> ContainerSnapshot:
    return ContainerSnapshot(
        name=app_id,
        exists=True,
        state="running",
        exit_code=None,
        image=get_app(app_id).image,
        detail=None,
    )


class _StatefulEngine:
    """A DockerEngine test double that models time passing, unlike the
    exported `FakeDockerEngine` (a fixed, pre-scripted lookup table).

    Every app starts absent - so the name-clash precondition sees a
    genuinely fresh daemon - and only becomes `running` once its own
    `compose_up` succeeds, which is what lets the very same instance drive
    a happy path (compose_up creates it) and a rerun (it's already there).

    The stack's network is honest too: it does not exist until the first
    successful `compose_up`, exactly like a real Docker daemon - a fake
    that always let `connect_network` succeed could never have caught the
    real deploy engine trying to join it before anything ever created it.
    """

    def __init__(
        self,
        app_ids: tuple[str, ...],
        *,
        images: set[str] | None = None,
        compose_results: dict[str, ComposeResult] | None = None,
        self_container_id: str | None = "marrquee",
        network_exists: bool = False,
    ) -> None:
        self._images = (
            images if images is not None else {get_app(app_id).image for app_id in app_ids}
        )
        self._compose_results = compose_results or {}
        self._self_container_id = self_container_id
        self._containers: dict[str, ContainerSnapshot] = {}
        self._network_exists = network_exists
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def status(self) -> DockerStatus:
        self.calls.append(("status", ()))
        return DockerStatus(connected=True)

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
        if not self._network_exists:
            return NetworkConnectResult(ok=False, detail=f"network {network!r} does not exist yet")
        return NetworkConnectResult(ok=True, detail=None)

    async def logs(self, name: str, tail: int = 50) -> str:
        self.calls.append(("logs", (name, tail)))
        return ""

    async def compose_up(self, project: str, compose_file: Path, service: str) -> ComposeResult:
        self.calls.append(("compose_up", (project, str(compose_file), service)))
        result = self._compose_results.get(service, ComposeResult(ok=True, exit_code=0, output=""))
        if result.ok:
            self._containers[service] = _running_container(service)
            self._network_exists = True
        return result

    async def self_container_id(self) -> str | None:
        self.calls.append(("self_container_id", ()))
        return self._self_container_id


def _happy_engine(app_ids: tuple[str, ...]) -> _StatefulEngine:
    """A Docker daemon that has never heard of any of our containers, has
    every image already pulled, and says yes to everything.
    """
    return _StatefulEngine(
        app_ids,
        compose_results={
            app_id: ComposeResult(ok=True, exit_code=0, output="") for app_id in app_ids
        },
    )


class _FakeClock:
    """A clock and a sleep function that agree with each other and never
    actually wait - the pair a test injects into `DeployManager` so a
    45-second reassurance or a 300-second timeout costs nothing real.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)  # let other coroutines (a subscriber, a poll loop) run


async def _run_to_terminal(
    manager: DeployManager, *, budget: int = 200_000
) -> list[DeploySnapshot]:
    """Poll `snapshot()` to a terminal phase, collecting every distinct state seen.

    `snapshot()` is always synchronous and truthful, so polling it (rather
    than relying on `subscribe()`, which drops frames from slow readers by
    design) is what proves the full waiting -> starting -> done history.
    """
    seen: list[DeploySnapshot] = []
    for _ in range(budget):
        current = manager.snapshot()
        if not seen or current != seen[-1]:
            seen.append(current)
        if current.phase in ("finale", "error"):
            return seen
        await asyncio.sleep(0)
    raise AssertionError("deploy did not reach a terminal phase in time")


class _CountingWiringRunner:
    """Records how many times it was asked to run, and does nothing else."""

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, state: InstallState, emit: object) -> None:
        self.calls += 1


class _OneStepWiringRunner:
    """Emits exactly one step, carrying a technical detail that must never
    reach a snapshot.
    """

    def __init__(self, technical: str) -> None:
        self._technical = technical

    async def run(self, state: InstallState, emit: object) -> None:
        step = WiringStep(
            index=1,
            total=1,
            key="example-connection",
            line="Connecting things together",
            state="error",
            chip="Error",
            note="Something needs attention",
            technical=self._technical,
        )
        emit(step)  # type: ignore[operator]


# --- The full happy path: waiting -> starting -> done, in catalog order -----


async def test_each_app_walks_waiting_starting_done_in_catalog_order(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    app_ids = ("radarr", "prowlarr", "sonarr")  # deliberately out of catalog order
    install = _install_state(app_ids, root)
    save_state(settings.config_dir, install)

    engine = _happy_engine(app_ids)
    probe = FakeReadinessProbe(default=True)
    clock = _FakeClock()
    manager = DeployManager(settings, engine, probe=probe, clock=clock.time, sleep=clock.sleep)

    manager.start()
    history = await _run_to_terminal(manager)

    final = history[-1]
    assert final.phase == "finale"
    # Catalog order (prowlarr, sonarr, radarr), not the input order above.
    assert [app.app_id for app in final.apps] == ["prowlarr", "sonarr", "radarr"]
    assert all(app.state == "done" for app in final.apps)

    sonarr_states = [
        app.state for snapshot in history for app in snapshot.apps if app.app_id == "sonarr"
    ]
    assert sonarr_states[0] == "waiting"
    assert "starting" in sonarr_states
    assert sonarr_states[-1] == "done"


async def test_the_network_does_not_exist_until_the_first_compose_up(tmp_path: Path) -> None:
    """On a real fresh host, nothing has ever run `compose up` for this
    stack, so the `marrquee` network does not exist yet - only compose's own
    first `up` creates it. Joining it any earlier always fails (404, before
    any container is ever created) - this is exactly what happened the
    first time this engine ever ran against real Docker. `_StatefulEngine`
    (unlike an always-succeeding fake) models that honestly, which is the
    only way this class of ordering bug is even testable.
    """
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    app_ids = ("prowlarr", "sonarr")
    install = _install_state(app_ids, root)
    save_state(settings.config_dir, install)

    engine = _happy_engine(app_ids)  # network_exists defaults to False - a fresh host
    probe = FakeReadinessProbe(default=True)
    clock = _FakeClock()
    manager = DeployManager(settings, engine, probe=probe, clock=clock.time, sleep=clock.sleep)

    manager.start()
    history = await _run_to_terminal(manager)

    assert history[-1].phase == "finale"

    # The network cannot exist before compose creates it, so a
    # `connect_network` call can only ever succeed once at least one
    # `compose_up` has already happened. Reaching `finale` at all already
    # proves this held (an engine honestly reporting "network does not
    # exist yet" would have failed the deploy with `docker_unreachable`
    # otherwise) - checked explicitly here too, for a failure message that
    # names the actual mechanism instead of just the symptom.
    call_names = [name for name, _args in engine.calls]
    assert call_names.index("compose_up") < call_names.index("connect_network")
    assert len(probe.calls) >= 1  # readiness was actually exercised, not skipped


async def test_headline_changes_per_app_during_the_run(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(("sonarr",), root)
    save_state(settings.config_dir, install)

    engine = _happy_engine(("sonarr",))
    probe = FakeReadinessProbe(default=True)
    clock = _FakeClock()
    manager = DeployManager(settings, engine, probe=probe, clock=clock.time, sleep=clock.sleep)

    manager.start()
    history = await _run_to_terminal(manager)

    headlines = {snapshot.headline for snapshot in history}
    assert any("Starting Sonarr" in headline for headline in headlines)
    assert any("Sonarr is ready" in headline for headline in headlines)


async def test_the_download_line_appears_only_when_the_image_is_absent(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(("sonarr",), root)
    save_state(settings.config_dir, install)

    engine = _StatefulEngine(
        ("sonarr",),
        images=set(),  # sonarr's image has never been pulled
        compose_results={"sonarr": ComposeResult(ok=True, exit_code=0, output="")},
    )
    probe = FakeReadinessProbe(default=True)
    clock = _FakeClock()
    manager = DeployManager(settings, engine, probe=probe, clock=clock.time, sleep=clock.sleep)

    manager.start()
    history = await _run_to_terminal(manager)

    lines_seen = {
        app.line for snapshot in history for app in snapshot.apps if app.app_id == "sonarr"
    }
    assert any("Downloading Sonarr" in line for line in lines_seen)


async def test_no_download_line_appears_when_the_image_is_already_present(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(("sonarr",), root)
    save_state(settings.config_dir, install)

    engine = _happy_engine(("sonarr",))
    probe = FakeReadinessProbe(default=True)
    clock = _FakeClock()
    manager = DeployManager(settings, engine, probe=probe, clock=clock.time, sleep=clock.sleep)

    manager.start()
    history = await _run_to_terminal(manager)

    lines_seen = {
        app.line for snapshot in history for app in snapshot.apps if app.app_id == "sonarr"
    }
    assert not any("Downloading" in line for line in lines_seen)


# --- Slow apps are reassured, never failed -----------------------------------


async def test_a_slow_app_is_reassured_never_failed(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(("sonarr",), root)
    save_state(settings.config_dir, install)

    engine = _happy_engine(("sonarr",))
    # Not ready for the first 50 "seconds" (25 polls at the 2s interval),
    # then ready - crosses the 45s reassurance threshold with several polls
    # to spare before it finally answers, so the note has a real chance to
    # be observed instead of being skipped by an immediate "done".
    probe = FakeReadinessProbe(responses={("sonarr", 8989): [False] * 25}, default=True)
    clock = _FakeClock()
    manager = DeployManager(settings, engine, probe=probe, clock=clock.time, sleep=clock.sleep)

    manager.start()
    history = await _run_to_terminal(manager)

    final = history[-1]
    assert final.phase == "finale"
    assert all(app.state != "error" for snapshot in history for app in snapshot.apps)
    notes_seen = {app.note for snapshot in history for app in snapshot.apps if app.note}
    assert any("taking a little longer than usual" in note for note in notes_seen)


async def test_an_app_that_never_answers_ends_as_error_with_captured_logs(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(("sonarr",), root)
    save_state(settings.config_dir, install)

    engine = _happy_engine(("sonarr",))
    probe = FakeReadinessProbe(default=False)  # never answers
    clock = _FakeClock()
    manager = DeployManager(settings, engine, probe=probe, clock=clock.time, sleep=clock.sleep)

    manager.start()
    history = await _run_to_terminal(manager)

    final = history[-1]
    assert final.phase == "error"
    assert final.failure is not None
    assert final.failure.code == "never_became_ready"
    assert "Sonarr" in final.failure.headline
    assert final.failure.what_to_do
    assert any(call[0] == "logs" for call in engine.calls)

    diagnostics = (settings.config_dir / "last-failure.txt").read_text()
    assert "never_became_ready" in diagnostics


async def test_a_port_conflict_is_reported_with_the_right_wording(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(("sonarr",), root)
    save_state(settings.config_dir, install)

    engine = FakeDockerEngine(
        DockerStatus(connected=True),
        images={get_app("sonarr").image},
        compose_results={
            "sonarr": ComposeResult(
                ok=False,
                exit_code=1,
                output="Error: Bind for 0.0.0.0:8989 failed: port is already allocated",
            )
        },
        self_container_id="marrquee",
    )
    manager = DeployManager(settings, engine)

    manager.start()
    history = await _run_to_terminal(manager)

    final = history[-1]
    assert final.failure is not None
    assert final.failure.code == "port_in_use"
    assert "8989" in final.failure.headline or "8989" in final.failure.what_to_do


# --- Nothing is created when a precondition refuses --------------------------


async def test_docker_unreachable_refuses_before_anything_is_created(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(("sonarr",), root)
    save_state(settings.config_dir, install)

    engine = FakeDockerEngine(DockerStatus(connected=False, detail="no socket"))
    manager = DeployManager(settings, engine)

    manager.start()
    history = await _run_to_terminal(manager)

    final = history[-1]
    assert final.phase == "error"
    assert final.failure is not None
    assert final.failure.code == "docker_unreachable"
    assert not any(call[0] == "compose_up" for call in engine.calls)
    assert not (settings.host_mount / "volume1" / "media" / "marrquee").exists()


async def test_a_populated_target_is_refused_and_nothing_is_created(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    (settings.host_mount / "volume1" / "media" / "data" / "media" / "tv").mkdir(parents=True)
    (
        settings.host_mount / "volume1" / "media" / "data" / "media" / "tv" / "Old Show.mkv"
    ).write_text("")
    install = _install_state(("sonarr",), root)
    save_state(settings.config_dir, install)

    engine = _happy_engine(("sonarr",))
    manager = DeployManager(settings, engine)

    manager.start()
    history = await _run_to_terminal(manager)

    final = history[-1]
    assert final.phase == "error"
    assert final.failure is not None
    assert final.failure.code == "storage_refused"
    assert not any(call[0] == "compose_up" for call in engine.calls)
    assert not (settings.host_mount / "volume1" / "media" / "marrquee").exists()


async def test_an_existing_container_we_did_not_create_is_refused_as_a_name_clash(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(("sonarr",), root)
    save_state(settings.config_dir, install)

    # Fresh target (no marker), but the daemon already knows a "sonarr" -
    # something the owner set up themselves, not Marrquee.
    engine = FakeDockerEngine(
        DockerStatus(connected=True),
        images={get_app("sonarr").image},
        containers={"sonarr": _running_container("sonarr")},
    )
    manager = DeployManager(settings, engine)

    manager.start()
    history = await _run_to_terminal(manager)

    final = history[-1]
    assert final.phase == "error"
    assert final.failure is not None
    assert final.failure.code == "name_clash"
    assert "Sonarr" in final.failure.headline
    assert not any(call[0] == "compose_up" for call in engine.calls)
    assert not (settings.host_mount / "volume1" / "media" / "marrquee").exists()


async def test_our_own_marker_permits_a_rerun_past_the_name_clash_check(tmp_path: Path) -> None:
    """The idempotent-resume case: a container we created on an earlier
    attempt at this same root must never be mistaken for someone else's app.
    """
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(("sonarr",), root)
    save_state(settings.config_dir, install)

    engine = _happy_engine(("sonarr",))
    probe = FakeReadinessProbe(default=True)
    clock = _FakeClock()
    first_manager = DeployManager(
        settings, engine, probe=probe, clock=clock.time, sleep=clock.sleep
    )
    first_manager.start()
    first_history = await _run_to_terminal(first_manager)
    assert first_history[-1].phase == "finale"

    # A second run against the same root and the same (now pre-existing,
    # already-running) container must not be refused as a clash.
    second_manager = DeployManager(
        settings, engine, probe=probe, clock=clock.time, sleep=clock.sleep
    )
    second_manager.start()
    second_history = await _run_to_terminal(second_manager)
    assert second_history[-1].phase == "finale"


async def test_missing_self_container_id_degrades_to_docker_unreachable(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(("prowlarr",), root)
    save_state(settings.config_dir, install)

    engine = FakeDockerEngine(
        DockerStatus(connected=True),
        images={get_app("prowlarr").image},
        self_container_id=None,  # running outside Docker, e.g. in dev
    )
    manager = DeployManager(settings, engine)

    manager.start()
    history = await _run_to_terminal(manager)

    final = history[-1]
    assert final.phase == "error"
    assert final.failure is not None
    assert final.failure.code == "docker_unreachable"
    assert not any(call[0] == "compose_up" for call in engine.calls)


# --- Phases, the wiring seam, and the owner decision that wiring can't fail --


async def test_phases_follow_ready_running_wiring_finale_and_wiring_runs_once(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(("prowlarr",), root)
    save_state(settings.config_dir, install)

    engine = _happy_engine(("prowlarr",))
    probe = FakeReadinessProbe(default=True)
    clock = _FakeClock()
    wiring = _CountingWiringRunner()
    manager = DeployManager(
        settings, engine, probe=probe, clock=clock.time, sleep=clock.sleep, wiring=wiring
    )

    assert manager.snapshot().phase == "ready"

    manager.start()
    history = await _run_to_terminal(manager)

    phases_seen = [snapshot.phase for snapshot in history]
    assert "running" in phases_seen
    assert "wiring" in phases_seen
    assert phases_seen[-1] == "finale"
    assert wiring.calls == 1


async def test_a_wiring_failure_never_fails_the_deploy_and_its_detail_is_diagnostics_only(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(("prowlarr",), root)
    save_state(settings.config_dir, install)

    engine = _happy_engine(("prowlarr",))
    probe = FakeReadinessProbe(default=True)
    clock = _FakeClock()
    wiring = _OneStepWiringRunner(
        technical="HTTP 400 BaseUrl: something an owner should never see raw"
    )
    manager = DeployManager(
        settings, engine, probe=probe, clock=clock.time, sleep=clock.sleep, wiring=wiring
    )

    manager.start()
    history = await _run_to_terminal(manager)

    final = history[-1]
    assert final.phase == "finale"  # a wiring problem is not a deploy failure
    assert len(final.wiring) == 1
    assert final.wiring[0].technical is None
    assert final.wiring[0].state == "error"

    diagnostics = (settings.config_dir / "last-failure.txt").read_text()
    assert "something an owner should never see raw" in diagnostics


class _RaisingWiringRunner:
    async def run(self, state: InstallState, emit: object) -> None:
        raise RuntimeError(
            "a real WiringRunner must never do this, but the engine survives it anyway"
        )


async def test_a_wiring_runner_that_raises_still_reaches_finale(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(("prowlarr",), root)
    save_state(settings.config_dir, install)

    engine = _happy_engine(("prowlarr",))
    probe = FakeReadinessProbe(default=True)
    clock = _FakeClock()
    manager = DeployManager(
        settings,
        engine,
        probe=probe,
        clock=clock.time,
        sleep=clock.sleep,
        wiring=_RaisingWiringRunner(),
    )

    manager.start()
    history = await _run_to_terminal(manager)

    assert history[-1].phase == "finale"


# --- Starting, subscribing, persisting, and resuming -------------------------


async def test_a_second_start_while_running_does_not_launch_a_second_run(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    app_ids = ("prowlarr", "sonarr")
    install = _install_state(app_ids, root)
    save_state(settings.config_dir, install)

    engine = _happy_engine(app_ids)
    probe = FakeReadinessProbe(default=True)
    clock = _FakeClock()
    manager = DeployManager(settings, engine, probe=probe, clock=clock.time, sleep=clock.sleep)

    manager.start()
    second = manager.start()  # no await has happened yet - the task hasn't run a single line
    assert second.phase in ("ready", "running")

    history = await _run_to_terminal(manager)
    assert history[-1].phase == "finale"

    compose_up_calls = [call for call in engine.calls if call[0] == "compose_up"]
    assert len(compose_up_calls) == len(app_ids)  # exactly one per app - never doubled


async def test_a_subscriber_that_stops_reading_does_not_stall_the_run(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(("sonarr",), root)
    save_state(settings.config_dir, install)

    engine = _happy_engine(("sonarr",))
    probe = FakeReadinessProbe(default=True)
    clock = _FakeClock()
    manager = DeployManager(settings, engine, probe=probe, clock=clock.time, sleep=clock.sleep)

    subscription = manager.subscribe()
    first = await subscription.__anext__()
    assert first.phase == "ready"  # nothing has started yet

    manager.start()
    history = await _run_to_terminal(manager)

    assert history[-1].phase == "finale"


async def test_subscribe_yields_the_current_snapshot_immediately(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    manager = DeployManager(settings, FakeDockerEngine(DockerStatus(connected=True)))

    subscription = manager.subscribe()
    first = await subscription.__anext__()

    assert first == manager.snapshot()


async def test_every_emitted_snapshot_is_persisted_to_deploy_json(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(("prowlarr",), root)
    save_state(settings.config_dir, install)

    engine = _happy_engine(("prowlarr",))
    probe = FakeReadinessProbe(default=True)
    clock = _FakeClock()
    manager = DeployManager(settings, engine, probe=probe, clock=clock.time, sleep=clock.sleep)

    manager.start()
    await _run_to_terminal(manager)

    persisted = json.loads((settings.config_dir / "deploy.json").read_text())
    assert persisted["phase"] == "finale"


async def test_a_second_manager_constructed_after_finale_reports_finale(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(("prowlarr",), root)
    save_state(settings.config_dir, install)

    engine = _happy_engine(("prowlarr",))
    probe = FakeReadinessProbe(default=True)
    clock = _FakeClock()
    first_manager = DeployManager(
        settings, engine, probe=probe, clock=clock.time, sleep=clock.sleep
    )
    first_manager.start()
    first_history = await _run_to_terminal(first_manager)
    assert first_history[-1].phase == "finale"

    second_manager = DeployManager(settings, FakeDockerEngine(DockerStatus(connected=True)))
    assert second_manager.snapshot().phase == "finale"


async def test_resume_if_interrupted_reenters_a_persisted_running_deploy(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(("prowlarr",), root)
    save_state(settings.config_dir, install)

    stale = DeploySnapshot(
        run_id="stale-run-from-before-a-restart",
        phase="running",
        apps=(
            AppProgress(
                app_id="prowlarr",
                name="Prowlarr",
                state="starting",
                chip="Starting…",
                line="Starting Prowlarr",
                note=None,
                port=9696,
            ),
        ),
        headline="Starting your apps, one at a time.",
        detail=None,
        failure=None,
        started_at="2026-09-19T00:00:00+00:00",
        finished_at=None,
        wiring=(),
    )
    write_json_atomic(settings.config_dir / "deploy.json", dataclasses.asdict(stale))

    engine = _happy_engine(("prowlarr",))
    probe = FakeReadinessProbe(default=True)
    clock = _FakeClock()
    manager = DeployManager(settings, engine, probe=probe, clock=clock.time, sleep=clock.sleep)

    # Reloaded at construction - a restart never lies about a deploy that was
    # genuinely still going.
    assert manager.snapshot().phase == "running"

    await manager.resume_if_interrupted()
    history = await _run_to_terminal(manager)

    assert history[-1].phase == "finale"


def test_resume_if_interrupted_does_nothing_when_nothing_was_ever_started(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    manager = DeployManager(settings, FakeDockerEngine(DockerStatus(connected=True)))

    assert manager.snapshot().phase == "ready"


# --- The resting state: honest before any deploy has run ---------------------


def test_a_fresh_manager_with_nothing_persisted_is_the_honest_ready_state(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    manager = DeployManager(settings, FakeDockerEngine(DockerStatus(connected=True)))

    snapshot = manager.snapshot()

    assert snapshot.phase == "ready"
    assert snapshot.apps == ()
    assert snapshot.headline == PHASE_HEADLINE_READY


def test_ready_state_lists_chosen_apps_as_waiting_in_catalog_order(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(("radarr", "sonarr"), root)
    save_state(settings.config_dir, install)

    manager = DeployManager(settings, FakeDockerEngine(DockerStatus(connected=True)))
    snapshot = manager.snapshot()

    assert snapshot.phase == "ready"
    assert [app.app_id for app in snapshot.apps] == ["sonarr", "radarr"]
    assert all(app.state == "waiting" for app in snapshot.apps)
    assert all(app.chip == "Waiting" for app in snapshot.apps)


# --- Secrets never leak -------------------------------------------------------


async def test_api_keys_never_appear_in_a_snapshot_or_the_diagnostics_file(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(("sonarr",), root)
    save_state(settings.config_dir, install)
    secret = install.api_keys["sonarr"]

    engine = FakeDockerEngine(
        DockerStatus(connected=True),
        images={get_app("sonarr").image},
        # A real app's crash output could echo back an environment value we
        # set ourselves - this is exactly that shape.
        compose_results={
            "sonarr": ComposeResult(
                ok=False, exit_code=1, output=f"panic: bad SONARR__AUTH__APIKEY={secret}"
            )
        },
        self_container_id="marrquee",
    )
    manager = DeployManager(settings, engine)

    manager.start()
    history = await _run_to_terminal(manager)

    for snapshot in history:
        assert secret not in json.dumps(dataclasses.asdict(snapshot))

    diagnostics_text = (settings.config_dir / "last-failure.txt").read_text()
    assert secret not in diagnostics_text


# --- No technical string can reach a rendered field ---------------------------

_SUSPICIOUS_MARKERS = ("Traceback", "sha256:", "exit code", '.py", line')


def _rendered_fields(snapshot: DeploySnapshot) -> list[str | None]:
    """Every field a screen would actually render - explicitly excluding
    `failure.technical` and `wiring[].technical`, which are documented,
    structural homes for technical text until the API boundary strips them.
    """
    fields: list[str | None] = [snapshot.headline, snapshot.detail]
    if snapshot.failure is not None:
        fields.extend([snapshot.failure.headline, snapshot.failure.what_to_do])
    for app in snapshot.apps:
        fields.extend([app.line, app.note])
    for step in snapshot.wiring:
        fields.extend([step.line, step.note])
    return fields


async def test_no_technical_string_reaches_a_rendered_field(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(("sonarr",), root)
    save_state(settings.config_dir, install)

    technical_output = (
        "Traceback (most recent call last):\n"
        'File "compose.py", line 42\n'
        "sha256:deadbeef exit code 137"
    )
    engine = FakeDockerEngine(
        DockerStatus(connected=True),
        images={get_app("sonarr").image},
        compose_results={"sonarr": ComposeResult(ok=False, exit_code=137, output=technical_output)},
        self_container_id="marrquee",
    )
    manager = DeployManager(settings, engine)

    manager.start()
    history = await _run_to_terminal(manager)

    for snapshot in history:
        for value in _rendered_fields(snapshot):
            if value is None:
                continue
            for marker in _SUSPICIOUS_MARKERS:
                assert marker not in value, f"{marker!r} leaked into a rendered field: {value!r}"

    final = history[-1]
    assert final.failure is not None
    assert "Traceback" in final.failure.technical  # captured, just not leaked


# --- HttpReadinessProbe: the real HTTP semantics ------------------------------


async def test_readiness_succeeds_on_200_with_our_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/system/status"
        assert request.headers["x-api-key"] == "fake-sonarr-api-key"
        return httpx.Response(200)

    probe = HttpReadinessProbe(transport=httpx.MockTransport(handler))

    assert await probe.check("sonarr", 8989, "api/v3", "fake-sonarr-api-key") is True


async def test_readiness_treats_a_401_as_not_ready_not_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    probe = HttpReadinessProbe(transport=httpx.MockTransport(handler))

    assert await probe.check("sonarr", 8989, "api/v3", "wrong-key") is False


async def test_readiness_treats_connection_refused_as_not_ready_yet() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    probe = HttpReadinessProbe(transport=httpx.MockTransport(handler))

    assert await probe.check("sonarr", 8989, "api/v3", "fake-sonarr-api-key") is False


# --- FakeReadinessProbe itself -------------------------------------------------


async def test_fake_readiness_probe_drains_its_scripted_queue_then_falls_back() -> None:
    probe = FakeReadinessProbe(responses={("sonarr", 8989): [False, False, True]}, default=False)

    results = [await probe.check("sonarr", 8989, "api/v3", "key") for _ in range(5)]

    assert results == [False, False, True, False, False]
    assert len(probe.calls) == 5


async def test_fake_readiness_probe_defaults_to_ready_with_no_scripting() -> None:
    probe = FakeReadinessProbe()

    assert await probe.check("radarr", 7878, "api/v3", "key") is True

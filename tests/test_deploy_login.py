"""Tests for `DeployManager`'s own side of the saved login: `_put_login`
(a fresh deploy) and `apply_login`/`_run_login` (choose, change and retry -
the two-phase run the Pitch stands on).

Every test here builds on `test_deploy`'s own fixtures (`_settings`,
`_fresh_root`, `_install_state`, `_FakeClock`, `_run_to_terminal`,
`_StatefulEngine`) - the same "never touch a real Docker daemon, never wait
a real second" shape the rest of the deploy engine's tests already use.
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path

from test_deploy import (
    _GLUETUN_ANSWERS,
    _FakeClock,
    _fresh_root,
    _install_state,
    _run_to_terminal,
    _settings,
    _StatefulEngine,
)
from test_deploy_add import _deployed_to_finale, _finish_add

from marrquee.catalog import CatalogApp, get_app
from marrquee.deploy import AppProgress, DeployManager, DeploySnapshot, FakeReadinessProbe
from marrquee.docker_client import ContainerSnapshot, DockerStatus, FakeDockerEngine
from marrquee.login import SavedLogin, load_login, pending_app_ids, save_login
from marrquee.login_apply import FakeLoginApplier, LoginApplier, LoginApplyResult
from marrquee.questions import save_step_answers
from marrquee.state import InstallState, save_state, write_json_atomic
from marrquee.wiring.qbit_client import FakeQbitClient, HttpQbitClient, QbitClient

# --- Small builders shared by every test below --------------------------------


def _running_snapshot(app_id: str) -> ContainerSnapshot:
    return ContainerSnapshot(
        name=app_id,
        exists=True,
        state="running",
        exit_code=None,
        image=get_app(app_id).image,
        detail=None,
    )


def _finale_snapshot(app_ids: tuple[str, ...]) -> DeploySnapshot:
    apps = tuple(
        AppProgress(
            app_id=app_id,
            name=get_app(app_id).name,
            state="done",
            chip="Ready",
            line=f"{get_app(app_id).name} is ready",
            note=None,
            port=get_app(app_id).port,
        )
        for app_id in app_ids
    )
    return DeploySnapshot(
        run_id="a-run-before-this-test",
        phase="finale",
        apps=apps,
        headline="Everything's ready.",
        detail=None,
        failure=None,
        started_at="2026-09-19T00:00:00+00:00",
        finished_at="2026-09-19T00:05:00+00:00",
        wiring=(),
    )


async def _finish_login(manager: DeployManager, *, budget: int = 200_000) -> None:
    """Poll until the login run's own task has finished - the login-run
    equivalent of `test_deploy_add`'s `_finish_add`.
    """
    for _ in range(budget):
        if not manager.is_busy():
            for _ in range(5):
                await asyncio.sleep(0)
            return
        await asyncio.sleep(0)
    raise AssertionError("login run did not finish in time")


def _already_finale_manager(
    tmp_path: Path,
    app_ids: tuple[str, ...],
    *,
    login_applier: LoginApplier,
    probe: FakeReadinessProbe | None = None,
    qbit: QbitClient | None = None,
) -> tuple[DeployManager, FakeDockerEngine, Path]:
    """A manager whose `deploy.json` already says `finale` for `app_ids`,
    each with a `FakeDockerEngine`-modelled running container - the Hub's own
    "existing NAS install" shape a login run is driven against, built
    directly (no real deploy ever runs) the same way
    `test_deploy_add.test_resume_finishes_an_interrupted_add` seeds its own
    already-finale `deploy.json`.
    """
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(app_ids, root)
    save_state(settings.config_dir, install)
    write_json_atomic(
        settings.config_dir / "deploy.json", dataclasses.asdict(_finale_snapshot(app_ids))
    )

    containers = {app_id: _running_snapshot(app_id) for app_id in app_ids}
    engine = FakeDockerEngine(
        DockerStatus(connected=True),
        containers=containers,
        network_exists=True,
        self_container_id="marrquee",
    )
    manager = DeployManager(
        settings,
        engine,
        probe=probe if probe is not None else FakeReadinessProbe(default=True),
        login=login_applier,
        qbit=qbit if qbit is not None else HttpQbitClient(),
    )
    assert manager.snapshot().phase == "finale"
    return manager, engine, settings.config_dir


# --- A fresh deploy puts the login on each app, before wiring -----------------


async def test_fresh_deploy_puts_the_login_on_each_app_before_wiring(tmp_path: Path) -> None:
    order: list[str] = []

    class _OrderedWiringRunner:
        async def run(
            self, state: InstallState, emit: object, *, only_app: str | None = None
        ) -> None:
            order.append("wiring")

    class _OrderedLoginApplier:
        async def apply(
            self, app: CatalogApp, install: InstallState, login: SavedLogin
        ) -> LoginApplyResult:
            order.append(f"login:{app.id}")
            return LoginApplyResult(ok=True, technical=None)

    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    app_ids = ("prowlarr", "sonarr")
    save_state(settings.config_dir, _install_state(app_ids, root))
    saved = save_login(settings.config_dir, "owner", "s3cret-password-1", honor_reset=None)

    engine = _StatefulEngine(app_ids, images={get_app(app_id).image for app_id in app_ids})
    clock = _FakeClock()
    manager = DeployManager(
        settings,
        engine,
        probe=FakeReadinessProbe(default=True),
        clock=clock.time,
        sleep=clock.sleep,
        wiring=_OrderedWiringRunner(),
        login=_OrderedLoginApplier(),
    )
    manager.start()
    history = await _run_to_terminal(manager)
    assert history[-1].phase == "finale"

    assert order == ["login:prowlarr", "login:sonarr", "wiring"]

    record = load_login(settings.config_dir)
    assert record.applied == {"prowlarr": saved.generation, "sonarr": saved.generation}


async def test_a_deploy_with_no_saved_login_never_calls_the_applier(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    app_ids = ("prowlarr",)
    save_state(settings.config_dir, _install_state(app_ids, root))
    applier = FakeLoginApplier()

    engine = _StatefulEngine(app_ids, images={get_app(app_id).image for app_id in app_ids})
    clock = _FakeClock()
    manager = DeployManager(
        settings,
        engine,
        probe=FakeReadinessProbe(default=True),
        clock=clock.time,
        sleep=clock.sleep,
        login=applier,
    )
    manager.start()
    history = await _run_to_terminal(manager)
    assert history[-1].phase == "finale"

    assert applier.calls == []
    assert load_login(settings.config_dir).login is None


async def test_a_full_deploy_applier_failure_never_leaks_the_password(tmp_path: Path) -> None:
    """The same redaction guarantee as the login-run test above, but through
    `_put_login`'s own failure-write path (`_run_steps`'s per-app loop) -
    a distinct call site from `_run_login_steps`, so a redaction dropped
    from just this one site would otherwise go untested.
    """
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    app_ids = ("prowlarr",)
    save_state(settings.config_dir, _install_state(app_ids, root))
    save_login(settings.config_dir, "owner", "hunter2-secret", honor_reset=None)
    applier = FakeLoginApplier(
        results={"prowlarr": False}, technical="400 bad request: hunter2-secret"
    )

    engine = _StatefulEngine(app_ids, images={get_app(app_id).image for app_id in app_ids})
    clock = _FakeClock()
    manager = DeployManager(
        settings,
        engine,
        probe=FakeReadinessProbe(default=True),
        clock=clock.time,
        sleep=clock.sleep,
        login=applier,
    )
    manager.start()
    history = await _run_to_terminal(manager)
    assert history[-1].phase == "finale"

    diagnostics = (settings.config_dir / "last-failure.txt").read_text()
    assert "hunter2-secret" not in diagnostics
    assert "<redacted-password>" in diagnostics
    assert load_login(settings.config_dir).applied == {}


# --- The login run: switches only the apps that accepted ---------------------


async def test_login_run_switches_only_the_apps_that_accepted(tmp_path: Path) -> None:
    app_ids = ("prowlarr", "sonarr", "radarr")
    manager, engine, config_dir = _already_finale_manager(
        tmp_path, app_ids, login_applier=FakeLoginApplier(results={"sonarr": False})
    )
    saved = save_login(config_dir, "owner", "s3cret-password-1", honor_reset=None)

    assert manager.apply_login() == "started"
    await _finish_login(manager)

    recreate_calls = [call[1][2] for call in engine.calls if call[0] == "compose_up_recreate"]
    assert recreate_calls == ["prowlarr", "radarr"]

    record = load_login(config_dir)
    assert record.applied == {"prowlarr": saved.generation, "radarr": saved.generation}
    assert pending_app_ids(record, app_ids) == ("sonarr",)


async def test_login_run_applies_qbittorrent_through_the_key_and_never_recreates_it(
    tmp_path: Path,
) -> None:
    """qBittorrent's login is live the moment phase 1's POST succeeds - it
    must be recorded `applied` right away, and never sit in phase 2's
    recreate loop (that loop is `login_kind == "arr"` only), since
    recreating it would also orphan whatever rides Gluetun's network for
    no reason at all.
    """
    app_ids = ("prowlarr", "gluetun", "qbittorrent")
    manager, engine, config_dir = _already_finale_manager(
        tmp_path,
        app_ids,
        login_applier=FakeLoginApplier(),
        qbit=FakeQbitClient({}),
    )
    save_step_answers(config_dir, "gluetun", _GLUETUN_ANSWERS)
    saved = save_login(config_dir, "owner", "s3cret-password-1", honor_reset=None)

    assert manager.apply_login() == "started"
    await _finish_login(manager)

    recreate_calls = [call[1][2] for call in engine.calls if call[0] == "compose_up_recreate"]
    assert recreate_calls == ["prowlarr"]

    record = load_login(config_dir)
    assert record.applied == {"prowlarr": saved.generation, "qbittorrent": saved.generation}


async def test_login_run_threads_saved_vpn_answers_into_the_stack_plan(tmp_path: Path) -> None:
    """`_run_login_steps`'s own `build_stack_plan` call renders the WHOLE
    install, not just the apps taking the login - Gluetun (`login_kind`
    "none", never a login target itself) still needs its own saved answers
    for that render to succeed, or the run crashes before Prowlarr's own
    recreate ever happens.
    """
    app_ids = ("prowlarr", "gluetun")
    manager, engine, config_dir = _already_finale_manager(
        tmp_path, app_ids, login_applier=FakeLoginApplier()
    )
    save_step_answers(config_dir, "gluetun", _GLUETUN_ANSWERS)
    saved = save_login(config_dir, "owner", "s3cret-password-1", honor_reset=None)

    assert manager.apply_login() == "started"
    await _finish_login(manager)

    record = load_login(config_dir)
    assert record.applied == {"prowlarr": saved.generation}

    diagnostics_path = config_dir / "last-failure.txt"
    assert not diagnostics_path.exists() or "crashed" not in diagnostics_path.read_text()

    compose_text = (
        config_dir.parent / "host" / "volume1" / "media" / "marrquee" / "compose.yaml"
    ).read_text()
    assert "\n  gluetun:\n" in compose_text


async def test_login_run_with_invalid_vpn_answers_records_a_diagnostics_line_not_a_crash(
    tmp_path: Path,
) -> None:
    """When Gluetun's saved answers are missing or invalid, `_run_login_steps`
    must record a plain, redacted diagnostics line and return - never let
    `build_stack_plan`'s `ValueError` escape to the generic "login run
    crashed" handler, and never recreate an app that DID accept the login
    before the run bailed.
    """
    app_ids = ("prowlarr", "gluetun")
    manager, engine, config_dir = _already_finale_manager(
        tmp_path, app_ids, login_applier=FakeLoginApplier()
    )
    # No answers saved for gluetun at all - `check_vpn_answers({})` refuses
    # on the missing provider, same as a fresh install that never asked.
    saved = save_login(config_dir, "owner", "s3cret-password-1", honor_reset=None)

    assert manager.apply_login() == "started"
    await _finish_login(manager)

    # Phase 1 (putting the login on prowlarr) already ran and accepted it,
    # but phase 2 (recreate) never got there - the vpn gate fires first.
    record = load_login(config_dir)
    assert record.applied == {}
    assert pending_app_ids(record, app_ids) == ("prowlarr",)
    assert saved.generation  # sanity: a real generation was saved

    diagnostics_path = config_dir / "last-failure.txt"
    assert diagnostics_path.exists()
    diagnostics = diagnostics_path.read_text()
    assert "crashed" not in diagnostics
    assert "Traceback" not in diagnostics


async def test_a_change_with_unchanged_compose_recreates_with_no_failure(tmp_path: Path) -> None:
    """The fake actually models the no-op: a Change's own recreate leaves
    the already-running container's identity untouched, since nothing about
    the app's own rendered config changed between Choose and Change.
    """
    app_ids = ("prowlarr", "sonarr")
    manager, engine, config_dir = _already_finale_manager(
        tmp_path, app_ids, login_applier=FakeLoginApplier()
    )
    save_login(config_dir, "owner", "first-password-1", honor_reset=None)

    assert manager.apply_login() == "started"
    await _finish_login(manager)

    before = {app_id: await engine.inspect(app_id) for app_id in app_ids}
    assert all(snapshot.state == "running" for snapshot in before.values())

    # A Change: same username, a new password - nothing about the rendered
    # compose file (env vars, API keys) actually changes.
    save_login(config_dir, "owner", "second-password-1", honor_reset=None)

    assert manager.apply_login() == "started"
    await _finish_login(manager)

    after = {app_id: await engine.inspect(app_id) for app_id in app_ids}
    recreate_calls = [call for call in engine.calls if call[0] == "compose_up_recreate"]
    # Both apps went through a real `compose_up_recreate` call each run...
    assert len(recreate_calls) == len(app_ids) * 2
    # ...but the fake's own no-op modelling proves nothing was ACTUALLY
    # recreated the second time: each container's identity is unchanged.
    for app_id in app_ids:
        assert after[app_id].started_at == before[app_id].started_at

    record = load_login(config_dir)
    assert record.login is not None
    assert record.applied == {app_id: record.login.generation for app_id in app_ids}
    assert not (config_dir / "last-failure.txt").exists()


# --- Mutual exclusion with an add -------------------------------------------


async def test_apply_login_refuses_while_an_add_runs(tmp_path: Path) -> None:
    manager, engine, settings = await _deployed_to_finale(tmp_path)
    radarr = get_app("radarr")
    manager._probe = FakeReadinessProbe(  # type: ignore[attr-defined]
        responses={(radarr.id, radarr.port): [False] * 1000}, default=False
    )

    result = manager.add_app("radarr")
    assert result == "started"

    assert manager.apply_login() == "busy"

    # Let the still-running add finish cleanly rather than leaking a task.
    manager._probe = FakeReadinessProbe(default=True)  # type: ignore[attr-defined]
    await _finish_add(manager)


async def test_add_app_refuses_while_a_login_run_runs(tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class _BlockingApplier:
        async def apply(
            self, app: CatalogApp, install: InstallState, login: SavedLogin
        ) -> LoginApplyResult:
            entered.set()
            await release.wait()
            return LoginApplyResult(ok=True, technical=None)

    app_ids = ("prowlarr", "sonarr")
    manager, engine, config_dir = _already_finale_manager(
        tmp_path, app_ids, login_applier=_BlockingApplier()
    )
    save_login(config_dir, "owner", "s3cret-password-1", honor_reset=None)

    assert manager.apply_login() == "started"
    await entered.wait()

    assert manager.add_app("radarr") == "busy"
    # A second `apply_login()` while one is already going refuses the same
    # way - "one thing at a time" covers a login run against itself too.
    assert manager.apply_login() == "busy"

    release.set()
    await _finish_login(manager)


# --- Stale generations are never recorded as applied --------------------------


async def test_a_mid_run_generation_bump_is_never_recorded_as_applied(tmp_path: Path) -> None:
    """Simulates a login saved (bumping the generation) partway through a
    run already in flight for the OLDER generation - `record_applied`'s own
    stale-generation guard (Chunk 1) must hold even when reached through a
    real `DeployManager` run, not just a direct unit call.
    """
    app_ids = ("prowlarr",)

    class _GenerationBumpingApplier:
        def __init__(self, config_dir: Path) -> None:
            self._config_dir = config_dir
            self._bumped = False

        async def apply(
            self, app: CatalogApp, install: InstallState, login: SavedLogin
        ) -> LoginApplyResult:
            if not self._bumped:
                self._bumped = True
                save_login(self._config_dir, "owner", "second-password-1", honor_reset=None)
            return LoginApplyResult(ok=True, technical=None)

    # `Settings.config_dir` is always `tmp_path / "config"` (see
    # `test_deploy._settings`) - deterministic, so the applier can be built
    # with the right path before the manager exists.
    config_dir = tmp_path / "config"
    manager, engine, config_dir = _already_finale_manager(
        tmp_path, app_ids, login_applier=_GenerationBumpingApplier(config_dir)
    )
    original = save_login(config_dir, "owner", "first-password-1", honor_reset=None)

    assert manager.apply_login() == "started"
    await _finish_login(manager)

    record = load_login(config_dir)
    assert record.login is not None
    assert record.login.generation == original.generation + 1
    # The run in flight for the OLD generation never gets to record success
    # for the new one, and `record_applied`'s own guard drops its attempt to
    # record the old generation once a newer one exists.
    assert record.applied.get("prowlarr") != original.generation
    assert record.applied.get("prowlarr") != record.login.generation


# --- Passwords never leak, even on a scripted applier failure -----------------


async def test_an_applier_failure_never_leaks_the_password(tmp_path: Path) -> None:
    app_ids = ("prowlarr",)
    manager, engine, config_dir = _already_finale_manager(
        tmp_path,
        app_ids,
        login_applier=FakeLoginApplier(
            results={"prowlarr": False}, technical="400 bad request: hunter2-secret"
        ),
    )
    save_login(config_dir, "owner", "hunter2-secret", honor_reset=None)

    assert manager.apply_login() == "started"
    await _finish_login(manager)

    diagnostics = (config_dir / "last-failure.txt").read_text()
    assert "hunter2-secret" not in diagnostics
    assert "<redacted-password>" in diagnostics

    record = load_login(config_dir)
    assert record.applied == {}

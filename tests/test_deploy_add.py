"""Tests for the add-one-app run inside `DeployManager`: `add_app`,
`retry_add`, `cancel_add` and `reconnect` - the second kind of run that
lives beside the full deploy but never leaves `phase == "finale"`.

Every test here starts from a `_StatefulEngine`-driven deploy of Prowlarr
+ Sonarr already at `finale`, then exercises the add path against that
same engine and manager: `_StatefulEngine` remembers every container it
creates, so an add that recreated prowlarr or sonarr would be caught here
the same way it would on a real daemon.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path, PurePosixPath

import pytest
from test_deploy import (
    _FakeClock,
    _fresh_root,
    _happy_engine,
    _install_state,
    _run_to_terminal,
    _running_container,
    _settings,
    _StatefulEngine,
)

from marrquee.catalog import AppRule, get_app
from marrquee.config import Settings
from marrquee.deploy import (
    AppAdd,
    AppProgress,
    DeployManager,
    DeploySnapshot,
    FakeReadinessProbe,
    WiringGap,
)
from marrquee.docker_client import ComposeResult
from marrquee.state import InstallState, load_state, save_state, write_json_atomic
from marrquee.storage import read_marker
from marrquee.wiring import WiringStep

# --- A wiring runner that scripts `only_app` and one gap ---------------------


class _RecordingWiringRunner:
    """Records every `only_app` it was called with, and emits whatever
    `WiringStep`s were scripted for that app - a missing entry means a
    clean, silent run.
    """

    def __init__(self, steps_by_app: dict[str, tuple[WiringStep, ...]] | None = None) -> None:
        self._steps_by_app = steps_by_app or {}
        self.calls: list[str | None] = []

    async def run(self, state: InstallState, emit: object, *, only_app: str | None = None) -> None:
        self.calls.append(only_app)
        for step in self._steps_by_app.get(only_app or "", ()):
            emit(step)  # type: ignore[operator]


def _error_step(line: str, technical: str | None = None) -> WiringStep:
    return WiringStep(
        index=1,
        total=1,
        key="app-sync:radarr",
        line=line,
        state="error",
        chip="Error",
        note="Something needs attention",
        technical=technical,
    )


async def _deployed_to_finale(
    tmp_path: Path, app_ids: tuple[str, ...] = ("prowlarr", "sonarr"), **engine_kwargs: object
) -> tuple[DeployManager, _StatefulEngine, Settings]:
    """A manager already at `finale` for `app_ids`, on the same stateful
    engine an add is then driven against.

    Every catalog app's image is pre-pulled by default (not just the ones
    in `app_ids`) - a test scripting `compose_results` for the app it is
    about to add is testing that failure, not an incidental "downloading"
    one caused by the fixture itself.
    """
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(app_ids, root)
    save_state(settings.config_dir, install)

    engine_kwargs.setdefault(
        "images", {get_app(app_id).image for app_id in ("prowlarr", "sonarr", "radarr")}
    )
    engine = _StatefulEngine(app_ids, **engine_kwargs)  # type: ignore[arg-type]
    probe = FakeReadinessProbe(default=True)
    clock = _FakeClock()
    manager = DeployManager(settings, engine, probe=probe, clock=clock.time, sleep=clock.sleep)
    manager.start()
    history = await _run_to_terminal(manager)
    assert history[-1].phase == "finale"
    return manager, engine, settings


async def _finish_add(manager: DeployManager, *, budget: int = 200_000) -> DeploySnapshot:
    """Poll `snapshot()` until an in-flight `adding` clears (success or error)."""
    for _ in range(budget):
        current = manager.snapshot()
        if current.adding is None or current.adding.state == "error":
            for _ in range(5):
                await asyncio.sleep(0)
            return current
        await asyncio.sleep(0)
    raise AssertionError("add did not reach a terminal state in time")


# --- One compose_up only, and the phase never leaves finale ------------------


async def test_an_add_runs_one_compose_up_and_never_leaves_finale(tmp_path: Path) -> None:
    manager, engine, settings = await _deployed_to_finale(tmp_path)
    prowlarr_before = await engine.inspect("prowlarr")
    sonarr_before = await engine.inspect("sonarr")

    wiring = _RecordingWiringRunner()
    manager._wiring = wiring  # type: ignore[attr-defined]

    seen: list[DeploySnapshot] = []
    original_emit = manager._emit  # type: ignore[attr-defined]

    def recording_emit(snapshot: DeploySnapshot) -> None:
        seen.append(snapshot)
        original_emit(snapshot)

    manager._emit = recording_emit  # type: ignore[method-assign]

    calls_before_add = len(engine.calls)
    result = manager.add_app("radarr")
    assert result == "started"

    final = await _finish_add(manager)

    assert final.adding is None
    assert all(snapshot.phase == "finale" for snapshot in seen)

    calls_during_add = engine.calls[calls_before_add:]
    compose_up_calls = [call for call in calls_during_add if call[0] == "compose_up"]
    assert [call[1][2] for call in compose_up_calls] == ["radarr"]

    prowlarr_after = await engine.inspect("prowlarr")
    sonarr_after = await engine.inspect("sonarr")
    assert prowlarr_after == prowlarr_before
    assert sonarr_after == sonarr_before

    assert [app.app_id for app in final.apps] == ["prowlarr", "sonarr", "radarr"]
    assert final.apps[-1].state == "done"
    assert wiring.calls == ["radarr"]


async def test_the_grown_install_keeps_old_keys_marker_and_compose_list_all_three(
    tmp_path: Path,
) -> None:
    manager, engine, settings = await _deployed_to_finale(tmp_path)
    before = load_state(settings.config_dir)
    assert before is not None

    manager.add_app("radarr")
    await _finish_add(manager)

    after = load_state(settings.config_dir)
    assert after is not None
    assert after.app_ids == ("prowlarr", "sonarr", "radarr")
    assert after.api_keys["prowlarr"] == before.api_keys["prowlarr"]
    assert after.api_keys["sonarr"] == before.api_keys["sonarr"]
    assert "radarr" in after.api_keys

    root_path = settings.host_mount / "volume1" / "media"
    marker = read_marker(settings, PurePosixPath("/volume1/media"))
    assert marker is not None
    assert set(marker.app_ids) == {"prowlarr", "sonarr", "radarr"}

    compose_text = (root_path / "marrquee" / "compose.yaml").read_text()
    assert compose_text.count("container_name:") == 3
    radarr_service = compose_text.split("\n  radarr:\n", 1)[1]
    assert "- marrquee" in radarr_service.split("networks:", 1)[1]


# --- A failed add keeps everything else intact -------------------------------


async def test_a_failed_add_keeps_the_other_apps_and_overwrites_last_problem(
    tmp_path: Path,
) -> None:
    manager, engine, settings = await _deployed_to_finale(tmp_path)
    (settings.config_dir / "last-failure.txt").write_text("old\n")

    engine._compose_results["radarr"] = ComposeResult(  # type: ignore[attr-defined]
        ok=False, exit_code=1, output="Error: port is already allocated"
    )

    result = manager.add_app("radarr")
    assert result == "started"
    final = await _finish_add(manager)

    assert final.adding is not None
    assert final.adding.state == "error"
    assert final.adding.failure is not None
    assert final.adding.failure.code == "port_in_use"
    assert final.adding.compose_ran is True
    assert [app.app_id for app in final.apps] == ["prowlarr", "sonarr"]

    diagnostics = (settings.config_dir / "last-failure.txt").read_text()
    # The add's own first technical write REPLACES the file - it must not
    # still hold the older run's own text, appended onto or otherwise.
    assert "old" not in diagnostics
    assert "port_in_use" in diagnostics


async def test_a_clean_add_leaves_an_older_last_problem_alone(tmp_path: Path) -> None:
    manager, engine, settings = await _deployed_to_finale(tmp_path)
    (settings.config_dir / "last-failure.txt").write_text("old\n")

    manager.add_app("radarr")
    final = await _finish_add(manager)

    assert final.adding is None
    assert (settings.config_dir / "last-failure.txt").read_text() == "old\n"


# --- Cancel: only ever removes a container it created, never a folder -------


async def test_cancel_removes_only_a_container_it_created_and_shrinks_install(
    tmp_path: Path,
) -> None:
    manager, engine, settings = await _deployed_to_finale(tmp_path)
    engine._compose_results["radarr"] = ComposeResult(  # type: ignore[attr-defined]
        ok=False, exit_code=1, output="Error: port is already allocated"
    )

    manager.add_app("radarr")
    failed = await _finish_add(manager)
    assert failed.adding is not None and failed.adding.state == "error"

    # The key the failed attempt generated and saved for radarr - Cancel
    # must keep it exactly, ready for a later re-add.
    grown = load_state(settings.config_dir)
    assert grown is not None
    grown_key = grown.api_keys["radarr"]

    ok = await manager.cancel_add()
    assert ok is True
    assert any(call[0] == "remove_container" and call[1] == ("radarr",) for call in engine.calls)

    after_cancel = load_state(settings.config_dir)
    assert after_cancel is not None
    assert after_cancel.app_ids == ("prowlarr", "sonarr")
    # The key is kept, ready for a later re-add.
    assert after_cancel.api_keys.get("radarr") == grown_key

    root_path = settings.host_mount / "volume1" / "media"
    assert (root_path / "marrquee").exists()  # never deletes a folder
    assert manager.snapshot().adding is None


async def test_cancel_never_removes_a_container_it_did_not_create(tmp_path: Path) -> None:
    manager, engine, settings = await _deployed_to_finale(tmp_path)
    # Someone else's "radarr" container, already on the daemon before the
    # add ever ran - a genuine name clash, never our own.
    engine._containers["radarr"] = _running_container("radarr")  # type: ignore[attr-defined]

    manager.add_app("radarr")
    final = await _finish_add(manager)

    assert final.adding is not None
    assert final.adding.state == "error"
    assert final.adding.failure is not None
    assert final.adding.failure.code == "name_clash"
    assert final.adding.compose_ran is False

    ok = await manager.cancel_add()
    assert ok is True
    assert not any(call[0] == "remove_container" for call in engine.calls)

    after_cancel = load_state(settings.config_dir)
    assert after_cancel is not None
    assert after_cancel.app_ids == ("prowlarr", "sonarr")


async def test_cancel_that_cannot_remove_the_container_keeps_the_failed_tile(
    tmp_path: Path,
) -> None:
    manager, engine, settings = await _deployed_to_finale(
        tmp_path, remove_results={"radarr": False}
    )
    engine._compose_results["radarr"] = ComposeResult(  # type: ignore[attr-defined]
        ok=False, exit_code=1, output="Error: port is already allocated"
    )

    manager.add_app("radarr")
    await _finish_add(manager)

    ok = await manager.cancel_add()
    assert ok is False

    still_failed = manager.snapshot()
    assert still_failed.adding is not None
    assert still_failed.adding.state == "error"
    assert "Radarr" in still_failed.adding.line

    after_cancel = load_state(settings.config_dir)
    assert after_cancel is not None
    assert after_cancel.app_ids == ("prowlarr", "sonarr", "radarr")  # nothing was shrunk


async def test_cancel_never_removes_a_container_when_a_reconnect_fails(tmp_path: Path) -> None:
    """A reconnect's own `AppAdd` always carries `compose_ran=True` (nothing
    about reconnecting ever calls Docker) - so `compose_ran` alone can't be
    Cancel's guard. If it were, Cancel on a failed reconnect would remove an
    already-installed, working app's own container. `purpose == "add"` is
    the guard that has to hold too.

    The failure itself is forced through `_emit`, not the wiring runner -
    `_run_wiring_for_add` already swallows a raising runner (a wiring
    problem is never an add failure), so the one path that can still reach
    `_fail_add` for a reconnect is the run's own final publish failing.
    """
    manager, engine, settings = await _deployed_to_finale(tmp_path)
    original_emit = manager._emit  # type: ignore[attr-defined]
    calls = {"n": 0}

    def flaky_emit(snapshot: DeploySnapshot) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk full")
        original_emit(snapshot)

    result = manager.reconnect("sonarr")
    assert result == "started"
    manager._emit = flaky_emit  # type: ignore[method-assign]

    failed = await _finish_add(manager)
    assert failed.adding is not None
    assert failed.adding.purpose == "reconnect"
    assert failed.adding.state == "error"
    assert failed.adding.compose_ran is True

    ok = await manager.cancel_add()

    assert ok is False
    assert not any(call[0] == "remove_container" for call in engine.calls)
    still_installed = load_state(settings.config_dir)
    assert still_installed is not None
    assert "sonarr" in still_installed.app_ids
    untouched = manager.snapshot().adding
    assert untouched is not None
    assert untouched.state == "error"


# --- Retry: press it again, without losing what already happened -----------


async def test_retry_add_reruns_a_failed_add_and_can_finish(tmp_path: Path) -> None:
    manager, engine, settings = await _deployed_to_finale(tmp_path)
    manager._wiring = _RecordingWiringRunner()  # type: ignore[attr-defined]
    engine._compose_results["radarr"] = ComposeResult(  # type: ignore[attr-defined]
        ok=False, exit_code=1, output="Error: port is already allocated"
    )

    manager.add_app("radarr")
    failed = await _finish_add(manager)
    assert failed.adding is not None
    assert failed.adding.state == "error"
    assert failed.adding.compose_ran is True

    # The port is free now - a real "fix it and try again" moment.
    del engine._compose_results["radarr"]  # type: ignore[attr-defined]
    result = manager.retry_add()
    assert result == "started"

    final = await _finish_add(manager)
    assert final.adding is None
    assert [app.app_id for app in final.apps] == ["prowlarr", "sonarr", "radarr"]


async def test_retry_add_refuses_when_nothing_has_failed(tmp_path: Path) -> None:
    manager, engine, settings = await _deployed_to_finale(tmp_path)
    assert manager.retry_add() == "busy"


async def test_retry_add_refuses_while_an_add_is_still_running(tmp_path: Path) -> None:
    manager, engine, settings = await _deployed_to_finale(tmp_path)
    manager._wiring = _RecordingWiringRunner()  # type: ignore[attr-defined]

    manager.add_app("radarr")
    assert manager.retry_add() == "busy"
    await _finish_add(manager)


# --- Wiring gaps and reconnect ------------------------------------------------


async def test_a_failed_wiring_step_records_a_gap_and_a_clean_reconnect_clears_it(
    tmp_path: Path,
) -> None:
    manager, engine, settings = await _deployed_to_finale(tmp_path)
    failing_wiring = _RecordingWiringRunner(
        {"radarr": (_error_step("Sync Prowlarr with Radarr", technical="raw detail"),)}
    )
    manager._wiring = failing_wiring  # type: ignore[attr-defined]

    manager.add_app("radarr")
    final = await _finish_add(manager)

    assert final.adding is None  # wiring never fails the add itself
    assert [app.app_id for app in final.apps] == ["prowlarr", "sonarr", "radarr"]
    assert final.wiring_gaps == (
        WiringGap(app_id="radarr", failed_lines=("Sync Prowlarr with Radarr",)),
    )

    clean_wiring = _RecordingWiringRunner()
    manager._wiring = clean_wiring  # type: ignore[attr-defined]

    result = manager.reconnect("radarr")
    assert result == "started"
    reconnected = await _finish_add(manager)

    assert reconnected.adding is None
    assert reconnected.wiring_gaps == ()
    assert clean_wiring.calls == ["radarr"]


async def test_reconnect_refuses_an_app_that_is_not_installed(tmp_path: Path) -> None:
    manager, engine, settings = await _deployed_to_finale(tmp_path)
    assert manager.reconnect("radarr") == "unknown_app"


async def test_reconnect_refuses_while_an_add_is_running(tmp_path: Path) -> None:
    manager, engine, settings = await _deployed_to_finale(tmp_path)
    manager._wiring = _RecordingWiringRunner()  # type: ignore[attr-defined]

    manager.add_app("radarr")
    assert manager.reconnect("sonarr") == "busy"
    await _finish_add(manager)


# --- add_app's refusal table --------------------------------------------------


async def test_add_app_refuses_busy_while_running(tmp_path: Path) -> None:
    manager, engine, settings = await _deployed_to_finale(tmp_path)
    manager._wiring = _RecordingWiringRunner()  # type: ignore[attr-defined]

    assert manager.add_app("radarr") == "started"
    assert manager.add_app("prowlarr") == "busy"
    await _finish_add(manager)


async def test_add_app_refuses_busy_while_a_failed_add_waits(tmp_path: Path) -> None:
    manager, engine, settings = await _deployed_to_finale(tmp_path)
    engine._compose_results["radarr"] = ComposeResult(  # type: ignore[attr-defined]
        ok=False, exit_code=1, output="boom"
    )
    manager.add_app("radarr")
    await _finish_add(manager)

    assert manager.add_app("radarr") == "busy"


async def test_add_app_refuses_already_installed(tmp_path: Path) -> None:
    manager, engine, settings = await _deployed_to_finale(tmp_path)
    assert manager.add_app("sonarr") == "already_installed"


async def test_add_app_prefers_already_installed_over_an_unrelated_unavailable_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An already-installed app is refused as `"already_installed"`, even
    when it also happens to carry a rule that would otherwise refuse it -
    the owner never sees "not available yet" for an app already running.
    """
    import marrquee.catalog as catalog_module

    manager, engine, settings = await _deployed_to_finale(tmp_path)

    patched = tuple(
        dataclasses.replace(
            app,
            rules=(AppRule(kind="needs_any", app_ids=("nothing-installed-has-this",), reason="x"),),
        )
        if app.id == "sonarr"
        else app
        for app in catalog_module.CATALOG
    )
    monkeypatch.setattr(catalog_module, "CATALOG", patched)

    assert manager.add_app("sonarr") == "already_installed"


async def test_add_app_refuses_unknown_app(tmp_path: Path) -> None:
    manager, engine, settings = await _deployed_to_finale(tmp_path)
    assert manager.add_app("not-a-real-app") == "unknown_app"


async def test_add_app_refuses_not_ready_before_finale(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = _happy_engine(())
    manager = DeployManager(settings, engine)
    assert manager.add_app("radarr") == "not_ready"


async def test_add_app_refuses_unavailable_via_a_catalog_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import marrquee.catalog as catalog_module

    manager, engine, settings = await _deployed_to_finale(tmp_path, app_ids=("sonarr",))

    patched = tuple(
        dataclasses.replace(
            app,
            rules=(
                AppRule(kind="needs_any", app_ids=("prowlarr",), reason="needs Prowlarr first"),
            ),
        )
        if app.id == "radarr"
        else app
        for app in catalog_module.CATALOG
    )
    monkeypatch.setattr(catalog_module, "CATALOG", patched)

    assert manager.add_app("radarr") == "unavailable"


# --- Restart safety ------------------------------------------------------------


async def test_resume_finishes_an_interrupted_add(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    grown = _install_state(("prowlarr", "sonarr", "radarr"), root)
    save_state(settings.config_dir, grown)

    finale = DeploySnapshot(
        run_id="run-before-a-restart",
        phase="finale",
        apps=(
            AppProgress(
                app_id="prowlarr",
                name="Prowlarr",
                state="done",
                chip="Ready",
                line="Prowlarr is ready",
                note=None,
                port=9696,
            ),
            AppProgress(
                app_id="sonarr",
                name="Sonarr",
                state="done",
                chip="Ready",
                line="Sonarr is ready",
                note=None,
                port=8989,
            ),
        ),
        headline="Everything's ready.",
        detail=None,
        failure=None,
        started_at="2026-09-19T00:00:00+00:00",
        finished_at="2026-09-19T00:05:00+00:00",
        wiring=(),
        adding=AppAdd(
            app_id="radarr",
            purpose="add",
            state="starting",
            line="Starting Radarr",
            note=None,
            failure=None,
            wiring=(),
            compose_ran=False,
            started_at="2026-09-19T00:06:00+00:00",
        ),
        wiring_gaps=(),
    )
    write_json_atomic(settings.config_dir / "deploy.json", dataclasses.asdict(finale))

    engine = _StatefulEngine(("prowlarr", "sonarr"))
    # The engine already thinks prowlarr/sonarr are running, matching a
    # real restart mid-way through radarr's own add.
    await engine.compose_up("marrquee-apps", Path("/dev/null"), "prowlarr")
    await engine.compose_up("marrquee-apps", Path("/dev/null"), "sonarr")
    probe = FakeReadinessProbe(default=True)
    clock = _FakeClock()
    manager = DeployManager(settings, engine, probe=probe, clock=clock.time, sleep=clock.sleep)

    assert manager.snapshot().phase == "finale"
    assert manager.snapshot().adding is not None

    await manager.resume_if_interrupted()
    final = await _finish_add(manager)

    assert final.adding is None
    assert [app.app_id for app in final.apps] == ["prowlarr", "sonarr", "radarr"]


def test_an_older_deploy_json_with_no_adding_key_loads_as_nothing_in_flight(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    # An older deploy.json shape, saved before `adding`/`wiring_gaps`
    # existed - every key that predates them is still here.
    older_payload: dict[str, object] = {
        "run_id": "old-run",
        "phase": "finale",
        "apps": [],
        "headline": "Everything's ready.",
        "detail": None,
        "failure": None,
        "started_at": "2026-09-19T00:00:00+00:00",
        "finished_at": "2026-09-19T00:05:00+00:00",
    }
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    (settings.config_dir / "deploy.json").write_text(json.dumps(older_payload))

    engine = _happy_engine(())
    manager = DeployManager(settings, engine)

    snapshot = manager.snapshot()
    assert snapshot.phase == "finale"
    assert snapshot.adding is None
    assert snapshot.wiring_gaps == ()


async def test_start_during_an_add_starts_nothing(tmp_path: Path) -> None:
    manager, engine, settings = await _deployed_to_finale(tmp_path)
    manager._wiring = _RecordingWiringRunner()  # type: ignore[attr-defined]

    manager.add_app("radarr")
    before_calls = list(engine.calls)

    result = manager.start()
    assert result.phase == "finale"
    assert result.adding is not None

    await _finish_add(manager)
    # `start()` issued no compose_up of its own - the only new engine
    # activity comes from the add already in flight.
    new_calls = engine.calls[len(before_calls) :]
    assert not any(name == "compose_up" and args[2] != "radarr" for name, args in new_calls)

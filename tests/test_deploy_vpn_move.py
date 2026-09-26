"""Tests for the engine that moves qBittorrent behind a VPN, changes an
already-connected VPN, and restores qBittorrent on its own network when a
move fails: `add_app("gluetun")` with movers, `change_vpn()`, `cancel_add()`'s
restore path, and `retry_add()`/`resume_if_interrupted()` for both.

Every scenario here builds on `test_deploy`'s own fixtures
(`_GLUETUN_ANSWERS`, `_FakeClock`, `_fresh_root`, `_install_state`,
`_run_to_terminal`, `_settings`, `_StatefulEngine`), `test_deploy_add`'s
`_deployed_to_finale`/`_finish_add` and `test_deploy_login`'s
`_already_finale_manager`/`_finish_login` - nothing here touches a real
Docker daemon, a real network or waits a real second.
"""

from __future__ import annotations

import dataclasses
import inspect
from datetime import UTC, datetime
from pathlib import Path

import yaml
from test_deploy import (
    _GLUETUN_ANSWERS,
    _fresh_root,
    _install_state,
    _RecordingHardlinkTrigger,
    _run_to_terminal,
    _settings,
    _StatefulEngine,
)
from test_deploy_add import _deployed_to_finale, _finish_add, _RecordingWiringRunner
from test_deploy_login import _already_finale_manager, _finish_login

import marrquee.deploy as deploy_module
from marrquee.catalog import get_app
from marrquee.config import Settings
from marrquee.deploy import AppAdd, DeployManager, FakeReadinessProbe
from marrquee.docker_client import ComposeResult
from marrquee.login import load_login, pending_app_ids, save_login
from marrquee.login_apply import FakeLoginApplier
from marrquee.questions import save_step_answers
from marrquee.state import load_state, save_state, write_json_atomic
from marrquee.vpn import TunnelPlace
from marrquee.vpn_control import FakeGluetunControl, GluetunControl, NoGluetunControl
from marrquee.wiring.qbit_client import FakeQbitClient, QbitClient, QbitResponse
from marrquee.without_vpn import save_without_vpn, without_vpn_confirmed

_QBIT_IMAGES = {
    get_app(app_id).image for app_id in ("prowlarr", "sonarr", "radarr", "gluetun", "qbittorrent")
}


def _place() -> TunnelPlace:
    return TunnelPlace(
        public_ip="185.1.1.1", city="Amsterdam", region="North Holland", country="Netherlands"
    )


def _ok_version() -> QbitResponse:
    return QbitResponse(ok=True, status=200, payload={"version": "5.2.3"}, detail=None)


def _qbit_client(count: int = 8) -> FakeQbitClient:
    """A qBittorrent client with plenty of "yes, I'm up" answers scripted
    for BOTH the host it answers on without a VPN and the host it answers
    on behind one - every scenario in this file moves it between the two
    at least once, often twice (an initial deploy, then a move or a retry).
    """
    return FakeQbitClient(
        {
            ("GET", "http://qbittorrent:8080", "api/v2/app/version"): [
                _ok_version() for _ in range(count)
            ],
            ("GET", "http://gluetun:8080", "api/v2/app/version"): [
                _ok_version() for _ in range(count)
            ],
        }
    )


def _service_name(call: tuple[str, tuple[object, ...]]) -> str | None:
    """The app id a recorded engine call was actually about - the third
    positional argument for a compose call (the first is the compose
    project), the first (and only) one for `stop_container`/`remove_container`.
    """
    name, args = call
    if name in ("compose_up", "compose_up_recreate"):
        return str(args[2])
    if name in ("stop_container", "remove_container"):
        return str(args[0])
    return None


async def _qbittorrent_without_vpn_to_finale(
    tmp_path: Path,
    *,
    app_ids: tuple[str, ...] = ("sonarr", "qbittorrent"),
    vpn: GluetunControl | None = None,
    qbit: QbitClient | None = None,
    **engine_kwargs: object,
) -> tuple[DeployManager, _StatefulEngine, Settings]:
    """A manager already at `finale` with qBittorrent running on its own
    network, its own port published, and the break-glass confirmation
    already on disk - the starting point every "Add your VPN" test moves
    from.
    """
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(app_ids, root)
    if "qbittorrent" in app_ids:
        # qBittorrent's key must actually pass `render_qbit_conf`'s own
        # shape check (`qbt_` + 28 characters) - `_install_state`'s plain
        # "fake-<id>-api-key" strings are fine for every arr app, which
        # never validates its key's shape at all.
        install = dataclasses.replace(
            install, api_keys={**install.api_keys, "qbittorrent": "qbt_" + "a" * 28}
        )
    save_state(settings.config_dir, install)
    save_login(settings.config_dir, "owner", "s3cret-password-1", honor_reset=None)
    save_without_vpn(settings.config_dir, now=datetime.now(UTC))

    engine_kwargs.setdefault("images", _QBIT_IMAGES)
    engine = _StatefulEngine(app_ids, **engine_kwargs)  # type: ignore[arg-type]
    manager = DeployManager(
        settings,
        engine,
        probe=FakeReadinessProbe(default=True),
        login=FakeLoginApplier(),
        vpn=vpn if vpn is not None else NoGluetunControl(),
        qbit=qbit if qbit is not None else _qbit_client(),
    )
    manager.start()
    history = await _run_to_terminal(manager)
    assert history[-1].phase == "finale"
    return manager, engine, settings


async def _protected_qbittorrent_to_finale(
    tmp_path: Path, **engine_kwargs: object
) -> tuple[DeployManager, _StatefulEngine, Settings]:
    """A manager already at `finale` with qBittorrent already running
    behind Gluetun - the starting point every Change VPN test moves from.
    """
    app_ids = ("sonarr", "gluetun", "qbittorrent")
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = dataclasses.replace(
        _install_state(app_ids, root),
        api_keys={
            **{app_id: f"fake-{app_id}-api-key" for app_id in app_ids},
            "qbittorrent": "qbt_" + "a" * 28,
        },
    )
    save_state(settings.config_dir, install)
    save_login(settings.config_dir, "owner", "s3cret-password-1", honor_reset=None)
    save_step_answers(settings.config_dir, "gluetun", _GLUETUN_ANSWERS)

    engine_kwargs.setdefault("images", _QBIT_IMAGES)
    engine_kwargs.setdefault("health_frames", {"gluetun": ["healthy"]})
    engine = _StatefulEngine(app_ids, **engine_kwargs)  # type: ignore[arg-type]
    manager = DeployManager(
        settings,
        engine,
        probe=FakeReadinessProbe(default=True),
        login=FakeLoginApplier(),
        vpn=FakeGluetunControl(status="running", place=_place()),
        qbit=_qbit_client(),
    )
    manager.start()
    history = await _run_to_terminal(manager)
    assert history[-1].phase == "finale"
    return manager, engine, settings


# --- FIRST TEST: the safe move order, and a refused tunnel never recreates it


async def test_add_your_vpn_stops_then_removes_qbittorrent_before_starting_gluetun(
    tmp_path: Path,
) -> None:
    manager, engine, settings = await _qbittorrent_without_vpn_to_finale(
        tmp_path,
        vpn=FakeGluetunControl(status="running", place=_place()),
        health_frames={"gluetun": ["healthy"]},
    )
    save_step_answers(settings.config_dir, "gluetun", _GLUETUN_ANSWERS)

    result = manager.add_app("gluetun")
    assert result == "started"
    final = await _finish_add(manager)

    assert final.adding is None
    # Catalog (deploy) order, never insertion order: gluetun (order 3)
    # sorts before qBittorrent (order 4).
    assert [progress.app_id for progress in final.apps] == ["sonarr", "gluetun", "qbittorrent"]
    assert without_vpn_confirmed(settings.config_dir) is False

    stop_index = next(
        i for i, call in enumerate(engine.calls) if call == ("stop_container", ("qbittorrent",))
    )
    remove_index = next(
        i for i, call in enumerate(engine.calls) if call == ("remove_container", ("qbittorrent",))
    )
    gluetun_up_index = next(
        i
        for i, call in enumerate(engine.calls)
        if call[0] == "compose_up" and call[1][2] == "gluetun"
    )
    qbit_up_index = next(
        i
        for i, call in enumerate(engine.calls)
        if call[0] == "compose_up" and call[1][2] == "qbittorrent" and i > remove_index
    )
    assert stop_index < remove_index < gluetun_up_index < qbit_up_index


async def test_a_successful_move_wires_the_mover_never_gluetun_itself(tmp_path: Path) -> None:
    manager, engine, settings = await _qbittorrent_without_vpn_to_finale(
        tmp_path,
        vpn=FakeGluetunControl(status="running", place=_place()),
        health_frames={"gluetun": ["healthy"]},
    )
    save_step_answers(settings.config_dir, "gluetun", _GLUETUN_ANSWERS)
    wiring = _RecordingWiringRunner()
    manager._wiring = wiring  # type: ignore[attr-defined]

    assert manager.add_app("gluetun") == "started"
    final = await _finish_add(manager)

    assert final.adding is None
    assert wiring.calls == ["qbittorrent"]


async def test_add_your_vpn_movers_success_asks_for_one_check(tmp_path: Path) -> None:
    manager, engine, settings = await _qbittorrent_without_vpn_to_finale(
        tmp_path,
        vpn=FakeGluetunControl(status="running", place=_place()),
        health_frames={"gluetun": ["healthy"]},
    )
    save_step_answers(settings.config_dir, "gluetun", _GLUETUN_ANSWERS)
    trigger = _RecordingHardlinkTrigger()
    manager._hardlinks = trigger  # type: ignore[attr-defined]

    assert manager.add_app("gluetun") == "started"
    final = await _finish_add(manager)

    assert final.adding is None
    assert trigger.calls == 1


async def test_a_refused_tunnel_never_recreates_qbittorrent(tmp_path: Path) -> None:
    manager, engine, settings = await _qbittorrent_without_vpn_to_finale(tmp_path)
    save_step_answers(settings.config_dir, "gluetun", _GLUETUN_ANSWERS)
    engine._logs["gluetun"] = "AUTH: Received control message: AUTH_FAILED, retrying"  # type: ignore[attr-defined]

    result = manager.add_app("gluetun")
    assert result == "started"
    failed = await _finish_add(manager)

    assert failed.adding is not None
    assert failed.adding.state == "error"
    assert failed.adding.failure is not None
    assert failed.adding.failure.code == "vpn_refused"
    assert failed.adding.moves == ("qbittorrent",)
    remove_index = next(
        i for i, call in enumerate(engine.calls) if call == ("remove_container", ("qbittorrent",))
    )
    assert not any(
        call[0] == "compose_up" and call[1][2] == "qbittorrent"
        for call in engine.calls[remove_index:]
    )
    # The confirmation survives a FAILED move - only a successful one clears it.
    assert without_vpn_confirmed(settings.config_dir) is True


async def test_a_failed_removal_stops_the_move_before_anything_starts(tmp_path: Path) -> None:
    manager, engine, settings = await _qbittorrent_without_vpn_to_finale(
        tmp_path, remove_results={"qbittorrent": False}
    )
    save_step_answers(settings.config_dir, "gluetun", _GLUETUN_ANSWERS)

    result = manager.add_app("gluetun")
    assert result == "started"
    failed = await _finish_add(manager)

    assert failed.adding is not None
    assert failed.adding.state == "error"
    assert failed.adding.failure is not None
    assert failed.adding.failure.code == "compose_failed"
    assert not any(call[0] == "compose_up" and call[1][2] == "gluetun" for call in engine.calls)


# --- Keep running without VPN restores qBittorrent -----------------------


async def test_keep_running_without_vpn_restores_qbittorrent_on_its_own_network(
    tmp_path: Path,
) -> None:
    qbit = _qbit_client()
    manager, engine, settings = await _qbittorrent_without_vpn_to_finale(tmp_path, qbit=qbit)
    save_step_answers(settings.config_dir, "gluetun", _GLUETUN_ANSWERS)
    # A refused tunnel is the failure "Keep running without VPN" recovers from.
    engine._logs["gluetun"] = "AUTH: Received control message: AUTH_FAILED, retrying"  # type: ignore[attr-defined]

    assert manager.add_app("gluetun") == "started"
    failed = await _finish_add(manager)
    assert failed.adding is not None
    assert failed.adding.state == "error"

    calls_before_cancel = len(engine.calls)
    ok = await manager.cancel_add()
    assert ok is True

    restoring = manager.snapshot()
    assert restoring.adding is not None
    assert restoring.adding.purpose == "restore"

    final = await _finish_add(manager)
    assert final.adding is None

    cancel_calls = engine.calls[calls_before_cancel:]
    assert ("remove_container", ("gluetun",)) in cancel_calls
    assert any(call[0] == "compose_up" and call[1][2] == "qbittorrent" for call in cancel_calls)
    # The restore's own readiness probe reaches qBittorrent on its own
    # network - never through Gluetun, which no longer exists. (The
    # initial deploy already made one such call; the restore makes a
    # second.)
    own_network_calls = [call for call in qbit.calls if call[1] == "http://qbittorrent:8080"]
    assert len(own_network_calls) >= 2
    assert not any(call[1] == "http://gluetun:8080" for call in qbit.calls)

    install = load_state(settings.config_dir)
    assert install is not None
    assert "gluetun" not in install.app_ids
    assert "qbittorrent" in install.app_ids

    secrets_folder = settings.host_mount / "volume1" / "media" / "marrquee" / "vpn"
    assert not any(entry.is_file() for entry in secrets_folder.iterdir())

    # The confirmation is KEPT - the owner is still running without a VPN.
    assert without_vpn_confirmed(settings.config_dir) is True

    compose_text = (
        settings.host_mount / "volume1" / "media" / "marrquee" / "compose.yaml"
    ).read_text()
    doc = yaml.safe_load(compose_text)
    assert doc["services"]["qbittorrent"]["networks"] == ["marrquee"]
    assert "gluetun" not in doc["services"]


async def test_restore_asks_for_no_check(tmp_path: Path) -> None:
    """Restore ends in `_run_wiring_for_add` directly, never `_run_add_steps`
    - it moves an existing pair back, it never plans a new folder.
    """
    manager, engine, settings = await _qbittorrent_without_vpn_to_finale(tmp_path)
    trigger = _RecordingHardlinkTrigger()
    manager._hardlinks = trigger  # type: ignore[attr-defined]
    save_step_answers(settings.config_dir, "gluetun", _GLUETUN_ANSWERS)
    engine._logs["gluetun"] = "AUTH: Received control message: AUTH_FAILED, retrying"  # type: ignore[attr-defined]

    assert manager.add_app("gluetun") == "started"
    failed = await _finish_add(manager)
    assert failed.adding is not None
    assert failed.adding.state == "error"
    assert trigger.calls == 0  # the failed move itself asks for none

    ok = await manager.cancel_add()
    assert ok is True
    final = await _finish_add(manager)

    assert final.adding is None
    assert trigger.calls == 0


async def test_cancel_of_a_move_without_the_confirmation_refuses_and_keeps_the_record(
    tmp_path: Path,
) -> None:
    manager, engine, settings = await _qbittorrent_without_vpn_to_finale(tmp_path)
    save_step_answers(settings.config_dir, "gluetun", _GLUETUN_ANSWERS)
    engine._logs["gluetun"] = "AUTH: Received control message: AUTH_FAILED, retrying"  # type: ignore[attr-defined]

    assert manager.add_app("gluetun") == "started"
    failed = await _finish_add(manager)
    assert failed.adding is not None
    assert failed.adding.state == "error"

    # Simulate the confirmation record having been lost from under this run.
    (settings.config_dir / "without_vpn.json").unlink()
    calls_before_cancel = len(engine.calls)

    ok = await manager.cancel_add()

    assert ok is False
    still_there = manager.snapshot()
    assert still_there.adding is not None
    assert still_there.adding.state == "error"
    assert still_there.adding.moves == ("qbittorrent",)
    # Nothing was touched - the refusal happens before any container work.
    assert engine.calls[calls_before_cancel:] == []


# --- Change VPN -----------------------------------------------------------


async def test_change_vpn_removes_and_recreates_gluetun_with_the_new_secrets_then_qbittorrent(
    tmp_path: Path,
) -> None:
    manager, engine, settings = await _protected_qbittorrent_to_finale(tmp_path)

    new_answers = dict(_GLUETUN_ANSWERS, openvpn_user="a-brand-new-username")
    save_step_answers(settings.config_dir, "gluetun", new_answers)

    calls_before = len(engine.calls)
    result = manager.change_vpn()
    assert result == "started"

    final = await _finish_add(manager)
    assert final.adding is None

    run_calls = engine.calls[calls_before:]
    qbit_calls = [call[0] for call in run_calls if _service_name(call) == "qbittorrent"]
    gluetun_calls = [call[0] for call in run_calls if _service_name(call) == "gluetun"]
    assert "stop_container" in qbit_calls
    assert "remove_container" in qbit_calls
    assert "remove_container" in gluetun_calls
    assert "compose_up" in gluetun_calls
    assert "compose_up_recreate" not in gluetun_calls

    remove_qbit_index = next(
        i for i, call in enumerate(run_calls) if call == ("remove_container", ("qbittorrent",))
    )
    remove_gluetun_index = next(
        i for i, call in enumerate(run_calls) if call == ("remove_container", ("gluetun",))
    )
    gluetun_up_index = next(
        i for i, call in enumerate(run_calls) if call[0] == "compose_up" and call[1][2] == "gluetun"
    )
    qbit_up_index = next(
        i
        for i, call in enumerate(run_calls)
        if call[0] == "compose_up" and call[1][2] == "qbittorrent"
    )
    assert remove_qbit_index < remove_gluetun_index < gluetun_up_index < qbit_up_index

    secrets_folder = settings.host_mount / "volume1" / "media" / "marrquee" / "vpn"
    assert (secrets_folder / "openvpn_user").read_text() == "a-brand-new-username"


async def test_change_vpn_asks_for_no_check(tmp_path: Path) -> None:
    """Change VPN ends in `_run_wiring_for_add` directly, never
    `_run_add_steps` - it recreates an existing pair, it never plans one.
    """
    manager, engine, settings = await _protected_qbittorrent_to_finale(tmp_path)
    trigger = _RecordingHardlinkTrigger()
    manager._hardlinks = trigger  # type: ignore[attr-defined]

    new_answers = dict(_GLUETUN_ANSWERS, openvpn_user="a-brand-new-username")
    save_step_answers(settings.config_dir, "gluetun", new_answers)

    assert manager.change_vpn() == "started"
    final = await _finish_add(manager)

    assert final.adding is None
    assert trigger.calls == 0


async def test_change_vpn_refuses_when_busy_adding_or_no_vpn_installed(tmp_path: Path) -> None:
    manager, engine, settings = await _deployed_to_finale(tmp_path / "no-vpn", app_ids=("sonarr",))
    assert manager.change_vpn() == "unknown_app"

    manager2, engine2, settings2 = await _protected_qbittorrent_to_finale(tmp_path / "protected")
    engine2._compose_results["radarr"] = ComposeResult(  # type: ignore[attr-defined]
        ok=False, exit_code=1, output="boom, radarr refused to start"
    )
    assert manager2.add_app("radarr") == "started"
    failed = await _finish_add(manager2)
    assert failed.adding is not None
    assert failed.adding.state == "error"
    assert manager2.is_busy() is False  # the failed, unrelated add's own task has finished

    # `adding is not None` (a failed OTHER add still sitting there) is
    # busy too, even though nothing is actively running right now.
    assert manager2.change_vpn() == "busy"


async def test_change_vpn_failure_keeps_qbittorrent_down_and_offers_only_retry(
    tmp_path: Path,
) -> None:
    manager, engine, settings = await _protected_qbittorrent_to_finale(tmp_path)
    engine._compose_results["gluetun"] = ComposeResult(  # type: ignore[attr-defined]
        ok=False, exit_code=1, output="boom, gluetun refused to start"
    )

    assert manager.change_vpn() == "started"
    failed = await _finish_add(manager)
    assert failed.adding is not None
    assert failed.adding.state == "error"
    assert failed.adding.purpose == "change_vpn"

    ok = await manager.cancel_add()
    assert ok is False
    still_failed = manager.snapshot()
    assert still_failed.adding is not None
    assert still_failed.adding.state == "error"


# --- retry and resume re-enter change_vpn and restore ---------------------


async def test_retry_add_re_enters_change_vpn(tmp_path: Path) -> None:
    manager, engine, settings = await _protected_qbittorrent_to_finale(tmp_path)
    engine._compose_results["gluetun"] = ComposeResult(  # type: ignore[attr-defined]
        ok=False, exit_code=1, output="boom, gluetun refused to start"
    )
    assert manager.change_vpn() == "started"
    failed = await _finish_add(manager)
    assert failed.adding is not None
    assert failed.adding.purpose == "change_vpn"

    del engine._compose_results["gluetun"]  # type: ignore[attr-defined]

    result = manager.retry_add()
    assert result == "started"
    final = await _finish_add(manager)
    assert final.adding is None


async def test_resume_if_interrupted_re_enters_restore(tmp_path: Path) -> None:
    manager, engine, settings = await _qbittorrent_without_vpn_to_finale(tmp_path)
    save_step_answers(settings.config_dir, "gluetun", _GLUETUN_ANSWERS)
    engine._logs["gluetun"] = "AUTH: Received control message: AUTH_FAILED, retrying"  # type: ignore[attr-defined]

    assert manager.add_app("gluetun") == "started"
    failed = await _finish_add(manager)
    assert failed.adding is not None
    ok = await manager.cancel_add()
    assert ok is True

    persisted = manager.snapshot()
    assert persisted.adding is not None
    assert persisted.adding.purpose == "restore"

    # A restart mid-restore: a fresh manager, same on-disk state.
    fresh_engine = _StatefulEngine(("sonarr", "qbittorrent"), images=_QBIT_IMAGES)
    fresh_manager = DeployManager(
        settings, fresh_engine, login=FakeLoginApplier(), qbit=_qbit_client()
    )
    resumed_adding = fresh_manager.snapshot().adding
    assert resumed_adding is not None
    assert resumed_adding.purpose == "restore"

    await fresh_manager.resume_if_interrupted()
    final = await _finish_add(fresh_manager)
    assert final.adding is None


async def test_resume_if_interrupted_re_enters_an_add_with_moves(tmp_path: Path) -> None:
    """A restart caught "Add your VPN" before it ever touched Docker -
    `resume_if_interrupted` must dispatch `purpose="add"` with `moves`
    back through `_run_add`, never silently through `_run_reconnect`.
    """
    manager, engine, settings = await _qbittorrent_without_vpn_to_finale(
        tmp_path,
        vpn=FakeGluetunControl(status="running", place=_place()),
        health_frames={"gluetun": ["healthy"]},
    )
    save_step_answers(settings.config_dir, "gluetun", _GLUETUN_ANSWERS)

    current = manager.snapshot()
    interrupted_adding = AppAdd(
        app_id="gluetun",
        purpose="add",
        state="starting",
        line="Starting the VPN",
        note=None,
        failure=None,
        wiring=(),
        compose_ran=False,
        started_at="2026-09-19T00:00:00+00:00",
        with_apps=(),
        moves=("qbittorrent",),
    )
    write_json_atomic(
        settings.config_dir / "deploy.json",
        dataclasses.asdict(dataclasses.replace(current, adding=interrupted_adding)),
    )

    fresh_manager = DeployManager(
        settings,
        engine,
        login=FakeLoginApplier(),
        vpn=FakeGluetunControl(status="running", place=_place()),
        qbit=_qbit_client(),
    )
    resumed_adding = fresh_manager.snapshot().adding
    assert resumed_adding is not None
    assert resumed_adding.purpose == "add"
    assert resumed_adding.moves == ("qbittorrent",)

    await fresh_manager.resume_if_interrupted()
    final = await _finish_add(fresh_manager)
    assert final.adding is None
    assert without_vpn_confirmed(settings.config_dir) is False


async def test_resume_if_interrupted_re_enters_change_vpn(tmp_path: Path) -> None:
    manager, engine, settings = await _protected_qbittorrent_to_finale(tmp_path)
    new_answers = dict(_GLUETUN_ANSWERS, openvpn_user="resumed-username")
    save_step_answers(settings.config_dir, "gluetun", new_answers)

    current = manager.snapshot()
    interrupted_adding = AppAdd(
        app_id="gluetun",
        purpose="change_vpn",
        state="starting",
        line="Starting the VPN",
        note=None,
        failure=None,
        wiring=(),
        compose_ran=True,
        started_at="2026-09-19T00:00:00+00:00",
        with_apps=(),
        moves=("qbittorrent",),
    )
    write_json_atomic(
        settings.config_dir / "deploy.json",
        dataclasses.asdict(dataclasses.replace(current, adding=interrupted_adding)),
    )

    fresh_manager = DeployManager(
        settings,
        engine,
        login=FakeLoginApplier(),
        vpn=FakeGluetunControl(status="running", place=_place()),
        qbit=_qbit_client(),
    )
    resumed_adding = fresh_manager.snapshot().adding
    assert resumed_adding is not None
    assert resumed_adding.purpose == "change_vpn"

    await fresh_manager.resume_if_interrupted()
    final = await _finish_add(fresh_manager)
    assert final.adding is None

    secrets_folder = settings.host_mount / "volume1" / "media" / "marrquee" / "vpn"
    assert (secrets_folder / "openvpn_user").read_text() == "resumed-username"


# --- qBittorrent without a VPN comes up at qbittorrent:8080 ---------------


async def test_qbittorrent_added_without_a_vpn_comes_up_at_qbittorrent_8080(tmp_path: Path) -> None:
    qbit = _qbit_client()
    manager, engine, settings = await _qbittorrent_without_vpn_to_finale(tmp_path, qbit=qbit)

    assert any(call[1] == "http://qbittorrent:8080" for call in qbit.calls)
    assert not any(call[1] == "http://gluetun:8080" for call in qbit.calls)


# --- every build_stack_plan in src goes through _stack_plan ----------------


def test_every_build_stack_plan_in_src_goes_through_stack_plan() -> None:
    source = inspect.getsource(deploy_module)
    lines_calling_it = [
        line for line in source.splitlines() if "build_stack_plan(" in line and "def " not in line
    ]
    assert len(lines_calling_it) == 1, lines_calling_it
    assert lines_calling_it[0].strip().startswith("return build_stack_plan(")
    assert "def _stack_plan(" in source


def test_only_the_login_runs_arr_only_recreate_ever_sets_recreate_true() -> None:
    """`change_vpn`'s whole reason to exist: a login-only change never
    shows up in compose.yaml, so `recreate=True` would leave Gluetun
    running the old login forever - nothing here may ever ask for a
    recreate of a `kind == "vpn"` app. The one remaining call site is the
    login run's own arr-only phase 2.
    """
    source = inspect.getsource(deploy_module)
    call_sites = [
        line
        for line in source.splitlines()
        if "recreate=True" in line and not line.strip().startswith("#")
    ]
    assert len(call_sites) == 1, call_sites
    assert "gluetun" not in "\n".join(call_sites)


# --- the login run in no-VPN mode goes through _stack_plan -----------------


async def test_login_run_in_no_vpn_mode_uses_stack_plan(tmp_path: Path) -> None:
    """Without this, the login run's own recreate phase would call
    `build_stack_plan` with `without_vpn=False` and raise
    `ValueError("qbittorrent needs gluetun")` the moment Sonarr's login
    needed a recreate - Sonarr's new login would silently never take.
    """
    app_ids = ("sonarr", "qbittorrent")
    manager, engine, config_dir = _already_finale_manager(
        tmp_path, app_ids, login_applier=FakeLoginApplier(), qbit=FakeQbitClient({})
    )
    save_without_vpn(config_dir, now=datetime.now(UTC))
    saved = save_login(config_dir, "owner", "s3cret-password-1", honor_reset=None)

    assert manager.apply_login() == "started"
    await _finish_login(manager)

    recreate_calls = [call[1][2] for call in engine.calls if call[0] == "compose_up_recreate"]
    assert recreate_calls == ["sonarr"]
    record = load_login(config_dir)
    assert record.applied.get("sonarr") == saved.generation
    assert "sonarr" not in pending_app_ids(record, app_ids)

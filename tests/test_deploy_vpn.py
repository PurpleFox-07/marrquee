"""Tests for the tunnel proof: `DeployManager` bringing up Gluetun.

Every scenario here drives `_bring_up_app`'s `kind == "vpn"` branch through
a full (or resumed) run - `write_vpn_secrets` happening for real on disk,
the tunnel loop reading a scripted `ContainerHealth`/log/`GluetunControl`
combination, and the resulting `Failure` or success line. Nothing here
waits a real second (`_FakeClock`) or touches a real Docker daemon or
network.
"""

from __future__ import annotations

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
from test_deploy_add import _finish_add

import marrquee.deploy as deploy_module
from marrquee.catalog import get_app
from marrquee.config import Settings
from marrquee.deploy import AppAdd, DeployManager, DeploySnapshot, FakeReadinessProbe
from marrquee.docker_client import (
    ComposeResult,
    ContainerSnapshot,
    DockerStatus,
    FakeDockerEngine,
)
from marrquee.questions import save_step_answers
from marrquee.state import save_state, write_json_atomic
from marrquee.vpn import TunnelPlace
from marrquee.vpn_control import FakeGluetunControl

# --- Shared setup -------------------------------------------------------------


def _prepare_gluetun_state(
    tmp_path: Path, *, answers: dict[str, str] | None = _GLUETUN_ANSWERS
) -> Settings:
    """A fresh, never-deployed-to root with `gluetun` chosen and (unless
    told otherwise) its saved answers already in place.
    """
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    save_state(settings.config_dir, _install_state(("gluetun",), root))
    if answers is not None:
        save_step_answers(settings.config_dir, "gluetun", answers)
    return settings


def _absent(app_id: str) -> ContainerSnapshot:
    return ContainerSnapshot(
        name=app_id, exists=False, state=None, exit_code=None, image=None, detail=None
    )


def _gluetun_snapshot(state: str, health: str | None) -> ContainerSnapshot:
    return ContainerSnapshot(
        name="gluetun",
        exists=True,
        state=state,  # type: ignore[arg-type]
        exit_code=None,
        image=get_app("gluetun").image,
        detail=None,
        health=health,  # type: ignore[arg-type]
    )


def _place() -> TunnelPlace:
    return TunnelPlace(
        public_ip="185.1.1.1", city="Amsterdam", region="North Holland", country="Netherlands"
    )


# --- Success: healthy, running, a reported place ------------------------------


async def test_healthy_running_place_reports_done_and_writes_secrets_before_compose_up(
    tmp_path: Path, monkeypatch: object
) -> None:
    settings = _prepare_gluetun_state(tmp_path)
    engine = _StatefulEngine(("gluetun",), health_frames={"gluetun": ["healthy"]})

    original_write = deploy_module.write_vpn_secrets

    def spy(write_settings: object, write_root: object, files: object) -> None:
        engine.calls.append(("write_vpn_secrets", ()))
        original_write(write_settings, write_root, files)  # type: ignore[arg-type]

    monkeypatch.setattr(deploy_module, "write_vpn_secrets", spy)  # type: ignore[attr-defined]

    clock = _FakeClock()
    manager = DeployManager(
        settings,
        engine,
        probe=FakeReadinessProbe(default=True),
        clock=clock.time,
        sleep=clock.sleep,
        vpn=FakeGluetunControl(status="running", place=_place()),
    )
    manager.start()
    history = await _run_to_terminal(manager)

    final = history[-1]
    assert final.phase == "finale"
    assert (
        final.apps[0].line
        == "Protected - your downloads appear to come from Amsterdam, Netherlands"
    )

    call_names = [call[0] for call in engine.calls]
    assert call_names.index("write_vpn_secrets") < call_names.index("compose_up")

    secrets_folder = settings.host_mount / "volume1" / "media" / "marrquee" / "vpn"
    assert (secrets_folder / "openvpn_user").read_text() == _GLUETUN_ANSWERS["openvpn_user"]


# --- AUTH_FAILED wins immediately, without waiting -----------------------------


async def test_auth_failed_in_logs_fails_immediately_as_vpn_refused(tmp_path: Path) -> None:
    settings = _prepare_gluetun_state(tmp_path)
    engine = FakeDockerEngine(
        DockerStatus(connected=True),
        frames={"gluetun": [_absent("gluetun"), _gluetun_snapshot("running", "starting")]},
        logs={"gluetun": "AUTH: Received control message: AUTH_FAILED, retrying"},
        images={get_app("gluetun").image},
        self_container_id="marrquee",
    )
    clock = _FakeClock()
    manager = DeployManager(
        settings,
        engine,
        probe=FakeReadinessProbe(default=True),
        clock=clock.time,
        sleep=clock.sleep,
    )
    manager.start()
    history = await _run_to_terminal(manager)

    final = history[-1]
    assert final.phase == "error"
    assert final.failure is not None
    assert final.failure.code == "vpn_refused"
    assert "Mullvad" in final.failure.headline
    # Never slept a single tick - the very first inspect/log pair already
    # carried the AUTH_FAILED marker.
    assert clock.now == 0.0


# --- The container vanishes mid-tunnel-loop (removed from outside) -----------


async def test_container_gone_mid_loop_becomes_a_plain_compose_failure(tmp_path: Path) -> None:
    settings = _prepare_gluetun_state(tmp_path)
    engine = FakeDockerEngine(
        DockerStatus(connected=True),
        frames={
            "gluetun": [
                _absent("gluetun"),  # the name-clash check
                _gluetun_snapshot("running", "starting"),  # first tunnel tick: connecting
                _absent("gluetun"),  # then it's simply gone (repeats forever)
            ]
        },
        logs={"gluetun": ""},
        images={get_app("gluetun").image},
        self_container_id="marrquee",
    )
    clock = _FakeClock()
    manager = DeployManager(
        settings,
        engine,
        probe=FakeReadinessProbe(default=True),
        clock=clock.time,
        sleep=clock.sleep,
    )
    manager.start()
    history = await _run_to_terminal(manager)

    final = history[-1]
    assert final.phase == "error"
    assert final.failure is not None
    assert final.failure.code == "compose_failed"


# --- A container Gluetun itself keeps restarting -------------------------------


async def test_restarting_fails_as_vpn_settings_refused_with_redacted_logs(tmp_path: Path) -> None:
    settings = _prepare_gluetun_state(tmp_path)
    password = _GLUETUN_ANSWERS["openvpn_password"]
    engine = FakeDockerEngine(
        DockerStatus(connected=True),
        frames={"gluetun": [_absent("gluetun"), _gluetun_snapshot("restarting", None)]},
        logs={"gluetun": f"gluetun exited: invalid setting (login was {password})"},
        images={get_app("gluetun").image},
        self_container_id="marrquee",
    )
    clock = _FakeClock()
    manager = DeployManager(
        settings,
        engine,
        probe=FakeReadinessProbe(default=True),
        clock=clock.time,
        sleep=clock.sleep,
    )
    manager.start()
    history = await _run_to_terminal(manager)

    final = history[-1]
    assert final.phase == "error"
    assert final.failure is not None
    assert final.failure.code == "vpn_settings_refused"
    assert password not in final.failure.technical
    assert "<redacted-vpn-login>" in final.failure.technical

    diagnostics = (settings.config_dir / "last-failure.txt").read_text()
    assert password not in diagnostics


# --- Healthy per Docker, but Gluetun's own status never says "running" --------


async def test_healthy_but_control_server_says_stopped_keeps_waiting_then_times_out(
    tmp_path: Path,
) -> None:
    settings = _prepare_gluetun_state(tmp_path)
    engine = _StatefulEngine(("gluetun",), health_frames={"gluetun": ["healthy"]})
    clock = _FakeClock()
    manager = DeployManager(
        settings,
        engine,
        probe=FakeReadinessProbe(default=True),
        clock=clock.time,
        sleep=clock.sleep,
        vpn=FakeGluetunControl(status="stopped"),
    )
    manager.start()
    history = await _run_to_terminal(manager)

    final = history[-1]
    assert final.phase == "error"
    assert final.failure is not None
    assert final.failure.code == "vpn_not_connected"
    # Times out at (not merely eventually after) the documented 120s: within
    # one poll tick of the boundary, never a much later or earlier value.
    assert (
        DeployManager.TUNNEL_NEVER_UP_AFTER_SECONDS
        <= clock.now
        < (DeployManager.TUNNEL_NEVER_UP_AFTER_SECONDS + DeployManager.POLL_INTERVAL_SECONDS)
    )


# --- Healthy, running, no place for the grace period --------------------------


async def test_healthy_running_no_place_for_the_grace_period_succeeds_with_plain_line(
    tmp_path: Path,
) -> None:
    settings = _prepare_gluetun_state(tmp_path)
    engine = _StatefulEngine(("gluetun",), health_frames={"gluetun": ["healthy"]})
    clock = _FakeClock()
    manager = DeployManager(
        settings,
        engine,
        probe=FakeReadinessProbe(default=True),
        clock=clock.time,
        sleep=clock.sleep,
        vpn=FakeGluetunControl(status="running", place=None),
    )
    manager.start()
    history = await _run_to_terminal(manager)

    final = history[-1]
    assert final.phase == "finale"
    assert final.apps[0].line == "Protected - your downloads go through your VPN."
    assert clock.now < DeployManager.TUNNEL_NEVER_UP_AFTER_SECONDS


# --- Compose itself fails because there's no tunnel device --------------------


async def test_compose_output_naming_dev_net_tun_becomes_vpn_no_tun(tmp_path: Path) -> None:
    settings = _prepare_gluetun_state(tmp_path)
    engine = _StatefulEngine(
        ("gluetun",),
        compose_results={
            "gluetun": ComposeResult(
                ok=False,
                exit_code=1,
                output=(
                    "error gathering device information while adding custom device "
                    '"/dev/net/tun": no such file or directory'
                ),
            )
        },
    )
    clock = _FakeClock()
    manager = DeployManager(
        settings,
        engine,
        probe=FakeReadinessProbe(default=True),
        clock=clock.time,
        sleep=clock.sleep,
    )
    manager.start()
    history = await _run_to_terminal(manager)

    final = history[-1]
    assert final.phase == "error"
    assert final.failure is not None
    assert final.failure.code == "vpn_no_tun"


# --- Missing answers refuse before compose is ever written --------------------


async def test_missing_vpn_answers_refuse_before_compose_is_written(tmp_path: Path) -> None:
    settings = _prepare_gluetun_state(tmp_path, answers=None)
    engine = _StatefulEngine(("gluetun",))
    clock = _FakeClock()
    manager = DeployManager(
        settings,
        engine,
        probe=FakeReadinessProbe(default=True),
        clock=clock.time,
        sleep=clock.sleep,
    )
    manager.start()
    history = await _run_to_terminal(manager)

    final = history[-1]
    assert final.phase == "error"
    assert final.failure is not None
    assert final.failure.code == "vpn_settings_refused"
    assert not any(call[0] in ("compose_up", "compose_up_recreate") for call in engine.calls)

    compose_path = settings.host_mount / "volume1" / "media" / "marrquee" / "compose.yaml"
    assert not compose_path.exists()


# --- No secret anywhere a failure can be read back -----------------------------


async def test_no_secret_reaches_diagnostics_deploy_json_or_the_snapshot(tmp_path: Path) -> None:
    settings = _prepare_gluetun_state(tmp_path)
    password = _GLUETUN_ANSWERS["openvpn_password"]
    user = _GLUETUN_ANSWERS["openvpn_user"]
    engine = FakeDockerEngine(
        DockerStatus(connected=True),
        frames={"gluetun": [_absent("gluetun"), _gluetun_snapshot("running", "starting")]},
        logs={"gluetun": f"AUTH_FAILED (user={user}, password={password})"},
        images={get_app("gluetun").image},
        self_container_id="marrquee",
    )
    clock = _FakeClock()
    manager = DeployManager(
        settings,
        engine,
        probe=FakeReadinessProbe(default=True),
        clock=clock.time,
        sleep=clock.sleep,
    )
    manager.start()
    history = await _run_to_terminal(manager)

    final = history[-1]
    assert final.failure is not None
    assert password not in final.failure.technical
    assert user not in final.failure.technical

    diagnostics = (settings.config_dir / "last-failure.txt").read_text()
    assert password not in diagnostics
    assert user not in diagnostics

    deploy_json = (settings.config_dir / "deploy.json").read_text()
    assert password not in deploy_json
    assert user not in deploy_json


# --- Restart safety: a persisted vpn_refused failure loads back ---------------


async def test_a_persisted_vpn_refused_failure_survives_a_restart(tmp_path: Path) -> None:
    settings = _prepare_gluetun_state(tmp_path)
    engine = FakeDockerEngine(
        DockerStatus(connected=True),
        frames={"gluetun": [_absent("gluetun"), _gluetun_snapshot("running", "starting")]},
        logs={"gluetun": "AUTH: Received control message: AUTH_FAILED"},
        images={get_app("gluetun").image},
        self_container_id="marrquee",
    )
    clock = _FakeClock()
    manager = DeployManager(
        settings,
        engine,
        probe=FakeReadinessProbe(default=True),
        clock=clock.time,
        sleep=clock.sleep,
    )
    manager.start()
    history = await _run_to_terminal(manager)
    assert history[-1].failure is not None
    assert history[-1].failure.code == "vpn_refused"

    # A fresh `DeployManager`, same settings, same config directory - the
    # "Marrquee restarted" case. It must read the persisted failure back
    # rather than treating an unrecognised code as nothing having run.
    second_manager = DeployManager(settings, FakeDockerEngine(DockerStatus(connected=True)))
    loaded = second_manager.snapshot()
    assert loaded.phase == "error"
    assert loaded.failure is not None
    assert loaded.failure.code == "vpn_refused"


# --- A resumed add re-proves the tunnel, never trusts a stale success ----------


async def test_a_resumed_gluetun_add_re_proves_the_tunnel(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    save_state(settings.config_dir, _install_state(("gluetun",), root))
    save_step_answers(settings.config_dir, "gluetun", _GLUETUN_ANSWERS)

    finale = DeploySnapshot(
        run_id="run-before-a-restart",
        phase="finale",
        apps=(),
        headline="Everything's ready.",
        detail=None,
        failure=None,
        started_at="2026-09-19T00:00:00+00:00",
        finished_at="2026-09-19T00:05:00+00:00",
        wiring=(),
        adding=AppAdd(
            app_id="gluetun",
            purpose="add",
            state="starting",
            line="Starting VPN",
            note=None,
            failure=None,
            wiring=(),
            compose_ran=False,
            started_at="2026-09-19T00:06:00+00:00",
        ),
        wiring_gaps=(),
    )
    write_json_atomic(settings.config_dir / "deploy.json", dataclasses.asdict(finale))

    # Nothing was ever actually created before the (simulated) restart -
    # `compose_ran=False` above matches a genuinely fresh engine here.
    engine = _StatefulEngine(("gluetun",), health_frames={"gluetun": ["healthy"]})
    clock = _FakeClock()
    manager = DeployManager(
        settings,
        engine,
        probe=FakeReadinessProbe(default=True),
        clock=clock.time,
        sleep=clock.sleep,
        vpn=FakeGluetunControl(status="running", place=_place()),
    )

    assert manager.snapshot().phase == "finale"
    assert manager.snapshot().adding is not None

    await manager.resume_if_interrupted()
    final = await _finish_add(manager)

    assert final.adding is None
    assert [app.app_id for app in final.apps] == ["gluetun"]
    assert final.apps[-1].state == "done"
    # A resumed add re-runs `_bring_up_app` from scratch (write_vpn_secrets,
    # compose_up, the tunnel loop) rather than trusting anything the
    # interrupted attempt claimed - proven by `_StatefulEngine` actually
    # having created the container fresh in THIS run.
    assert any(call[0] == "compose_up" and call[1][2] == "gluetun" for call in engine.calls)


# --- Cancel of a failed gluetun add also clears the secrets folder ------------


async def test_cancel_of_a_failed_gluetun_add_removes_container_and_clears_secrets(
    tmp_path: Path,
) -> None:
    from test_deploy_add import _deployed_to_finale

    manager, engine, settings = await _deployed_to_finale(
        tmp_path, images={get_app(app_id).image for app_id in ("prowlarr", "sonarr", "gluetun")}
    )
    save_step_answers(settings.config_dir, "gluetun", _GLUETUN_ANSWERS)
    engine._compose_results["gluetun"] = ComposeResult(  # type: ignore[attr-defined]
        ok=False,
        exit_code=1,
        output=(
            "error gathering device information while adding custom device "
            '"/dev/net/tun": no such file or directory'
        ),
    )

    result = manager.add_app("gluetun")
    assert result == "started"
    failed = await _finish_add(manager)
    assert failed.adding is not None
    assert failed.adding.state == "error"
    assert failed.adding.failure is not None
    assert failed.adding.failure.code == "vpn_no_tun"

    secrets_folder = settings.host_mount / "volume1" / "media" / "marrquee" / "vpn"
    assert any(entry.is_file() for entry in secrets_folder.iterdir())

    ok = await manager.cancel_add()
    assert ok is True
    assert any(call[0] == "remove_container" and call[1] == ("gluetun",) for call in engine.calls)
    assert not any(entry.is_file() for entry in secrets_folder.iterdir())

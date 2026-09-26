"""Tests for qBittorrent's own place in the deploy engine: bringing it up
behind a proven tunnel, its own readiness check (the key, not the arr
probe), the port-conflict wording naming it as Gluetun's rider, cancelling
a failed add, and the login run never recreating it.

Every scenario here builds on `test_deploy`'s own fixtures
(`_GLUETUN_ANSWERS`, `_FakeClock`, `_fresh_root`, `_install_state`,
`_run_to_terminal`, `_settings`, `_StatefulEngine`) and `test_deploy_add`'s
`_deployed_to_finale`/`_finish_add` - nothing here touches a real Docker
daemon, a real network or waits a real second.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_deploy import (
    _GLUETUN_ANSWERS,
    _fresh_root,
    _install_state,
    _run_to_terminal,
    _settings,
    _StatefulEngine,
)
from test_deploy_add import _deployed_to_finale, _finish_add

import marrquee.deploy as deploy_module
from marrquee.catalog import get_app
from marrquee.deploy import DeployManager, FakeReadinessProbe
from marrquee.docker_client import ComposeResult
from marrquee.questions import save_step_answers
from marrquee.state import save_state
from marrquee.vpn import TunnelPlace
from marrquee.vpn_control import FakeGluetunControl
from marrquee.wiring.qbit_client import FakeQbitClient, QbitResponse

_QBIT_IMAGES = {
    get_app(app_id).image for app_id in ("prowlarr", "sonarr", "radarr", "gluetun", "qbittorrent")
}


def _place() -> TunnelPlace:
    return TunnelPlace(
        public_ip="185.1.1.1", city="Amsterdam", region="North Holland", country="Netherlands"
    )


def _ok_version() -> QbitResponse:
    return QbitResponse(ok=True, status=200, payload={"version": "5.2.3"}, detail=None)


def _forbidden_version() -> QbitResponse:
    return QbitResponse(ok=False, status=403, payload=None, detail="Forbidden")


# --- FIRST TEST: a refused tunnel never lets qBittorrent's compose_up run ----


async def test_add_of_qbittorrent_stops_before_qbittorrent_when_gluetun_is_refused(
    tmp_path: Path,
) -> None:
    manager, engine, settings = await _deployed_to_finale(
        tmp_path, app_ids=("prowlarr", "sonarr"), images=_QBIT_IMAGES
    )
    save_step_answers(settings.config_dir, "gluetun", _GLUETUN_ANSWERS)
    engine._logs["gluetun"] = "AUTH: Received control message: AUTH_FAILED, retrying"  # type: ignore[attr-defined]

    result = manager.add_app("qbittorrent")
    assert result == "started"
    failed = await _finish_add(manager)

    assert failed.adding is not None
    assert failed.adding.state == "error"
    assert failed.adding.failure is not None
    assert failed.adding.failure.code == "vpn_refused"
    assert not any(call[0] == "compose_up" and call[1][2] == "qbittorrent" for call in engine.calls)


# --- The conf is written before compose_up, behind a proven tunnel ----------


async def test_tunnel_proven_then_qbittorrent_conf_written_before_compose_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qbit = FakeQbitClient({("GET", "http://gluetun:8080", "api/v2/app/version"): [_ok_version()]})
    manager, engine, settings = await _deployed_to_finale(
        tmp_path,
        app_ids=("prowlarr", "sonarr"),
        images=_QBIT_IMAGES,
        health_frames={"gluetun": ["healthy"]},
        vpn=FakeGluetunControl(status="running", place=_place()),
        qbit=qbit,
    )
    save_step_answers(settings.config_dir, "gluetun", _GLUETUN_ANSWERS)

    original_write_conf = deploy_module.write_qbit_conf

    def spy(
        write_settings: object, write_root: object, api_key: object, puid: object, pgid: object
    ) -> bool:
        engine.calls.append(("write_qbit_conf", ()))
        return original_write_conf(write_settings, write_root, api_key, puid, pgid)  # type: ignore[arg-type]

    monkeypatch.setattr(deploy_module, "write_qbit_conf", spy)

    result = manager.add_app("qbittorrent")
    assert result == "started"
    final = await _finish_add(manager)

    assert final.adding is None
    assert [progress.app_id for progress in final.apps] == [
        "prowlarr",
        "sonarr",
        "gluetun",
        "qbittorrent",
    ]

    write_index = next(i for i, call in enumerate(engine.calls) if call[0] == "write_qbit_conf")
    compose_index = next(
        i
        for i, call in enumerate(engine.calls)
        if call[0] == "compose_up" and call[1][2] == "qbittorrent"
    )
    assert write_index < compose_index

    # Belt-and-suspenders: the file genuinely landed on disk with a key in it.
    secrets_conf = (
        settings.host_mount
        / "volume1"
        / "media"
        / "marrquee"
        / "apps"
        / "qbittorrent"
        / "qBittorrent"
        / "qBittorrent.conf"
    )
    assert "WebUI\\APIKey=qbt_" in secrets_conf.read_text()


# --- Readiness only through the key, never the arr probe --------------------


async def test_qbittorrent_is_ready_only_once_the_key_call_answers_200(tmp_path: Path) -> None:
    qbit = FakeQbitClient(
        {
            ("GET", "http://gluetun:8080", "api/v2/app/version"): [
                _forbidden_version(),
                _forbidden_version(),
                _ok_version(),
            ]
        }
    )
    manager, engine, settings = await _deployed_to_finale(
        tmp_path,
        app_ids=("prowlarr", "sonarr"),
        images=_QBIT_IMAGES,
        health_frames={"gluetun": ["healthy"]},
        vpn=FakeGluetunControl(status="running", place=_place()),
        qbit=qbit,
    )
    save_step_answers(settings.config_dir, "gluetun", _GLUETUN_ANSWERS)

    result = manager.add_app("qbittorrent")
    assert result == "started"
    final = await _finish_add(manager)

    assert final.adding is None
    assert "qbittorrent" in [progress.app_id for progress in final.apps]
    assert len(qbit.calls) == 3


# --- Cancel removes qbittorrent then gluetun, and clears the secrets --------


async def test_cancel_removes_qbittorrent_then_gluetun_and_clears_secrets(tmp_path: Path) -> None:
    manager, engine, settings = await _deployed_to_finale(
        tmp_path,
        app_ids=("prowlarr", "sonarr"),
        images=_QBIT_IMAGES,
        health_frames={"gluetun": ["healthy"]},
        vpn=FakeGluetunControl(status="running", place=_place()),
    )
    save_step_answers(settings.config_dir, "gluetun", _GLUETUN_ANSWERS)
    engine._compose_results["qbittorrent"] = ComposeResult(  # type: ignore[attr-defined]
        ok=False, exit_code=1, output="boom, qbittorrent refused to start"
    )

    result = manager.add_app("qbittorrent")
    assert result == "started"
    failed = await _finish_add(manager)
    assert failed.adding is not None
    assert failed.adding.state == "error"

    secrets_folder = settings.host_mount / "volume1" / "media" / "marrquee" / "vpn"
    assert any(entry.is_file() for entry in secrets_folder.iterdir())

    ok = await manager.cancel_add()
    assert ok is True

    remove_calls = [call[1][0] for call in engine.calls if call[0] == "remove_container"]
    assert remove_calls == ["qbittorrent", "gluetun"]
    assert not any(entry.is_file() for entry in secrets_folder.iterdir())


# --- A port conflict on Gluetun names qBittorrent, its rider ----------------


async def test_adding_qbittorrent_behind_an_already_installed_gluetun_recreates_it(
    tmp_path: Path,
) -> None:
    """CONTRACT FIX: when Gluetun is already installed (a separate,
    earlier add), adding qBittorrent must remove-then-bring-up Gluetun
    again (rewriting its secrets with the downloader key/script,
    re-publishing 8080, re-proving the tunnel) - never `recreate=True`,
    which would leave its unchanged compose service, and the network
    namespace qBittorrent needs to join, untouched.
    """
    save_step_answers(_settings(tmp_path).config_dir, "gluetun", _GLUETUN_ANSWERS)
    qbit = FakeQbitClient({("GET", "http://gluetun:8080", "api/v2/app/version"): [_ok_version()]})
    manager, engine, settings = await _deployed_to_finale(
        tmp_path,
        app_ids=("prowlarr", "sonarr", "gluetun"),
        images=_QBIT_IMAGES,
        health_frames={"gluetun": ["healthy"]},
        vpn=FakeGluetunControl(status="running", place=_place()),
        qbit=qbit,
    )

    result = manager.add_app("qbittorrent")
    assert result == "started"
    final = await _finish_add(manager)

    assert final.adding is None
    assert [progress.app_id for progress in final.apps] == [
        "prowlarr",
        "sonarr",
        "gluetun",
        "qbittorrent",
    ]

    gluetun_calls = [call[0] for call in engine.calls if call[1] and call[1][-1] == "gluetun"]
    assert "remove_container" in [call[0] for call in engine.calls if call[0] == "remove_container"]
    # Never a recreate for the VPN app - a fresh, plain compose_up instead.
    assert "compose_up_recreate" not in gluetun_calls
    remove_index = next(
        i for i, call in enumerate(engine.calls) if call == ("remove_container", ("gluetun",))
    )
    second_gluetun_up = [
        i
        for i, call in enumerate(engine.calls)
        if call[0] == "compose_up" and call[1][2] == "gluetun"
    ]
    assert any(i > remove_index for i in second_gluetun_up)


async def test_cancel_after_recreating_an_already_installed_gluetun_never_touches_it(
    tmp_path: Path,
) -> None:
    """Gluetun pre-existed this add (the CONTRACT FIX path above) - a
    failed qBittorrent-only add's own Cancel must remove only qBittorrent,
    never Gluetun and never its secrets, since Gluetun is a separate,
    already-working install this add never "brought along".
    """
    save_step_answers(_settings(tmp_path).config_dir, "gluetun", _GLUETUN_ANSWERS)
    manager, engine, settings = await _deployed_to_finale(
        tmp_path,
        app_ids=("prowlarr", "sonarr", "gluetun"),
        images=_QBIT_IMAGES,
        health_frames={"gluetun": ["healthy"]},
        vpn=FakeGluetunControl(status="running", place=_place()),
    )
    engine._compose_results["qbittorrent"] = ComposeResult(  # type: ignore[attr-defined]
        ok=False, exit_code=1, output="boom, qbittorrent refused to start"
    )

    result = manager.add_app("qbittorrent")
    assert result == "started"
    failed = await _finish_add(manager)
    assert failed.adding is not None
    assert failed.adding.state == "error"

    secrets_folder = settings.host_mount / "volume1" / "media" / "marrquee" / "vpn"
    secrets_before = {entry.name for entry in secrets_folder.iterdir() if entry.is_file()}
    calls_before_cancel = len(engine.calls)

    ok = await manager.cancel_add()
    assert ok is True

    # Only calls Cancel itself made - the add's OWN remove-then-recreate of
    # an already-installed Gluetun (the CONTRACT FIX path) already put a
    # `remove_container("gluetun")` earlier in this same list, which is not
    # what this assertion is about.
    cancel_calls = engine.calls[calls_before_cancel:]
    remove_calls = [call[1][0] for call in cancel_calls if call[0] == "remove_container"]
    assert remove_calls == ["qbittorrent"]
    # Gluetun's own secrets are untouched - still exactly what they were.
    secrets_after = {entry.name for entry in secrets_folder.iterdir() if entry.is_file()}
    assert secrets_after == secrets_before


async def test_port_conflict_on_gluetun_names_qbittorrent_and_its_port(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    save_state(settings.config_dir, _install_state(("gluetun", "qbittorrent"), root))
    save_step_answers(settings.config_dir, "gluetun", _GLUETUN_ANSWERS)

    engine = _StatefulEngine(
        ("gluetun", "qbittorrent"),
        images=_QBIT_IMAGES,
        compose_results={
            "gluetun": ComposeResult(
                ok=False,
                exit_code=1,
                output="Bind for 0.0.0.0:8080 failed: port is already allocated",
            )
        },
    )
    manager = DeployManager(settings, engine, probe=FakeReadinessProbe(default=True))
    manager.start()
    history = await _run_to_terminal(manager)

    final = history[-1]
    assert final.phase == "error"
    assert final.failure is not None
    assert final.failure.code == "port_in_use"
    assert "qBittorrent" in final.failure.headline
    assert "8080" in final.failure.headline

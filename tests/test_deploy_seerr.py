"""Tests for the deploy engine's own Seerr bring-up: `_bring_up_seerr`,
reached through `DeployManager.add_app`/`cancel_add`, and its dispatch out
of `_bring_up_app`.

Nothing here waits a real second or touches a real Docker daemon or a real
Seerr: `FakeDockerEngine`'s own `frames=` scripts exactly what each
`inspect("seerr")` call sees, `FakeSeerrClient` scripts Seerr's own API, and
`_FakeClock` (imported from `test_deploy`) advances only when the loop
itself sleeps.

Every scenario here starts from a persisted `finale` snapshot for the
media-server app(s) already in place - never a real run of their own
bring-up - because Seerr's config folder is owned by a fixed uid rather
than the drive's own puid:pgid, and a real `build_folders` call in a
non-root test process can't chown a folder to a uid it doesn't own.
`_stub_build_folders_chown` (below) patches `deploy.py`'s own
`build_folders` reference, for the length of one test, to a wrapper that
never calls the real `os.chown` - production keeps the real default
untouched.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
from pathlib import Path, PurePosixPath

import pytest
from test_deploy import (
    _FakeClock,
    _fresh_root,
    _install_state,
    _run_to_terminal,
    _running_container,
    _settings,
)
from test_deploy_add import _finish_add

from marrquee import deploy
from marrquee.catalog import get_app
from marrquee.config import Settings
from marrquee.deploy import (
    AppProgress,
    DeployManager,
    DeploySnapshot,
    FakeReadinessProbe,
)
from marrquee.docker_client import ContainerSnapshot, DockerStatus, FakeDockerEngine
from marrquee.jellyfin import FakeJellyfinServer, JellyfinResponse, save_jellyfin
from marrquee.login import record_applied, save_login
from marrquee.login_apply import FakeLoginApplier
from marrquee.plex import load_plex_account, save_plex_sign_in
from marrquee.seerr import FakeSeerrClient, SeerrResponse
from marrquee.state import load_state, save_state, write_json_atomic
from marrquee.storage import write_marker
from marrquee.words import PHASE_HEADLINE_FINALE, STATUS_CHIP_DONE, app_line_done

_LOGIN_USERNAME = "owner"
_LOGIN_PASSWORD = "s3cret-password-1"
_SEERR_APP = get_app("seerr")


@pytest.fixture(autouse=True)
def _stub_build_folders_chown(monkeypatch: pytest.MonkeyPatch) -> None:
    real_build_folders = deploy.build_folders

    def _without_chowning_for_real(*args: object, **kwargs: object) -> object:
        kwargs["chown"] = lambda *_a: None
        return real_build_folders(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(deploy, "build_folders", _without_chowning_for_real)


def _absent(name: str) -> ContainerSnapshot:
    return ContainerSnapshot(
        name=name, exists=False, state=None, exit_code=None, image=None, detail=None
    )


def _done_progress(app_id: str) -> AppProgress:
    app = get_app(app_id)
    return AppProgress(
        app_id=app.id,
        name=app.name,
        state="done",
        chip=STATUS_CHIP_DONE,
        line=app_line_done(app.name),
        note=None,
        port=app.port,
    )


def _write_finale_snapshot(settings: Settings, app_ids: tuple[str, ...]) -> None:
    write_json_atomic(
        settings.config_dir / "deploy.json",
        dataclasses.asdict(
            DeploySnapshot(
                run_id="run-1",
                phase="finale",
                apps=tuple(_done_progress(app_id) for app_id in app_ids),
                headline=PHASE_HEADLINE_FINALE,
                detail=None,
                failure=None,
                started_at="2026-09-27T00:00:00+00:00",
                finished_at="2026-09-27T00:05:00+00:00",
            )
        ),
    )


def _seerr_manager(
    tmp_path: Path,
    base_app_ids: tuple[str, ...],
    *,
    seerr: FakeSeerrClient | None = None,
    host_gateway: str | None = "172.20.0.1",
    host_gateways: dict[str, str] | None = None,
    seerr_frames: list[ContainerSnapshot] | None = None,
    with_login: bool = True,
    login_applied: bool = True,
    extra_api_keys: dict[str, str] | None = None,
) -> tuple[DeployManager, FakeDockerEngine, Settings, PurePosixPath]:
    """A manager already at `finale` for `base_app_ids`, ready for
    `add_app("seerr")` - built from a persisted snapshot, never a real run.

    `login_applied=False` saves the one login but never records it as
    applied to jellyfin - the "the one login is on disk, but jellyfin
    hasn't received it yet" case, distinct from `with_login=False` ("there
    is no login at all").
    """
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(base_app_ids, root)
    if extra_api_keys:
        install = dataclasses.replace(install, api_keys={**install.api_keys, **extra_api_keys})
    save_state(settings.config_dir, install)
    if with_login:
        saved = save_login(settings.config_dir, _LOGIN_USERNAME, _LOGIN_PASSWORD, honor_reset=None)
        if "jellyfin" in base_app_ids and login_applied:
            record_applied(settings.config_dir, "jellyfin", saved.generation)
    _write_finale_snapshot(settings, base_app_ids)

    images = {get_app(app_id).image for app_id in (*base_app_ids, "seerr")}
    frames = {"seerr": seerr_frames or [_absent("seerr"), _running_container("seerr")]}
    engine = FakeDockerEngine(
        DockerStatus(connected=True),
        images=images,
        frames=frames,
        host_gateway=host_gateway,
        host_gateways=host_gateways,
        self_container_id="marrquee",
    )
    clock = _FakeClock()
    manager = DeployManager(
        settings, engine, clock=clock.time, sleep=clock.sleep, login=FakeLoginApplier()
    )
    manager._seerr = seerr if seerr is not None else FakeSeerrClient({})  # type: ignore[attr-defined]
    return manager, engine, settings, root


def _jellyfin_first_run_script(
    *, extra_detail: str | None = None
) -> dict[tuple[str, str], list[SeerrResponse]]:
    return {
        ("GET", "api/v1/settings/public"): [
            SeerrResponse(
                ok=True,
                status=200,
                payload={"mediaServerType": 4, "initialized": False},
                detail=None,
            )
        ],
        ("POST", "api/v1/auth/jellyfin"): [
            SeerrResponse(ok=True, status=200, payload={}, detail=None)
        ],
        ("GET", "api/v1/settings/main"): [
            SeerrResponse(ok=True, status=200, payload={}, detail=None)
        ],
        ("POST", "api/v1/settings/main"): [
            SeerrResponse(ok=True, status=200, payload={}, detail=None)
        ],
        ("POST", "api/v1/settings/initialize"): [
            SeerrResponse(ok=True, status=200, payload={"initialized": True}, detail=None)
        ],
    }


# --- FIRST TEST: a fresh Seerr add reaches done with Jellyfin's sign-in ------


async def test_add_seerr_with_jellyfin_reaches_done(tmp_path: Path) -> None:
    seerr = FakeSeerrClient(_jellyfin_first_run_script())
    manager, engine, settings, root = _seerr_manager(
        tmp_path, ("sonarr", "radarr", "jellyfin"), seerr=seerr
    )

    manager.add_app("seerr")
    final = await _finish_add(manager)

    assert final.adding is None
    assert any(app.app_id == "seerr" and app.state == "done" for app in final.apps)
    assert any(call[0] == "compose_up" and call[1][2] == "seerr" for call in engine.calls)
    assert any(call[0] == "connect_network" and call[1][1] == "marrquee" for call in engine.calls)
    auth_bodies = [
        body
        for (method, path, _, _), body in zip(seerr.calls, seerr.bodies, strict=True)
        if path == "api/v1/auth/jellyfin"
    ]
    assert len(auth_bodies) == 1
    assert isinstance(auth_bodies[0], dict)
    assert auth_bodies[0]["username"] == _LOGIN_USERNAME


# --- the gateway looked up (and signed in with) is Seerr's own, never
# Marrquee's --------------------------------------------------------------


async def test_gateway_lookup_and_sign_in_use_seerrs_own_container_not_marrquees(
    tmp_path: Path,
) -> None:
    """Marrquee's own gateway can be `127.0.0.1` on a real NAS - an address
    useless from inside a different container. The sign-in Seerr's own
    Jellyfin auth call carries must come from Seerr's own gateway lookup,
    never Marrquee's.
    """
    seerr = FakeSeerrClient(_jellyfin_first_run_script())
    manager, engine, settings, root = _seerr_manager(
        tmp_path,
        ("sonarr", "radarr", "jellyfin"),
        seerr=seerr,
        host_gateway="127.0.0.1",  # Marrquee's own gateway - never Seerr's to use
        host_gateways={"seerr": "172.30.0.7"},
    )

    manager.add_app("seerr")
    final = await _finish_add(manager)

    assert final.adding is None
    assert any(app.app_id == "seerr" and app.state == "done" for app in final.apps)
    assert ("host_gateway", ("seerr",)) in engine.calls
    auth_bodies = [
        body
        for (method, path, _, _), body in zip(seerr.calls, seerr.bodies, strict=True)
        if path == "api/v1/auth/jellyfin"
    ]
    assert len(auth_bodies) == 1
    assert isinstance(auth_bodies[0], dict)
    assert auth_bodies[0]["hostname"] == "172.30.0.7"


# --- Plex's own sign-in carries plex.json's token ----------------------------


async def test_add_seerr_with_plex_uses_plex_jsons_token(tmp_path: Path) -> None:
    # `with_login=True` (the default): `add_app` refuses ANY add without the
    # one login saved, regardless of which media server Seerr itself signs
    # in through - this is not something Seerr's own sign-in resolution
    # decides.
    manager, engine, settings, root = _seerr_manager(tmp_path, ("sonarr", "plex"))
    save_plex_sign_in(settings.config_dir, "plex-tv-token-abc", "owner")
    account = load_plex_account(settings.config_dir)
    assert account is not None and account.token == "plex-tv-token-abc"
    seerr = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/public"): [
                SeerrResponse(
                    ok=True,
                    status=200,
                    payload={"mediaServerType": 4, "initialized": False},
                    detail=None,
                )
            ],
            ("POST", "api/v1/auth/plex"): [
                SeerrResponse(ok=True, status=200, payload={}, detail=None)
            ],
            ("GET", "api/v1/settings/main"): [
                SeerrResponse(ok=True, status=200, payload={}, detail=None)
            ],
            ("POST", "api/v1/settings/main"): [
                SeerrResponse(ok=True, status=200, payload={}, detail=None)
            ],
            ("POST", "api/v1/settings/initialize"): [
                SeerrResponse(ok=True, status=200, payload={"initialized": True}, detail=None)
            ],
        }
    )
    manager._seerr = seerr  # type: ignore[attr-defined]

    manager.add_app("seerr")
    final = await _finish_add(manager)

    assert any(app.app_id == "seerr" and app.state == "done" for app in final.apps)
    auth_bodies = [
        body
        for (method, path, _, _), body in zip(seerr.calls, seerr.bodies, strict=True)
        if path == "api/v1/auth/plex"
    ]
    assert len(auth_bodies) == 1
    assert isinstance(auth_bodies[0], dict)
    assert auth_bodies[0]["authToken"] == "plex-tv-token-abc"


# --- no sign-in on disk refuses before compose ever runs ---------------------


async def test_no_sign_in_saved_refuses_before_compose(tmp_path: Path) -> None:
    # The one login IS saved (`add_app` itself refuses any add without it,
    # regardless of Seerr), but nothing ever signed in to Plex - Seerr's own
    # sign-in resolution has nothing usable yet.
    manager, engine, settings, root = _seerr_manager(tmp_path, ("sonarr", "plex"))

    manager.add_app("seerr")
    final = await _finish_add(manager)

    assert final.adding is not None
    assert final.adding.state == "error"
    assert final.adding.failure is not None
    assert final.adding.failure.code == "seerr_setup_refused"
    assert not any(call[0].startswith("compose_up") for call in engine.calls)


async def test_login_not_yet_applied_to_jellyfin_refuses_before_compose(tmp_path: Path) -> None:
    # The one login is on disk (so `seerr_sign_in_kind` finds it usable),
    # but jellyfin hasn't received it yet - Seerr must not sign in with a
    # login Jellyfin itself would still refuse.
    manager, engine, settings, root = _seerr_manager(
        tmp_path, ("sonarr", "jellyfin"), login_applied=False
    )

    manager.add_app("seerr")
    final = await _finish_add(manager)

    assert final.adding is not None
    assert final.adding.state == "error"
    assert final.adding.failure is not None
    assert final.adding.failure.code == "seerr_setup_refused"
    assert not any(call[0].startswith("compose_up") for call in engine.calls)


# --- not_ours and refused map to their own failure codes ---------------------


async def test_not_ours_maps_to_seerr_not_ours(tmp_path: Path) -> None:
    seerr = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/public"): [
                SeerrResponse(
                    ok=True,
                    status=200,
                    payload={"mediaServerType": 1, "initialized": True},
                    detail=None,
                )
            ]
        }
    )
    manager, engine, settings, root = _seerr_manager(tmp_path, ("sonarr", "jellyfin"), seerr=seerr)

    manager.add_app("seerr")
    final = await _finish_add(manager)

    assert final.adding is not None
    assert final.adding.failure is not None
    assert final.adding.failure.code == "seerr_not_ours"


async def test_refused_maps_to_seerr_setup_refused(tmp_path: Path) -> None:
    seerr = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/public"): [
                SeerrResponse(
                    ok=True,
                    status=200,
                    payload={"mediaServerType": 2, "initialized": True},
                    detail=None,
                )
            ],
            ("GET", "api/v1/settings/main"): [
                SeerrResponse(ok=False, status=403, payload=None, detail="forbidden")
            ],
        }
    )
    manager, engine, settings, root = _seerr_manager(tmp_path, ("sonarr", "jellyfin"), seerr=seerr)

    manager.add_app("seerr")
    final = await _finish_add(manager)

    assert final.adding is not None
    assert final.adding.failure is not None
    assert final.adding.failure.code == "seerr_setup_refused"


# --- waiting first, then done, shows the warming-up line ---------------------


async def test_waiting_then_done_shows_the_warming_up_line_first(tmp_path: Path) -> None:
    seerr = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/public"): [
                SeerrResponse(ok=False, status=503, payload=None, detail=None),
                SeerrResponse(
                    ok=True,
                    status=200,
                    payload={"mediaServerType": 2, "initialized": True},
                    detail=None,
                ),
            ],
            ("GET", "api/v1/settings/main"): [
                SeerrResponse(ok=True, status=200, payload={}, detail=None)
            ],
        }
    )
    manager, engine, settings, root = _seerr_manager(tmp_path, ("sonarr", "jellyfin"), seerr=seerr)

    manager.add_app("seerr")
    seen_warming_up = False
    for _ in range(200_000):
        current = manager.snapshot()
        if current.adding is not None and "waking up" in current.adding.line.lower():
            seen_warming_up = True
        if current.adding is None or current.adding.state == "error":
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("add never finished")

    final = manager.snapshot()
    assert seen_warming_up
    assert final.adding is None
    assert any(app.app_id == "seerr" and app.state == "done" for app in final.apps)


# --- a timeout's technical carries both the container logs and the last
# setup attempt's own technical -----------------------------------------


async def test_timeout_technical_carries_both_logs_and_last_setup_technical(
    tmp_path: Path,
) -> None:
    """Seerr that never settles (every `settings/public` answers 503) must
    fail with `never_became_ready` whose technical holds BOTH the
    container's own logs AND the last setup attempt's own technical - never
    only one of the two.
    """
    seerr = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/public"): [
                SeerrResponse(ok=False, status=503, payload=None, detail=None)
            ]
        }
    )
    manager, engine, settings, root = _seerr_manager(tmp_path, ("sonarr", "jellyfin"), seerr=seerr)
    engine._logs["seerr"] = "seerr container logs: still booting"  # type: ignore[attr-defined]

    manager.add_app("seerr")
    final = await _finish_add(manager)

    assert final.adding is not None
    assert final.adding.state == "error"
    assert final.adding.failure is not None
    assert final.adding.failure.code == "never_became_ready"
    technical = final.adding.failure.technical
    assert "seerr container logs: still booting" in technical
    assert "api/v1/settings/public" in technical
    assert "503" in technical


# --- a redeploy with Seerr already set up makes only the two GETs -----------


async def test_a_redeploy_with_seerr_set_up_makes_only_the_two_gets(tmp_path: Path) -> None:
    """A full deploy re-run (both containers already exist, both already
    proven) must never repeat Seerr's writes - only the two read-back GETs
    `_ensure_seerr_setup` makes once it sees `initialized: true`.
    """
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(("jellyfin", "seerr"), root)
    save_state(settings.config_dir, install)
    saved = save_login(settings.config_dir, _LOGIN_USERNAME, _LOGIN_PASSWORD, honor_reset=None)
    record_applied(settings.config_dir, "jellyfin", saved.generation)
    save_jellyfin(settings.config_dir, "already-good-jellyfin-key", "u1")
    write_marker(settings, root, ("jellyfin", "seerr"), os.getuid(), os.getgid())

    jellyfin = FakeJellyfinServer(
        {
            ("GET", "/System/Info/Public"): [
                JellyfinResponse(
                    ok=True, status=200, payload={"ServerName": "Marrquee"}, detail=None
                )
            ],
            ("GET", "/System/Info"): [
                JellyfinResponse(
                    ok=True, status=200, payload={"ServerName": "Marrquee"}, detail=None
                )
            ],
        }
    )
    seerr = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/public"): [
                SeerrResponse(
                    ok=True,
                    status=200,
                    payload={"mediaServerType": 2, "initialized": True},
                    detail=None,
                )
            ],
            ("GET", "api/v1/settings/main"): [
                SeerrResponse(ok=True, status=200, payload={}, detail=None)
            ],
        }
    )
    engine = FakeDockerEngine(
        DockerStatus(connected=True),
        containers={
            "jellyfin": _running_container("jellyfin"),
            "seerr": _running_container("seerr"),
        },
        images={get_app("jellyfin").image, _SEERR_APP.image},
        self_container_id="marrquee",
    )
    clock = _FakeClock()
    manager = DeployManager(
        settings,
        engine,
        probe=FakeReadinessProbe(default=True),
        clock=clock.time,
        sleep=clock.sleep,
        login=FakeLoginApplier(),
    )
    manager._jellyfin = jellyfin  # type: ignore[attr-defined]
    manager._seerr = seerr  # type: ignore[attr-defined]

    manager.start()
    history = await _run_to_terminal(manager)

    assert history[-1].phase == "finale"
    assert history[-1].failure is None
    assert seerr.calls == [
        ("GET", "api/v1/settings/public", (), False),
        ("GET", "api/v1/settings/main", (), True),
    ]


# --- a snapshot holding seerr_not_ours reloads --------------------------------


async def test_a_snapshot_holding_seerr_not_ours_reloads(tmp_path: Path) -> None:
    seerr = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/public"): [
                SeerrResponse(
                    ok=True,
                    status=200,
                    payload={"mediaServerType": 1, "initialized": True},
                    detail=None,
                )
            ]
        }
    )
    manager, engine, settings, root = _seerr_manager(tmp_path, ("sonarr", "jellyfin"), seerr=seerr)

    manager.add_app("seerr")
    final = await _finish_add(manager)
    assert final.adding is not None
    assert final.adding.failure is not None
    assert final.adding.failure.code == "seerr_not_ours"

    reloaded = DeployManager(settings, engine)
    reloaded_snapshot = reloaded.snapshot()
    assert reloaded_snapshot.adding is not None
    assert reloaded_snapshot.adding.failure is not None
    assert reloaded_snapshot.adding.failure.code == "seerr_not_ours"


# --- secrets never reach diagnostics, even echoed back by Seerr itself -------


async def test_no_key_token_or_password_in_diagnostics(tmp_path: Path) -> None:
    seerr_key = "fake-seerr-secret-key-999"
    plex_token = "plex-tv-leaked-token-777"
    manager, engine, settings, root = _seerr_manager(
        tmp_path,
        ("sonarr", "jellyfin"),
        extra_api_keys={"seerr": seerr_key},
    )
    save_plex_sign_in(settings.config_dir, plex_token, "owner")
    leaking_detail = (
        f"leaked seerr key {seerr_key}, plex token {plex_token}, password {_LOGIN_PASSWORD}"
    )
    seerr = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/public"): [
                SeerrResponse(
                    ok=True,
                    status=200,
                    payload={"mediaServerType": 2, "initialized": True},
                    detail=None,
                )
            ],
            ("GET", "api/v1/settings/main"): [
                SeerrResponse(ok=False, status=403, payload=None, detail=leaking_detail)
            ],
        }
    )
    manager._seerr = seerr  # type: ignore[attr-defined]

    manager.add_app("seerr")
    final = await _finish_add(manager)

    assert final.adding is not None
    assert final.adding.failure is not None
    assert final.adding.failure.code == "seerr_setup_refused"
    grown = load_state(settings.config_dir)
    assert grown is not None and grown.api_keys["seerr"] == seerr_key

    diagnostics = (settings.config_dir / "last-failure.txt").read_text()
    assert seerr_key not in diagnostics
    assert plex_token not in diagnostics
    assert _LOGIN_PASSWORD not in diagnostics

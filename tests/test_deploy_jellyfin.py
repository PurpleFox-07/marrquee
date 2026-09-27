"""Tests for the deploy engine's own Jellyfin bring-up: `_bring_up_jellyfin`
and its helpers, reached through `DeployManager.start()`/`add_app`/
`retry_add`/`cancel_add` exactly the way the Plex suite exercises
`_bring_up_plex`.

Nothing here waits a real second or touches a real Docker daemon or a real
Jellyfin: `FakeDockerEngine`'s own `frames=`/`containers=` script exactly
what each `inspect("jellyfin")` call sees, `FakeJellyfinServer` scripts the
local Jellyfin server, and `_FakeClock` (imported from `test_deploy`)
advances only when the loop itself sleeps.

Every fresh-root scenario here brings up "jellyfin" alone, or beside an
already-`finale` "sonarr" - either way, `_find_name_clash` (run before any
bring-up) is the FIRST `inspect("jellyfin")` call whenever "jellyfin" isn't
already the marker's own, so a scripted `frames["jellyfin"]` array always
reserves its first slot(s) for that check, the same shape
`test_deploy_plex.py` documents for Plex.
"""

from __future__ import annotations

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
from marrquee.catalog import get_app, require_port
from marrquee.config import Settings
from marrquee.deploy import DeployManager, DeploySnapshot, FakeReadinessProbe
from marrquee.docker_client import ComposeResult, ContainerSnapshot, DockerStatus, FakeDockerEngine
from marrquee.jellyfin import FakeJellyfinServer, JellyfinResponse, load_jellyfin, save_jellyfin
from marrquee.login import save_login
from marrquee.login_apply import FakeLoginApplier
from marrquee.state import load_state, save_state
from marrquee.storage import write_marker

_JELLYFIN_APP = get_app("jellyfin")
_JELLYFIN_PORT = require_port(_JELLYFIN_APP)
_LOGIN_USERNAME = "owner"
_LOGIN_PASSWORD = "s3cret-password-1"


def _absent(name: str) -> ContainerSnapshot:
    return ContainerSnapshot(
        name=name, exists=False, state=None, exit_code=None, image=None, detail=None
    )


def _jellyfin_only_manager(
    tmp_path: Path,
    *,
    jellyfin: FakeJellyfinServer | None = None,
    with_login: bool = True,
    already_owned: bool = False,
    containers: dict[str, ContainerSnapshot] | None = None,
    frames: dict[str, list[ContainerSnapshot]] | None = None,
    logs: dict[str, str] | None = None,
) -> tuple[DeployManager, FakeDockerEngine, Settings, PurePosixPath]:
    """A `DeployManager` about to run a fresh, Jellyfin-only full deploy.

    `already_owned=True` pre-writes the marker with "jellyfin" already in
    it - the redeploy/resume shape, where `_find_name_clash` skips
    "jellyfin" entirely rather than mistaking Marrquee's own earlier
    container for someone else's.
    """
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(("jellyfin",), root)
    save_state(settings.config_dir, install)
    if with_login:
        save_login(settings.config_dir, _LOGIN_USERNAME, _LOGIN_PASSWORD, honor_reset=None)
    if already_owned:
        write_marker(settings, root, ("jellyfin",), os.getuid(), os.getgid())

    engine = FakeDockerEngine(
        DockerStatus(connected=True),
        containers=containers,
        images={_JELLYFIN_APP.image},
        frames=frames,
        logs=logs,
    )
    clock = _FakeClock()
    manager = DeployManager(settings, engine, clock=clock.time, sleep=clock.sleep)
    manager._jellyfin = jellyfin if jellyfin is not None else FakeJellyfinServer({})  # type: ignore[attr-defined]
    return manager, engine, settings, root


async def _run_jellyfin_deploy(manager: DeployManager) -> DeploySnapshot:
    manager.start()
    history = await _run_to_terminal(manager)
    return history[-1]


# --- no login means no bring-up ever touches Docker --------------------------


async def test_no_login_refuses_before_any_bring_up_docker_call(tmp_path: Path) -> None:
    manager, engine, settings, root = _jellyfin_only_manager(
        tmp_path, with_login=False, frames={"jellyfin": [_absent("jellyfin")]}
    )

    final = await _run_jellyfin_deploy(manager)

    assert final.failure is not None
    assert final.failure.code == "jellyfin_setup_refused"
    inspect_calls = [call for call in engine.calls if call == ("inspect", ("jellyfin",))]
    # Exactly the name-clash check's own call - the no-login refusal returns
    # before `_bring_up_jellyfin` ever touches Docker a second time.
    assert len(inspect_calls) == 1
    assert not any(call[0] == "image_present" for call in engine.calls)
    assert not any(call[0].startswith("compose_up") for call in engine.calls)
    assert load_jellyfin(settings.config_dir) is None


# --- a name clash is caught before Jellyfin's own port pre-flight -------------


async def test_an_owners_own_container_named_jellyfin_hits_name_clash(tmp_path: Path) -> None:
    manager, engine, settings, root = _jellyfin_only_manager(
        tmp_path, frames={"jellyfin": [_running_container("jellyfin")]}
    )

    final = await _run_jellyfin_deploy(manager)

    assert final.failure is not None
    assert final.failure.code == "name_clash"
    assert not any(call[0] == "image_present" for call in engine.calls)


async def test_something_on_8096_before_our_container_is_port_taken(tmp_path: Path) -> None:
    jellyfin = FakeJellyfinServer(
        {
            ("GET", "/System/Info/Public"): [
                JellyfinResponse(
                    ok=True, status=200, payload={"ServerName": "not-ours"}, detail=None
                )
            ]
        }
    )
    manager, engine, settings, root = _jellyfin_only_manager(
        tmp_path,
        jellyfin=jellyfin,
        frames={
            "jellyfin": [_absent("jellyfin"), _absent("jellyfin"), _running_container("jellyfin")]
        },
    )

    final = await _run_jellyfin_deploy(manager)

    assert final.failure is not None
    assert final.failure.code == "jellyfin_port_taken"
    assert str(_JELLYFIN_PORT) in final.failure.technical
    assert not any(call[0].startswith("compose_up") for call in engine.calls)


async def test_a_dead_pre_flight_is_absent_not_port_taken(tmp_path: Path) -> None:
    """Status 0 (no answer at all) is exactly what a genuinely empty port
    looks like - it must fall through to a normal bring-up, never
    `jellyfin_port_taken`.
    """
    jellyfin = FakeJellyfinServer(
        {
            ("GET", "/System/Info/Public"): [
                JellyfinResponse(ok=False, status=0, payload=None, detail="connection refused")
            ]
        }
    )
    manager, engine, settings, root = _jellyfin_only_manager(
        tmp_path,
        jellyfin=jellyfin,
        frames={"jellyfin": [_absent("jellyfin"), _absent("jellyfin")]},
    )
    engine._compose_results["jellyfin"] = ComposeResult(  # type: ignore[attr-defined]
        ok=False, exit_code=1, output="Error: something went wrong"
    )

    final = await _run_jellyfin_deploy(manager)

    assert final.failure is not None
    assert final.failure.code == "compose_failed"


# --- green only after Marrquee's own first-time setup -------------------------


async def test_green_only_after_setup(tmp_path: Path) -> None:
    """A redeploy of an already-existing container: the pre-flight port
    check never fires (the container already exists), and the loop only
    reports "done" once `ensure_jellyfin_admin` itself says so - a warming
    up tick first, because the container answers before Jellyfin itself
    has finished settling.
    """
    jellyfin = FakeJellyfinServer(
        {
            ("GET", "/System/Info/Public"): [
                JellyfinResponse(ok=False, status=503, payload=None, detail=None),
                JellyfinResponse(
                    ok=True, status=200, payload={"StartupWizardCompleted": False}, detail=None
                ),
            ],
            ("GET", "/Startup/User"): [
                JellyfinResponse(ok=True, status=200, payload={}, detail=None)
            ],
            ("POST", "/Startup/User"): [
                JellyfinResponse(ok=True, status=204, payload=None, detail=None)
            ],
            ("POST", "/Startup/Complete"): [
                JellyfinResponse(ok=True, status=204, payload=None, detail=None)
            ],
            ("POST", "/Users/AuthenticateByName"): [
                JellyfinResponse(
                    ok=True,
                    status=200,
                    payload={"AccessToken": "session-tok", "User": {"Id": "u1"}},
                    detail=None,
                )
            ],
            ("GET", "/Auth/Keys"): [
                JellyfinResponse(ok=True, status=200, payload={"Items": []}, detail=None),
                JellyfinResponse(
                    ok=True,
                    status=200,
                    payload={"Items": [{"Id": 3, "AppName": "Marrquee", "AccessToken": "key-abc"}]},
                    detail=None,
                ),
            ],
            ("POST", "/Auth/Keys"): [
                JellyfinResponse(ok=True, status=204, payload=None, detail=None)
            ],
            ("GET", "/System/Configuration"): [
                JellyfinResponse(ok=True, status=200, payload={"ServerName": ""}, detail=None)
            ],
            ("POST", "/System/Configuration"): [
                JellyfinResponse(ok=True, status=204, payload=None, detail=None)
            ],
            ("POST", "/Sessions/Logout"): [
                JellyfinResponse(ok=True, status=204, payload=None, detail=None)
            ],
        }
    )
    manager, engine, settings, root = _jellyfin_only_manager(
        tmp_path,
        jellyfin=jellyfin,
        already_owned=True,
        containers={"jellyfin": _running_container("jellyfin")},
    )

    final = await _run_jellyfin_deploy(manager)

    assert final.phase == "finale"
    assert all(app.state == "done" for app in final.apps)
    record = load_jellyfin(settings.config_dir)
    assert record is not None
    assert record.api_key == "key-abc"
    assert record.admin_id == "u1"


async def test_a_redeploy_with_a_working_saved_key_does_no_setup(tmp_path: Path) -> None:
    manager, engine, settings, root = _jellyfin_only_manager(
        tmp_path,
        already_owned=True,
        containers={"jellyfin": _running_container("jellyfin")},
    )
    save_jellyfin(settings.config_dir, "already-good-key", "u1")
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
    manager._jellyfin = jellyfin  # type: ignore[attr-defined]

    final = await _run_jellyfin_deploy(manager)

    assert final.phase == "finale"
    # The EXACT call list, not just the set of paths touched: a pre-flight
    # port check that fired anyway (because the container already exists)
    # would add a THIRD call here, and a set comparison would hide it since
    # it repeats a path already in the set.
    assert jellyfin.calls == [
        ("GET", "/System/Info/Public", (), False),
        ("GET", "/System/Info", (), True),
    ]


async def test_pre_flight_never_runs_for_a_container_that_already_exists(tmp_path: Path) -> None:
    """A redeploy or a Try-again of an already-running Jellyfin must never
    run the port pre-flight check at all - only a container Docker reports
    as absent could possibly be someone else's process on our port.
    """
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
    manager, engine, settings, root = _jellyfin_only_manager(
        tmp_path,
        jellyfin=jellyfin,
        already_owned=True,
        containers={"jellyfin": _running_container("jellyfin")},
    )
    save_jellyfin(settings.config_dir, "already-good-key", "u1")

    final = await _run_jellyfin_deploy(manager)

    assert final.phase == "finale"
    assert final.failure is None
    # A wrongly-firing pre-flight would answer 200 (status != 0) and fail
    # the run as `jellyfin_port_taken` before `compose_up` ever ran - so
    # `compose_up` actually running, AND the jellyfin call list holding
    # only `ensure_jellyfin_admin`'s own two calls (never a third, earlier
    # one), together rule that out.
    assert any(call[0].startswith("compose_up") for call in engine.calls)
    assert jellyfin.calls == [
        ("GET", "/System/Info/Public", (), False),
        ("GET", "/System/Info", (), True),
    ]


# --- someone else's Jellyfin never becomes ours -------------------------------


async def test_not_ours_fails_with_no_save_and_no_docker_change(tmp_path: Path) -> None:
    jellyfin = FakeJellyfinServer(
        {
            ("GET", "/System/Info/Public"): [
                JellyfinResponse(
                    ok=True, status=200, payload={"StartupWizardCompleted": True}, detail=None
                )
            ],
            ("POST", "/Users/AuthenticateByName"): [
                JellyfinResponse(ok=False, status=401, payload=None, detail=None)
            ],
        }
    )
    manager, engine, settings, root = _jellyfin_only_manager(
        tmp_path,
        jellyfin=jellyfin,
        already_owned=True,
        containers={"jellyfin": _running_container("jellyfin")},
    )

    final = await _run_jellyfin_deploy(manager)

    assert final.failure is not None
    assert final.failure.code == "jellyfin_not_ours"
    assert load_jellyfin(settings.config_dir) is None

    # The failure code round-trips through a persisted snapshot (the same
    # `_failure_from_payload` allow-list every other code goes through).
    reloaded = DeployManager(settings, engine)
    reloaded_snapshot = reloaded.snapshot()
    assert reloaded_snapshot.failure is not None
    assert reloaded_snapshot.failure.code == "jellyfin_not_ours"


async def test_a_refused_setup_surfaces_fast_never_as_never_became_ready(tmp_path: Path) -> None:
    """A hard refusal partway through setup (a 400 that isn't 401/403 and
    isn't a 5xx) must end the run as `jellyfin_setup_refused` on the very
    first tick - never be swallowed into the generic "still waiting" line
    and left to spin until the 300-second timeout.
    """
    jellyfin = FakeJellyfinServer(
        {
            ("GET", "/System/Info/Public"): [
                JellyfinResponse(
                    ok=True, status=200, payload={"StartupWizardCompleted": False}, detail=None
                )
            ],
            ("GET", "/Startup/User"): [
                JellyfinResponse(ok=True, status=200, payload={}, detail=None)
            ],
            ("POST", "/Startup/User"): [
                JellyfinResponse(ok=False, status=400, payload=None, detail=None)
            ],
        }
    )
    manager, engine, settings, root = _jellyfin_only_manager(
        tmp_path,
        jellyfin=jellyfin,
        already_owned=True,
        containers={"jellyfin": _running_container("jellyfin")},
    )

    final = await _run_jellyfin_deploy(manager)

    assert final.failure is not None
    assert final.failure.code == "jellyfin_setup_refused"
    # The loop never slept waiting for a retry that was never coming - a
    # refused setup is a first-tick verdict, nowhere near the 300-second
    # never-became-ready timeout.
    assert manager._clock() < DeployManager.REASSURANCE_AFTER_SECONDS  # type: ignore[attr-defined]


# --- redaction: the saved key never reaches diagnostics -----------------------


async def test_diagnostics_never_contain_the_jellyfin_key(tmp_path: Path) -> None:
    manager, engine, settings, root = _jellyfin_only_manager(
        tmp_path,
        already_owned=True,
        containers={"jellyfin": _running_container("jellyfin")},
        logs={"jellyfin": "leaked key key-leaked-999 right here in the logs"},
    )
    save_jellyfin(settings.config_dir, "key-leaked-999", "u1")
    jellyfin = FakeJellyfinServer(
        {
            ("GET", "/System/Info/Public"): [
                JellyfinResponse(
                    ok=True, status=200, payload={"StartupWizardCompleted": True}, detail=None
                )
            ],
            ("GET", "/System/Info"): [
                JellyfinResponse(ok=False, status=401, payload=None, detail=None)
            ],
            ("POST", "/Users/AuthenticateByName"): [
                JellyfinResponse(ok=False, status=0, payload=None, detail="connection refused")
            ],
        }
    )
    manager._jellyfin = jellyfin  # type: ignore[attr-defined]

    final = await _run_jellyfin_deploy(manager)

    assert final.failure is not None
    assert final.failure.code == "never_became_ready"
    diagnostics = (settings.config_dir / "last-failure.txt").read_text()
    assert "key-leaked-999" not in diagnostics
    assert "<redacted-jellyfin-key>" in diagnostics


# --- add/cancel: cancelling a failed add keeps jellyfin.json -----------------


async def _jellyfin_added_to_sonarr(
    tmp_path: Path,
    *,
    jellyfin: FakeJellyfinServer | None = None,
) -> tuple[DeployManager, FakeDockerEngine, Settings, PurePosixPath]:
    """A manager already at `finale` for `("sonarr",)`, ready for
    `add_app("jellyfin")` against the same `FakeDockerEngine`.
    """
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(("sonarr",), root)
    save_state(settings.config_dir, install)
    save_login(settings.config_dir, _LOGIN_USERNAME, _LOGIN_PASSWORD, honor_reset=None)
    write_marker(settings, root, ("sonarr",), os.getuid(), os.getgid())

    engine = FakeDockerEngine(
        DockerStatus(connected=True),
        containers={"sonarr": _running_container("sonarr")},
        images={get_app("sonarr").image, _JELLYFIN_APP.image},
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
    manager._jellyfin = jellyfin if jellyfin is not None else FakeJellyfinServer({})  # type: ignore[attr-defined]
    manager.start()
    history = await _run_to_terminal(manager)
    assert history[-1].phase == "finale"
    return manager, engine, settings, root


async def test_cancel_of_a_failed_jellyfin_add_keeps_jellyfin_json(tmp_path: Path) -> None:
    """The design correction: Jellyfin's own cancel never touches Plex's
    claim-clearing helper, and its settings folder (and so its key) is
    left exactly where a later add would find it.
    """
    jellyfin = FakeJellyfinServer(
        {
            ("GET", "/System/Info/Public"): [
                JellyfinResponse(ok=False, status=0, payload=None, detail="connection refused")
            ]
        }
    )
    manager, engine, settings, root = await _jellyfin_added_to_sonarr(tmp_path, jellyfin=jellyfin)
    # A prior successful setup left this file behind - cancelling a fresh
    # failed add must never touch it.
    save_jellyfin(settings.config_dir, "already-good-key", "u1")
    engine._compose_results["jellyfin"] = ComposeResult(  # type: ignore[attr-defined]
        ok=False, exit_code=1, output="Error: something went wrong"
    )

    manager.add_app("jellyfin")
    failed = await _finish_add(manager)
    assert failed.adding is not None
    assert failed.adding.state == "error"
    assert failed.adding.failure is not None
    assert failed.adding.failure.code == "compose_failed"

    ok = await manager.cancel_add()
    assert ok is True
    assert manager.snapshot().adding is None
    record = load_jellyfin(settings.config_dir)
    assert record is not None
    assert record.api_key == "already-good-key"

    after_cancel = load_state(settings.config_dir)
    assert after_cancel is not None
    assert after_cancel.app_ids == ("sonarr",)


async def test_cancel_of_a_failed_jellyfin_add_never_calls_clear_plex_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`cancel_add`'s own media-server branch is narrowed to Plex's own id -
    a Jellyfin-only cancel must never even call Plex's claim-clearing
    helper, not merely leave nothing for it to find.
    """
    calls: list[str] = []
    monkeypatch.setattr(deploy, "clear_plex_claim", lambda settings, root: calls.append("called"))
    jellyfin = FakeJellyfinServer(
        {
            ("GET", "/System/Info/Public"): [
                JellyfinResponse(ok=False, status=0, payload=None, detail="connection refused")
            ]
        }
    )
    manager, engine, settings, root = await _jellyfin_added_to_sonarr(tmp_path, jellyfin=jellyfin)
    engine._compose_results["jellyfin"] = ComposeResult(  # type: ignore[attr-defined]
        ok=False, exit_code=1, output="Error: something went wrong"
    )

    manager.add_app("jellyfin")
    await _finish_add(manager)
    assert await manager.cancel_add() is True

    assert calls == []

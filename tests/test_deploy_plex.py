"""Tests for the deploy engine's own Plex bring-up: `_bring_up_plex` and its
helpers, reached through `DeployManager.start()`/`add_app`/`retry_add`/
`cancel_add` exactly the way the arr suite exercises `_bring_up_app`.

Nothing here waits a real second or touches a real Docker daemon or plex.tv:
`FakeDockerEngine`'s own `frames=` scripts exactly what each `inspect("plex")`
call sees, `FakePlexTv`/`FakePlexServer` script plex.tv and the local Plex
server, and `_FakeClock` (imported from `test_deploy`) advances only when the
loop itself sleeps.

Every fresh-root scenario here brings up "plex" alone, or "plex" added
beside an already-`finale` "sonarr" - either way, `_find_name_clash` (run
before any bring-up, for the add path AND the full-deploy path alike) is the
FIRST `inspect("plex")` call whenever "plex" isn't already the marker's own,
so a scripted `frames["plex"]` array always reserves its first slot(s) for
that check, exactly as the story's own amendment describes.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

from test_deploy import (
    _FakeClock,
    _fresh_root,
    _install_state,
    _run_to_terminal,
    _running_container,
    _settings,
)
from test_deploy_add import _finish_add

from marrquee.catalog import get_app, require_port
from marrquee.config import Settings
from marrquee.deploy import DeployManager, DeploySnapshot, FakeReadinessProbe
from marrquee.docker_client import ContainerSnapshot, DockerStatus, FakeDockerEngine
from marrquee.login import save_login
from marrquee.login_apply import FakeLoginApplier
from marrquee.plex import (
    FakePlexServer,
    FakePlexTv,
    PlexIdentity,
    PlexServer,
    PlexTv,
    load_plex_account,
    save_plex_sign_in,
    write_plex_claim,
)
from marrquee.state import save_state
from marrquee.storage import to_host_view, write_marker

_PLEX_APP = get_app("plex")
_PLEX_PORT = require_port(_PLEX_APP)
_UNABLE_TO_CLAIM_LOG = "starting services\nUnable to claim Plex server: invalid code\n"


def _absent(name: str) -> ContainerSnapshot:
    return ContainerSnapshot(
        name=name, exists=False, state=None, exit_code=None, image=None, detail=None
    )


def _claim_file(settings: Settings, root: PurePosixPath) -> Path:
    return to_host_view(settings, str(root)) / "marrquee" / "plex" / "plex_claim"


def _plex_only_manager(
    tmp_path: Path,
    *,
    plex_tv: PlexTv | None = None,
    plex_server: PlexServer | None = None,
    already_owned: bool = False,
    containers: dict[str, ContainerSnapshot] | None = None,
    frames: dict[str, list[ContainerSnapshot]] | None = None,
    logs: dict[str, str] | None = None,
    remove_results: dict[str, bool] | None = None,
) -> tuple[DeployManager, FakeDockerEngine, Settings, PurePosixPath]:
    """A `DeployManager` about to run a fresh, Plex-only full deploy.

    `already_owned=True` pre-writes the marker with "plex" already in it -
    the redeploy/resume shape, where `_find_name_clash` skips "plex"
    entirely rather than mistaking Marrquee's own earlier container for
    someone else's.
    """
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(("plex",), root)
    save_state(settings.config_dir, install)
    save_plex_sign_in(settings.config_dir, "tok-secret-123", "owner")
    if already_owned:
        write_marker(settings, root, ("plex",), os.getuid(), os.getgid())

    engine = FakeDockerEngine(
        DockerStatus(connected=True),
        containers=containers,
        images={_PLEX_APP.image},
        frames=frames,
        logs=logs,
        remove_results=remove_results,
    )
    clock = _FakeClock()
    manager = DeployManager(
        settings,
        engine,
        clock=clock.time,
        sleep=clock.sleep,
        plex_tv=plex_tv if plex_tv is not None else FakePlexTv(),
        plex_server=plex_server if plex_server is not None else FakePlexServer(),
    )
    return manager, engine, settings, root


async def _run_plex_deploy(manager: DeployManager) -> DeploySnapshot:
    manager.start()
    history = await _run_to_terminal(manager)
    return history[-1]


# --- FIRST TEST: an expired claim is recovered once, and cleared -------------


async def test_expired_claim_is_recovered_once_and_cleared(tmp_path: Path) -> None:
    plex_tv = FakePlexTv(claims=("claim-fresh1", "claim-fresh2"))
    plex_server = FakePlexServer(
        identities=[None, PlexIdentity(claimed=False, machine_id="m"), PlexIdentity(True, "m")]
    )
    manager, engine, settings, root = _plex_only_manager(
        tmp_path,
        plex_tv=plex_tv,
        plex_server=plex_server,
        frames={"plex": [_absent("plex"), _absent("plex"), _running_container("plex")]},
        logs={"plex": _UNABLE_TO_CLAIM_LOG},
    )

    final = await _run_plex_deploy(manager)

    assert final.phase == "finale"
    assert all(app.state == "done" for app in final.apps)
    assert plex_tv.calls.count("claim_token") == 2
    assert [call for call in engine.calls if call[0] == "remove_container"] == [
        ("remove_container", ("plex",))
    ]
    assert not _claim_file(settings, root).exists()


async def test_a_second_unclaimed_identity_fails_plex_not_claimed(tmp_path: Path) -> None:
    plex_tv = FakePlexTv(claims=("claim-fresh1", "claim-fresh2"))
    plex_server = FakePlexServer(
        identities=[
            None,
            PlexIdentity(claimed=False, machine_id="m"),
            PlexIdentity(claimed=False, machine_id="m"),
        ]
    )
    manager, engine, settings, root = _plex_only_manager(
        tmp_path,
        plex_tv=plex_tv,
        plex_server=plex_server,
        frames={"plex": [_absent("plex"), _absent("plex"), _running_container("plex")]},
        logs={"plex": _UNABLE_TO_CLAIM_LOG},
    )

    final = await _run_plex_deploy(manager)

    assert final.phase == "error"
    assert final.failure is not None
    assert final.failure.code == "plex_not_claimed"
    assert plex_tv.calls.count("claim_token") == 2
    assert not _claim_file(settings, root).exists()


async def test_a_transient_unclaimed_tick_under_the_grace_period_is_not_a_retry(
    tmp_path: Path,
) -> None:
    """No failure log, and the run never gets near the 90s grace period - a
    lone unclaimed tick before Plex settles must never spend the one retry
    this loop is allowed.
    """
    plex_tv = FakePlexTv()
    plex_server = FakePlexServer(
        identities=[None, PlexIdentity(claimed=False, machine_id="m"), PlexIdentity(True, "m")]
    )
    manager, engine, settings, root = _plex_only_manager(
        tmp_path,
        plex_tv=plex_tv,
        plex_server=plex_server,
        frames={"plex": [_absent("plex"), _absent("plex"), _running_container("plex")]},
        logs={"plex": "starting services\n"},
    )

    final = await _run_plex_deploy(manager)

    assert final.phase == "finale"
    assert plex_tv.calls.count("claim_token") == 1
    assert not any(call[0] == "remove_container" for call in engine.calls)


async def test_something_on_32400_before_our_container_is_port_taken(tmp_path: Path) -> None:
    plex_tv = FakePlexTv()
    plex_server = FakePlexServer(identities=[PlexIdentity(claimed=True, machine_id="not-ours")])
    manager, engine, settings, root = _plex_only_manager(
        tmp_path,
        plex_tv=plex_tv,
        plex_server=plex_server,
        frames={"plex": [_absent("plex"), _absent("plex")]},
    )

    final = await _run_plex_deploy(manager)

    assert final.failure is not None
    assert final.failure.code == "plex_port_taken"
    assert str(_PLEX_PORT) in final.failure.technical
    assert not any(call[0].startswith("compose_up") for call in engine.calls)
    assert plex_tv.calls == []


async def test_an_owners_own_container_named_plex_hits_name_clash_before_port_taken(
    tmp_path: Path,
) -> None:
    """`_find_name_clash` runs before any bring-up, for every app - an
    existing container named "plex" that Marrquee didn't create (no marker
    owns it) must be refused as a clash, never mistaken for "someone else's
    Plex already answers on our port" (that pre-flight check never even
    runs: it lives inside `_bring_up_plex`, which a name clash never
    reaches).
    """
    plex_tv = FakePlexTv()
    manager, engine, settings, root = _plex_only_manager(
        tmp_path,
        plex_tv=plex_tv,
        frames={"plex": [_running_container("plex")]},
    )

    final = await _run_plex_deploy(manager)

    assert final.failure is not None
    assert final.failure.code == "name_clash"
    assert plex_tv.calls == []
    assert not any(call[0] == "image_present" for call in engine.calls)


async def test_unclaimed_for_the_grace_period_alone_triggers_one_retry(tmp_path: Path) -> None:
    """No "Unable to claim" anywhere in the logs - only
    `PLEX_UNCLAIMED_GRACE_SECONDS` of continuously unclaimed answers may
    trigger the one retry this loop is allowed.
    """
    plex_tv = FakePlexTv(claims=("claim-fresh1", "claim-fresh2"))
    plex_server = FakePlexServer(identities=[None, PlexIdentity(claimed=False, machine_id="m")])
    manager, engine, settings, root = _plex_only_manager(
        tmp_path,
        plex_tv=plex_tv,
        plex_server=plex_server,
        frames={"plex": [_absent("plex"), _absent("plex"), _running_container("plex")]},
        logs={"plex": "starting services\n"},
    )

    final = await _run_plex_deploy(manager)

    assert final.failure is not None
    assert final.failure.code == "plex_not_claimed"
    assert plex_tv.calls.count("claim_token") == 2
    assert any(call[0] == "remove_container" for call in engine.calls)


async def test_a_gap_with_no_identity_reading_restarts_the_grace_clock(tmp_path: Path) -> None:
    """A container that briefly stops answering (a restart) between two
    unclaimed stretches must restart the 90s grace clock, not let the two
    stretches sum toward it - a bug here would fire the retry at global
    elapsed 90s (counting from the very first unclaimed tick); the correct
    behaviour only fires once the SECOND stretch itself has run 90s, at
    elapsed 100s in this script.
    """
    plex_tv = FakePlexTv(claims=("claim-fresh1", "claim-fresh2"))
    plex_server = FakePlexServer(identities=[None, PlexIdentity(claimed=False, machine_id="m")])
    manager, engine, settings, root = _plex_only_manager(
        tmp_path,
        plex_tv=plex_tv,
        plex_server=plex_server,
        frames={
            "plex": [
                _absent("plex"),  # name-clash check
                _absent("plex"),  # pre-flight
                _running_container("plex"),  # stretch 1, tick 0 - elapsed 0
                _running_container("plex"),  # tick 1 - elapsed 2
                _running_container("plex"),  # tick 2 - elapsed 4
                _absent("plex"),  # gap: not running - elapsed 6
                _absent("plex"),  # gap continues - elapsed 8
                _running_container("plex"),  # stretch 2 resumes - elapsed 10; repeats forever
            ]
        },
    )
    retry_ticks: list[float] = []
    clock_now = manager._clock  # type: ignore[attr-defined]
    original_remove_container = engine.remove_container

    async def _recording_remove_container(name: str):
        retry_ticks.append(clock_now())
        return await original_remove_container(name)

    engine.remove_container = _recording_remove_container  # type: ignore[method-assign]

    await _run_plex_deploy(manager)

    assert retry_ticks, "the loop never retried at all"
    assert retry_ticks[0] > 90.0


async def test_no_sign_in_refuses_before_any_bring_up_docker_call(tmp_path: Path) -> None:
    manager, engine, settings, root = _plex_only_manager(tmp_path)
    # `_plex_only_manager` always saves a sign-in - undo it so plex.json
    # goes back to being entirely absent.
    (settings.config_dir / "plex.json").unlink()

    final = await _run_plex_deploy(manager)

    assert final.failure is not None
    assert final.failure.code == "plex_sign_in_needed"
    inspect_plex_calls = [call for call in engine.calls if call == ("inspect", ("plex",))]
    # Exactly the name-clash check's own call - the sign-in refusal returns
    # before `_bring_up_plex` ever touches Docker a second time.
    assert len(inspect_plex_calls) == 1
    assert not any(call[0] == "image_present" for call in engine.calls)
    assert not any(call[0].startswith("compose_up") for call in engine.calls)


async def test_a_redeploy_of_an_already_claimed_plex_fetches_no_claim(tmp_path: Path) -> None:
    plex_tv = FakePlexTv()
    plex_server = FakePlexServer(identities=[PlexIdentity(claimed=True, machine_id="m")])
    manager, engine, settings, root = _plex_only_manager(
        tmp_path,
        plex_tv=plex_tv,
        plex_server=plex_server,
        already_owned=True,
        containers={"plex": _running_container("plex")},
    )

    final = await _run_plex_deploy(manager)

    assert final.phase == "finale"
    assert plex_tv.calls == []


async def test_plex_bring_up_never_calls_connect_network_or_the_arr_probe(tmp_path: Path) -> None:
    plex_server = FakePlexServer(identities=[PlexIdentity(claimed=True, machine_id="m")])
    probe = FakeReadinessProbe(default=False)  # would fail the run if it were ever asked
    manager, engine, settings, root = _plex_only_manager(
        tmp_path,
        plex_server=plex_server,
        already_owned=True,
        containers={"plex": _running_container("plex")},
    )
    manager._probe = probe  # type: ignore[attr-defined]

    final = await _run_plex_deploy(manager)

    assert final.phase == "finale"
    assert not any(call[0] == "connect_network" for call in engine.calls)
    assert probe.calls == []


async def test_diagnostics_never_contain_the_token_or_a_claim(tmp_path: Path) -> None:
    manager, engine, settings, root = _plex_only_manager(
        tmp_path,
        plex_tv=FakePlexTv(claims=("claim-abc_DEF",)),
        plex_server=FakePlexServer(identities=[]),  # never answers - always None
        logs={"plex": "leaked token tok-secret-123 and claim-abc_DEF right here"},
    )

    final = await _run_plex_deploy(manager)

    assert final.failure is not None
    assert final.failure.code == "never_became_ready"
    diagnostics = (settings.config_dir / "last-failure.txt").read_text()
    assert "tok-secret-123" not in diagnostics
    assert "claim-abc_DEF" not in diagnostics
    assert "<redacted-plex-token>" in diagnostics
    assert "<redacted-plex-claim>" in diagnostics


async def test_a_persisted_plex_not_claimed_failure_reloads_after_restart(tmp_path: Path) -> None:
    plex_server = FakePlexServer(
        identities=[
            None,
            PlexIdentity(claimed=False, machine_id="m"),
            PlexIdentity(claimed=False, machine_id="m"),
        ]
    )
    manager, engine, settings, root = _plex_only_manager(
        tmp_path,
        plex_server=plex_server,
        frames={"plex": [_absent("plex"), _absent("plex"), _running_container("plex")]},
        logs={"plex": _UNABLE_TO_CLAIM_LOG},
    )

    final = await _run_plex_deploy(manager)
    assert final.failure is not None
    assert final.failure.code == "plex_not_claimed"

    reloaded = DeployManager(settings, engine)
    reloaded_snapshot = reloaded.snapshot()
    assert reloaded_snapshot.failure is not None
    assert reloaded_snapshot.failure.code == "plex_not_claimed"


# --- add/retry/cancel: the same bring-up, reached through the add path ------


async def _plex_added_to_sonarr(
    tmp_path: Path,
    *,
    plex_tv: PlexTv | None = None,
    plex_server: PlexServer | None = None,
    frames: dict[str, list[ContainerSnapshot]] | None = None,
    logs: dict[str, str] | None = None,
    remove_results: dict[str, bool] | None = None,
) -> tuple[DeployManager, FakeDockerEngine, Settings, PurePosixPath]:
    """A manager already at `finale` for `("sonarr",)`, ready for
    `add_app("plex")` against the same `FakeDockerEngine`.
    """
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(("sonarr",), root)
    save_state(settings.config_dir, install)
    save_login(settings.config_dir, "owner", "s3cret-password-1", honor_reset=None)
    save_plex_sign_in(settings.config_dir, "tok-secret-123", "owner")
    # Sonarr's own deploy already happened in an earlier session - the
    # marker is what tells `_find_name_clash` this container is Marrquee's
    # own, not a clash, the moment the fixture's own full deploy checks it.
    write_marker(settings, root, ("sonarr",), os.getuid(), os.getgid())

    engine = FakeDockerEngine(
        DockerStatus(connected=True),
        containers={"sonarr": _running_container("sonarr")},
        images={get_app("sonarr").image, _PLEX_APP.image},
        frames=frames,
        logs=logs,
        remove_results=remove_results,
    )
    clock = _FakeClock()
    manager = DeployManager(
        settings,
        engine,
        probe=FakeReadinessProbe(default=True),
        clock=clock.time,
        sleep=clock.sleep,
        login=FakeLoginApplier(),
        plex_tv=plex_tv if plex_tv is not None else FakePlexTv(),
        plex_server=plex_server if plex_server is not None else FakePlexServer(),
    )
    manager.start()
    history = await _run_to_terminal(manager)
    assert history[-1].phase == "finale"
    return manager, engine, settings, root


async def test_try_again_after_plex_not_claimed_claims_once_more(tmp_path: Path) -> None:
    plex_tv = FakePlexTv(claims=("claim-fresh1", "claim-fresh2"))
    plex_server = FakePlexServer(
        identities=[
            None,
            PlexIdentity(claimed=False, machine_id="m"),
            PlexIdentity(claimed=False, machine_id="m"),
        ]
    )
    manager, engine, settings, root = await _plex_added_to_sonarr(
        tmp_path,
        plex_tv=plex_tv,
        plex_server=plex_server,
        frames={"plex": [_absent("plex"), _absent("plex"), _running_container("plex")]},
        logs={"plex": _UNABLE_TO_CLAIM_LOG},
    )

    calls_before_add = len(engine.calls)
    assert manager.add_app("plex") == "started"
    first_attempt = await _finish_add(manager)
    assert first_attempt.adding is not None
    assert first_attempt.adding.state == "error"
    assert first_attempt.adding.failure is not None
    assert first_attempt.adding.failure.code == "plex_not_claimed"
    assert plex_tv.calls.count("claim_token") == 2

    # A retry on an existing Plex container never repeats the pre-flight
    # port/clash check's own claim - only the loop's own unclaimed branch
    # is allowed to claim again, once, while unclaimed persists.
    calls_before_retry = len(engine.calls)
    plex_server_retry = FakePlexServer(
        identities=[PlexIdentity(claimed=False, machine_id="m"), PlexIdentity(True, "m")]
    )
    manager._plex_server = plex_server_retry  # type: ignore[attr-defined]

    assert manager.retry_add() == "started"
    final = await _finish_add(manager)

    assert final.adding is None
    assert plex_tv.calls.count("claim_token") == 3  # the failed attempt's 2, plus one more retry
    retry_calls = engine.calls[calls_before_retry:]
    assert not any(call == ("inspect", ("plex",)) for call in retry_calls[:1])
    assert calls_before_retry > calls_before_add  # the first attempt really did touch Docker


async def test_cancel_clears_the_claim_file_and_keeps_plex_json(tmp_path: Path) -> None:
    plex_tv = FakePlexTv(claims=("claim-fresh1", "claim-fresh2"))
    plex_server = FakePlexServer(
        identities=[
            None,
            PlexIdentity(claimed=False, machine_id="m"),
            PlexIdentity(claimed=False, machine_id="m"),
        ]
    )
    manager, engine, settings, root = await _plex_added_to_sonarr(
        tmp_path,
        plex_tv=plex_tv,
        plex_server=plex_server,
        frames={"plex": [_absent("plex"), _absent("plex"), _running_container("plex")]},
        logs={"plex": _UNABLE_TO_CLAIM_LOG},
    )

    assert manager.add_app("plex") == "started"
    failed = await _finish_add(manager)
    assert failed.adding is not None
    assert failed.adding.state == "error"
    assert failed.adding.failure is not None
    assert failed.adding.failure.code == "plex_not_claimed"
    # The bring-up's own `finally` already cleared it - write a fresh one so
    # Cancel has something real of its own to clear.
    write_plex_claim(settings, root, "claim-still-here")
    assert _claim_file(settings, root).exists()

    assert await manager.cancel_add() is True

    assert manager.snapshot().adding is None
    assert not _claim_file(settings, root).exists()
    account = load_plex_account(settings.config_dir)
    assert account is not None
    assert account.token == "tok-secret-123"

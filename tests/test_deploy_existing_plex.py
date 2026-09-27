"""Tests for the deploy engine's own unmanaged branch: adding, cancelling
and disconnecting the owner's own, already-running Plex.

`existing-plex` never has a container - these tests are what proves that
promise at the engine level: no `remove_container`/`compose_up`/`inspect`
call ever names it, whatever the outcome. Every fixture here builds its own
small deploy directly (rather than reusing `test_deploy_add.py`'s
prowlarr/sonarr fixture), since a bring-up for this app needs a `PlexServer`
fake instead of a Docker one.
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path

import pytest
from test_deploy import _fresh_root, _install_state, _run_to_terminal, _settings, _StatefulEngine
from test_deploy_add import _finish_add

from marrquee.catalog import AppRule, get_app
from marrquee.config import Settings
from marrquee.deploy import DeployManager, Disconnect, FakeReadinessProbe
from marrquee.docker_client import ComposeResult
from marrquee.links import LinkCard, load_links, save_links
from marrquee.login import save_login
from marrquee.login_apply import FakeLoginApplier
from marrquee.plex import (
    ExistingPlex,
    FakePlexServer,
    PlexIdentity,
    PlexServer,
    load_existing_plex,
    save_existing_plex,
)
from marrquee.state import load_state, save_state

_BASE_URL = "http://192.168.1.20:32400"


def _record(
    *, machine_id: str = "m1", replaces_link: str | None = None, token: str = "tok-secret-999"
) -> ExistingPlex:
    return ExistingPlex(
        machine_id=machine_id,
        name="Den",
        base_url=_BASE_URL,
        port=32400,
        on_this_nas=True,
        token=token,
        folders={"movies": "unchecked", "tv": "unchecked"},
        sections={},
        replaces_link=replaces_link,
    )


async def _finale_with_apps(
    tmp_path: Path,
    app_ids: tuple[str, ...],
    *,
    plex_server: PlexServer | None = None,
) -> tuple[DeployManager, _StatefulEngine, Settings]:
    """A manager already at `finale` for `app_ids` - built directly rather
    than through `test_deploy_add`'s fixture, since a run that includes
    `existing-plex` needs its own `PlexServer` fake wired in from the start
    (a full deploy's bring-up runs before any test could patch it in).
    """
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(app_ids, root)
    save_state(settings.config_dir, install)
    save_login(settings.config_dir, "owner", "s3cret-password-1", honor_reset=None)
    engine = _StatefulEngine(app_ids)
    manager = DeployManager(
        settings,
        engine,
        probe=FakeReadinessProbe(default=True),
        login=FakeLoginApplier(),
        plex_server=plex_server if plex_server is not None else FakePlexServer(),
    )
    manager.start()
    history = await _run_to_terminal(manager)
    assert history[-1].phase == "finale"
    return manager, engine, settings


# --- Adding: no Docker call ever names existing-plex --------------------------


async def test_connecting_your_plex_never_touches_docker(tmp_path: Path) -> None:
    manager, engine, settings = await _finale_with_apps(tmp_path, ())
    save_existing_plex(settings.config_dir, _record())
    manager._plex_server = FakePlexServer(  # type: ignore[attr-defined]
        identities_by_url={_BASE_URL: PlexIdentity(True, "m1")}
    )

    calls_before = len(engine.calls)
    result = manager.add_app("existing-plex")
    assert result == "started"
    final = await _finish_add(manager)

    assert final.adding is None
    assert [app.app_id for app in final.apps] == ["existing-plex"]
    assert final.apps[0].state == "done"

    calls_during_add = engine.calls[calls_before:]
    assert not any("existing-plex" in call[1] for call in calls_during_add)
    assert not any(call[0] == "inspect" for call in calls_during_add)
    assert not any(call[0].startswith("compose_up") for call in calls_during_add)
    assert not any(call[0] == "connect_network" for call in calls_during_add)


async def test_a_wrong_machine_id_fails_with_plain_words_and_never_leaks_the_token(
    tmp_path: Path,
) -> None:
    manager, engine, settings = await _finale_with_apps(tmp_path, ())
    record = _record()
    save_existing_plex(settings.config_dir, record)
    manager._plex_server = FakePlexServer(  # type: ignore[attr-defined]
        identities_by_url={_BASE_URL: PlexIdentity(True, "not-the-same-machine")}
    )

    manager.add_app("existing-plex")
    final = await _finish_add(manager)

    assert final.adding is not None
    assert final.adding.state == "error"
    assert final.adding.failure is not None
    assert final.adding.failure.code == "existing_plex_unreachable"
    assert final.adding.compose_ran is False
    assert record.token not in final.adding.failure.technical
    assert record.token not in final.adding.failure.headline
    assert record.token not in final.adding.failure.what_to_do

    diagnostics = (settings.config_dir / "last-failure.txt").read_text()
    assert record.token not in diagnostics
    assert "existing_plex_unreachable" in diagnostics


async def test_no_saved_record_fails_the_same_way(tmp_path: Path) -> None:
    manager, engine, settings = await _finale_with_apps(tmp_path, ())

    manager.add_app("existing-plex")
    final = await _finish_add(manager)

    assert final.adding is not None
    assert final.adding.state == "error"
    assert final.adding.failure is not None
    assert final.adding.failure.code == "existing_plex_unreachable"


async def test_the_existing_plex_token_is_redacted_from_any_leaked_failure(tmp_path: Path) -> None:
    """`_redact` scrubs by VALUE, not by knowing which app's own failure it
    came from - proven here by leaking the saved existing-Plex token
    through an unrelated app's own compose output, the same "Docker could
    echo back a secret we set ourselves" case the account-token version of
    this test already covers.
    """
    manager, engine, settings = await _finale_with_apps(tmp_path, ())
    token = "tok-existing-plex-leak-1"
    save_existing_plex(settings.config_dir, _record(token=token))
    engine._compose_results["sonarr"] = ComposeResult(  # type: ignore[attr-defined]
        ok=False, exit_code=1, output=f"leaked token {token} right here"
    )

    manager.add_app("sonarr")
    final = await _finish_add(manager)

    assert final.adding is not None and final.adding.state == "error"
    assert final.adding.failure is not None
    assert token not in final.adding.failure.technical
    assert "<redacted-plex-token>" in final.adding.failure.technical

    diagnostics = (settings.config_dir / "last-failure.txt").read_text()
    assert token not in diagnostics
    assert "<redacted-plex-token>" in diagnostics


# --- The replaced link card goes only when the connect succeeds ---------------


async def test_a_replaced_link_card_goes_only_when_the_connect_succeeds(tmp_path: Path) -> None:
    manager, engine, settings = await _finale_with_apps(tmp_path, ())
    kept = LinkCard(id="1111111111111111", label="My NAS", url="http://nas.example/")
    replaced = LinkCard(id="2222222222222222", label="Old Plex", url="http://old-plex.example/")
    save_links(settings.config_dir, [kept, replaced])
    save_existing_plex(settings.config_dir, _record(replaces_link=replaced.id))
    manager._plex_server = FakePlexServer(  # type: ignore[attr-defined]
        identities_by_url={_BASE_URL: PlexIdentity(True, "m1")}
    )

    manager.add_app("existing-plex")
    final = await _finish_add(manager)

    assert final.adding is None
    assert final.apps[0].state == "done"

    remaining = load_links(settings.config_dir)
    assert [link.id for link in remaining] == [kept.id]

    record = load_existing_plex(settings.config_dir)
    assert record is not None
    assert record.replaces_link is None


async def test_a_failed_connect_never_touches_the_replaced_link_card(tmp_path: Path) -> None:
    manager, engine, settings = await _finale_with_apps(tmp_path, ())
    replaced = LinkCard(id="2222222222222222", label="Old Plex", url="http://old-plex.example/")
    save_links(settings.config_dir, [replaced])
    save_existing_plex(settings.config_dir, _record(replaces_link=replaced.id))
    manager._plex_server = FakePlexServer(  # type: ignore[attr-defined]
        identities_by_url={_BASE_URL: None}
    )

    manager.add_app("existing-plex")
    failed = await _finish_add(manager)
    assert failed.adding is not None and failed.adding.state == "error"

    ok = await manager.cancel_add()
    assert ok is True

    remaining = load_links(settings.config_dir)
    assert [link.id for link in remaining] == [replaced.id]
    assert load_existing_plex(settings.config_dir) is None


# --- Cancel's removal loop refuses existing-plex even with a stale flag -------


async def test_cancel_refuses_remove_container_for_existing_plex_even_with_a_stale_compose_ran(
    tmp_path: Path,
) -> None:
    """A genuine run never sets `compose_ran=True` for an unmanaged app, but
    `retry_add` copies whatever value the previous failed attempt carried
    forward without re-deriving it - so the removal loop needs its own
    per-app `managed` check, not just the outer `if current.compose_ran`
    gate, to keep a copied-forward `True` from ever reaching
    `remove_container("existing-plex")`.
    """
    manager, engine, settings = await _finale_with_apps(tmp_path, ())
    save_existing_plex(settings.config_dir, _record())
    manager._plex_server = FakePlexServer(  # type: ignore[attr-defined]
        identities_by_url={_BASE_URL: None}
    )

    manager.add_app("existing-plex")
    failed = await _finish_add(manager)
    assert failed.adding is not None and failed.adding.state == "error"
    assert failed.adding.compose_ran is False  # the honest value a real run produces

    stale = dataclasses.replace(failed.adding, compose_ran=True)
    manager._emit(manager._replace_finale(adding=stale))  # type: ignore[attr-defined]

    calls_before = len(engine.calls)
    ok = await manager.cancel_add()

    assert ok is True
    assert not any(call[0] == "remove_container" for call in engine.calls[calls_before:])
    assert load_existing_plex(settings.config_dir) is None


# --- Disconnecting -------------------------------------------------------------


async def test_disconnect_removes_only_marrquees_record(tmp_path: Path) -> None:
    manager, engine, settings = await _finale_with_apps(
        tmp_path,
        (),
        plex_server=FakePlexServer(identities_by_url={_BASE_URL: PlexIdentity(True, "m1")}),
    )
    save_existing_plex(settings.config_dir, _record())
    manager.add_app("existing-plex")
    await _finish_add(manager)
    assert load_state(settings.config_dir) is not None
    calls_before = len(engine.calls)

    result = await manager.disconnect("existing-plex")

    assert result == Disconnect("done", None)
    assert engine.calls[calls_before:] == []  # never touches Docker

    after = load_state(settings.config_dir)
    assert after is not None
    assert "existing-plex" not in after.app_ids
    assert load_existing_plex(settings.config_dir) is None
    assert [app.app_id for app in manager.snapshot().apps] == []


async def test_disconnect_is_refused_while_busy(tmp_path: Path) -> None:
    manager, engine, settings = await _finale_with_apps(tmp_path, ())
    save_existing_plex(settings.config_dir, _record())
    manager._plex_server = FakePlexServer(  # type: ignore[attr-defined]
        identities_by_url={_BASE_URL: PlexIdentity(True, "m1")}
    )
    manager.add_app("existing-plex")
    await _finish_add(manager)

    busy_task: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(1000))
    manager._task = busy_task  # type: ignore[attr-defined]
    try:
        result = await manager.disconnect("existing-plex")
        assert result.outcome == "busy"
    finally:
        busy_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await busy_task


async def test_disconnect_is_refused_for_a_managed_app(tmp_path: Path) -> None:
    manager, engine, settings = await _finale_with_apps(tmp_path, ("sonarr",))

    result = await manager.disconnect("sonarr")

    assert result.outcome == "not_disconnectable"
    after = load_state(settings.config_dir)
    assert after is not None
    assert "sonarr" in after.app_ids


async def test_disconnect_is_refused_for_an_uninstalled_app(tmp_path: Path) -> None:
    manager, engine, settings = await _finale_with_apps(tmp_path, ())

    result = await manager.disconnect("existing-plex")

    assert result.outcome == "not_installed"


async def test_disconnect_is_refused_while_another_app_needs_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A monkeypatched fixture rule (`needs_any(("existing-plex",))` on an
    already-installed app) stands in for Story 10's Seerr, which will carry
    the real one.
    """
    import marrquee.catalog as catalog_module

    patched = tuple(
        dataclasses.replace(
            app,
            rules=(
                *app.rules,
                AppRule(
                    kind="needs_any", app_ids=("existing-plex",), reason="Sonarr needs your Plex"
                ),
            ),
        )
        if app.id == "sonarr"
        else app
        for app in catalog_module.CATALOG
    )
    monkeypatch.setattr(catalog_module, "CATALOG", patched)

    settings = _settings(tmp_path)
    save_existing_plex(settings.config_dir, _record())
    manager, engine, settings = await _finale_with_apps(
        tmp_path,
        ("sonarr", "existing-plex"),
        plex_server=FakePlexServer(identities_by_url={_BASE_URL: PlexIdentity(True, "m1")}),
    )

    result = await manager.disconnect("existing-plex")

    assert result.outcome == "needed"
    assert result.needed_by == get_app("sonarr").name
    after = load_state(settings.config_dir)
    assert after is not None
    assert "existing-plex" in after.app_ids

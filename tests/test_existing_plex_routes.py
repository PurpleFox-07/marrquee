"""Tests for the connect-your-own-Plex route surface: `POST /plex/sign-in`
(`then=connect`), `GET /?panel=plex-servers`, `POST /plex/connect`, the
Manage pane, and `POST /plex/disconnect`.

Every server-list test runs offline through `FakePlexTv`/`FakePlexServer` -
the live plex.tv round trip and the real `/identity` answers stay PENDING
the owner's NAS. Reuses fixtures already proven elsewhere (`_settings`,
`_install_state`, `_finale_snapshot`, `_write_snapshot`, `_existing_plex_record`,
`_pane_html` from the Hub route tests; the state-token helpers from the
Plex sign-in round trip's own tests) so a connected Plex's Hub state, and
the mint-and-redeem state token, are each read exactly the way their own
tests already read them.
"""

from __future__ import annotations

import asyncio
import dataclasses
import html
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_hub_routes import (
    _existing_plex_record,
    _finale_snapshot,
    _install_state,
    _pane_html,
    _settings,
    _write_snapshot,
)
from test_plex_routes import _forward_url_from_location, _state_from_location

import marrquee.catalog as catalog_module
from marrquee import words
from marrquee.catalog import AppRule, get_app
from marrquee.config import Settings
from marrquee.deploy import AddStart, DeployManager
from marrquee.docker_client import DockerStatus, FakeDockerEngine
from marrquee.links import LinkCard, save_links
from marrquee.login import save_login
from marrquee.main import create_app
from marrquee.plex import (
    FakePlexServer,
    FakePlexTv,
    PlexConnection,
    PlexIdentity,
    PlexServer,
    PlexServerChoice,
    PlexServers,
    PlexTv,
    load_existing_plex,
    load_plex_account,
    save_existing_plex,
    save_plex_sign_in,
)
from marrquee.questions import load_answers
from marrquee.state import save_state

_BASE_URL = "http://192.168.1.20:32400"


def _client(
    settings: Settings,
    *,
    engine: FakeDockerEngine | None = None,
    plex_tv: PlexTv | None = None,
    plex_server: PlexServer | None = None,
) -> TestClient:
    if engine is None:
        engine = FakeDockerEngine(DockerStatus(connected=True, version="27.3.1"))
    app = create_app(settings=settings, engine=engine, plex_tv=plex_tv, plex_server=plex_server)
    return TestClient(app)


def _den(**overrides: object) -> PlexServerChoice:
    fields: dict[str, object] = {
        "machine_id": "m1",
        "name": "Den",
        "online": True,
        "https_required": False,
        "connections": (
            PlexConnection(
                protocol="http",
                address="192.168.1.20",
                port=32400,
                uri="https://192-168-1-20.x.plex.direct:32400",
                local=True,
                ipv6=False,
            ),
        ),
        "token": "server-token-1",
    }
    fields.update(overrides)
    return PlexServerChoice(**fields)  # type: ignore[arg-type]


# --- FIRST TEST: connect's own sign-in saves the account only ---------------


def test_sign_in_for_connect_saves_the_account_only_and_opens_the_servers_pane(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(()))
    _write_snapshot(settings, _finale_snapshot(()))
    fake_plex_tv = FakePlexTv(servers=PlexServers("ok", (_den(),)))
    client = _client(settings, plex_tv=fake_plex_tv)

    post_response = client.post("/plex/sign-in", data={"then": "connect"}, follow_redirects=False)
    assert post_response.status_code == 303
    location = post_response.headers["location"]
    assert re.search(r"pin", _forward_url_from_location(location), re.IGNORECASE) is None
    state = _state_from_location(location)

    get_response = client.get("/plex/signed-in", params={"state": state}, follow_redirects=False)
    assert get_response.status_code == 303
    assert get_response.headers["location"] == "/?panel=plex-servers#hub-panel"

    account = load_plex_account(settings.config_dir)
    assert account is not None
    assert account.token == "fake-plex-token"
    saved_answers = load_answers(settings.config_dir)
    assert "plex" not in saved_answers

    page = client.get("/", params={"panel": "plex-servers"})
    pane = html.unescape(_pane_html(page.text, "plex-servers"))
    assert 'data-plex-server="m1"' in pane
    assert '<form method="post" action="/plex/connect">' in pane
    assert '<input type="hidden" name="machine_id" value="m1">' in pane


def test_the_servers_pane_never_shows_an_address_or_a_token(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(()))
    _write_snapshot(settings, _finale_snapshot(()))
    save_plex_sign_in(settings.config_dir, "owner-account-token", "owner")
    fake_plex_tv = FakePlexTv(servers=PlexServers("ok", (_den(),)))
    client = _client(settings, plex_tv=fake_plex_tv)

    page = client.get("/", params={"panel": "plex-servers"})
    pane = html.unescape(_pane_html(page.text, "plex-servers"))

    assert "192.168.1.20" not in pane
    assert "32400" not in pane
    assert "plex.direct" not in pane
    assert "owner-account-token" not in pane
    assert "server-token-1" not in pane


def test_the_servers_pane_explains_signed_out_plex_tv_down_and_no_servers(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(()))
    _write_snapshot(settings, _finale_snapshot(()))

    # No saved account at all - nothing to ask plex.tv with.
    signed_out_client = _client(settings, plex_tv=FakePlexTv(servers=PlexServers("ok")))
    signed_out_page = signed_out_client.get("/", params={"panel": "plex-servers"})
    assert words.EXISTING_PLEX_SIGN_IN_AGAIN in html.unescape(signed_out_page.text)

    save_plex_sign_in(settings.config_dir, "owner-account-token", "owner")

    unreachable_client = _client(settings, plex_tv=FakePlexTv(servers=PlexServers("unreachable")))
    unreachable_page = unreachable_client.get("/", params={"panel": "plex-servers"})
    assert words.EXISTING_PLEX_LIST_FAILED in html.unescape(unreachable_page.text)

    empty_client = _client(settings, plex_tv=FakePlexTv(servers=PlexServers("ok", ())))
    empty_page = empty_client.get("/", params={"panel": "plex-servers"})
    assert words.EXISTING_PLEX_NO_SERVERS in html.unescape(empty_page.text)


# --- POST /plex/connect: saved only after the machine id matches -----------


async def test_connect_saves_only_after_the_machine_id_matches(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(()))
    _write_snapshot(settings, _finale_snapshot(()))
    save_plex_sign_in(settings.config_dir, "owner-account-token", "owner")
    save_login(settings.config_dir, "owner", "s3cret-password-1", honor_reset=None)
    fake_plex_tv = FakePlexTv(servers=PlexServers("ok", (_den(),)))
    plex_server = FakePlexServer(identities_by_url={_BASE_URL: PlexIdentity(True, "m1")})
    app = create_app(
        settings=settings,
        engine=FakeDockerEngine(DockerStatus(connected=True, version="27.3.1")),
        plex_tv=fake_plex_tv,
        plex_server=plex_server,
    )

    with TestClient(app) as client:
        response = client.post("/plex/connect", data={"machine_id": "m1"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    record = load_existing_plex(settings.config_dir)
    assert record is not None
    assert record.machine_id == "m1"
    assert record.name == "Den"
    assert record.base_url == _BASE_URL
    assert record.port == 32400
    assert record.token == "server-token-1"
    assert record.folders == {"movies": "unchecked", "tv": "unchecked"}
    assert record.sections == {}
    assert record.replaces_link is None


async def test_nothing_saved_when_unreachable(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(()))
    _write_snapshot(settings, _finale_snapshot(()))
    save_plex_sign_in(settings.config_dir, "owner-account-token", "owner")
    fake_plex_tv = FakePlexTv(servers=PlexServers("ok", (_den(),)))
    plex_server = FakePlexServer(identities_by_url={_BASE_URL: None})
    app = create_app(
        settings=settings,
        engine=FakeDockerEngine(DockerStatus(connected=True, version="27.3.1")),
        plex_tv=fake_plex_tv,
        plex_server=plex_server,
    )

    with TestClient(app) as client:
        response = client.post("/plex/connect", data={"machine_id": "m1"}, follow_redirects=False)

    assert response.status_code == 200
    assert words.existing_plex_unreachable("Den") in html.unescape(response.text)
    assert load_existing_plex(settings.config_dir) is None


async def test_a_missing_machine_id_shows_the_list_failed_problem(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(()))
    _write_snapshot(settings, _finale_snapshot(()))
    save_plex_sign_in(settings.config_dir, "owner-account-token", "owner")
    fake_plex_tv = FakePlexTv(servers=PlexServers("ok", (_den(),)))
    app = create_app(
        settings=settings,
        engine=FakeDockerEngine(DockerStatus(connected=True, version="27.3.1")),
        plex_tv=fake_plex_tv,
    )

    with TestClient(app) as client:
        response = client.post(
            "/plex/connect", data={"machine_id": "not-a-real-server"}, follow_redirects=False
        )

    assert response.status_code == 200
    assert words.EXISTING_PLEX_LIST_FAILED in html.unescape(response.text)
    assert load_existing_plex(settings.config_dir) is None


async def test_a_posted_replace_link_that_doesnt_match_the_server_is_ignored(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(()))
    _write_snapshot(settings, _finale_snapshot(()))
    save_plex_sign_in(settings.config_dir, "owner-account-token", "owner")
    save_login(settings.config_dir, "owner", "s3cret-password-1", honor_reset=None)
    unrelated = LinkCard(id="1234567890abcdef", label="My NAS", url="http://nas.example/")
    save_links(settings.config_dir, [unrelated])
    fake_plex_tv = FakePlexTv(servers=PlexServers("ok", (_den(),)))
    plex_server = FakePlexServer(identities_by_url={_BASE_URL: PlexIdentity(True, "m1")})
    app = create_app(
        settings=settings,
        engine=FakeDockerEngine(DockerStatus(connected=True, version="27.3.1")),
        plex_tv=fake_plex_tv,
        plex_server=plex_server,
    )

    with TestClient(app) as client:
        response = client.post(
            "/plex/connect",
            data={"machine_id": "m1", "replace_link": unrelated.id},
            follow_redirects=False,
        )

    assert response.status_code == 303
    record = load_existing_plex(settings.config_dir)
    assert record is not None
    assert record.replaces_link is None


async def test_a_posted_replace_link_that_matches_the_server_is_saved(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(()))
    _write_snapshot(settings, _finale_snapshot(()))
    save_plex_sign_in(settings.config_dir, "owner-account-token", "owner")
    save_login(settings.config_dir, "owner", "s3cret-password-1", honor_reset=None)
    matching = LinkCard(id="abcdef1234567890", label="Old Plex", url=_BASE_URL)
    save_links(settings.config_dir, [matching])
    fake_plex_tv = FakePlexTv(servers=PlexServers("ok", (_den(),)))
    plex_server = FakePlexServer(identities_by_url={_BASE_URL: PlexIdentity(True, "m1")})
    app = create_app(
        settings=settings,
        engine=FakeDockerEngine(DockerStatus(connected=True, version="27.3.1")),
        plex_tv=fake_plex_tv,
        plex_server=plex_server,
    )

    with TestClient(app) as client:
        response = client.post(
            "/plex/connect",
            data={"machine_id": "m1", "replace_link": matching.id},
            follow_redirects=False,
        )

    assert response.status_code == 303
    record = load_existing_plex(settings.config_dir)
    assert record is not None
    assert record.replaces_link == matching.id


# --- The Manage pane ---------------------------------------------------------


def test_manage_pane_explains_a_folder_plex_cant_see_with_the_host_path(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("existing-plex",)))
    _write_snapshot(settings, _finale_snapshot(("existing-plex",)))
    record = _existing_plex_record(folders={"movies": "added", "tv": "not_seen"})
    save_existing_plex(settings.config_dir, record)
    plex_server = FakePlexServer(identities_by_url={record.base_url: PlexIdentity(True, "m1")})
    client = _client(settings, plex_server=plex_server)

    page = client.get("/", params={"panel": "plex"})
    pane = html.unescape(_pane_html(page.text, "plex"))

    assert words.existing_plex_cant_see_help("/volume1/media/data/media") in pane
    assert 'action="/hub/apps/existing-plex/reconnect"' in pane
    assert 'action="/plex/disconnect"' in pane
    assert record.token not in pane


def test_manage_pane_shows_folder_ok_when_nothing_is_unseen(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("existing-plex",)))
    _write_snapshot(settings, _finale_snapshot(("existing-plex",)))
    record = _existing_plex_record(folders={"movies": "added", "tv": "already"})
    save_existing_plex(settings.config_dir, record)
    plex_server = FakePlexServer(identities_by_url={record.base_url: PlexIdentity(True, "m1")})
    client = _client(settings, plex_server=plex_server)

    page = client.get("/", params={"panel": "plex"})
    pane = html.unescape(_pane_html(page.text, "plex"))

    assert words.EXISTING_PLEX_FOLDER_OK in pane


# --- Disconnect: done, busy, needed -----------------------------------------


def test_disconnect_removes_it_and_redirects_home(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("existing-plex",)))
    _write_snapshot(settings, _finale_snapshot(("existing-plex",)))
    record = _existing_plex_record()
    save_existing_plex(settings.config_dir, record)
    plex_server = FakePlexServer(identities_by_url={record.base_url: PlexIdentity(True, "m1")})
    client = _client(settings, plex_server=plex_server)

    response = client.post("/plex/disconnect", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert load_existing_plex(settings.config_dir) is None


async def test_disconnect_while_busy_shows_the_busy_sentence(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("existing-plex",)))
    _write_snapshot(settings, _finale_snapshot(("existing-plex",)))
    save_existing_plex(settings.config_dir, _existing_plex_record())
    engine = FakeDockerEngine(DockerStatus(connected=True, version="27.3.1"))
    manager = DeployManager(settings, engine)
    app = create_app(settings=settings, engine=engine, manager=manager)

    busy_task: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(1000))
    manager._task = busy_task  # type: ignore[attr-defined]
    try:
        with TestClient(app) as client:
            response = client.post("/plex/disconnect", follow_redirects=False)
        assert response.status_code == 200
        assert words.EXISTING_PLEX_DISCONNECT_BUSY in html.unescape(response.text)
    finally:
        busy_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await busy_task
    assert load_existing_plex(settings.config_dir) is not None


def test_disconnect_refused_while_another_app_needs_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A monkeypatched fixture rule stands in for a later app that needs the
    owner's own Plex to stay connected, the same seam the deploy-engine level
    disconnect tests already exercise.
    """
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
    save_state(settings.config_dir, _install_state(("sonarr", "existing-plex")))
    _write_snapshot(settings, _finale_snapshot(("sonarr", "existing-plex")))
    record = _existing_plex_record()
    save_existing_plex(settings.config_dir, record)
    plex_server = FakePlexServer(identities_by_url={record.base_url: PlexIdentity(True, "m1")})
    client = _client(settings, plex_server=plex_server)

    response = client.post("/plex/disconnect", follow_redirects=False)

    assert response.status_code == 200
    assert words.existing_plex_disconnect_needed(get_app("sonarr").name) in html.unescape(
        response.text
    )
    assert load_existing_plex(settings.config_dir) is not None


# --- The JSON install endpoint refuses the unmanaged app --------------------


def test_the_json_install_endpoint_refuses_the_existing_plex(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = FakeDockerEngine(DockerStatus(connected=True, version="27.3.1"))
    manager = DeployManager(settings, engine)
    app = create_app(settings=settings, engine=engine, manager=manager)
    client = TestClient(app)

    response = client.post("/api/hub/apps/existing-plex/install", json={"answers": {}})

    assert response.status_code == 409
    assert response.json()["message"] == words.HUB_INSTALL_UNKNOWN


# --- Every new selector actually exists on the rendered page ----------------


def test_every_new_selector_exists_in_the_rendered_page(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("existing-plex",)))
    _write_snapshot(settings, _finale_snapshot(("existing-plex",)))
    record = _existing_plex_record()
    save_existing_plex(settings.config_dir, record)
    save_plex_sign_in(settings.config_dir, "owner-account-token", "owner")
    fake_plex_tv = FakePlexTv(servers=PlexServers("ok", (_den(),)))
    plex_server = FakePlexServer(identities_by_url={record.base_url: PlexIdentity(True, "m1")})
    client = _client(settings, plex_tv=fake_plex_tv, plex_server=plex_server)

    page = client.get("/", params={"panel": "plex-servers"}).text

    assert 'data-plex-server="m1"' in page
    assert 'data-panel-pane="plex"' in page
    assert 'data-managed="false"' in page


def test_the_manage_panel_closes_when_no_plex_is_connected(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    client = _client(settings)

    page = client.get("/", params={"panel": "plex"})

    assert 'data-panel-mode="closed"' in page.text


# --- The cross-site guard covers the new POSTs too --------------------------


def test_a_cross_site_post_to_plex_connect_is_refused(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(()))
    _write_snapshot(settings, _finale_snapshot(()))
    client = _client(settings)

    response = client.post(
        "/plex/connect",
        data={"machine_id": "m1"},
        headers={"Origin": "http://evil.example"},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert response.text == words.CROSS_SITE_REFUSED


# --- Mutation coverage: each arm of both connect guards, proven separately --
#
# Every test below isolates exactly ONE of a guard's `or`-joined conditions:
# the scenario is built so that condition is the ONLY one that would ever
# refuse the request - every other arm reads false on purpose - so a planted
# bug that drops or disables that one arm (and no other) flips the test from
# green to red.


def _spy_add_app(manager: DeployManager) -> list[str]:
    """Every `app_id` `manager.add_app` was actually called with, without
    changing what it does - a plain method-assign on the instance, the same
    "wrap, don't replace" shape a `unittest.mock.Mock(wraps=...)` gives, but
    with no new dependency.
    """
    calls: list[str] = []
    original = manager.add_app

    def _spy(app_id: str) -> AddStart:
        calls.append(app_id)
        return original(app_id)

    manager.add_app = _spy  # type: ignore[method-assign, assignment]
    return calls


# --- post_plex_sign_in's own connect guard (routes/plex.py ~:176-181) -------


async def test_sign_in_connect_guard_refuses_before_finale(tmp_path: Path) -> None:
    """Isolates `snapshot.phase != "finale"`: nothing is installed (so
    neither of the guard's other two arms can fire), and no deploy has ever
    run, so the manager's own resting snapshot reads `phase="ready"`.
    """
    settings = _settings(tmp_path)
    fake_plex_tv = FakePlexTv()
    client = _client(settings, plex_tv=fake_plex_tv)

    response = client.post("/plex/sign-in", data={"then": "connect"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert "create_pin" not in fake_plex_tv.calls


async def test_sign_in_connect_guard_refuses_when_already_installed(tmp_path: Path) -> None:
    """Isolates `EXISTING_PLEX_APP_ID in installed`: the deploy is at
    `finale` (so the phase arm reads false) with ONLY existing-plex
    installed, so the exclusion-reason arm reads false too (a lone
    existing-plex conflicts with nothing).
    """
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("existing-plex",)))
    _write_snapshot(settings, _finale_snapshot(("existing-plex",)))
    fake_plex_tv = FakePlexTv()
    client = _client(settings, plex_tv=fake_plex_tv)

    response = client.post("/plex/sign-in", data={"then": "connect"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert "create_pin" not in fake_plex_tv.calls


async def test_sign_in_connect_guard_refuses_when_jellyfin_excludes_it(tmp_path: Path) -> None:
    """Isolates `unavailable_reason(...) is not None`: the deploy is at
    `finale` (phase arm false) with only Jellyfin installed - existing-plex
    itself is not installed (the second arm false), but Jellyfin's own
    exclusion rule refuses it.
    """
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("jellyfin",)))
    _write_snapshot(settings, _finale_snapshot(("jellyfin",)))
    fake_plex_tv = FakePlexTv()
    client = _client(settings, plex_tv=fake_plex_tv)

    response = client.post("/plex/sign-in", data={"then": "connect"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert "create_pin" not in fake_plex_tv.calls


# --- post_plex_connect's own guard (routes/plex.py ~:277-283) ---------------


async def test_connect_post_guard_refuses_before_finale(tmp_path: Path) -> None:
    """Isolates `manager.snapshot().phase != "finale"` - nothing installed,
    no deploy ever run, so every other arm reads false.
    """
    settings = _settings(tmp_path)
    engine = FakeDockerEngine(DockerStatus(connected=True, version="27.3.1"))
    manager = DeployManager(settings, engine)
    add_calls = _spy_add_app(manager)
    fake_plex_tv = FakePlexTv(servers=PlexServers("ok", (_den(),)))
    app = create_app(settings=settings, engine=engine, manager=manager, plex_tv=fake_plex_tv)

    with TestClient(app) as client:
        response = client.post("/plex/connect", data={"machine_id": "m1"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert load_existing_plex(settings.config_dir) is None
    assert add_calls == []
    assert "servers" not in fake_plex_tv.calls


async def test_connect_post_guard_refuses_when_already_installed(tmp_path: Path) -> None:
    """Isolates `EXISTING_PLEX_APP_ID in installed` - finale, only
    existing-plex present, so neither the phase nor the unavailable-reason
    arm can fire.
    """
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("existing-plex",)))
    _write_snapshot(settings, _finale_snapshot(("existing-plex",)))
    engine = FakeDockerEngine(DockerStatus(connected=True, version="27.3.1"))
    manager = DeployManager(settings, engine)
    add_calls = _spy_add_app(manager)
    fake_plex_tv = FakePlexTv(servers=PlexServers("ok", (_den(),)))
    app = create_app(settings=settings, engine=engine, manager=manager, plex_tv=fake_plex_tv)

    with TestClient(app) as client:
        response = client.post("/plex/connect", data={"machine_id": "m1"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert load_existing_plex(settings.config_dir) is None
    assert add_calls == []
    assert "servers" not in fake_plex_tv.calls


async def test_connect_post_guard_refuses_when_jellyfin_excludes_it(tmp_path: Path) -> None:
    """Isolates `unavailable_reason(...) is not None` - finale, only
    Jellyfin present (existing-plex itself is not installed), so only
    Jellyfin's own exclusion rule can refuse this.
    """
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("jellyfin",)))
    _write_snapshot(settings, _finale_snapshot(("jellyfin",)))
    engine = FakeDockerEngine(DockerStatus(connected=True, version="27.3.1"))
    manager = DeployManager(settings, engine)
    add_calls = _spy_add_app(manager)
    fake_plex_tv = FakePlexTv(servers=PlexServers("ok", (_den(),)))
    app = create_app(settings=settings, engine=engine, manager=manager, plex_tv=fake_plex_tv)

    with TestClient(app) as client:
        response = client.post("/plex/connect", data={"machine_id": "m1"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert load_existing_plex(settings.config_dir) is None
    assert add_calls == []
    assert "servers" not in fake_plex_tv.calls


async def test_connect_post_guard_refuses_an_oversized_machine_id(tmp_path: Path) -> None:
    """Isolates `len(machine_id) > _MACHINE_ID_MAX_LENGTH` - finale, nothing
    installed (so the other three arms all read false), and a server whose
    `machine_id` genuinely IS the posted 65-character string, so only the
    length cap - never a lookup miss - is what refuses this.
    """
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(()))
    _write_snapshot(settings, _finale_snapshot(()))
    save_login(settings.config_dir, "owner", "s3cret-password-1", honor_reset=None)
    long_id = "m" * 65
    engine = FakeDockerEngine(DockerStatus(connected=True, version="27.3.1"))
    manager = DeployManager(settings, engine)
    add_calls = _spy_add_app(manager)
    fake_plex_tv = FakePlexTv(servers=PlexServers("ok", (_den(machine_id=long_id),)))
    plex_server = FakePlexServer(identities_by_url={_BASE_URL: PlexIdentity(True, long_id)})
    app = create_app(
        settings=settings,
        engine=engine,
        manager=manager,
        plex_tv=fake_plex_tv,
        plex_server=plex_server,
    )

    with TestClient(app) as client:
        response = client.post(
            "/plex/connect", data={"machine_id": long_id}, follow_redirects=False
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert load_existing_plex(settings.config_dir) is None
    assert add_calls == []
    assert "servers" not in fake_plex_tv.calls


async def test_connect_clears_the_record_when_add_app_is_refused(tmp_path: Path) -> None:
    """A connect whose own address match succeeds, but whose `add_app` is
    refused (busy, here) must still leave no `existing_plex.json` behind -
    proven by forcing `add_app` itself to return `"busy"` (a live task
    already occupies the manager) only AFTER `find_connection` has already
    matched, so the save-then-refuse ordering is exercised for real.
    """
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(()))
    _write_snapshot(settings, _finale_snapshot(()))
    save_login(settings.config_dir, "owner", "s3cret-password-1", honor_reset=None)
    save_plex_sign_in(settings.config_dir, "owner-account-token", "owner")
    fake_plex_tv = FakePlexTv(servers=PlexServers("ok", (_den(),)))
    plex_server = FakePlexServer(identities_by_url={_BASE_URL: PlexIdentity(True, "m1")})
    engine = FakeDockerEngine(DockerStatus(connected=True, version="27.3.1"))
    manager = DeployManager(settings, engine)
    app = create_app(
        settings=settings,
        engine=engine,
        manager=manager,
        plex_tv=fake_plex_tv,
        plex_server=plex_server,
    )

    busy_task: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(1000))
    manager._task = busy_task  # type: ignore[attr-defined]
    try:
        with TestClient(app) as client:
            response = client.post(
                "/plex/connect", data={"machine_id": "m1"}, follow_redirects=False
            )
        assert response.status_code == 200
        assert words.hub_install_busy(get_app("existing-plex").name) in html.unescape(response.text)
    finally:
        busy_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await busy_task

    assert load_existing_plex(settings.config_dir) is None

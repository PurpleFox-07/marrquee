"""Tests for the Plex sign-in round trip: `POST /plex/sign-in` and
`GET /plex/signed-in`, reachable from both the Hub's install panel and the
wizard's Plex step.

Everything here runs offline through `FakePlexTv` (Chunk 2) - no network,
no filesystem beyond `tmp_path`. The live plex.tv shapes stay PENDING the
owner's NAS; these tests only prove Marrquee's own side of the round trip:
the high-entropy state token it mints is the only thing the return leg ever
trusts (never the pin id, which plex.tv hands out as a small, guessable
number), the `forwardUrl` it builds always points back at its own address,
and a declined, expired or wrongly-keyed sign-in saves nothing.
"""

from __future__ import annotations

import dataclasses
import html
import re
import urllib.parse
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import marrquee.catalog as catalog_module
from marrquee import words
from marrquee.catalog import AppRule
from marrquee.config import Settings
from marrquee.deploy import AppProgress, DeploySnapshot
from marrquee.docker_client import DockerStatus, FakeDockerEngine
from marrquee.main import create_app
from marrquee.plex import (
    FakePlexTv,
    PlexPin,
    PlexTv,
    load_plex_account,
    plex_auth_url,
    plex_client_id,
)
from marrquee.questions import load_answers
from marrquee.routes.plex import PendingPlexSignIn, PlexSignInStore
from marrquee.state import write_json_atomic


def _settings(tmp_path: Path) -> Settings:
    return Settings(config_dir=tmp_path / "config", host_mount=tmp_path / "host")


def _client(settings: Settings, *, plex_tv: PlexTv | None = None) -> TestClient:
    engine = FakeDockerEngine(DockerStatus(connected=True, version="27.3.1"))
    app = create_app(settings=settings, engine=engine, plex_tv=plex_tv)
    return TestClient(app)


def _write_snapshot(settings: Settings, app_ids: tuple[str, ...]) -> None:
    apps = tuple(
        AppProgress(
            app_id=app_id,
            name=app_id,
            state="done",
            chip="Ready",
            line="Ready",
            note=None,
            port=None,
        )
        for app_id in app_ids
    )
    snapshot = DeploySnapshot(
        run_id="run-1",
        phase="finale",
        apps=apps,
        headline="Now showing",
        detail=None,
        failure=None,
        started_at="2026-09-26T00:00:00+00:00",
        finished_at="2026-09-26T00:05:00+00:00",
        wiring=(),
    )
    write_json_atomic(settings.config_dir / "deploy.json", dataclasses.asdict(snapshot))


def _forward_url_from_location(location: str) -> str:
    """The `forwardUrl` query parameter out of the `app.plex.tv/auth#?...`
    Location header - the `#?` makes this a fragment to `urlsplit`, so the
    query string is pulled out by hand instead.
    """
    query_string = location.split("#?", 1)[1]
    return urllib.parse.parse_qs(query_string)["forwardUrl"][0]


def _state_from_location(location: str) -> str:
    forward_url = _forward_url_from_location(location)
    query = urllib.parse.urlsplit(forward_url).query
    return urllib.parse.parse_qs(query)["state"][0]


# --- FIRST TEST: the round trip saves and reopens the panel -----------------


def test_sign_in_round_trip_saves_and_reopens_the_panel(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    fake_plex_tv = FakePlexTv(pin=PlexPin(id=7, code="abcd"), token="t", username="ryan")
    client = _client(settings, plex_tv=fake_plex_tv)
    client_id = plex_client_id(settings.config_dir)

    post_response = client.post("/plex/sign-in", data={"then": "hub"}, follow_redirects=False)

    assert post_response.status_code == 303
    location = post_response.headers["location"]
    # The pin id never appears anywhere in the URL the browser is sent to -
    # only the high-entropy state token does.
    assert "7" not in _forward_url_from_location(location).split("?", 1)[0]
    assert re.search(r"pin", _forward_url_from_location(location), re.IGNORECASE) is None

    state = _state_from_location(location)
    assert len(state) >= 40
    assert re.fullmatch(r"[A-Za-z0-9_-]+", state)

    forward_url = f"http://testserver/plex/signed-in?state={state}"
    assert location == plex_auth_url(client_id, "abcd", forward_url)

    get_response = client.get("/plex/signed-in", params={"state": state}, follow_redirects=False)

    assert get_response.status_code == 303
    assert get_response.headers["location"] == "/?panel=install#hub-panel"

    account = load_plex_account(settings.config_dir)
    assert account is not None
    assert account.token == "t"
    assert account.username == "ryan"
    saved = load_answers(settings.config_dir).get("plex", {})
    assert saved == {"plex_account": "ryan"}

    # Single use: the state was already taken, so a second visit finds nothing.
    second = client.get("/plex/signed-in", params={"state": state}, follow_redirects=False)
    assert second.status_code == 303
    assert second.headers["location"] == "/?panel=install&sign_in=failed#hub-panel"


def test_a_declined_sign_in_saves_nothing_and_shows_the_refusal(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings, plex_tv=FakePlexTv(pin=PlexPin(id=9, code="wxyz"), token=None))

    post_response = client.post("/plex/sign-in", data={"then": "hub"}, follow_redirects=False)
    state = _state_from_location(post_response.headers["location"])

    response = client.get("/plex/signed-in", params={"state": state}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/?panel=install&sign_in=failed#hub-panel"
    account = load_plex_account(settings.config_dir)
    assert account is None or account.token is None
    assert load_answers(settings.config_dir).get("plex", {}) == {}


def test_an_unknown_state_gets_the_hub_failure_redirect(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings, plex_tv=FakePlexTv())

    response = client.get(
        "/plex/signed-in", params={"state": "a-state-nobody-ever-minted"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/?panel=install&sign_in=failed#hub-panel"


def test_a_wrong_state_saves_nothing_and_the_real_one_still_works(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = _client(
        settings, plex_tv=FakePlexTv(pin=PlexPin(id=5, code="abcd"), token="t", username="ryan")
    )

    post_response = client.post("/plex/sign-in", data={"then": "hub"}, follow_redirects=False)
    real_state = _state_from_location(post_response.headers["location"])

    wrong_response = client.get(
        "/plex/signed-in", params={"state": real_state + "-guessed"}, follow_redirects=False
    )

    assert wrong_response.status_code == 303
    assert wrong_response.headers["location"] == "/?panel=install&sign_in=failed#hub-panel"
    account = load_plex_account(settings.config_dir)
    assert account is None or account.token is None

    # The wrong guess never consumed the real, still-pending entry.
    real_response = client.get(
        "/plex/signed-in", params={"state": real_state}, follow_redirects=False
    )
    assert real_response.status_code == 303
    assert real_response.headers["location"] == "/?panel=install#hub-panel"


def test_the_real_pin_id_in_the_query_string_is_never_honoured(tmp_path: Path) -> None:
    """plex.tv's own pin `id` is a small, guessable integer - the return leg
    must never accept it as a substitute for the high-entropy state token,
    even when an attacker (or a stale bookmark) supplies the genuine id.
    """
    settings = _settings(tmp_path)
    client = _client(
        settings, plex_tv=FakePlexTv(pin=PlexPin(id=42, code="abcd"), token="t", username="ryan")
    )

    post_response = client.post("/plex/sign-in", data={"then": "hub"}, follow_redirects=False)
    state = _state_from_location(post_response.headers["location"])

    pin_attempt = client.get("/plex/signed-in", params={"pin": "42"}, follow_redirects=False)

    assert pin_attempt.status_code == 303
    assert pin_attempt.headers["location"] == "/?panel=install&sign_in=failed#hub-panel"
    account = load_plex_account(settings.config_dir)
    assert account is None or account.token is None

    # The real state token, never consumed by the bogus attempt, still works.
    ok_response = client.get("/plex/signed-in", params={"state": state}, follow_redirects=False)
    assert ok_response.status_code == 303
    assert ok_response.headers["location"] == "/?panel=install#hub-panel"


def test_an_expired_state_gets_the_hub_failure_redirect() -> None:
    store = PlexSignInStore()
    store.put(
        "old-state-token",
        PendingPlexSignIn(pin=PlexPin(id=3, code="old0"), then="hub", apps=(), created=0.0),
    )

    from marrquee.routes.plex import PLEX_PIN_TTL_SECONDS

    assert store.take("old-state-token", PLEX_PIN_TTL_SECONDS + 1.0) is None
    # Single use: even well within the TTL, a state already taken is gone.
    store.put(
        "new-state-token",
        PendingPlexSignIn(pin=PlexPin(id=4, code="new1"), then="hub", apps=(), created=0.0),
    )
    assert store.take("new-state-token", 1.0) is not None
    assert store.take("new-state-token", 1.0) is None


def test_a_missing_state_never_raises(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings, plex_tv=FakePlexTv())

    response = client.get("/plex/signed-in", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/?panel=install&sign_in=failed#hub-panel"


# --- The wizard's own landing -------------------------------------------------


def test_the_wizard_round_trip_lands_on_the_plex_step_showing_signed_in_as(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    fake_plex_tv = FakePlexTv(pin=PlexPin(id=11, code="wiz1"), token="tok", username="owner")
    client = _client(settings, plex_tv=fake_plex_tv)

    post_response = client.post(
        "/plex/sign-in", data={"then": "wizard", "apps": "plex"}, follow_redirects=False
    )
    assert post_response.status_code == 303
    state = _state_from_location(post_response.headers["location"])

    get_response = client.get("/plex/signed-in", params={"state": state}, follow_redirects=False)
    assert get_response.status_code == 303
    assert get_response.headers["location"] == "/setup/questions/plex/sign-in?apps=plex"

    page = client.get(get_response.headers["location"])
    assert page.status_code == 200
    assert words.plex_signed_in_as("owner") in html.unescape(page.text)


def test_wizard_sign_in_is_blocked_once_the_hub_already_exists(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_snapshot(settings, ())
    client = _client(settings, plex_tv=FakePlexTv())

    response = client.post(
        "/plex/sign-in", data={"then": "wizard", "apps": "plex"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"


# --- Nothing happens for an already-installed or unavailable Plex -----------


def test_sign_in_for_an_already_installed_plex_does_nothing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_snapshot(settings, ("plex",))
    client = _client(settings, plex_tv=FakePlexTv())

    response = client.post("/plex/sign-in", data={"then": "hub"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert load_plex_account(settings.config_dir) is None


def test_sign_in_for_an_unavailable_plex_does_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    needs_something_absent = AppRule(
        kind="needs_any", app_ids=("something-else",), reason="needs something else first"
    )
    patched = tuple(
        dataclasses.replace(app, rules=(needs_something_absent,)) if app.id == "plex" else app
        for app in catalog_module.CATALOG
    )
    monkeypatch.setattr(catalog_module, "CATALOG", patched)

    settings = _settings(tmp_path)
    client = _client(settings, plex_tv=FakePlexTv())

    response = client.post("/plex/sign-in", data={"then": "hub"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert load_plex_account(settings.config_dir) is None


def test_an_unknown_then_value_goes_home(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings, plex_tv=FakePlexTv())

    response = client.post("/plex/sign-in", data={"then": "nonsense"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_a_failed_pin_create_redirects_without_saving(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings, plex_tv=FakePlexTv(pin=None))

    response = client.post("/plex/sign-in", data={"then": "hub"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/?panel=install&sign_in=failed#hub-panel"
    account = load_plex_account(settings.config_dir)
    assert account is None or account.token is None

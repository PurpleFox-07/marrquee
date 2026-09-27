"""Tests for the Seerr door: the client, the fake, sign-in resolution and
first-run setup.

Everything here runs offline: `FakeSeerrClient` and `httpx.MockTransport`
stand in for a real Seerr, and `tmp_path` stands in for the owner's drive.
The live v3.4.1 shapes are read from source, not from a running Seerr - see
the story's Pitch conditions - so these tests pin the documented shape and
leave the live answer PENDING the owner's NAS or CI.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from marrquee.catalog import get_app
from marrquee.jellyfin import JELLYFIN_PORT
from marrquee.login import save_login
from marrquee.plex import save_plex_sign_in
from marrquee.seerr import (
    SEERR_JELLYFIN,
    SEERR_PLEX,
    SEERR_REQUEST_PERMISSION,
    FakeSeerrClient,
    HttpSeerrClient,
    SeerrJellyfinSignIn,
    SeerrPlexSignIn,
    SeerrResponse,
    SeerrSetup,
    ensure_seerr_setup,
    seerr_base_url,
    seerr_sign_in,
    seerr_sign_in_kind,
)
from marrquee.wiring.steps import app_base_url


def _ok(payload: object = None, *, status: int = 200) -> SeerrResponse:
    return SeerrResponse(ok=True, status=status, payload=payload, detail=None)


def _fail(status: int, payload: object = None, *, detail: str | None = None) -> SeerrResponse:
    return SeerrResponse(ok=False, status=status, payload=payload, detail=detail)


# --- FIRST TEST: the Plex first run -------------------------------------------


async def test_plex_first_run_creates_the_admin_and_initializes() -> None:
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/public"): [_ok({"mediaServerType": 4, "initialized": False})],
            ("POST", "api/v1/auth/plex"): [_ok({"id": 1})],
            ("GET", "api/v1/settings/main"): [_ok({"apiKey": "k"})],
            ("POST", "api/v1/settings/main"): [_ok({})],
            ("POST", "api/v1/settings/initialize"): [_ok({"initialized": True})],
        }
    )

    result = await ensure_seerr_setup(
        client, "http://seerr:5055", "k", SeerrPlexSignIn(token="tok")
    )

    assert result == SeerrSetup(state="done", technical=None)
    assert client.calls == [
        ("GET", "api/v1/settings/public", (), False),
        ("POST", "api/v1/auth/plex", (), False),
        ("GET", "api/v1/settings/main", (), True),
        ("POST", "api/v1/settings/main", (), True),
        ("POST", "api/v1/settings/initialize", (), True),
    ]
    assert client.bodies[1] == {"authToken": "tok"}
    assert client.bodies[3] == {
        "localLogin": False,
        "newPlexLogin": True,
        "defaultPermissions": SEERR_REQUEST_PERMISSION,
    }


# --- the Jellyfin first run ----------------------------------------------------


async def test_jellyfin_first_run_posts_hostname_port_and_server_type() -> None:
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/public"): [_ok({"mediaServerType": 4, "initialized": False})],
            ("POST", "api/v1/auth/jellyfin"): [_ok({"id": 1})],
            ("GET", "api/v1/settings/main"): [_ok({})],
            ("POST", "api/v1/settings/main"): [_ok({})],
            ("POST", "api/v1/settings/initialize"): [_ok({"initialized": True})],
        }
    )
    sign_in = SeerrJellyfinSignIn(
        username="owner", password="s3cret-pass", hostname="10.0.0.5", port=JELLYFIN_PORT
    )

    result = await ensure_seerr_setup(client, "http://seerr:5055", "k", sign_in)

    assert result.state == "done"
    assert client.bodies[1] == {
        "username": "owner",
        "password": "s3cret-pass",
        "hostname": "10.0.0.5",
        "port": JELLYFIN_PORT,
        "useSsl": False,
        "urlBase": "",
        "serverType": SEERR_JELLYFIN,
    }
    assert "email" not in client.bodies[1]  # type: ignore[operator]


# --- already set up --------------------------------------------------------------


async def test_already_set_up_does_two_gets_and_nothing_else() -> None:
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/public"): [_ok({"mediaServerType": 2, "initialized": True})],
            ("GET", "api/v1/settings/main"): [_ok({})],
        }
    )
    sign_in = SeerrJellyfinSignIn(username="owner", password="pw", hostname="10.0.0.5")

    result = await ensure_seerr_setup(client, "http://seerr:5055", "k", sign_in)

    assert result == SeerrSetup(state="done", technical=None)
    assert [(method, path) for method, path, _params, _key in client.calls] == [
        ("GET", "api/v1/settings/public"),
        ("GET", "api/v1/settings/main"),
    ]


# --- the type mismatch is not_ours, everything else is refused/waiting ---------


async def test_other_server_type_is_not_ours_with_no_auth_call() -> None:
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/public"): [
                _ok({"mediaServerType": SEERR_PLEX, "initialized": True})
            ]
        }
    )
    sign_in = SeerrJellyfinSignIn(username="owner", password="pw", hostname="10.0.0.5")

    result = await ensure_seerr_setup(client, "http://seerr:5055", "k", sign_in)

    assert result == SeerrSetup(state="not_ours", technical=None)
    assert len(client.calls) == 1


async def test_auth_403_is_refused_not_not_ours() -> None:
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/public"): [_ok({"mediaServerType": 4, "initialized": False})],
            ("POST", "api/v1/auth/plex"): [_fail(403)],
        }
    )

    result = await ensure_seerr_setup(
        client, "http://seerr:5055", "k", SeerrPlexSignIn(token="tok")
    )

    assert result.state == "refused"


async def test_auth_503_is_waiting() -> None:
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/public"): [_ok({"mediaServerType": 4, "initialized": False})],
            ("POST", "api/v1/auth/plex"): [_fail(503)],
        }
    )

    result = await ensure_seerr_setup(
        client, "http://seerr:5055", "k", SeerrPlexSignIn(token="tok")
    )

    assert result.state == "waiting"


async def test_public_unreachable_is_waiting() -> None:
    client = FakeSeerrClient({("GET", "api/v1/settings/public"): [_fail(0)]})

    result = await ensure_seerr_setup(
        client, "http://seerr:5055", "k", SeerrPlexSignIn(token="tok")
    )

    assert result.state == "waiting"


async def test_public_with_a_bad_shape_is_waiting() -> None:
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/public"): [
                _ok({"mediaServerType": "nope", "initialized": False})
            ]
        }
    )

    result = await ensure_seerr_setup(
        client, "http://seerr:5055", "k", SeerrPlexSignIn(token="tok")
    )

    assert result.state == "waiting"


async def test_settings_main_403_is_refused() -> None:
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/public"): [_ok({"mediaServerType": 2, "initialized": True})],
            ("GET", "api/v1/settings/main"): [_fail(403)],
        }
    )
    sign_in = SeerrJellyfinSignIn(username="owner", password="pw", hostname="10.0.0.5")

    result = await ensure_seerr_setup(client, "http://seerr:5055", "k", sign_in)

    assert result.state == "refused"


async def test_initialize_answering_initialized_false_is_refused() -> None:
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/public"): [_ok({"mediaServerType": 2, "initialized": False})],
            ("GET", "api/v1/settings/main"): [_ok({})],
            ("POST", "api/v1/settings/main"): [_ok({})],
            ("POST", "api/v1/settings/initialize"): [_ok({"initialized": False})],
        }
    )
    sign_in = SeerrJellyfinSignIn(username="owner", password="pw", hostname="10.0.0.5")

    result = await ensure_seerr_setup(client, "http://seerr:5055", "k", sign_in)

    assert result.state == "refused"


# --- technical never carries a secret ------------------------------------------


async def test_technical_never_carries_the_api_key() -> None:
    secret_key = "sk-do-not-leak-0001"
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/public"): [_ok({"mediaServerType": 2, "initialized": True})],
            ("GET", "api/v1/settings/main"): [
                _fail(403, payload={"apiKey": secret_key}, detail="forbidden")
            ],
        }
    )
    sign_in = SeerrJellyfinSignIn(username="owner", password="pw", hostname="10.0.0.5")

    result = await ensure_seerr_setup(client, "http://seerr:5055", secret_key, sign_in)

    assert result.state == "refused"
    assert result.technical is not None
    assert secret_key not in result.technical


async def test_technical_never_carries_a_token_from_a_failing_auth_body() -> None:
    token = "plex-token-do-not-leak"
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/public"): [_ok({"mediaServerType": 4, "initialized": False})],
            ("POST", "api/v1/auth/plex"): [
                _fail(403, payload={"authToken": token}, detail="forbidden")
            ],
        }
    )

    result = await ensure_seerr_setup(
        client, "http://seerr:5055", "k", SeerrPlexSignIn(token=token)
    )

    assert result.state == "refused"
    assert result.technical is not None
    assert token not in result.technical
    # confirms the token really was in the request body, so the assertion
    # above is proving something.
    assert client.bodies[1] == {"authToken": token}


# --- FakeSeerrClient ------------------------------------------------------------


async def test_fake_seerr_client_raises_key_error_on_an_unscripted_call() -> None:
    client = FakeSeerrClient({})

    with pytest.raises(KeyError):
        await client.request("GET", "http://seerr:5055", "api/v1/settings/public", api_key=None)


async def test_fake_seerr_client_repeats_the_last_scripted_response() -> None:
    client = FakeSeerrClient({("GET", "api/v1/settings/public"): [_ok({"a": 1}), _ok({"a": 2})]})

    path = "api/v1/settings/public"
    first = await client.request("GET", "http://seerr:5055", path, api_key=None)
    second = await client.request("GET", "http://seerr:5055", path, api_key=None)
    third = await client.request("GET", "http://seerr:5055", path, api_key=None)

    assert first.payload == {"a": 1}
    assert second.payload == {"a": 2}
    assert third.payload == {"a": 2}


# --- sign-in resolution: seerr_sign_in_kind, seerr_sign_in ---------------------


def test_sign_in_kind_and_sign_in_for_plex(tmp_path: Path) -> None:
    save_plex_sign_in(tmp_path, "plex-tok", "owner")

    assert seerr_sign_in_kind(("plex",), tmp_path) == "plex"
    assert seerr_sign_in("plex", tmp_path, jellyfin_host=None) == SeerrPlexSignIn(token="plex-tok")


def test_sign_in_kind_for_existing_plex(tmp_path: Path) -> None:
    save_plex_sign_in(tmp_path, "plex-tok", "owner")

    assert seerr_sign_in_kind(("existing-plex",), tmp_path) == "plex"


def test_sign_in_kind_and_sign_in_for_jellyfin(tmp_path: Path) -> None:
    save_login(tmp_path, "owner", "s3cret-pass", honor_reset=None)

    assert seerr_sign_in_kind(("jellyfin",), tmp_path) == "jellyfin"
    result = seerr_sign_in("jellyfin", tmp_path, jellyfin_host="10.0.0.5")
    assert result == SeerrJellyfinSignIn(
        username="owner", password="s3cret-pass", hostname="10.0.0.5", port=JELLYFIN_PORT
    )


def test_sign_in_kind_is_none_without_a_media_server(tmp_path: Path) -> None:
    assert seerr_sign_in_kind((), tmp_path) is None
    assert seerr_sign_in_kind(("sonarr",), tmp_path) is None


def test_sign_in_kind_is_none_without_a_saved_login(tmp_path: Path) -> None:
    assert seerr_sign_in_kind(("jellyfin",), tmp_path) is None


def test_sign_in_kind_is_none_without_a_saved_plex_token(tmp_path: Path) -> None:
    assert seerr_sign_in_kind(("plex",), tmp_path) is None


def test_sign_in_is_none_without_a_jellyfin_host(tmp_path: Path) -> None:
    save_login(tmp_path, "owner", "s3cret-pass", honor_reset=None)

    assert seerr_sign_in("jellyfin", tmp_path, jellyfin_host=None) is None


def test_sign_in_is_none_without_a_saved_plex_account(tmp_path: Path) -> None:
    assert seerr_sign_in("plex", tmp_path, jellyfin_host=None) is None


# --- HttpSeerrClient -------------------------------------------------------------


async def test_http_seerr_client_sends_api_key_only_when_given() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    client = HttpSeerrClient(transport=httpx.MockTransport(handler))

    await client.request("GET", "http://seerr:5055", "api/v1/settings/public", api_key=None)
    await client.request("GET", "http://seerr:5055", "api/v1/settings/main", api_key="k")

    assert "x-api-key" not in seen[0].headers
    assert seen[1].headers["x-api-key"] == "k"
    assert seen[0].headers["accept"] == "application/json"
    assert "k" not in str(seen[1].url)


async def test_http_seerr_client_500_message_becomes_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "seerr fell over"})

    client = HttpSeerrClient(transport=httpx.MockTransport(handler))

    response = await client.request("GET", "http://seerr:5055", "api/v1/settings/main", api_key="k")

    assert response.ok is False
    assert response.status == 500
    assert response.detail == "seerr fell over"


async def test_http_seerr_client_connect_error_is_status_zero() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = HttpSeerrClient(transport=httpx.MockTransport(handler))

    response = await client.request(
        "GET", "http://seerr:5055", "api/v1/settings/public", api_key=None
    )

    assert response.ok is False
    assert response.status == 0
    assert response.detail is not None


# --- seerr_base_url --------------------------------------------------------------


def test_seerr_base_url_equals_app_base_url() -> None:
    assert seerr_base_url() == app_base_url(get_app("seerr"), ("seerr",))
    assert seerr_base_url() == "http://seerr:5055"

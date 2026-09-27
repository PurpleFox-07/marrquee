"""Tests for the Jellyfin door: the `MediaBrowser` header, `jellyfin.json`,
first-time setup, and the library parser.

Everything here runs offline: `httpx.MockTransport` stands in for a local
Jellyfin server and `tmp_path` stands in for the owner's drive. The live
12.1 shapes (the startup endpoints, the header, the key round trip) are
documented but unverified - see the story's Pitch conditions - so these
tests pin the documented shape and leave the live answer PENDING the
owner's NAS.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from marrquee import __version__
from marrquee.jellyfin import (
    JELLYFIN_KEY_APP_NAME,
    JELLYFIN_PORT,
    FakeJellyfinServer,
    HttpJellyfinServer,
    JellyfinLibrary,
    JellyfinRecord,
    JellyfinResponse,
    JellyfinSetup,
    ensure_jellyfin_admin,
    jellyfin_base_url,
    load_jellyfin,
    parse_virtual_folders,
    save_jellyfin,
)
from marrquee.login import SavedLogin

_LOGIN = SavedLogin(username="owner", generation=1, password="s3cret-pass")


def _ok(payload: object = None, *, status: int = 200) -> JellyfinResponse:
    return JellyfinResponse(ok=True, status=status, payload=payload, detail=None)


def _fail(status: int, payload: object = None) -> JellyfinResponse:
    return JellyfinResponse(ok=False, status=status, payload=payload, detail=None)


# --- FIRST TEST: the header shape --------------------------------------------


async def test_first_header_carries_the_mediabrowser_scheme_and_the_token() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    server = HttpJellyfinServer(transport=httpx.MockTransport(handler))

    await server.request("GET", "http://jellyfin:8096", "/System/Info", token="k")

    assert len(seen) == 1
    request = seen[0]
    assert request.headers["accept"] == "application/json"
    assert request.headers["authorization"] == (
        f'MediaBrowser Client="Marrquee", Device="Marrquee", DeviceId="marrquee", '
        f'Version="{__version__}", Token="k"'
    )
    assert "k" not in str(request.url)


async def test_header_without_a_token_has_no_token_part() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    server = HttpJellyfinServer(transport=httpx.MockTransport(handler))

    await server.request("GET", "http://jellyfin:8096", "/System/Info/Public", token=None)

    header = seen[0].headers["authorization"]
    assert header == (
        f'MediaBrowser Client="Marrquee", Device="Marrquee", DeviceId="marrquee", '
        f'Version="{__version__}"'
    )
    assert "Token=" not in header


# --- HttpJellyfinServer.request: never raises, payload shape -----------------


async def test_a_204_has_no_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    server = HttpJellyfinServer(transport=httpx.MockTransport(handler))

    response = await server.request("POST", "http://jellyfin:8096", "/Startup/Complete", token=None)

    assert response.ok is True
    assert response.status == 204
    assert response.payload is None


async def test_a_non_json_body_falls_back_to_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    server = HttpJellyfinServer(transport=httpx.MockTransport(handler))

    response = await server.request("GET", "http://jellyfin:8096", "/System/Info", token="k")

    assert response.payload == "not json"


async def test_a_connect_error_is_status_zero_never_a_crash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    server = HttpJellyfinServer(transport=httpx.MockTransport(handler))

    response = await server.request(
        "GET", "http://jellyfin:8096", "/System/Info/Public", token=None
    )

    assert response.ok is False
    assert response.status == 0
    assert response.detail is not None


async def test_a_failing_response_never_leaks_its_payload_into_detail() -> None:
    """A failure's body might echo back a token - `detail` must stay None
    so nothing above this door can build `technical` from it.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"leaked": "super-secret-session-token"})

    server = HttpJellyfinServer(transport=httpx.MockTransport(handler))

    response = await server.request("GET", "http://jellyfin:8096", "/Auth/Keys", token="s")

    assert response.ok is False
    assert response.detail is None


# --- jellyfin_base_url --------------------------------------------------------


def test_jellyfin_base_url_uses_the_jellyfin_port() -> None:
    assert jellyfin_base_url("192.168.1.5") == f"http://192.168.1.5:{JELLYFIN_PORT}"
    assert JELLYFIN_PORT == 8096


def test_jellyfin_base_url_brackets_an_ipv6_address() -> None:
    assert jellyfin_base_url("fe80::1") == f"http://[fe80::1]:{JELLYFIN_PORT}"


# --- jellyfin.json: never raises ----------------------------------------------


def test_load_jellyfin_is_none_with_no_file(tmp_path: Path) -> None:
    assert load_jellyfin(tmp_path) is None


def test_load_jellyfin_is_none_with_a_blank_file(tmp_path: Path) -> None:
    (tmp_path / "jellyfin.json").write_text("   ")
    assert load_jellyfin(tmp_path) is None


def test_load_jellyfin_is_none_with_garbage_text(tmp_path: Path) -> None:
    (tmp_path / "jellyfin.json").write_text("{not json")
    assert load_jellyfin(tmp_path) is None


def test_load_jellyfin_is_none_with_the_wrong_version(tmp_path: Path) -> None:
    (tmp_path / "jellyfin.json").write_text('{"version": 2, "admin_id": "u1", "api_key": "k"}')
    assert load_jellyfin(tmp_path) is None


def test_load_jellyfin_is_none_with_a_missing_field(tmp_path: Path) -> None:
    (tmp_path / "jellyfin.json").write_text('{"version": 1, "admin_id": "u1"}')
    assert load_jellyfin(tmp_path) is None


def test_load_jellyfin_is_none_when_a_directory_sits_where_the_file_should_be(
    tmp_path: Path,
) -> None:
    (tmp_path / "jellyfin.json").mkdir()
    assert load_jellyfin(tmp_path) is None


def test_save_then_load_jellyfin_round_trips(tmp_path: Path) -> None:
    save_jellyfin(tmp_path, "the-api-key", "u1")

    record = load_jellyfin(tmp_path)

    assert record == JellyfinRecord(admin_id="u1", api_key="the-api-key")


def test_jellyfin_record_never_shows_the_key_in_repr() -> None:
    record = JellyfinRecord(admin_id="u1", api_key="super-secret-key")
    assert "super-secret-key" not in repr(record)


# --- ensure_jellyfin_admin: fresh setup ---------------------------------------


async def test_fresh_setup_runs_in_order_and_saves_key_and_admin_id(tmp_path: Path) -> None:
    """The pre-flight walk: a brand-new Jellyfin, no saved key, finishes
    setup and saves the key it minted.
    """
    server = FakeJellyfinServer(
        {
            ("GET", "/System/Info/Public"): [_ok({"StartupWizardCompleted": False})],
            ("GET", "/Startup/User"): [_ok(status=200)],
            ("POST", "/Startup/User"): [_ok(status=204)],
            ("POST", "/Startup/Complete"): [_ok(status=204)],
            ("POST", "/Users/AuthenticateByName"): [
                _ok({"AccessToken": "s", "User": {"Id": "u1"}})
            ],
            ("GET", "/Auth/Keys"): [
                _ok({"Items": []}),
                _ok({"Items": [{"Id": 3, "AppName": "Marrquee", "AccessToken": "key"}]}),
            ],
            ("POST", "/Auth/Keys"): [_ok(status=204)],
            ("GET", "/System/Configuration"): [_ok({"ServerName": ""})],
            ("POST", "/System/Configuration"): [_ok(status=204)],
            ("POST", "/Sessions/Logout"): [_ok(status=204)],
        }
    )

    result = await ensure_jellyfin_admin(
        server, "http://jellyfin:8096", _LOGIN, tmp_path, server_name="Marrquee"
    )

    assert result == JellyfinSetup(state="done", technical=None)
    assert [(method, path, had_token) for method, path, _params, had_token in server.calls] == [
        ("GET", "/System/Info/Public", False),
        ("GET", "/Startup/User", False),
        ("POST", "/Startup/User", False),
        ("POST", "/Startup/Complete", False),
        ("POST", "/Users/AuthenticateByName", False),
        ("GET", "/Auth/Keys", True),
        ("POST", "/Auth/Keys", True),
        ("GET", "/Auth/Keys", True),
        ("GET", "/System/Configuration", True),
        ("POST", "/System/Configuration", True),
        ("POST", "/Sessions/Logout", True),
    ]
    assert load_jellyfin(tmp_path) == JellyfinRecord(admin_id="u1", api_key="key")
    key_create_call = next(c for c in server.calls if c[0] == "POST" and c[1] == "/Auth/Keys")
    assert key_create_call[2] == (("app", JELLYFIN_KEY_APP_NAME),)


async def test_resumed_setup_startup_user_403_carries_on_to_sign_in(tmp_path: Path) -> None:
    server = FakeJellyfinServer(
        {
            ("GET", "/System/Info/Public"): [_ok({"StartupWizardCompleted": False})],
            ("GET", "/Startup/User"): [_fail(403)],
            ("POST", "/Users/AuthenticateByName"): [
                _ok({"AccessToken": "s", "User": {"Id": "u1"}})
            ],
            ("GET", "/Auth/Keys"): [
                _ok({"Items": [{"Id": 5, "AppName": "Marrquee", "AccessToken": "key5"}]})
            ],
            ("GET", "/System/Configuration"): [_ok({"ServerName": "Den"})],
            ("POST", "/Sessions/Logout"): [_ok(status=204)],
        }
    )

    result = await ensure_jellyfin_admin(
        server, "http://jellyfin:8096", _LOGIN, tmp_path, server_name="Marrquee"
    )

    assert result == JellyfinSetup(state="done", technical=None)
    called = [(method, path) for method, path, _params, _token in server.calls]
    assert ("POST", "/Startup/User") not in called
    assert ("POST", "/Startup/Complete") not in called
    assert ("POST", "/Auth/Keys") not in called
    assert load_jellyfin(tmp_path) == JellyfinRecord(admin_id="u1", api_key="key5")


async def test_saved_key_still_works_done_with_exactly_two_calls(tmp_path: Path) -> None:
    save_jellyfin(tmp_path, "existing-key", "u1")
    server = FakeJellyfinServer(
        {
            ("GET", "/System/Info/Public"): [_ok({"StartupWizardCompleted": True})],
            ("GET", "/System/Info"): [_ok({})],
        }
    )

    result = await ensure_jellyfin_admin(
        server, "http://jellyfin:8096", _LOGIN, tmp_path, server_name="Marrquee"
    )

    assert result == JellyfinSetup(state="done", technical=None)
    assert len(server.calls) == 2
    assert [(m, p) for m, p, _params, _token in server.calls] == [
        ("GET", "/System/Info/Public"),
        ("GET", "/System/Info"),
    ]


async def test_stale_saved_key_signs_in_and_reuses_the_newest_key_no_post(
    tmp_path: Path,
) -> None:
    save_jellyfin(tmp_path, "old-key", "u1")
    server = FakeJellyfinServer(
        {
            ("GET", "/System/Info/Public"): [_ok({"StartupWizardCompleted": True})],
            ("GET", "/System/Info"): [_fail(401)],
            ("POST", "/Users/AuthenticateByName"): [
                _ok({"AccessToken": "s2", "User": {"Id": "u1"}})
            ],
            ("GET", "/Auth/Keys"): [
                _ok({"Items": [{"Id": 2, "AppName": "Marrquee", "AccessToken": "new-key"}]})
            ],
            ("GET", "/System/Configuration"): [_ok({"ServerName": "Den"})],
            ("POST", "/Sessions/Logout"): [_ok(status=204)],
        }
    )

    result = await ensure_jellyfin_admin(
        server, "http://jellyfin:8096", _LOGIN, tmp_path, server_name="Marrquee"
    )

    assert result == JellyfinSetup(state="done", technical=None)
    called = [(method, path) for method, path, _params, _token in server.calls]
    assert ("POST", "/Auth/Keys") not in called
    assert load_jellyfin(tmp_path) == JellyfinRecord(admin_id="u1", api_key="new-key")


async def test_reuses_the_highest_id_marrquee_key_among_several(tmp_path: Path) -> None:
    """Two "Marrquee" keys can exist (a prior run that never saved before
    losing power, say) - the newest one, not just any one, must be reused.
    """
    server = FakeJellyfinServer(
        {
            ("GET", "/System/Info/Public"): [_ok({"StartupWizardCompleted": True})],
            ("POST", "/Users/AuthenticateByName"): [
                _ok({"AccessToken": "s", "User": {"Id": "u1"}})
            ],
            ("GET", "/Auth/Keys"): [
                _ok(
                    {
                        "Items": [
                            {"Id": 1, "AppName": "Marrquee", "AccessToken": "oldest-key"},
                            {"Id": 9, "AppName": "Marrquee", "AccessToken": "newest-key"},
                            {"Id": 5, "AppName": "Marrquee", "AccessToken": "middle-key"},
                            {"Id": 42, "AppName": "SomeoneElse", "AccessToken": "not-ours"},
                        ]
                    }
                )
            ],
            ("GET", "/System/Configuration"): [_ok({"ServerName": "Den"})],
            ("POST", "/Sessions/Logout"): [_ok(status=204)],
        }
    )

    result = await ensure_jellyfin_admin(
        server, "http://jellyfin:8096", _LOGIN, tmp_path, server_name="Marrquee"
    )

    assert result == JellyfinSetup(state="done", technical=None)
    assert load_jellyfin(tmp_path) == JellyfinRecord(admin_id="u1", api_key="newest-key")


async def test_completed_but_login_refused_is_not_ours_and_jellyfin_json_untouched(
    tmp_path: Path,
) -> None:
    server = FakeJellyfinServer(
        {
            ("GET", "/System/Info/Public"): [_ok({"StartupWizardCompleted": True})],
            ("POST", "/Users/AuthenticateByName"): [_fail(401)],
        }
    )

    result = await ensure_jellyfin_admin(
        server, "http://jellyfin:8096", _LOGIN, tmp_path, server_name="Marrquee"
    )

    assert result == JellyfinSetup(state="not_ours", technical=None)
    assert load_jellyfin(tmp_path) is None
    assert len(server.calls) == 2


async def test_503_while_starting_is_waiting(tmp_path: Path) -> None:
    server = FakeJellyfinServer({("GET", "/System/Info/Public"): [_fail(503)]})

    result = await ensure_jellyfin_admin(
        server, "http://jellyfin:8096", _LOGIN, tmp_path, server_name="Marrquee"
    )

    assert result.state == "waiting"
    assert load_jellyfin(tmp_path) is None
    assert len(server.calls) == 1


@pytest.mark.parametrize("status", [500, 503])
async def test_a_5xx_partway_through_setup_is_waiting_not_refused(
    tmp_path: Path, status: int
) -> None:
    """`/System/Info/Public` already succeeded - the server is reachable -
    so a later 500/503 is a transient hiccup worth trying again next
    tick, never a hard "no".
    """
    server = FakeJellyfinServer(
        {
            ("GET", "/System/Info/Public"): [_ok({"StartupWizardCompleted": False})],
            ("GET", "/Startup/User"): [_fail(status)],
        }
    )

    result = await ensure_jellyfin_admin(
        server, "http://jellyfin:8096", _LOGIN, tmp_path, server_name="Marrquee"
    )

    assert result.state == "waiting"
    assert load_jellyfin(tmp_path) is None


async def test_a_400_partway_through_setup_is_refused(tmp_path: Path) -> None:
    """The contrast case: a considered 400 at the exact same step is a
    real refusal, never mistaken for "still starting".
    """
    server = FakeJellyfinServer(
        {
            ("GET", "/System/Info/Public"): [_ok({"StartupWizardCompleted": False})],
            ("GET", "/Startup/User"): [_fail(400)],
        }
    )

    result = await ensure_jellyfin_admin(
        server, "http://jellyfin:8096", _LOGIN, tmp_path, server_name="Marrquee"
    )

    assert result.state == "refused"
    assert load_jellyfin(tmp_path) is None


async def test_server_name_set_only_when_empty(tmp_path: Path) -> None:
    server = FakeJellyfinServer(
        {
            ("GET", "/System/Info/Public"): [_ok({"StartupWizardCompleted": False})],
            ("GET", "/Startup/User"): [_ok(status=200)],
            ("POST", "/Startup/User"): [_ok(status=204)],
            ("POST", "/Startup/Complete"): [_ok(status=204)],
            ("POST", "/Users/AuthenticateByName"): [
                _ok({"AccessToken": "s", "User": {"Id": "u1"}})
            ],
            ("GET", "/Auth/Keys"): [
                _ok({"Items": []}),
                _ok({"Items": [{"Id": 1, "AppName": "Marrquee", "AccessToken": "key"}]}),
            ],
            ("POST", "/Auth/Keys"): [_ok(status=204)],
            ("GET", "/System/Configuration"): [_ok({"ServerName": "Den"})],
            ("POST", "/Sessions/Logout"): [_ok(status=204)],
        }
    )

    result = await ensure_jellyfin_admin(
        server, "http://jellyfin:8096", _LOGIN, tmp_path, server_name="Marrquee"
    )

    assert result == JellyfinSetup(state="done", technical=None)
    called = [(method, path) for method, path, _params, _token in server.calls]
    assert ("POST", "/System/Configuration") not in called


async def test_technical_never_contains_the_session_token(tmp_path: Path) -> None:
    server = FakeJellyfinServer(
        {
            ("GET", "/System/Info/Public"): [_ok({"StartupWizardCompleted": True})],
            ("POST", "/Users/AuthenticateByName"): [
                _ok({"AccessToken": "s", "User": {"Id": "u1"}})
            ],
            ("GET", "/Auth/Keys"): [
                _fail(400, {"error": "nope", "hint": "super-secret-token-xyz"})
            ],
        }
    )

    result = await ensure_jellyfin_admin(
        server, "http://jellyfin:8096", _LOGIN, tmp_path, server_name="Marrquee"
    )

    assert result.state == "refused"
    assert result.technical is not None
    assert "super-secret-token-xyz" not in result.technical
    assert "s" != result.technical  # sanity: the session token itself never leaks either


async def test_no_secret_ever_travels_as_a_query_param(tmp_path: Path) -> None:
    server = FakeJellyfinServer(
        {
            ("GET", "/System/Info/Public"): [_ok({"StartupWizardCompleted": False})],
            ("GET", "/Startup/User"): [_ok(status=200)],
            ("POST", "/Startup/User"): [_ok(status=204)],
            ("POST", "/Startup/Complete"): [_ok(status=204)],
            ("POST", "/Users/AuthenticateByName"): [
                _ok({"AccessToken": "session-secret", "User": {"Id": "u1"}})
            ],
            ("GET", "/Auth/Keys"): [
                _ok({"Items": []}),
                _ok({"Items": [{"Id": 1, "AppName": "Marrquee", "AccessToken": "final-key"}]}),
            ],
            ("POST", "/Auth/Keys"): [_ok(status=204)],
            ("GET", "/System/Configuration"): [_ok({"ServerName": "Den"})],
            ("POST", "/Sessions/Logout"): [_ok(status=204)],
        }
    )

    await ensure_jellyfin_admin(
        server, "http://jellyfin:8096", _LOGIN, tmp_path, server_name="Marrquee"
    )

    secrets = {_LOGIN.password, "session-secret", "final-key"}
    for _method, _path, params, _had_token in server.calls:
        for _name, value in params:
            assert value not in secrets


async def test_ensure_jellyfin_admin_never_raises_on_a_broken_payload(tmp_path: Path) -> None:
    server = FakeJellyfinServer(
        {
            ("GET", "/System/Info/Public"): [_ok({"StartupWizardCompleted": True})],
            ("POST", "/Users/AuthenticateByName"): [_ok(["not", "a", "dict"])],
        }
    )

    result = await ensure_jellyfin_admin(
        server, "http://jellyfin:8096", _LOGIN, tmp_path, server_name="Marrquee"
    )

    assert result.state == "refused"


# --- parse_virtual_folders -----------------------------------------------------


def test_parse_virtual_folders_reads_name_type_locations_and_id() -> None:
    payload = [
        {
            "Name": "Films",
            "CollectionType": "movies",
            "Locations": ["/data/media/movies"],
            "ItemId": "1",
        }
    ]

    libraries = parse_virtual_folders(payload)

    assert libraries == (
        JellyfinLibrary(
            name="Films",
            collection_type="movies",
            locations=("/data/media/movies",),
            item_id="1",
        ),
    )


def test_parse_virtual_folders_lowercases_collection_type() -> None:
    payload = [{"Name": "Films", "CollectionType": "Movies", "Locations": [], "ItemId": "1"}]

    (library,) = parse_virtual_folders(payload)

    assert library.collection_type == "movies"


def test_parse_virtual_folders_skips_items_missing_name_or_item_id() -> None:
    payload = [
        {"CollectionType": "movies", "Locations": [], "ItemId": "1"},
        {"Name": "Films", "CollectionType": "movies", "Locations": []},
        {
            "Name": "TV Shows",
            "CollectionType": "tvshows",
            "Locations": ["/data/media/tv"],
            "ItemId": "2",
        },
    ]

    libraries = parse_virtual_folders(payload)

    assert len(libraries) == 1
    assert libraries[0].item_id == "2"


def test_parse_virtual_folders_is_empty_for_a_non_list() -> None:
    assert parse_virtual_folders({"not": "a list"}) == ()
    assert parse_virtual_folders(None) == ()


# --- FakeJellyfinServer: unscripted raises, last repeats ----------------------


async def test_fake_jellyfin_server_raises_on_an_unscripted_call() -> None:
    server = FakeJellyfinServer({})
    with pytest.raises(KeyError):
        await server.request("GET", "http://jellyfin:8096", "/System/Info", token=None)


async def test_fake_jellyfin_server_repeats_its_last_scripted_response() -> None:
    server = FakeJellyfinServer({("GET", "/x"): [_ok(status=200), _ok(status=201)]})

    first = await server.request("GET", "http://jellyfin:8096", "/x", token=None)
    second = await server.request("GET", "http://jellyfin:8096", "/x", token=None)
    third = await server.request("GET", "http://jellyfin:8096", "/x", token=None)

    assert (first.status, second.status, third.status) == (200, 201, 201)

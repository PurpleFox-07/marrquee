"""Tests for Seerr's own wiring steps: its connection to Plex or Jellyfin,
every movie/TV library it can see there, and its Sonarr/Radarr entries.

Everything here runs offline through `FakeSeerrClient` - no network, no
Docker, no real Seerr. `plan_wiring`'s own placement of Seerr's tasks is
tested in `test_wiring_engine.py`, alongside the rest of the engine.
"""

from __future__ import annotations

from marrquee import words
from marrquee.catalog import CatalogApp, get_app, require_port
from marrquee.seerr import FakeSeerrClient, SeerrResponse
from marrquee.state import STATE_VERSION, InstallState
from marrquee.wiring.seerr_steps import (
    SeerrPlexTarget,
    ensure_seerr_arr,
    ensure_seerr_media_server,
    refresh_seerr_profiles,
    seerr_plex_target,
)

_SEERR_BASE_URL = "http://seerr:5055"


def _install_state(app_ids: tuple[str, ...]) -> InstallState:
    return InstallState(
        version=STATE_VERSION,
        storage_root="/volume1/media",
        app_ids=app_ids,
        api_keys={app_id: f"{app_id}-key" for app_id in app_ids},
        puid=1000,
        pgid=1000,
        umask="002",
        timezone="Etc/UTC",
        created="2026-09-27T00:00:00+00:00",
    )


def _ok(payload: object = None, *, status: int = 200) -> SeerrResponse:
    return SeerrResponse(ok=True, status=status, payload=payload, detail=None)


def _fail(status: int, payload: object = None, *, detail: str | None = None) -> SeerrResponse:
    return SeerrResponse(ok=False, status=status, payload=payload, detail=detail)


def _expected_arr_body(
    app: CatalogApp,
    key: str,
    *,
    profile_id: int,
    profile_name: str,
    folder: str,
    owner_host: str | None,
) -> dict[str, object]:
    """The whole POST body a brand-new arr entry gets - mirrors, but never
    calls, `seerr_steps.py`'s own private builder, so a test that uses this
    stays an independent check on the contract's shape.
    """
    body: dict[str, object] = {
        "name": app.name,
        "hostname": app.id,
        "port": require_port(app),
        "apiKey": key,
        "useSsl": False,
        "baseUrl": "",
        "activeProfileId": profile_id,
        "activeProfileName": profile_name,
        "activeDirectory": folder,
        "tags": [],
        "is4k": False,
        "isDefault": True,
        "externalUrl": f"http://{owner_host}:{require_port(app)}" if owner_host else "",
        "syncEnabled": True,
        "preventSearch": False,
        "tagRequests": False,
        "overrideRule": [],
    }
    if app.id == "radarr":
        body["minimumAvailability"] = "released"
    else:
        body.update(
            {
                "seriesType": "standard",
                "animeSeriesType": "anime",
                "enableSeasonFolders": True,
                "monitorNewItems": "all",
            }
        )
    return body


# --- seerr_plex_target: pure address parsing ----------------------------------


def test_plex_target_from_https_plex_direct_address() -> None:
    target = seerr_plex_target("https://1-2-3-4.abc123.plex.direct:32400")

    assert target == SeerrPlexTarget(ip="1-2-3-4.abc123.plex.direct", port=32400, use_ssl=True)


def test_plex_target_ipv6_host_is_bracketed() -> None:
    target = seerr_plex_target("http://[2001:db8::1]:32400")

    assert target == SeerrPlexTarget(ip="[2001:db8::1]", port=32400, use_ssl=False)


def test_plex_target_defaults_the_port_by_scheme() -> None:
    http_target = seerr_plex_target("http://192.168.1.20")
    https_target = seerr_plex_target("https://192.168.1.20")

    assert http_target is not None and http_target.port == 80
    assert https_target is not None and https_target.port == 443


def test_plex_target_rejects_anything_that_is_not_a_url() -> None:
    assert seerr_plex_target("ftp://example.com:32400") is None
    assert seerr_plex_target("not a url at all") is None


# --- ensure_seerr_media_server: the Plex connection ---------------------------


async def test_plex_connection_left_alone_when_it_already_matches() -> None:
    target = SeerrPlexTarget(ip="172.18.0.1", port=32400, use_ssl=False)
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/plex"): [
                _ok(
                    {
                        "ip": "172.18.0.1",
                        "port": 32400,
                        "useSsl": False,
                        "machineId": "m1",
                        "libraries": [],
                    }
                )
            ],
            ("GET", "api/v1/settings/plex/library"): [_ok([])],
        }
    )

    outcome = await ensure_seerr_media_server(
        client,
        _SEERR_BASE_URL,
        "k",
        "plex",
        plex_target=target,
        expected_machine_id="m1",
        owner_host=None,
        name="Plex",
    )

    assert outcome.state == "skipped"
    assert outcome.note == words.seerr_note_no_libraries("Plex")
    assert [call[0] for call in client.calls] == ["GET", "GET"]


async def test_plex_connection_written_when_it_differs() -> None:
    target = SeerrPlexTarget(ip="172.18.0.1", port=32400, use_ssl=False)
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/plex"): [
                _ok({"ip": "", "port": 32400, "useSsl": False, "machineId": "", "libraries": []})
            ],
            ("POST", "api/v1/settings/plex"): [_ok({"machineId": "m1"})],
            ("GET", "api/v1/settings/plex/library"): [_ok([])],
        }
    )

    outcome = await ensure_seerr_media_server(
        client,
        _SEERR_BASE_URL,
        "k",
        "plex",
        plex_target=target,
        expected_machine_id="m1",
        owner_host=None,
        name="Plex",
    )

    assert outcome.state == "skipped"  # the connection wrote fine; no libraries to enable
    assert client.bodies[1] == {"ip": "172.18.0.1", "port": 32400, "useSsl": False}


async def test_plex_machine_id_mismatch_errors() -> None:
    target = SeerrPlexTarget(ip="172.18.0.1", port=32400, use_ssl=False)
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/plex"): [
                _ok({"ip": "", "port": 32400, "useSsl": False, "machineId": "", "libraries": []})
            ],
            ("POST", "api/v1/settings/plex"): [_ok({"machineId": "m2"})],
        }
    )

    outcome = await ensure_seerr_media_server(
        client,
        _SEERR_BASE_URL,
        "k",
        "plex",
        plex_target=target,
        expected_machine_id="m1",
        owner_host=None,
        name="Plex",
    )

    assert outcome.state == "error"
    assert outcome.note == words.SEERR_FAILURE_WRONG_PLEX


async def test_plex_no_target_is_an_honest_error() -> None:
    outcome = await ensure_seerr_media_server(
        FakeSeerrClient({}),
        _SEERR_BASE_URL,
        "k",
        "plex",
        plex_target=None,
        expected_machine_id=None,
        owner_host=None,
        name="Plex",
    )

    assert outcome.state == "error"
    assert outcome.note == words.wiring_failure_unreachable("Plex")


# --- ensure_seerr_media_server: the Jellyfin connection -----------------------


async def test_jellyfin_connection_sets_external_hostname_only_with_a_host() -> None:
    target = SeerrPlexTarget(ip="172.18.0.1", port=8096, use_ssl=False)
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/jellyfin"): [
                _ok({"ip": "", "port": 0, "useSsl": True, "urlBase": "x", "libraries": []})
            ],
            ("POST", "api/v1/settings/jellyfin"): [_ok({})],
            ("GET", "api/v1/settings/jellyfin/library"): [_ok([])],
        }
    )

    await ensure_seerr_media_server(
        client,
        _SEERR_BASE_URL,
        "k",
        "jellyfin",
        plex_target=target,
        expected_machine_id=None,
        owner_host="nas.local",
        name="Jellyfin",
    )

    assert client.bodies[1] == {
        "ip": "172.18.0.1",
        "port": 8096,
        "useSsl": False,
        "urlBase": "",
        "externalHostname": "http://nas.local:8096",
    }


async def test_jellyfin_connection_omits_external_hostname_without_a_host() -> None:
    target = SeerrPlexTarget(ip="172.18.0.1", port=8096, use_ssl=False)
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/jellyfin"): [
                _ok({"ip": "", "port": 0, "useSsl": True, "urlBase": "x", "libraries": []})
            ],
            ("POST", "api/v1/settings/jellyfin"): [_ok({})],
            ("GET", "api/v1/settings/jellyfin/library"): [_ok([])],
        }
    )

    await ensure_seerr_media_server(
        client,
        _SEERR_BASE_URL,
        "k",
        "jellyfin",
        plex_target=target,
        expected_machine_id=None,
        owner_host=None,
        name="Jellyfin",
    )

    assert client.bodies[1] == {"ip": "172.18.0.1", "port": 8096, "useSsl": False, "urlBase": ""}


# --- ensure_seerr_media_server: libraries -------------------------------------


async def test_libraries_unchanged_set_is_already_connected() -> None:
    target = SeerrPlexTarget(ip="172.18.0.1", port=32400, use_ssl=False)
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/plex"): [
                _ok(
                    {
                        "ip": "172.18.0.1",
                        "port": 32400,
                        "useSsl": False,
                        "machineId": "m1",
                        "libraries": [{"id": "a", "enabled": True}],
                    }
                )
            ],
            ("GET", "api/v1/settings/plex/library"): [
                _ok([{"id": "a"}]),
                _ok([{"id": "a", "enabled": True}]),
            ],
        }
    )

    outcome = await ensure_seerr_media_server(
        client,
        _SEERR_BASE_URL,
        "k",
        "plex",
        plex_target=target,
        expected_machine_id="m1",
        owner_host=None,
        name="Plex",
    )

    assert outcome.state == "done"
    assert outcome.changed is False
    assert outcome.note == words.WIRING_NOTE_ALREADY_CONNECTED
    assert not any(call[0] == "POST" for call in client.calls)


async def test_libraries_changed_set_starts_a_scan() -> None:
    target = SeerrPlexTarget(ip="172.18.0.1", port=32400, use_ssl=False)
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/plex"): [
                _ok(
                    {
                        "ip": "172.18.0.1",
                        "port": 32400,
                        "useSsl": False,
                        "machineId": "m1",
                        "libraries": [{"id": "a", "enabled": True}],
                    }
                )
            ],
            ("GET", "api/v1/settings/plex/library"): [
                _ok([{"id": "a"}, {"id": "b"}]),
                _ok([{"id": "a", "enabled": True}, {"id": "b", "enabled": True}]),
            ],
            ("POST", "api/v1/settings/plex/sync"): [_ok({})],
        }
    )

    outcome = await ensure_seerr_media_server(
        client,
        _SEERR_BASE_URL,
        "k",
        "plex",
        plex_target=target,
        expected_machine_id="m1",
        owner_host=None,
        name="Plex",
    )

    assert outcome.state == "done"
    assert outcome.changed is True
    assert outcome.note == words.seerr_note_libraries("Plex")
    assert client.calls[-1][:2] == ("POST", "api/v1/settings/plex/sync")
    assert client.bodies[-1] == {"start": True}


async def test_a_still_disabled_library_errors() -> None:
    target = SeerrPlexTarget(ip="172.18.0.1", port=8096, use_ssl=False)
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/jellyfin"): [
                _ok(
                    {
                        "ip": "172.18.0.1",
                        "port": 8096,
                        "useSsl": False,
                        "urlBase": "",
                        "libraries": [],
                    }
                )
            ],
            ("GET", "api/v1/settings/jellyfin/library"): [
                _ok([{"id": "a"}, {"id": "b"}]),
                _ok([{"id": "a", "enabled": True}, {"id": "b", "enabled": False}]),
            ],
        }
    )

    outcome = await ensure_seerr_media_server(
        client,
        _SEERR_BASE_URL,
        "k",
        "jellyfin",
        plex_target=target,
        expected_machine_id=None,
        owner_host=None,
        name="Jellyfin",
    )

    assert outcome.state == "error"


async def test_an_empty_library_list_is_skipped() -> None:
    target = SeerrPlexTarget(ip="172.18.0.1", port=32400, use_ssl=False)
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/plex"): [
                _ok(
                    {
                        "ip": "172.18.0.1",
                        "port": 32400,
                        "useSsl": False,
                        "machineId": "m1",
                        "libraries": [],
                    }
                )
            ],
            ("GET", "api/v1/settings/plex/library"): [_ok([])],
        }
    )

    outcome = await ensure_seerr_media_server(
        client,
        _SEERR_BASE_URL,
        "k",
        "plex",
        plex_target=target,
        expected_machine_id="m1",
        owner_host=None,
        name="Plex",
    )

    assert outcome.state == "skipped"
    assert outcome.note == words.seerr_note_no_libraries("Plex")


async def test_a_404_on_library_sync_is_skipped() -> None:
    target = SeerrPlexTarget(ip="172.18.0.1", port=32400, use_ssl=False)
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/plex"): [
                _ok(
                    {
                        "ip": "172.18.0.1",
                        "port": 32400,
                        "useSsl": False,
                        "machineId": "m1",
                        "libraries": [],
                    }
                )
            ],
            ("GET", "api/v1/settings/plex/library"): [_fail(404)],
        }
    )

    outcome = await ensure_seerr_media_server(
        client,
        _SEERR_BASE_URL,
        "k",
        "plex",
        plex_target=target,
        expected_machine_id="m1",
        owner_host=None,
        name="Plex",
    )

    assert outcome.state == "skipped"
    assert outcome.note == words.seerr_note_no_libraries("Plex")


async def test_media_server_technical_never_carries_the_key_or_a_payload() -> None:
    target = SeerrPlexTarget(ip="172.18.0.1", port=32400, use_ssl=False)
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/plex"): [
                _fail(400, payload={"machineId": "leaked-machine-id"})
            ],
        }
    )

    outcome = await ensure_seerr_media_server(
        client,
        _SEERR_BASE_URL,
        "seerr-secret-key",
        "plex",
        plex_target=target,
        expected_machine_id="m1",
        owner_host=None,
        name="Plex",
    )

    assert outcome.state == "error"
    assert outcome.technical is not None
    assert "leaked-machine-id" not in outcome.technical
    assert "seerr-secret-key" not in outcome.technical


# --- ensure_seerr_arr: the FIRST TEST ------------------------------------------


async def test_sonarr_is_added_with_recyclarrs_profile_and_the_owners_address() -> None:
    sonarr = get_app("sonarr")
    expected_body = _expected_arr_body(
        sonarr,
        "sk",
        profile_id=7,
        profile_name="WEB-1080p",
        folder="/data/media/tv",
        owner_host="nas.local",
    )
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/sonarr"): [_ok([])],
            ("POST", "api/v1/settings/sonarr/test"): [
                _ok(
                    {
                        "profiles": [{"id": 1, "name": "Any"}, {"id": 7, "name": "WEB-1080p"}],
                        "rootFolders": [{"id": 1, "path": "/data/media/tv"}],
                    }
                )
            ],
            ("POST", "api/v1/settings/sonarr"): [_ok(expected_body, status=201)],
        }
    )

    outcome = await ensure_seerr_arr(
        client,
        _SEERR_BASE_URL,
        "k",
        sonarr,
        "sk",
        preferred_profile="WEB-1080p",
        owner_host="nas.local",
    )

    assert outcome.state == "done"
    assert outcome.changed is True
    assert [call[:2] for call in client.calls] == [
        ("GET", "api/v1/settings/sonarr"),
        ("POST", "api/v1/settings/sonarr/test"),
        ("POST", "api/v1/settings/sonarr"),
    ]
    assert client.bodies[1] == {
        "hostname": "sonarr",
        "port": 8989,
        "apiKey": "sk",
        "useSsl": False,
        "baseUrl": "",
    }
    assert client.bodies[2] == {
        "name": "Sonarr",
        "hostname": "sonarr",
        "port": 8989,
        "apiKey": "sk",
        "useSsl": False,
        "baseUrl": "",
        "activeProfileId": 7,
        "activeProfileName": "WEB-1080p",
        "activeDirectory": "/data/media/tv",
        "tags": [],
        "is4k": False,
        "isDefault": True,
        "externalUrl": "http://nas.local:8989",
        "syncEnabled": True,
        "preventSearch": False,
        "tagRequests": False,
        "overrideRule": [],
        "seriesType": "standard",
        "animeSeriesType": "anime",
        "enableSeasonFolders": True,
        "monitorNewItems": "all",
    }
    assert client.bodies[2] == expected_body
    assert outcome.note == words.seerr_note_profile("Sonarr", "WEB-1080p")


async def test_radarr_body_uses_minimum_availability_no_series_keys() -> None:
    radarr = get_app("radarr")
    expected_body = _expected_arr_body(
        radarr,
        "rk",
        profile_id=9,
        profile_name="UHD Bluray + WEB",
        folder="/data/media/movies",
        owner_host="nas.local",
    )
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/radarr"): [_ok([])],
            ("POST", "api/v1/settings/radarr/test"): [
                _ok(
                    {
                        "profiles": [{"id": 9, "name": "UHD Bluray + WEB"}],
                        "rootFolders": [{"id": 1, "path": "/data/media/movies"}],
                    }
                )
            ],
            ("POST", "api/v1/settings/radarr"): [_ok(expected_body, status=201)],
        }
    )

    outcome = await ensure_seerr_arr(
        client,
        _SEERR_BASE_URL,
        "k",
        radarr,
        "rk",
        preferred_profile="UHD Bluray + WEB",
        owner_host="nas.local",
    )

    assert outcome.state == "done"
    body = client.bodies[2]
    assert isinstance(body, dict)
    assert body["minimumAvailability"] == "released"
    for series_key in ("seriesType", "animeSeriesType", "enableSeasonFolders", "monitorNewItems"):
        assert series_key not in body


async def test_existing_entry_already_right_is_left_alone() -> None:
    sonarr = get_app("sonarr")
    existing_entry = {
        "id": 3,
        "name": "Sonarr",
        "hostname": "sonarr",
        "port": 8989,
        "apiKey": "sk",
        "useSsl": False,
        "baseUrl": "",
        "activeProfileId": 7,
        "activeProfileName": "WEB-1080p",
        "activeDirectory": "/data/media/tv",
        "is4k": False,
        "isDefault": True,
        "syncEnabled": True,
        "externalUrl": "http://nas.local:8989",
    }
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/sonarr"): [_ok([existing_entry])],
            ("POST", "api/v1/settings/sonarr/test"): [
                _ok(
                    {
                        "profiles": [{"id": 7, "name": "WEB-1080p"}],
                        "rootFolders": [{"id": 1, "path": "/data/media/tv"}],
                    }
                )
            ],
        }
    )

    outcome = await ensure_seerr_arr(
        client,
        _SEERR_BASE_URL,
        "k",
        sonarr,
        "sk",
        preferred_profile="WEB-1080p",
        owner_host="nas.local",
    )

    assert outcome.state == "done"
    assert outcome.changed is False
    assert outcome.note == words.WIRING_NOTE_ALREADY_CONNECTED
    assert len(client.calls) == 2  # GET listing + POST test only - no PUT, no POST


async def test_existing_entry_with_another_profile_and_no_recyclarr_keeps_it() -> None:
    sonarr = get_app("sonarr")
    existing_entry = {
        "id": 3,
        "name": "Sonarr",
        "hostname": "sonarr",
        "port": 8989,
        "apiKey": "sk",
        "useSsl": False,
        "baseUrl": "",
        "activeProfileId": 2,
        "activeProfileName": "HD-720p",
        "activeDirectory": "/data/media/tv",
        "is4k": False,
        "isDefault": True,
        "syncEnabled": True,
        "tags": [3],
    }
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/sonarr"): [_ok([existing_entry])],
            ("POST", "api/v1/settings/sonarr/test"): [
                _ok(
                    {
                        "profiles": [
                            {"id": 2, "name": "HD-720p"},
                            {"id": 4, "name": "HD-1080p"},
                        ],
                        "rootFolders": [{"id": 1, "path": "/data/media/tv"}],
                    }
                )
            ],
            ("PUT", "api/v1/settings/sonarr/3"): [
                _ok(
                    {
                        **existing_entry,
                        "externalUrl": "http://nas.local:8989",
                    },
                    status=200,
                )
            ],
        }
    )

    outcome = await ensure_seerr_arr(
        client,
        _SEERR_BASE_URL,
        "k",
        sonarr,
        "sk",
        preferred_profile=None,
        owner_host="nas.local",
    )

    assert outcome.state == "done"
    assert outcome.changed is True
    assert outcome.note == words.seerr_note_profile("Sonarr", "HD-720p")
    put_body = client.bodies[-1]
    assert isinstance(put_body, dict)
    assert put_body["activeProfileId"] == 2
    assert put_body["activeProfileName"] == "HD-720p"


async def test_owner_edit_outside_owned_keys_survives_a_put() -> None:
    sonarr = get_app("sonarr")
    existing_entry = {
        "id": 3,
        "name": "Sonarr",
        "hostname": "sonarr",
        "port": 8989,
        "apiKey": "stale-key",
        "useSsl": False,
        "baseUrl": "",
        "activeProfileId": 7,
        "activeProfileName": "WEB-1080p",
        "activeDirectory": "/data/media/tv",
        "is4k": False,
        "isDefault": True,
        "syncEnabled": True,
        "tags": [3],
    }
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/sonarr"): [_ok([existing_entry])],
            ("POST", "api/v1/settings/sonarr/test"): [
                _ok(
                    {
                        "profiles": [{"id": 7, "name": "WEB-1080p"}],
                        "rootFolders": [{"id": 1, "path": "/data/media/tv"}],
                    }
                )
            ],
            ("PUT", "api/v1/settings/sonarr/3"): [
                _ok(
                    {**existing_entry, "apiKey": "sk", "externalUrl": "http://nas.local:8989"},
                    status=200,
                )
            ],
        }
    )

    outcome = await ensure_seerr_arr(
        client,
        _SEERR_BASE_URL,
        "k",
        sonarr,
        "sk",
        preferred_profile="WEB-1080p",
        owner_host="nas.local",
    )

    assert outcome.state == "done"
    assert outcome.changed is True
    put_body = client.bodies[-1]
    assert isinstance(put_body, dict)
    assert put_body["tags"] == [3]
    assert put_body["apiKey"] == "sk"


async def test_missing_recyclarr_profile_falls_back_to_hd_1080p() -> None:
    sonarr = get_app("sonarr")
    profiles = [{"id": 4, "name": "HD-1080p"}, {"id": 9, "name": "Ultra-HD"}]
    expected_body = _expected_arr_body(
        sonarr,
        "sk",
        profile_id=4,
        profile_name="HD-1080p",
        folder="/data/media/tv",
        owner_host=None,
    )
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/sonarr"): [_ok([])],
            ("POST", "api/v1/settings/sonarr/test"): [
                _ok({"profiles": profiles, "rootFolders": [{"id": 1, "path": "/data/media/tv"}]})
            ],
            ("POST", "api/v1/settings/sonarr"): [_ok(expected_body, status=201)],
        }
    )

    outcome = await ensure_seerr_arr(
        client,
        _SEERR_BASE_URL,
        "k",
        sonarr,
        "sk",
        preferred_profile="WEB-2160p",  # not listed
        owner_host=None,
    )

    assert outcome.state == "done"
    body = client.bodies[2]
    assert isinstance(body, dict)
    assert body["activeProfileId"] == 4
    assert body["activeProfileName"] == "HD-1080p"


async def test_no_hd_1080p_falls_back_to_the_first_listed_profile() -> None:
    sonarr = get_app("sonarr")
    profiles = [{"id": 11, "name": "Ultra-HD"}, {"id": 12, "name": "SD"}]
    expected_body = _expected_arr_body(
        sonarr,
        "sk",
        profile_id=11,
        profile_name="Ultra-HD",
        folder="/data/media/tv",
        owner_host=None,
    )
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/sonarr"): [_ok([])],
            ("POST", "api/v1/settings/sonarr/test"): [
                _ok({"profiles": profiles, "rootFolders": [{"id": 1, "path": "/data/media/tv"}]})
            ],
            ("POST", "api/v1/settings/sonarr"): [_ok(expected_body, status=201)],
        }
    )

    outcome = await ensure_seerr_arr(
        client, _SEERR_BASE_URL, "k", sonarr, "sk", preferred_profile=None, owner_host=None
    )

    assert outcome.state == "done"
    body = client.bodies[2]
    assert isinstance(body, dict)
    assert body["activeProfileId"] == 11
    assert body["activeProfileName"] == "Ultra-HD"


async def test_test_call_failure_is_cant_reach_and_transient_on_500() -> None:
    sonarr = get_app("sonarr")
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/sonarr"): [_ok([])],
            ("POST", "api/v1/settings/sonarr/test"): [_fail(500)],
        }
    )

    outcome = await ensure_seerr_arr(
        client, _SEERR_BASE_URL, "k", sonarr, "sk", preferred_profile=None, owner_host=None
    )

    assert outcome.state == "error"
    assert outcome.note == words.seerr_failure_cant_reach("Sonarr")
    assert outcome.transient is True


async def test_missing_root_folder_is_no_folder() -> None:
    sonarr = get_app("sonarr")
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/sonarr"): [_ok([])],
            ("POST", "api/v1/settings/sonarr/test"): [
                _ok(
                    {
                        "profiles": [{"id": 1, "name": "Any"}],
                        "rootFolders": [{"id": 1, "path": "/data/other"}],
                    }
                )
            ],
        }
    )

    outcome = await ensure_seerr_arr(
        client, _SEERR_BASE_URL, "k", sonarr, "sk", preferred_profile=None, owner_host=None
    )

    assert outcome.state == "error"
    assert outcome.note == words.seerr_failure_no_folder("Sonarr")


async def test_no_profiles_is_no_profiles() -> None:
    sonarr = get_app("sonarr")
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/sonarr"): [_ok([])],
            ("POST", "api/v1/settings/sonarr/test"): [
                _ok({"profiles": [], "rootFolders": [{"id": 1, "path": "/data/media/tv"}]})
            ],
        }
    )

    outcome = await ensure_seerr_arr(
        client, _SEERR_BASE_URL, "k", sonarr, "sk", preferred_profile=None, owner_host=None
    )

    assert outcome.state == "error"
    assert outcome.note == words.seerr_failure_no_profiles("Sonarr")


async def test_arr_technical_never_carries_the_seerr_key_or_arr_key() -> None:
    sonarr = get_app("sonarr")
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/sonarr"): [
                _fail(400, payload={"apiKey": "super-secret-arr-key", "message": None})
            ],
        }
    )

    outcome = await ensure_seerr_arr(
        client,
        _SEERR_BASE_URL,
        "super-seerr-key",
        sonarr,
        "super-secret-arr-key",
        preferred_profile=None,
        owner_host=None,
    )

    assert outcome.state == "error"
    assert outcome.technical is not None
    assert "super-secret-arr-key" not in outcome.technical
    assert "super-seerr-key" not in outcome.technical


# --- mutation-guard tests: each pins a branch a passing-suite mutant skipped --


async def test_a_4k_entry_is_never_treated_as_the_existing_one() -> None:
    """`_find_seerr_arr_entry` must skip an `is4k` entry even when its
    hostname and port match - it is a second, independent connection
    Marrquee never touches.
    """
    sonarr = get_app("sonarr")
    entries = [
        {
            "id": 99,
            "name": "Sonarr - 4K",
            "hostname": "sonarr",
            "port": 8989,
            "is4k": True,
            "activeProfileId": 1,
        }
    ]
    expected_body = _expected_arr_body(
        sonarr,
        "sk",
        profile_id=7,
        profile_name="WEB-1080p",
        folder="/data/media/tv",
        owner_host=None,
    )
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/sonarr"): [_ok(entries)],
            ("POST", "api/v1/settings/sonarr/test"): [
                _ok(
                    {
                        "profiles": [{"id": 7, "name": "WEB-1080p"}],
                        "rootFolders": [{"id": 1, "path": "/data/media/tv"}],
                    }
                )
            ],
            ("POST", "api/v1/settings/sonarr"): [_ok(expected_body, status=201)],
            # Scripted only so a wrongly-matched PUT doesn't crash the test
            # with an unscripted-call KeyError - the assertions below are
            # what actually prove the 4K entry was left alone.
            ("PUT", "api/v1/settings/sonarr/99"): [
                _ok({**entries[0], **expected_body}, status=200)
            ],
        }
    )

    outcome = await ensure_seerr_arr(
        client,
        _SEERR_BASE_URL,
        "k",
        sonarr,
        "sk",
        preferred_profile="WEB-1080p",
        owner_host=None,
    )

    call_pairs = [call[:2] for call in client.calls]
    assert outcome.state == "done"
    assert ("POST", "api/v1/settings/sonarr") in call_pairs
    assert ("PUT", "api/v1/settings/sonarr/99") not in call_pairs


async def test_a_hand_edited_entry_is_found_by_name_and_repaired() -> None:
    """An entry the owner (or Seerr itself) renamed away from `arr.id` as
    its `hostname` must still be found by name and repaired with a PUT,
    never duplicated with a second POST.
    """
    sonarr = get_app("sonarr")
    existing_entry = {
        "id": 5,
        "name": "Sonarr",
        "hostname": "192.168.1.5",
        "port": 8989,
        "apiKey": "old-key",
        "useSsl": False,
        "baseUrl": "",
        "activeProfileId": 7,
        "activeProfileName": "WEB-1080p",
        "activeDirectory": "/data/media/tv",
        "is4k": False,
        "isDefault": True,
        "syncEnabled": True,
    }
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/sonarr"): [_ok([existing_entry])],
            ("POST", "api/v1/settings/sonarr/test"): [
                _ok(
                    {
                        "profiles": [{"id": 7, "name": "WEB-1080p"}],
                        "rootFolders": [{"id": 1, "path": "/data/media/tv"}],
                    }
                )
            ],
            ("PUT", "api/v1/settings/sonarr/5"): [
                _ok({**existing_entry, "hostname": "sonarr", "apiKey": "sk"}, status=200)
            ],
            # Scripted only so a fallback-less bug takes a clean, assertable
            # path instead of an unscripted-call KeyError.
            ("POST", "api/v1/settings/sonarr"): [_ok(status=201)],
        }
    )

    outcome = await ensure_seerr_arr(
        client,
        _SEERR_BASE_URL,
        "k",
        sonarr,
        "sk",
        preferred_profile="WEB-1080p",
        owner_host=None,
    )

    call_pairs = [call[:2] for call in client.calls]
    assert outcome.state == "done"
    assert ("PUT", "api/v1/settings/sonarr/5") in call_pairs
    assert ("POST", "api/v1/settings/sonarr") not in call_pairs


async def test_plex_connection_rewritten_when_only_the_machine_id_is_wrong() -> None:
    """A Plex whose address already matches but whose `machineId` is
    someone else's must still be re-POSTed - a matching address alone is
    not proof this is the right Plex.
    """
    target = SeerrPlexTarget(ip="172.18.0.1", port=32400, use_ssl=False)
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/plex"): [
                _ok(
                    {
                        "ip": "172.18.0.1",
                        "port": 32400,
                        "useSsl": False,
                        "machineId": "other",
                        "libraries": [],
                    }
                )
            ],
            ("POST", "api/v1/settings/plex"): [_ok({"machineId": "other"})],
            ("GET", "api/v1/settings/plex/library"): [_ok([])],
        }
    )

    outcome = await ensure_seerr_media_server(
        client,
        _SEERR_BASE_URL,
        "k",
        "plex",
        plex_target=target,
        expected_machine_id="m1",
        owner_host=None,
        name="Plex",
    )

    assert client.calls[1][:2] == ("POST", "api/v1/settings/plex")
    assert outcome.state == "error"
    assert outcome.note == words.SEERR_FAILURE_WRONG_PLEX


async def test_403_on_a_media_server_settings_get_is_refused_not_transient() -> None:
    target = SeerrPlexTarget(ip="172.18.0.1", port=32400, use_ssl=False)
    client = FakeSeerrClient({("GET", "api/v1/settings/plex"): [_fail(403)]})

    outcome = await ensure_seerr_media_server(
        client,
        _SEERR_BASE_URL,
        "k",
        "plex",
        plex_target=target,
        expected_machine_id="m1",
        owner_host=None,
        name="Plex",
    )

    assert outcome.state == "error"
    assert outcome.note == words.wiring_failure_refused("Seerr")
    assert outcome.transient is False


async def test_403_on_the_arr_settings_get_is_refused_not_transient() -> None:
    sonarr = get_app("sonarr")
    client = FakeSeerrClient({("GET", "api/v1/settings/sonarr"): [_fail(403)]})

    outcome = await ensure_seerr_arr(
        client, _SEERR_BASE_URL, "k", sonarr, "sk", preferred_profile=None, owner_host=None
    )

    assert outcome.state == "error"
    assert outcome.note == words.wiring_failure_refused("Seerr")
    assert outcome.transient is False


async def test_stale_jellyfin_external_hostname_is_rewritten() -> None:
    """A Jellyfin connection whose `ip`/`port`/`useSsl`/`urlBase` already
    match but whose `externalHostname` is stale must still be re-POSTed
    with the owner's current address.
    """
    target = SeerrPlexTarget(ip="172.18.0.1", port=8096, use_ssl=False)
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/jellyfin"): [
                _ok(
                    {
                        "ip": "172.18.0.1",
                        "port": 8096,
                        "useSsl": False,
                        "urlBase": "",
                        "externalHostname": "http://old.local:8096",
                        "libraries": [],
                    }
                )
            ],
            ("POST", "api/v1/settings/jellyfin"): [_ok({})],
            ("GET", "api/v1/settings/jellyfin/library"): [_ok([])],
        }
    )

    await ensure_seerr_media_server(
        client,
        _SEERR_BASE_URL,
        "k",
        "jellyfin",
        plex_target=target,
        expected_machine_id=None,
        owner_host="nas.local",
        name="Jellyfin",
    )

    assert client.bodies[1] == {
        "ip": "172.18.0.1",
        "port": 8096,
        "useSsl": False,
        "urlBase": "",
        "externalHostname": "http://nas.local:8096",
    }


# --- refresh_seerr_profiles: after a Marrquee-started Recyclarr sync ----------


async def test_refresh_moves_sonarr_and_radarr_to_recyclarrs_profiles() -> None:
    install = _install_state(("sonarr", "radarr", "seerr", "recyclarr"))
    sonarr_existing = {
        "id": 3,
        "name": "Sonarr",
        "hostname": "sonarr",
        "port": 8989,
        "apiKey": "sonarr-key",
        "useSsl": False,
        "baseUrl": "",
        "activeProfileId": 2,
        "activeProfileName": "HD-720p",
        "activeDirectory": "/data/media/tv",
        "is4k": False,
        "isDefault": True,
        "syncEnabled": True,
    }
    radarr_existing = {
        "id": 4,
        "name": "Radarr",
        "hostname": "radarr",
        "port": 7878,
        "apiKey": "radarr-key",
        "useSsl": False,
        "baseUrl": "",
        "activeProfileId": 5,
        "activeProfileName": "HD-1080p",
        "activeDirectory": "/data/media/movies",
        "is4k": False,
        "isDefault": True,
        "syncEnabled": True,
    }
    client = FakeSeerrClient(
        {
            ("GET", "api/v1/settings/sonarr"): [_ok([sonarr_existing])],
            ("POST", "api/v1/settings/sonarr/test"): [
                _ok(
                    {
                        "profiles": [
                            {"id": 2, "name": "HD-720p"},
                            {"id": 7, "name": "WEB-1080p"},
                        ],
                        "rootFolders": [{"id": 1, "path": "/data/media/tv"}],
                    }
                )
            ],
            ("PUT", "api/v1/settings/sonarr/3"): [
                _ok(
                    {
                        **sonarr_existing,
                        "activeProfileId": 7,
                        "activeProfileName": "WEB-1080p",
                        "externalUrl": "http://nas.local:8989",
                    }
                )
            ],
            ("GET", "api/v1/settings/radarr"): [_ok([radarr_existing])],
            ("POST", "api/v1/settings/radarr/test"): [
                _ok(
                    {
                        "profiles": [
                            {"id": 5, "name": "HD-1080p"},
                            {"id": 9, "name": "UHD Bluray + WEB"},
                        ],
                        "rootFolders": [{"id": 1, "path": "/data/media/movies"}],
                    }
                )
            ],
            ("PUT", "api/v1/settings/radarr/4"): [
                _ok(
                    {
                        **radarr_existing,
                        "activeProfileId": 9,
                        "activeProfileName": "UHD Bluray + WEB",
                        "externalUrl": "http://nas.local:7878",
                    }
                )
            ],
        }
    )
    answers = {"sonarr": {"tv_quality": "1080p"}, "radarr": {"movie_quality": "4k"}}

    outcomes = await refresh_seerr_profiles(client, install, answers, "nas.local")

    assert [outcome.state for outcome in outcomes] == ["done", "done"]
    assert [outcome.changed for outcome in outcomes] == [True, True]
    call_pairs = [call[:2] for call in client.calls]
    assert ("PUT", "api/v1/settings/sonarr/3") in call_pairs
    assert ("PUT", "api/v1/settings/radarr/4") in call_pairs
    sonarr_put_body = client.bodies[2]
    radarr_put_body = client.bodies[5]
    assert isinstance(sonarr_put_body, dict)
    assert isinstance(radarr_put_body, dict)
    assert sonarr_put_body["activeProfileName"] == "WEB-1080p"
    assert radarr_put_body["activeProfileName"] == "UHD Bluray + WEB"


async def test_refresh_does_nothing_without_seerr_or_recyclarr() -> None:
    client = FakeSeerrClient({})

    without_seerr = await refresh_seerr_profiles(
        client, _install_state(("sonarr", "radarr", "recyclarr")), {}, None
    )
    without_recyclarr = await refresh_seerr_profiles(
        client, _install_state(("sonarr", "radarr", "seerr")), {}, None
    )

    assert without_seerr == ()
    assert without_recyclarr == ()
    assert client.calls == []

"""Tests for the Plex doors: plex.json, plex.tv, the auth URL, the claim
secret, and a local Plex server.

Everything here runs offline: `httpx.MockTransport` stands in for plex.tv
and a local server, and `tmp_path` stands in for the owner's drive. The
live plex.tv shapes (PIN create/poll, the claim body) are documented but
unverified - see the story's Pitch conditions - so these tests pin the
documented shape and leave the live answer PENDING the owner's NAS.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
from pathlib import Path, PurePosixPath
from typing import cast

import httpx
import pytest

from marrquee import __version__
from marrquee.config import Settings
from marrquee.docker_client import DockerEngine
from marrquee.links import LinkCard
from marrquee.plex import (
    PLEX_CLAIM_FILE_NAME,
    PLEX_PRODUCT,
    PROBE_FOLDER_PREFIX,
    ExistingPlex,
    FakePlexServer,
    FakePlexTv,
    FolderSeen,
    HttpPlexServer,
    HttpPlexTv,
    PlexAccount,
    PlexCandidate,
    PlexConnection,
    PlexIdentity,
    PlexPin,
    PlexResponse,
    PlexSection,
    PlexServer,
    PlexServerChoice,
    PlexServers,
    PlexTv,
    browse_path,
    clear_existing_plex,
    clear_plex_claim,
    connection_candidates,
    existing_plex_web_url,
    find_connection,
    link_matches_plex,
    load_existing_plex,
    load_plex_account,
    make_folder_marker,
    parse_browse_folders,
    parse_plex_sections,
    plex_auth_url,
    plex_base_url,
    plex_client_id,
    plex_host_address,
    plex_secrets_host_path,
    probe_folder,
    remove_folder_marker,
    remove_stale_folder_markers,
    save_existing_plex,
    save_plex_sign_in,
    update_existing_plex,
    write_plex_claim,
)
from marrquee.storage import PathEscapesRoot, container_media_path, host_media_path

# --- plex.tv: create_pin -----------------------------------------------------


async def test_create_pin_sends_the_plex_headers_and_strong_true() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"id": 7, "code": "abcd"})

    client = HttpPlexTv(transport=httpx.MockTransport(handler))

    pin = await client.create_pin("cid")

    assert pin == PlexPin(id=7, code="abcd")
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == "https://plex.tv/api/v2/pins?strong=true"
    assert request.headers["accept"] == "application/json"
    assert request.headers["x-plex-product"] == PLEX_PRODUCT
    assert request.headers["x-plex-version"] == __version__
    assert request.headers["x-plex-client-identifier"] == "cid"
    assert "x-plex-token" not in request.headers


async def test_create_pin_is_none_on_a_bad_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"id": "not-an-int", "code": "abcd"})

    client = HttpPlexTv(transport=httpx.MockTransport(handler))

    assert await client.create_pin("cid") is None


async def test_create_pin_is_none_on_a_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = HttpPlexTv(transport=httpx.MockTransport(handler))

    assert await client.create_pin("cid") is None


# --- plex.tv: pin_token -------------------------------------------------------


async def test_pin_token_returns_none_while_the_pin_is_unapproved() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"authToken": None})

    client = HttpPlexTv(transport=httpx.MockTransport(handler))

    token = await client.pin_token("cid", PlexPin(id=7, code="abcd"))

    assert token is None


async def test_pin_token_returns_the_token_once_approved() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://plex.tv/api/v2/pins/7?code=abcd"
        return httpx.Response(200, json={"authToken": "plex-token-xyz"})

    client = HttpPlexTv(transport=httpx.MockTransport(handler))

    token = await client.pin_token("cid", PlexPin(id=7, code="abcd"))

    assert token == "plex-token-xyz"


# --- plex.tv: account_name ----------------------------------------------------


async def test_account_name_prefers_username_over_title() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-plex-token"] == "tok"
        return httpx.Response(200, json={"username": "ryan", "title": "Ryan S"})

    client = HttpPlexTv(transport=httpx.MockTransport(handler))

    assert await client.account_name("cid", "tok") == "ryan"


async def test_account_name_falls_back_to_title_when_username_is_blank() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"username": "", "title": "Ryan S"})

    client = HttpPlexTv(transport=httpx.MockTransport(handler))

    assert await client.account_name("cid", "tok") == "Ryan S"


async def test_account_name_is_none_when_neither_field_is_usable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = HttpPlexTv(transport=httpx.MockTransport(handler))

    assert await client.account_name("cid", "tok") is None


# --- plex.tv: claim_token ------------------------------------------------------


async def test_claim_token_reads_a_json_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-plex-token"] == "tok"
        return httpx.Response(200, json={"token": "claim-abc_DEF"})

    client = HttpPlexTv(transport=httpx.MockTransport(handler))

    assert await client.claim_token("cid", "tok") == "claim-abc_DEF"


async def test_claim_token_reads_an_xml_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'<MediaContainer token="claim-xyz123"/>',
            headers={"content-type": "text/xml"},
        )

    client = HttpPlexTv(transport=httpx.MockTransport(handler))

    assert await client.claim_token("cid", "tok") == "claim-xyz123"


async def test_claim_token_rejects_a_value_that_does_not_look_like_a_claim() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token": "not-a-claim-value"})

    client = HttpPlexTv(transport=httpx.MockTransport(handler))

    assert await client.claim_token("cid", "tok") is None


async def test_claim_token_is_none_on_a_401() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    client = HttpPlexTv(transport=httpx.MockTransport(handler))

    assert await client.claim_token("cid", "tok") is None


# --- Never a token in a URL, never a token or claim in a log or a fake's calls -


async def test_no_call_logs_or_records_a_token(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    fake = FakePlexTv(
        pin=PlexPin(id=7, code="abcd"),
        token="super-secret-token",  # noqa: S105 - a test fixture value, not a real secret
        username="owner",
        claims=("claim-topsecret1",),
    )

    pin = await fake.create_pin("cid")
    assert pin is not None
    token = await fake.pin_token("cid", pin)
    name = await fake.account_name("cid", token or "")
    claim = await fake.claim_token("cid", token or "")

    assert token == "super-secret-token"
    assert name == "owner"
    assert claim == "claim-topsecret1"
    assert fake.calls == ["create_pin", "pin_token", "account_name", "claim_token"]
    assert "super-secret-token" not in caplog.text
    assert "claim-topsecret1" not in caplog.text


async def test_the_plex_token_travels_as_a_header_never_in_the_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-plex-token"] == "super-secret-token"
        assert "super-secret-token" not in str(request.url)
        return httpx.Response(200, json={"ok": True})

    server = HttpPlexServer(transport=httpx.MockTransport(handler))

    response = await server.request(
        "PUT",
        "http://host:32400",
        "/:/prefs",
        "super-secret-token",
        params=[("TranscoderCanOnlyRemuxVideo", "1")],
    )

    assert response.ok is True


# --- plex.json -----------------------------------------------------------------


def test_plex_json_round_trips(tmp_path: Path) -> None:
    save_plex_sign_in(tmp_path, "tok-secret-123", "ryan")

    account = load_plex_account(tmp_path)

    assert account is not None
    assert account.username == "ryan"
    assert account.token == "tok-secret-123"
    assert account.client_id


def test_plex_json_is_0600(tmp_path: Path) -> None:
    save_plex_sign_in(tmp_path, "tok-secret-123", "ryan")

    written = tmp_path / "plex.json"

    assert (written.stat().st_mode & 0o777) == 0o600


def test_a_missing_plex_json_loads_as_none(tmp_path: Path) -> None:
    assert load_plex_account(tmp_path) is None


def test_a_corrupt_plex_json_loads_as_none(tmp_path: Path) -> None:
    (tmp_path / "plex.json").write_text("not json at all")

    assert load_plex_account(tmp_path) is None


def test_an_empty_plex_json_loads_as_none(tmp_path: Path) -> None:
    (tmp_path / "plex.json").write_text("")

    assert load_plex_account(tmp_path) is None


def test_a_plex_json_with_the_wrong_version_loads_as_none(tmp_path: Path) -> None:
    (tmp_path / "plex.json").write_text(
        '{"version": 2, "client_id": "x", "username": null, "token": null}'
    )

    assert load_plex_account(tmp_path) is None


def test_a_plex_json_with_a_bool_version_loads_as_none(tmp_path: Path) -> None:
    (tmp_path / "plex.json").write_text(
        '{"version": true, "client_id": "x", "username": null, "token": null}'
    )

    assert load_plex_account(tmp_path) is None


def test_plex_client_id_is_stable_across_calls(tmp_path: Path) -> None:
    first = plex_client_id(tmp_path)
    second = plex_client_id(tmp_path)

    assert first == second
    assert first


def test_plex_client_id_survives_a_corrupt_plex_json(tmp_path: Path) -> None:
    (tmp_path / "plex.json").write_text("{not json")

    client_id = plex_client_id(tmp_path)

    assert client_id
    account = load_plex_account(tmp_path)
    assert account is not None
    assert account.client_id == client_id


def test_save_plex_sign_in_keeps_the_existing_client_id(tmp_path: Path) -> None:
    client_id = plex_client_id(tmp_path)

    save_plex_sign_in(tmp_path, "tok", "ryan")

    account = load_plex_account(tmp_path)
    assert account is not None
    assert account.client_id == client_id


# --- repr never leaks the token -------------------------------------------------


def test_repr_of_plex_account_hides_the_token() -> None:
    account = PlexAccount(client_id="cid", username="ryan", token="tok-secret-123")

    assert "tok-secret-123" not in repr(account)
    assert "cid" in repr(account)


# --- the auth URL ----------------------------------------------------------------


def test_auth_url_encodes_the_product_context_and_forward_url() -> None:
    url = plex_auth_url("cid-1", "abcd", "http://nas:7788/plex/signed-in?pin=7")

    assert url.startswith("https://app.plex.tv/auth#?")
    assert "clientID=cid-1" in url
    assert "code=abcd" in url
    assert "context%5Bdevice%5D%5Bproduct%5D=Marrquee" in url
    assert "forwardUrl=http%3A%2F%2Fnas%3A7788%2Fplex%2Fsigned-in%3Fpin%3D7" in url


# --- the claim secret --------------------------------------------------------------


def test_plex_secrets_host_path_is_marrquee_plex_under_the_root() -> None:
    assert plex_secrets_host_path(PurePosixPath("/volume1/media")) == PurePosixPath(
        "/volume1/media/marrquee/plex"
    )


def _plex_settings_and_root(tmp_path: Path) -> tuple[Settings, PurePosixPath]:
    (tmp_path / "volume1" / "media").mkdir(parents=True)
    return Settings(host_mount=tmp_path), PurePosixPath("/volume1/media")


def _plex_secrets_folder(tmp_path: Path) -> Path:
    return tmp_path / "volume1" / "media" / "marrquee" / "plex"


def test_write_plex_claim_makes_a_root_only_folder_of_a_root_only_file(tmp_path: Path) -> None:
    settings, root = _plex_settings_and_root(tmp_path)

    write_plex_claim(settings, root, "claim-abc_DEF")

    folder = _plex_secrets_folder(tmp_path)
    assert folder.is_dir()
    assert (folder.stat().st_mode & 0o777) == 0o700
    written = folder / PLEX_CLAIM_FILE_NAME
    assert written.is_file()
    assert (written.stat().st_mode & 0o777) == 0o600
    assert written.read_text() == "claim-abc_DEF"
    assert not any(folder.glob(".*.tmp"))


def test_write_plex_claim_never_chowns_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, root = _plex_settings_and_root(tmp_path)
    calls: list[object] = []
    monkeypatch.setattr(os, "chown", lambda *args, **kwargs: calls.append((args, kwargs)))

    write_plex_claim(settings, root, "claim-abc")

    assert calls == []


def test_write_plex_claim_refuses_a_symlinked_secrets_folder(tmp_path: Path) -> None:
    settings, root = _plex_settings_and_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    marrquee_dir = tmp_path / "volume1" / "media" / "marrquee"
    marrquee_dir.mkdir()
    (marrquee_dir / "plex").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathEscapesRoot):
        write_plex_claim(settings, root, "claim-abc")


def test_write_plex_claim_overwrites_a_previous_claim(tmp_path: Path) -> None:
    settings, root = _plex_settings_and_root(tmp_path)

    write_plex_claim(settings, root, "claim-first")
    write_plex_claim(settings, root, "claim-second")

    written = _plex_secrets_folder(tmp_path) / PLEX_CLAIM_FILE_NAME
    assert written.read_text() == "claim-second"


def test_clear_plex_claim_removes_the_file(tmp_path: Path) -> None:
    settings, root = _plex_settings_and_root(tmp_path)
    write_plex_claim(settings, root, "claim-abc")

    clear_plex_claim(settings, root)

    assert not (_plex_secrets_folder(tmp_path) / PLEX_CLAIM_FILE_NAME).exists()


def test_clear_plex_claim_never_raises_when_nothing_was_ever_written(tmp_path: Path) -> None:
    settings, root = _plex_settings_and_root(tmp_path)

    clear_plex_claim(settings, root)


def test_clear_plex_claim_never_raises_on_a_symlinked_secrets_folder(tmp_path: Path) -> None:
    settings, root = _plex_settings_and_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    marrquee_dir = tmp_path / "volume1" / "media" / "marrquee"
    marrquee_dir.mkdir()
    (marrquee_dir / "plex").symlink_to(outside, target_is_directory=True)

    clear_plex_claim(settings, root)


def test_clear_plex_claim_ignores_a_directory_named_plex_claim(tmp_path: Path) -> None:
    settings, root = _plex_settings_and_root(tmp_path)
    folder = _plex_secrets_folder(tmp_path)
    folder.mkdir(parents=True)
    (folder / PLEX_CLAIM_FILE_NAME).mkdir()

    clear_plex_claim(settings, root)

    assert (folder / PLEX_CLAIM_FILE_NAME).is_dir()


def test_clear_plex_claim_ignores_a_symlink_named_plex_claim(tmp_path: Path) -> None:
    settings, root = _plex_settings_and_root(tmp_path)
    folder = _plex_secrets_folder(tmp_path)
    folder.mkdir(parents=True)
    target = tmp_path / "elsewhere.txt"
    target.write_text("do not touch")
    (folder / PLEX_CLAIM_FILE_NAME).symlink_to(target)

    clear_plex_claim(settings, root)

    assert (folder / PLEX_CLAIM_FILE_NAME).is_symlink()
    assert target.read_text() == "do not touch"


# --- the local Plex server: identity -------------------------------------------


@pytest.mark.parametrize(
    ("raw_claimed", "expected"),
    [
        ("0", False),
        ("1", True),
        (True, True),
        (False, False),
        ("true", True),
        ("false", False),
    ],
)
async def test_identity_parses_claimed_from_every_documented_shape(
    raw_claimed: object, expected: bool
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"MediaContainer": {"claimed": raw_claimed, "machineIdentifier": "m-1"}},
        )

    server = HttpPlexServer(transport=httpx.MockTransport(handler))

    identity = await server.identity("http://host:32400")

    assert identity == PlexIdentity(claimed=expected, machine_id="m-1")


async def test_identity_sends_no_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "x-plex-token" not in request.headers
        return httpx.Response(
            200, json={"MediaContainer": {"claimed": True, "machineIdentifier": "m-1"}}
        )

    server = HttpPlexServer(transport=httpx.MockTransport(handler))

    assert await server.identity("http://host:32400") == PlexIdentity(
        claimed=True, machine_id="m-1"
    )


async def test_identity_is_none_on_a_bad_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    server = HttpPlexServer(transport=httpx.MockTransport(handler))

    assert await server.identity("http://host:32400") is None


async def test_identity_is_none_on_a_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    server = HttpPlexServer(transport=httpx.MockTransport(handler))

    assert await server.identity("http://host:32400") is None


async def test_identity_is_none_on_a_malformed_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"MediaContainer": {"claimed": "sideways"}})

    server = HttpPlexServer(transport=httpx.MockTransport(handler))

    assert await server.identity("http://host:32400") is None


# --- the local Plex server: request --------------------------------------------


async def test_request_status_is_zero_on_a_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    server = HttpPlexServer(transport=httpx.MockTransport(handler))

    response = await server.request("GET", "http://host:32400", "/identity", "tok")

    assert response.ok is False
    assert response.status == 0
    assert response.detail is not None
    assert "ConnectError" in response.detail


async def test_request_parses_a_non_json_body_as_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"plain text body")

    server = HttpPlexServer(transport=httpx.MockTransport(handler))

    response = await server.request("GET", "http://host:32400", "/web", "tok")

    assert response.ok is True
    assert response.payload == "plain text body"


# --- FakePlexTv / FakePlexServer -----------------------------------------------


def test_fake_plex_tv_satisfies_the_protocol() -> None:
    fake = FakePlexTv()
    satisfies_protocol: PlexTv = fake
    assert satisfies_protocol is fake


async def test_fake_plex_tv_drains_claims_and_repeats_the_last() -> None:
    fake = FakePlexTv(claims=("claim-one", "claim-two"))

    first = await fake.claim_token("cid", "tok")
    second = await fake.claim_token("cid", "tok")
    third = await fake.claim_token("cid", "tok")

    assert first == "claim-one"
    assert second == "claim-two"
    assert third == "claim-two"


async def test_fake_plex_tv_answers_none_when_scripted_none() -> None:
    fake = FakePlexTv(pin=None, token=None, username=None)

    assert await fake.create_pin("cid") is None
    assert await fake.pin_token("cid", PlexPin(1, "abcd")) is None
    assert await fake.account_name("cid", "tok") is None


def test_fake_plex_server_satisfies_the_protocol() -> None:
    fake = FakePlexServer()
    satisfies_protocol: PlexServer = fake
    assert satisfies_protocol is fake


async def test_fake_plex_server_drains_identities_and_repeats_the_last() -> None:
    fake = FakePlexServer(identities=[None, PlexIdentity(False, "m"), PlexIdentity(True, "m")])

    first = await fake.identity("http://host:32400")
    second = await fake.identity("http://host:32400")
    third = await fake.identity("http://host:32400")
    fourth = await fake.identity("http://host:32400")

    assert first is None
    assert second == PlexIdentity(False, "m")
    assert third == PlexIdentity(True, "m")
    assert fourth == PlexIdentity(True, "m")


async def test_fake_plex_server_answers_none_with_no_identities_scripted() -> None:
    fake = FakePlexServer()

    assert await fake.identity("http://host:32400") is None


async def test_fake_plex_server_replays_scripted_requests_and_records_calls() -> None:
    response = PlexResponse(ok=True, status=200, payload={"ok": True}, detail=None)
    fake = FakePlexServer(script={("GET", "/identity"): [response]})

    got = await fake.request("GET", "http://host:32400", "/identity", "tok")

    assert got is response
    assert fake.calls == [("GET", "/identity", ())]


async def test_fake_plex_server_names_an_unscripted_call() -> None:
    fake = FakePlexServer()

    with pytest.raises(KeyError, match="/identity"):
        await fake.request("GET", "http://host:32400", "/identity", "tok")


# --- plex_host_address / plex_base_url -----------------------------------------


class _FakeGatewayEngine:
    """A minimal stand-in for `DockerEngine.host_gateway` alone.

    `plex_host_address` only ever calls `host_gateway` - `cast` is what lets
    this narrower double stand in for a full `DockerEngine` here without
    implementing every other method the real protocol declares.
    """

    def __init__(self, address: str | None) -> None:
        self.address = address
        self.calls: list[str] = []

    async def host_gateway(self, container: str) -> str | None:
        self.calls.append(container)
        return self.address


async def test_plex_host_address_is_none_without_a_self_id() -> None:
    engine = _FakeGatewayEngine("172.18.0.1")

    address = await plex_host_address(cast(DockerEngine, engine), None)

    assert address is None
    assert engine.calls == []


async def test_plex_host_address_delegates_to_the_engines_gateway() -> None:
    engine = _FakeGatewayEngine("172.18.0.1")

    address = await plex_host_address(cast(DockerEngine, engine), "self")

    assert address == "172.18.0.1"
    assert engine.calls == ["self"]


def test_plex_base_url_wraps_an_ipv6_literal_in_brackets() -> None:
    assert plex_base_url("172.18.0.1") == "http://172.18.0.1:32400"
    assert plex_base_url("fe80::1") == "http://[fe80::1]:32400"


# --- plex.tv: the owner's own servers -------------------------------------------

_RESOURCES_JSON: list[object] = [
    {
        "name": "Den",
        "clientIdentifier": "m1",
        "provides": "server",
        "owned": True,
        "presence": True,
        "httpsRequired": False,
        "accessToken": "t1",
        "connections": [
            {
                "protocol": "http",
                "address": "192.168.1.20",
                "port": 32400,
                "uri": "https://192-168-1-20.x.plex.direct:32400",
                "local": True,
                "relay": False,
                "IPv6": False,
            }
        ],
    },
    {
        "name": "Some Phone",
        "clientIdentifier": "m2",
        "provides": "client,player",
        "owned": True,
        "presence": True,
    },
    {
        "name": "Friend's Plex",
        "clientIdentifier": "m3",
        "provides": "server",
        "owned": False,
        "presence": True,
    },
]


async def test_resources_keep_only_the_owners_own_servers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == ("https://plex.tv/api/v2/resources?includeHttps=1&includeIPv6=1")
        assert request.headers["accept"] == "application/json"
        assert request.headers["x-plex-product"] == PLEX_PRODUCT
        assert request.headers["x-plex-version"] == __version__
        assert request.headers["x-plex-client-identifier"] == "cid"
        assert request.headers["x-plex-token"] == "tok"
        assert "tok" not in str(request.url)
        return httpx.Response(200, json=_RESOURCES_JSON)

    client = HttpPlexTv(transport=httpx.MockTransport(handler))

    result = await client.servers("cid", "tok")

    assert result == PlexServers(
        state="ok",
        servers=(
            PlexServerChoice(
                machine_id="m1",
                name="Den",
                online=True,
                https_required=False,
                connections=(
                    PlexConnection(
                        protocol="http",
                        address="192.168.1.20",
                        port=32400,
                        uri="https://192-168-1-20.x.plex.direct:32400",
                        local=True,
                        ipv6=False,
                    ),
                ),
                token="t1",
            ),
        ),
    )


async def test_a_401_from_plextv_means_signed_out() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    client = HttpPlexTv(transport=httpx.MockTransport(handler))

    assert await client.servers("cid", "tok") == PlexServers("signed_out")


async def test_a_500_from_plextv_means_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = HttpPlexTv(transport=httpx.MockTransport(handler))

    assert await client.servers("cid", "tok") == PlexServers("unreachable")


async def test_a_transport_error_from_plextv_means_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = HttpPlexTv(transport=httpx.MockTransport(handler))

    assert await client.servers("cid", "tok") == PlexServers("unreachable")


async def test_a_non_list_body_from_plextv_means_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not": "a list"})

    client = HttpPlexTv(transport=httpx.MockTransport(handler))

    assert await client.servers("cid", "tok") == PlexServers("unreachable")


async def test_resources_drops_relay_connections_and_skips_bad_entries() -> None:
    payload: list[object] = [
        {"name": None, "clientIdentifier": "no-name", "provides": "server", "owned": True},
        {"name": "X", "clientIdentifier": "", "provides": "server", "owned": True},
        {
            "name": "Relay Only",
            "clientIdentifier": "m9",
            "provides": "server",
            "owned": "1",
            "presence": "1",
            "httpsRequired": "0",
            "connections": [
                {
                    "protocol": "https",
                    "address": "1.2.3.4",
                    "port": 32400,
                    "uri": "https://1.2.3.4:32400",
                    "local": False,
                    "relay": "true",
                    "IPv6": False,
                }
            ],
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = HttpPlexTv(transport=httpx.MockTransport(handler))

    result = await client.servers("cid", "tok")

    assert result.state == "ok"
    assert len(result.servers) == 1
    choice = result.servers[0]
    assert choice.machine_id == "m9"
    assert choice.online is True
    assert choice.https_required is False
    assert choice.connections == ()


async def test_fake_plex_tv_servers_returns_the_scripted_value_and_records_the_call() -> None:
    scripted = PlexServers("ok", (PlexServerChoice("m1", "Den", True, False, ()),))
    fake = FakePlexTv(servers=scripted)

    result = await fake.servers("cid", "tok")

    assert result is scripted
    assert fake.calls == ["servers"]


# --- browse: Path or Directory entries ------------------------------------------


def test_browse_reads_path_entries() -> None:
    payload = {"MediaContainer": {"Path": [{"path": "/data/media/movies", "title": "movies"}]}}

    assert parse_browse_folders(payload) == (("/data/media/movies", "movies"),)


def test_browse_reads_directory_entries() -> None:
    payload = {"MediaContainer": {"Directory": [{"path": "/data/media/tv", "title": "tv"}]}}

    assert parse_browse_folders(payload) == (("/data/media/tv", "tv"),)


def test_browse_reads_both_kinds_and_skips_non_dict_entries() -> None:
    payload = {
        "MediaContainer": {
            "Path": ["not-a-dict", {"path": "/a", "title": "a"}],
            "Directory": [{"path": "/b", "title": "b"}],
        }
    }

    assert parse_browse_folders(payload) == (("/a", "a"), ("/b", "b"))


def test_browse_never_raises_on_a_bad_shape() -> None:
    assert parse_browse_folders(None) == ()
    assert parse_browse_folders({"MediaContainer": "not-a-dict"}) == ()
    assert parse_browse_folders({"MediaContainer": {"Path": "not-a-list"}}) == ()


def test_browse_path_is_a_base64_folder_path() -> None:
    assert browse_path("/data/media/movies") == (
        "/services/browse/" + base64.b64encode(b"/data/media/movies").decode()
    )


# --- connection_candidates / find_connection ------------------------------------


def _connection(
    *, address: str, port: int, uri: str, local: bool, ipv6: bool = False
) -> PlexConnection:
    return PlexConnection(
        protocol="http", address=address, port=port, uri=uri, local=local, ipv6=ipv6
    )


def test_candidates_try_local_http_then_this_nas_then_plex_direct_then_remote() -> None:
    # A relay connection can never reach here in the first place - it is
    # dropped while parsing plex.tv's own resources JSON (see
    # test_resources_drops_relay_connections_and_skips_bad_entries above).
    choice = PlexServerChoice(
        machine_id="m1",
        name="Den",
        online=True,
        https_required=False,
        connections=(
            _connection(
                address="192.168.1.20",
                port=32400,
                uri="https://local.plex.direct:32400",
                local=True,
            ),
            _connection(
                address="fe80::1",
                port=32401,
                uri="https://local6.plex.direct:32401",
                local=True,
                ipv6=True,
            ),
            _connection(
                address="203.0.113.5",
                port=32400,
                uri="https://remote.plex.direct:32400",
                local=False,
            ),
        ),
    )

    candidates = connection_candidates(choice, "172.18.0.1")

    assert candidates == (
        PlexCandidate(base_url="http://192.168.1.20:32400", port=32400, on_this_nas=False),
        PlexCandidate(base_url="http://172.18.0.1:32400", port=32400, on_this_nas=True),
        PlexCandidate(base_url="http://172.18.0.1:32401", port=32401, on_this_nas=True),
        PlexCandidate(base_url="https://local.plex.direct:32400", port=32400, on_this_nas=False),
        PlexCandidate(base_url="https://local6.plex.direct:32401", port=32401, on_this_nas=False),
        PlexCandidate(base_url="http://[fe80::1]:32401", port=32401, on_this_nas=False),
        PlexCandidate(base_url="https://remote.plex.direct:32400", port=32400, on_this_nas=False),
        PlexCandidate(base_url="http://203.0.113.5:32400", port=32400, on_this_nas=False),
    )


def test_candidates_use_port_32400_for_this_nas_with_no_connections() -> None:
    choice = PlexServerChoice(
        machine_id="m1", name="Den", online=True, https_required=False, connections=()
    )

    candidates = connection_candidates(choice, "172.18.0.1")

    assert candidates == (
        PlexCandidate(base_url="http://172.18.0.1:32400", port=32400, on_this_nas=True),
    )


def test_candidates_are_deduplicated_by_base_url() -> None:
    choice = PlexServerChoice(
        machine_id="m1",
        name="Den",
        online=True,
        https_required=False,
        connections=(
            _connection(
                address="192.168.1.20", port=32400, uri="https://d.plex.direct:32400", local=True
            ),
        ),
    )

    candidates = connection_candidates(choice, None)

    assert candidates == (
        PlexCandidate(base_url="http://192.168.1.20:32400", port=32400, on_this_nas=False),
        PlexCandidate(base_url="https://d.plex.direct:32400", port=32400, on_this_nas=False),
    )


def test_https_required_drops_every_http_candidate() -> None:
    choice = PlexServerChoice(
        machine_id="m1",
        name="Den",
        online=True,
        https_required=True,
        connections=(
            _connection(
                address="192.168.1.20",
                port=32400,
                uri="https://local.plex.direct:32400",
                local=True,
            ),
            _connection(
                address="203.0.113.5",
                port=32400,
                uri="https://remote.plex.direct:32400",
                local=False,
            ),
        ),
    )

    candidates = connection_candidates(choice, "172.18.0.1")

    assert candidates == (
        PlexCandidate(base_url="https://local.plex.direct:32400", port=32400, on_this_nas=False),
        PlexCandidate(base_url="https://remote.plex.direct:32400", port=32400, on_this_nas=False),
    )


async def test_find_connection_picks_the_first_matching_machine_id() -> None:
    choice = PlexServerChoice(
        machine_id="m1",
        name="Den",
        online=True,
        https_required=False,
        connections=(
            _connection(
                address="10.0.0.5", port=32400, uri="https://wrong.plex.direct:32400", local=True
            ),
        ),
    )
    server = FakePlexServer(
        identities_by_url={
            "http://10.0.0.5:32400": PlexIdentity(True, "m9"),
            "https://wrong.plex.direct:32400": PlexIdentity(True, "m1"),
        }
    )

    candidate = await find_connection(server, choice, None)

    assert candidate == PlexCandidate(
        base_url="https://wrong.plex.direct:32400", port=32400, on_this_nas=False
    )
    assert set(server.identity_calls) == {
        "http://10.0.0.5:32400",
        "https://wrong.plex.direct:32400",
    }


async def test_find_connection_is_none_when_nothing_matches() -> None:
    choice = PlexServerChoice(
        machine_id="m1",
        name="Den",
        online=True,
        https_required=False,
        connections=(
            _connection(
                address="10.0.0.5", port=32400, uri="https://wrong.plex.direct:32400", local=True
            ),
        ),
    )
    server = FakePlexServer(
        identities_by_url={
            "http://10.0.0.5:32400": PlexIdentity(True, "zzz"),
            "https://wrong.plex.direct:32400": None,
        }
    )

    assert await find_connection(server, choice, None) is None


async def test_find_connection_is_none_with_no_candidates_at_all() -> None:
    choice = PlexServerChoice(
        machine_id="m1", name="Den", online=True, https_required=False, connections=()
    )
    server = FakePlexServer()

    assert await find_connection(server, choice, None) is None
    assert server.identity_calls == []


# --- FakePlexServer: identities_by_url / identity_calls -------------------------


async def test_fake_plex_server_identities_by_url_ignores_the_drain_list() -> None:
    fake = FakePlexServer(
        identities=[PlexIdentity(True, "should-never-be-used")],
        identities_by_url={"http://host:32400": PlexIdentity(True, "m1")},
    )

    assert await fake.identity("http://host:32400") == PlexIdentity(True, "m1")
    assert await fake.identity("http://other:32400") is None
    assert fake.identity_calls == ["http://host:32400", "http://other:32400"]


# --- existing_plex.json ----------------------------------------------------------


def _existing_plex_record(**overrides: object) -> ExistingPlex:
    fields: dict[str, object] = {
        "machine_id": "m1",
        "name": "Den",
        "base_url": "http://192.168.1.20:32400",
        "port": 32400,
        "on_this_nas": False,
        "token": "server-secret-token",
        "folders": {"movies": "added", "tv": "not_seen"},
        "sections": {"movies": "1"},
        "replaces_link": None,
    }
    fields.update(overrides)
    return ExistingPlex(**fields)  # type: ignore[arg-type]


def test_existing_plex_json_round_trips(tmp_path: Path) -> None:
    record = _existing_plex_record()

    save_existing_plex(tmp_path, record)

    assert load_existing_plex(tmp_path) == record


def test_existing_plex_json_is_0600(tmp_path: Path) -> None:
    save_existing_plex(tmp_path, _existing_plex_record())

    written = tmp_path / "existing_plex.json"
    assert (written.stat().st_mode & 0o777) == 0o600


def test_a_missing_existing_plex_json_loads_as_none(tmp_path: Path) -> None:
    assert load_existing_plex(tmp_path) is None


def test_a_corrupt_existing_plex_json_loads_as_none(tmp_path: Path) -> None:
    (tmp_path / "existing_plex.json").write_text("not json at all")

    assert load_existing_plex(tmp_path) is None


def test_an_empty_existing_plex_json_loads_as_none(tmp_path: Path) -> None:
    (tmp_path / "existing_plex.json").write_text("")

    assert load_existing_plex(tmp_path) is None


def test_an_existing_plex_json_with_the_wrong_version_loads_as_none(tmp_path: Path) -> None:
    payload = {
        "version": 2,
        "machine_id": "m1",
        "name": "Den",
        "base_url": "http://192.168.1.20:32400",
        "port": 32400,
        "on_this_nas": False,
        "token": "tok",
        "folders": {},
        "sections": {},
        "replaces_link": None,
    }
    (tmp_path / "existing_plex.json").write_text(json.dumps(payload))

    assert load_existing_plex(tmp_path) is None


def test_an_existing_plex_json_with_a_bad_folder_state_loads_as_none(tmp_path: Path) -> None:
    payload = {
        "version": 1,
        "machine_id": "m1",
        "name": "Den",
        "base_url": "http://192.168.1.20:32400",
        "port": 32400,
        "on_this_nas": False,
        "token": "tok",
        "folders": {"movies": "sideways"},
        "sections": {},
        "replaces_link": None,
    }
    (tmp_path / "existing_plex.json").write_text(json.dumps(payload))

    assert load_existing_plex(tmp_path) is None


def test_an_existing_plex_json_with_a_non_int_port_loads_as_none(tmp_path: Path) -> None:
    payload = {
        "version": 1,
        "machine_id": "m1",
        "name": "Den",
        "base_url": "http://192.168.1.20:32400",
        "port": "32400",
        "on_this_nas": False,
        "token": "tok",
        "folders": {},
        "sections": {},
        "replaces_link": None,
    }
    (tmp_path / "existing_plex.json").write_text(json.dumps(payload))

    assert load_existing_plex(tmp_path) is None


def test_clear_existing_plex_never_raises_when_nothing_was_ever_written(tmp_path: Path) -> None:
    clear_existing_plex(tmp_path)


def test_clear_existing_plex_removes_the_file(tmp_path: Path) -> None:
    save_existing_plex(tmp_path, _existing_plex_record())

    clear_existing_plex(tmp_path)

    assert not (tmp_path / "existing_plex.json").exists()


def test_update_existing_plex_replaces_folders_and_sections_only(tmp_path: Path) -> None:
    save_existing_plex(tmp_path, _existing_plex_record())

    updated = update_existing_plex(
        tmp_path, folders={"movies": "added", "tv": "not_seen"}, sections={"movies": "7"}
    )

    assert updated is True
    record = load_existing_plex(tmp_path)
    assert record is not None
    assert record.folders == {"movies": "added", "tv": "not_seen"}
    assert record.sections == {"movies": "7"}
    assert record.machine_id == "m1"
    assert record.token == "server-secret-token"
    assert record.replaces_link is None


def test_update_existing_plex_with_no_saved_record_returns_false(tmp_path: Path) -> None:
    assert update_existing_plex(tmp_path, folders={}, sections={}) is False
    assert not (tmp_path / "existing_plex.json").exists()


def test_repr_of_existing_plex_hides_the_token() -> None:
    record = _existing_plex_record(token="super-secret-server-token")

    assert "super-secret-server-token" not in repr(record)
    assert "m1" in repr(record)


def test_existing_plex_web_url_on_this_nas_uses_the_browsers_own_address() -> None:
    record = _existing_plex_record(on_this_nas=True, port=32400)

    assert existing_plex_web_url(record, "nas.local:7788") == "http://nas.local:32400/web"


def test_existing_plex_web_url_off_this_nas_uses_the_chosen_address() -> None:
    record = _existing_plex_record(on_this_nas=False, base_url="https://d.plex.direct:32400")

    assert existing_plex_web_url(record, "nas.local:7788") == "https://d.plex.direct:32400/web"


def test_existing_plex_web_url_on_this_nas_with_no_browser_address_is_none() -> None:
    record = _existing_plex_record(on_this_nas=True, port=32400)

    assert existing_plex_web_url(record, None) is None


# --- folder markers: never touch a folder that isn't a Marrquee marker ----------


def _media_settings_and_root(
    tmp_path: Path, media_folder: str = "movies"
) -> tuple[Settings, PurePosixPath]:
    (tmp_path / "volume1" / "media" / "data" / "media" / media_folder).mkdir(parents=True)
    return Settings(host_mount=tmp_path), PurePosixPath("/volume1/media")


def _media_folder_path(tmp_path: Path, media_folder: str = "movies") -> Path:
    return tmp_path / "volume1" / "media" / "data" / "media" / media_folder


def test_make_folder_marker_creates_an_empty_named_folder(tmp_path: Path) -> None:
    settings, root = _media_settings_and_root(tmp_path)

    name = make_folder_marker(settings, root, "movies", marker_name="marrquee-plex-check-deadbeef")

    assert name == "marrquee-plex-check-deadbeef"
    created = _media_folder_path(tmp_path) / name
    assert created.is_dir()
    assert not any(created.iterdir())


def test_make_folder_marker_defaults_to_a_random_prefixed_name(tmp_path: Path) -> None:
    settings, root = _media_settings_and_root(tmp_path)

    name = make_folder_marker(settings, root, "movies")

    assert name.startswith(PROBE_FOLDER_PREFIX)
    assert len(name) > len(PROBE_FOLDER_PREFIX)


def test_make_folder_marker_propagates_a_path_that_escapes_the_root(tmp_path: Path) -> None:
    settings, root = _media_settings_and_root(tmp_path)
    media = tmp_path / "volume1" / "media" / "data" / "media"
    outside = tmp_path / "outside"
    outside.mkdir()
    (media / "movies").rmdir()
    (media / "movies").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathEscapesRoot):
        make_folder_marker(settings, root, "movies", marker_name="marrquee-plex-check-escape")


def test_remove_folder_marker_removes_an_empty_marker(tmp_path: Path) -> None:
    settings, root = _media_settings_and_root(tmp_path)
    name = make_folder_marker(settings, root, "movies", marker_name="marrquee-plex-check-abc123")
    folder = _media_folder_path(tmp_path) / name

    remove_folder_marker(settings, root, "movies", name)

    assert not folder.exists()


def test_remove_folder_marker_refuses_a_name_without_the_prefix(tmp_path: Path) -> None:
    settings, root = _media_settings_and_root(tmp_path)
    real_folder = _media_folder_path(tmp_path) / "Inception (2010)"
    real_folder.mkdir()

    remove_folder_marker(settings, root, "movies", "Inception (2010)")

    assert real_folder.is_dir()


def test_remove_folder_marker_never_removes_a_non_empty_folder(tmp_path: Path) -> None:
    settings, root = _media_settings_and_root(tmp_path)
    marker = _media_folder_path(tmp_path) / "marrquee-plex-check-full"
    marker.mkdir()
    (marker / "movie.mkv").write_text("not empty")

    remove_folder_marker(settings, root, "movies", "marrquee-plex-check-full")

    assert marker.is_dir()
    assert (marker / "movie.mkv").exists()


def test_remove_folder_marker_never_removes_a_file(tmp_path: Path) -> None:
    settings, root = _media_settings_and_root(tmp_path)
    stray = _media_folder_path(tmp_path) / "marrquee-plex-check-file"
    stray.write_text("not a folder")

    remove_folder_marker(settings, root, "movies", "marrquee-plex-check-file")

    assert stray.is_file()


def test_remove_folder_marker_never_follows_a_symlink(tmp_path: Path) -> None:
    settings, root = _media_settings_and_root(tmp_path)
    movies = _media_folder_path(tmp_path)
    real_target = movies / "Inception (2010)"
    real_target.mkdir()
    symlink = movies / "marrquee-plex-check-symlink"
    symlink.symlink_to(real_target, target_is_directory=True)

    remove_folder_marker(settings, root, "movies", "marrquee-plex-check-symlink")

    assert symlink.is_symlink()
    assert real_target.is_dir()


def test_remove_folder_marker_never_raises_when_the_marker_is_already_gone(
    tmp_path: Path,
) -> None:
    settings, root = _media_settings_and_root(tmp_path)

    remove_folder_marker(settings, root, "movies", "marrquee-plex-check-never-existed")


def test_remove_stale_folder_markers_removes_only_empty_prefix_named_folders(
    tmp_path: Path,
) -> None:
    settings, root = _media_settings_and_root(tmp_path)
    movies = _media_folder_path(tmp_path)
    stale = movies / "marrquee-plex-check-stale"
    stale.mkdir()
    real_folder = movies / "Inception (2010)"
    real_folder.mkdir()
    (real_folder / "movie.mkv").write_text("do not touch")
    non_empty_marker = movies / "marrquee-plex-check-full"
    non_empty_marker.mkdir()
    (non_empty_marker / "leftover.txt").write_text("leftover")

    remove_stale_folder_markers(settings, root, "movies")

    assert not stale.exists()
    assert real_folder.is_dir()
    assert (real_folder / "movie.mkv").exists()
    assert non_empty_marker.is_dir()
    assert (non_empty_marker / "leftover.txt").exists()


def test_remove_stale_folder_markers_never_raises_on_a_missing_folder(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    root = PurePosixPath("/volume1/media")

    remove_stale_folder_markers(settings, root, "movies")


# --- probe_folder: proves visibility with a real marker, never leaks the token --


async def test_probe_folder_counts_only_the_unique_folder_then_removes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, root = _media_settings_and_root(tmp_path)
    monkeypatch.setattr(secrets, "token_hex", lambda n: "deadbeef")
    marker_name = f"{PROBE_FOLDER_PREFIX}deadbeef"
    host_candidate = str(host_media_path(str(root), "movies"))
    container_candidate = str(container_media_path("movies"))
    server = FakePlexServer(
        script={
            ("GET", browse_path(host_candidate)): [
                PlexResponse(
                    ok=True,
                    status=200,
                    payload={
                        "MediaContainer": {
                            "Path": [{"path": f"{host_candidate}/other", "title": "other"}]
                        }
                    },
                    detail=None,
                )
            ],
            ("GET", browse_path(container_candidate)): [
                PlexResponse(
                    ok=True,
                    status=200,
                    payload={
                        "MediaContainer": {
                            "Path": [
                                {
                                    "path": f"{container_candidate}/{marker_name}",
                                    "title": marker_name,
                                }
                            ]
                        }
                    },
                    detail=None,
                )
            ],
        }
    )

    result = await probe_folder(server, "http://host:32400", "tok", settings, root, "movies")

    assert result == FolderSeen(state="seen", path=container_candidate, technical=None)
    assert not (_media_folder_path(tmp_path) / marker_name).exists()


async def test_probe_folder_is_not_seen_when_neither_candidate_lists_the_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, root = _media_settings_and_root(tmp_path)
    monkeypatch.setattr(secrets, "token_hex", lambda n: "deadbeef")
    host_candidate = str(host_media_path(str(root), "movies"))
    container_candidate = str(container_media_path("movies"))
    empty_response = PlexResponse(
        ok=True, status=200, payload={"MediaContainer": {"Path": []}}, detail=None
    )
    real_folder = _media_folder_path(tmp_path) / "Inception (2010)"
    real_folder.mkdir()
    server = FakePlexServer(
        script={
            ("GET", browse_path(host_candidate)): [empty_response],
            ("GET", browse_path(container_candidate)): [empty_response],
        }
    )

    result = await probe_folder(server, "http://host:32400", "tok", settings, root, "movies")

    assert result == FolderSeen(state="not_seen", path=None, technical=None)
    assert real_folder.is_dir()
    marker_name = f"{PROBE_FOLDER_PREFIX}deadbeef"
    assert not (_media_folder_path(tmp_path) / marker_name).exists()


async def test_probe_folder_is_unknown_on_a_transient_status_and_still_removes_the_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, root = _media_settings_and_root(tmp_path)
    monkeypatch.setattr(secrets, "token_hex", lambda n: "deadbeef")
    host_candidate = str(host_media_path(str(root), "movies"))
    container_candidate = str(container_media_path("movies"))
    server = FakePlexServer(
        script={
            ("GET", browse_path(host_candidate)): [
                PlexResponse(ok=False, status=503, payload=None, detail="Service Unavailable")
            ],
            ("GET", browse_path(container_candidate)): [
                PlexResponse(ok=False, status=503, payload=None, detail="Service Unavailable")
            ],
        }
    )

    result = await probe_folder(server, "http://host:32400", "tok", settings, root, "movies")

    assert result.state == "unknown"
    marker_name = f"{PROBE_FOLDER_PREFIX}deadbeef"
    assert not (_media_folder_path(tmp_path) / marker_name).exists()


async def test_probe_folder_never_puts_the_token_in_the_request_path_or_params(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, root = _media_settings_and_root(tmp_path)
    monkeypatch.setattr(secrets, "token_hex", lambda n: "deadbeef")
    host_candidate = str(host_media_path(str(root), "movies"))
    container_candidate = str(container_media_path("movies"))
    empty_response = PlexResponse(
        ok=True, status=200, payload={"MediaContainer": {"Path": []}}, detail=None
    )
    server = FakePlexServer(
        script={
            ("GET", browse_path(host_candidate)): [empty_response],
            ("GET", browse_path(container_candidate)): [empty_response],
        }
    )

    await probe_folder(
        server, "http://host:32400", "super-secret-server-token", settings, root, "movies"
    )

    assert server.calls
    for _method, path, params in server.calls:
        assert "super-secret-server-token" not in path
        for _key, value in params:
            assert "super-secret-server-token" not in value


async def test_probe_folder_is_unknown_when_the_marker_cannot_be_made(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    root = PurePosixPath("/volume1/media")  # data/media/movies deliberately not created

    result = await probe_folder(
        FakePlexServer(), "http://host:32400", "tok", settings, root, "movies"
    )

    assert result.state == "unknown"
    assert result.technical is not None


# --- parse_plex_sections ----------------------------------------------------------


def test_parse_plex_sections_reads_directories() -> None:
    payload = {
        "MediaContainer": {
            "Directory": [
                {
                    "key": "1",
                    "type": "movie",
                    "title": "Movies",
                    "Location": [{"path": "/data/media"}],
                },
                "not-a-dict",
                {"type": "show", "title": "no key"},
            ]
        }
    }

    sections = parse_plex_sections(payload)

    assert sections == (
        PlexSection(key="1", type="movie", title="Movies", locations=("/data/media",)),
    )


def test_parse_plex_sections_never_raises_on_a_bad_shape() -> None:
    assert parse_plex_sections(None) == ()
    assert parse_plex_sections({"MediaContainer": "not-a-dict"}) == ()
    assert parse_plex_sections({"MediaContainer": {"Directory": "not-a-list"}}) == ()


# --- link_matches_plex -------------------------------------------------------------


def test_link_matches_plex_on_the_same_host_and_port() -> None:
    choice = PlexServerChoice(
        machine_id="m1",
        name="Den",
        online=True,
        https_required=False,
        connections=(
            _connection(
                address="192.168.1.20", port=32400, uri="https://d.plex.direct:32400", local=True
            ),
        ),
    )
    link = LinkCard(id="0123456789abcdef", label="My Plex", url="http://192.168.1.20:32400/web")

    assert link_matches_plex(link, choice) is True


def test_link_matches_plex_is_false_on_a_different_port() -> None:
    choice = PlexServerChoice(
        machine_id="m1",
        name="Den",
        online=True,
        https_required=False,
        connections=(
            _connection(
                address="192.168.1.20", port=32400, uri="https://d.plex.direct:32400", local=True
            ),
        ),
    )
    link = LinkCard(id="0123456789abcdef", label="My Plex", url="http://192.168.1.20:9999/web")

    assert link_matches_plex(link, choice) is False


def test_link_matches_plex_against_the_uri_form_too() -> None:
    choice = PlexServerChoice(
        machine_id="m1",
        name="Den",
        online=True,
        https_required=False,
        connections=(
            _connection(
                address="192.168.1.20", port=32400, uri="https://d.plex.direct:32400", local=True
            ),
        ),
    )
    link = LinkCard(id="0123456789abcdef", label="My Plex", url="https://d.plex.direct:32400/web")

    assert link_matches_plex(link, choice) is True

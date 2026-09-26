"""Tests for the Plex doors: plex.json, plex.tv, the auth URL, the claim
secret, and a local Plex server.

Everything here runs offline: `httpx.MockTransport` stands in for plex.tv
and a local server, and `tmp_path` stands in for the owner's drive. The
live plex.tv shapes (PIN create/poll, the claim body) are documented but
unverified - see the story's Pitch conditions - so these tests pin the
documented shape and leave the live answer PENDING the owner's NAS.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path, PurePosixPath
from typing import cast

import httpx
import pytest

from marrquee import __version__
from marrquee.config import Settings
from marrquee.docker_client import DockerEngine
from marrquee.plex import (
    PLEX_CLAIM_FILE_NAME,
    PLEX_PRODUCT,
    FakePlexServer,
    FakePlexTv,
    HttpPlexServer,
    HttpPlexTv,
    PlexAccount,
    PlexIdentity,
    PlexPin,
    PlexResponse,
    PlexServer,
    PlexTv,
    clear_plex_claim,
    load_plex_account,
    plex_auth_url,
    plex_base_url,
    plex_client_id,
    plex_host_address,
    plex_secrets_host_path,
    save_plex_sign_in,
    write_plex_claim,
)
from marrquee.storage import PathEscapesRoot

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

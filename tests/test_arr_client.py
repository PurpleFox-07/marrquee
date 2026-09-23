"""Tests for the one door every wiring HTTP call goes through.

Everything here runs with no network, no Docker and no arr app: the real
client is driven by `httpx.MockTransport`, and the fake replays a scripted
list of `ArrResponse`s. Both prove the same thing from opposite ends - the
real client turns actual HTTP semantics into an `ArrResponse` that never
raises, and the fake lets everything above it (Chunks 2 and 3) be tested
the same way.
"""

from __future__ import annotations

import httpx
import pytest

from marrquee.wiring.arr_client import (
    ArrClient,
    ArrFailure,
    ArrResponse,
    FakeArrClient,
    HttpArrClient,
)

# The shape of a real Prowlarr validation-error body, read out of
# `ProwlarrErrorPipeline` on 2026-09-19: a bare JSON array, each element
# carrying more than we read - `attemptedValue` and `severity` are noise
# this parser must tolerate rather than choke on.
_PROWLARR_400_BODY = [
    {
        "propertyName": "BaseUrl",
        "errorMessage": "Unable to connect to Sonarr",
        "attemptedValue": "http://sonarr:8989",
        "severity": "error",
        "isWarning": False,
    }
]

# Sonarr's root-folder validator writes the same array shape, naming the
# `Path` field instead.
_SONARR_ROOTFOLDER_400_BODY = [
    {
        "propertyName": "Path",
        "errorMessage": "Folder does not exist",
        "isWarning": False,
    }
]


async def test_a_real_prowlarr_400_body_becomes_failures() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=_PROWLARR_400_BODY)

    client = HttpArrClient(transport=httpx.MockTransport(handler))

    response = await client.request(
        "POST", "http://prowlarr:9696", "api/v1/applications", "prowlarr-key", json_body={}
    )

    assert response.ok is False
    assert response.status == 400
    assert response.failures == (ArrFailure("BaseUrl", "Unable to connect to Sonarr", False),)


async def test_a_sonarr_root_folder_400_becomes_a_path_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=_SONARR_ROOTFOLDER_400_BODY)

    client = HttpArrClient(transport=httpx.MockTransport(handler))

    response = await client.request(
        "POST", "http://sonarr:8989", "api/v3/rootfolder", "sonarr-key", json_body={"path": "/data"}
    )

    assert response.ok is False
    assert response.failures == (ArrFailure("Path", "Folder does not exist", False),)


async def test_a_400_that_is_an_object_keeps_its_text_in_detail_and_has_no_failures() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "something unexpected"})

    client = HttpArrClient(transport=httpx.MockTransport(handler))

    response = await client.request("GET", "http://sonarr:8989", "api/v3/rootfolder", "sonarr-key")

    assert response.ok is False
    assert response.failures == ()
    assert response.detail is not None
    assert "something unexpected" in response.detail


async def test_a_dead_app_is_an_answer_not_an_exception() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = HttpArrClient(transport=httpx.MockTransport(handler))

    response = await client.request(
        "GET", "http://sonarr:8989", "api/v3/system/status", "sonarr-key"
    )

    assert response.ok is False
    assert response.status == 0
    assert response.detail is not None
    assert "ConnectError" in response.detail


async def test_a_timeout_is_an_answer_not_an_exception() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    client = HttpArrClient(transport=httpx.MockTransport(handler))

    response = await client.request(
        "GET", "http://sonarr:8989", "api/v3/system/status", "sonarr-key"
    )

    assert response.ok is False
    assert response.status == 0
    assert response.detail is not None
    assert "TimeoutException" in response.detail


async def test_the_api_key_travels_as_a_header_and_never_in_the_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "super-secret-key"
        assert "super-secret-key" not in str(request.url)
        return httpx.Response(200)

    client = HttpArrClient(transport=httpx.MockTransport(handler))

    await client.request("GET", "http://sonarr:8989", "api/v3/system/status", "super-secret-key")


async def test_a_201_is_ok_and_its_body_is_parsed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"id": 7, "name": "Sonarr"})

    client = HttpArrClient(transport=httpx.MockTransport(handler))

    response = await client.request(
        "POST", "http://prowlarr:9696", "api/v1/applications", "prowlarr-key", json_body={}
    )

    assert response.ok is True
    assert response.status == 201
    assert response.payload == {"id": 7, "name": "Sonarr"}


async def test_a_200_with_an_empty_body_is_ok_with_payload_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    client = HttpArrClient(transport=httpx.MockTransport(handler))

    response = await client.request(
        "GET", "http://sonarr:8989", "api/v3/system/status", "sonarr-key"
    )

    assert response.ok is True
    assert response.payload is None


async def test_base_url_and_path_join_with_exactly_one_slash() -> None:
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200)

    client = HttpArrClient(transport=httpx.MockTransport(handler))

    await client.request("GET", "http://sonarr:8989/", "api/v3/rootfolder", "sonarr-key")
    await client.request("GET", "http://sonarr:8989", "/api/v3/rootfolder", "sonarr-key")

    assert seen_urls == [
        "http://sonarr:8989/api/v3/rootfolder",
        "http://sonarr:8989/api/v3/rootfolder",
    ]


async def test_the_fake_satisfies_the_protocol_replays_its_script_records_calls() -> None:
    response_one = ArrResponse(ok=True, status=200, payload=[], failures=(), detail=None)
    response_two = ArrResponse(ok=True, status=201, payload={"id": 1}, failures=(), detail=None)
    fake = FakeArrClient(
        {
            ("GET", "http://sonarr:8989", "api/v3/rootfolder"): [response_one],
            ("POST", "http://prowlarr:9696", "api/v1/applications"): [response_two],
        }
    )
    satisfies_protocol: ArrClient = fake  # a type-checked proof, not just a runtime one
    assert satisfies_protocol is fake

    first = await fake.request("GET", "http://sonarr:8989", "api/v3/rootfolder", "sonarr-key")
    second = await fake.request(
        "POST", "http://prowlarr:9696", "api/v1/applications", "prowlarr-key", json_body={"a": 1}
    )

    assert first is response_one
    assert second is response_two
    assert fake.calls == [
        ("GET", "http://sonarr:8989", "api/v3/rootfolder", None),
        ("POST", "http://prowlarr:9696", "api/v1/applications", {"a": 1}),
    ]


async def test_the_fake_names_an_unscripted_call() -> None:
    fake = FakeArrClient({})

    with pytest.raises(KeyError, match="api/v3/rootfolder"):
        await fake.request("GET", "http://sonarr:8989", "api/v3/rootfolder", "sonarr-key")

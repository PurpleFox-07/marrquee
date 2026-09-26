"""Tests for the one door every wiring HTTP call to qBittorrent goes
through: a real `httpx` implementation and an exported in-memory fake.

Mirrors `test_arr_client.py`'s split: the real client is driven by
`httpx.MockTransport` with no network involved, and the fake replays a
scripted list of `QbitResponse`s for everything above it (the login branch,
the wiring engine).
"""

from __future__ import annotations

import json

import httpx
import pytest

from marrquee.wiring.qbit_client import (
    FakeQbitClient,
    HttpQbitClient,
    QbitClient,
    QbitResponse,
    preferences_form,
)


async def test_the_key_travels_as_a_bearer_header_never_in_the_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer super-secret-key"
        assert "super-secret-key" not in str(request.url)
        return httpx.Response(200)

    client = HttpQbitClient(transport=httpx.MockTransport(handler))

    response = await client.request(
        "GET", "http://gluetun:8080", "api/v2/app/version", "super-secret-key"
    )

    assert response.ok is True
    assert response.status == 200


async def test_a_post_sends_form_encoded_data_not_json() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["content_type"] = request.headers.get("content-type")
        seen["body"] = request.content.decode()
        return httpx.Response(200, text="Ok.")

    client = HttpQbitClient(transport=httpx.MockTransport(handler))

    response = await client.request(
        "POST",
        "http://gluetun:8080",
        "api/v2/app/setPreferences",
        "a-key",
        form={"json": '{"max_ratio_act":0}'},
    )

    assert response.ok is True
    assert seen["content_type"] == "application/x-www-form-urlencoded"
    assert seen["body"] == "json=%7B%22max_ratio_act%22%3A0%7D"


async def test_a_2xx_json_body_is_parsed_into_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"max_ratio_act": 0})

    client = HttpQbitClient(transport=httpx.MockTransport(handler))

    response = await client.request("GET", "http://gluetun:8080", "api/v2/app/preferences", "key")

    assert response.ok is True
    assert response.payload == {"max_ratio_act": 0}


async def test_a_2xx_non_json_body_is_kept_as_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="Ok.")

    client = HttpQbitClient(transport=httpx.MockTransport(handler))

    response = await client.request("POST", "http://gluetun:8080", "api/v2/auth/login", "key")

    assert response.ok is True
    assert response.payload == "Ok."


async def test_a_403_is_not_ok_and_keeps_its_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Forbidden")

    client = HttpQbitClient(transport=httpx.MockTransport(handler))

    response = await client.request("GET", "http://gluetun:8080", "api/v2/app/version", "wrong-key")

    assert response.ok is False
    assert response.status == 403
    assert response.detail == "Forbidden"


async def test_a_connect_error_is_status_zero_never_raised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = HttpQbitClient(transport=httpx.MockTransport(handler))

    response = await client.request("GET", "http://gluetun:8080", "api/v2/app/version", "key")

    assert response.ok is False
    assert response.status == 0
    assert response.detail is not None
    assert "ConnectError" in response.detail


async def test_a_timeout_is_status_zero_never_raised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    client = HttpQbitClient(transport=httpx.MockTransport(handler))

    response = await client.request("GET", "http://gluetun:8080", "api/v2/app/version", "key")

    assert response.ok is False
    assert response.status == 0
    assert "TimeoutException" in (response.detail or "")


async def test_base_url_and_path_join_with_exactly_one_slash() -> None:
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200)

    client = HttpQbitClient(transport=httpx.MockTransport(handler))

    await client.request("GET", "http://gluetun:8080/", "api/v2/app/version", "key")
    await client.request("GET", "http://gluetun:8080", "/api/v2/app/version", "key")

    assert seen_urls == [
        "http://gluetun:8080/api/v2/app/version",
        "http://gluetun:8080/api/v2/app/version",
    ]


# --- preferences_form ---------------------------------------------------------


def test_preferences_form_is_a_sorted_compact_json_string() -> None:
    form = preferences_form({"b": 1, "a": True})

    assert form == {"json": '{"a":true,"b":1}'}
    assert json.loads(form["json"]) == {"a": True, "b": 1}


# --- FakeQbitClient ------------------------------------------------------------


async def test_the_fake_satisfies_the_protocol_replays_its_script_records_calls() -> None:
    response_one = QbitResponse(ok=True, status=200, payload={}, detail=None)
    response_two = QbitResponse(ok=True, status=200, payload="Ok.", detail=None)
    fake = FakeQbitClient(
        {
            ("GET", "http://gluetun:8080", "api/v2/torrents/categories"): [response_one],
            ("POST", "http://gluetun:8080", "api/v2/auth/login"): [response_two],
        }
    )
    satisfies_protocol: QbitClient = fake  # a type-checked proof, not just a runtime one
    assert satisfies_protocol is fake

    first = await fake.request("GET", "http://gluetun:8080", "api/v2/torrents/categories", "key")
    second = await fake.request(
        "POST", "http://gluetun:8080", "api/v2/auth/login", "key", form={"username": "u"}
    )

    assert first is response_one
    assert second is response_two
    assert fake.calls == [
        ("GET", "http://gluetun:8080", "api/v2/torrents/categories", None),
        ("POST", "http://gluetun:8080", "api/v2/auth/login", {"username": "u"}),
    ]


async def test_the_fake_raises_key_error_for_an_unscripted_call() -> None:
    fake = FakeQbitClient({})

    with pytest.raises(KeyError):
        await fake.request("GET", "http://gluetun:8080", "api/v2/app/version", "key")


async def test_the_fake_raises_key_error_once_its_queue_is_drained() -> None:
    fake = FakeQbitClient(
        {("GET", "http://gluetun:8080", "api/v2/app/version"): [QbitResponse(True, 200, {}, None)]}
    )

    await fake.request("GET", "http://gluetun:8080", "api/v2/app/version", "key")

    with pytest.raises(KeyError):
        await fake.request("GET", "http://gluetun:8080", "api/v2/app/version", "key")


def test_qbit_response_is_a_frozen_dataclass() -> None:
    import dataclasses

    response = QbitResponse(ok=True, status=200, payload=None, detail=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        response.ok = False  # type: ignore[misc]

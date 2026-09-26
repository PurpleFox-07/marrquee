"""Tests for the per-app login appliers: `HttpLoginApplier`, `NoLoginApplier`
and the exported `FakeLoginApplier`.

Nothing here touches a real network - `FakeArrClient` (the wiring seam's own
in-memory double) scripts every HTTP call, which is what lets the FIRST test
below pin the exact request shape the Pitch stands on before any real Docker
is involved.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from marrquee.catalog import get_app
from marrquee.login import SavedLogin
from marrquee.login_apply import (
    FakeLoginApplier,
    HttpLoginApplier,
    LoginApplyResult,
    NoLoginApplier,
)
from marrquee.state import InstallState
from marrquee.wiring.arr_client import ArrFailure, ArrResponse, FakeArrClient
from marrquee.wiring.qbit_client import FakeQbitClient, QbitResponse

_SONARR = get_app("sonarr")


def _install(
    app_ids: tuple[str, ...] = ("sonarr",), api_keys: dict[str, str] | None = None
) -> InstallState:
    return InstallState(
        version=2,
        storage_root="/volume1/media",
        app_ids=app_ids,
        api_keys=api_keys if api_keys is not None else {"sonarr": "sonarr-api-key"},
        puid=1000,
        pgid=1000,
        umask="002",
        timezone="Etc/UTC",
        created="2026-09-24T00:00:00+00:00",
    )


def _login(*, generation: int = 1, password: str = "s3cret-pass") -> SavedLogin:
    return SavedLogin(username="owner", generation=generation, password=password)


_GET_KEY = ("GET", "http://sonarr:8989", "api/v3/config/host")
_PUT_KEY = ("PUT", "http://sonarr:8989", "api/v3/config/host")


# --- FIRST TEST: the Pitch's exact request shape -----------------------------


async def test_first_puts_the_login_through_config_host_get_then_put() -> None:
    """FIRST TEST - the Pitch's weakest assumption made concrete: does the
    applier really GET config/host then PUT it back with forms/enabled/the
    username/password/confirmation, keeping whatever `id` the app already
    reported?
    """
    client = FakeArrClient(
        {
            _GET_KEY: [
                ArrResponse(
                    ok=True,
                    status=200,
                    payload={
                        "id": 1,
                        "authenticationMethod": "external",
                        "authenticationRequired": "disabledForLocalAddresses",
                        "username": "",
                        "password": "",
                    },
                    failures=(),
                    detail=None,
                )
            ],
            _PUT_KEY: [ArrResponse(ok=True, status=202, payload=None, failures=(), detail=None)],
        }
    )
    applier = HttpLoginApplier(client)
    install = _install()
    login = _login()

    result = await applier.apply(_SONARR, install, login)

    assert result.ok is True
    assert len(client.calls) == 2
    get_call, put_call = client.calls
    assert get_call[:3] == ("GET", "http://sonarr:8989", "api/v3/config/host")
    assert put_call[:3] == ("PUT", "http://sonarr:8989", "api/v3/config/host")
    body = put_call[3]
    assert isinstance(body, dict)
    assert body["id"] == 1
    assert body["authenticationMethod"] == "forms"
    assert body["authenticationRequired"] == "enabled"
    assert body["username"] == "owner"
    assert body["password"] == "s3cret-pass"
    assert body["passwordConfirmation"] == "s3cret-pass"


# --- failure shape: never the payload, always redacted -----------------------


async def test_a_400_with_failures_reports_property_and_message_never_the_payload() -> None:
    client = FakeArrClient(
        {
            _GET_KEY: [
                ArrResponse(ok=True, status=200, payload={"id": 1}, failures=(), detail=None)
            ],
            _PUT_KEY: [
                ArrResponse(
                    ok=False,
                    status=400,
                    payload=[{"propertyName": "Password", "errorMessage": "too short"}],
                    failures=(
                        ArrFailure(
                            property_name="Password", error_message="too short", is_warning=False
                        ),
                    ),
                    detail=None,
                )
            ],
        }
    )
    applier = HttpLoginApplier(client)

    result = await applier.apply(_SONARR, _install(), _login(password="attemptedValueSecret1"))

    assert result.ok is False
    assert result.technical is not None
    assert "Password: too short" in result.technical
    # `technical` is built only from the failure's own property/message -
    # never the request payload - so a FluentValidation body echoing back
    # `attemptedValue` never even reaches this string in the first place.
    assert "attemptedValueSecret1" not in result.technical


async def test_a_failure_technical_never_leaks_the_password() -> None:
    client = FakeArrClient(
        {
            _GET_KEY: [
                ArrResponse(
                    ok=False,
                    status=400,
                    payload=None,
                    failures=(),
                    detail="body echoed s3cret-pass back",
                )
            ],
        }
    )
    applier = HttpLoginApplier(client)

    result = await applier.apply(_SONARR, _install(), _login(password="s3cret-pass"))

    assert result.ok is False
    assert result.technical is not None
    assert "s3cret-pass" not in result.technical
    assert "<redacted-password>" in result.technical


# --- retry: only 0 or >=500 --------------------------------------------------


async def test_a_503_is_retried_a_400_is_not() -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = FakeArrClient(
        {
            _GET_KEY: [
                ArrResponse(ok=False, status=503, payload=None, failures=(), detail="unavailable"),
                ArrResponse(ok=True, status=200, payload={"id": 1}, failures=(), detail=None),
            ],
            _PUT_KEY: [ArrResponse(ok=True, status=202, payload=None, failures=(), detail=None)],
        }
    )
    applier = HttpLoginApplier(client, sleep=fake_sleep, attempts=3, retry_delay=0.01)

    result = await applier.apply(_SONARR, _install(), _login())

    assert result.ok is True
    assert len(sleeps) == 1
    # 2 GET attempts (1 retried) + 1 PUT
    assert len(client.calls) == 3


async def test_a_400_is_never_retried() -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = FakeArrClient(
        {
            _GET_KEY: [
                ArrResponse(ok=False, status=400, payload=None, failures=(), detail="bad request")
            ],
        }
    )
    applier = HttpLoginApplier(client, sleep=fake_sleep, attempts=3, retry_delay=0.01)

    result = await applier.apply(_SONARR, _install(), _login())

    assert result.ok is False
    assert sleeps == []
    assert len(client.calls) == 1


# --- missing key, non-arr kind ------------------------------------------------


async def test_a_missing_api_key_is_not_ok_and_makes_no_request() -> None:
    client = FakeArrClient({})
    applier = HttpLoginApplier(client)
    install = _install(app_ids=(), api_keys={})

    result = await applier.apply(_SONARR, install, _login())

    assert result.ok is False
    assert client.calls == []


async def test_a_non_dict_get_payload_is_not_ok_and_sends_no_put() -> None:
    client = FakeArrClient(
        {
            _GET_KEY: [
                ArrResponse(ok=True, status=200, payload=["unexpected"], failures=(), detail=None)
            ]
        }
    )
    applier = HttpLoginApplier(client)

    result = await applier.apply(_SONARR, _install(), _login())

    assert result.ok is False
    assert len(client.calls) == 1


async def test_a_non_arr_kind_is_not_ok_and_makes_no_request() -> None:
    non_login_app = dataclasses.replace(get_app("prowlarr"), login_kind="none")
    client = FakeArrClient({})
    applier = HttpLoginApplier(client)
    install = _install(app_ids=("prowlarr",), api_keys={"prowlarr": "k"})

    result = await applier.apply(non_login_app, install, _login())

    assert result.ok is False
    assert client.calls == []


# --- NoLoginApplier: honest, never records success ---------------------------


async def test_no_login_applier_is_always_not_ok() -> None:
    applier = NoLoginApplier()

    result = await applier.apply(_SONARR, _install(), _login())

    assert result.ok is False
    assert result.technical == "no login applier configured"


# --- FakeLoginApplier: scripted, records username and generation but never the password


async def test_fake_login_applier_records_calls_without_the_password() -> None:
    applier = FakeLoginApplier(results={"sonarr": False})

    result = await applier.apply(_SONARR, _install(), _login(generation=3, password="whatever-1"))

    assert result.ok is False
    assert applier.calls == [("sonarr", "owner", 3)]
    assert "whatever-1" not in str(applier.calls)


async def test_fake_login_applier_defaults_to_ok() -> None:
    applier = FakeLoginApplier()

    result = await applier.apply(_SONARR, _install(), _login())

    assert result.ok is True


def test_login_apply_result_is_a_frozen_dataclass() -> None:
    result = LoginApplyResult(ok=True, technical=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.ok = False  # type: ignore[misc]


# --- the qBittorrent branch: the key, never the old password -----------------

_QBITTORRENT = get_app("qbittorrent")
_QBIT_SET_PREFS_KEY = ("POST", "http://gluetun:8080", "api/v2/app/setPreferences")


def _qbit_install(api_keys: dict[str, str] | None = None) -> InstallState:
    return InstallState(
        version=2,
        storage_root="/volume1/media",
        app_ids=("gluetun", "qbittorrent"),
        api_keys=api_keys if api_keys is not None else {"qbittorrent": "qbt_" + "a" * 28},
        puid=1000,
        pgid=1000,
        umask="002",
        timezone="Etc/UTC",
        created="2026-09-24T00:00:00+00:00",
    )


async def test_the_qbittorrent_branch_posts_username_and_password_with_the_key() -> None:
    qbit = FakeQbitClient(
        {_QBIT_SET_PREFS_KEY: [QbitResponse(ok=True, status=200, payload="Ok.", detail=None)]}
    )
    applier = HttpLoginApplier(qbit=qbit)
    login = _login(password="new-owner-password")

    result = await applier.apply(_QBITTORRENT, _qbit_install(), login)

    assert result.ok is True
    assert len(qbit.calls) == 1
    method, base_url, path, form = qbit.calls[0]
    assert (method, base_url, path) == _QBIT_SET_PREFS_KEY
    assert form is not None
    body = json.loads(form["json"])
    assert body == {"web_ui_username": "owner", "web_ui_password": "new-owner-password"}


async def test_the_qbittorrent_branch_defaults_to_a_real_http_client_when_none_is_given() -> None:
    # CI calls a bare HttpLoginApplier() - the qbit keyword must default to a
    # real HttpQbitClient, the same way `client` defaults to HttpArrClient.
    applier = HttpLoginApplier()

    assert applier._qbit is not None


async def test_the_qbittorrent_branch_retries_a_503_never_a_400() -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    qbit = FakeQbitClient(
        {
            _QBIT_SET_PREFS_KEY: [
                QbitResponse(ok=False, status=503, payload=None, detail="unavailable"),
                QbitResponse(ok=True, status=200, payload="Ok.", detail=None),
            ]
        }
    )
    applier = HttpLoginApplier(qbit=qbit, sleep=fake_sleep, attempts=3, retry_delay=0.01)

    result = await applier.apply(_QBITTORRENT, _qbit_install(), _login())

    assert result.ok is True
    assert len(sleeps) == 1
    assert len(qbit.calls) == 2


async def test_the_qbittorrent_branch_never_retries_a_400() -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    qbit = FakeQbitClient(
        {_QBIT_SET_PREFS_KEY: [QbitResponse(ok=False, status=400, payload=None, detail="bad")]}
    )
    applier = HttpLoginApplier(qbit=qbit, sleep=fake_sleep, attempts=3, retry_delay=0.01)

    result = await applier.apply(_QBITTORRENT, _qbit_install(), _login())

    assert result.ok is False
    assert sleeps == []
    assert len(qbit.calls) == 1


async def test_the_qbittorrent_branch_technical_never_contains_the_password() -> None:
    qbit = FakeQbitClient(
        {
            _QBIT_SET_PREFS_KEY: [
                QbitResponse(
                    ok=False, status=400, payload=None, detail="body echoed s3cret-pw back"
                )
            ]
        }
    )
    applier = HttpLoginApplier(qbit=qbit)

    result = await applier.apply(_QBITTORRENT, _qbit_install(), _login(password="s3cret-pw"))

    assert result.ok is False
    assert result.technical is not None
    assert "s3cret-pw" not in result.technical
    assert "<redacted-password>" in result.technical


async def test_the_qbittorrent_branch_with_a_missing_key_is_not_ok_and_makes_no_request() -> None:
    qbit = FakeQbitClient({})
    applier = HttpLoginApplier(qbit=qbit)

    result = await applier.apply(_QBITTORRENT, _qbit_install(api_keys={}), _login())

    assert result.ok is False
    assert qbit.calls == []


async def test_the_qbittorrent_branch_runs_before_the_arr_only_guard() -> None:
    """qBittorrent's `login_kind` is "qbittorrent", not "arr" - if the
    qbittorrent branch didn't come first, the arr-only guard would refuse
    it outright and this would fail with "does not take a login".
    """
    qbit = FakeQbitClient(
        {_QBIT_SET_PREFS_KEY: [QbitResponse(ok=True, status=200, payload="Ok.", detail=None)]}
    )
    applier = HttpLoginApplier(qbit=qbit)

    result = await applier.apply(_QBITTORRENT, _qbit_install(), _login())

    assert result.ok is True

"""The one door every wiring HTTP call to Prowlarr, Sonarr or Radarr goes
through: a real `httpx` implementation and an exported in-memory fake.

Nothing above this module ever touches `httpx` directly - a connection
that never answers, a timeout, a validation error and a clean success all
become the same `ArrResponse` shape, so Chunks 2 and 3 write one kind of
code no matter which of those actually happened.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx


@dataclass(frozen=True)
class ArrFailure:
    """One field Prowlarr or Sonarr/Radarr refused, read out of a 400 body."""

    property_name: str
    error_message: str
    is_warning: bool


@dataclass(frozen=True)
class ArrResponse:
    """What one call to an arr app's API came back with.

    `detail` is raw technical text - the connect-error message, or a
    non-array error body's own text - and is for diagnostics only; nothing
    that reaches the owner may read it directly.
    """

    ok: bool
    status: int
    payload: object
    failures: tuple[ArrFailure, ...]
    detail: str | None


class ArrClient(Protocol):
    """Talks to one arr app's API. Never raises - see `HttpArrClient.request`."""

    async def request(
        self,
        method: Literal["GET", "POST", "PUT"],
        base_url: str,
        path: str,
        api_key: str,
        *,
        json_body: object | None = None,
    ) -> ArrResponse: ...


class HttpArrClient:
    """The real `ArrClient`, over `httpx`.

    A fresh `AsyncClient` per call - a deploy makes a handful of these in
    total, so there is no connection pool worth keeping warm across calls.
    """

    def __init__(
        self, *, timeout: float = 30.0, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        # 30s, not the readiness probe's 3s: adding an application makes
        # Prowlarr call the target app, which calls Prowlarr back, before
        # Prowlarr answers us - a short timeout would read that live
        # round-trip as a dead app.
        self._timeout = timeout
        self._transport = transport

    async def request(
        self,
        method: Literal["GET", "POST", "PUT"],
        base_url: str,
        path: str,
        api_key: str,
        *,
        json_body: object | None = None,
    ) -> ArrResponse:
        url = _join(base_url, path)
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout
            ) as client:
                response = await client.request(
                    method, url, headers={"X-Api-Key": api_key}, json=json_body
                )
        except httpx.HTTPError as error:
            # Covers connect errors and timeouts alike: "couldn't reach it"
            # is a real answer, not something wiring should crash over.
            return ArrResponse(
                ok=False,
                status=0,
                payload=None,
                failures=(),
                detail=f"{type(error).__name__}: {error}",
            )
        return _parse_response(response)


def _join(base_url: str, path: str) -> str:
    """Join with exactly one slash, whatever slashes either side carries.

    httpx does not collapse a double slash in a URL - `base_url` ending in
    "/" plus `path` starting with "/" would otherwise 404 against both
    apps.
    """
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _parse_response(response: httpx.Response) -> ArrResponse:
    ok = 200 <= response.status_code < 300
    payload: object = None
    if response.content:
        try:
            payload = response.json()
        except ValueError:
            payload = None
    if ok:
        return ArrResponse(
            ok=True, status=response.status_code, payload=payload, failures=(), detail=None
        )
    if isinstance(payload, list):
        return ArrResponse(
            ok=False,
            status=response.status_code,
            payload=payload,
            failures=tuple(
                _failure_from_element(item) for item in payload if isinstance(item, dict)
            ),
            detail=None,
        )
    detail = response.text if response.content else None
    return ArrResponse(
        ok=False, status=response.status_code, payload=payload, failures=(), detail=detail
    )


def _failure_from_element(item: Mapping[str, object]) -> ArrFailure:
    return ArrFailure(
        property_name=str(item.get("propertyName", "")),
        error_message=str(item.get("errorMessage", "")),
        is_warning=bool(item.get("isWarning", False)),
    )


class FakeArrClient:
    """A scriptable `ArrClient` for tests - no network involved.

    `script` maps `(method, base_url, path)` to a queue of `ArrResponse`s,
    drained in order; a call with no matching key, or an exhausted queue,
    raises `KeyError` naming exactly what was unscripted rather than
    guessing an answer.
    """

    def __init__(self, script: Mapping[tuple[str, str, str], Sequence[ArrResponse]]) -> None:
        self._queues: dict[tuple[str, str, str], list[ArrResponse]] = {
            key: list(responses) for key, responses in script.items()
        }
        self.calls: list[tuple[str, str, str, object | None]] = []

    async def request(
        self,
        method: Literal["GET", "POST", "PUT"],
        base_url: str,
        path: str,
        api_key: str,
        *,
        json_body: object | None = None,
    ) -> ArrResponse:
        self.calls.append((method, base_url, path, json_body))
        key = (method, base_url, path)
        queue = self._queues.get(key)
        if not queue:
            raise KeyError(f"no scripted response left for {key!r}")
        return queue.pop(0)

"""The one door every wiring HTTP call to qBittorrent goes through: a real
`httpx` implementation and an exported in-memory fake.

Mirrors `arr_client.py`'s split, but qBittorrent is not an arr app: there is
no `X-Api-Key` header, no cookie login and no FluentValidation body to read
failures out of. Every call carries `Authorization: Bearer <key>` instead -
a qBittorrent API-key session skips the CSRF check entirely
(`webapplication.cpp:659`), and `doProcessRequest` refuses a key only on the
`auth` scope (`:331-341`), so this is the one door every non-auth call
(preferences, categories, torrents) goes through with no session dance at
all.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx


@dataclass(frozen=True)
class QbitResponse:
    """What one call to qBittorrent's API came back with.

    `detail` is raw technical text - a connect-error message, or a non-JSON
    body's own text - for diagnostics only, same contract as `ArrResponse`.
    """

    ok: bool
    status: int
    payload: object
    detail: str | None


class QbitClient(Protocol):
    """Talks to qBittorrent's API. Never raises - see `HttpQbitClient.request`."""

    async def request(
        self,
        method: Literal["GET", "POST"],
        base_url: str,
        path: str,
        api_key: str,
        *,
        form: Mapping[str, str] | None = None,
    ) -> QbitResponse: ...


class HttpQbitClient:
    """The real `QbitClient`, over `httpx`.

    A fresh `AsyncClient` per call - the same choice `HttpArrClient` makes,
    for the same reason: a deploy or a wiring run makes a handful of these
    calls in total, so there is no connection pool worth keeping warm.
    """

    def __init__(
        self, *, timeout: float = 10.0, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._timeout = timeout
        self._transport = transport

    async def request(
        self,
        method: Literal["GET", "POST"],
        base_url: str,
        path: str,
        api_key: str,
        *,
        form: Mapping[str, str] | None = None,
    ) -> QbitResponse:
        url = _join(base_url, path)
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout
            ) as client:
                response = await client.request(
                    method,
                    url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    data=form,
                )
        except httpx.HTTPError as error:
            # Covers connect errors and timeouts alike - "couldn't reach
            # it" is a real answer, never a crash, the same as
            # `HttpArrClient`.
            return QbitResponse(
                ok=False, status=0, payload=None, detail=f"{type(error).__name__}: {error}"
            )
        return _parse_response(response)


def _join(base_url: str, path: str) -> str:
    """Join with exactly one slash, whatever slashes either side carries."""
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _parse_response(response: httpx.Response) -> QbitResponse:
    ok = 200 <= response.status_code < 300
    payload: object = None
    if response.content:
        try:
            payload = response.json()
        except ValueError:
            payload = response.text
    if ok:
        return QbitResponse(ok=True, status=response.status_code, payload=payload, detail=None)
    detail = response.text if response.content else None
    return QbitResponse(ok=False, status=response.status_code, payload=payload, detail=detail)


def preferences_form(prefs: Mapping[str, object]) -> dict[str, str]:
    """The form body `app/setPreferences` requires: one `json` field holding
    a compact, deterministically-ordered JSON object (`requireParams({"json"})`,
    `appcontroller.cpp:513-519`).

    Sorted keys and a fixed separator are what make two calls with the same
    preferences produce byte-identical form bodies - useful for a test
    asserting the exact posted string, and harmless everywhere else.
    """
    return {"json": json.dumps(prefs, sort_keys=True, separators=(",", ":"))}


class FakeQbitClient:
    """A scriptable `QbitClient` for tests - no network involved.

    `script` maps `(method, base_url, path)` to a queue of `QbitResponse`s,
    drained in order; a call with no matching key, or an exhausted queue,
    raises `KeyError` naming exactly what was unscripted rather than
    guessing an answer - the same contract `FakeArrClient` makes.
    """

    def __init__(self, script: Mapping[tuple[str, str, str], Sequence[QbitResponse]]) -> None:
        self._queues: dict[tuple[str, str, str], list[QbitResponse]] = {
            key: list(responses) for key, responses in script.items()
        }
        self.calls: list[tuple[str, str, str, Mapping[str, str] | None]] = []

    async def request(
        self,
        method: Literal["GET", "POST"],
        base_url: str,
        path: str,
        api_key: str,
        *,
        form: Mapping[str, str] | None = None,
    ) -> QbitResponse:
        self.calls.append((method, base_url, path, form))
        key = (method, base_url, path)
        queue = self._queues.get(key)
        if not queue:
            raise KeyError(f"no scripted response left for {key!r}")
        return queue.pop(0)

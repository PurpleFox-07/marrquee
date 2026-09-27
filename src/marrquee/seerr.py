"""The Seerr door: `SeerrClient`, its fake, sign-in resolution and first-run
setup.

This is a leaf module - it imports nothing from `deploy`, `wiring` or
`words`, so both the deploy engine and the wiring engine can import it
without a cycle. `HttpSeerrClient` follows `wiring/arr_client.py`'s and
`jellyfin.py`'s own shape: a real `httpx` door and an exported in-memory
fake, with every transport exception, timeout and bad body funnelled into
the same never-raising result - a dead Seerr is a real answer, never a
crash.

Seerr's API key, the owner's Plex token and the one login's password are
the three secrets this module ever touches. The key travels only in the
`X-Api-Key` header; the token and the password travel only in the JSON
body of `POST auth/{plex,jellyfin}`, Seerr's own contract for making its
first admin. None of the three is ever folded into a `technical` string -
see `_technical`'s own docstring - and none of them ever reaches a URL, a
query string or a log line.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, Protocol

import httpx

from marrquee.catalog import (
    EXISTING_PLEX_APP_ID,
    JELLYFIN_APP_ID,
    PLEX_APP_ID,
    SEERR_APP_ID,
    get_app,
    media_server_of,
)
from marrquee.jellyfin import JELLYFIN_PORT
from marrquee.login import load_login
from marrquee.plex import load_plex_account

logger = logging.getLogger(__name__)

SEERR_API: Final = "api/v1"

# constants/server.ts's MediaServerType enum - only the values this module
# reads or writes.
SEERR_PLEX: Final = 1
SEERR_JELLYFIN: Final = 2
SEERR_NOT_CONFIGURED: Final = 4

# lib/permissions.ts's REQUEST bit - shared people's requests wait for the
# owner's approval; the owner's own requests auto-approve as admin.
SEERR_REQUEST_PERMISSION: Final = 32


def seerr_base_url() -> str:
    """Where Marrquee reaches Seerr - its own container, on the `marrquee`
    network Marrquee itself joins, the same address every other managed
    app answers at.
    """
    return f"http://seerr:{get_app(SEERR_APP_ID).port}"


# --- the wire shape: request, response, real door, fake door -----------------


@dataclass(frozen=True)
class SeerrResponse:
    """What one call to Seerr's API came back with.

    `detail` is raw technical text for diagnostics only - a connect error's
    own text, Seerr's own `message` field, or a failing body cut to 300
    characters - never the full `payload`, which may carry Seerr's key or
    an echoed secret.
    """

    ok: bool
    status: int
    payload: object
    detail: str | None


class SeerrClient(Protocol):
    """Talks to Seerr's API. Never raises - see `HttpSeerrClient.request`."""

    async def request(
        self,
        method: Literal["GET", "POST", "PUT"],
        base_url: str,
        path: str,
        *,
        api_key: str | None,
        params: Sequence[tuple[str, str]] = (),
        json_body: object | None = None,
    ) -> SeerrResponse: ...


def _join(base_url: str, path: str) -> str:
    """Join with exactly one slash, whatever slashes either side carries."""
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _parse_response(response: httpx.Response) -> SeerrResponse:
    ok = 200 <= response.status_code < 300
    payload: object = None
    if response.content:
        try:
            payload = response.json()
        except ValueError:
            payload = None
    if ok:
        return SeerrResponse(ok=True, status=response.status_code, payload=payload, detail=None)

    detail: str | None = None
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, str):
            detail = message
    if detail is None and response.text:
        detail = response.text[:300]
    return SeerrResponse(ok=False, status=response.status_code, payload=payload, detail=detail)


class HttpSeerrClient:
    """The real `SeerrClient`, over `httpx`.

    A fresh `AsyncClient` per call - a bring-up or a wiring run makes a
    handful of these in total, so there is no connection pool worth
    keeping warm across calls.
    """

    def __init__(
        self, *, timeout: float = 30.0, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        # 30s, not a short probe timeout: Seerr's own sign-in and settings
        # test calls reach out to Plex, Jellyfin, Sonarr or Radarr before
        # answering us.
        self._timeout = timeout
        self._transport = transport

    async def request(
        self,
        method: Literal["GET", "POST", "PUT"],
        base_url: str,
        path: str,
        *,
        api_key: str | None,
        params: Sequence[tuple[str, str]] = (),
        json_body: object | None = None,
    ) -> SeerrResponse:
        url = _join(base_url, path)
        headers = {"Accept": "application/json"}
        if api_key is not None:
            headers["X-Api-Key"] = api_key
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout
            ) as client:
                response = await client.request(
                    method, url, headers=headers, params=tuple(params), json=json_body
                )
        except httpx.HTTPError as error:
            # Covers connect errors and timeouts alike - a dead Seerr is a
            # real answer, never a crash, the same as `HttpArrClient`.
            return SeerrResponse(
                ok=False, status=0, payload=None, detail=f"{type(error).__name__}: {error}"
            )
        return _parse_response(response)


class FakeSeerrClient:
    """A scriptable `SeerrClient` for tests - no network involved.

    `script` maps `(method, path)` to a queue of `SeerrResponse`s; each
    queue drains in order and repeats its last entry forever, the way a
    server that has settled into one state keeps answering the same way -
    the same contract `FakeJellyfinServer` makes. A call whose
    `(method, path)` was never scripted raises `KeyError` naming exactly
    what was unscripted, rather than guessing an answer. `calls` records
    method, path, params and whether a key was sent - never the key or a
    body - and `bodies` records every `json_body` in call order, for a
    test that wants to look inside one without touching `calls`.
    """

    def __init__(self, script: Mapping[tuple[str, str], Sequence[SeerrResponse]]) -> None:
        self._queues: dict[tuple[str, str], list[SeerrResponse]] = {
            key: list(responses) for key, responses in script.items()
        }
        self.calls: list[tuple[str, str, tuple[tuple[str, str], ...], bool]] = []
        self.bodies: list[object] = []

    async def request(
        self,
        method: Literal["GET", "POST", "PUT"],
        base_url: str,
        path: str,
        *,
        api_key: str | None,
        params: Sequence[tuple[str, str]] = (),
        json_body: object | None = None,
    ) -> SeerrResponse:
        self.calls.append((method, path, tuple(params), api_key is not None))
        self.bodies.append(json_body)
        key = (method, path)
        queue = self._queues.get(key)
        if not queue:
            raise KeyError(f"no scripted response left for {key!r}")
        if len(queue) > 1:
            return queue.pop(0)
        return queue[0]


# --- sign-in resolution: which door, and what to carry through it ------------


@dataclass(frozen=True)
class SeerrPlexSignIn:
    """Signs in to Seerr as the owner's Plex account.

    `token` is excluded from `repr` - a logged object or an unhandled
    traceback must not leak it, the same standard `PlexAccount` sets.
    """

    token: str = field(repr=False)


@dataclass(frozen=True)
class SeerrJellyfinSignIn:
    """Signs in to Seerr with the one login, against a Jellyfin at
    `hostname`:`port`.

    `hostname` has no default: it can only ever be Seerr's own view of
    Jellyfin's address (its Docker gateway), which does not exist before
    Seerr's container does. `password` is excluded from `repr`, the same
    standard `SavedLogin` sets.
    """

    username: str
    password: str = field(repr=False)
    hostname: str
    port: int = JELLYFIN_PORT


SeerrSignIn = SeerrPlexSignIn | SeerrJellyfinSignIn


def seerr_sign_in_kind(
    app_ids: Iterable[str], config_dir: Path
) -> Literal["plex", "jellyfin"] | None:
    """Which door Seerr's first-run sign-in should use, or None when
    nothing usable is on disk yet. Never raises, and touches no network -
    the Jellyfin door needs a gateway address that only exists once Seerr's
    own container is up, so building the actual sign-in is `seerr_sign_in`,
    a separate step.
    """
    media_app = media_server_of(app_ids)
    if media_app is None:
        return None

    if media_app.id in (PLEX_APP_ID, EXISTING_PLEX_APP_ID):
        account = load_plex_account(config_dir)
        if account is None or account.token is None:
            return None
        return "plex"

    if media_app.id == JELLYFIN_APP_ID:
        if load_login(config_dir).login is None:
            return None
        return "jellyfin"

    return None


def seerr_sign_in(
    kind: Literal["plex", "jellyfin"], config_dir: Path, *, jellyfin_host: str | None
) -> SeerrSignIn | None:
    """Build the sign-in `seerr_sign_in_kind` said was possible. Never
    raises: anything that has since gone missing (a cleared plex.json, a
    Jellyfin address not resolved yet) is `None`, the same "nothing usable
    yet" contract every reader in this module makes.
    """
    if kind == "plex":
        account = load_plex_account(config_dir)
        if account is None or account.token is None:
            return None
        return SeerrPlexSignIn(token=account.token)

    if jellyfin_host is None:
        return None
    login = load_login(config_dir).login
    if login is None:
        return None
    return SeerrJellyfinSignIn(
        username=login.username,
        password=login.password,
        hostname=jellyfin_host,
        port=JELLYFIN_PORT,
    )


# --- first-run setup: the admin, the sign-in rules, and initialized=true ------


@dataclass(frozen=True)
class SeerrSetup:
    """What one attempt at Seerr's first-run setup came back with."""

    state: Literal["done", "waiting", "not_ours", "refused"]
    technical: str | None


async def ensure_seerr_setup(
    client: SeerrClient, base_url: str, api_key: str, sign_in: SeerrSignIn
) -> SeerrSetup:
    """Finish Seerr's first-run setup with `sign_in`'s account as the
    admin, or confirm it already is one. Never raises - any surprise shape
    becomes `refused` rather than a crash.
    """
    try:
        return await _ensure_seerr_setup(client, base_url, api_key, sign_in)
    except Exception as error:
        logger.exception("seerr setup raised")
        return SeerrSetup(state="refused", technical=f"seerr: {type(error).__name__}")


async def _ensure_seerr_setup(
    client: SeerrClient, base_url: str, api_key: str, sign_in: SeerrSignIn
) -> SeerrSetup:
    expected_type = SEERR_PLEX if isinstance(sign_in, SeerrPlexSignIn) else SEERR_JELLYFIN

    public_path = _path("settings/public")
    public = await client.request("GET", base_url, public_path, api_key=None)
    payload = public.payload if isinstance(public.payload, dict) else None
    media_type = payload.get("mediaServerType") if payload is not None else None
    initialized = payload.get("initialized") if payload is not None else None
    if not public.ok or not _is_plain_int(media_type) or not isinstance(initialized, bool):
        return SeerrSetup(state="waiting", technical=_technical("GET", public_path, public))

    if media_type == SEERR_NOT_CONFIGURED:
        signed_in = await _sign_in(client, base_url, sign_in)
        if signed_in is not None:
            return signed_in
    elif media_type != expected_type:
        return SeerrSetup(state="not_ours", technical=None)

    main_path = _path("settings/main")
    main = await client.request("GET", base_url, main_path, api_key=api_key)
    if not main.ok:
        return SeerrSetup(
            state=_status_state(main.status), technical=_technical("GET", main_path, main)
        )

    if initialized:
        return SeerrSetup(state="done", technical=None)

    write = await client.request(
        "POST",
        base_url,
        main_path,
        api_key=api_key,
        json_body={
            "localLogin": False,
            "newPlexLogin": True,
            "defaultPermissions": SEERR_REQUEST_PERMISSION,
        },
    )
    if not write.ok:
        return SeerrSetup(
            state=_status_state(write.status), technical=_technical("POST", main_path, write)
        )

    init_path = _path("settings/initialize")
    init = await client.request("POST", base_url, init_path, api_key=api_key)
    if not init.ok:
        return SeerrSetup(
            state=_status_state(init.status), technical=_technical("POST", init_path, init)
        )

    init_payload = init.payload if isinstance(init.payload, dict) else {}
    if init_payload.get("initialized") is not True:
        return SeerrSetup(state="refused", technical=_technical("POST", init_path, init))

    return SeerrSetup(state="done", technical=None)


async def _sign_in(client: SeerrClient, base_url: str, sign_in: SeerrSignIn) -> SeerrSetup | None:
    """POST the right auth call for a fresh (type 4) Seerr. Returns None to
    mean "carry on" - the admin now exists. Only a non-2xx answer ends the
    run here: a 401/403 means the account isn't Jellyfin's admin, or the
    plex.tv token is stale, and that is `refused`, not `not_ours` - nothing
    is left over from someone else, this Seerr is just not signed in yet.
    """
    if isinstance(sign_in, SeerrPlexSignIn):
        path = _path("auth/plex")
        body: dict[str, object] = {"authToken": sign_in.token}
    else:
        path = _path("auth/jellyfin")
        body = {
            "username": sign_in.username,
            "password": sign_in.password,
            "hostname": sign_in.hostname,
            "port": sign_in.port,
            "useSsl": False,
            "urlBase": "",
            "serverType": SEERR_JELLYFIN,
        }
    auth = await client.request("POST", base_url, path, api_key=None, json_body=body)
    if auth.ok:
        return None
    return SeerrSetup(state=_status_state(auth.status), technical=_technical("POST", path, auth))


def _status_state(status: int) -> Literal["waiting", "refused"]:
    # 0 (no reply) or a 5xx is worth trying again next tick; any other
    # non-2xx (including a 401/403) is a considered refusal, not a hiccup.
    return "waiting" if status == 0 or status >= 500 else "refused"


def _technical(method: str, path: str, response: SeerrResponse) -> str:
    """Built only from the method, path, status and `detail` - never
    `payload` or a request body - so a settings payload carrying Seerr's
    key, or an auth body carrying the Plex token or the one login's
    password, can never reach diagnostics through this string.
    """
    parts = [f"seerr: {method} {path} -> HTTP {response.status}"]
    if response.detail:
        parts.append(response.detail)
    return " ".join(parts)


def _path(suffix: str) -> str:
    return f"{SEERR_API}/{suffix}"


def _is_plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)

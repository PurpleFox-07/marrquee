"""The Jellyfin door: `jellyfin.json`, the `MediaBrowser` header, first-time
setup, and the library parser.

This is a leaf module - it imports nothing from `deploy`, `wiring`,
`words`, `plex` or `docker_client`, so both the wiring engine and the
deploy engine can import it without a cycle. `HttpJellyfinServer` follows
`wiring/arr_client.py`'s own shape: a real `httpx` door and an exported
in-memory fake, with every transport exception, timeout and bad body
funnelled into the same never-raising result - a dead Jellyfin is a real
answer, never a crash.

Jellyfin's admin API key and the session token `AuthenticateByName` hands
back are the two secrets this module ever touches. Both travel only in the
`Authorization` header, never as a URL query parameter, and neither is
ever folded into a `technical` string - see `_technical`'s own docstring.
The admin password travels only in a JSON body, the same way.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, Protocol, cast

import httpx

from marrquee import __version__
from marrquee.login import SavedLogin
from marrquee.state import write_json_atomic

logger = logging.getLogger(__name__)

JELLYFIN_KEY_APP_NAME: Final = "Marrquee"
JELLYFIN_PORT: Final = 8096

_JELLYFIN_JSON_FILE_NAME = "jellyfin.json"
_JELLYFIN_JSON_VERSION = 1

# Fixed so every call looks like it came from the same client - Jellyfin's
# own Devices list would otherwise grow one row per request instead of one
# row for the whole app.
_JELLYFIN_CLIENT = "Marrquee"
_JELLYFIN_DEVICE = "Marrquee"
_JELLYFIN_DEVICE_ID = "marrquee"


# --- jellyfin.json: the admin id and the API key Marrquee minted ------------


@dataclass(frozen=True)
class JellyfinRecord:
    """What `jellyfin.json` currently holds.

    `api_key` is excluded from `repr` - a logged object or an unhandled
    traceback must not leak it the way an ordinary dataclass field would.
    """

    admin_id: str
    api_key: str = field(repr=False)


def load_jellyfin(config_dir: Path) -> JellyfinRecord | None:
    """Read `<config_dir>/jellyfin.json`, or None for any reason at all.

    A missing file, an empty file, text that isn't JSON, JSON with the
    wrong shape, a `version` this build doesn't recognise, or even a
    directory sitting where the file should be, are all the same answer:
    nothing usable is here yet, not an exception - the same contract
    `state.load_state` and `plex.load_plex_account` make.
    """
    try:
        raw = (config_dir / _JELLYFIN_JSON_FILE_NAME).read_text()
    except OSError:
        return None

    if not raw.strip():
        return None

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None

    if not isinstance(payload, dict) or not _is_current_version(payload.get("version")):
        return None

    try:
        return _record_from_payload(payload)
    except (KeyError, TypeError, ValueError):
        return None


def save_jellyfin(config_dir: Path, api_key: str, admin_id: str) -> None:
    """Persist the admin id and the API key Marrquee minted, root-only and
    never chowned - only Marrquee itself ever reads this file.
    """
    write_json_atomic(
        config_dir / _JELLYFIN_JSON_FILE_NAME,
        {"version": _JELLYFIN_JSON_VERSION, "admin_id": admin_id, "api_key": api_key},
    )


def _is_current_version(value: object) -> bool:
    # bool is an int subclass in Python; excluded so a stray `true` in the
    # file can't silently be read as version 1.
    return (
        isinstance(value, int) and not isinstance(value, bool) and value == _JELLYFIN_JSON_VERSION
    )


def _record_from_payload(payload: Mapping[str, object]) -> JellyfinRecord:
    return JellyfinRecord(
        admin_id=_require_str(payload.get("admin_id")),
        api_key=_require_str(payload.get("api_key")),
    )


def _require_str(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError(f"expected a non-empty string, got {value!r}")
    return value


# --- a local Jellyfin server: requests ---------------------------------------


@dataclass(frozen=True)
class JellyfinResponse:
    """What one call to a local Jellyfin server came back with.

    `detail` is raw technical text for a dead transport only (a connect
    error or a timeout) - never a Jellyfin response body, since a failing
    body can echo back a token. Diagnostics that want more than the status
    code read `payload`, at their own risk, never `technical` strings built
    in this module.
    """

    ok: bool
    status: int
    payload: object
    detail: str | None


class JellyfinServer(Protocol):
    """Talks to one local Jellyfin server. Never raises - see
    `HttpJellyfinServer.request`.
    """

    async def request(
        self,
        method: Literal["GET", "POST"],
        base_url: str,
        path: str,
        *,
        token: str | None,
        params: Sequence[tuple[str, str]] = (),
        json_body: object | None = None,
    ) -> JellyfinResponse: ...


def _join(base_url: str, path: str) -> str:
    """Join with exactly one slash, whatever slashes either side carries."""
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _jellyfin_auth_header(token: str | None) -> str:
    """The one header Jellyfin 12.x accepts by default - the legacy
    `X-Emby-Token`/`?api_key=` forms are switched off, so this is the only
    door in.
    """
    parts = [
        f'Client="{urllib.parse.quote(_JELLYFIN_CLIENT, safe="")}"',
        f'Device="{urllib.parse.quote(_JELLYFIN_DEVICE, safe="")}"',
        f'DeviceId="{urllib.parse.quote(_JELLYFIN_DEVICE_ID, safe="")}"',
        f'Version="{urllib.parse.quote(__version__, safe="")}"',
    ]
    if token is not None:
        parts.append(f'Token="{urllib.parse.quote(token, safe="")}"')
    return "MediaBrowser " + ", ".join(parts)


def _parse_response(response: httpx.Response) -> JellyfinResponse:
    ok = 200 <= response.status_code < 300
    payload: object = None
    if response.content:
        try:
            payload = response.json()
        except ValueError:
            payload = response.text
    return JellyfinResponse(ok=ok, status=response.status_code, payload=payload, detail=None)


class HttpJellyfinServer:
    """The real `JellyfinServer`, over `httpx`.

    A fresh `AsyncClient` per call - setup and a wiring run each make a
    handful of these in total, so there is no connection pool worth
    keeping warm across calls.
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
        *,
        token: str | None,
        params: Sequence[tuple[str, str]] = (),
        json_body: object | None = None,
    ) -> JellyfinResponse:
        url = _join(base_url, path)
        headers = {"Accept": "application/json", "Authorization": _jellyfin_auth_header(token)}
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout
            ) as client:
                response = await client.request(
                    method, url, headers=headers, params=tuple(params), json=json_body
                )
        except httpx.HTTPError as error:
            # Covers connect errors and timeouts alike - a dead Jellyfin is
            # a real answer, never a crash, the same as `HttpArrClient`.
            return JellyfinResponse(
                ok=False, status=0, payload=None, detail=f"{type(error).__name__}: {error}"
            )
        return _parse_response(response)


class FakeJellyfinServer:
    """A scriptable `JellyfinServer` for tests - no network involved.

    `script` maps `(method, path)` to a queue of `JellyfinResponse`s; each
    queue drains in order and repeats its last entry forever, the way a
    server that has settled into one state keeps answering the same way. A
    call whose `(method, path)` was never scripted raises `KeyError` naming
    exactly what was unscripted, rather than guessing an answer. `calls`
    records method, path, params and whether a token was sent - never the
    token or a body - and `bodies` records every `json_body` in call order,
    for a test that wants to look inside one without touching `calls`.
    """

    def __init__(self, script: Mapping[tuple[str, str], Sequence[JellyfinResponse]]) -> None:
        self._queues: dict[tuple[str, str], list[JellyfinResponse]] = {
            key: list(responses) for key, responses in script.items()
        }
        self.calls: list[tuple[str, str, tuple[tuple[str, str], ...], bool]] = []
        self.bodies: list[object] = []

    async def request(
        self,
        method: Literal["GET", "POST"],
        base_url: str,
        path: str,
        *,
        token: str | None,
        params: Sequence[tuple[str, str]] = (),
        json_body: object | None = None,
    ) -> JellyfinResponse:
        self.calls.append((method, path, tuple(params), token is not None))
        self.bodies.append(json_body)
        key = (method, path)
        queue = self._queues.get(key)
        if not queue:
            raise KeyError(f"no scripted response left for {key!r}")
        if len(queue) > 1:
            return queue.pop(0)
        return queue[0]


# --- reaching a Jellyfin bound to the host's own network --------------------


def jellyfin_base_url(address: str) -> str:
    """The local Jellyfin server's base URL for one reachable address.

    Jellyfin runs with host networking, so this takes the same host
    address Plex does, never a Docker-network name like
    `http://jellyfin:8096` - a Jellyfin-only install may never create that
    network at all.
    """
    host = f"[{address}]" if ":" in address else address
    return f"http://{host}:{JELLYFIN_PORT}"


# --- first-time setup: the admin becomes the one login -----------------------


@dataclass(frozen=True)
class JellyfinSetup:
    """What one attempt to make Jellyfin's admin the one login came back
    with.
    """

    state: Literal["done", "waiting", "not_ours", "refused"]
    technical: str | None


async def ensure_jellyfin_admin(
    server: JellyfinServer,
    base_url: str,
    login: SavedLogin,
    config_dir: Path,
    *,
    server_name: str,
) -> JellyfinSetup:
    """Finish Jellyfin's first-time setup with `login` as the admin, or
    confirm it already is one. Never raises - any surprise shape becomes
    `refused` rather than a crash.
    """
    try:
        return await _ensure_jellyfin_admin(server, base_url, login, config_dir, server_name)
    except Exception as error:
        logger.exception("jellyfin setup raised")
        return JellyfinSetup(state="refused", technical=f"jellyfin: {type(error).__name__}")


async def _ensure_jellyfin_admin(
    server: JellyfinServer,
    base_url: str,
    login: SavedLogin,
    config_dir: Path,
    server_name: str,
) -> JellyfinSetup:
    public_info = await server.request("GET", base_url, "/System/Info/Public", token=None)
    if not public_info.ok:
        # Any failure here, hard 4xx included, just means "still booting" -
        # there is nothing yet to tell "ours" from "someone else's".
        return JellyfinSetup(
            state="waiting", technical=_technical("GET", "/System/Info/Public", public_info)
        )

    record = load_jellyfin(config_dir)
    if record is not None:
        check = await server.request("GET", base_url, "/System/Info", token=record.api_key)
        if check.ok:
            return JellyfinSetup(state="done", technical=None)
        # A stale key falls through to signing in fresh below, rather than
        # failing outright - the admin is still ours, only the key is dead.

    public_payload = public_info.payload if isinstance(public_info.payload, dict) else {}
    if public_payload.get("StartupWizardCompleted") is not True:
        outcome = await _run_startup(server, base_url, login)
        if outcome is not None:
            return outcome

    auth = await server.request(
        "POST",
        base_url,
        "/Users/AuthenticateByName",
        token=None,
        json_body={"Username": login.username, "Pw": login.password},
    )
    if auth.status in (401, 403):
        return JellyfinSetup(state="not_ours", technical=None)
    if not auth.ok:
        return _fallback("POST", "/Users/AuthenticateByName", auth)

    auth_payload = auth.payload if isinstance(auth.payload, dict) else {}
    user = auth_payload.get("User")
    session_token = auth_payload.get("AccessToken")
    admin_id = user.get("Id") if isinstance(user, dict) else None
    if (
        not isinstance(session_token, str)
        or not session_token
        or not isinstance(admin_id, str)
        or not admin_id
    ):
        return JellyfinSetup(state="refused", technical="jellyfin: bad sign-in shape")

    listing = await server.request("GET", base_url, "/Auth/Keys", token=session_token)
    if not listing.ok:
        return _fallback("GET", "/Auth/Keys", listing)
    api_key = _newest_marrquee_key(listing.payload)

    if api_key is None:
        created = await server.request(
            "POST",
            base_url,
            "/Auth/Keys",
            token=session_token,
            params=[("app", JELLYFIN_KEY_APP_NAME)],
        )
        if not created.ok:
            return _fallback("POST", "/Auth/Keys", created)

        refreshed = await server.request("GET", base_url, "/Auth/Keys", token=session_token)
        if not refreshed.ok:
            return _fallback("GET", "/Auth/Keys", refreshed)
        api_key = _newest_marrquee_key(refreshed.payload)
        if api_key is None:
            return JellyfinSetup(state="refused", technical="jellyfin: no key after create")

    try:
        save_jellyfin(config_dir, api_key, admin_id)
    except OSError as error:
        return JellyfinSetup(state="refused", technical=f"jellyfin: {type(error).__name__}")

    await _finish_setup(server, base_url, api_key, session_token, server_name)
    return JellyfinSetup(state="done", technical=None)


async def _run_startup(
    server: JellyfinServer, base_url: str, login: SavedLogin
) -> JellyfinSetup | None:
    """Jellyfin's obsolete-but-load-bearing startup dance. Returns None to
    mean "carry on to sign-in": either the admin now exists, or a 401/403
    partway through means someone already finished this.
    """
    get_user = await server.request("GET", base_url, "/Startup/User", token=None)
    if get_user.status in (401, 403):
        return None
    if not get_user.ok:
        return _fallback("GET", "/Startup/User", get_user)

    create_user = await server.request(
        "POST",
        base_url,
        "/Startup/User",
        token=None,
        json_body={"Name": login.username, "Password": login.password},
    )
    if create_user.status in (401, 403):
        return None
    if not create_user.ok:
        return _fallback("POST", "/Startup/User", create_user)

    complete = await server.request("POST", base_url, "/Startup/Complete", token=None)
    if not complete.ok:
        return _fallback("POST", "/Startup/Complete", complete)
    return None


async def _finish_setup(
    server: JellyfinServer, base_url: str, api_key: str, session_token: str, server_name: str
) -> None:
    """Best effort: Jellyfin already has a working admin key by this
    point, so a failure here never changes the outcome.
    """
    config = await server.request("GET", base_url, "/System/Configuration", token=api_key)
    if config.ok and isinstance(config.payload, dict) and config.payload.get("ServerName") == "":
        updated = dict(config.payload)
        updated["ServerName"] = server_name
        await server.request(
            "POST", base_url, "/System/Configuration", token=api_key, json_body=updated
        )
    await server.request("POST", base_url, "/Sessions/Logout", token=session_token)


def _fallback(method: str, path: str, response: JellyfinResponse) -> JellyfinSetup:
    """The one rule for every step past the first: a dead or overloaded
    server is worth trying again next tick; any other refusal is real.
    """
    state: Literal["waiting", "refused"] = (
        "waiting" if response.status == 0 or response.status >= 500 else "refused"
    )
    return JellyfinSetup(state=state, technical=_technical(method, path, response))


def _technical(method: str, path: str, response: JellyfinResponse) -> str:
    """Built only from the method, path, status and `detail` - never
    `payload` - so a body that happens to echo a token or a password can
    never reach diagnostics through this string.
    """
    parts = [f"jellyfin: {method} {path} -> HTTP {response.status}"]
    if response.detail:
        parts.append(response.detail)
    return " ".join(parts)


def _is_plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _newest_marrquee_key(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    items = payload.get("Items")
    if not isinstance(items, list):
        return None

    best_id: int | None = None
    best_key: str | None = None
    for item in items:
        if not isinstance(item, dict) or item.get("AppName") != JELLYFIN_KEY_APP_NAME:
            continue
        item_id = item.get("Id")
        token = item.get("AccessToken")
        if not _is_plain_int(item_id) or not isinstance(token, str) or not token:
            continue
        if best_id is None or cast(int, item_id) > best_id:
            best_id = cast(int, item_id)
            best_key = token
    return best_key


# --- libraries: what a wiring run needs read back ----------------------------


@dataclass(frozen=True)
class JellyfinLibrary:
    """One entry read back from `GET /Library/VirtualFolders`."""

    name: str
    collection_type: str | None
    locations: tuple[str, ...]
    item_id: str


def parse_virtual_folders(payload: object) -> tuple[JellyfinLibrary, ...]:
    """Never raises: a non-list, or an item missing `Name` or `ItemId`, is
    simply skipped rather than crashing a wiring run.
    """
    if not isinstance(payload, list):
        return ()

    libraries: list[JellyfinLibrary] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = item.get("Name")
        item_id = item.get("ItemId")
        if not isinstance(name, str) or not name or not isinstance(item_id, str) or not item_id:
            continue
        collection_type = item.get("CollectionType")
        collection_type = collection_type.lower() if isinstance(collection_type, str) else None
        locations = item.get("Locations")
        location_tuple = (
            tuple(loc for loc in locations if isinstance(loc, str))
            if isinstance(locations, list)
            else ()
        )
        libraries.append(
            JellyfinLibrary(
                name=name,
                collection_type=collection_type,
                locations=location_tuple,
                item_id=item_id,
            )
        )
    return tuple(libraries)

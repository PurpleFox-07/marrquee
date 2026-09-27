"""The Plex doors: remembering a sign-in, talking to plex.tv, writing the
one-time claim secret, and talking to a local Plex server.

This is a leaf module - it imports nothing from `deploy`, `wiring` or
`words`, so both the wiring engine and the deploy engine can import it
without a cycle. Every door here follows `wiring/arr_client.py` and
`wiring/qbit_client.py`'s own shape: a real `httpx` door and an exported
in-memory fake, with every transport exception, timeout and bad body
funnelled into the same never-raising result - a dead plex.tv or a dead
local server is a real answer, never a crash.

The owner's Plex account token and the server's one-time claim code are
the two secrets this module ever touches. Neither is ever sent as a URL
query parameter (httpx logs full URLs), neither is ever logged, and
`PlexAccount.token` never appears in a `repr()`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import secrets
import stat
import urllib.parse
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Final, Literal, Protocol, cast
from xml.etree import ElementTree

import httpx

from marrquee import __version__, storage
from marrquee.addresses import app_url
from marrquee.config import Settings
from marrquee.docker_client import DockerEngine
from marrquee.links import LinkCard
from marrquee.state import write_json_atomic
from marrquee.storage import PathEscapesRoot, container_media_path, host_media_path, to_host_view

logger = logging.getLogger(__name__)

PLEX_PRODUCT: Final = "Marrquee"
PLEX_TV_URL: Final = "https://plex.tv"
PLEX_AUTH_URL: Final = "https://app.plex.tv/auth#?"
PLEX_CLAIM_FILE_NAME: Final = "plex_claim"
PROBE_FOLDER_PREFIX: Final = "marrquee-plex-check-"

_PLEX_JSON_FILE_NAME = "plex.json"
_PLEX_JSON_VERSION = 1
_PLEX_PORT = 32400

_EXISTING_PLEX_JSON_FILE_NAME = "existing_plex.json"
_EXISTING_PLEX_JSON_VERSION = 1

_CLAIM_TOKEN_PATTERN = re.compile(r"^claim-[A-Za-z0-9_-]+$")


# --- plex.json: the owner's Plex account ---------------------------------------


@dataclass(frozen=True)
class PlexAccount:
    """What Marrquee remembers about the owner's Plex sign-in.

    `token` never appears in `repr()` - a logged object or an unhandled
    traceback must not leak it the way an ordinary dataclass field would.
    """

    client_id: str
    username: str | None
    token: str | None = field(default=None, repr=False)


def load_plex_account(config_dir: Path) -> PlexAccount | None:
    """Read `<config_dir>/plex.json`, or None for any reason at all.

    A missing file, an empty file, text that isn't JSON, JSON with the
    wrong shape, or a `version` this build doesn't recognise are all the
    same answer: nothing usable is here yet, not an exception - the same
    contract `state.load_state` makes.
    """
    try:
        raw = (config_dir / _PLEX_JSON_FILE_NAME).read_text()
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
        return _account_from_payload(payload)
    except (KeyError, TypeError, ValueError):
        return None


def plex_client_id(config_dir: Path) -> str:
    """The saved `X-Plex-Client-Identifier`, minting and saving a fresh one
    if none is on disk yet (or the file is unreadable). OSError propagates -
    a client id that fails to save is a real failure, not one to swallow.
    """
    account = load_plex_account(config_dir)
    if account is not None:
        return account.client_id

    client_id = uuid.uuid4().hex
    _write_plex_json(config_dir, PlexAccount(client_id=client_id, username=None, token=None))
    return client_id


def save_plex_sign_in(config_dir: Path, token: str, username: str) -> None:
    """Persist a completed sign-in, keeping whatever client id is already saved."""
    client_id = plex_client_id(config_dir)
    _write_plex_json(config_dir, PlexAccount(client_id=client_id, username=username, token=token))


def _write_plex_json(config_dir: Path, account: PlexAccount) -> None:
    write_json_atomic(
        config_dir / _PLEX_JSON_FILE_NAME,
        {
            "version": _PLEX_JSON_VERSION,
            "client_id": account.client_id,
            "username": account.username,
            "token": account.token,
        },
    )


def _is_current_version(value: object) -> bool:
    # bool is an int subclass in Python; excluded so a stray `true` in the
    # file can't silently be read as version 1.
    return isinstance(value, int) and not isinstance(value, bool) and value == _PLEX_JSON_VERSION


def _account_from_payload(payload: Mapping[str, object]) -> PlexAccount:
    return PlexAccount(
        client_id=_require_str(payload.get("client_id")),
        username=_require_optional_str(payload.get("username")),
        token=_require_optional_str(payload.get("token")),
    )


def _require_str(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"expected a string, got {value!r}")
    return value


def _require_optional_str(value: object) -> str | None:
    if value is None:
        return None
    return _require_str(value)


# --- plex.tv: the PIN sign-in round trip ----------------------------------------


@dataclass(frozen=True)
class PlexPin:
    """One PIN plex.tv issued - the code the owner approves on plex.tv, and
    the id Marrquee polls back with.
    """

    id: int
    code: str


class PlexTv(Protocol):
    """Talks to plex.tv. Never raises - see `HttpPlexTv._request`/`_send`."""

    async def create_pin(self, client_id: str) -> PlexPin | None: ...
    async def pin_token(self, client_id: str, pin: PlexPin) -> str | None: ...
    async def account_name(self, client_id: str, token: str) -> str | None: ...
    async def claim_token(self, client_id: str, token: str) -> str | None: ...
    async def servers(self, client_id: str, token: str) -> PlexServers: ...


def _plex_headers(client_id: str, token: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "X-Plex-Product": PLEX_PRODUCT,
        "X-Plex-Version": __version__,
        "X-Plex-Client-Identifier": client_id,
    }
    if token is not None:
        headers["X-Plex-Token"] = token
    return headers


def _json_object(response: httpx.Response) -> dict[str, object] | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _is_plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_bool(value: object) -> bool | None:
    """Read a boolean out of anything plex.tv or a local Plex might send one
    as - a real `bool`, `0`/`1`, or `"0"`/`"1"`/`"true"`/`"false"` - or
    `None` when the shape is something else entirely.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value) if value in (0, 1) else None
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true"):
            return True
        if lowered in ("0", "false"):
            return False
    return None


def _json_list(response: httpx.Response) -> list[object] | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, list) else None


# --- plex.tv: the owner's own Plex servers ---------------------------------------


@dataclass(frozen=True)
class PlexConnection:
    """One way plex.tv says a server can be reached."""

    protocol: str
    address: str
    port: int
    uri: str
    local: bool
    ipv6: bool


@dataclass(frozen=True)
class PlexServerChoice:
    """One of the owner's own Plex servers, as plex.tv's resources API
    describes it - enough to list it on the servers pane, and enough to try
    reaching it.

    `token` never appears in `repr()`, the same guarantee `PlexAccount`
    makes for the owner's account token.
    """

    machine_id: str
    name: str
    online: bool
    https_required: bool
    connections: tuple[PlexConnection, ...]
    token: str = field(default="", repr=False)


@dataclass(frozen=True)
class PlexServers:
    """The owner's own Plex servers, or the reason there are none to show."""

    state: Literal["ok", "signed_out", "unreachable"]
    servers: tuple[PlexServerChoice, ...] = ()


def _provides_server(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return "server" in {part.strip() for part in value.split(",")}


def _parse_connections(raw: object) -> tuple[PlexConnection, ...]:
    if not isinstance(raw, list):
        return ()
    connections: list[PlexConnection] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        protocol = entry.get("protocol")
        address = entry.get("address")
        port = entry.get("port")
        uri = entry.get("uri")
        if not isinstance(protocol, str) or not isinstance(address, str):
            continue
        if not isinstance(uri, str) or not _is_plain_int(port):
            continue
        # Relay is plex.tv's bandwidth-capped last resort, and a Plex only
        # reachable that way can't see the NAS's own folders anyway - it
        # never becomes a candidate address at all.
        if _parse_bool(entry.get("relay")):
            continue
        connections.append(
            PlexConnection(
                protocol=protocol,
                address=address,
                port=cast(int, port),
                uri=uri,
                local=bool(_parse_bool(entry.get("local"))),
                ipv6=bool(_parse_bool(entry.get("IPv6"))),
            )
        )
    return tuple(connections)


def _parse_server_choices(payload: list[object]) -> tuple[PlexServerChoice, ...]:
    choices: list[PlexServerChoice] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        machine_id = entry.get("clientIdentifier")
        name = entry.get("name")
        if not isinstance(machine_id, str) or not machine_id:
            continue
        if not isinstance(name, str) or not name:
            continue
        if not _provides_server(entry.get("provides")):
            continue
        if not _parse_bool(entry.get("owned")):
            continue
        token = entry.get("accessToken")
        choices.append(
            PlexServerChoice(
                machine_id=machine_id,
                name=name,
                online=bool(_parse_bool(entry.get("presence"))),
                https_required=bool(_parse_bool(entry.get("httpsRequired"))),
                connections=_parse_connections(entry.get("connections")),
                token=token if isinstance(token, str) else "",
            )
        )
    choices.sort(key=lambda choice: choice.name.casefold())
    return tuple(choices)


class HttpPlexTv:
    """The real `PlexTv`, over `httpx`.

    A fresh `AsyncClient` per call - a sign-in round trip makes a handful
    of these in total, so there is no connection pool worth keeping warm,
    the same choice `HttpArrClient` makes.
    """

    def __init__(
        self, *, timeout: float = 10.0, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._timeout = timeout
        self._transport = transport

    async def _request(
        self,
        method: Literal["GET", "POST"],
        path: str,
        client_id: str,
        *,
        token: str | None = None,
        params: Sequence[tuple[str, str]] = (),
    ) -> httpx.Response | None:
        url = f"{PLEX_TV_URL}{path}"
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout
            ) as client:
                response = await client.request(
                    method, url, headers=_plex_headers(client_id, token), params=tuple(params)
                )
        except httpx.HTTPError:
            # Covers connect errors and timeouts alike - a dead plex.tv is a
            # real answer, never a crash, the same as `HttpArrClient`.
            return None
        if not (200 <= response.status_code < 300):
            return None
        return response

    async def create_pin(self, client_id: str) -> PlexPin | None:
        response = await self._request(
            "POST", "/api/v2/pins", client_id, params=[("strong", "true")]
        )
        if response is None:
            return None
        payload = _json_object(response)
        if payload is None:
            return None
        pin_id = payload.get("id")
        code = payload.get("code")
        if not _is_plain_int(pin_id) or not isinstance(code, str) or not code:
            return None
        return PlexPin(id=cast(int, pin_id), code=code)

    async def pin_token(self, client_id: str, pin: PlexPin) -> str | None:
        response = await self._request(
            "GET", f"/api/v2/pins/{pin.id}", client_id, params=[("code", pin.code)]
        )
        if response is None:
            return None
        payload = _json_object(response)
        if payload is None:
            return None
        token = payload.get("authToken")
        return token if isinstance(token, str) and token else None

    async def account_name(self, client_id: str, token: str) -> str | None:
        response = await self._request("GET", "/api/v2/user", client_id, token=token)
        if response is None:
            return None
        payload = _json_object(response)
        if payload is None:
            return None
        username = payload.get("username")
        if isinstance(username, str) and username:
            return username
        title = payload.get("title")
        return title if isinstance(title, str) and title else None

    async def claim_token(self, client_id: str, token: str) -> str | None:
        response = await self._request("GET", "/api/claim/token.json", client_id, token=token)
        if response is None:
            return None
        return _parse_claim_token(response.text)

    async def _send(
        self,
        method: Literal["GET", "POST"],
        path: str,
        client_id: str,
        *,
        token: str | None = None,
        params: Sequence[tuple[str, str]] = (),
    ) -> httpx.Response | None:
        """Like `_request`, but with no status filter.

        `servers` has to tell a 401 (signed out) apart from every other
        failure (plex.tv itself unreachable) - a response `_request` would
        have already thrown away below 200 can never make that distinction.
        """
        url = f"{PLEX_TV_URL}{path}"
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout
            ) as client:
                return await client.request(
                    method, url, headers=_plex_headers(client_id, token), params=tuple(params)
                )
        except httpx.HTTPError:
            return None

    async def servers(self, client_id: str, token: str) -> PlexServers:
        response = await self._send(
            "GET",
            "/api/v2/resources",
            client_id,
            token=token,
            params=[("includeHttps", "1"), ("includeIPv6", "1")],
        )
        if response is None:
            return PlexServers("unreachable")
        if response.status_code == 401:
            return PlexServers("signed_out")
        if not (200 <= response.status_code < 300):
            return PlexServers("unreachable")
        payload = _json_list(response)
        if payload is None:
            return PlexServers("unreachable")
        return PlexServers("ok", _parse_server_choices(payload))


async def account_plex_servers(plex_tv: PlexTv, config_dir: Path) -> PlexServers:
    """The owner's own Plex servers, read with whatever account is saved in
    `<config_dir>/plex.json` - `signed_out` (never a call to plex.tv at all)
    when there's no saved account or its token is missing, the same "ask
    nothing you don't need to" shape every other door in this module keeps.
    """
    account = load_plex_account(config_dir)
    if account is None or account.token is None:
        return PlexServers("signed_out")
    return await plex_tv.servers(account.client_id, account.token)


def _parse_claim_token(text: str) -> str | None:
    candidate = _claim_token_from_json(text)
    if candidate is None:
        candidate = _claim_token_from_xml(text)
    if candidate is not None and _CLAIM_TOKEN_PATTERN.match(candidate):
        return candidate
    return None


def _claim_token_from_json(text: str) -> str | None:
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("token")
    return value if isinstance(value, str) else None


def _claim_token_from_xml(text: str) -> str | None:
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return None
    return root.get("token")


class FakePlexTv:
    """A scriptable `PlexTv` for tests - no network involved.

    `claims` drains in order and repeats its last entry forever, the way a
    fresh code is minted on every retry of an unclaimed server while the
    account itself never changes. `calls` records method names only - never
    a pin, a token or a claim - so a test can prove a fake was exercised
    without ever holding a secret it shouldn't.
    """

    def __init__(
        self,
        *,
        pin: PlexPin | None = PlexPin(1, "abcd"),
        token: str | None = "fake-plex-token",
        username: str | None = "owner",
        claims: Sequence[str | None] = ("claim-fake1", "claim-fake2"),
        servers: PlexServers = PlexServers("ok"),
    ) -> None:
        self._pin = pin
        self._token = token
        self._username = username
        self._claims: list[str | None] = list(claims)
        self._servers = servers
        self.calls: list[str] = []

    async def create_pin(self, client_id: str) -> PlexPin | None:
        self.calls.append("create_pin")
        return self._pin

    async def pin_token(self, client_id: str, pin: PlexPin) -> str | None:
        self.calls.append("pin_token")
        return self._token

    async def account_name(self, client_id: str, token: str) -> str | None:
        self.calls.append("account_name")
        return self._username

    async def claim_token(self, client_id: str, token: str) -> str | None:
        self.calls.append("claim_token")
        if not self._claims:
            return None
        if len(self._claims) > 1:
            return self._claims.pop(0)
        return self._claims[0]

    async def servers(self, client_id: str, token: str) -> PlexServers:
        self.calls.append("servers")
        return self._servers


# --- connection_candidates / find_connection: choosing a working address --------


@dataclass(frozen=True)
class PlexCandidate:
    """One address worth trying for a chosen `PlexServerChoice`."""

    base_url: str
    port: int
    on_this_nas: bool


def _distinct_ports(connections: tuple[PlexConnection, ...]) -> tuple[int, ...]:
    ports: list[int] = []
    for connection in connections:
        if connection.port not in ports:
            ports.append(connection.port)
    return tuple(ports)


def connection_candidates(
    choice: PlexServerChoice, host_address: str | None
) -> tuple[PlexCandidate, ...]:
    """Every base URL worth trying for `choice`, in the order to try them,
    pure and deduplicated by `base_url`.

    Local, unencrypted addresses come first - the simplest path for both
    Marrquee and a later Seerr to use, with no plex.direct DNS (which
    routers with rebinding protection block) and no TLS. `host_address`
    (Marrquee's own network gateway) comes next, for a Plex bridged on the
    same NAS that advertises an unreachable container IP as "local".
    Encrypted plex.direct addresses come after, then anything remote.
    Relay is never offered here - it's filtered out of `connections`
    entirely while parsing plex.tv's own resources answer.
    """
    candidates: list[PlexCandidate] = []
    seen_urls: set[str] = set()

    def add(base_url: str, port: int, *, on_this_nas: bool) -> None:
        if base_url in seen_urls:
            return
        seen_urls.add(base_url)
        candidates.append(PlexCandidate(base_url=base_url, port=port, on_this_nas=on_this_nas))

    local = [connection for connection in choice.connections if connection.local]
    remote = [connection for connection in choice.connections if not connection.local]

    for connection in local:
        if not connection.ipv6:
            add(
                f"http://{connection.address}:{connection.port}",
                connection.port,
                on_this_nas=False,
            )

    if host_address is not None:
        host = f"[{host_address}]" if ":" in host_address else host_address
        ports = _distinct_ports(choice.connections) or (_PLEX_PORT,)
        for port in ports:
            add(f"http://{host}:{port}", port, on_this_nas=True)

    for connection in local:
        add(connection.uri, connection.port, on_this_nas=False)

    for connection in local:
        if connection.ipv6:
            add(
                f"http://[{connection.address}]:{connection.port}",
                connection.port,
                on_this_nas=False,
            )

    for connection in remote:
        add(connection.uri, connection.port, on_this_nas=False)

    for connection in remote:
        host = f"[{connection.address}]" if ":" in connection.address else connection.address
        add(f"http://{host}:{connection.port}", connection.port, on_this_nas=False)

    if choice.https_required:
        candidates = [
            candidate for candidate in candidates if not candidate.base_url.startswith("http://")
        ]

    return tuple(candidates)


async def find_connection(
    server: PlexServer, choice: PlexServerChoice, host_address: str | None
) -> PlexCandidate | None:
    """The first candidate address whose `/identity` names this exact
    server, or `None` when nothing does.

    Every candidate is asked concurrently - a slow or dead address must
    never delay trying the next one - and the FIRST match in
    `connection_candidates`' own order wins, so a bridged Plex answering at
    the NAS gateway is never preferred over its own advertised local
    address. The machine-id match is what stops a different Plex at a
    recycled IP from ever being mistaken for the chosen one.
    """
    candidates = connection_candidates(choice, host_address)
    if not candidates:
        return None
    identities = await asyncio.gather(
        *(server.identity(candidate.base_url) for candidate in candidates)
    )
    for candidate, identity in zip(candidates, identities, strict=True):
        if identity is not None and identity.machine_id == choice.machine_id:
            return candidate
    return None


# --- link_matches_plex: does a saved link card already point at this server -----


def link_matches_plex(link: LinkCard, choice: PlexServerChoice) -> bool:
    """Whether `link` points at the exact same host and port as one of
    `choice`'s advertised connections.

    Strict host:port equality only - a generic `app.plex.tv` bookmark names
    no server in particular, and a fuzzier match risks offering to replace
    the wrong card.
    """
    link_host, link_port = _host_and_port(link.url)
    if link_host is None:
        return False
    for connection in choice.connections:
        if _normalize_host(connection.address) == link_host and connection.port == link_port:
            return True
        uri_host, uri_port = _host_and_port(connection.uri)
        if uri_host == link_host and uri_port == link_port:
            return True
    return False


def _host_and_port(url: str) -> tuple[str | None, int | None]:
    split = urllib.parse.urlsplit(url)
    hostname = split.hostname
    if hostname is None:
        return None, None
    port = split.port
    if port is None:
        port = 443 if split.scheme == "https" else 80
    return hostname.lower(), port


def _normalize_host(address: str) -> str:
    return address.strip("[]").lower()


# --- the auth URL the browser is sent to ----------------------------------------


def plex_auth_url(client_id: str, code: str, forward_url: str) -> str:
    """The `app.plex.tv/auth` URL that hands the browser to plex.tv and back.

    Whether a LAN `http://` `forwardUrl` is honoured by plex.tv is
    unverified by reading - Chunk 6's own FIRST test pins the URL this
    builds; the live round trip is PENDING the owner's NAS.
    """
    query = urllib.parse.urlencode(
        [
            ("clientID", client_id),
            ("code", code),
            ("context[device][product]", PLEX_PRODUCT),
            ("forwardUrl", forward_url),
        ]
    )
    return PLEX_AUTH_URL + query


# --- the one-time claim secret ---------------------------------------------------


def plex_secrets_host_path(root: PurePosixPath) -> PurePosixPath:
    """Where Plex's one-time claim code lives, as a HOST path under `root`."""
    return root / "marrquee" / "plex"


def write_plex_claim(settings: Settings, root: PurePosixPath, claim: str) -> None:
    """Write the one-time claim code linuxserver's `FILE__PLEX_CLAIM` reads.

    Mirrors `compose.write_vpn_secrets`: the folder is created if missing
    and set to `0o700` every time, and the file itself lands through a
    sibling temp file chmod'd `0o600` then `os.replace` - never briefly
    world-readable, never half-written, and with no trailing newline (a
    claim code linuxserver's `init-envfile` would otherwise warn about).
    Neither the folder nor the file is ever chowned - only Marrquee itself
    ever reads this folder. `storage._safe_join` turns a symlink planted
    where this folder should be into a loud `PathEscapesRoot` rather than a
    silent write somewhere else on the drive; both that and any `OSError`
    the write itself raises propagate, since a claim that failed to write
    is a real bring-up failure, not one to swallow.

    Unlike the VPN secrets folder, nothing else here is ever deleted - this
    folder holds exactly one file, and this function only ever touches it.
    """
    container_root = to_host_view(settings, str(root))
    relative = plex_secrets_host_path(root).relative_to(root)
    folder = storage._safe_join(container_root, relative)

    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)

    path = folder / PLEX_CLAIM_FILE_NAME
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(claim)
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, path)


def clear_plex_claim(settings: Settings, root: PurePosixPath) -> None:
    """Remove the claim file, without ever raising.

    Called after every Plex bring-up, success or failure - a claim code is
    only ever good for a few minutes, so it has no reason to sit on disk a
    moment longer. `lstat` (never `stat`) is what keeps this from following
    a symlink planted in the file's own place; only a genuine regular file
    is ever unlinked, never a directory, a symlink or anything else found
    there. A missing folder or file is simply nothing to do; any other
    filesystem hiccup is only logged, never raised, the same contract
    `compose.clear_vpn_secrets` makes.
    """
    try:
        container_root = to_host_view(settings, str(root))
        relative = plex_secrets_host_path(root).relative_to(root)
        path = storage._safe_join(container_root, relative) / PLEX_CLAIM_FILE_NAME
        entry_stat = path.lstat()
    except FileNotFoundError:
        return
    except (OSError, PathEscapesRoot) as error:
        logger.warning("could not clear the Plex claim file: %s", error)
        return

    if not stat.S_ISREG(entry_stat.st_mode):
        return
    try:
        path.unlink()
    except OSError as error:
        logger.warning("could not clear the Plex claim file: %s", error)


# --- a local Plex server: identity and requests ----------------------------------


@dataclass(frozen=True)
class PlexIdentity:
    """The unauthenticated verdict `/identity` gives about a Plex server."""

    claimed: bool
    machine_id: str


@dataclass(frozen=True)
class PlexResponse:
    """What one call to a local Plex server came back with.

    `detail` is raw technical text - a connect-error message, or a
    non-JSON body's own text - for diagnostics only, the same contract
    `ArrResponse`/`QbitResponse` make.
    """

    ok: bool
    status: int
    payload: object
    detail: str | None


class PlexServer(Protocol):
    """Talks to one local Plex server. Never raises - see `HttpPlexServer.request`."""

    async def identity(self, base_url: str) -> PlexIdentity | None: ...
    async def request(
        self,
        method: Literal["GET", "POST", "PUT"],
        base_url: str,
        path: str,
        token: str,
        *,
        params: Sequence[tuple[str, str]] = (),
    ) -> PlexResponse: ...


def _join(base_url: str, path: str) -> str:
    """Join with exactly one slash, whatever slashes either side carries."""
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


class HttpPlexServer:
    """The real `PlexServer`, over `httpx`."""

    def __init__(
        self, *, timeout: float = 5.0, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._timeout = timeout
        self._transport = transport

    async def identity(self, base_url: str) -> PlexIdentity | None:
        # No `X-Plex-Token` - `/identity` answers before a server is ever
        # claimed, which is exactly what makes it the right pre-flight and
        # readiness check.
        url = _join(base_url, "/identity")
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout
            ) as client:
                response = await client.get(url, headers={"Accept": "application/json"})
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        payload = _json_object(response)
        if payload is None:
            return None
        container = payload.get("MediaContainer")
        if not isinstance(container, dict):
            return None
        claimed = _parse_bool(container.get("claimed"))
        machine_id = container.get("machineIdentifier")
        if claimed is None or not isinstance(machine_id, str) or not machine_id:
            return None
        return PlexIdentity(claimed=claimed, machine_id=machine_id)

    async def request(
        self,
        method: Literal["GET", "POST", "PUT"],
        base_url: str,
        path: str,
        token: str,
        *,
        params: Sequence[tuple[str, str]] = (),
    ) -> PlexResponse:
        url = _join(base_url, path)
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout
            ) as client:
                response = await client.request(
                    method,
                    url,
                    headers={"Accept": "application/json", "X-Plex-Token": token},
                    params=tuple(params),
                )
        except httpx.HTTPError as error:
            return PlexResponse(
                ok=False, status=0, payload=None, detail=f"{type(error).__name__}: {error}"
            )

        ok = 200 <= response.status_code < 300
        payload: object = None
        if response.content:
            try:
                payload = response.json()
            except ValueError:
                payload = response.text
        detail = None if ok else (response.text if response.content else None)
        return PlexResponse(ok=ok, status=response.status_code, payload=payload, detail=detail)


class FakePlexServer:
    """A scriptable `PlexServer` for tests - no network involved.

    `identities` drains in order and repeats its last entry - modelling
    `init-plex-claim`'s own retry-until-claimed behaviour - and is empty by
    default, answering None. `identities_by_url`, when given, answers by
    address instead and ignores the drain list entirely - what
    `find_connection` needs to prove it picks the right one out of several
    candidates. `script` maps `(method, path)` to a queue of `PlexResponse`s,
    drained in order; an unscripted call, or an exhausted queue, raises
    `KeyError` naming exactly what was unscripted, the same contract
    `FakeArrClient` makes.
    """

    def __init__(
        self,
        *,
        identities: Sequence[PlexIdentity | None] = (),
        script: Mapping[tuple[str, str], Sequence[PlexResponse]] | None = None,
        identities_by_url: Mapping[str, PlexIdentity | None] | None = None,
    ) -> None:
        self._identities: list[PlexIdentity | None] = list(identities)
        self._queues: dict[tuple[str, str], list[PlexResponse]] = {
            key: list(responses) for key, responses in (script or {}).items()
        }
        self._identities_by_url = identities_by_url
        self.calls: list[tuple[str, str, tuple[tuple[str, str], ...]]] = []
        self.identity_calls: list[str] = []

    async def identity(self, base_url: str) -> PlexIdentity | None:
        self.identity_calls.append(base_url)
        if self._identities_by_url is not None:
            return self._identities_by_url.get(base_url)
        if not self._identities:
            return None
        if len(self._identities) > 1:
            return self._identities.pop(0)
        return self._identities[0]

    async def request(
        self,
        method: Literal["GET", "POST", "PUT"],
        base_url: str,
        path: str,
        token: str,
        *,
        params: Sequence[tuple[str, str]] = (),
    ) -> PlexResponse:
        self.calls.append((method, path, tuple(params)))
        key = (method, path)
        queue = self._queues.get(key)
        if not queue:
            raise KeyError(f"no scripted response left for {key!r}")
        return queue.pop(0)


# --- existing_plex.json: the owner's own, already-running Plex ------------------


FolderState = Literal["unchecked", "added", "already", "not_seen"]
_FOLDER_STATES: Final = ("unchecked", "added", "already", "not_seen")


@dataclass(frozen=True)
class ExistingPlex:
    """What Marrquee remembers about the owner's own, already-running Plex.

    `token` never appears in `repr()` - the same guarantee `PlexAccount`
    makes, since this record carries the server's own access token rather
    than the owner's plex.tv one.
    """

    machine_id: str
    name: str
    base_url: str
    port: int
    on_this_nas: bool
    token: str = field(repr=False)
    folders: Mapping[str, FolderState]
    sections: Mapping[str, str]
    replaces_link: str | None


def save_existing_plex(config_dir: Path, record: ExistingPlex) -> None:
    """Persist the connected Plex's record, root-only, through the same
    atomic write every secret-bearing file in this package uses.
    """
    write_json_atomic(
        config_dir / _EXISTING_PLEX_JSON_FILE_NAME,
        {
            "version": _EXISTING_PLEX_JSON_VERSION,
            "machine_id": record.machine_id,
            "name": record.name,
            "base_url": record.base_url,
            "port": record.port,
            "on_this_nas": record.on_this_nas,
            "token": record.token,
            "folders": dict(record.folders),
            "sections": dict(record.sections),
            "replaces_link": record.replaces_link,
        },
    )


def load_existing_plex(config_dir: Path) -> ExistingPlex | None:
    """Read `<config_dir>/existing_plex.json`, or None for any reason at
    all - the same never-raising contract `load_plex_account` makes.
    """
    try:
        raw = (config_dir / _EXISTING_PLEX_JSON_FILE_NAME).read_text()
    except OSError:
        return None

    if not raw.strip():
        return None

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None

    if not isinstance(payload, dict) or not _is_existing_plex_version(payload.get("version")):
        return None

    try:
        return _existing_plex_from_payload(payload)
    except (KeyError, TypeError, ValueError):
        return None


def update_existing_plex(
    config_dir: Path, *, folders: Mapping[str, FolderState], sections: Mapping[str, str]
) -> bool:
    """Replace the saved record's `folders`/`sections` with a fresh wiring
    verdict, keeping every other field (the address, the token, the
    replaced-link id) exactly as they were.

    Returns False, without writing anything, when there is no record to
    update - the owner disconnected mid-run, say. OSError from the write
    itself propagates: a verdict that failed to save is a real failure, not
    one this function is allowed to swallow.
    """
    record = load_existing_plex(config_dir)
    if record is None:
        return False
    save_existing_plex(config_dir, replace(record, folders=dict(folders), sections=dict(sections)))
    return True


def clear_existing_plex(config_dir: Path) -> None:
    """Delete the saved record, without ever raising - a missing file is
    already the state disconnecting wants.
    """
    try:
        (config_dir / _EXISTING_PLEX_JSON_FILE_NAME).unlink()
    except FileNotFoundError:
        return
    except OSError as error:
        logger.warning("could not clear the existing-Plex record: %s", error)


def _is_existing_plex_version(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value == _EXISTING_PLEX_JSON_VERSION
    )


def _existing_plex_from_payload(payload: Mapping[str, object]) -> ExistingPlex:
    port = payload.get("port")
    if not _is_plain_int(port):
        raise ValueError(f"expected an int port, got {port!r}")
    on_this_nas = payload.get("on_this_nas")
    if not isinstance(on_this_nas, bool):
        raise ValueError(f"expected a bool on_this_nas, got {on_this_nas!r}")
    return ExistingPlex(
        machine_id=_require_str(payload.get("machine_id")),
        name=_require_str(payload.get("name")),
        base_url=_require_str(payload.get("base_url")),
        port=cast(int, port),
        on_this_nas=on_this_nas,
        token=_require_str(payload.get("token")),
        folders=_require_folder_states(payload.get("folders")),
        sections=_require_section_keys(payload.get("sections")),
        replaces_link=_require_optional_str(payload.get("replaces_link")),
    )


def _require_folder_states(value: object) -> dict[str, FolderState]:
    if not isinstance(value, dict):
        raise ValueError(f"expected a folders object, got {value!r}")
    result: dict[str, FolderState] = {}
    for key, state in value.items():
        if not isinstance(key, str) or state not in _FOLDER_STATES:
            raise ValueError(f"bad folder state: {key!r} -> {state!r}")
        result[key] = cast(FolderState, state)
    return result


def _require_section_keys(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError(f"expected a sections object, got {value!r}")
    result: dict[str, str] = {}
    for key, section_key in value.items():
        if not isinstance(key, str) or not isinstance(section_key, str):
            raise ValueError(f"bad section entry: {key!r} -> {section_key!r}")
        result[key] = section_key
    return result


def existing_plex_web_url(record: ExistingPlex, authority: str | None) -> str | None:
    """The link the Hub shows for a connected Plex.

    A Plex Marrquee found on this NAS gets a link built the same way every
    other app's does - through the browser's own address - since
    `base_url` may be a gateway address that's useless to a phone on the
    LAN. Anything else keeps the address Marrquee actually connected
    through, which the owner's own browser can always reach.
    """
    if record.on_this_nas:
        return app_url(authority, record.port, path="/web")
    return f"{record.base_url}/web"


# --- folder-visibility markers: prove Plex can see a folder, then remove it -----


def make_folder_marker(
    settings: Settings,
    root: PurePosixPath,
    media_folder: str,
    *,
    marker_name: str | None = None,
) -> str:
    """Create a uniquely named, empty folder inside `media_folder`'s own
    library folder, and return its name.

    A folder, not a file: Plex's `includeFiles=0` browse only ever lists
    folders, exactly what `PlexServer.browse`'s own `isBrowsable` checks -
    whether it lists a stray file at all is unverified by reading. Both an
    `OSError` and a `PathEscapesRoot` propagate: a marker that failed to
    write means the probe can prove nothing at all, which is a real
    failure, not one to swallow.
    """
    name = marker_name if marker_name is not None else PROBE_FOLDER_PREFIX + secrets.token_hex(6)
    container_root = to_host_view(settings, str(root))
    relative = host_media_path(str(root), media_folder).relative_to(root)
    parent = storage._safe_join(container_root, relative)
    (parent / name).mkdir(exist_ok=False)
    return name


def remove_folder_marker(
    settings: Settings, root: PurePosixPath, media_folder: str, name: str
) -> None:
    """Remove one marker `make_folder_marker` created, without ever raising.

    Refuses anything without `PROBE_FOLDER_PREFIX` outright - the owner's
    own folders are never something this function is allowed to touch.
    `_safe_join` resolves only the library folder, never `name` itself, so
    the follow-up `lstat` still sees the marker's true nature; only a
    genuine, empty directory is ever removed, through `rmdir` alone - never
    a recursive delete, and never anything that only happens to be *named*
    like a marker (a real folder, a symlink, or a plain file).
    """
    if not name.startswith(PROBE_FOLDER_PREFIX):
        logger.warning("refusing to remove a folder Marrquee didn't mark itself")
        return

    try:
        container_root = to_host_view(settings, str(root))
        relative = host_media_path(str(root), media_folder).relative_to(root)
        parent = storage._safe_join(container_root, relative)
        path = parent / name
        entry_stat = path.lstat()
    except FileNotFoundError:
        return
    except (OSError, PathEscapesRoot) as error:
        logger.warning("could not remove the Plex folder-visibility marker: %s", error)
        return

    if not stat.S_ISDIR(entry_stat.st_mode):
        return
    try:
        path.rmdir()
    except OSError as error:
        logger.warning("could not remove the Plex folder-visibility marker: %s", error)


def remove_stale_folder_markers(settings: Settings, root: PurePosixPath, media_folder: str) -> None:
    """Remove any of Marrquee's OWN marker folders an interrupted probe left
    behind, before a new one is made.

    Only ever inspects entries whose name already starts with
    `PROBE_FOLDER_PREFIX` - every other entry in the library folder, real or
    not, is never even looked at twice. Never raises: a folder that can't
    be listed yet simply has nothing stale to clean up.
    """
    try:
        container_root = to_host_view(settings, str(root))
        relative = host_media_path(str(root), media_folder).relative_to(root)
        parent = storage._safe_join(container_root, relative)
        names = tuple(entry.name for entry in parent.iterdir())
    except (OSError, PathEscapesRoot):
        return

    for name in names:
        if name.startswith(PROBE_FOLDER_PREFIX):
            remove_folder_marker(settings, root, media_folder, name)


# --- browse: does Plex's own filesystem view include our marker folder ---------


def browse_path(path: str) -> str:
    """The `/services/browse/<base64>` path for one folder path.

    Plex expects the raw path base64-encoded in the URL's own path segment,
    never URL-encoded directly - the same encoding python-plexapi's
    `utils.base64str` uses, so a folder path with spaces or slashes travels
    safely.
    """
    return "/services/browse/" + base64.b64encode(path.encode()).decode()


def parse_browse_folders(payload: object) -> tuple[tuple[str, str], ...]:
    """Read `MediaContainer.Path[]`/`Directory[]` into `(path, title)` pairs.

    Never raises. Plex answers a plain filesystem browse with `Path`
    entries and other browse contexts with `Directory` entries; reading
    either (or both, if a future Plex version sends both) is what keeps
    this working across both.
    """
    if not isinstance(payload, dict):
        return ()
    container = payload.get("MediaContainer")
    if not isinstance(container, dict):
        return ()

    folders: list[tuple[str, str]] = []
    for key in ("Path", "Directory"):
        raw = container.get(key)
        if not isinstance(raw, list):
            continue
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            title = entry.get("title")
            folders.append(
                (path if isinstance(path, str) else "", title if isinstance(title, str) else "")
            )
    return tuple(folders)


@dataclass(frozen=True)
class FolderSeen:
    """Whether a Plex server's own browse listing includes our marker."""

    state: Literal["seen", "not_seen", "unknown"]
    path: str | None
    technical: str | None


def _marker_seen(folders: tuple[tuple[str, str], ...], name: str) -> bool:
    return any(title == name or PurePosixPath(path).name == name for path, title in folders)


async def probe_folder(
    server: PlexServer,
    base_url: str,
    token: str,
    settings: Settings,
    root: PurePosixPath,
    media_folder: str,
) -> FolderSeen:
    """Prove whether `server` can see one of Marrquee's own media folders,
    by planting a uniquely named empty folder inside it and looking for
    that exact name in Plex's own browse listing.

    A path merely existing inside Plex's own filesystem view proves
    nothing - a remote Plex following the same TRaSH layout has
    `/data/media/movies` too. The marker's name, freshly random every
    probe, is what a lookalike can never have. Tries the owner's own host
    path first, then the container path Marrquee's own data mount uses;
    either counts as seen. Never raises: every failure this can hit - a
    dead Plex, a folder that can't be written, a browse that times out -
    comes back as a `FolderSeen` instead of an exception. The marker is
    always removed again in `finally`, whatever happened, and its
    `technical` text is built only from the candidate path and the status
    Plex answered with - never the token.
    """
    remove_stale_folder_markers(settings, root, media_folder)
    try:
        name = make_folder_marker(settings, root, media_folder)
    except (OSError, PathEscapesRoot) as error:
        return FolderSeen(state="unknown", path=None, technical=type(error).__name__)

    try:
        candidates: list[str] = []
        for candidate in (
            str(host_media_path(str(root), media_folder)),
            str(container_media_path(media_folder)),
        ):
            if candidate not in candidates:
                candidates.append(candidate)

        transient_technical: str | None = None
        for candidate in candidates:
            response = await server.request(
                "GET",
                base_url,
                browse_path(candidate),
                token,
                params=[("includeFiles", "0")],
            )
            if response.status == 0 or response.status >= 500:
                transient_technical = f"{candidate}: HTTP {response.status}"
                continue
            if response.ok and _marker_seen(parse_browse_folders(response.payload), name):
                return FolderSeen(state="seen", path=candidate, technical=None)

        if transient_technical is not None:
            return FolderSeen(state="unknown", path=None, technical=transient_technical)
        return FolderSeen(state="not_seen", path=None, technical=None)
    finally:
        remove_folder_marker(settings, root, media_folder, name)


# --- Plex's own library sections: the shared parser -------------------------------


@dataclass(frozen=True)
class PlexSection:
    """One library Plex already has, as `GET /library/sections` describes it."""

    key: str
    type: str
    title: str
    locations: tuple[str, ...]


def _section_locations(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    locations: list[str] = []
    for entry in raw:
        if isinstance(entry, dict):
            path = entry.get("path")
            if isinstance(path, str):
                locations.append(path)
    return tuple(locations)


def parse_plex_sections(payload: object) -> tuple[PlexSection, ...]:
    """Read `MediaContainer.Directory[]` into `PlexSection`s, skipping any
    entry without a usable string `key` - never raises.
    """
    if not isinstance(payload, dict):
        return ()
    container = payload.get("MediaContainer")
    if not isinstance(container, dict):
        return ()
    directories = container.get("Directory")
    if not isinstance(directories, list):
        return ()

    sections: list[PlexSection] = []
    for entry in directories:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if not isinstance(key, str) or not key:
            continue
        section_type = entry.get("type")
        title = entry.get("title")
        sections.append(
            PlexSection(
                key=key,
                type=section_type if isinstance(section_type, str) else "",
                title=title if isinstance(title, str) else "",
                locations=_section_locations(entry.get("Location")),
            )
        )
    return tuple(sections)


# --- reaching Marrquee's own host from inside a container ------------------------


async def plex_host_address(engine: DockerEngine, self_id: str | None) -> str | None:
    """Marrquee's own reachable address for a host-networked Plex.

    None when Marrquee doesn't know its own container id - every caller
    that matters resolves that first. Plex binds every host interface, so
    the gateway of any one of Marrquee's own networks is a real address for
    it, host-networked stack or not.
    """
    if self_id is None:
        return None
    return await engine.host_gateway(self_id)


def plex_base_url(address: str) -> str:
    """The local Plex server's base URL for one reachable address."""
    host = f"[{address}]" if ":" in address else address
    return f"http://{host}:{_PLEX_PORT}"

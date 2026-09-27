"""Sign in with Plex: the plex.tv PIN round trip, reachable from the Hub's
install panel and the wizard's Plex step alike.

`POST /plex/sign-in` asks plex.tv for a fresh pin and sends the browser to
app.plex.tv with a `forwardUrl` built entirely from Marrquee's own request -
never from anything a query string or form field supplies - so the round
trip can never become an open redirect. `GET /plex/signed-in` only ever
accepts a `state` token Marrquee minted itself with `secrets.token_urlsafe`
and is still holding in `PlexSignInStore` - NEVER plex.tv's own pin `id`,
which is a small, plausibly-guessable number (and is never even sent to the
browser); a token there would sit in every access log between here and the
browser, which the high-entropy state itself would too if it appeared
anywhere else, so it is the ONLY identifier this route trusts.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Final, Literal

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from marrquee import words
from marrquee.catalog import EXISTING_PLEX_APP_ID, PLEX_APP_ID, get_app, unavailable_reason
from marrquee.config import Settings
from marrquee.deploy import DeployManager
from marrquee.docker_client import DockerEngine
from marrquee.hub import HubPanel, plex_servers_panel
from marrquee.links import LINK_ID_RE, LinkCard, load_links
from marrquee.plex import (
    ExistingPlex,
    PlexPin,
    PlexServer,
    PlexServers,
    PlexTv,
    account_plex_servers,
    clear_existing_plex,
    find_connection,
    link_matches_plex,
    plex_auth_url,
    plex_client_id,
    plex_host_address,
    save_existing_plex,
    save_plex_sign_in,
)
from marrquee.questions import PLEX_ACCOUNT_FIELD, save_step_answers
from marrquee.wizard import parse_app_ids

router = APIRouter()

# A restart just means pressing Sign in with Plex again - nothing here is
# ever worth persisting to disk.
PLEX_PIN_TTL_SECONDS: Final = 1800.0
_MAX_PENDING_SIGN_INS: Final = 20
# 32 random bytes, url-safe-base64-encoded to ~43 characters - large enough
# that guessing or brute-forcing one before it's used (single-use, and
# capped at `PLEX_PIN_TTL_SECONDS`) is not a real attack surface, unlike
# plex.tv's own small, sequential-looking pin `id`.
_STATE_TOKEN_BYTES: Final = 32
# plex.tv's own `clientIdentifier` is a UUID hex string - this is a generous
# ceiling on the POSTED `machine_id`, never a shape check, so an oversized
# value is refused before it's ever compared against the freshly-fetched
# server list.
_MACHINE_ID_MAX_LENGTH: Final = 64

PlexSignInThen = Literal["hub", "wizard", "connect"]


@dataclass(frozen=True)
class PendingPlexSignIn:
    """One pin Marrquee itself asked plex.tv for, waiting on the owner to
    approve it there and come back - keyed in `PlexSignInStore` by a state
    token, never by `pin.id` itself.
    """

    pin: PlexPin
    then: PlexSignInThen
    apps: tuple[str, ...]
    created: float


class PlexSignInStore:
    """Every pending sign-in Marrquee is still waiting on plex.tv for, in
    memory only, keyed by a high-entropy state token (never plex.tv's own
    pin id, which is a small, plausibly-guessable number) - that token,
    single-use and short-lived, is what makes `GET /plex/signed-in` safe to
    leave unguarded by `SameOriginGuard` (which only ever covers unsafe
    methods).
    """

    def __init__(self) -> None:
        self._pending: dict[str, PendingPlexSignIn] = {}

    def put(self, state: str, pending: PendingPlexSignIn) -> None:
        """Remember `pending` under `state`, keeping at most the 20 newest -
        an owner who opens the sign-in flow in a dozen abandoned tabs can
        never grow this past a small, bounded amount of memory.
        """
        self._pending[state] = pending
        while len(self._pending) > _MAX_PENDING_SIGN_INS:
            oldest_state = min(self._pending, key=lambda key: self._pending[key].created)
            del self._pending[oldest_state]

    def take(self, state: str, now: float) -> PendingPlexSignIn | None:
        """The pending sign-in for `state`, removed either way - a second
        look-up (a reloaded return page, a replayed link) always finds
        nothing, the same "single use" guarantee a real login token gets.

        `None` for a state never put here (an unknown value, a guess, or
        plex.tv's own pin id mistaken for one), and for one put here too
        long ago (older than `PLEX_PIN_TTL_SECONDS`, by `now`). The lookup
        itself is an exact dict key match - safe because `state` is a
        32-byte random token, not a value worth a constant-time compare.
        """
        pending = self._pending.pop(state, None)
        if pending is None:
            return None
        if now - pending.created > PLEX_PIN_TTL_SECONDS:
            return None
        return pending


def _hub_failure_redirect() -> Response:
    return RedirectResponse("/?panel=install&sign_in=failed#hub-panel", status_code=303)


def _wizard_failure_redirect(apps: tuple[str, ...]) -> Response:
    apps_csv = ",".join(apps)
    return RedirectResponse(
        f"/setup/questions/plex/sign-in?apps={apps_csv}&sign_in=failed", status_code=303
    )


def _failure_redirect(then: PlexSignInThen, apps: tuple[str, ...]) -> Response:
    if then == "wizard":
        return _wizard_failure_redirect(apps)
    return _hub_failure_redirect()


def _installed_ids(request: Request) -> tuple[str, ...]:
    manager: DeployManager = request.app.state.deploy
    return tuple(progress.app_id for progress in manager.snapshot().apps)


@router.post("/plex/sign-in")
async def post_plex_sign_in(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    form = await request.form()
    then_raw = form.get("then")
    then: PlexSignInThen | None
    if then_raw == "hub":
        then = "hub"
    elif then_raw == "wizard":
        then = "wizard"
    elif then_raw == "connect":
        then = "connect"
    else:
        return RedirectResponse("/", status_code=303)

    manager: DeployManager = request.app.state.deploy
    snapshot = manager.snapshot()
    if then == "wizard" and snapshot.phase == "finale":
        # Mirrors `routes/wizard._hub_exists` - a stale wizard tab must never
        # resurrect the wizard, or worse, start a sign-in plex.tv would come
        # back to a screen that no longer exists.
        return RedirectResponse("/", status_code=303)

    installed = _installed_ids(request)
    if then == "hub" and (
        PLEX_APP_ID in installed or unavailable_reason(get_app(PLEX_APP_ID), installed) is not None
    ):
        return RedirectResponse("/", status_code=303)
    if then == "connect" and (
        snapshot.phase != "finale"
        or EXISTING_PLEX_APP_ID in installed
        or unavailable_reason(get_app(EXISTING_PLEX_APP_ID), installed) is not None
    ):
        return RedirectResponse("/", status_code=303)

    apps_raw = form.get("apps")
    apps = parse_app_ids(apps_raw if isinstance(apps_raw, str) else "")

    plex_tv: PlexTv = request.app.state.plex_tv
    client_id = plex_client_id(settings.config_dir)
    pin = await plex_tv.create_pin(client_id)
    if pin is None:
        return _failure_redirect(then, apps)

    state = secrets.token_urlsafe(_STATE_TOKEN_BYTES)
    store: PlexSignInStore = request.app.state.plex_sign_in
    store.put(state, PendingPlexSignIn(pin=pin, then=then, apps=apps, created=time.monotonic()))

    base = str(request.base_url).rstrip("/")
    forward_url = f"{base}/plex/signed-in?state={state}"
    return RedirectResponse(plex_auth_url(client_id, pin.code, forward_url), status_code=303)


@router.get("/plex/signed-in")
async def get_plex_signed_in(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    # `pin` (plex.tv's own small, guessable id) is never accepted here, even
    # if present - only the state token this route itself minted counts.
    state = request.query_params.get("state")
    if not state:
        return _hub_failure_redirect()

    store: PlexSignInStore = request.app.state.plex_sign_in
    pending = store.take(state, time.monotonic())
    if pending is None:
        return _hub_failure_redirect()

    plex_tv: PlexTv = request.app.state.plex_tv
    client_id = plex_client_id(settings.config_dir)
    token = await plex_tv.pin_token(client_id, pending.pin)
    if token is None:
        return _failure_redirect(pending.then, pending.apps)

    name = await plex_tv.account_name(client_id, token)
    if name is None:
        return _failure_redirect(pending.then, pending.apps)

    # The token lands on disk FIRST, so an answer can never exist without
    # one - `check_step`'s `sign_in` field trusts a saved answer alone, so
    # the two must never disagree about whether the sign-in actually holds.
    save_plex_sign_in(settings.config_dir, token, name)

    if pending.then == "connect":
        # Never `save_step_answers` here - that write is what the new-Plex
        # row's own `needs_sign_in` question reads, and saving it for a
        # connect would flip that row to "Add Plex" the moment the owner
        # only meant to connect their existing one.
        return RedirectResponse("/?panel=plex-servers#hub-panel", status_code=303)

    save_step_answers(settings.config_dir, PLEX_APP_ID, {PLEX_ACCOUNT_FIELD: name})

    if pending.then == "wizard":
        apps_csv = ",".join(pending.apps)
        return RedirectResponse(f"/setup/questions/plex/sign-in?apps={apps_csv}", status_code=303)
    return RedirectResponse("/?panel=install#hub-panel", status_code=303)


# --- Picking a server and connecting to it, without JavaScript --------------


def _form_value(form: object, key: str) -> str:
    value = form.get(key)  # type: ignore[attr-defined]
    return value if isinstance(value, str) else ""


async def _plex_connect_refusal(
    request: Request, servers: PlexServers, links: list[LinkCard], problem: str
) -> Response:
    """Re-render the live Hub with the `plex-servers` pane still open, the
    server list already in hand (never re-fetched), and `problem` in place
    of whatever `servers.state` would otherwise say - nothing is ever saved
    on this path.
    """
    from marrquee.routes.hub import _hub_response, read_hub_view

    view = await read_hub_view(request)
    panel = plex_servers_panel(servers, links, problem=problem)
    return _hub_response(request, view, panel, status_code=200)


@router.post("/plex/connect")
async def post_plex_connect(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    manager: DeployManager = request.app.state.deploy
    form = await request.form()
    machine_id = _form_value(form, "machine_id")
    replace_link_id = _form_value(form, "replace_link")

    installed = _installed_ids(request)
    if (
        manager.snapshot().phase != "finale"
        or EXISTING_PLEX_APP_ID in installed
        or unavailable_reason(get_app(EXISTING_PLEX_APP_ID), installed) is not None
        or not machine_id
        or len(machine_id) > _MACHINE_ID_MAX_LENGTH
    ):
        return RedirectResponse("/", status_code=303)

    plex_tv: PlexTv = request.app.state.plex_tv
    servers = await account_plex_servers(plex_tv, settings.config_dir)
    links = list(load_links(settings.config_dir))
    choice = next((server for server in servers.servers if server.machine_id == machine_id), None)
    if choice is None:
        return await _plex_connect_refusal(request, servers, links, words.EXISTING_PLEX_LIST_FAILED)

    engine: DockerEngine = request.app.state.docker_engine
    plex_server: PlexServer = request.app.state.plex_server
    host_address = await plex_host_address(engine, await engine.self_container_id())
    candidate = await find_connection(plex_server, choice, host_address)
    if candidate is None:
        return await _plex_connect_refusal(
            request, servers, links, words.existing_plex_unreachable(choice.name)
        )

    # The browser posts only an id - the address and the token above are
    # both re-read on the server, from plex.tv's own answer and the
    # candidate `find_connection` just proved, never from anything the
    # form carried.
    replaces_link: str | None = None
    if replace_link_id and LINK_ID_RE.match(replace_link_id):
        link = next((card for card in links if card.id == replace_link_id), None)
        if link is not None and link_matches_plex(link, choice):
            replaces_link = replace_link_id

    save_existing_plex(
        settings.config_dir,
        ExistingPlex(
            machine_id=choice.machine_id,
            name=choice.name,
            base_url=candidate.base_url,
            port=candidate.port,
            on_this_nas=candidate.on_this_nas,
            token=choice.token,
            folders=dict.fromkeys(get_app(EXISTING_PLEX_APP_ID).library_folders, "unchecked"),
            sections={},
            replaces_link=replaces_link,
        ),
    )
    result = manager.add_app(EXISTING_PLEX_APP_ID)
    if result == "started":
        return RedirectResponse("/", status_code=303)

    # A deferred import: `routes/api.py` itself imports `read_hub_view` from
    # `routes/hub.py`, so importing its helper back at module load time
    # would be a real cycle - by the time this function actually runs, both
    # modules are already fully loaded.
    from marrquee.routes.api import _add_start_refusal_message

    clear_existing_plex(settings.config_dir)
    message = _add_start_refusal_message(result, get_app(EXISTING_PLEX_APP_ID), manager)
    return await _plex_connect_refusal(request, servers, links, message)


# --- Managing and disconnecting an already-connected Plex -------------------


async def _plex_manage_refusal(request: Request, message: str) -> Response:
    """Re-render the live Hub with the Manage pane still open and `message`
    shown - Disconnect's own busy/needed refusals, neither of which change
    anything on disk.
    """
    from marrquee.routes.hub import _hub_response, read_hub_view

    view = await read_hub_view(request)
    panel = HubPanel(mode="plex", edit=None, label="", url="", error=message)
    return _hub_response(request, view, panel, status_code=200)


@router.post("/plex/disconnect")
async def post_plex_disconnect(request: Request) -> Response:
    manager: DeployManager = request.app.state.deploy
    result = await manager.disconnect(EXISTING_PLEX_APP_ID)
    if result.outcome == "busy":
        return await _plex_manage_refusal(request, words.EXISTING_PLEX_DISCONNECT_BUSY)
    if result.outcome == "needed":
        return await _plex_manage_refusal(
            request, words.existing_plex_disconnect_needed(result.needed_by or "")
        )
    return RedirectResponse("/", status_code=303)

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

from marrquee.catalog import PLEX_APP_ID, get_app, unavailable_reason
from marrquee.config import Settings
from marrquee.deploy import DeployManager
from marrquee.plex import PlexPin, PlexTv, plex_auth_url, plex_client_id, save_plex_sign_in
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

PlexSignInThen = Literal["hub", "wizard"]


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
    else:
        return RedirectResponse("/", status_code=303)

    manager: DeployManager = request.app.state.deploy
    if then == "wizard" and manager.snapshot().phase == "finale":
        # Mirrors `routes/wizard._hub_exists` - a stale wizard tab must never
        # resurrect the wizard, or worse, start a sign-in plex.tv would come
        # back to a screen that no longer exists.
        return RedirectResponse("/", status_code=303)

    installed = _installed_ids(request)
    if then == "hub" and (
        PLEX_APP_ID in installed or unavailable_reason(get_app(PLEX_APP_ID), installed) is not None
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
    save_step_answers(settings.config_dir, PLEX_APP_ID, {PLEX_ACCOUNT_FIELD: name})

    if pending.then == "wizard":
        apps_csv = ",".join(pending.apps)
        return RedirectResponse(f"/setup/questions/plex/sign-in?apps={apps_csv}", status_code=303)
    return RedirectResponse("/?panel=install#hub-panel", status_code=303)

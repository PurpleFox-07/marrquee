"""Puts the one saved login on an app through that app's own API.

This is the contract every later `LoginKind` extends: a new applier branch
that works from the login alone - an admin credential Marrquee already
holds (the app's API key) - never the app's OLD password. That is what
lets Change and the forgotten-password reset go through the exact same
path as choosing a login the first time.

Never imports `deploy` - the dependency runs one direction only, the same
`wiring` seam shape (`marrquee/wiring/__init__.py`'s own docstring makes
the same promise): the deploy engine calls into this module, never the
other way around.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from marrquee.catalog import CatalogApp
from marrquee.jellyfin import (
    HttpJellyfinServer,
    JellyfinResponse,
    JellyfinServer,
    jellyfin_base_url,
    load_jellyfin,
)
from marrquee.login import SavedLogin
from marrquee.state import InstallState
from marrquee.wiring.arr_client import ArrClient, ArrResponse, HttpArrClient
from marrquee.wiring.qbit_client import HttpQbitClient, QbitClient, QbitResponse, preferences_form
from marrquee.wiring.steps import app_base_url

logger = logging.getLogger(__name__)

_CONFIG_HOST_PATH = "config/host"
_REDACTED_PASSWORD_PLACEHOLDER = "<redacted-password>"


@dataclass(frozen=True)
class LoginApplyResult:
    """What one attempt to put the saved login on one app came back with.

    `technical` is raw text for diagnostics only, same contract as every
    other `technical`/`detail`-shaped field in this codebase - and it is
    already redacted by the time it leaves this module.
    """

    ok: bool
    technical: str | None


class LoginApplier(Protocol):
    """Puts `login` on `app`. Never raises.

    A new `LoginKind` later (qBittorrent, Jellyfin, Seerr) is a new branch
    here plus a new value on `LoginKind` - never a change to this
    signature, and never a dependency on the app's previous password.
    """

    async def apply(
        self, app: CatalogApp, install: InstallState, login: SavedLogin
    ) -> LoginApplyResult: ...


class NoLoginApplier:
    """`DeployManager`'s own default - honest, never records a success.

    Mirrors `wiring.NoWiringYet`: a `DeployManager` built on its own (the
    shape most tests use) never claims to have put a login anywhere.
    """

    async def apply(
        self, app: CatalogApp, install: InstallState, login: SavedLogin
    ) -> LoginApplyResult:
        return LoginApplyResult(ok=False, technical="no login applier configured")


class FakeLoginApplier:
    """A scriptable `LoginApplier` for tests - no network involved.

    `results` maps an app id to whether it accepts the login; an app with
    no entry defaults to accepting it. `calls` never carries the password -
    only what a Hub reader is allowed to know: which app, which username,
    which generation.
    """

    def __init__(
        self, results: Mapping[str, bool] | None = None, technical: str | None = None
    ) -> None:
        self._results = dict(results) if results is not None else {}
        self._technical = technical
        self.calls: list[tuple[str, str, int]] = []

    async def apply(
        self, app: CatalogApp, install: InstallState, login: SavedLogin
    ) -> LoginApplyResult:
        self.calls.append((app.id, login.username, login.generation))
        ok = self._results.get(app.id, True)
        if ok:
            return LoginApplyResult(ok=True, technical=None)
        return LoginApplyResult(
            ok=False, technical=self._technical or f"scripted failure for {app.id}"
        )


def _is_transient(status: int) -> bool:
    """The two cases worth retrying - no reply at all, or a 5xx. A 400 is a
    considered answer, never a hiccup.
    """
    return status == 0 or 500 <= status < 600


def _redact_password(text: str, password: str) -> str:
    if not password:
        return text
    return text.replace(password, _REDACTED_PASSWORD_PLACEHOLDER)


def _failure_technical(app_id: str, method: str, path: str, response: ArrResponse) -> str:
    """Built only from the app id, method, path, status, each failure's
    property/message, and the free-text detail - NEVER from `payload`, since
    a FluentValidation body can echo `attemptedValue` straight back.
    """
    parts = [f"{app_id}: {method} {path} -> HTTP {response.status}"]
    if response.failures:
        parts.append(
            "; ".join(
                f"{failure.property_name}: {failure.error_message}" for failure in response.failures
            )
        )
    if response.detail:
        parts.append(response.detail)
    return " ".join(parts)


def _qbit_failure_technical(app_id: str, path: str, response: QbitResponse) -> str:
    """Built only from the app id, path, status and the free-text detail -
    NEVER from `payload` - the same "no echoed body" rule `_failure_technical`
    keeps for the arr apps.
    """
    parts = [f"{app_id}: POST {path} -> HTTP {response.status}"]
    if response.detail:
        parts.append(response.detail)
    return " ".join(parts)


def _jellyfin_failure_technical(
    app_id: str, method: str, path: str, response: JellyfinResponse
) -> str:
    """Built only from the app id, method, path, status and `detail` -
    `detail` is only ever a dead-transport message (see
    `jellyfin.JellyfinResponse`), never a Jellyfin response body, so this
    can never echo back the admin's key or password either.
    """
    parts = [f"{app_id}: {method} {path} -> HTTP {response.status}"]
    if response.detail:
        parts.append(response.detail)
    return " ".join(parts)


class HttpLoginApplier:
    """The real `LoginApplier`, for `login_kind == "arr"` and `"qbittorrent"`
    apps.

    An arr app is `GET`'s its current host config, then `PUT`'s it back with
    the forms/enabled auth switch and the saved username and password - the
    same single-user `Upsert` Change and the forgotten-password reset also
    go through, since neither one needs the app's old password. qBittorrent
    takes the exact same login through one POST instead, using its own API
    key rather than a cookie session - see `_apply_qbittorrent`.
    """

    def __init__(
        self,
        client: ArrClient | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        attempts: int = 3,
        retry_delay: float = 2.0,
        qbit: QbitClient | None = None,
        jellyfin: JellyfinServer | None = None,
        jellyfin_address: Callable[[], Awaitable[str | None]] | None = None,
        config_dir: Path | None = None,
    ) -> None:
        self._client = client if client is not None else HttpArrClient()
        self._sleep = sleep
        self._attempts = attempts
        self._retry_delay = retry_delay
        self._qbit = qbit if qbit is not None else HttpQbitClient()
        self._jellyfin = jellyfin if jellyfin is not None else HttpJellyfinServer()
        self._jellyfin_address = jellyfin_address
        self._config_dir = config_dir

    async def apply(
        self, app: CatalogApp, install: InstallState, login: SavedLogin
    ) -> LoginApplyResult:
        try:
            return await self._apply(app, install, login)
        except Exception as error:  # never raise - a login problem is never a crash
            logger.exception("login applier raised for %s", app.id)
            technical = _redact_password(
                f"{app.id}: {type(error).__name__}: {error}", login.password
            )
            return LoginApplyResult(ok=False, technical=technical)

    async def _apply(
        self, app: CatalogApp, install: InstallState, login: SavedLogin
    ) -> LoginApplyResult:
        if app.login_kind == "jellyfin":
            return await self._apply_jellyfin(app, login)

        if app.login_kind == "qbittorrent":
            return await self._apply_qbittorrent(app, install, login)

        if app.login_kind != "arr":
            return LoginApplyResult(ok=False, technical=f"{app.id}: does not take a login")

        api_key = install.api_keys.get(app.id)
        if not api_key:
            return LoginApplyResult(ok=False, technical=f"{app.id}: no API key generated yet")

        base_url = app_base_url(app, install.app_ids)
        path = f"{app.api_base}/{_CONFIG_HOST_PATH}"

        get_response = await self._request_with_retry("GET", base_url, path, api_key)
        if not get_response.ok:
            technical = _failure_technical(app.id, "GET", path, get_response)
            return LoginApplyResult(ok=False, technical=_redact_password(technical, login.password))

        if not isinstance(get_response.payload, dict):
            return LoginApplyResult(
                ok=False, technical=f"{app.id}: GET {path} returned an unexpected payload shape"
            )

        body: dict[str, object] = dict(get_response.payload)
        body["authenticationMethod"] = "forms"
        body["authenticationRequired"] = "enabled"
        body["username"] = login.username
        body["password"] = login.password
        body["passwordConfirmation"] = login.password

        put_response = await self._request_with_retry(
            "PUT", base_url, path, api_key, json_body=body
        )
        if not put_response.ok:
            technical = _failure_technical(app.id, "PUT", path, put_response)
            return LoginApplyResult(ok=False, technical=_redact_password(technical, login.password))

        return LoginApplyResult(ok=True, technical=None)

    async def _apply_qbittorrent(
        self, app: CatalogApp, install: InstallState, login: SavedLogin
    ) -> LoginApplyResult:
        """qBittorrent takes the login through its own API key, never a
        cookie session and never its own old password: one POST of
        `web_ui_username`/`web_ui_password`, hashed server-side
        (`appcontroller.cpp:907-921`). This is what lets Change and the
        forgotten-password reset work with no password qBittorrent already
        has - the key is the only credential Marrquee ever needs to hold.
        """
        api_key = install.api_keys.get(app.id)
        if not api_key:
            return LoginApplyResult(ok=False, technical=f"{app.id}: no API key generated yet")

        base_url = app_base_url(app, install.app_ids)
        path = f"{app.api_base}/app/setPreferences"
        form = preferences_form(
            {"web_ui_username": login.username, "web_ui_password": login.password}
        )

        response = await self._qbit_request_with_retry(base_url, path, api_key, form)
        if not response.ok:
            technical = _qbit_failure_technical(app.id, path, response)
            return LoginApplyResult(ok=False, technical=_redact_password(technical, login.password))

        return LoginApplyResult(ok=True, technical=None)

    async def _apply_jellyfin(self, app: CatalogApp, login: SavedLogin) -> LoginApplyResult:
        """Jellyfin takes the login through its admin API key, never its
        own current password: a rename, a password reset, then a sign-in
        to prove it took - the exact "no old password needed" contract
        this applier already gives every other login-taking app. No
        restart follows - the key alone is enough for Jellyfin to accept
        the new login next time it's asked.
        """
        if self._jellyfin_address is None:
            return LoginApplyResult(ok=False, technical=f"{app.id}: no address")
        address = await self._jellyfin_address()
        if address is None:
            return LoginApplyResult(ok=False, technical=f"{app.id}: no address")
        if self._config_dir is None:
            return LoginApplyResult(ok=False, technical=f"{app.id}: no config dir")

        record = load_jellyfin(self._config_dir)
        if record is None:
            return LoginApplyResult(ok=False, technical=f"{app.id}: no Jellyfin key saved")

        base_url = jellyfin_base_url(address)
        api_key = record.api_key
        admin_id = record.admin_id
        user_path = f"/Users/{admin_id}"

        get_response = await self._jellyfin_request_with_retry("GET", base_url, user_path, api_key)
        if not get_response.ok:
            technical = _jellyfin_failure_technical(app.id, "GET", user_path, get_response)
            return LoginApplyResult(ok=False, technical=_redact_password(technical, login.password))
        if not isinstance(get_response.payload, dict):
            return LoginApplyResult(
                ok=False, technical=f"{app.id}: GET {user_path} bad payload shape"
            )

        user: dict[str, object] = dict(get_response.payload)
        if user.get("Name") != login.username:
            user["Name"] = login.username
            rename_response = await self._jellyfin_request_with_retry(
                "POST", base_url, "/Users", api_key, params=[("userId", admin_id)], json_body=user
            )
            if not rename_response.ok:
                technical = _jellyfin_failure_technical(app.id, "POST", "/Users", rename_response)
                return LoginApplyResult(
                    ok=False, technical=_redact_password(technical, login.password)
                )

        password_response = await self._jellyfin_request_with_retry(
            "POST",
            base_url,
            "/Users/Password",
            api_key,
            params=[("userId", admin_id)],
            json_body={"NewPw": login.password},
        )
        if not password_response.ok:
            technical = _jellyfin_failure_technical(
                app.id, "POST", "/Users/Password", password_response
            )
            return LoginApplyResult(ok=False, technical=_redact_password(technical, login.password))

        verify_response = await self._jellyfin_request_with_retry(
            "POST",
            base_url,
            "/Users/AuthenticateByName",
            None,
            json_body={"Username": login.username, "Pw": login.password},
        )
        if not verify_response.ok:
            technical = _jellyfin_failure_technical(
                app.id, "POST", "/Users/AuthenticateByName", verify_response
            )
            return LoginApplyResult(ok=False, technical=_redact_password(technical, login.password))

        session_token: str | None = None
        if isinstance(verify_response.payload, dict):
            token = verify_response.payload.get("AccessToken")
            if isinstance(token, str) and token:
                session_token = token
        if session_token is not None:
            # Best effort - the login already verified, so a failed logout
            # never changes the outcome.
            await self._jellyfin_request_with_retry(
                "POST", base_url, "/Sessions/Logout", session_token
            )

        return LoginApplyResult(ok=True, technical=None)

    async def _jellyfin_request_with_retry(
        self,
        method: Literal["GET", "POST"],
        base_url: str,
        path: str,
        token: str | None,
        *,
        params: Sequence[tuple[str, str]] = (),
        json_body: object | None = None,
    ) -> JellyfinResponse:
        response = await self._jellyfin.request(
            method, base_url, path, token=token, params=params, json_body=json_body
        )
        attempt = 1
        while _is_transient(response.status) and attempt < self._attempts:
            await self._sleep(self._retry_delay)
            response = await self._jellyfin.request(
                method, base_url, path, token=token, params=params, json_body=json_body
            )
            attempt += 1
        return response

    async def _qbit_request_with_retry(
        self, base_url: str, path: str, api_key: str, form: Mapping[str, str]
    ) -> QbitResponse:
        response = await self._qbit.request("POST", base_url, path, api_key, form=form)
        attempt = 1
        while _is_transient(response.status) and attempt < self._attempts:
            await self._sleep(self._retry_delay)
            response = await self._qbit.request("POST", base_url, path, api_key, form=form)
            attempt += 1
        return response

    async def _request_with_retry(
        self,
        method: Literal["GET", "PUT"],
        base_url: str,
        path: str,
        api_key: str,
        *,
        json_body: object | None = None,
    ) -> ArrResponse:
        response = await self._client.request(method, base_url, path, api_key, json_body=json_body)
        attempt = 1
        while _is_transient(response.status) and attempt < self._attempts:
            await self._sleep(self._retry_delay)
            response = await self._client.request(
                method, base_url, path, api_key, json_body=json_body
            )
            attempt += 1
        return response

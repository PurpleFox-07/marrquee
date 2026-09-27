"""The wiring engine: turns the owner's chosen apps into an honest list of
steps, waits patiently for anything slow to answer, runs each step once
with bounded retries, and reports every step in plain words.

`plan_wiring` is pure - it never touches the network - so the full shape of
a run (which steps, in what order, "Step N of M") is provable with nothing
but an `InstallState`. `WiringEngine.run` is the only part that talks to
the apps, and it never raises: a wiring problem is not a deploy failure, so
every problem becomes an emitted step with `state="error"` instead of an
exception reaching the deploy engine that owns this seam.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, Literal, Protocol

from marrquee.catalog import (
    CATALOG,
    EXISTING_PLEX_APP_ID,
    JELLYFIN_APP_ID,
    PLEX_APP_ID,
    SEERR_APP_ID,
    CatalogApp,
    apps_in_order,
    media_server_of,
    require_port,
)
from marrquee.config import Settings
from marrquee.jellyfin import HttpJellyfinServer, JellyfinServer, jellyfin_base_url, load_jellyfin
from marrquee.plex import (
    ExistingPlex,
    HttpPlexServer,
    PlexServer,
    load_existing_plex,
    load_plex_account,
    plex_base_url,
)
from marrquee.qbittorrent import QBIT_BASE_PREFERENCES
from marrquee.questions import load_answers, uses_graphics_chip
from marrquee.recyclarr import quality_profile_name
from marrquee.seeding import seeding_preferences
from marrquee.seen_host import load_seen_host
from marrquee.seerr import HttpSeerrClient, SeerrClient, seerr_base_url
from marrquee.state import InstallState
from marrquee.storage import container_media_path, host_media_path
from marrquee.vpn_control import GluetunControl, NoGluetunControl
from marrquee.wiring import WiringStep, WiringStepState
from marrquee.wiring.arr_client import ArrClient, HttpArrClient
from marrquee.wiring.jellyfin_steps import ensure_jellyfin_graphics, ensure_jellyfin_libraries
from marrquee.wiring.plex_steps import (
    ensure_existing_plex_libraries,
    ensure_plex_direct_play,
    ensure_plex_libraries,
)
from marrquee.wiring.qbit_client import HttpQbitClient, QbitClient
from marrquee.wiring.seerr_steps import (
    SeerrPlexTarget,
    ensure_seerr_arr,
    ensure_seerr_media_server,
    seerr_plex_target,
)
from marrquee.wiring.steps import (
    StepOutcome,
    app_base_url,
    ensure_application,
    ensure_download_client,
    ensure_qbit_category,
    ensure_qbit_preferences,
    ensure_root_folder,
)
from marrquee.words import (
    MEDIA_FOLDER_LABEL,
    WIRING_CHIP_DONE,
    WIRING_CHIP_ERROR,
    WIRING_CHIP_RUNNING,
    WIRING_CHIP_SKIPPED,
    WIRING_EXISTING_PLEX_MISSING,
    WIRING_JELLYFIN_NOT_SET_UP,
    WIRING_LINE_JELLYFIN_GRAPHICS,
    WIRING_LINE_PLEX_DIRECT_PLAY,
    WIRING_NOTHING_TO_CONNECT,
    WIRING_PLEX_SIGN_IN_NEEDED,
    WIRING_SKIP_PROWLARR_ALONE,
    wiring_failure_unreachable,
    wiring_line_app_sync,
    wiring_line_download_client,
    wiring_line_downloader_settings,
    wiring_line_libraries,
    wiring_line_root_folder,
    wiring_line_seerr,
    wiring_note_still_waking,
    wiring_skip_no_prowlarr,
)

# Sonarr's and Radarr's own field name for "which qBittorrent category holds
# this app's downloads" - keyed by the catalog's own `media_folders` entries,
# the same key `words.MEDIA_FOLDER_LABEL` uses. Lives here, not in
# `wiring/steps.py`: that module never hardcodes which media folders exist.
_CATEGORY_FIELD_FOR_MEDIA_FOLDER: Final[Mapping[str, str]] = {
    "tv": "tvCategory",
    "movies": "movieCategory",
}

_CHIP_FOR_STATE: dict[WiringStepState, str] = {
    "running": WIRING_CHIP_RUNNING,
    "done": WIRING_CHIP_DONE,
    "skipped": WIRING_CHIP_SKIPPED,
    "error": WIRING_CHIP_ERROR,
}

_SYSTEM_STATUS_PATH = "system/status"


@dataclass(frozen=True)
class WiringContext:
    """Everything a task needs to actually run - built once per `run()` call.

    `qbit` and `vpn` are only ever read by qBittorrent's own tasks - an
    ordinary arr-only run never touches either. `answers` is read fresh at
    the start of every `run()` (see `WiringEngine.run`), never cached across
    runs, so a "Change seeding" saved a moment ago is what the very next
    wiring run actually applies. `plex_base_url`/`plex_token` and
    `jellyfin_base_url`/`jellyfin_key` are each resolved once per run, only
    when a Plex or Jellyfin task is actually planned - all four stay None
    otherwise, and a plan with neither task never reads `plex`/`jellyfin`
    at all. `existing_plex` is the owner's own Plex's saved record, loaded
    the same way - only when an existing-Plex task is planned. `settings`
    and `config_dir` are what that task needs to probe a folder and save
    its own updated record back. `seerr_gateway` is Seerr's own container's
    Docker gateway (never Marrquee's own, which `plex_base_url` already
    covers) and `seerr_host` is the owner's last-seen address - both
    resolved once per run, only when a Seerr task is actually planned.
    """

    client: ArrClient
    state: InstallState
    apps: Mapping[str, CatalogApp]
    qbit: QbitClient
    vpn: GluetunControl
    answers: Mapping[str, Mapping[str, str]]
    plex: PlexServer | None = None
    plex_base_url: str | None = None
    plex_token: str | None = None
    jellyfin: JellyfinServer | None = None
    jellyfin_base_url: str | None = None
    jellyfin_key: str | None = None
    existing_plex: ExistingPlex | None = None
    settings: Settings | None = None
    config_dir: Path | None = None
    seerr: SeerrClient | None = None
    seerr_gateway: str | None = None
    seerr_host: str | None = None


class WiringTask(Protocol):
    """One connection this run might make - or explain why it can't yet.

    Knows nothing about ordering, waiting, retrying or reporting - the
    engine owns all of that. `involved` doubles as the list of apps the
    engine proves ready before calling `apply`, in the order the mockup
    expects the posters to light up in (subject, then object). `about` is
    a separate, wider list: every app this step concerns, whether or not
    anything gets proven ready or written for it. Filtering on `involved`
    would silently drop a graceful "nothing to sync yet" explanation, whose
    `involved` is always empty.
    """

    # Declared as read-only properties, not plain attributes - every real
    # task is a frozen dataclass, and mypy treats a frozen field as
    # read-only, which only satisfies a Protocol that asks for the same.
    @property
    def key(self) -> str: ...

    @property
    def line(self) -> str: ...

    @property
    def involved(self) -> tuple[str, ...]: ...

    @property
    def about(self) -> tuple[str, ...]: ...

    async def apply(self, ctx: WiringContext) -> StepOutcome: ...


@dataclass(frozen=True)
class AppSyncTask:
    """Prowlarr gains, or repairs, a full-sync application entry for one partner."""

    key: str
    line: str
    involved: tuple[str, ...]
    prowlarr_id: str
    target_id: str

    @property
    def about(self) -> tuple[str, ...]:
        return self.involved

    async def apply(self, ctx: WiringContext) -> StepOutcome:
        prowlarr = ctx.apps[self.prowlarr_id]
        target = ctx.apps[self.target_id]
        return await ensure_application(
            ctx.client,
            prowlarr,
            ctx.state.api_keys[self.prowlarr_id],
            target,
            ctx.state.api_keys[self.target_id],
        )


@dataclass(frozen=True)
class RootFolderTask:
    """One app gains, or already has, one library folder."""

    key: str
    line: str
    involved: tuple[str, ...]
    app_id: str
    media_folder: str
    host_path: PurePosixPath

    @property
    def about(self) -> tuple[str, ...]:
        return self.involved

    async def apply(self, ctx: WiringContext) -> StepOutcome:
        app = ctx.apps[self.app_id]
        return await ensure_root_folder(
            ctx.client,
            app,
            ctx.state.api_keys[self.app_id],
            container_path=container_media_path(self.media_folder),
            host_path=self.host_path,
        )


@dataclass(frozen=True)
class QbitSettingsTask:
    """qBittorrent's own global preferences: the base folder/UPnP defaults,
    the owner's saved seeding choice, and the VPN's forwarded port when the
    tunnel has one.

    `involved` names only qBittorrent - the app was already proved ready by
    its own bring-up (the key call answering `app/version`), so there is
    nothing else for the engine to wait on before `apply` runs.

    The forwarded port is only ever asked for while Gluetun is actually
    part of this install: `with_app_removed` keeps a departed app's own
    key in `api_keys` (Story 5), so a stale `api_keys["gluetun"]` must
    never be read once gluetun has left `app_ids` - that would call a
    Gluetun that no longer exists.
    """

    key: str
    line: str
    involved: tuple[str, ...]

    @property
    def about(self) -> tuple[str, ...]:
        return self.involved

    async def apply(self, ctx: WiringContext) -> StepOutcome:
        app_id = self.involved[0]
        app = ctx.apps[app_id]
        api_key = ctx.state.api_keys[app_id]

        prefs: dict[str, object] = {
            **QBIT_BASE_PREFERENCES,
            **seeding_preferences(ctx.answers.get(app_id)),
        }
        gluetun_key = ctx.state.api_keys.get("gluetun")
        if "gluetun" in ctx.state.app_ids and gluetun_key is not None:
            port = await ctx.vpn.forwarded_port(gluetun_key)
            if isinstance(port, int):
                prefs["listen_port"] = port

        return await ensure_qbit_preferences(
            ctx.qbit, app, api_key, prefs, present=ctx.state.app_ids
        )


@dataclass(frozen=True)
class DownloadClientTask:
    """One partner's (Sonarr's or Radarr's) connection to qBittorrent: its
    own category first, so Automatic Torrent Management has somewhere to
    save into, then the download-client entry itself.

    `involved` is `(partner_id, downloader_id)` - subject then object, the
    same order `AppSyncTask` uses - so the partner's poster proves ready
    before qBittorrent's own (already-proved) poster lights up next to it.
    """

    key: str
    line: str
    involved: tuple[str, ...]
    media_folder: str
    category_field: str

    @property
    def about(self) -> tuple[str, ...]:
        return self.involved

    async def apply(self, ctx: WiringContext) -> StepOutcome:
        partner_id, downloader_id = self.involved
        partner = ctx.apps[partner_id]
        downloader = ctx.apps[downloader_id]
        save_path = f"/data/torrents/{self.media_folder}"

        category_outcome = await ensure_qbit_category(
            ctx.qbit,
            downloader,
            ctx.state.api_keys[downloader_id],
            media_folder=self.media_folder,
            save_path=save_path,
            present=ctx.state.app_ids,
        )
        if category_outcome.state == "error":
            return category_outcome

        client_outcome = await ensure_download_client(
            ctx.client,
            partner,
            ctx.state.api_keys[partner_id],
            downloader,
            ctx.state.api_keys[downloader_id],
            category_field=self.category_field,
            category=self.media_folder,
            present=ctx.state.app_ids,
        )
        if client_outcome.state == "error" or not category_outcome.changed:
            return client_outcome

        # The category write changed something even though the client
        # entry itself was already correct - the step as a whole still
        # counts as "changed", so the owner sees it did something.
        return StepOutcome(
            state=client_outcome.state,
            note=None,
            technical=client_outcome.technical,
            changed=True,
            transient=client_outcome.transient,
        )


def _plex_precheck(ctx: WiringContext) -> StepOutcome | None:
    """The one check both Plex tasks make before calling their own `ensure_*`
    function - neither ever gets a real address or token to hand it a bare
    `None`. Not an outcome from Plex itself, so it's never transient: the
    engine's own retry budget has nothing to gain from repeating it.
    """
    if ctx.plex_base_url is None:
        return StepOutcome(
            state="error",
            note=wiring_failure_unreachable("Plex"),
            technical="no address for plex",
            changed=False,
            transient=False,
        )
    if ctx.plex_token is None:
        return StepOutcome(
            state="error",
            note=WIRING_PLEX_SIGN_IN_NEEDED,
            technical="no plex sign-in saved",
            changed=False,
            transient=False,
        )
    return None


@dataclass(frozen=True)
class PlexLibrariesTask:
    """Plex gains its Movies and TV Shows libraries, pointing at the shared
    data root's own media folders.
    """

    key: str
    line: str
    involved: tuple[str, ...]

    @property
    def about(self) -> tuple[str, ...]:
        return self.involved

    async def apply(self, ctx: WiringContext) -> StepOutcome:
        outcome = _plex_precheck(ctx)
        if outcome is not None:
            return outcome
        assert ctx.plex is not None
        assert ctx.plex_base_url is not None
        assert ctx.plex_token is not None
        return await ensure_plex_libraries(ctx.plex, ctx.plex_base_url, ctx.plex_token)


@dataclass(frozen=True)
class PlexDirectPlayTask:
    """Plex's server-wide "never transcode video" setting, read back to
    prove it actually took - the owner's Plex Pass status may gate it.
    """

    key: str
    line: str
    involved: tuple[str, ...]

    @property
    def about(self) -> tuple[str, ...]:
        return self.involved

    async def apply(self, ctx: WiringContext) -> StepOutcome:
        outcome = _plex_precheck(ctx)
        if outcome is not None:
            return outcome
        assert ctx.plex is not None
        assert ctx.plex_base_url is not None
        assert ctx.plex_token is not None
        return await ensure_plex_direct_play(ctx.plex, ctx.plex_base_url, ctx.plex_token)


def _jellyfin_precheck(ctx: WiringContext) -> StepOutcome | None:
    """The one check both Jellyfin tasks make before calling their own
    `ensure_*` function - mirrors `_plex_precheck`. Never transient: the
    engine's own retry budget has nothing to gain from repeating a check
    that never talks to Jellyfin at all.
    """
    if ctx.jellyfin_base_url is None:
        return StepOutcome(
            state="error",
            note=wiring_failure_unreachable("Jellyfin"),
            technical="no address for jellyfin",
            changed=False,
            transient=False,
        )
    if ctx.jellyfin_key is None:
        return StepOutcome(
            state="error",
            note=WIRING_JELLYFIN_NOT_SET_UP,
            technical="no jellyfin key saved",
            changed=False,
            transient=False,
        )
    return None


@dataclass(frozen=True)
class JellyfinLibrariesTask:
    """Jellyfin gains its Movies and TV Shows libraries, pointing at the
    shared data root's own media folders.
    """

    key: str
    line: str
    involved: tuple[str, ...]

    @property
    def about(self) -> tuple[str, ...]:
        return self.involved

    async def apply(self, ctx: WiringContext) -> StepOutcome:
        outcome = _jellyfin_precheck(ctx)
        if outcome is not None:
            return outcome
        assert ctx.jellyfin is not None
        assert ctx.jellyfin_base_url is not None
        assert ctx.jellyfin_key is not None
        return await ensure_jellyfin_libraries(
            ctx.jellyfin, ctx.jellyfin_base_url, ctx.jellyfin_key
        )


@dataclass(frozen=True)
class JellyfinGraphicsTask:
    """Jellyfin's VA-API hardware-transcode setting, read back to prove it
    actually took - planned only when the owner said yes to the graphics
    chip question.
    """

    key: str
    line: str
    involved: tuple[str, ...]

    @property
    def about(self) -> tuple[str, ...]:
        return self.involved

    async def apply(self, ctx: WiringContext) -> StepOutcome:
        outcome = _jellyfin_precheck(ctx)
        if outcome is not None:
            return outcome
        assert ctx.jellyfin is not None
        assert ctx.jellyfin_base_url is not None
        assert ctx.jellyfin_key is not None
        return await ensure_jellyfin_graphics(ctx.jellyfin, ctx.jellyfin_base_url, ctx.jellyfin_key)


def _existing_plex_precheck(ctx: WiringContext) -> StepOutcome | None:
    """The two things this run needs before it can even ask the owner's own
    Plex a question - mirrors `_plex_precheck`/`_jellyfin_precheck`. Neither
    check ever talks to that Plex, so neither is ever transient.
    """
    if ctx.plex is None or ctx.settings is None:
        return StepOutcome(
            state="error",
            note=wiring_failure_unreachable("Plex"),
            technical="no plex server or settings",
            changed=False,
            transient=False,
        )
    if ctx.existing_plex is None or ctx.config_dir is None:
        return StepOutcome(
            state="error",
            note=WIRING_EXISTING_PLEX_MISSING,
            technical="no saved existing-plex record",
            changed=False,
            transient=False,
        )
    return None


@dataclass(frozen=True)
class ExistingPlexLibrariesTask:
    """The owner's own, already-running Plex gains "Movies (Marrquee)" and
    "TV Shows (Marrquee)", each only where that Plex can actually see the
    folder - proven fresh every run, since what it can see may change
    between one wiring run and the next.
    """

    key: str
    line: str
    involved: tuple[str, ...]

    @property
    def about(self) -> tuple[str, ...]:
        return self.involved

    async def apply(self, ctx: WiringContext) -> StepOutcome:
        outcome = _existing_plex_precheck(ctx)
        if outcome is not None:
            return outcome
        assert ctx.plex is not None
        assert ctx.settings is not None
        assert ctx.existing_plex is not None
        assert ctx.config_dir is not None
        root = PurePosixPath(ctx.state.storage_root or "")
        return await ensure_existing_plex_libraries(
            ctx.plex, ctx.existing_plex, ctx.settings, root, ctx.config_dir
        )


def _seerr_client_precheck(ctx: WiringContext) -> StepOutcome | None:
    """The one check every Seerr task makes before it ever calls Seerr -
    mirrors `_plex_precheck`/`_jellyfin_precheck`. Never transient: nothing
    here talks to Seerr at all.
    """
    if ctx.seerr is None or SEERR_APP_ID not in ctx.state.api_keys:
        return StepOutcome(
            state="error",
            note=wiring_failure_unreachable("Seerr"),
            technical="no seerr client or key",
            changed=False,
            transient=False,
        )
    return None


def _seerr_media_precheck(ctx: WiringContext, media_id: str) -> StepOutcome | None:
    """The extra checks only Seerr's media-server task needs, on top of
    `_seerr_client_precheck`: the owner's own Plex record when that's the
    media server in play, and a gateway address for anything Seerr must
    reach on the host network (every media server except a remote or
    https-only existing Plex).
    """
    outcome = _seerr_client_precheck(ctx)
    if outcome is not None:
        return outcome

    needs_gateway = True
    if media_id == EXISTING_PLEX_APP_ID:
        if ctx.existing_plex is None:
            return StepOutcome(
                state="error",
                note=WIRING_EXISTING_PLEX_MISSING,
                technical="no saved existing-plex record",
                changed=False,
                transient=False,
            )
        needs_gateway = ctx.existing_plex.on_this_nas

    if needs_gateway and ctx.seerr_gateway is None:
        return StepOutcome(
            state="error",
            note=wiring_failure_unreachable("Seerr"),
            technical="no address for seerr's gateway",
            changed=False,
            transient=False,
        )
    return None


async def _seerr_plex_expected_machine_id(ctx: WiringContext) -> str | None:
    """The machine id Marrquee's OWN (already-working) connection to Plex
    reports, so a Seerr pointed at the wrong Plex can be told apart from one
    that's merely still starting up. `None` when Marrquee's own connection
    isn't resolved either - Seerr's write is still attempted; only the
    read-back comparison is skipped.
    """
    if ctx.plex is None or ctx.plex_base_url is None:
        return None
    identity = await ctx.plex.identity(ctx.plex_base_url)
    return identity.machine_id if identity is not None else None


@dataclass(frozen=True)
class SeerrMediaServerTask:
    """Seerr's connection to whichever media server the install actually
    has - new Plex, new Jellyfin, or the owner's own already-running Plex -
    plus every movie/TV library it can see there.
    """

    key: str
    line: str
    involved: tuple[str, ...]

    @property
    def about(self) -> tuple[str, ...]:
        return self.involved

    async def apply(self, ctx: WiringContext) -> StepOutcome:
        media_id = self.involved[1]
        outcome = _seerr_media_precheck(ctx, media_id)
        if outcome is not None:
            return outcome
        assert ctx.seerr is not None

        media_app = ctx.apps[media_id]
        server, target, expected_machine_id = await self._resolve_target(ctx, media_id, media_app)
        return await ensure_seerr_media_server(
            ctx.seerr,
            seerr_base_url(),
            ctx.state.api_keys[SEERR_APP_ID],
            server,
            plex_target=target,
            expected_machine_id=expected_machine_id,
            owner_host=ctx.seerr_host,
            name=media_app.name,
        )

    @staticmethod
    async def _resolve_target(
        ctx: WiringContext, media_id: str, media_app: CatalogApp
    ) -> tuple[Literal["plex", "jellyfin"], SeerrPlexTarget | None, str | None]:
        """Which server Seerr connects as, the address it reaches it at, and
        (Plex only) the machine id that address should answer with -
        resolved from Seerr's own network position, never Marrquee's.
        """
        if media_id == JELLYFIN_APP_ID:
            gateway = ctx.seerr_gateway
            target = (
                SeerrPlexTarget(ip=gateway, port=require_port(media_app), use_ssl=False)
                if gateway is not None
                else None
            )
            return "jellyfin", target, None

        if media_id == PLEX_APP_ID:
            gateway = ctx.seerr_gateway
            target = (
                SeerrPlexTarget(ip=gateway, port=require_port(media_app), use_ssl=False)
                if gateway is not None
                else None
            )
            machine_id = await _seerr_plex_expected_machine_id(ctx)
            return "plex", target, machine_id

        assert ctx.existing_plex is not None  # the precheck already proved this
        record = ctx.existing_plex
        if record.on_this_nas:
            gateway = ctx.seerr_gateway
            target = (
                SeerrPlexTarget(ip=gateway, port=record.port, use_ssl=False)
                if gateway is not None
                else None
            )
        else:
            target = seerr_plex_target(record.base_url)
        return "plex", target, record.machine_id


@dataclass(frozen=True)
class SeerrArrTask:
    """Seerr's connection to one installed Sonarr or Radarr - the known
    key, its own library folder, and a quality profile.
    """

    key: str
    line: str
    involved: tuple[str, ...]

    @property
    def about(self) -> tuple[str, ...]:
        return self.involved

    async def apply(self, ctx: WiringContext) -> StepOutcome:
        outcome = _seerr_client_precheck(ctx)
        if outcome is not None:
            return outcome
        assert ctx.seerr is not None

        arr_id = self.involved[1]
        arr = ctx.apps[arr_id]
        preferred_profile = (
            quality_profile_name(arr_id, ctx.answers) if "recyclarr" in ctx.state.app_ids else None
        )
        return await ensure_seerr_arr(
            ctx.seerr,
            seerr_base_url(),
            ctx.state.api_keys[SEERR_APP_ID],
            arr,
            ctx.state.api_keys[arr_id],
            preferred_profile=preferred_profile,
            owner_host=ctx.seerr_host,
        )


@dataclass(frozen=True)
class ExplainTask:
    """A step whose outcome is always `skipped`, with a fixed plain-language note.

    Covers the two graceful "nothing to sync yet" cases: a partner chosen
    without Prowlarr, and Prowlarr chosen with no partner. `involved` is
    empty - there is no app to prove ready and nothing gets written - but
    `about` still names the app this explanation concerns, so filtering an
    add to `only_app` keeps it instead of silently dropping it.
    """

    key: str
    line: str
    involved: tuple[str, ...]
    about: tuple[str, ...]
    note: str

    async def apply(self, ctx: WiringContext) -> StepOutcome:
        return StepOutcome(
            state="skipped", note=self.note, technical=None, changed=False, transient=False
        )


def plan_wiring(
    state: InstallState,
    *,
    only_app: str | None = None,
    answers: Mapping[str, Mapping[str, str]] = {},
) -> tuple[WiringTask, ...]:
    """The honest list of steps the owner's chosen apps justify. Pure - no client.

    Application-sync steps (or their graceful explanations) come first, in
    catalog order; then one root-folder step per media folder of each
    chosen app, also in catalog order. Input order never matters, the same
    way `apps_in_order` already guarantees for folder creation and compose.

    `only_app` narrows the result to the steps *about* that one app (used
    by an add or a reconnect), keeping the same relative order. `None`
    (the default) returns every step, unchanged from before this parameter
    existed. `answers` decides only whether Jellyfin's graphics task is
    planned at all - an install with no Jellyfin never reads it, so the
    plan for every other combination of apps is byte-identical to before
    this parameter existed.
    """
    apps = apps_in_order(state.app_ids)
    prowlarr = next((app for app in apps if app.id == "prowlarr"), None)
    partners = [app for app in apps if app.id != "prowlarr" and app.media_folders]

    tasks: list[WiringTask] = []

    if prowlarr is not None:
        if partners:
            for partner in partners:
                tasks.append(
                    AppSyncTask(
                        key=f"app-sync:{partner.id}",
                        line=wiring_line_app_sync(prowlarr.name, partner.name),
                        involved=(prowlarr.id, partner.id),
                        prowlarr_id=prowlarr.id,
                        target_id=partner.id,
                    )
                )
        else:
            tasks.append(
                ExplainTask(
                    key="prowlarr-alone",
                    line=WIRING_SKIP_PROWLARR_ALONE,
                    involved=(),
                    about=("prowlarr",),
                    note=WIRING_SKIP_PROWLARR_ALONE,
                )
            )
    else:
        for partner in partners:
            note = wiring_skip_no_prowlarr(partner.name)
            tasks.append(
                ExplainTask(
                    key=f"no-prowlarr:{partner.id}",
                    line=note,
                    involved=(),
                    about=(partner.id,),
                    note=note,
                )
            )

    downloader = next((app for app in apps if app.id == "qbittorrent"), None)
    if downloader is not None:
        tasks.append(
            QbitSettingsTask(
                key=f"downloader-settings:{downloader.id}",
                line=wiring_line_downloader_settings(downloader.name),
                involved=(downloader.id,),
            )
        )
        for partner in partners:
            media_folder = partner.media_folders[0]
            tasks.append(
                DownloadClientTask(
                    key=f"download-client:{partner.id}",
                    line=wiring_line_download_client(partner.name, downloader.name),
                    involved=(partner.id, downloader.id),
                    media_folder=media_folder,
                    category_field=_CATEGORY_FIELD_FOR_MEDIA_FOLDER[media_folder],
                )
            )

    for app in apps:
        for media_folder in app.media_folders:
            tasks.append(
                RootFolderTask(
                    key=f"root-folder:{app.id}",
                    line=wiring_line_root_folder(app.name, MEDIA_FOLDER_LABEL[media_folder]),
                    involved=(app.id,),
                    app_id=app.id,
                    media_folder=media_folder,
                    host_path=host_media_path(state.storage_root or "", media_folder),
                )
            )

    if "plex" in state.app_ids:
        tasks.append(
            PlexLibrariesTask(
                key="libraries:plex", line=wiring_line_libraries("Plex"), involved=("plex",)
            )
        )
        tasks.append(
            PlexDirectPlayTask(
                key="direct-play:plex", line=WIRING_LINE_PLEX_DIRECT_PLAY, involved=("plex",)
            )
        )

    if "jellyfin" in state.app_ids:
        tasks.append(
            JellyfinLibrariesTask(
                key="libraries:jellyfin",
                line=wiring_line_libraries("Jellyfin"),
                involved=("jellyfin",),
            )
        )
        if uses_graphics_chip(answers):
            tasks.append(
                JellyfinGraphicsTask(
                    key="graphics:jellyfin",
                    line=WIRING_LINE_JELLYFIN_GRAPHICS,
                    involved=("jellyfin",),
                )
            )

    if "existing-plex" in state.app_ids:
        tasks.append(
            ExistingPlexLibrariesTask(
                key="libraries:existing-plex",
                line=wiring_line_libraries("Plex"),
                involved=("existing-plex",),
            )
        )

    if SEERR_APP_ID in state.app_ids:
        media_app = media_server_of(state.app_ids)
        if media_app is not None:
            tasks.append(
                SeerrMediaServerTask(
                    key="media-server:seerr",
                    line=wiring_line_seerr(media_app.name),
                    involved=(SEERR_APP_ID, media_app.id),
                )
            )
        for app in apps:
            if app.id in ("sonarr", "radarr"):
                tasks.append(
                    SeerrArrTask(
                        key=f"seerr:{app.id}",
                        line=wiring_line_seerr(app.name),
                        involved=(SEERR_APP_ID, app.id),
                    )
                )

    if only_app is None:
        return tuple(tasks)
    return tuple(task for task in tasks if only_app in task.about)


class WiringEngine:
    """Plans, waits for, runs and reports every wiring step for one run.

    Every non-deterministic dependency - the HTTP client, the clock, the
    sleep function - is injected, the same shape as `DeployManager`, so a
    test drives a slow app, a retry or a timeout with no network and no
    real waiting.
    """

    def __init__(
        self,
        client: ArrClient | None = None,
        *,
        qbit: QbitClient | None = None,
        vpn: GluetunControl | None = None,
        config_dir: Path | None = None,
        plex: PlexServer | None = None,
        plex_address: Callable[[], Awaitable[str | None]] | None = None,
        jellyfin: JellyfinServer | None = None,
        jellyfin_address: Callable[[], Awaitable[str | None]] | None = None,
        settings: Settings | None = None,
        seerr: SeerrClient | None = None,
        seerr_address: Callable[[], Awaitable[str | None]] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        ready_timeout: float = 180.0,
        ready_interval: float = 2.0,
        reassure_after: float = 30.0,
        attempts: int = 4,
        retry_delay: float = 3.0,
    ) -> None:
        self._client = client if client is not None else HttpArrClient()
        self._qbit = qbit if qbit is not None else HttpQbitClient()
        self._vpn = vpn if vpn is not None else NoGluetunControl()
        self._config_dir = config_dir
        self._plex = plex if plex is not None else HttpPlexServer()
        self._plex_address = plex_address
        self._jellyfin = jellyfin if jellyfin is not None else HttpJellyfinServer()
        self._jellyfin_address = jellyfin_address
        self._settings = settings
        self._seerr = seerr if seerr is not None else HttpSeerrClient()
        self._seerr_address = seerr_address
        self._sleep = sleep
        self._clock = clock
        self._ready_timeout = ready_timeout
        self._ready_interval = ready_interval
        self._reassure_after = reassure_after
        self._attempts = attempts
        self._retry_delay = retry_delay

    async def run(
        self,
        state: InstallState,
        emit: Callable[[WiringStep], None],
        *,
        only_app: str | None = None,
    ) -> None:
        # Read fresh at the start of every run, never cached - a "Change
        # seeding" saved a moment ago must be what this very run applies,
        # and it also decides whether Jellyfin's graphics task belongs in
        # this run's plan at all.
        answers = load_answers(self._config_dir) if self._config_dir is not None else {}

        tasks = plan_wiring(state, only_app=only_app, answers=answers)
        if not tasks:
            emit(
                WiringStep(
                    index=1,
                    total=1,
                    key="nothing-to-connect",
                    line=WIRING_NOTHING_TO_CONNECT,
                    state="skipped",
                    chip=WIRING_CHIP_SKIPPED,
                    note=None,
                    technical=None,
                    involved=(),
                )
            )
            return

        # Resolved once per run, and only when a Plex task is actually
        # planned - an arr-only run never calls `plex_address()` or reads
        # plex.json at all.
        resolved_plex_base_url: str | None = None
        resolved_plex_token: str | None = None
        if any("plex" in task.about for task in tasks):
            address = await self._plex_address() if self._plex_address is not None else None
            if address is not None:
                resolved_plex_base_url = plex_base_url(address)
            if self._config_dir is not None:
                account = load_plex_account(self._config_dir)
                if account is not None:
                    resolved_plex_token = account.token

        # Same shape as Plex above, and for the same reason - an install
        # with no Jellyfin never calls `jellyfin_address()` or reads
        # jellyfin.json at all.
        resolved_jellyfin_base_url: str | None = None
        resolved_jellyfin_key: str | None = None
        if any("jellyfin" in task.about for task in tasks):
            jellyfin_address = (
                await self._jellyfin_address() if self._jellyfin_address is not None else None
            )
            if jellyfin_address is not None:
                resolved_jellyfin_base_url = jellyfin_base_url(jellyfin_address)
            if self._config_dir is not None:
                record = load_jellyfin(self._config_dir)
                if record is not None:
                    resolved_jellyfin_key = record.api_key

        # Same shape again - an install with no existing-Plex never reads
        # existing_plex.json at all, the same zero-cost guarantee Plex and
        # Jellyfin above already make for their own apps.
        resolved_existing_plex: ExistingPlex | None = None
        if any("existing-plex" in task.about for task in tasks) and self._config_dir is not None:
            resolved_existing_plex = load_existing_plex(self._config_dir)

        # Same shape again - an install with no Seerr never calls
        # `seerr_address()` or reads seen_host.json at all.
        resolved_seerr_gateway: str | None = None
        resolved_seerr_host: str | None = None
        if any("seerr" in task.about for task in tasks):
            resolved_seerr_gateway = (
                await self._seerr_address() if self._seerr_address is not None else None
            )
            if self._config_dir is not None:
                resolved_seerr_host = load_seen_host(self._config_dir)

        ctx = WiringContext(
            client=self._client,
            state=state,
            apps={app.id: app for app in CATALOG},
            qbit=self._qbit,
            vpn=self._vpn,
            answers=answers,
            plex=self._plex,
            plex_base_url=resolved_plex_base_url,
            plex_token=resolved_plex_token,
            jellyfin=self._jellyfin,
            jellyfin_base_url=resolved_jellyfin_base_url,
            jellyfin_key=resolved_jellyfin_key,
            existing_plex=resolved_existing_plex,
            settings=self._settings,
            config_dir=self._config_dir,
            seerr=self._seerr,
            seerr_gateway=resolved_seerr_gateway,
            seerr_host=resolved_seerr_host,
        )
        ready_apps: set[str] = set()
        total = len(tasks)
        for index, task in enumerate(tasks, start=1):
            await self._process_task(task, index, total, ctx, ready_apps, emit)

    async def _process_task(
        self,
        task: WiringTask,
        index: int,
        total: int,
        ctx: WiringContext,
        ready_apps: set[str],
        emit: Callable[[WiringStep], None],
    ) -> None:
        emit(self._frame(task, index, total, state="running", note=None))
        try:
            for app_id in task.involved:
                if app_id in ready_apps:
                    continue
                became_ready = await self._wait_until_ready(task, index, total, ctx, app_id, emit)
                if not became_ready:
                    name = ctx.apps[app_id].name
                    emit(
                        self._frame(
                            task,
                            index,
                            total,
                            state="error",
                            note=wiring_failure_unreachable(name),
                            technical=f"{task.key}: {app_id} never answered {_SYSTEM_STATUS_PATH}",
                        )
                    )
                    return
                ready_apps.add(app_id)

            outcome = await self._apply_with_retry(task, ctx)
        except Exception as error:  # a task must never take the whole run down with it
            name = ctx.apps[task.involved[0]].name if task.involved else task.key
            emit(
                self._frame(
                    task,
                    index,
                    total,
                    state="error",
                    note=wiring_failure_unreachable(name),
                    technical=f"{task.key}: {type(error).__name__}: {error}",
                )
            )
            return

        emit(
            self._frame(
                task,
                index,
                total,
                state=outcome.state,
                note=outcome.note,
                technical=f"{task.key}: {outcome.technical}" if outcome.technical else None,
            )
        )

    async def _wait_until_ready(
        self,
        task: WiringTask,
        index: int,
        total: int,
        ctx: WiringContext,
        app_id: str,
        emit: Callable[[WiringStep], None],
    ) -> bool:
        """Poll `system/status` until it answers 2xx, or the budget runs out.

        Every app is proved ready at most once per run - the caller only
        reaches this for an `app_id` not already in `ready_apps`. An app
        that isn't an arr app (qBittorrent, today) has no `system/status` to
        poll - its own bring-up already proved it ready (the key call
        answering `app/version`), so it counts as ready at once.
        """
        app = ctx.apps[app_id]
        if app.kind != "arr":
            return True

        api_key = ctx.state.api_keys.get(app_id, "")
        base_url = app_base_url(app, ctx.state.app_ids)
        path = f"{app.api_base}/{_SYSTEM_STATUS_PATH}"

        start = self._clock()
        reassured = False
        while True:
            response = await ctx.client.request("GET", base_url, path, api_key)
            if response.ok:
                return True

            elapsed = self._clock() - start
            if elapsed >= self._ready_timeout:
                return False

            if not reassured and elapsed >= self._reassure_after:
                reassured = True
                emit(
                    self._frame(
                        task,
                        index,
                        total,
                        state="running",
                        note=wiring_note_still_waking(app.name),
                    )
                )

            await self._sleep(self._ready_interval)

    async def _apply_with_retry(self, task: WiringTask, ctx: WiringContext) -> StepOutcome:
        """Call `task.apply` up to `self._attempts` times, but only while it
        keeps coming back transient - a considered "no" is never retried.
        """
        outcome = await task.apply(ctx)
        attempt = 1
        while outcome.transient and attempt < self._attempts:
            await self._sleep(self._retry_delay)
            outcome = await task.apply(ctx)
            attempt += 1
        return outcome

    @staticmethod
    def _frame(
        task: WiringTask,
        index: int,
        total: int,
        *,
        state: WiringStepState,
        note: str | None,
        technical: str | None = None,
    ) -> WiringStep:
        return WiringStep(
            index=index,
            total=total,
            key=task.key,
            line=task.line,
            state=state,
            chip=_CHIP_FOR_STATE[state],
            note=note,
            technical=technical,
            involved=task.involved,
        )
